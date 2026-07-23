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
