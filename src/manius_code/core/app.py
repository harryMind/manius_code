import asyncio
import logging
import signal
import time
from typing import Any
from uuid import uuid4

from manius_code.core.agent.runner import AgentRunner
from manius_code.core.bus.commands import AgentRunCommand, AgentRunResult, EventSubscribeCommand, EventSubscribeResult, PingCommand, PongResult
from manius_code.core.config import ManiusConfig, load_config
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.logging import setup_logging
from manius_code.core.transport.socket_server import SocketServer

SERVER_VERSION = "0.0.1"
logger = logging.getLogger(__name__)


class CoreApp:
    # 记录 daemon 启动时刻以计算后续的运行时间。
    def __init__(self) -> None:
        self._started_at = time.monotonic()
        self._config: ManiusConfig | None = None
        self._event_broadcaster = IpcEventBroadcaster()
        self._agent_tasks: set[asyncio.Task[Any]] = set()

    # 处理 ping 命令并返回 daemon 版本和已运行时间。
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        PingCommand.model_validate(params)
        uptime_ms = round((time.monotonic() - self._started_at) * 1000)
        return PongResult(server=SERVER_VERSION, uptime_ms=uptime_ms)

    # 订阅当前客户端连接以接收后续 Agent 事件通知。
    async def _event_subscribe_handler(self, params: dict[str, Any], writer: asyncio.StreamWriter) -> EventSubscribeResult:
        EventSubscribeCommand.model_validate(params)
        self._event_broadcaster.subscribe(writer)
        return EventSubscribeResult()

    # 创建后台 Agent 任务并立即返回其可追踪的运行标识。
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        if self._config is None:
            raise RuntimeError("CoreApp configuration is not initialized")
        command = AgentRunCommand.model_validate(params)
        run_id = uuid4().hex
        runner = AgentRunner(self._config, event_subscribers=[self._event_broadcaster.handle])
        task = asyncio.create_task(runner.run(command.goal, run_id))
        self._agent_tasks.add(task)
        task.add_done_callback(self._record_agent_task_result)
        return AgentRunResult(run_id=run_id)

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
        server = SocketServer(config.host, config.port)
        # 注册handler以及需要tcp连接的handler
        server.register("core.ping", self._ping_handler)
        server.register_connection_handler("event.subscribe", self._event_subscribe_handler)
        server.register("agent.run", self._agent_run_handler)
        server.add_disconnect_handler(self._event_broadcaster.unsubscribe)

        await server.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except NotImplementedError:
                pass
        try:
            await stopped.wait()
        finally:
            await self._stop_agent_tasks()
            await server.stop()
            logger.info("manius-core stopped")


# 运行 manius-core 的命令行入口。
def main() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
