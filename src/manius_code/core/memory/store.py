from __future__ import annotations

import json
from pathlib import Path

from manius_code.core.autonomy.contracts import Plan, StepResult


class MemoryStore:
    # 初始化 run 内摘要文件和按工作区隔离的项目情景记忆文件。
    def __init__(self, run_dir: Path, workspace: Path) -> None:
        self._run_path = run_dir / "plan" / "memory.json"
        self._project_path = workspace / ".manius" / "memory" / "episodes.jsonl"

    # 读取最近经过验证的项目经验并限制返回数量。
    def retrieve(self, limit: int = 3) -> list[str]:
        if not self._project_path.is_file():
            return []
        records: list[str] = []
        for line in self._project_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            summary = record.get("summary") if isinstance(record, dict) else None
            if isinstance(summary, str):
                records.append(summary)
        return records[-limit:]

    # 在任务成功后同时写入 run 摘要和可检索项目情景记忆。
    def record_verified(self, goal: str, summary: str, plan: Plan, history: list[StepResult]) -> None:
        record = {
            "goal": goal,
            "summary": summary,
            "plan_version": plan.version,
            "verified_steps": [
                {"id": step.id, "title": step.title, "allowed_tools": step.allowed_tools, "artifacts": step.artifacts}
                for step in plan.steps
                if step.status == "succeeded"
            ],
            "tool_preferences": list(dict.fromkeys(item.tool_name for item in history if item.tool_name is not None)),
            "recovered_failures": [item.error for item in history if item.error is not None],
        }
        self._run_path.parent.mkdir(parents=True, exist_ok=True)
        self._run_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._project_path.parent.mkdir(parents=True, exist_ok=True)
        with self._project_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
