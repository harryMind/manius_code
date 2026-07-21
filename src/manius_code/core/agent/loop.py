from manius_code.core.agent.context import ExecutionContext
from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import StepDoneEvent, StepPlanningEvent
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tools.invocation import ToolExecutionError, ToolInvoker


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
