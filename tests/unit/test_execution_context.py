import pytest

from manius_code.core.agent.context import ExecutionContext


# 功能：验证运行中的上下文可被标记成功或失败，并进入终止状态。
# 设计：分别构造两个全新上下文，覆盖两种合法终止流转而不依赖 AgentLoop。
def test_execution_context_marks_terminal_states() -> None:
    successful = ExecutionContext(run_id="run-success", goal="goal")
    failed = ExecutionContext(run_id="run-failed", goal="goal")
    successful.mark_success("done")
    failed.mark_failed("tool failed")
    assert successful.status == "success"
    assert successful.result == "done"
    assert successful.is_done()
    assert failed.status == "failed"
    assert failed.reason == "tool failed"
    assert failed.is_done()


# 功能：验证终止状态不能再次流转为另一种终止状态。
# 设计：先完成一次合法状态变更，再断言两个标记入口都拒绝重复或反向变更。
def test_execution_context_rejects_illegal_terminal_transitions() -> None:
    context = ExecutionContext(run_id="run-1", goal="goal")
    context.mark_success()
    with pytest.raises(ValueError, match="Cannot mark success"):
        context.mark_success()
    with pytest.raises(ValueError, match="Cannot mark success"):
        context.mark_failed("late failure")
