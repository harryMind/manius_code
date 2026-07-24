from __future__ import annotations

from pathlib import Path

from manius_code.core.autonomy.contracts import AuditResult, AuditViolation, ActionProposal, PlanProposal, PlanStep
from manius_code.core.tools.paths import resolve_workspace_path


class Auditor:
    # 注入允许工具和受限工作区以统一审计计划与动作路径。
    def __init__(self, allowed_tools: set[str], workspace: Path) -> None:
        self._allowed_tools = allowed_tools
        self._workspace = workspace.expanduser().resolve()

    # 返回计划的全部机器规则违规项而不抛出会终止 Agent 的异常。
    def approve_plan(self, proposal: PlanProposal) -> AuditResult:
        violations: list[AuditViolation] = []
        identifiers = [step.id for step in proposal.steps]
        if len(identifiers) != len(set(identifiers)):
            violations.append(AuditViolation(code="duplicate_step_id", message="plan step IDs must be unique"))
        known_steps = set(identifiers)
        for step in proposal.steps:
            if step.id in step.dependencies:
                violations.append(
                    AuditViolation(code="self_dependency", message=f"step {step.id} cannot depend on itself")
                )
            unknown_dependencies = set(step.dependencies) - known_steps
            if unknown_dependencies:
                violations.append(
                    AuditViolation(
                        code="unknown_dependency",
                        message=f"step {step.id} has unknown dependencies: {sorted(unknown_dependencies)}",
                    )
                )
            unknown_tools = set(step.allowed_tools) - self._allowed_tools
            if unknown_tools:
                violations.append(
                    AuditViolation(
                        code="unavailable_tool",
                        message=f"step {step.id} uses unavailable tools: {sorted(unknown_tools)}",
                    )
                )
            if len(step.allowed_tools) != 1:
                violations.append(
                    AuditViolation(code="tool_count", message=f"step {step.id} must allow exactly one tool")
                )
            if len(step.artifacts) > 1:
                violations.append(
                    AuditViolation(code="artifact_count", message=f"step {step.id} must declare at most one artifact")
                )
            if not step.acceptance_criteria:
                violations.append(
                    AuditViolation(code="missing_acceptance", message=f"step {step.id} must declare acceptance criteria")
                )
            criterion_paths = {criterion.path for criterion in step.acceptance_criteria if criterion.path is not None}
            if len(criterion_paths) > 1:
                violations.append(
                    AuditViolation(
                        code="multiple_output_paths",
                        message=f"step {step.id} cannot verify multiple file outputs",
                    )
                )
            if step.artifacts and criterion_paths and step.artifacts[0] not in criterion_paths:
                violations.append(
                    AuditViolation(
                        code="artifact_path_mismatch",
                        message=f"step {step.id} artifact must match its acceptance path",
                    )
                )
            for criterion in step.acceptance_criteria:
                if criterion.path is None:
                    continue
                try:
                    resolve_workspace_path(criterion.path, self._workspace)
                except ValueError:
                    violations.append(
                        AuditViolation(
                            code="unsafe_acceptance_path",
                            message=f"step {step.id} acceptance path is outside the workspace: {criterion.path}",
                        )
                    )
        if self._has_cycle(proposal.steps):
            violations.append(AuditViolation(code="cyclic_dependencies", message="plan dependencies must be acyclic"))
        return self._result(violations)

    # 返回动作的机器规则违规项，使运行器能够只重试当前步骤。
    def approve_action(self, step: PlanStep, proposal: ActionProposal) -> AuditResult:
        violations: list[AuditViolation] = []
        if proposal.step_id != step.id:
            violations.append(
                AuditViolation(code="step_mismatch", message="action step does not match the scheduled step")
            )
        if proposal.tool_name not in step.allowed_tools:
            violations.append(
                AuditViolation(
                    code="tool_not_allowed",
                    message=(
                        f"tool {proposal.tool_name!r} is not allowed for step {step.id}; "
                        f"allowed tools: {', '.join(step.allowed_tools)}"
                    ),
                )
            )
        path = proposal.arguments.get("path")
        if isinstance(path, str):
            try:
                resolve_workspace_path(path, self._workspace)
            except ValueError:
                violations.append(
                    AuditViolation(
                        code="unsafe_action_path",
                        message=f"action path is outside the workspace: {path}",
                    )
                )
        return self._result(violations)

    # 将违规列表规范为可持久化、可注入模型上下文的紧凑审计结果。
    def _result(self, violations: list[AuditViolation]) -> AuditResult:
        return AuditResult(
            approved=not violations,
            summary="; ".join(violation.message for violation in violations),
            violations=violations,
        )

    # 通过深度优先遍历判断计划依赖图是否存在环。
    def _has_cycle(self, steps: list[PlanStep]) -> bool:
        dependencies = {step.id: step.dependencies for step in steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        # 深度遍历单个步骤并在回到访问中节点时标记依赖环。
        def visit(step_id: str) -> bool:
            if step_id in visiting:
                return True
            if step_id in visited:
                return False
            visiting.add(step_id)
            has_cycle = any(visit(dependency) for dependency in dependencies[step_id] if dependency in dependencies)
            visiting.remove(step_id)
            visited.add(step_id)
            return has_cycle

        return any(visit(step_id) for step_id in dependencies)
