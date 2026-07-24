import asyncio
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from manius_code.core.agent.runner import AgentRunner
from manius_code.core.bus.commands import AgentResumeCommand, AgentRunCommand, AgentRunResult, EventListCommand, EventListResult, EventSubscribeCommand, EventSubscribeResult, EventUnsubscribeCommand, EventUnsubscribeResult, PingCommand, PongResult, SessionCreateCommand, SessionCreateResult, SessionDestroyCommand, SessionDestroyResult, SessionGetCommand, SessionGetResult, SessionListCommand, SessionListResult, SessionMetaResult, SessionSendCommand, SessionSendResult, SessionThreadEntryResult
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig, load_config
from manius_code.core.events.bus import EventBus
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.events.subscribers import EventWriter
from manius_code.core.logging import setup_logging
from manius_code.core.sessions.manager import SessionManager
from manius_code.core.sessions.models import SessionRunRequest
from manius_code.core.sessions.store import SessionStore
from manius_code.core.tools.defaults import default_tool_catalog
from manius_code.core.tools.note import NoteSaveTool
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
        self._active_run_ids: set[str] = set()
        self._runs_dir = Path("runs")
        self._tracer: TracingProvider | None = None
        self._session_manager: SessionManager | None = None

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
        self._track_agent_task(run_id, task)
        return AgentRunResult(run_id=run_id)

    # 恢复已停止且持久化计划仍未完成的任务，并拒绝同一运行的重复恢复。
    async def _agent_resume_handler(self, params: dict[str, Any]) -> AgentRunResult:
        if self._config is None:
            raise RuntimeError("CoreApp configuration is not initialized")
        command = AgentResumeCommand.model_validate(params)
        if command.run_id in self._active_run_ids:
            raise RuntimeError(f"run is already active: {command.run_id}")
        runner = AgentRunner(
            self._config,
            event_subscribers=[self._event_broadcaster.handle],
            tracer=self._tracer,
        )
        runner.validate_resume(command.run_id)
        task = asyncio.create_task(runner.resume(command.run_id))
        self._track_agent_task(command.run_id, task)
        return AgentRunResult(run_id=command.run_id)

    # 创建新会话并返回可供客户端后续持续交互使用的持久化元数据。
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        command = SessionCreateCommand.model_validate(params)
        meta = await self._require_session_manager().create_session(command.client_id)
        return SessionCreateResult(session=SessionMetaResult.model_validate(meta.model_dump()))

    # 向指定会话提交一轮用户目标并立即返回后台 Agent 运行标识。
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendResult:
        command = SessionSendCommand.model_validate(params)
        run_id = await self._require_session_manager().send_message(command.session_id, command.content)
        return SessionSendResult(session_id=command.session_id, run_id=run_id)

    # 返回会话元数据及其已沉淀的短期对话摘要，供客户端恢复交互界面。
    async def _session_get_handler(self, params: dict[str, Any]) -> SessionGetResult:
        command = SessionGetCommand.model_validate(params)
        meta, thread = await self._require_session_manager().get_session(command.session_id)
        return SessionGetResult(
            session=SessionMetaResult.model_validate(meta.model_dump()),
            thread=[SessionThreadEntryResult.model_validate(entry.model_dump()) for entry in thread],
        )

    # 列出 daemon 已持久化的所有会话，并保持会话管理器的最近活跃排序。
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        SessionListCommand.model_validate(params)
        sessions = await self._require_session_manager().list_sessions()
        return SessionListResult(sessions=[SessionMetaResult.model_validate(meta.model_dump()) for meta in sessions])

    # 释放指定会话的进程内状态但不删除其磁盘历史，支持后续跨连接恢复。
    async def _session_destroy_handler(self, params: dict[str, Any]) -> SessionDestroyResult:
        command = SessionDestroyCommand.model_validate(params)
        destroyed = await self._require_session_manager().destroy_session(command.session_id)
        return SessionDestroyResult(session_id=command.session_id, destroyed=destroyed)

    # 为会话运行器绑定上下文和 note_save 工具，复用独立任务的 AgentRunner 及事件出口。
    def _make_session_runner(self, request: SessionRunRequest) -> AgentRunner:
        if self._config is None:
            raise RuntimeError("CoreApp configuration is not initialized")
        manager = self._require_session_manager()

        # 将当前运行标识封装进笔记回调，避免工具层了解会话或运行生命周期。
        async def save_note(title: str, content: str, tags: list[str]):
            return await manager.save_note(request.session_id, title, content, tags, request.run_id)

        # 在新的目录实例上附加会话工具，确保普通独立运行不会意外获得会话能力。
        def session_tools(config: ManiusConfig):
            return default_tool_catalog(config).with_tool(NoteSaveTool(save_note))

        return AgentRunner(
            self._config,
            tool_factory=session_tools,
            event_subscribers=[self._event_broadcaster.handle],
            tracer=self._tracer,
            system_context=request.system_context,
        )

    # 返回已在 daemon 生命周期中初始化的会话管理器，防止处理器在启动前被错误调用。
    def _require_session_manager(self) -> SessionManager:
        if self._session_manager is None:
            raise RuntimeError("SessionManager is not initialized")
        return self._session_manager

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

    # 注册后台任务及其运行标识，供恢复接口识别正在执行的任务。
    def _track_agent_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        self._agent_tasks.add(task)
        self._active_run_ids.add(run_id)
        task.add_done_callback(lambda completed: self._record_agent_task_result(run_id, completed))

    # 回收结束任务、解除运行占用并记录未处理的后台异常。
    def _record_agent_task_result(self, run_id: str, task: asyncio.Task[Any]) -> None:
        self._agent_tasks.discard(task)
        self._active_run_ids.discard(run_id)
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
        self._session_manager = SessionManager(
            SessionStore(config.session.directory),
            self._make_session_runner,
            self._event_broadcaster.handle,
            thread_turn_limit=config.session.thread_turn_limit,
            notes_top_k=config.session.notes_top_k,
            tracer=self._tracer,
            task_observer=self._track_agent_task,
        )
        server = SocketServer(config.host, config.port, tracer=self._tracer)
        # 注册handler以及需要tcp连接的handler
        server.register("core.ping", self._ping_handler)
        server.register_connection_handler("event.subscribe", self._event_subscribe_handler)
        server.register("event.unsubscribe", self._event_unsubscribe_handler)
        server.register("event.list", self._event_list_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("agent.resume", self._agent_resume_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send", self._session_send_handler)
        server.register("session.get", self._session_get_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.destroy", self._session_destroy_handler)
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
            if self._session_manager is not None:
                await self._session_manager.stop()
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
