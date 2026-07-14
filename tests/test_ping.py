import asyncio

from manius_code.cli.main import ping


# 功能：验证 CLI 能经由 TCP JSON-RPC 获得 daemon 的 pong 结果。
# 设计：调用真实客户端函数并连接子进程 daemon，覆盖完整的 S0 跨进程路径。
def test_ping_returns_daemon_metadata(core_daemon: int, monkeypatch) -> None:
    monkeypatch.setenv("MANIUS_PORT", str(core_daemon))
    result, latency_ms = asyncio.run(ping())
    assert result.server == "0.0.1"
    assert result.uptime_ms >= 0
    assert latency_ms >= 0
