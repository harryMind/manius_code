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


class FailingAnthropicProvider:
    # 直接抛出 Provider 异常以验证运行上下文会记录失败原因。
    async def complete(self, run_id: str, step: int, messages: list[dict]) -> LlmResponse:
        raise RuntimeError("Provider request failed")


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
    assert summary.status == "success"
    assert summary.result == "README summary complete."
    assert summary.reason is None
    assert summary.total_steps == 2
    assert "tool_call_start" in event_types
    assert "tool_call_success" in event_types
    assert event_types[-1] == "run_finished"


# 功能：验证 Provider 异常会写入上下文终态并传播至运行汇总与完成事件。
# 设计：运行器继续返回失败汇总，方便 CLI 依据 status 统一决定退出状态。
def test_agent_runner_records_provider_failure_in_summary_and_event(tmp_path: Path) -> None:
    runner = AgentRunner(
        ManiusConfig(max_steps=2),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus, _tools: FailingAnthropicProvider(),
    )
    summary = asyncio.run(runner.run("Fail the provider request"))
    event_path = tmp_path / "runs" / summary.run_id / "events.jsonl"
    finished_event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])

    assert summary.status == "failed"
    assert summary.result == ""
    assert summary.reason == "Provider request failed"
    assert finished_event["status"] == "failed"
    assert finished_event["summary"] == "Provider request failed"
    assert finished_event["reason"] == "Provider request failed"


# 功能：验证达到最大步数会将统一上下文状态标记为失败并输出失败原因。
# 设计：复用含工具调用的替身，确保在已有执行步骤后触发步数限制。
def test_agent_runner_records_max_steps_failure_in_summary_and_event(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# Main sections\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(
        ManiusConfig(max_steps=1),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus, _tools: FakeAnthropicProvider(),
    )
    summary = asyncio.run(runner.run("Summarize README.md"))
    event_path = tmp_path / "runs" / summary.run_id / "events.jsonl"
    finished_event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])

    assert summary.status == "failed"
    assert summary.result == ""
    assert summary.reason == "Agent exceeded max_steps=1"
    assert finished_event["status"] == "failed"
    assert finished_event["reason"] == "Agent exceeded max_steps=1"


# 功能：验证关闭本地事件打印后，AgentRunner 仍会完成任务并持久化事件。
# 设计：注入会失败的确定性 Provider，使测试仅关注标准输出开关而不依赖文件工具或外部模型。
def test_agent_runner_can_disable_local_event_output(tmp_path: Path, capsys) -> None:
    runner = AgentRunner(
        ManiusConfig(max_steps=1),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus, _tools: FailingAnthropicProvider(),
        print_events=False,
    )
    summary = asyncio.run(runner.run("Fail without daemon output"))
    event_path = tmp_path / "runs" / summary.run_id / "events.jsonl"

    assert summary.status == "failed"
    assert capsys.readouterr().out == ""
    assert json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])["type"] == "run_finished"
