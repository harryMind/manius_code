import logging
from pathlib import Path

from manius_code.core.config import CoreConfig


# 根据当前配置初始化 daemon 日志处理器。
def setup_logging(config: CoreConfig) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_file:
        log_path: Path = config.log_file.expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if config.log_format == "json":
        log_format = '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
    logging.basicConfig(level=config.log_level.upper(), format=log_format, handlers=handlers, force=True)
