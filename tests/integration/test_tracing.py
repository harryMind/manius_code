import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from manius_code.core.transport.socket_client import SocketClient


# 功能：验证真实 CoreApp 的 core.ping 会写入不关联 run_id 的入站与出站全局追踪记录。
# 设计：使用带临时追踪路径的 daemon 子进程和实际 SocketClient，覆盖 CoreApp 生命周期到 SocketServer 埋点的完整链路。
def test_core_ping_writes_global_ipc_trace(tmp_path: Path, free_port: int) -> None:
    trace_path = tmp_path / "traces" / "daemon.jsonl"
    environment = {
        **os.environ,
        "MANIUS_PORT": str(free_port),
        "MANIUS_TRACE_FILE": str(trace_path),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "manius_code.core.app"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", free_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("manius-core did not start")

        # 通过真实长连接客户端发起一次无 run_id 的全局 ping 命令。
        async def invoke_ping() -> None:
            client = SocketClient("127.0.0.1", free_port)
            await client.connect()
            try:
                await client.send_command("core.ping", {"type": "core.ping", "client": "trace-test"})
            finally:
                await client.close()

        asyncio.run(invoke_ping())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not trace_path.is_file():
            time.sleep(0.02)
        records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    finally:
        process.terminate()
        process.wait(timeout=5)

    request = next(record for record in records if record["direction"] == "client_to_core")
    response = next(record for record in records if record["direction"] == "core_to_client")
    assert request["payload"]["method"] == "core.ping"
    assert request["run_id"] is None
    assert response["trace_id"] == request["trace_id"]
    assert response["run_id"] is None
