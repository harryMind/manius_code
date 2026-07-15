import asyncio
import json
from pathlib import Path

from manius_code.core.config import ManiusConfig
from manius_code.core.agent.runner import AgentRunner
from manius_code.core.llm.anthropic import LlmResponse, ToolCall


class FakeAnthropicProvider:
    # 记录调用次数以模拟一次读文件和一次最终回答。
    def __init__(self) -> None:
        self._calls = 0

    # 依次返回工具调用和最终文本响应。
    async def complete(self, run_id: str, step: int, messages: list[dict]) -> LlmResponse:
        self._calls += 1
        if self._calls == 1:
            return LlmResponse(
                text="I will inspect the file.",
                tool_calls=[ToolCall(id="read-1", name="read_file", arguments={"path": "README.md"})],
                assistant_content=[{"type": "tool_use", "id": "read-1", "name": "read_file", "input": {"path": "README.md"}}],
            )
        return LlmResponse(
            text="README summary complete.",
            tool_calls=[],
            assistant_content=[{"type": "text", "text": "README summary complete."}],
        )


# 功能：验证 AgentRunner 会执行读文件、持久化事件并完成多轮任务。
# 设计：注入确定性 Claude 替身，覆盖 plan-act-observe 和 events.jsonl，而不依赖外部 API。
def test_agent_runner_persists_end_to_end_events(tmp_path: Path, monkeypatch) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Main sections\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(
        ManiusConfig(max_steps=2),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus, _tools: FakeAnthropicProvider(),
    )
    summary = asyncio.run(runner.run("Summarize README.md"))
    event_path = tmp_path / "runs" / summary.run_id / "events.jsonl"
    event_types = [json.loads(line)["type"] for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert summary.status == "completed"
    assert summary.total_steps == 2
    assert "tool_call_start" in event_types
    assert "tool_call_success" in event_types
    assert event_types[-1] == "run_finished"
