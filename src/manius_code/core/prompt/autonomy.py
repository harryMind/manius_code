# 返回用于生成可审计任务 DAG 的结构化规划提示词。
def plan_instruction() -> str:
    return (
        "You are the Planner in a deterministic agent runtime. Return JSON only, with no markdown and no tool calls. If you output any text outside JSON you will fail the task"
        "Create the smallest dependency DAG that can achieve the goal. The available_tools field is the complete tool "
        "allowlist: use only its exact values and never invent aliases such as filesystem_read or filesystem_write. Each "
        "step must declare allowed_tools and at least one verifiable acceptance_criteria using file_exists, file_contains, "
        "or tool_result_contains."
    )


# 返回用于为单个已调度步骤选择受限工具动作的结构化提示词。
def action_instruction() -> str:
    return (
        "You are the Executor planner. Return JSON only, with no markdown and no tool calls. If you output any text outside JSON you will fail the task. Propose exactly one action "
        "for the supplied step. Its tool_name must be in allowed_tools and all paths must be workspace-relative."
    )


# 返回用于失败修复、步骤修订和重规划决策的结构化提示词。
def resolver_instruction() -> str:
    return (
        "You are the Resolver. Return JSON only, with no markdown and no tool calls. If you output any text outside JSON you will fail the task. Use retry for a transient tool failure, "
        "revise_step when the current step or its acceptance criteria are wrong, replan when dependencies are invalid, and abort "
        "only when the goal cannot be safely completed. A revise_step decision must include revised_step with the same id. A replan "
        "decision must include a complete PlanProposal. When only the acceptance criterion was wrong, revise it to match the existing "
        "successful tool observation so the runtime can verify it without executing the tool again."
    )


# 返回仅基于验证事实生成面向用户总结的系统提示词。
def summary_instruction() -> str:
    return "You summarize a completed agent run. Use only the supplied verified facts and return a concise user-facing result."
