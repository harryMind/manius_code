from __future__ import annotations

from pathlib import Path

from manius_code.core.autonomy.contracts import ActionProposal, PlanProposal, PlanStep
from manius_code.core.tools.paths import resolve_workspace_path


class AuditError(ValueError):
    pass


class Auditor:
    # 注入允许工具和受限工作区以统一审计计划与动作路径。
    def __init__(self, allowed_tools: set[str], workspace: Path) -> None:
        self._allowed_tools = allowed_tools
        self._workspace = workspace.expanduser().resolve()

    # 校验计划 DAG、工具声明和验收条件是否满足机器规则。
    def approve_plan(self, proposal: PlanProposal) -> None:
        identifiers = [step.id for step in proposal.steps]
        if len(identifiers) != len(set(identifiers)):
            raise AuditError("plan step IDs must be unique")
        known_steps = set(identifiers)
        for step in proposal.steps:
            if step.id in step.dependencies:
                raise AuditError(f"step {step.id} cannot depend on itself")
            unknown_dependencies = set(step.dependencies) - known_steps
            if unknown_dependencies:
                raise AuditError(f"step {step.id} has unknown dependencies: {sorted(unknown_dependencies)}")
            unknown_tools = set(step.allowed_tools) - self._allowed_tools
            if unknown_tools:
                raise AuditError(f"step {step.id} uses unavailable tools: {sorted(unknown_tools)}")
            if len(step.allowed_tools) != 1:
                raise AuditError(f"step {step.id} must allow exactly one tool")
            if len(step.artifacts) > 1:
                raise AuditError(f"step {step.id} must declare at most one artifact")
            if not step.acceptance_criteria:
                raise AuditError(f"step {step.id} must declare acceptance criteria")
            criterion_paths = {criterion.path for criterion in step.acceptance_criteria if criterion.path is not None}
            if len(criterion_paths) > 1:
                raise AuditError(f"step {step.id} cannot verify multiple file outputs")
            if step.artifacts and criterion_paths and step.artifacts[0] not in criterion_paths:
                raise AuditError(f"step {step.id} artifact must match its acceptance path")
            for criterion in step.acceptance_criteria:
                if criterion.path is not None:
                    try:
                        resolve_workspace_path(criterion.path, self._workspace)
                    except ValueError as error:
                        raise AuditError(f"step {step.id} has an unsafe acceptance path") from error
        self._assert_acyclic(proposal.steps)

    # 校验动作只会作用于当前步骤允许的工具和工作区路径。
    def approve_action(self, step: PlanStep, proposal: ActionProposal) -> None:
        if proposal.step_id != step.id:
            raise AuditError("action step does not match the scheduled step")
        if proposal.tool_name not in step.allowed_tools:
            raise AuditError(f"tool {proposal.tool_name} is not allowed for step {step.id}")
        path = proposal.arguments.get("path")
        if isinstance(path, str):
            try:
                resolve_workspace_path(path, self._workspace)
            except ValueError as error:
                raise AuditError("action path must stay within the workspace") from error

    # 通过深度优先遍历拒绝任意环状依赖。
    def _assert_acyclic(self, steps: list[PlanStep]) -> None:
        dependencies = {step.id: step.dependencies for step in steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        # 深度遍历单个步骤依赖以检测回边。
        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise AuditError("plan dependencies must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in dependencies:
            visit(step_id)
