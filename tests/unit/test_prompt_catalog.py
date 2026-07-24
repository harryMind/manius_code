from manius_code.core.prompt import action_instruction, legacy_agent_instruction, plan_instruction, resolver_instruction, summary_instruction


# 功能：验证核心运行时使用的全部系统提示词均可从 core.prompt 统一按需取得。
# 设计：直接覆盖公开导出函数，避免测试依赖某个厂商 Provider，从而保持提示词目录可独立替换和复用。
def test_prompt_catalog_exposes_all_runtime_instructions() -> None:
    instructions = [
        legacy_agent_instruction(),
        plan_instruction(),
        action_instruction(),
        resolver_instruction(),
        summary_instruction(),
    ]

    assert all(instruction.strip() for instruction in instructions)
    assert len(set(instructions)) == len(instructions)


# 功能：验证规划提示词使用真实 PlanProposal 字段并明确目标导向分解规则。
# 设计：直接检查公开提示词的关键 Schema 字段和验收组合，防止提示词与 Pydantic 契约逐步漂移。
def test_plan_instruction_matches_plan_proposal_schema_and_goal_based_decomposition() -> None:
    instruction = plan_instruction()

    assert "Goal-based Decomposition" in instruction
    assert "goal and steps" in instruction
    assert "id, title, description, dependencies, allowed_tools, acceptance_criteria, and artifacts" in instruction
    assert "status, attempt_count, or last_error" in instruction
    assert "tool_result_contains" in instruction
    assert "file_exists" in instruction
    assert "file_contains" in instruction
