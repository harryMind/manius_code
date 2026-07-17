from __future__ import annotations

import argparse
import asyncio

from manius_code.core.agent.runner import AgentRunner
from manius_code.core.config import ManiusConfig


# 向 CLI 解析器注册前台 Agent 运行命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("run", help="Run an Agent task in the foreground")
    parser.add_argument("--goal", required=True, help="Task goal for the Agent")
    parser.set_defaults(handler=run)


# 运行 Agent 并以进程状态码表示任务是否成功完成。
def run(config: ManiusConfig, goal: str) -> None:
    summary = asyncio.run(AgentRunner(config).run(goal))
    if summary.status != "success":
        raise SystemExit(1)
