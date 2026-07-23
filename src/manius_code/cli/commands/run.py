from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import suppress
from typing import Any

from pydantic import TypeAdapter, ValidationError

from manius_code.core.bus.commands import AgentRunResult, EventListResult, EventSubscribeResult
from manius_code.core.bus.events import AgentEvent, RunFinishedEvent
from manius_code.core.config import ManiusConfig
from manius_code.core.events.subscribers import StdoutPrinter
from manius_code.core.transport.socket_client import IpcError, SocketClient

_EVENT_ADAPTER = TypeAdapter(AgentEvent)


# 向 CLI 注册新建任务和恢复已停止任务的前台观察命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("run", help="Run an Agent task in the foreground")
    parser.add_argument("--goal", required=True, help="Task goal for the Agent")
    parser.set_defaults(handler=run)
    resume_parser = subparsers.add_parser("resume", help="Resume a stopped Agent task in the foreground")
    resume_parser.add_argument("--run-id", required=True, help="Stopped Agent run identifier")
    resume_parser.set_defaults(handler=resume)


# 复用历史回放、精确订阅和完成等待逻辑观察一次远程运行或恢复请求。
async def _watch_remote(config: ManiusConfig, method: str, params: dict[str, Any]) -> RunFinishedEvent:
    printer = StdoutPrinter()
    finished_events: dict[str, RunFinishedEvent] = {}
    finished_signal = asyncio.Event()
    sub_id: str | None = None

    # 校验并渲染一条历史或实时事件，同时记录任意运行的完成态。
    async def consume_event(message: dict[str, Any], render: bool = True) -> None:
        try:
            event = _EVENT_ADAPTER.validate_python(message)
        except ValidationError:
            return
        if render:
            printer.handle(event)
        if isinstance(event, RunFinishedEvent):
            finished_events[event.run_id] = event
            finished_signal.set()

    # 将 SocketClient 推送统一交给历史回放也会使用的事件消费逻辑。
    async def handle_event(message: dict[str, Any]) -> None:
        await consume_event(message)

    client = SocketClient(config.host, config.port, event_handler=handle_event)

    # 精确等待目标 run_id 的完成事件，并在事件流异常关闭时返回 IPC 错误。
    async def wait_for_finished(run_id: str) -> RunFinishedEvent:
        while True:
            if run_id in finished_events:
                return finished_events[run_id]
            event_loop_task = client._event_loop_task
            if event_loop_task is None:
                raise IpcError(-32000, "Event stream is not running")
            signal_waiter = asyncio.create_task(finished_signal.wait())
            done, _ = await asyncio.wait(
                {signal_waiter, event_loop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if signal_waiter not in done:
                signal_waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await signal_waiter
            if run_id in finished_events:
                return finished_events[run_id]
            if event_loop_task in done:
                if event_loop_task.cancelled():
                    raise IpcError(-32000, "Event stream was cancelled before run finished")
                error = event_loop_task.exception()
                detail = f": {error}" if error is not None else ""
                raise IpcError(-32000, f"Event stream closed before run finished{detail}")
            finished_signal.clear()

    try:
        await client.connect()
        started = AgentRunResult.model_validate((await client.send_command(method, params)).result)
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
            event_loop_task = client._event_loop_task
            if sub_id is not None and event_loop_task is not None and not event_loop_task.done():
                with suppress(IpcError, OSError):
                    await client.send_command("event.unsubscribe", {"type": "event.unsubscribe", "sub_id": sub_id})
        finally:
            with suppress(IpcError, OSError, RuntimeError, ValueError):
                await client.close()


# 发起新的 agent.run 请求并持续观察该任务直到收到完成事件。
async def _run_remote(config: ManiusConfig, goal: str) -> RunFinishedEvent:
    return await _watch_remote(config, "agent.run", {"type": "agent.run", "goal": goal})


# 发起 agent.resume 请求并持续观察恢复后的同一任务直到再次完成。
async def _resume_remote(config: ManiusConfig, run_id: str) -> RunFinishedEvent:
    return await _watch_remote(config, "agent.resume", {"type": "agent.resume", "run_id": run_id})


# 执行新建任务命令并以完成事件状态映射进程退出码。
def run(config: ManiusConfig, goal: str) -> None:
    _exit_for_finished_event(_run_remote, config, goal)


# 执行恢复任务命令并以完成事件状态映射进程退出码。
def resume(config: ManiusConfig, run_id: str) -> None:
    _exit_for_finished_event(_resume_remote, config, run_id)


# 统一输出 IPC 或数据校验错误，并将失败完成事件转换为非零退出码。
def _exit_for_finished_event(remote, config: ManiusConfig, argument: str) -> None:
    try:
        finished_event = asyncio.run(remote(config, argument))
    except IpcError as error:
        print(f"manius: IPC request failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValidationError) as error:
        print(f"manius: unable to run task: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    if finished_event.status != "success":
        raise SystemExit(1)
