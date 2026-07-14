import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest


@pytest.fixture
# 功能：分配一个可供测试 daemon 使用的空闲 TCP 端口。
# 设计：临时绑定端口后立即释放，避免固定端口与本机进程发生冲突。
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
# 功能：启动真实 daemon 子进程并在测试结束后终止它。
# 设计：通过轮询 TCP 连接确认就绪，覆盖与实际 CLI 相同的跨进程通信路径。
def core_daemon(free_port: int) -> Iterator[int]:
    environment = {"MANIUS_PORT": str(free_port)}
    process = subprocess.Popen([sys.executable, "-m", "manius_code.core.app"], env={**os.environ, **environment}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=0.1):
                pass
            break
        except OSError:
            time.sleep(0.05)
    else:
        process.terminate()
        pytest.fail("manius-core did not start")
    yield free_port
    process.terminate()
    process.wait(timeout=5)
