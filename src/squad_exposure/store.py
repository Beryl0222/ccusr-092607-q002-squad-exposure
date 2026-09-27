"""只追加的事件存储。

职责：
- JSONL 持久化，重启后按原顺序重放；
- ``event_id`` 业务幂等：同一标识同一内容直接返回已存事件，不重复落库；
- 冲突隔离：同一标识但内容不同，拒绝写入并隔离到 quarantine 目录，
  已提交的事件流不受污染；
- 每个聚合的版本号必须严格 +1，防止并发覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


class StoreError(RuntimeError):
    pass


class EventConflict(StoreError):
    """相同 event_id 但内容不同：调用方输入被隔离，不能进入事件流。"""

    def __init__(self, event_id: str, quarantine_path: Path | None):
        super().__init__(f"事件标识冲突，内容已隔离: {event_id}")
        self.event_id = event_id
        self.quarantine_path = quarantine_path


class AggregateVersionConflict(StoreError):
    pass


@dataclass(frozen=True)
class AppendOutcome:
    event: dict[str, Any]
    deduped: bool  # True 表示命中相同 event_id 的既有事实，未重复落库


@dataclass(frozen=True)
class EventStore:
    path: Path
    quarantine_dir: Path | None = None

    @classmethod
    def open(cls, path: str | Path, quarantine_dir: str | Path | None = None) -> "EventStore":
        store = cls(path=Path(path), quarantine_dir=Path(quarantine_dir) if quarantine_dir else None)
        store._ensure_file()
        return store

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        if self.quarantine_dir is not None:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    # ---- 读取 -----------------------------------------------------------------

    def load(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    # ---- 写入 -----------------------------------------------------------------

    def append(self, event: Mapping[str, Any]) -> AppendOutcome:
        event = dict(event)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        issues = validate_event(event, schema)
        if issues:
            rendered = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise StoreError(f"事件不满足契约: {rendered}")

        events = self.load()
        versions: dict[str, int] = {}
        for prior in events:
            if prior["event_id"] == event["event_id"]:
                if prior == event:
                    return AppendOutcome(event=prior, deduped=True)  # 业务幂等
                quarantine_path = self._quarantine(event)
                raise EventConflict(event["event_id"], quarantine_path)
            versions[prior["aggregate_id"]] = prior["version"]

        expected = versions.get(event["aggregate_id"], 0) + 1
        if event["version"] != expected:
            raise AggregateVersionConflict(
                f"聚合 {event['aggregate_id']} 版本应为 {expected}，收到 {event['version']}"
            )

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return AppendOutcome(event=event, deduped=False)

    def _quarantine(self, event: Mapping[str, Any]) -> Path | None:
        if self.quarantine_dir is None:
            return None
        target = self.quarantine_dir / f"{event['event_id'].replace('/', '_')}.json"
        target.write_text(
            json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        return target
