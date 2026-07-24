from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from manius_code.core.bus.events import AgentEvent, NoteSavedEvent, SessionCreatedEvent, SessionMessageSentEvent
from manius_code.core.sessions.models import SessionMeta, SessionNote, SessionRunRequest, ThreadEntry
from manius_code.core.sessions.store import SessionStore
from manius_code.core.tracing import TracingProvider

if TYPE_CHECKING:
    from manius_code.core.agent.runner import AgentRunner, RunSummary

SessionRunnerFactory = Callable[[SessionRunRequest], "AgentRunner"]
EventPublisher = Callable[[AgentEvent], None]
TaskObserver = Callable[[str, asyncio.Task["RunSummary"]], None]


class SessionManager:
    # 注入持久化仓库、运行器工厂与现有事件/追踪出口，避免会话层依赖传输实现。
    def __init__(
        self,
        store: SessionStore,
        runner_factory: SessionRunnerFactory,
        event_publisher: EventPublisher,
        *,
        thread_turn_limit: int = 6,
        notes_top_k: int = 5,
        tracer: TracingProvider | None = None,
        task_observer: TaskObserver | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._event_publisher = event_publisher
        self._thread_turn_limit = thread_turn_limit
        self._notes_top_k = notes_top_k
        self._tracer = tracer
        self._task_observer = task_observer
        self._tasks: set[asyncio.Task[RunSummary]] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    # 创建持久化会话并向既有事件流发布会话创建通知。
    async def create_session(self, client_id: str | None = None) -> SessionMeta:
        meta = await asyncio.to_thread(self._store.create, client_id)
        self._locks[meta.session_id] = asyncio.Lock()
        self._emit(SessionCreatedEvent(session_id=meta.session_id, client_id=meta.client_id), "created")
        return meta

    # 在会话中启动一次后台 Agent 运行，并立即返回可订阅的运行标识。
    async def send_message(self, session_id: str, content: str) -> str:
        goal = content.strip()
        if not goal:
            raise ValueError("content must not be blank")
        lock = self._lock_for(session_id)
        async with lock:
            meta = await asyncio.to_thread(self._store.load_meta, session_id)
            system_context = await asyncio.to_thread(self._build_system_context, session_id, goal)
            run_id = uuid4().hex
            meta.run_ids.append(run_id)
            meta.updated_at = _now()
            await asyncio.to_thread(self._store.save_meta, meta)
        request = SessionRunRequest(
            session_id=session_id,
            run_id=run_id,
            goal=goal,
            system_context=system_context,
        )
        task = asyncio.create_task(self._run_and_record(request), name=f"manius-session-{session_id}-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if self._task_observer is not None:
            self._task_observer(run_id, task)
        self._emit(SessionMessageSentEvent(session_id=session_id, run_id=run_id, content=goal), "message_sent", run_id)
        return run_id

    # 读取一个会话的元数据与完整短期对话摘要，供 IPC 查询和恢复会话使用。
    async def get_session(self, session_id: str) -> tuple[SessionMeta, list[ThreadEntry]]:
        meta, thread = await asyncio.gather(
            asyncio.to_thread(self._store.load_meta, session_id),
            asyncio.to_thread(self._store.load_thread, session_id),
        )
        return meta, thread

    # 列出全部持久化会话并保持最近活跃会话优先的顺序。
    async def list_sessions(self) -> list[SessionMeta]:
        return await asyncio.to_thread(self._store.list_meta)

    # 丢弃进程内会话锁而保留所有会话磁盘数据，允许后续跨连接重新加载。
    async def destroy_session(self, session_id: str) -> bool:
        await asyncio.to_thread(self._store.load_meta, session_id)
        self._locks.pop(session_id, None)
        self._trace("destroyed", {"session_id": session_id})
        return True

    # 为当前会话异步保存一条结构化笔记，并通过既有事件通道公开笔记创建结果。
    async def save_note(
        self,
        session_id: str,
        title: str,
        content: str,
        tags: list[str],
        source_run_id: str,
    ) -> SessionNote:
        lock = self._lock_for(session_id)
        async with lock:
            note = await asyncio.to_thread(
                self._store.create_note,
                session_id,
                title.strip(),
                content.strip(),
                [tag.strip() for tag in tags if tag.strip()],
                source_run_id,
            )
            meta = await asyncio.to_thread(self._store.load_meta, session_id)
            meta.updated_at = _now()
            await asyncio.to_thread(self._store.save_meta, meta)
        self._emit(
            NoteSavedEvent(
                session_id=session_id,
                run_id=source_run_id,
                note_id=note.id,
                title=note.title,
            ),
            "note_saved",
            source_run_id,
        )
        return note

    # 取消并等待所有会话发起的后台运行，供 daemon 生命周期有序关闭。
    async def stop(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 执行会话运行并在无论成功或失败后把本轮用户目标和最终摘要写回短期记忆。
    async def _run_and_record(self, request: SessionRunRequest) -> RunSummary:
        result_text = ""
        try:
            summary = await self._runner_factory(request).run(request.goal, request.run_id)
            result_text = summary.result if summary.status == "success" else summary.reason or "任务执行失败"
            return summary
        except asyncio.CancelledError:
            result_text = "任务已被 daemon 停止"
            raise
        except Exception as error:
            result_text = f"任务启动失败: {error}"
            raise
        finally:
            await self._record_turn(request, result_text)

    # 以同会话锁串行写入双角色摘要和更新后的活跃时间，保持对话顺序稳定。
    async def _record_turn(self, request: SessionRunRequest, result_text: str) -> None:
        lock = self._lock_for(request.session_id)
        async with lock:
            await asyncio.to_thread(
                self._store.append_thread,
                request.session_id,
                ThreadEntry(role="user", content=request.goal),
            )
            await asyncio.to_thread(
                self._store.append_thread,
                request.session_id,
                ThreadEntry(role="assistant", content=result_text or "任务未返回摘要", run_id=request.run_id),
            )
            meta = await asyncio.to_thread(self._store.load_meta, request.session_id)
            meta.turn_count += 1
            meta.updated_at = _now()
            await asyncio.to_thread(self._store.save_meta, meta)
        self._trace("turn_recorded", {"session_id": request.session_id, "run_id": request.run_id}, request.run_id)

    # 按配置截取最近对话轮次和目标相关笔记，构造成注入 LLM 系统提示的紧凑背景。
    def _build_system_context(self, session_id: str, goal: str) -> str:
        entries = self._store.load_thread(session_id)[-(self._thread_turn_limit * 2) :]
        notes = self._store.retrieve_notes(session_id, goal, self._notes_top_k)
        sections: list[str] = []
        if entries:
            thread = "\n".join(f"- {entry.role}: {entry.content}" for entry in entries)
            sections.append(f"Recent session summaries (background only):\n{thread}")
        if notes:
            note_text = "\n".join(
                f"- [{note.id}] {note.title} ({', '.join(note.tags) or 'untagged'}): {note.content}"
                for note in notes
            )
            sections.append(f"Relevant saved notes (background only):\n{note_text}")
        if not sections:
            return ""
        return "Use the following session background only when it is relevant. The current user goal remains authoritative.\n\n" + "\n\n".join(sections)

    # 获取或延迟创建会话级异步锁，保证同一会话的元数据和笔记写入不竞争。
    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # 同时发布会话领域事件和结构化追踪记录，复用已有 IPC 广播与 Trace 文件。
    def _emit(self, event: AgentEvent, kind: str, run_id: str | None = None) -> None:
        self._event_publisher(event)
        self._trace(kind, event.model_dump(mode="json"), run_id)

    # 向既有全局 Trace 写入会话域的内部状态变化。
    def _trace(self, kind: str, data: dict[str, object], run_id: str | None = None) -> None:
        if self._tracer is not None:
            self._tracer.emit("CORE", "session", kind, data, run_id=run_id)


# 返回 UTC 时间以保证会话元数据和线程记录可跨进程稳定排序。
def _now() -> datetime:
    return datetime.now(timezone.utc)
