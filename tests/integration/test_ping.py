import asyncio

from manius_code.cli.commands.ping import ping
from manius_code.core.config import load_config
from manius_code.core.transport.socket_client import SocketClient


# 功能：验证 CLI 能经由 TCP JSON-RPC 获得 daemon 的 pong 结果。
# 设计：调用真实客户端函数并连接子进程 daemon，覆盖完整的 S0 跨进程路径。
def test_ping_returns_daemon_metadata(core_daemon: int, monkeypatch) -> None:
    monkeypatch.setenv("MANIUS_PORT", str(core_daemon))
    result, latency_ms = asyncio.run(ping(load_config()))
    assert result.server == "0.0.1"
    assert result.uptime_ms >= 0
    assert latency_ms >= 0


# 通过同一长连接并发发送两个 ping 并返回其响应。
async def _send_concurrent_pings(port: int):
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    try:
        return await asyncio.gather(
            client.send_command("core.ping", {"type": "core.ping", "client": "test/one"}),
            client.send_command("core.ping", {"type": "core.ping", "client": "test/two"}),
        )
    finally:
        await client.close()


# 功能：验证长连接客户端能按请求 ID 匹配并发 ping 的响应。
# 设计：在同一个 SocketClient 上同时发出两条请求，覆盖 _pending Future 映射而不依赖响应顺序。
def test_socket_client_matches_concurrent_ping_responses(core_daemon: int) -> None:
    responses = asyncio.run(_send_concurrent_pings(core_daemon))
    assert [response.result["server"] for response in responses] == ["0.0.1", "0.0.1"]
