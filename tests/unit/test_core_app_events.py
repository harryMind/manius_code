import asyncio

from manius_code.core.app import CoreApp
from manius_code.core.config import ManiusConfig


# 功能：验证 agent.run 参数错误仍返回 run_id 并生成可回放的失败完成事件。
# 设计：直接驱动 CoreApp 处理器以隔离网络层，同时收集广播和 JSONL 回放两个事件出口。
def test_agent_run_parameter_failure_emits_replayable_finished_event(tmp_path, monkeypatch) -> None:
    # 执行参数错误任务并等待后台失败事件闭环完成。
    async def exercise():
        app = CoreApp()
        app._config = ManiusConfig()
        app._runs_dir = tmp_path / "runs"
        broadcast_events = []
        monkeypatch.setattr(app._event_broadcaster, "handle", broadcast_events.append)

        started = await app._agent_run_handler({"type": "agent.run", "goal": ""})
        await asyncio.sleep(0)
        history = await app._event_list_handler({"type": "event.list", "run_id": started.run_id})
        return started, broadcast_events, history

    started, broadcast_events, history = asyncio.run(exercise())
    assert [event.type for event in broadcast_events] == ["run_started", "run_finished"]
    assert broadcast_events[-1].status == "failed"
    assert history.run_id == started.run_id
    assert history.events[-1]["type"] == "run_finished"
    assert history.events[-1]["status"] == "failed"
    assert history.events[-1]["reason"]
