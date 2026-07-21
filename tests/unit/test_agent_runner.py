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


# 构造包含标准 tool_use 内容块的确定性工具调用响应。
def _tool_response(call_id: str, name: str, arguments: dict) -> LlmResponse:
    return LlmResponse(
        text="Planning the next task.",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        assistant_content=[{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
    )


class AutonomousPlanningProvider:
    # 初始化确定性规划步骤并保存运行器注入的工具定义名称。
    def __init__(self) -> None:
        self._calls = 0
        self.tool_names: set[str] = set()

    # 记录运行器提供的工具定义后返回同一 Provider 替身。
    def with_tool_definitions(self, definitions: list[dict]) -> "AutonomousPlanningProvider":
        self.tool_names = {definition["name"] for definition in definitions}
        return self

    # 模拟创建依赖任务、执行任务并输出最终交付结果的完整规划流程。
    async def complete(self, run_id: str, step: int, messages: list[dict]) -> LlmResponse:
        self._calls += 1
        calls = [
            ("create-1", "task_create", {"subject": "Inspect repository"}),
            ("create-2", "task_create", {"subject": "Write structure report", "blocked_by": [1]}),
            ("update-1", "task_update", {"task_id": 1, "status": "completed"}),
            ("update-2", "task_update", {"task_id": 2, "status": "in_progress"}),
            ("write-1", "write_file", {"path": "structure.md", "content": "# Structure\n"}),
            ("update-3", "task_update", {"task_id": 2, "status": "completed"}),
        ]
        if self._calls <= len(calls):
            return _tool_response(*calls[self._calls - 1])
        return LlmResponse(
            text="Repository structure report completed.",
            tool_calls=[],
            assistant_content=[{"type": "text", "text": "Repository structure report completed."}],
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


# 功能：验证 daemon 运行器不会本地打印事件，但仍会完成任务并持久化事件。
# 设计：注入会失败的确定性 Provider，使测试仅关注服务端无终端输出的职责边界。
def test_agent_runner_does_not_print_local_events(tmp_path: Path, capsys) -> None:
    runner = AgentRunner(
        ManiusConfig(max_steps=1),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus, _tools: FailingAnthropicProvider(),
    )
    summary = asyncio.run(runner.run("Fail without daemon output"))
    event_path = tmp_path / "runs" / summary.run_id / "events.jsonl"

    assert summary.status == "failed"
    assert capsys.readouterr().out == ""
    assert json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])["type"] == "run_finished"


# 功能：验证运行器为每次任务注册八个工具，并由 Agent 自主完成带依赖的任务规划和交付。
# 设计：以确定性 Provider 串联创建、解锁、执行与完成操作，直接断言 run 私有 .tasks 文件和最终产物。
def test_agent_runner_executes_autonomous_task_plan_in_isolated_run_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    provider = AutonomousPlanningProvider()
    runner = AgentRunner(
        ManiusConfig(max_steps=7),
        runs_dir=tmp_path / "runs",
        provider_factory=lambda _bus, definitions: provider.with_tool_definitions(definitions),
    )

    summary = asyncio.run(runner.run("Analyze the repository and generate a structure report."))
    tasks_dir = tmp_path / "runs" / summary.run_id / ".tasks"
    second_task = json.loads((tasks_dir / "task_2.json").read_text(encoding="utf-8"))

    assert provider.tool_names == {
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "read_file",
        "write_file",
        "list_dir",
        "bash",
    }
    assert summary.status == "success"
    assert (tmp_path / "structure.md").read_text(encoding="utf-8") == "# Structure\n"
    assert second_task["status"] == "completed"
    assert second_task["blocked_by"] == []
