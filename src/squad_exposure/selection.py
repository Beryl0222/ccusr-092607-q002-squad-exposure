"""纯函数的资格校验与选拔评分。

规则引擎不碰存储：服务层在提交提案时冻结数据快照，裁决只基于快照，
因此重放同一决定必然返回同一结果。

硬约束（任一不满足则条目不可入选）：
- 健康许可存在、状态 cleared、scope 覆盖赛事级别、有效期覆盖赛事结束；
- 康复复查未完成（rehab）直接暂缓；
- 报名截止前提交；
- 个人近期负荷 + 本次计划负荷不超过政策上限；
- 双打条目必须互相点名搭档，且双方组合在技术画像中互相登记、
  共同出场样本达到下限；
- 报名位必须存在于赛事且方案内不重复占用；
- 全队负荷之和不超过赛事/政策上限。

软信号（不阻断，但进入解释）：
- 针对该对手打法的交锋样本不足；
- 近期教练观察条数不足。

评分同时考虑短期成绩（对该对手类型的准备价值）与长期补齐阵容
厚度（对应培养缺口的严重度），权重来自冻结政策。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .time_utils import parse_iso

# 硬约束代码 → 中文说明
REASON_TEXT = {
    "no_clearance": "缺少健康许可记录",
    "clearance_restricted": "健康许可状态为限制出场",
    "clearance_rehab": "处于康复期，需先完成复查",
    "clearance_scope": "健康许可范围不覆盖该赛事级别",
    "clearance_expired": "健康许可在赛事结束前到期",
    "review_pending": "康复复查日期尚未到达",
    "past_deadline": "提案提交晚于报名截止时间",
    "individual_load": "个人近期负荷叠加本次赛程超出上限",
    "pair_not_mutual": "双打组合未在双方画像中互相登记",
    "pair_sample": "双打共同出场样本不足",
    "partner_missing": "双打条目缺少互相匹配的搭档条目",
    "unknown_slot": "报名位未在赛事中登记",
    "duplicate_slot": "同一报名位在方案中被重复占用",
    "team_load": "全队总负荷超出上限",
}


@dataclass
class EntryResult:
    slot_ref: str
    discipline: str
    athlete_id: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score: float = 0.0
    score_detail: dict[str, float] = field(default_factory=dict)
    prepared_opponent_style: str | None = None


@dataclass
class Evaluation:
    feasible: bool
    frozen_policy_version: str
    evaluated_at: str
    fingerprint: str
    entries: list[EntryResult]
    team_load_minutes: int
    team_load_cap: int
    selected: dict[str, str]  # athlete_id -> slot_ref
    held: dict[str, list[str]]  # athlete_id -> reasons / warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "frozen_policy_version": self.frozen_policy_version,
            "evaluated_at": self.evaluated_at,
            "fingerprint": self.fingerprint,
            "team_load_minutes": self.team_load_minutes,
            "team_load_cap": self.team_load_cap,
            "entries": [
                {
                    "slot_ref": e.slot_ref,
                    "discipline": e.discipline,
                    "athlete_id": e.athlete_id,
                    "eligible": e.eligible,
                    "reasons": e.reasons,
                    "warnings": e.warnings,
                    "score": round(e.score, 4),
                    "score_detail": {k: round(v, 4) for k, v in e.score_detail.items()},
                    "prepared_opponent_style": e.prepared_opponent_style,
                }
                for e in self.entries
            ],
            "selected": dict(self.selected),
            "held": {k: list(v) for k, v in self.held.items()},
        }


# ---- 冻结快照 ---------------------------------------------------------------


def build_snapshot(
    state: Any,
    event_ref: str,
    entries: list[dict[str, Any]],
    policy_version: str,
    at_iso: str,
) -> dict[str, Any]:
    """把裁决需要的全部数据按当前版本冻结进快照。"""
    competition = state.events[event_ref]
    policy = _policy_at_version(state, policy_version)
    athlete_ids = set()
    for entry in entries:
        athlete_ids.add(entry["athlete_id"])
        if entry.get("partner_id"):
            athlete_ids.add(entry["partner_id"])
    athletes = {}
    for aid in sorted(athlete_ids):
        athlete = state.athletes[aid]
        athletes[aid] = {
            "profile": athlete.profile,
            "clearance": athlete.clearance,
            "observations": list(athlete.observations),
        }
    return {
        "snapshot_at": at_iso,
        "event_ref": event_ref,
        "event": {k: v for k, v in competition.items() if k != "version"},
        "frozen_policy_version": policy_version,
        "policy_rules": policy["rules"],
        "athletes": athletes,
        "entries": [dict(e) for e in entries],
    }


def _policy_at_version(state: Any, policy_version: str) -> dict[str, Any]:
    for policy in state.policies.values():
        if policy["policy_version"] == policy_version:
            return policy
    raise KeyError(f"政策版本不存在: {policy_version}")


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---- 裁决 -------------------------------------------------------------------


def evaluate_snapshot(snapshot: dict[str, Any]) -> Evaluation:
    rules = snapshot["policy_rules"]
    competition = snapshot["event"]
    entries = snapshot["entries"]
    athletes = snapshot["athletes"]
    at_iso = snapshot["snapshot_at"]

    results: list[EntryResult] = []
    known_slots = {s["slot_ref"]: s for s in competition.get("slots", [])}
    team_load = 0
    team_cap = competition.get("team_load_cap", rules.get("team_load_cap", 10**9))

    # 同一报名位下的条目：单打恰有 1 条，双打恰有互相点名的 2 条。
    entries_by_slot: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        entries_by_slot.setdefault(entry["slot_ref"], []).append(entry)

    for slot_ref, slot_entries in entries_by_slot.items():
        slot = known_slots.get(slot_ref)
        if slot is None:
            slot_level_reasons = ["unknown_slot"]
        elif len(slot_entries) > 2:
            slot_level_reasons = ["duplicate_slot"]
        else:
            slot_level_reasons = []
        if slot is not None:
            team_load += int(slot.get("planned_minutes", 0))

        is_doubles_slot = bool(slot and slot.get("discipline") == "doubles") or any(
            e.get("discipline") == "doubles" or e.get("partner_id") for e in slot_entries
        )
        if slot is not None and not slot_level_reasons:
            if is_doubles_slot and len(slot_entries) != 2:
                slot_level_reasons = ["partner_missing"]
            if not is_doubles_slot and len(slot_entries) != 1:
                slot_level_reasons = ["duplicate_slot"]

        for entry in slot_entries:
            aid = entry["athlete_id"]
            data = athletes[aid]
            profile = data["profile"]
            reasons: list[str] = list(slot_level_reasons)
            warnings: list[str] = []

            # 健康许可
            clearance = data.get("clearance")
            level = competition.get("event_level")
            if clearance is None:
                reasons.append("no_clearance")
            else:
                status = clearance.get("status", "cleared")
                if status == "restricted":
                    reasons.append("clearance_restricted")
                if status == "rehab":
                    reasons.append("clearance_rehab")
                    review_at = clearance.get("review_at")
                    if review_at and parse_iso(at_iso) < parse_iso(review_at):
                        reasons.append("review_pending")
                scope = clearance.get("scope", "all")
                if scope != "all" and level not in scope:
                    reasons.append("clearance_scope")
                cover_until = competition.get("competition_end") or at_iso
                if parse_iso(clearance["valid_until"]) < parse_iso(cover_until):
                    reasons.append("clearance_expired")

            # 报名截止
            deadline = competition.get("entry_deadline")
            if deadline and parse_iso(at_iso) > parse_iso(deadline):
                reasons.append("past_deadline")

            # 个人负荷
            planned = (slot or {}).get("planned_minutes", 0)
            recent_load = int(profile.get("recent_load_minutes", 0))
            load_cap = rules.get("max_individual_load_minutes", 10**9)
            if recent_load + planned > load_cap:
                reasons.append("individual_load")

            # 双打组合：两名队员必须在同一位内互相点名，画像中互相登记且样本达标。
            if is_doubles_slot:
                partner_id = entry.get("partner_id")
                mate = next(
                    (
                        e
                        for e in slot_entries
                        if e["athlete_id"] == partner_id and e.get("partner_id") == aid
                    ),
                    None,
                )
                if partner_id is None or mate is None:
                    reasons.append("partner_missing")
                else:
                    pairing = _find_pairing(profile, partner_id)
                    back = _find_pairing(athletes[partner_id]["profile"], aid)
                    if pairing is None or back is None:
                        reasons.append("pair_not_mutual")
                    else:
                        shared = min(int(pairing.get("matches", 0)), int(back.get("matches", 0)))
                        if shared < rules.get("min_pair_matches", 1):
                            reasons.append("pair_sample")

            # 证据软信号
            opponent_style = entry.get("opponent_style") or (slot or {}).get("opponent_style")
            matchup = profile.get("opponent_matchups", {}).get(opponent_style or "", {})
            sample = int(matchup.get("sample", 0))
            if opponent_style and sample < rules.get("min_matchup_sample", 1):
                warnings.append("insufficient_matchup_sample")
            lookback = rules.get("observation_lookback_days", 30)
            fresh_observations = _count_fresh_observations(
                data.get("observations", []), at_iso, lookback
            )
            if fresh_observations < rules.get("min_observations", 0):
                warnings.append("insufficient_observations")

            score, detail = _score(
                profile, opponent_style, matchup, rules, warnings, reasons
            )
            results.append(
                EntryResult(
                    slot_ref=slot_ref,
                    discipline=entry.get("discipline", (slot or {}).get("discipline", "unknown")),
                    athlete_id=aid,
                    eligible=not reasons,
                    reasons=reasons,
                    warnings=warnings,
                    score=score,
                    score_detail=detail,
                    prepared_opponent_style=opponent_style,
                )
            )

    if team_load > team_cap:
        for result in results:
            if "team_load" not in result.reasons:
                result.reasons.append("team_load")

    feasible = all(r.eligible for r in results) and len(results) > 0
    selected = {r.athlete_id: r.slot_ref for r in results if r.eligible}
    held: dict[str, list[str]] = {}
    for r in results:
        if not r.eligible:
            held[r.athlete_id] = list(dict.fromkeys(r.reasons + r.warnings))

    return Evaluation(
        feasible=feasible,
        frozen_policy_version=snapshot["frozen_policy_version"],
        evaluated_at=at_iso,
        fingerprint=snapshot_fingerprint(snapshot),
        entries=results,
        team_load_minutes=team_load,
        team_load_cap=team_cap,
        selected=selected,
        held=held,
    )


def _find_pairing(profile: dict[str, Any], partner_id: str) -> dict[str, Any] | None:
    for pair in profile.get("pairs", []):
        if pair.get("partner_id") == partner_id:
            return pair
    return None


def _count_fresh_observations(observations: list[dict[str, Any]], at_iso: str, days: int) -> int:
    cutoff = parse_iso(at_iso).timestamp() - days * 86400
    return sum(1 for o in observations if parse_iso(o["observed_at"]).timestamp() >= cutoff)


def _score(
    profile: dict[str, Any],
    opponent_style: str | None,
    matchup: dict[str, Any],
    rules: dict[str, Any],
    warnings: list[str],
    reasons: list[str],
) -> tuple[float, dict[str, float]]:
    w_short = float(rules.get("weight_short_term", 0.6))
    w_long = float(rules.get("weight_long_term", 0.4))
    min_sample = max(1, int(rules.get("min_matchup_sample", 1)))

    value = float(matchup.get("value", 0.0))
    sample = int(matchup.get("sample", 0))
    confidence = min(1.0, sample / min_sample) if min_sample else 1.0
    short_term = value * confidence

    # 长期价值：该对手类型恰好对应一项仍存在的培养缺口时，取缺口严重度。
    long_term = 0.0
    if opponent_style:
        for gap in profile.get("gaps", []):
            if gap.get("trainable_against") == opponent_style:
                long_term = max(long_term, float(gap.get("severity", 0.0)))

    score = w_short * short_term + w_long * long_term
    if reasons:
        score = 0.0  # 不具资格的条目不参与排序
    return score, {
        "short_term": round(short_term, 4),
        "long_term": round(long_term, 4),
        "confidence": round(confidence, 4),
        "weight_short_term": w_short,
        "weight_long_term": w_long,
    }


def explain(evaluation: Evaluation, athlete_id: str) -> dict[str, Any]:
    """给出某位队员入选或暂缓的可追溯解释。"""
    items = [e for e in evaluation.entries if e.athlete_id == athlete_id]
    if not items:
        return {"athlete_id": athlete_id, "status": "not_in_proposal"}
    out_entries = []
    for item in items:
        out_entries.append(
            {
                "slot_ref": item.slot_ref,
                "discipline": item.discipline,
                "opponent_style": item.prepared_opponent_style,
                "status": "selected" if item.eligible else "held",
                "score": round(item.score, 4),
                "score_detail": item.score_detail,
                "reasons": [{"code": c, "message": REASON_TEXT[c]} for c in item.reasons],
                "warnings": list(item.warnings),
            }
        )
    return {
        "athlete_id": athlete_id,
        "frozen_policy_version": evaluation.frozen_policy_version,
        "fingerprint": evaluation.fingerprint,
        "entries": out_entries,
    }
