import argparse

from manius_code.cli.commands import ping
from manius_code.core.config import ConfigError, load_config


# 创建 CLI 顶层解析器并注册所有子命令。
def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manius")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ping.register(subparsers)
    return parser


# 加载配置、解析命令并将控制权交给对应子命令。
def main() -> None:
    parser = _create_parser()
    arguments = parser.parse_args()
    try:
        config = load_config()
    except ConfigError as error:
        parser.exit(1, f"manius: {error}\n")
    arguments.handler(config)
