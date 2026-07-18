from __future__ import annotations

import argparse
import asyncio
from typing import Any

from pydantic import TypeAdapter, ValidationError

from manius_code.core.bus.commands import AgentRunResult, EventListResult, EventSubscribeResult
from manius_code.core.config import ManiusConfig
from manius_code.core.bus.events import AgentEvent, RunFinishedEvent
from manius_code.core.events.subscribers import StdoutPrinter
from manius_code.core.transport.socket_client import SocketClient

_EVENT_ADAPTER = TypeAdapter(AgentEvent)


# 向 CLI 解析器注册前台 Agent 运行命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("run", help="Run an Agent task in the foreground")
    parser.add_argument("--goal", required=True, help="Task goal for the Agent")
    parser.set_defaults(handler=run)


# 运行 Agent 并以进程状态码表示任务是否成功完成。
# 通过订阅、远程调用和完成事件等待执行一次 daemon 托管任务。
async def _run_remote(config: ManiusConfig, goal: str) -> RunFinishedEvent:
    printer = StdoutPrinter()
    finished_events: dict[str, RunFinishedEvent] = {}
    finished_signal = asyncio.Event()
    sub_id: str | None = None

    # 渲染服务端事件，并记录已结束的远程运行。 处理服务端推送的event
    async def consume_event(message: dict[str, Any], render: bool = True) -> None:
        try:
            event = _EVENT_ADAPTER.validate_python(message)
        except ValidationError:
            return
        if render:
            printer.handle(event)
        if isinstance(event, RunFinishedEvent):
            finished_events[event.run_id] = event
            finished_signal.set() # 唤醒在服务端的await事件

    # 等待目标运行的完成事件，并保留其他订阅任务的事件。
    # 将实时推送事件交给统一的事件消费逻辑。
    async def handle_event(message: dict[str, Any]) -> None:
        await consume_event(message)

    async def wait_for_finished(run_id: str) -> RunFinishedEvent:
        while run_id not in finished_events:
            await finished_signal.wait()
            finished_signal.clear()
        return finished_events[run_id]

    client = SocketClient(config.host, config.port, event_handler=handle_event)
    await client.connect()
    try:
        response = await client.send_command("agent.run", {"type": "agent.run", "goal": goal})
        started = AgentRunResult.model_validate(response.result)
        history = EventListResult.model_validate(
            (await client.send_command("event.list", {"type": "event.list", "run_id": started.run_id})).result
        )
        for event in history.events:
            await consume_event(event)
        if started.run_id in finished_events:
            return finished_events[started.run_id]
        subscription = EventSubscribeResult.model_validate(
            (
                await client.send_command(
                    "event.subscribe",
                    {"type": "event.subscribe", "run_id": started.run_id, "topics": ["*"]},
                )
            ).result
        )
        sub_id = subscription.sub_id
        current_history = EventListResult.model_validate(
            (await client.send_command("event.list", {"type": "event.list", "run_id": started.run_id})).result
        )
        for event in current_history.events:
            await consume_event(event, render=False)
        return await wait_for_finished(started.run_id)
    finally:
        try:
            if sub_id is not None:
                await client.send_command("event.unsubscribe", {"type": "event.unsubscribe", "sub_id": sub_id})
        finally:
            await client.close()


# 运行远程 Agent 并以最终事件状态表示命令是否成功完成。
def run(config: ManiusConfig, goal: str) -> None:
    finished_event = asyncio.run(_run_remote(config, goal))
    if finished_event.status != "success":
        raise SystemExit(1)
