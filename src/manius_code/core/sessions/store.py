from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from manius_code.core.sessions.models import SessionMeta, SessionNote, ThreadEntry


class SessionStore:
    # 初始化会话根目录，并将其规范化为后续路径校验使用的绝对路径。
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    # 创建独立会话目录及其初始元数据、对话文件和笔记目录。
    def create(self, client_id: str | None = None) -> SessionMeta:
        self._root.mkdir(parents=True, exist_ok=True)
        session_id = uuid4().hex
        directory = self._session_dir(session_id)
        directory.mkdir(exist_ok=False)
        (directory / "notes").mkdir()
        (directory / "thread.jsonl").touch()
        meta = SessionMeta(session_id=session_id, client_id=client_id)
        self.save_meta(meta)
        return meta

    # 读取一个已持久化会话的元数据，不存在时显式报告会话缺失。
    def load_meta(self, session_id: str) -> SessionMeta:
        path = self._session_dir(session_id) / "meta.json"
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        return SessionMeta.model_validate_json(path.read_text(encoding="utf-8"))

    # 原子替换会话元数据文件，避免读者观察到半写入的 JSON。
    def save_meta(self, meta: SessionMeta) -> None:
        directory = self._session_dir(meta.session_id)
        if not directory.is_dir():
            raise FileNotFoundError(f"session not found: {meta.session_id}")
        self._atomic_write(directory / "meta.json", meta.model_dump_json(indent=2) + "\n")

    # 返回全部历史会话元数据，并按最近活跃时间倒序排列。
    def list_meta(self) -> list[SessionMeta]:
        if not self._root.is_dir():
            return []
        sessions: list[SessionMeta] = []
        for directory in self._root.iterdir():
            if not directory.is_dir():
                continue
            try:
                sessions.append(self.load_meta(directory.name))
            except (FileNotFoundError, ValueError):
                continue
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    # 将一条短期对话摘要追加到会话专属 JSONL 文件。
    def append_thread(self, session_id: str, entry: ThreadEntry) -> None:
        path = self._session_dir(session_id) / "thread.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        with path.open("a", encoding="utf-8") as file:
            file.write(entry.model_dump_json() + "\n")

    # 读取会话短期对话摘要，并跳过损坏的单行记录以保留可恢复性。
    def load_thread(self, session_id: str) -> list[ThreadEntry]:
        path = self._session_dir(session_id) / "thread.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        entries: list[ThreadEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(ThreadEntry.model_validate_json(line))
            except ValueError:
                continue
        return entries

    # 在当前会话笔记目录创建一个只增不改的结构化长期笔记。
    def create_note(self, session_id: str, title: str, content: str, tags: list[str], source_run_id: str) -> SessionNote:
        notes_directory = self._session_dir(session_id) / "notes"
        if not notes_directory.is_dir():
            raise FileNotFoundError(f"session not found: {session_id}")
        note = SessionNote(
            id=self._next_note_id(notes_directory),
            title=title,
            content=content,
            tags=tags,
            source_run_id=source_run_id,
        )
        self._atomic_write(notes_directory / f"note_{note.id}.json", note.model_dump_json(indent=2) + "\n")
        return note

    # 读取会话全部长期笔记，并忽略手工损坏的个别笔记文件。
    def load_notes(self, session_id: str) -> list[SessionNote]:
        notes_directory = self._session_dir(session_id) / "notes"
        if not notes_directory.is_dir():
            raise FileNotFoundError(f"session not found: {session_id}")
        notes: list[SessionNote] = []
        for path in notes_directory.glob("note_*.json"):
            try:
                notes.append(SessionNote.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(notes, key=lambda item: item.id)

    # 以轻量关键词匹配检索当前会话最相关的 Top-K 长期笔记。
    def retrieve_notes(self, session_id: str, query: str, limit: int) -> list[SessionNote]:
        keywords = _keywords(query)
        if not keywords:
            return self.load_notes(session_id)[-limit:]
        ranked: list[tuple[int, datetime, SessionNote]] = []
        for note in self.load_notes(session_id):
            searchable = " ".join([note.title, note.content, *note.tags]).lower()
            score = sum(keyword in searchable for keyword in keywords)
            if score:
                ranked.append((score, note.updated_at, note))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [note for _, _, note in ranked[:limit]]

    # 计算下一个单调递增笔记编号，避免删除或重启导致编号复用。
    def _next_note_id(self, notes_directory: Path) -> int:
        highest = 0
        for path in notes_directory.glob("note_*.json"):
            match = re.fullmatch(r"note_(\d+)\.json", path.name)
            if match is not None:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    # 校验会话标识并返回始终位于会话根目录内的目标路径。
    def _session_dir(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("invalid session_id")
        directory = (self._root / session_id).resolve()
        try:
            directory.relative_to(self._root)
        except ValueError as error:
            raise ValueError("session path must stay within the session root") from error
        return directory

    # 使用同目录临时文件原子写入文本，避免持久化中断留下半成品。
    def _atomic_write(self, path: Path, content: str) -> None:
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


# 提取英文词和单个中文字符，使中文目标也能参与无依赖关键词检索。
def _keywords(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text)
        if len(token) > 1 or "\u4e00" <= token <= "\u9fff"
    }
