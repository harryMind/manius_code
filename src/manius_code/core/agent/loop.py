from pathlib import Path

from manius_code.core.agent.context import ExecutionContext
from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import StepDoneEvent, StepPlanningEvent
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tools.invocation import ToolExecutionError, ToolInvoker
from manius_code.core.tools.paths import resolve_workspace_path


# 判断目标是否同时包含写入动作和明确的文件或工作区路径指向。
def _requires_file_output(goal: str) -> bool:
    normalized = goal.casefold()
    has_write_action = any(
        action in normalized
        for action in ("write", "create", "generate", "save", "add", "写", "创建", "生成", "保存", "新增", "添加")
    )
    has_file_target = "/" in goal or "\\" in goal or "file" in normalized or "文件" in goal
    return has_write_action and has_file_target


class AgentLoop:
    # 注入上下文、Claude Provider、工具注册表和事件总线。
    def __init__(
        self,
        context: ExecutionContext,
        provider: AnthropicProvider,
        tool_invoker: ToolInvoker,
        event_bus: EventBus,
        max_steps: int,
    ) -> None:
        self._context = context
        self._provider = provider
        self._tool_invoker = tool_invoker
        self._event_bus = event_bus
        self._max_steps = max_steps
        self._requires_file_output = _requires_file_output(context.goal)
        self._written_files: set[Path] = set()

    # 按计划、行动、观察循环执行任务直至 Claude 完成。
    async def run(self) -> str:
        try:
            while not self._context.is_done() and self._context.step < self._max_steps:
                self._context.step += 1
                await self._event_bus.publish(
                    StepPlanningEvent(
                        run_id=self._context.run_id,
                        step=self._context.step,
                        plan="Requesting the next plan from manius",
                    )
                )
                response = await self._provider.complete(
                    self._context.run_id,
                    self._context.step,
                    self._context.messages,
                )
                self._context.add_assistant_response(response.assistant_content)
                if not response.tool_calls:
                    if self._requires_file_output and not self._has_verified_file_write():
                        self._context.add_user_feedback(
                            "The goal requires a workspace file, but no successful write_file call produced one. "
                            "Use write_file with a workspace-relative path, then verify the file exists before finishing."
                        )
                        await self._event_bus.publish(
                            StepDoneEvent(
                                run_id=self._context.run_id,
                                step=self._context.step,
                                complete=False,
                                observation="A required workspace file has not been written; continue the task.",
                            )
                        )
                        continue
                    self._context.mark_success(response.text)
                    await self._event_bus.publish(
                        StepDoneEvent(
                            run_id=self._context.run_id,
                            step=self._context.step,
                            complete=True,
                            observation=self._context.result,
                        )
                    )
                    return self._context.result
                for tool_call in response.tool_calls:
                    try:
                        result = await self._tool_invoker.invoke(tool_call.name, tool_call.arguments)
                    except ToolExecutionError as error:
                        result = f"Tool '{error.tool_name}' failed: {error.message}"
                    else:
                        if tool_call.name == "write_file":
                            self._record_successful_file_write(tool_call.arguments)
                    self._context.add_tool_result(tool_call.id, result)
                await self._event_bus.publish(
                    StepDoneEvent(
                        run_id=self._context.run_id,
                        step=self._context.step,
                        complete=False,
                        observation="Tool observations added to context",
                    )
                )
            if self._context.is_done():
                return self._context.result
            reason = f"Agent exceeded max_steps={self._max_steps}"
            self._context.mark_failed(reason)
            raise RuntimeError(reason)
        except Exception as error:
            if not self._context.is_done():
                self._context.mark_failed(str(error))
            raise

    # 记录经过工具成功响应且当前仍存在于工作区内的文件路径。
    def _record_successful_file_write(self, arguments: dict) -> None:
        path_value = arguments.get("path")
        if not isinstance(path_value, str):
            return
        try:
            path = resolve_workspace_path(path_value)
        except ValueError:
            return
        if path.is_file():
            self._written_files.add(path)

    # 在最终结束前重新确认本轮成功写入的至少一个文件仍然存在。
    def _has_verified_file_write(self) -> bool:
        return any(path.is_file() for path in self._written_files)
