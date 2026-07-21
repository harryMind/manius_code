import asyncio
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from manius_code.core.agent.runner import AgentRunner
from manius_code.core.bus.commands import AgentRunCommand, AgentRunResult, EventListCommand, EventListResult, EventSubscribeCommand, EventSubscribeResult, EventUnsubscribeCommand, EventUnsubscribeResult, PingCommand, PongResult
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig, load_config
from manius_code.core.events.bus import EventBus
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.events.subscribers import EventWriter
from manius_code.core.logging import setup_logging
from manius_code.core.transport.socket_server import SocketServer
from manius_code.core.tracing import TracingProvider

SERVER_VERSION = "0.0.1"
logger = logging.getLogger(__name__)


class CoreApp:
    # 记录 daemon 启动时刻以计算后续的运行时间。
    def __init__(self) -> None:
        self._started_at = time.monotonic()
        self._config: ManiusConfig | None = None
        self._event_broadcaster = IpcEventBroadcaster()
        self._agent_tasks: set[asyncio.Task[Any]] = set()
        self._runs_dir = Path("runs")
        self._tracer: TracingProvider | None = None

    # 处理 ping 命令并返回 daemon 版本和已运行时间。
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        PingCommand.model_validate(params)
        uptime_ms = round((time.monotonic() - self._started_at) * 1000)
        return PongResult(server=SERVER_VERSION, uptime_ms=uptime_ms)

    # 订阅当前客户端连接以接收后续 Agent 事件通知。
    async def _event_subscribe_handler(self, params: dict[str, Any], writer: asyncio.StreamWriter) -> EventSubscribeResult:
        command = EventSubscribeCommand.model_validate(params)
        sub_id = self._event_broadcaster.subscribe(writer, command.run_id, command.topics)
        return EventSubscribeResult(sub_id=sub_id, run_id=command.run_id, topics=command.topics)

    # 按订阅标识取消当前客户端已有的事件订阅。
    async def _event_unsubscribe_handler(self, params: dict[str, Any]) -> EventUnsubscribeResult:
        command = EventUnsubscribeCommand.model_validate(params)
        return EventUnsubscribeResult(unsubscribed=self._event_broadcaster.unsubscribe(command.sub_id))

    # 从指定运行的 JSONL 文件读取持久化事件用于断线重连回放。
    async def _event_list_handler(self, params: dict[str, Any]) -> EventListResult:
        command = EventListCommand.model_validate(params)
        event_path = self._runs_dir / command.run_id / "events.jsonl"
        if not event_path.is_file():
            return EventListResult(run_id=command.run_id, events=[])
        events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        return EventListResult(run_id=command.run_id, events=events)

    # 创建后台 Agent 任务并立即返回其可追踪的运行标识。
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        if self._config is None:
            raise RuntimeError("CoreApp configuration is not initialized")
        run_id = uuid4().hex
        try:
            command = AgentRunCommand.model_validate(params)
            if not command.goal.strip():
                raise ValueError("goal must not be blank")
        except (ValueError, TypeError) as error:
            task = asyncio.create_task(self._publish_failed_run(run_id, str(error)))
        else:
            runner = AgentRunner(
                self._config,
                event_subscribers=[self._event_broadcaster.handle],
                tracer=self._tracer,
            )
            task = asyncio.create_task(runner.run(command.goal, run_id))
        self._agent_tasks.add(task)
        task.add_done_callback(self._record_agent_task_result)
        return AgentRunResult(run_id=run_id)

    # 为参数校验失败的远程任务持久化并广播完整的失败事件闭环。
    async def _publish_failed_run(self, run_id: str, reason: str) -> None:
        run_dir = self._runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        writer = EventWriter(run_dir / "events.jsonl")
        event_bus = EventBus(self._tracer)
        event_bus.subscribe(writer.handle)
        event_bus.subscribe(self._event_broadcaster.handle)
        try:
            await event_bus.publish(RunStartedEvent(run_id=run_id, goal="", run_dir=str(run_dir)))
            await event_bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status="failed",
                    total_steps=0,
                    duration_ms=0,
                    summary=reason,
                    reason=reason,
                )
            )
        finally:
            writer.close()

    # 回收已结束的后台任务并记录未处理的运行异常。
    def _record_agent_task_result(self, task: asyncio.Task[Any]) -> None:
        self._agent_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error("Agent task failed: %s", exception)

    # 取消并等待 daemon 停止时仍在执行的后台 Agent 任务。
    async def _stop_agent_tasks(self) -> None:
        tasks = tuple(self._agent_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 启动 daemon 并在收到终止信号后有序关闭服务器。
    async def run(self) -> None:
        config = load_config()
        self._config = config
        setup_logging(config)
        if config.trace.enabled:
            tracer = TracingProvider(
                config.trace.file,
                config.trace.max_queue_size,
                config.trace.max_size_mb,
                config.trace.backup_count,
            )
            try:
                await tracer.start()
            except OSError as error:
                logger.warning("Tracing is disabled because its file cannot be opened: %s", error)
            else:
                self._tracer = tracer
        self._event_broadcaster = IpcEventBroadcaster(self._tracer)
        server = SocketServer(config.host, config.port, tracer=self._tracer)
        # 注册handler以及需要tcp连接的handler
        server.register("core.ping", self._ping_handler)
        server.register_connection_handler("event.subscribe", self._event_subscribe_handler)
        server.register("event.unsubscribe", self._event_unsubscribe_handler)
        server.register("event.list", self._event_list_handler)
        server.register("agent.run", self._agent_run_handler)
        server.add_disconnect_handler(self._event_broadcaster.unsubscribe_writer)

        try:
            await server.start()
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, stopped.set)
                except NotImplementedError:
                    pass
            await stopped.wait()
        finally:
            await self._stop_agent_tasks()
            await server.stop()
            if self._tracer is not None:
                await self._tracer.stop()
            logger.info("manius-core stopped")


# 运行 manius-core 的命令行入口。
def main() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
