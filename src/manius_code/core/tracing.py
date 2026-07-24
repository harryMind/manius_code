import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import BaseModel


TraceDirection = Literal[
    "CLIENT->CORE",
    "CORE>CLIENT",
    "CORE",
    "CORE>LLM",
    "LLM>CORE",
]
TraceLayer = Literal["ipc", "event", "llm", "session"]

logger = logging.getLogger(__name__)
_INDEX_VERSION = 1


class TraceRecord(BaseModel):
    ts: str
    direction: TraceDirection
    layer: TraceLayer
    kind: str
    run_id: str | None = None
    step: int | None = None
    client_id: str | None = None
    trace_id: str | None = None
    data: dict[str, Any]


# 返回按时间顺序排列的归档和活动追踪文件路径。
def trace_paths(path: Path) -> list[Path]:
    paths: list[Path] = []
    for entry in _load_index(path)["files"]:
        name = entry.get("file")
        if not isinstance(name, str) or Path(name).name != name:
            continue
        archive = path.parent / name
        if archive.is_file():
            paths.append(archive)
    if path.is_file():
        paths.append(path)
    return paths


# 生成与活动 JSONL 文件配套的归档索引路径。
def _index_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.index.json")


# 读取索引文件并在文件缺失或损坏时返回空索引。
def _load_index(path: Path) -> dict[str, Any]:
    empty_index: dict[str, Any] = {"version": _INDEX_VERSION, "files": []}
    index_path = _index_path(path)
    if not index_path.is_file():
        return empty_index
    try:
        content = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_index
    files = content.get("files") if isinstance(content, dict) else None
    if not isinstance(files, list):
        return empty_index
    return {"version": _INDEX_VERSION, "files": [entry for entry in files if isinstance(entry, dict)]}


class TracingProvider:
    # 初始化全局追踪文件、内存队列和后台写入任务状态。
    def __init__(
        self,
        path: Path,
        max_queue_size: int = 10_000,
        max_size_mb: int = 10,
        backup_count: int = 5,
    ) -> None:
        if max_size_mb < 1:
            raise ValueError("max_size_mb must be at least 1")
        if backup_count < 0:
            raise ValueError("backup_count must not be negative")
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=max_queue_size)
        self._drain_task: asyncio.Task[None] | None = None
        self._file: TextIO | None = None
        self._accepting = False
        self._dropped_records = 0
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._backup_count = backup_count

    # 在独立落盘协程启动前准备追踪目录和追加写入文件。
    async def start(self) -> None:
        if self._drain_task is not None:
            return
        await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self._ensure_index)
        self._file = await asyncio.to_thread(self._path.open, "a", encoding="utf-8")
        self._accepting = True
        self._drain_task = asyncio.create_task(self._drain(), name="manius-trace-drain")

    # 将完整追踪记录无阻塞地放入有界队列。
    def emit(
        self,
        direction: TraceDirection,
        layer: TraceLayer,
        kind: str,
        data: dict[str, Any],
        *,
        run_id: str | None = None,
        step: int | None = None,
        client_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        if not self._accepting:
            return
        record = TraceRecord(
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            direction=direction,
            layer=layer,
            kind=kind,
            run_id=run_id,
            step=step,
            client_id=client_id,
            trace_id=trace_id,
            data=data,
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
        lines = [f"{record.model_dump_json()}\n" for record in records]
        self._rotate_if_needed(sum(len(line.encode("utf-8")) for line in lines))
        if self._file is None:
            return
        self._file.writelines(lines)
        self._file.flush()

    # 在下一批记录会超出上限时归档活动文件并创建新的写入文件。
    def _rotate_if_needed(self, incoming_size: int) -> None:
        if self._file is None or not self._path.is_file():
            return
        if self._path.stat().st_size == 0 or self._path.stat().st_size + incoming_size <= self._max_size_bytes:
            return
        self._file.flush()
        self._file.close()
        archive_path = self._next_archive_path()
        try:
            self._path.replace(archive_path)
            self._add_archive_to_index(archive_path)
        finally:
            self._file = self._path.open("a", encoding="utf-8")

    # 生成不会覆盖现有归档文件的 UTC 时间戳名称。
    def _next_archive_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = self._path.with_name(f"{self._path.stem}.{timestamp}{self._path.suffix}")
        sequence = 1
        while candidate.exists():
            candidate = self._path.with_name(f"{self._path.stem}.{timestamp}.{sequence}{self._path.suffix}")
            sequence += 1
        return candidate

    # 将归档元数据写入索引并删除超出保留数量的最旧归档。
    def _add_archive_to_index(self, archive_path: Path) -> None:
        index = _load_index(self._path)
        files = index["files"]
        files.append(self._archive_metadata(archive_path))
        while len(files) > self._backup_count:
            removed = files.pop(0)
            name = removed.get("file")
            if isinstance(name, str) and Path(name).name == name:
                (self._path.parent / name).unlink(missing_ok=True)
        self._write_index(index)

    # 汇总单个归档文件的记录数量、时间范围和字节大小。
    def _archive_metadata(self, archive_path: Path) -> dict[str, Any]:
        first_ts: str | None = None
        last_ts: str | None = None
        record_count = 0
        with archive_path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                record_count += 1
                timestamp = record.get("ts")
                if isinstance(timestamp, str):
                    first_ts = first_ts or timestamp
                    last_ts = timestamp
        return {
            "file": archive_path.name,
            "record_count": record_count,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "size_bytes": archive_path.stat().st_size,
            "rotated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }

    # 在首次启动时创建空索引文件，供 CLI 统一读取历史追踪。
    def _ensure_index(self) -> None:
        if not _index_path(self._path).is_file():
            self._write_index({"version": _INDEX_VERSION, "files": []})

    # 通过临时文件替换写入索引，避免 CLI 读取到半截 JSON。
    def _write_index(self, index: dict[str, Any]) -> None:
        index_path = _index_path(self._path)
        temporary_path = index_path.with_name(f"{index_path.name}.tmp")
        temporary_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(index_path)
