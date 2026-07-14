import asyncio
import logging
import signal
import time

from .bus.commands import PingCommand, PongResult
from .config import load_config
from .logging import setup_logging
from .transport.socket_server import SocketServer

SERVER_VERSION = "0.0.1"
logger = logging.getLogger(__name__)


class CoreApp:
    # 记录 daemon 启动时刻以计算后续的运行时间。
    def __init__(self) -> None:
        self._started_at = time.monotonic()

    # 处理 ping 命令并返回 daemon 版本和已运行时间。
    async def _ping(self, _: PingCommand) -> PongResult:
        uptime_ms = round((time.monotonic() - self._started_at) * 1000)
        return PongResult(server=SERVER_VERSION, uptime_ms=uptime_ms)

    # 启动 daemon 并在收到终止信号后有序关闭服务器。
    async def run(self) -> None:
        config = load_config()
        setup_logging(config)
        server = SocketServer(config.host, config.port)
        server.register("core.ping", self._ping)
        await server.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except NotImplementedError:
                pass
        try:
            await stopped.wait()
        finally:
            await server.stop()
            logger.info("manius-core stopped")


# 运行 manius-core 的命令行入口。
def main() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
