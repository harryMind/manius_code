import asyncio
import json
import re
from pathlib import Path
from typing import Any

from manius_code.core.bus.commands import EventSubscribeResult
from manius_code.core.bus.events import StepPlanningEvent
from manius_code.core.config import LlmConfig
from manius_code.core.events.bus import EventBus
from manius_code.core.events.ipc import IpcEventBroadcaster
from manius_code.core.llm.anthropic import AnthropicProvider
from manius_code.core.tracing import TracingProvider, trace_paths
from manius_code.core.transport.socket_client import SocketClient
from manius_code.core.transport.socket_server import SocketServer
from tests.unit.test_anthropic_provider import FakeClient


# 功能：验证追踪器以毫秒时间戳按入队顺序完整落盘，并在停止时排空队列。
# 设计：使用独立临时文件和连续 emit，直接覆盖全局 JSONL 的异步 drain 与优雅关闭边界。
def test_tracing_provider_drains_global_file_on_stop(tmp_path: Path) -> None:
    # 启动追踪器、提交多种记录并在停止后读取全部持久化结果。
    async def exercise() -> list[dict[str, Any]]:
        trace_path = tmp_path / "traces" / "daemon.jsonl"
        tracer = TracingProvider(trace_path, max_queue_size=8)
        await tracer.start()
        tracer.emit("CLIENT->CORE", "ipc", "request", {"method": "core.ping"}, trace_id="rpc-1")
        tracer.emit("CORE", "event", "run_started", {"type": "run_started"}, run_id="run-1", step=0)
        await tracer.stop()
        return [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    records = asyncio.run(exercise())
    assert [record["direction"] for record in records] == ["CLIENT->CORE", "CORE"]
    assert [(record["layer"], record["kind"]) for record in records] == [("ipc", "request"), ("event", "run_started")]
    assert records[0]["trace_id"] == "rpc-1"
    assert records[1]["run_id"] == "run-1"
    assert records[1]["step"] == 0
    assert re.fullmatch(r".+\+00:00", records[0]["ts"])


# 功能：验证追踪文件按大小轮转、索引归档元数据，并清理超过保留数量的旧文件。
# 设计：逐批排空队列以稳定触发两次轮转，断言索引、活动文件和唯一保留归档的记录顺序。
def test_tracing_provider_rotates_files_and_maintains_archive_index(tmp_path: Path) -> None:
    # 写入三批超过 1MB 总阈值的记录以生成两个归档。
    async def exercise() -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
        trace_path = tmp_path / "traces" / "daemon.jsonl"
        tracer = TracingProvider(trace_path, max_size_mb=1, backup_count=1)
        await tracer.start()
        for kind in ("first", "second", "third"):
            tracer.emit("CORE", "event", kind, {"payload": "x" * 600_000})
            await tracer._queue.join()
        await tracer.stop()
        index = json.loads((tmp_path / "traces" / "daemon.index.json").read_text(encoding="utf-8"))
        archive_path = trace_path.parent / index["files"][0]["file"]
        active_records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        archive_records = [json.loads(line) for line in archive_path.read_text(encoding="utf-8").splitlines()]
        return trace_path, index, active_records, archive_records, trace_paths(trace_path)

    trace_path, index, active_records, archive_records, paths = asyncio.run(exercise())
    assert index["version"] == 1
    assert len(index["files"]) == 1
    assert index["files"][0]["record_count"] == 1
    assert index["files"][0]["first_ts"]
    assert index["files"][0]["last_ts"]
    assert index["files"][0]["size_bytes"] > 0
    assert [record["kind"] for record in archive_records] == ["second"]
    assert [record["kind"] for record in active_records] == ["third"]
    assert paths[-1] == trace_path
    assert len(paths) == 2


# 功能：验证 SocketServer 对正常请求、损坏帧及对应响应均写入完整 IPC 追踪记录。
# 设计：使用真实 TCP 客户端和原始坏帧覆盖解析前后两个边界，断言请求与响应通过 RPC ID 关联。
def test_socket_server_traces_requests_responses_and_parse_errors(tmp_path: Path, free_port: int) -> None:
    # 运行带追踪的 TCP 服务并返回本次连接产生的全部记录。
    async def exercise() -> tuple[str, list[dict[str, Any]]]:
        trace_path = tmp_path / "daemon.jsonl"
        tracer = TracingProvider(trace_path)
        server = SocketServer("127.0.0.1", free_port, tracer=tracer)

        # 返回原样业务参数以构造可校验的 JSON-RPC 成功响应。
        async def echo(params: dict[str, Any]) -> dict[str, Any]:
            return {"echo": params}

        server.register("test.echo", echo)
        await tracer.start()
        await server.start()
        client = SocketClient("127.0.0.1", free_port)
        try:
            await client.connect()
            response = await client.send_command("test.echo", {"value": "complete-payload"})
            reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.write(b"{not-json}\n")
            await writer.drain()
            await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await client.close()
            await server.stop()
            await tracer.stop()
        return str(response.id), [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    request_id, records = asyncio.run(exercise())
    request = next(record for record in records if record["direction"] == "CLIENT->CORE" and record["trace_id"] == request_id)
    response = next(record for record in records if record["direction"] == "CORE>CLIENT" and record["trace_id"] == request_id)
    parse_error = next(record for record in records if record["data"].get("error") == "Parse error")
    assert request["data"]["params"] == {"value": "complete-payload"}
    assert request["layer"] == "ipc"
    assert request["kind"] == "request"
    assert response["data"]["result"] == {"echo": {"value": "complete-payload"}}
    assert response["kind"] == "response"
    assert request["client_id"] == response["client_id"]
    assert parse_error["data"]["raw"] == "{not-json}"
    assert request["run_id"] is None


# 功能：验证 EventBus 业务事件和 IpcEventBroadcaster 的 event.push 使用同一全局追踪器。
# 设计：真实订阅连接接收一步规划事件，分别断言 core_event 与通知信封在同一追踪文件中出现。
def test_event_bus_and_ipc_push_are_traced(tmp_path: Path, free_port: int) -> None:
    # 建立订阅、发布事件并返回客户端收到的消息与追踪记录。
    async def exercise() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        trace_path = tmp_path / "daemon.jsonl"
        tracer = TracingProvider(trace_path)
        broadcaster = IpcEventBroadcaster(tracer)
        server = SocketServer("127.0.0.1", free_port, tracer=tracer)
        received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # 将当前连接注册为全量事件通知订阅者。
        async def subscribe(params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
            sub_id = broadcaster.subscribe(writer, params.get("run_id"), params.get("topics"))
            return EventSubscribeResult(sub_id=sub_id, run_id=params.get("run_id"), topics=params.get("topics", ["*"])).model_dump()

        # 收集 SocketClient 解封后的事件体供断言使用。
        async def handle_event(event: dict[str, Any]) -> None:
            await received.put(event)

        server.register_connection_handler("event.subscribe", subscribe)
        server.add_disconnect_handler(broadcaster.unsubscribe_writer)
        await tracer.start()
        await server.start()
        client = SocketClient("127.0.0.1", free_port, event_handler=handle_event)
        try:
            await client.connect()
            await client.send_command("event.subscribe", {"type": "event.subscribe", "run_id": "run-1", "topics": ["*"]})
            event_bus = EventBus(tracer)
            event_bus.subscribe(broadcaster.handle)
            await event_bus.publish(StepPlanningEvent(run_id="run-1", step=1, plan="inspect file"))
            received_event = await asyncio.wait_for(received.get(), timeout=1)
        finally:
            await client.close()
            await server.stop()
            await tracer.stop()
        return received_event, [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    received_event, records = asyncio.run(exercise())
    core_event = next(record for record in records if record["direction"] == "CORE")
    push = next(record for record in records if record["direction"] == "CORE>CLIENT" and record["data"].get("method") == "event.push")
    assert received_event["type"] == "step_planning"
    assert core_event["layer"] == "event"
    assert core_event["kind"] == "step_planning"
    assert core_event["step"] == 1
    assert core_event["data"]["plan"] == "inspect file"
    assert push["kind"] == "push"
    assert push["data"]["params"]["run_id"] == "run-1"


# 功能：验证 AnthropicProvider 只追踪完整请求和最终响应，而不追踪逐 token 增量。
# 设计：复用离线 SDK 替身，比较同一 trace_id 的成对记录并检查缓存、工具和完整 content 字段。
def test_anthropic_provider_traces_full_request_and_final_response(tmp_path: Path) -> None:
    # 调用离线 Provider 后读取全局追踪文件中的 LLM 往返记录。
    async def exercise() -> list[dict[str, Any]]:
        trace_path = tmp_path / "daemon.jsonl"
        tracer = TracingProvider(trace_path)
        await tracer.start()
        provider = AnthropicProvider(
            LlmConfig(api_key="test-key", default_model="test-model"),
            EventBus(),
            [{"name": "read_file", "input_schema": {"type": "object"}}],
            client=FakeClient(),
            tracer=tracer,
        )
        await provider.complete("run-1", 1, [{"role": "user", "content": "Read README.md"}])
        await tracer.stop()
        return [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    records = asyncio.run(exercise())
    request, response = records
    assert [record["direction"] for record in records] == ["CORE>LLM", "LLM>CORE"]
    assert request["trace_id"] == response["trace_id"]
    assert request["layer"] == response["layer"] == "llm"
    assert request["kind"] == "request"
    assert request["step"] == response["step"] == 1
    assert request["data"]["message_count"] == 1
    assert request["data"]["request"]["cache_control"] == {"type": "ephemeral"}
    assert request["data"]["request"]["tools"][0]["name"] == "read_file"
    assert response["kind"] == "response"
    assert response["data"]["response"]["content"][0]["text"] == "I will read the file."
