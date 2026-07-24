import asyncio

from manius_code.core.agent.runner import RunSummary
from manius_code.core.sessions.manager import SessionManager
from manius_code.core.sessions.models import SessionRunRequest
from manius_code.core.sessions.store import SessionStore
from manius_code.core.tools.note import NoteSaveTool


class DeterministicRunner:
    # 保存调用方传入的会话运行请求，供测试验证上下文注入是否正确。
    def __init__(self, request: SessionRunRequest, requests: list[SessionRunRequest]) -> None:
        self._request = request
        self._requests = requests

    # 返回不依赖 LLM 的固定成功摘要，使会话生命周期测试聚焦持久化行为。
    async def run(self, goal: str, run_id: str) -> RunSummary:
        self._requests.append(self._request)
        return RunSummary(run_id=run_id, status="success", total_steps=1, duration_ms=1, result=f"summary: {goal}")


# 功能：验证会话能持久化多轮 Thread、检索相关 Notes，并把两类记忆注入下一次运行。
# 设计：使用确定性运行器替身隔离 S4 执行内核，仅断言 SessionManager 的存储边界和异步收尾逻辑。
def test_session_manager_persists_thread_and_retrieves_notes(tmp_path) -> None:
    # 驱动同一会话连续两轮运行和一次笔记写入。
    async def exercise() -> tuple[SessionManager, SessionStore, str, list[SessionRunRequest], list[object]]:
        store = SessionStore(tmp_path / "sessions")
        requests: list[SessionRunRequest] = []
        events: list[object] = []

        # 构造会话运行器替身并记录其接收到的请求。
        def runner_factory(request: SessionRunRequest) -> DeterministicRunner:
            return DeterministicRunner(request, requests)

        manager = SessionManager(store, runner_factory, events.append)
        session = await manager.create_session("test-client")
        first_run = await manager.send_message(session.session_id, "分析 README 结构")
        await asyncio.gather(*tuple(manager._tasks))
        note = await manager.save_note(
            session.session_id,
            "README 约定",
            "README 中说明了双进程 daemon 架构。",
            ["README", "architecture"],
            first_run,
        )
        await manager.send_message(session.session_id, "基于 README 继续说明架构")
        await asyncio.gather(*tuple(manager._tasks))
        return manager, store, session.session_id, requests, events

    manager, store, session_id, requests, events = asyncio.run(exercise())
    meta = store.load_meta(session_id)
    thread = store.load_thread(session_id)
    assert meta.turn_count == 2
    assert len(meta.run_ids) == 2
    assert [entry.role for entry in thread] == ["user", "assistant", "user", "assistant"]
    assert "README 约定" in requests[-1].system_context
    assert "summary: 分析 README 结构" in requests[-1].system_context
    assert any(getattr(event, "type", None) == "session_created" for event in events)
    assert any(getattr(event, "type", None) == "note_saved" for event in events)
    asyncio.run(manager.destroy_session(session_id))
    assert store.load_meta(session_id).session_id == session_id


# 功能：验证 note_save 工具只通过注入的会话写入器保存笔记并返回稳定确认文本。
# 设计：以异步回调替身代替真实文件系统，覆盖工具参数传递与会话边界解耦而不重复测试 SessionStore。
def test_note_save_tool_delegates_to_session_writer() -> None:
    # 调用工具并记录被传递给会话层的标题、正文和标签。
    async def exercise() -> tuple[str, list[tuple[str, str, list[str]]]]:
        calls: list[tuple[str, str, list[str]]] = []

        # 返回最小笔记对象来模拟会话层的成功写入。
        async def save_note(title: str, content: str, tags: list[str]):
            calls.append((title, content, tags))
            from manius_code.core.sessions.models import SessionNote

            return SessionNote(id=3, title=title, content=content, tags=tags, source_run_id="run-1")

        result = await NoteSaveTool(save_note).execute(
            {"title": "项目规则", "content": "使用相对路径", "tags": ["rule"]}
        )
        return result, calls

    result, calls = asyncio.run(exercise())
    assert result == "saved note 3: 项目规则"
    assert calls == [("项目规则", "使用相对路径", ["rule"])]
