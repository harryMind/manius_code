import argparse

import manius_code
from manius_code.cli.commands import chat, ping, run, trace
from manius_code.core.config import ConfigError, load_config


# 创建 CLI 顶层解析器并注册所有子命令。
def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manius")
    parser.add_argument("--version",help="Print version and exit",action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=False)

    ping.register(subparsers)
    run.register(subparsers)
    chat.register(subparsers)
    trace.register(subparsers)
    return parser


# 加载配置、解析命令并将控制权交给对应子命令。
def main() -> None:
    parser = _create_parser()
    arguments = parser.parse_args()
    # 顶层命令
    if arguments.version:
        print(manius_code.__version__)
        return
    try:
        config = load_config()
    except ConfigError as error:
        parser.exit(1, f"manius: {error}\n")
    command_arguments = vars(arguments).copy()
    for name in ("command", "handler", "version"):
        command_arguments.pop(name, None)
    arguments.handler(config, **command_arguments)
