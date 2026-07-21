from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from manius_code.core.config import ManiusConfig
from manius_code.core.tracing import trace_paths


# 向顶层 CLI 解析器注册全局追踪文件的查看子命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("trace", help="Inspect daemon trace records")
    actions = parser.add_subparsers(dest="trace_action", required=True)

    tail_parser = actions.add_parser("tail", help="Follow new trace records")
    tail_parser.set_defaults(handler=tail)

    follow_parser = actions.add_parser("follow", help="Print recent trace records and follow new records")
    follow_parser.add_argument("--line", type=_positive_line, default=10, help="Number of recent records to print first")
    follow_parser.set_defaults(handler=follow)

    filter_parser = actions.add_parser("filter", help="Filter persisted trace records")
    filters = filter_parser.add_mutually_exclusive_group(required=True)
    filters.add_argument("--run-id", help="Show records for one Agent run")
    filters.add_argument("--direction", choices=("CLIENT->CORE", "CORE>CLIENT", "CORE", "CORE>LLM", "LLM>CORE"))
    filter_parser.set_defaults(handler=filter_records)

    llm_parser = actions.add_parser("llm", help="Show LLM request and response traces")
    llm_parser.add_argument("--run-id", required=True, help="Agent run identifier")
    llm_parser.set_defaults(handler=llm)


# 持续输出追踪文件中新追加的有效 JSON Lines 记录。
def tail(config: ManiusConfig, **_: object) -> None:
    _follow_file(config.trace.file)


# 先输出最后指定数量的追踪记录，再持续跟随活动追踪文件。
def follow(config: ManiusConfig, line: int = 10, **_: object) -> None:
    for record in _read_records(config.trace.file)[-line:]:
        print(json.dumps(record, ensure_ascii=False))
    _follow_file(config.trace.file)


# 将命令行的初始输出行数限制为正整数。
def _positive_line(value: str) -> int:
    try:
        line = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("line must be a positive integer") from error
    if line < 1:
        raise argparse.ArgumentTypeError("line must be a positive integer")
    return line


# 持续读取活动文件新增内容，并在轮转后从新文件开头继续读取。
def _follow_file(path: Path) -> None:
    position = path.stat().st_size if path.is_file() else 0
    try:
        while True:
            if path.is_file():
                size = path.stat().st_size
                if size < position:
                    position = 0
                with path.open("r", encoding="utf-8") as file:
                    file.seek(position)
                    for line in file:
                        _print_record(line)
                    position = file.tell()
            time.sleep(0.2)
    except KeyboardInterrupt:
        return


# 按运行标识或数据流方向过滤并输出已有的全局追踪记录。
def filter_records(config: ManiusConfig, run_id: str | None = None, direction: str | None = None, **_: object) -> None:
    for record in _read_records(config.trace.file):
        if run_id is not None and record.get("run_id") != run_id:
            continue
        if direction is not None and record.get("direction") != direction:
            continue
        print(json.dumps(record, ensure_ascii=False))


# 输出指定任务的完整 LLM 请求和最终响应追踪记录。
def llm(config: ManiusConfig, run_id: str, **_: object) -> None:
    for record in _read_records(config.trace.file):
        if record.get("run_id") != run_id:
            continue
        if record.get("direction") not in {"CORE>LLM", "LLM>CORE"}:
            continue
        print(json.dumps(record, ensure_ascii=False))


# 从 JSONL 追踪文件读取并忽略损坏或非对象记录。
def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for trace_path in trace_paths(path):
        try:
            with trace_path.open("r", encoding="utf-8") as file:
                for line in file:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            continue
    return records


# 校验并原样输出 tail 读取到的单条 JSONL 记录。
def _print_record(line: str) -> None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(record, dict):
        print(json.dumps(record, ensure_ascii=False))
