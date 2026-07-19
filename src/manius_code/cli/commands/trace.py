from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from manius_code.core.config import ManiusConfig


# 向顶层 CLI 解析器注册全局追踪文件的查看子命令。
def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("trace", help="Inspect daemon trace records")
    actions = parser.add_subparsers(dest="trace_action", required=True)

    tail_parser = actions.add_parser("tail", help="Follow new trace records")
    tail_parser.set_defaults(handler=tail)

    filter_parser = actions.add_parser("filter", help="Filter persisted trace records")
    filters = filter_parser.add_mutually_exclusive_group(required=True)
    filters.add_argument("--run-id", help="Show records for one Agent run")
    filters.add_argument("--direction", choices=("client_to_core", "core_to_client", "core_event", "core_to_llm", "llm_to_core"))
    filter_parser.set_defaults(handler=filter_records)

    llm_parser = actions.add_parser("llm", help="Show LLM request and response traces")
    llm_parser.add_argument("--run-id", required=True, help="Agent run identifier")
    llm_parser.set_defaults(handler=llm)


# 持续输出追踪文件中新追加的有效 JSON Lines 记录。
def tail(config: ManiusConfig, **_: object) -> None:
    path = config.trace.file
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
        if record.get("direction") not in {"core_to_llm", "llm_to_core"}:
            continue
        print(json.dumps(record, ensure_ascii=False))


# 从 JSONL 追踪文件读取并忽略损坏或非对象记录。
def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


# 校验并原样输出 tail 读取到的单条 JSONL 记录。
def _print_record(line: str) -> None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(record, dict):
        print(json.dumps(record, ensure_ascii=False))
