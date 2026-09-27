"""可推进的模拟时钟。

服务不直接读取系统时间：选拔语境里需要把日期推进到报名截止、
康复复查或赛事结束。时钟本身也是一个聚合，推进动作以
``CLOCK_ADVANCED`` 事件落库，重启重放后时钟继续往前走。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .time_utils import now_iso, parse_iso

CLOCK_AGGREGATE = "clock"
CLOCK_ID = "simulation"


class ClockError(ValueError):
    pass


@dataclass(frozen=True)
class SimulationClock:
    """不可变时钟值；推进返回新实例。"""

    current: datetime

    @classmethod
    def start(cls, at: datetime | None = None) -> "SimulationClock":
        if at is None:
            at = datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        if at.tzinfo is None:
            raise ClockError("启动时间必须携带时区")
        return cls(current=at)

    def advance_to(self, moment: datetime) -> "SimulationClock":
        if moment.tzinfo is None:
            raise ClockError("目标时间必须携带时区")
        if moment < self.current:
            raise ClockError(
                f"模拟时间不能回退: {moment.isoformat()} 早于 {self.current.isoformat()}"
            )
        return SimulationClock(current=moment)

    def advance_by(self, delta: timedelta) -> "SimulationClock":
        return self.advance_to(self.current + delta)

    def is_before(self, iso_value: str) -> bool:
        return self.current < parse_iso(iso_value)

    def is_at_or_after(self, iso_value: str) -> bool:
        return self.current >= parse_iso(iso_value)

    @property
    def iso(self) -> str:
        return now_iso(self.current)
