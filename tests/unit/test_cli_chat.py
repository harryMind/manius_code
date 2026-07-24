import asyncio
from datetime import datetime, timezone
from typing import Any

from manius_code.cli.commands.chat import _open_session
from manius_code.cli.commands.run import _watch_remote
from manius_code.core.bus.commands import EventListResult, EventSubscribeResult, EventUnsubscribeResult, SessionCreateResult, SessionMetaResult, SessionSendResult
from manius_code.core.bus.events import RunFinishedEvent, RunStartedEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.transport.socket_server import SocketServer


# 功能：验证 chat 可创建会话，并复用既有 run 观察器消费 session.send 返回的运行事件。
# 设计：以真实 JSON-RPC 长连接替身覆盖会话 RPC 与 run 级事件回放，避免在 chat 模块复制订阅协议。
def test_chat_opens_session_and_watches_session_run(free_port: int) -> None:
    # 启动最小 daemon 替身并完成创建、发送、历史回放和实时订阅。
    async def exercise() -> tuple[str, RunFinishedEvent]:
        broadcaster = IpcEventBroadcaster()
        server = SocketServer("127.0.0.1", free_port)
        history: dict[str, list[dict[str, Any]]] = {}
        timestamp = datetime.now(timezone.utc)

        # 返回持久会话元数据以满足 chat 创建会话的强类型校验。
        async def create_session(params: dict[str, Any]) -> dict[str, Any]:
            assert params == {"type": "session.create", "client_id": "cli"}
            return SessionCreateResult(
                session=SessionMetaResult(
                    session_id="chat-session",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            ).model_dump(mode="json")

        # 立即返回运行标识并在订阅建立后广播完成事件。
        async def send_message(params: dict[str, Any]) -> dict[str, Any]:
            assert params == {"type": "session.send", "session_id": "chat-session", "content": "总结 README"}
            started = RunStartedEvent(run_id="chat-run", goal="总结 README", run_dir="runs/chat-run")
            history["chat-run"] = [started.model_dump(mode="json")]
            return SessionSendResult(session_id="chat-session", run_id="chat-run").model_dump()

        # 返回目标运行到当前时刻的持久事件历史。
        async def list_events(params: dict[str, Any]) -> dict[str, Any]:
            return EventListResult(run_id=params["run_id"], events=history.get(params["run_id"], [])).model_dump()

        # 创建 run 范围订阅并异步发送最终事件，以覆盖 chat 对既有观察器的复用。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            sub_id = broadcaster.subscribe(writer, params["run_id"], params["topics"])

            # 将完成事件留给实时推送路径，避免仅通过历史回放结束测试。
            async def finish() -> None:
                await asyncio.sleep(0.01)
                finished = RunFinishedEvent(run_id="chat-run", status="success", total_steps=1, duration_ms=1, summary="done")
                history["chat-run"].append(finished.model_dump(mode="json"))
                broadcaster.handle(finished)

            asyncio.create_task(finish())
            return EventSubscribeResult(sub_id=sub_id, run_id=params["run_id"], topics=params["topics"]).model_dump()

        # 提供 CLI finally 块需要的退订确认。
        async def unsubscribe(params: dict[str, Any]) -> dict[str, Any]:
            return EventUnsubscribeResult(unsubscribed=broadcaster.unsubscribe(params["sub_id"])).model_dump()

        server.register("session.create", create_session)
        server.register("session.send", send_message)
        server.register("event.list", list_events)
        server.register_connection_handler("event.subscribe", subscribe)
        server.register("event.unsubscribe", unsubscribe)
        server.add_disconnect_handler(broadcaster.unsubscribe_writer)
        await server.start()
        try:
            config = ManiusConfig(port=free_port)
            session_id = await _open_session(config, None)
            finished = await _watch_remote(
                config,
                "session.send",
                {"type": "session.send", "session_id": session_id, "content": "总结 README"},
            )
            return session_id, finished
        finally:
            await server.stop()

    session_id, finished = asyncio.run(exercise())
    assert session_id == "chat-session"
    assert finished.run_id == "chat-run"
    assert finished.status == "success"
