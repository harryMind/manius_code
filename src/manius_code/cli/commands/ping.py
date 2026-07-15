from __future__ import annotations

import argparse
import asyncio
import time

import manius_code
from pydantic import ValidationError

from manius_code.core.bus.commands import PongResult
from manius_code.core.config import ManiusConfig
from manius_code.core.transport.socket_client import SocketClient


# 向 CLI 解析器注册 ping 子命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("ping", help="Check that manius-core is reachable")
    parser.set_defaults(handler=run)


# 发送一次 ping 请求并返回 daemon 的 pong 结果与往返延迟。
async def ping(config: ManiusConfig) -> tuple[PongResult, int]:
    started_at = time.monotonic()
    client = SocketClient(config.host, config.port)
    await client.connect()
    try:
        response = await client.send_command(
            "core.ping",
            {"type": "core.ping", "client": f"cli/{manius_code.__version__}"},
        )
    finally:
        await client.close()
    latency_ms = round((time.monotonic() - started_at) * 1000)
    return PongResult.model_validate(response.result), latency_ms


# 执行 ping 子命令并输出可读的连接状态。
def run(config: ManiusConfig) -> None:
    try:
        result, latency_ms = asyncio.run(ping(config))
    except (OSError, RuntimeError, ValidationError) as error:
        raise SystemExit(f"manius: {error}") from error
    print(f"pong server={result.server} uptime={result.uptime_ms}ms latency={latency_ms}ms")
