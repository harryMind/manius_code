from manius_code.core.agent.context import ExecutionContext
from manius_code.core.events.bus import EventBus
from manius_code.core.events.models import StepDoneEvent, StepPlanningEvent
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tools.registry import ToolRegistry


class AgentLoop:
    # 注入上下文、Claude Provider、工具注册表和事件总线。
    def __init__(
        self,
        context: ExecutionContext,
        provider: AnthropicProvider,
        tools: ToolRegistry,
        event_bus: EventBus,
        max_steps: int,
    ) -> None:
        self._context = context
        self._provider = provider
        self._tools = tools
        self._event_bus = event_bus
        self._max_steps = max_steps

    # 按计划、行动、观察循环执行任务直至 Claude 完成。
    async def run(self) -> str:
        while self._context.step < self._max_steps:
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
                await self._event_bus.publish(
                    StepDoneEvent(
                        run_id=self._context.run_id,
                        step=self._context.step,
                        complete=True,
                        observation=response.text,
                    )
                )
                return response.text
            for tool_call in response.tool_calls:
                result = await self._tools.get(tool_call.name).execute(tool_call.arguments)
                self._context.add_tool_result(tool_call.id, result)
            await self._event_bus.publish(
                StepDoneEvent(
                    run_id=self._context.run_id,
                    step=self._context.step,
                    complete=False,
                    observation="Tool observations added to context",
                )
            )
        raise RuntimeError(f"Agent exceeded max_steps={self._max_steps}")
