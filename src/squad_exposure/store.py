"""只追加的领域事件存储。

职责：
- JSONL 原子追加与整段重放；
- event_id 业务幂等：同号同内容直接识别为重放，同号异内容拒绝并隔离；
- 聚合内版本号乐观并发（expected_version）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import validate_event


class StorageError(RuntimeError):
    """存储层通用错误。"""


class EventConflictError(StorageError):
    """相同事件标识对应不同内容，必须隔离而不是静默覆盖。"""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件标识冲突，内容不一致: {event_id}")
        self.event_id = event_id


class AggregateVersionError(StorageError):
    """聚合版本号断档或回退。"""


def _canonical(event: Mapping[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EventStore:
    def __init__(self, path: str | os.PathLike[str], schema: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.schema = schema
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            self._index(event, line_no)

    def _index(self, event: Mapping[str, Any], line_no: int | None = None) -> None:
        event_id = event.get("event_id")
        aggregate_id = event.get("aggregate_id")
        version = event.get("version")
        existing = self._by_id.get(event_id)
        if existing is not None:
            if _canonical(existing) != _canonical(event):
                raise EventConflictError(event_id)
            return
        if isinstance(aggregate_id, str) and isinstance(version, int):
            if self._versions.get(aggregate_id, 0) + 1 != version:
                where = f"第 {line_no} 行" if line_no else "新追加"
                raise AggregateVersionError(
                    f"{where} 聚合 {aggregate_id} 版本断档: 期望 {self._versions.get(aggregate_id, 0) + 1}，实际 {version}"
                )
            self._versions[aggregate_id] = version
        self._events.append(dict(event))
        self._by_id[event_id] = dict(event)

    # ---- 读侧 ----

    def events(self, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        if aggregate_id is None:
            return [dict(e) for e in self._events]
        return [dict(e) for e in self._events if e["aggregate_id"] == aggregate_id]

    def next_version(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0) + 1

    def current_version(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    def contains(self, event_id: str) -> bool:
        return event_id in self._by_id

    # ---- 写侧 ----

    def append(self, event: Mapping[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        return self.append_many([event], expected_versions={event["aggregate_id"]: expected_version} if expected_version is not None else None)[0]

    def append_many(
        self,
        events: Sequence[Mapping[str, Any]],
        expected_versions: Mapping[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        """整批通过校验与索引后一次性落盘，保证原子可见。"""
        if not events:
            return []
        committed = [dict(e) for e in self._events]
        staged: list[dict[str, Any]] = []
        pending_expected = dict(expected_versions or {})
        try:
            for raw in events:
                event = dict(raw)
                if self.schema is not None:
                    issues = validate_event(event, self.schema)
                    if issues:
                        detail = "；".join(f"{i.field} {i.code}" for i in issues)
                        raise StorageError(f"事件未通过契约校验: {detail}")
                if event["event_id"] in self._by_id:
                    existing = self._by_id[event["event_id"]]
                    if _canonical(existing) != _canonical(event):
                        raise EventConflictError(event["event_id"])
                    staged.append(dict(existing))
                    continue
                aggregate_id = event["aggregate_id"]
                base = pending_expected.pop(aggregate_id, None)
                if base is not None and base + 1 != event["version"]:
                    raise AggregateVersionError(
                        f"聚合 {aggregate_id} 乐观并发失败: 调用方基于 v{base}，存储当前 v{self._versions.get(aggregate_id, 0)}，"
                        f"新事件必须是 v{base + 1}，实际 v{event['version']}"
                    )
                self._index(event)
                staged.append(dict(event))
        except BaseException:
            self._events = committed
            self._by_id = {e["event_id"]: dict(e) for e in committed}
            self._versions = {}
            for e in committed:
                self._versions[e["aggregate_id"]] = e["version"]
            raise

        fd, tmp_name = tempfile.mkstemp(prefix=".events-", suffix=".jsonl", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for e in self._events:
                    handle.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return staged
