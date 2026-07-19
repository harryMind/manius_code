import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import BaseModel


TraceDirection = Literal[
    "client_to_core",
    "core_to_client",
    "core_event",
    "core_to_llm",
    "llm_to_core",
]

logger = logging.getLogger(__name__)


class TraceRecord(BaseModel):
    timestamp: str
    direction: TraceDirection
    run_id: str | None = None
    trace_id: str | None = None
    payload: dict[str, Any]


class TracingProvider:
    # 初始化全局追踪文件、内存队列和后台写入任务状态。
    def __init__(self, path: Path, max_queue_size: int = 10_000) -> None:
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=max_queue_size)
        self._drain_task: asyncio.Task[None] | None = None
        self._file: TextIO | None = None
        self._accepting = False
        self._dropped_records = 0

    # 在独立落盘协程启动前准备追踪目录和追加写入文件。
    async def start(self) -> None:
        if self._drain_task is not None:
            return
        await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
        self._file = await asyncio.to_thread(self._path.open, "a", encoding="utf-8")
        self._accepting = True
        self._drain_task = asyncio.create_task(self._drain(), name="manius-trace-drain")

    # 将完整追踪记录无阻塞地放入有界队列。
    def emit(
        self,
        direction: TraceDirection,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        if not self._accepting:
            return
        record = TraceRecord(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            direction=direction,
            run_id=run_id,
            trace_id=trace_id,
            payload=payload,
        )
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped_records += 1
            if self._dropped_records == 1:
                logger.warning("Trace queue is full; dropping trace records")

    # 等待队列清空后关闭写入任务和文件句柄。
    async def stop(self) -> None:
        self._accepting = False
        if self._drain_task is None:
            return
        await self._queue.join()
        self._drain_task.cancel()
        try:
            await self._drain_task
        except asyncio.CancelledError:
            pass
        self._drain_task = None
        if self._file is not None:
            await asyncio.to_thread(self._file.close)
            self._file = None

    # 批量读取待写记录并将文件 I/O 转移到工作线程。
    async def _drain(self) -> None:
        while True:
            records = [await self._queue.get()]
            while True:
                try:
                    records.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self._write_records, records)
            except Exception:
                logger.exception("Unable to write trace records")
            finally:
                for _ in records:
                    self._queue.task_done()

    # 将一批追踪记录以 JSON Lines 格式追加到全局追踪文件。
    def _write_records(self, records: list[TraceRecord]) -> None:
        if self._file is None:
            return
        self._file.writelines(f"{record.model_dump_json()}\n" for record in records)
        self._file.flush()
