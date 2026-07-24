from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


# 返回统一使用 UTC 的会话时间戳，保证落盘记录可跨进程稳定排序。
def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionMeta(BaseModel):
    session_id: str
    client_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    run_ids: list[str] = Field(default_factory=list)
    turn_count: int = Field(default=0, ge=0)


class ThreadEntry(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None = None
    timestamp: datetime = Field(default_factory=_now)


class SessionNote(BaseModel):
    id: int = Field(ge=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    source_run_id: str
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


@dataclass(frozen=True)
class SessionRunRequest:
    session_id: str
    run_id: str
    goal: str
    system_context: str
