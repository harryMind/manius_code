import asyncio

from manius_code.core.agent.runner import RunSummary
from manius_code.core.app import CoreApp
from manius_code.core.config import ManiusConfig, SessionConfig
from manius_code.core.sessions.manager import SessionManager
from manius_code.core.sessions.models import SessionRunRequest
from manius_code.core.sessions.store import SessionStore


class SuccessfulRunner:
    # 保存会话运行请求以便断言 CoreApp 已将会话上下文交给运行器工厂。
    def __init__(self, request: SessionRunRequest) -> None:
        self._request = request

    # 返回固定成功摘要，使 RPC 处理器测试不依赖外部 LLM 服务。
    async def run(self, goal: str, run_id: str) -> RunSummary:
        return RunSummary(run_id=run_id, status="success", total_steps=1, duration_ms=1, result=f"done: {goal}")


# 功能：验证 CoreApp 的 session.* 处理器可完成创建、发送、查询、列出和释放而不影响后台任务跟踪。
# 设计：替换唯一的 AgentRunner 工厂为确定性成功替身，直接覆盖 IPC 处理器的领域编排与持久化边界。
def test_core_app_session_handlers_manage_persistent_session(tmp_path) -> None:
    # 驱动一轮完整会话 RPC 生命周期并等待后台运行回写 Thread。
    async def exercise():
        app = CoreApp()
        app._config = ManiusConfig(session=SessionConfig(directory=tmp_path / "sessions"))
        requests: list[SessionRunRequest] = []

        # 构造替身运行器并记录会话管理器提供的上下文请求。
        def runner_factory(request: SessionRunRequest) -> SuccessfulRunner:
            requests.append(request)
            return SuccessfulRunner(request)

        app._session_manager = SessionManager(
            SessionStore(app._config.session.directory),
            runner_factory,
            app._event_broadcaster.handle,
            task_observer=app._track_agent_task,
        )
        created = await app._session_create_handler({"type": "session.create", "client_id": "test-client"})
        sent = await app._session_send_handler(
            {"type": "session.send", "session_id": created.session.session_id, "content": "读取 README"}
        )
        await asyncio.gather(*tuple(app._agent_tasks))
        fetched = await app._session_get_handler({"type": "session.get", "session_id": created.session.session_id})
        listed = await app._session_list_handler({"type": "session.list"})
        destroyed = await app._session_destroy_handler({"type": "session.destroy", "session_id": created.session.session_id})
        return created, sent, fetched, listed, destroyed, requests

    created, sent, fetched, listed, destroyed, requests = asyncio.run(exercise())
    assert sent.session_id == created.session.session_id
    assert fetched.session.run_ids == [sent.run_id]
    assert [entry.role for entry in fetched.thread] == ["user", "assistant"]
    assert [session.session_id for session in listed.sessions] == [created.session.session_id]
    assert destroyed.destroyed is True
    assert requests[0].goal == "读取 README"
