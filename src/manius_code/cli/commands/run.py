from __future__ import annotations

import argparse
import asyncio

from pydantic import TypeAdapter, ValidationError

from manius_code.core.bus.commands import AgentRunResult
from manius_code.core.config import ManiusConfig
from manius_code.core.events.models import AgentEvent, RunFinishedEvent
from manius_code.core.events.subscribers import StdoutPrinter
from manius_code.core.transport.socket_client import SocketClient

_EVENT_ADAPTER = TypeAdapter(AgentEvent)


class RunEventClient(SocketClient):
    # 初始化事件打印器和按运行标识索引的完成通知。
    def __init__(self, host: str, port: int, printer: StdoutPrinter) -> None:
        super().__init__(host, port)
        self._printer = printer
        self._finished_events: dict[str, RunFinishedEvent] = {}
        self._finished_waiters: dict[str, asyncio.Event] = {}

    # 反序列化服务端事件通知、实时打印并唤醒已完成任务的等待者。
    async def on_event(self, message: dict[str, object]) -> None:
        if message.get("method") != "event.push" or not isinstance(message.get("params"), dict):
            return
        try:
            event = _EVENT_ADAPTER.validate_python(message["params"])
        except ValidationError:
            return
        self._printer.handle(event)
        if isinstance(event, RunFinishedEvent):
            self._finished_events[event.run_id] = event
            waiter = self._finished_waiters.get(event.run_id)
            if waiter is not None:
                waiter.set()

    # 等待指定运行的完成事件并返回服务端汇总状态。
    async def wait_for_finished(self, run_id: str) -> RunFinishedEvent:
        finished_event = self._finished_events.get(run_id)
        if finished_event is not None:
            return finished_event
        waiter = self._finished_waiters.setdefault(run_id, asyncio.Event())
        await waiter.wait()
        return self._finished_events[run_id]


# 向 CLI 解析器注册前台 Agent 运行命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("run", help="Run an Agent task in the foreground")
    parser.add_argument("--goal", required=True, help="Task goal for the Agent")
    parser.set_defaults(handler=run)


# 运行 Agent 并以进程状态码表示任务是否成功完成。
# 通过订阅、远程调用和完成事件等待执行一次 daemon 托管任务。
async def _run_remote(config: ManiusConfig, goal: str) -> RunFinishedEvent:
    client = RunEventClient(config.host, config.port, StdoutPrinter())
    await client.connect()
    try:
        await client.send_command("event.subscribe", {"type": "event.subscribe"})
        response = await client.send_command("agent.run", {"type": "agent.run", "goal": goal})
        started = AgentRunResult.model_validate(response.result)
        return await client.wait_for_finished(started.run_id)
    finally:
        await client.close()


# 运行远程 Agent 并以最终事件状态表示命令是否成功完成。
def run(config: ManiusConfig, goal: str) -> None:
    finished_event = asyncio.run(_run_remote(config, goal))
    if finished_event.status != "success":
        raise SystemExit(1)
