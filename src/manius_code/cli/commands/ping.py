from __future__ import annotations

import argparse
import asyncio
import json
import time

from pydantic import ValidationError
import manius_code
from manius_code.core.bus.commands import PongResult
from manius_code.core.bus.envelope import JsonRpcError, JsonRpcSuccess
from manius_code.core.config import ManiusConfig


# 向 CLI 解析器注册 ping 子命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("ping", help="Check that manius-core is reachable")
    # 给ping命令绑定hander函数，也就是说解析到ping命令便执行run函数
    parser.set_defaults(handler=run)


# 发送一次 ping 请求并返回 daemon 的 pong 结果与往返延迟。
async def ping(config: ManiusConfig) -> tuple[PongResult, int]:
    started_at = time.monotonic()
    reader, writer = await asyncio.open_connection(config.host, config.port)
    try:
        request = {"jsonrpc": "2.0", "id": "cli-1", "method": "core.ping", "params": {"client": f"cli/{manius_code.__version__}"}}
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


# 执行 ping 子命令并输出可读的连接状态。
def run(config: ManiusConfig) -> None:
    try:
        result, latency_ms = asyncio.run(ping(config))
    except (OSError, RuntimeError, json.JSONDecodeError, ValidationError) as error:
        raise SystemExit(f"manius: {error}") from error
    print(f"pong server={result.server} uptime={result.uptime_ms}ms latency={latency_ms}ms")
