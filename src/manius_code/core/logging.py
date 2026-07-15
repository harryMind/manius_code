import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

from manius_code.core.config import ManiusConfig


# 根据当前配置初始化 daemon 日志处理器。
def setup_logging(cfg: ManiusConfig):
    root_logger = logging.getLogger()
    root_logger.setLevel(cfg.log.level)

    if cfg.log.file is None:
        # 控制台输出
        handler = logging.StreamHandler()
    else:
        path = cfg.log.file.expanduser()
        path.parent.mkdir(exist_ok=True, parents=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=cfg.log.max_size_mb * 1024 * 1024,
            backupCount=cfg.log.backup_count,
            encoding="utf-8"
        )

    if cfg.log.format == "json":
        # json格式化处理器
        pass
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    root_logger.addHandler(handler)