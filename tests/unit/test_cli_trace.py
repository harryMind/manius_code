import argparse
import json
from pathlib import Path

from manius_code.cli.commands import trace
from manius_code.core.config import ManiusConfig, TraceConfig


# 功能：验证 trace follow --line 会跨归档索引输出最后指定数量的记录后开始跟随。
# 设计：构造一个归档和活动文件并替换阻塞跟随函数，直接断言参数解析与初始回放顺序。
def test_trace_follow_prints_requested_recent_records_from_index(tmp_path: Path, monkeypatch, capsys) -> None:
    trace_path = tmp_path / "daemon.jsonl"
    archive_path = tmp_path / "daemon.20260721T000000000000Z.jsonl"
    archive_path.write_text(json.dumps({"sequence": 1}) + "\n", encoding="utf-8")
    trace_path.write_text(
        "".join(f"{json.dumps({'sequence': sequence})}\n" for sequence in range(2, 7)),
        encoding="utf-8",
    )
    (tmp_path / "daemon.index.json").write_text(
        json.dumps({"version": 1, "files": [{"file": archive_path.name}]}),
        encoding="utf-8",
    )
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    trace.register(subparsers)
    arguments = parser.parse_args(["trace", "follow", "--line", "5"])
    monkeypatch.setattr(trace, "_follow_file", lambda _path: None)

    arguments.handler(ManiusConfig(trace=TraceConfig(file=trace_path)), line=arguments.line)

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert arguments.line == 5
    assert [record["sequence"] for record in records] == [2, 3, 4, 5, 6]
