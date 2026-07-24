from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import suppress

from pydantic import ValidationError

from manius_code.cli.commands.run import _watch_remote
from manius_code.core.bus.commands import SessionCreateResult, SessionGetResult
from manius_code.core.config import ManiusConfig
from manius_code.core.transport.socket_client import IpcError, SocketClient


# 向 CLI 顶层解析器注册可创建或恢复持久会话的交互式聊天命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("chat", help="Start or resume a persistent Agent session")
    parser.add_argument("--session-id", help="Existing session identifier to resume")
    parser.set_defaults(handler=chat)


# 创建新会话或确认指定会话存在，并返回客户端后续要持续使用的会话标识。
async def _open_session(config: ManiusConfig, session_id: str | None) -> str:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
        if session_id is None:
            response = await client.send_command("session.create", {"type": "session.create", "client_id": "cli"})
            return SessionCreateResult.model_validate(response.result).session.session_id
        response = await client.send_command("session.get", {"type": "session.get", "session_id": session_id})
        return SessionGetResult.model_validate(response.result).session.session_id
    finally:
        with suppress(OSError, RuntimeError, ValueError):
            await client.close()


# 在同一会话中发送一个目标并复用既有 run 观察器完整回放、订阅和终端渲染。
async def _send_and_watch(config: ManiusConfig, session_id: str, goal: str) -> None:
    await _watch_remote(
        config,
        "session.send",
        {"type": "session.send", "session_id": session_id, "content": goal},
    )


# 运行终端 REPL，连续提交目标直到用户输入 exit、文件结束或中断。
async def _chat_loop(config: ManiusConfig, session_id: str) -> None:
    print(f"manius chat session: {session_id}")
    print("Enter a goal, or type exit to leave the session.")
    while True:
        try:
            content = await asyncio.to_thread(input, "manius> ")
        except EOFError:
            return
        goal = content.strip()
        if goal.lower() in {"exit", "quit"}:
            return
        if not goal:
            continue
        try:
            await _send_and_watch(config, session_id, goal)
        except IpcError as error:
            print(f"manius: IPC request failed: {error}", file=sys.stderr)
        except (OSError, ValidationError) as error:
            print(f"manius: unable to run task: {error}", file=sys.stderr)


# 进入持久会话聊天模式，并将连接或参数问题映射为清晰的 CLI 退出状态。
def chat(config: ManiusConfig, session_id: str | None = None) -> None:
    try:
        active_session_id = asyncio.run(_open_session(config, session_id))
        asyncio.run(_chat_loop(config, active_session_id))
    except KeyboardInterrupt:
        return
    except IpcError as error:
        print(f"manius: IPC request failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValidationError) as error:
        print(f"manius: unable to open session: {error}", file=sys.stderr)
        raise SystemExit(1) from None
