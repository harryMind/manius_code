import argparse
import asyncio
import json
import time

from pydantic import ValidationError

from manius_code.core.bus.commands import PongResult
from manius_code.core.bus.envelope import JsonRpcError, JsonRpcSuccess
from manius_code.core.config import ConfigError, load_config


# 发送一次 ping 请求并返回 daemon 的 pong 结果与往返延迟。
async def ping() -> tuple[PongResult, int]:
    config = load_config()
    started_at = time.monotonic()
    reader, writer = await asyncio.open_connection(config.host, config.port)
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "core.ping", "params": {"type": "core.ping"}}
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    latency_ms = round((time.monotonic() - started_at) * 1000)
    response = json.loads(line)
    try:
        success = JsonRpcSuccess.model_validate(response)
    except ValidationError:
        error = JsonRpcError.model_validate(response)
        raise RuntimeError(f"core.ping failed: {error.error.message}")
    return PongResult.model_validate(success.result), latency_ms


# 解析命令行参数并执行指定的客户端命令。
def main() -> None:
    parser = argparse.ArgumentParser(prog="manius")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ping", help="Check that manius-core is reachable")
    arguments = parser.parse_args()
    if arguments.command != "ping":
        parser.error("Unknown command")
    try:
        result, latency_ms = asyncio.run(ping())
    except (ConfigError, OSError, RuntimeError, json.JSONDecodeError, ValidationError) as error:
        parser.exit(1, f"manius: {error}\n")
    print(f"pong server={result.server} uptime={result.uptime_ms}ms latency={latency_ms}ms")
