import time
from collections.abc import Callable
from typing import Any

from manius_code.core.events.bus import EventBus
from manius_code.core.bus.events import ToolCallFailedEvent, ToolCallStartEvent, ToolCallSuccessEvent
from manius_code.core.tools.registry import ToolRegistry


class ToolExecutionError(RuntimeError):
    # 保存工具名和可展示给 Agent 的执行错误信息。
    def __init__(self, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"{tool_name}: {message}")


class ToolInvoker:
    # 注入工具查询、运行元数据和统一事件广播依赖。
    def __init__(self, registry: ToolRegistry, event_bus: EventBus, run_id: str, step_getter: Callable[[], int]) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._run_id = run_id
        self._step_getter = step_getter

    # 执行工具并统一广播调用事件、结果、失败信息和耗时。
    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        step = self._step_getter()
        await self._event_bus.publish(
            ToolCallStartEvent(run_id=self._run_id, step=step, tool_name=name, arguments=arguments)
        )
        started_at = time.monotonic()
        try:
            result = await self._registry.get(name).execute(arguments)
        except ToolExecutionError as error:
            duration_ms = round((time.monotonic() - started_at) * 1000)
            await self._event_bus.publish(
                ToolCallFailedEvent(
                    run_id=self._run_id,
                    step=step,
                    tool_name=name,
                    duration_ms=duration_ms,
                    error=error.message,
                )
            )
            raise
        except KeyError:
            duration_ms = round((time.monotonic() - started_at) * 1000)
            message = "tool is not registered"
            await self._event_bus.publish(
                ToolCallFailedEvent(
                    run_id=self._run_id,
                    step=step,
                    tool_name=name,
                    duration_ms=duration_ms,
                    error=message,
                )
            )
            raise ToolExecutionError(name, message) from None
        except Exception as error:
            duration_ms = round((time.monotonic() - started_at) * 1000)
            message = f"unexpected execution error: {error}"
            await self._event_bus.publish(
                ToolCallFailedEvent(
                    run_id=self._run_id,
                    step=step,
                    tool_name=name,
                    duration_ms=duration_ms,
                    error=message,
                )
            )
            raise ToolExecutionError(name, message) from error
        duration_ms = round((time.monotonic() - started_at) * 1000)
        await self._event_bus.publish(
            ToolCallSuccessEvent(
                run_id=self._run_id,
                step=step,
                tool_name=name,
                duration_ms=duration_ms,
                result=result,
            )
        )
        return result
