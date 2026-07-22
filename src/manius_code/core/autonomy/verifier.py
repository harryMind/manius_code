from __future__ import annotations

from manius_code.core.autonomy.contracts import PlanStep, StepResult, VerificationResult
from manius_code.core.tools.paths import resolve_workspace_path


class Verifier:
    # 按步骤声明的结构化验收条件验证工具执行结果和产物。
    def verify(self, step: PlanStep, result: StepResult) -> VerificationResult:
        evidence: list[str] = []
        for criterion in step.acceptance_criteria:
            if criterion.kind == "tool_result_contains":
                if criterion.expected is None or criterion.expected not in result.observation:
                    return VerificationResult(passed=False, reason=f"tool result does not contain {criterion.expected!r}")
                evidence.append(f"tool result contains {criterion.expected!r}")
            elif criterion.kind == "file_exists":
                if criterion.path is None or not resolve_workspace_path(criterion.path).is_file():
                    return VerificationResult(passed=False, reason=f"expected file does not exist: {criterion.path}")
                evidence.append(f"file exists: {criterion.path}")
            elif criterion.kind == "file_contains":
                if criterion.path is None or criterion.expected is None:
                    return VerificationResult(passed=False, reason="file_contains requires path and expected")
                path = resolve_workspace_path(criterion.path)
                if not path.is_file() or criterion.expected not in path.read_text(encoding="utf-8"):
                    return VerificationResult(passed=False, reason=f"file does not contain expected text: {criterion.path}")
                evidence.append(f"file contains expected text: {criterion.path}")
        return VerificationResult(passed=True, evidence=evidence)
