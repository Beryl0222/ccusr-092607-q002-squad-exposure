"""显式推进的模拟时钟。

时间只随 CLOCK_ADVANCED 事件前进（不可回退），重启后从事件流恢复当前时刻。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .store import EventStore

CLOCK_AGGREGATE = "simulation_clock"
CLOCK_ID = "clock:main"


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return dt


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PendingMoment:
    """到达即需要继续处理的时刻。"""

    key: str
    at: datetime
    label: str


class SimulationClock:
    def __init__(self, store: EventStore, start: str = "2026-09-26T09:00:00+08:00") -> None:
        self.store = store
        events = store.events(CLOCK_ID)
        if events:
            self._now = parse_ts(events[-1]["payload"]["to_time"])
        else:
            self._now = parse_ts(start)

    @property
    def now(self) -> datetime:
        return self._now

    def now_iso(self) -> str:
        return format_ts(self._now)

    def advance_to(self, target: str | datetime, reason: str, *, allow_same: bool = False) -> dict[str, Any]:
        """把时间推进到指定时刻；目标早于当前时刻将被拒绝。"""
        target_dt = parse_ts(target) if isinstance(target, str) else target
        if target_dt < self._now or (target_dt == self._now and not allow_same):
            raise ValueError(f"模拟时间只能前进: 当前 {format_ts(self._now)}，目标 {format_ts(target_dt)}")
        if target_dt == self._now:
            return self.store.events(CLOCK_ID)[-1]
        event = {
            "event_id": f"clock-{self.store.next_version(CLOCK_ID)}",
            "event_type": "CLOCK_ADVANCED",
            "aggregate_type": CLOCK_AGGREGATE,
            "aggregate_id": CLOCK_ID,
            "occurred_at": format_ts(target_dt),
            "version": self.store.next_version(CLOCK_ID),
            "payload": {"to_time": format_ts(target_dt), "reason": reason},
        }
        stored = self.store.append(event)
        self._now = target_dt
        return stored

    def advance_through(self, moments: list[PendingMoment]) -> list[tuple[PendingMoment, dict[str, Any]]]:
        """按时间顺序推进过一组里程碑（报名截止、康复复查、赛事结束）。"""
        reached: list[tuple[PendingMoment, dict[str, Any]]] = []
        for moment in sorted(moments, key=lambda m: m.at):
            if moment.at > self._now:
                event = self.advance_to(moment.at, moment.label)
            else:
                event = self.store.events(CLOCK_ID)[-1]
            reached.append((moment, event))
        return reached
