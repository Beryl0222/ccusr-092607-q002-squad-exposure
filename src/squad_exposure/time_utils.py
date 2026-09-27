"""时间与 JSON 序列化工具：所有落库时间均为带时区的 ISO 字符串。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 领域内部统一使用东八区表达，比较时一律转 aware datetime。
CANONICAL_TZ = timezone.utc


def now_iso(moment: datetime | None = None) -> str:
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return moment.isoformat()


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"时间缺少时区: {value}")
    return parsed


def to_jsonable(value: Any) -> Any:
    """把领域对象转换成可稳定 JSON 序列化的形式（deepcopy 友好）。"""
    if isinstance(value, datetime):
        return now_iso(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value
