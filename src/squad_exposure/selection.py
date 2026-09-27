"""阵容求解：在冻结快照上做确定性的约束满足与评分。

求解器是纯函数：同一份冻结快照永远返回同一阵容（重放同一决定返回原结果）。
约束同时覆盖：
- 个人资格：健康许可（按 scope）、个人负荷上限、退赛生效；
- 组合约束：双打/团体关键盘只能使用登记过的搭档组合或相容性；
- 全队负荷：所有拟出场队员的增量负荷之和不得超过全队上限；
- 名额：每个报名位最多一份方案占用。
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from itertools import combinations
from typing import Any

from .clock import parse_ts

# 理由代码 -> 中文模板在 _reason_text 中给出


def _evidence_counts(profile: dict[str, Any]) -> dict[str, int]:
    return profile.get("evidence", {}) or {}


def _has_enough_evidence(profile: dict[str, Any], slot: dict[str, Any], ruleset: dict[str, Any]) -> tuple[bool, list[str]]:
    """证据计数低于政策阈值的属性只做标记；是否构成硬门槛由 evidence_gates 决定。"""
    counts = _evidence_counts(profile)
    minimums = ruleset.get("min_evidence", {})
    missing: list[str] = []
    for attribute, threshold in minimums.items():
        if attribute == "style_edges":
            opponent = slot.get("opponent_style")
            if not opponent:
                continue  # 该盘次没有指定对手打法，对阵证据不在此盘检验
            value = counts.get(f"style_edges.{opponent}", counts.get("style_edges", 0))
        else:
            value = counts.get(attribute)
        if value is None or value < threshold:
            missing.append(attribute if attribute != "style_edges" else f"style_edges.{slot.get('opponent_style', '?')}")
    gates = set(ruleset.get("evidence_gates", []))
    hard_missing = [m for m in missing if m.split(".", 1)[0] in gates]
    return (not hard_missing), missing


def eligibility(athlete_id: str, snap: dict[str, Any], slot: dict[str, Any]) -> list[tuple[str, str]]:
    """返回不可入选理由（代码, 参数）；空列表表示具备资格。"""
    athlete = snap["athletes"][athlete_id]
    reasons: list[tuple[str, str]] = []
    scope = slot.get("scope", "all")
    if not athlete.get(f"clearance_scope_{scope}") and not athlete.get("clearance_scope_all"):
        reasons.append(("clearance_invalid", scope))
    load = athlete.get("load")
    cap = snap["policy"].get("personal_load_cap", 100)
    if load is None:
        reasons.append(("load_unknown", ""))
    elif load["load_index"] + snap["policy"].get("appearance_load", 30) > cap:
        reasons.append(("over_personal_load", str(load["load_index"])))
    if athlete_id in snap.get("withdrawn_active", []):
        reasons.append(("withdrawn", ""))
    enough, _missing = _has_enough_evidence(athlete["profile"], slot, snap["policy"])
    if not enough:
        reasons.append(("evidence_gate_failed", ""))
    return reasons


def _density_bonus(athlete_id: str, snap: dict[str, Any]) -> tuple[int, int, int]:
    """近期实战密度越低，培养补偿越高。返回 (补偿分, 近窗出场数, 缺口数)。"""
    ruleset = snap["policy"]
    window_days = ruleset.get("density_window_days", 30)
    target = ruleset.get("density_target_appearances", 3)
    bonus_max = ruleset.get("density_bonus_max", 20)
    frozen = parse_ts(snap["frozen_at"])
    count = 0
    for appearance in snap["athletes"][athlete_id].get("appearances", []):
        if frozen - parse_ts(appearance["at"]) <= timedelta(days=window_days):
            count += 1
    deficit = max(0, target - count)
    bonus = min(bonus_max, deficit * ruleset.get("density_bonus_per_gap", 7))
    return bonus, count, deficit


def score_assignment(athlete_ids: tuple[str, ...], slot: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """对一个候选出场组合打分，全部使用整数运算并给出可解释的构成。"""
    ruleset = snap["policy"]
    weights = ruleset.get("weights", {})
    # 短期/长期视角用整数权重表达（默认各 50），避免浮点
    horizon = ruleset.get("horizon_weights", {"short_term": 50, "long_term": 50})
    short_sum = horizon.get("short_term", 50) + horizon.get("long_term", 50)
    w = {
        "result": weights.get("result", 6),
        "development": weights.get("development", 3),
        "style_matchup": weights.get("style_matchup", 4),
        "density": weights.get("density", 2),
        "synergy": weights.get("synergy", 5),
    }
    denominator = (
        w["result"] * horizon.get("short_term", 50)
        + w["development"] * horizon.get("long_term", 50)
        + w["style_matchup"] * short_sum
        + w["density"] * horizon.get("long_term", 50)
    )

    per_athlete: dict[str, dict[str, Any]] = {}
    total = 0
    for athlete_id in athlete_ids:
        profile = snap["athletes"][athlete_id]["profile"]
        result = int(profile.get("result_score", 0))
        development = min(100, ruleset.get("development_gap_bonus", 8) * sum((profile.get("gap_needs") or {}).values()))
        opponent = slot.get("opponent_style")
        if opponent:
            matchup = int((profile.get("style_edges") or {}).get(opponent, ruleset.get("neutral_matchup", 50)))
        else:
            matchup = ruleset.get("neutral_matchup", 50)
        density, recent, deficit = _density_bonus(athlete_id, snap)
        numerator = (
            w["result"] * horizon.get("short_term", 50) * result
            + w["development"] * horizon.get("long_term", 50) * development
            + w["style_matchup"] * short_sum * matchup
            + w["density"] * horizon.get("long_term", 50) * density
        )
        score = numerator // denominator
        per_athlete[athlete_id] = {
            "score": score,
            "components": {
                "result": result,
                "development": development,
                "style_matchup": matchup,
                "density_bonus": density,
                "recent_appearances": recent,
                "density_deficit": deficit,
                "opponent_style": opponent,
            },
        }
        total += score

    synergy = 0
    if len(athlete_ids) > 1:
        for left, right in combinations(athlete_ids, 2):
            edges = snap["athletes"][left]["profile"].get("pair_synergy") or {}
            synergy += int(edges.get(right, ruleset.get("neutral_synergy", 0)))
    synergy_bonus = w["synergy"] * synergy
    total += synergy_bonus
    return {"score": int(total), "synergy": synergy, "synergy_bonus": int(synergy_bonus), "per_athlete": per_athlete}


def _candidate_sets(slot: dict[str, Any], snap: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, str]]]]:
    """枚举该报名位所有可行候选（单打为单人，双打/关键盘为登记组合）。"""
    blocked: dict[str, list[tuple[str, str]]] = {}
    pool = sorted(snap["athletes"])
    capacity = int(slot.get("capacity", 1))
    if slot.get("allowed_pairs"):
        raw_combos = [tuple(sorted(pair)) for pair in slot["allowed_pairs"]]
    elif slot.get("kind") in ("doubles", "team_key") and capacity == 2:
        raw_combos = [tuple(sorted(pair)) for pair in combinations(pool, 2) if _pair_declared(pair, snap)]
    else:
        raw_combos = [tuple(c) for c in combinations(pool, capacity)]
    candidates: list[dict[str, Any]] = []
    for combo in dict.fromkeys(raw_combos):  # 去重并保序
        reasons_per: dict[str, list[tuple[str, str]]] = {}
        for athlete_id in combo:
            reasons = eligibility(athlete_id, snap, slot)
            if reasons:
                reasons_per[athlete_id] = reasons
                blocked.setdefault(athlete_id, []).extend(reasons)
        if reasons_per:
            continue
        scored = score_assignment(combo, slot, snap)
        candidates.append({"slot_ref": slot["slot_ref"], "athlete_ids": list(combo), **scored})
    # 分值高者优先；同分时按队员标识排序，保证确定性
    candidates.sort(key=lambda c: (-c["score"], tuple(c["athlete_ids"])))
    return candidates, blocked


def _pair_declared(pair: tuple[str, ...], snap: dict[str, Any]) -> bool:
    left, right = pair
    table = snap["athletes"][left]["profile"].get("pair_synergy") or {}
    return right in table


def solve_lineup(snapshot: dict[str, Any]) -> dict[str, Any]:
    """在冻结快照上求最优可行阵容。返回阵容、排序备选与全部暂缓理由。"""
    ruleset = snapshot["policy"]
    open_slots = [s for s in snapshot["event"]["slots"] if s["slot_ref"] not in snapshot["occupied_slots"]]
    enumerated = {s["slot_ref"]: _candidate_sets(s, snapshot) for s in open_slots}

    appearance_load = ruleset.get("appearance_load", 30)
    team_cap = ruleset.get("team_load_cap", 10**9)

    base_loads = {aid: (info["load"]["load_index"] if info.get("load") else 0) for aid, info in snapshot["athletes"].items()}
    personal_cap = ruleset.get("personal_load_cap", 100)

    best: dict[str, Any] | None = None
    # 深度优先 + 分支定界：槽位顺序固定，候选已按分值排序
    def visit(index: int, chosen: dict[str, dict[str, Any]], added_load: Counter[str]) -> None:
        nonlocal best
        if sum(added_load.values()) > team_cap:
            return
        if best is not None and sum(c["score"] for c in chosen.values()) + _remaining_upper_bound(index) < best["score"]:
            return
        if index == len(open_slots):
            current_score = sum(c["score"] for c in chosen.values())
            # 空槽位以 () 进入 canonical，确保“空哪些盘”不同的方案不会被视为同一阵容
            canonical = tuple(
                (s["slot_ref"], tuple(chosen[s["slot_ref"]]["athlete_ids"]) if s["slot_ref"] in chosen else ())
                for s in open_slots
            )
            if best is None or current_score > best["score"] or (current_score == best["score"] and canonical < best["canonical"]):
                best = {"score": current_score, "canonical": canonical, "chosen": dict(chosen)}
            return
        slot = open_slots[index]
        # 该位空着（弃权该盘）也是一个分支，保证容量受限时仍给出可行方案
        visit(index + 1, chosen, added_load)
        candidates, _blocked = enumerated[slot["slot_ref"]]
        for candidate in candidates:
            loads = Counter(added_load)
            feasible = True
            for athlete_id in candidate["athlete_ids"]:
                loads[athlete_id] += appearance_load
                if base_loads[athlete_id] + loads[athlete_id] > personal_cap:
                    feasible = False
                    break
            if not feasible or sum(loads.values()) > team_cap:
                continue
            chosen[slot["slot_ref"]] = candidate
            visit(index + 1, chosen, loads)
            del chosen[slot["slot_ref"]]

    def _remaining_upper_bound(index: int) -> int:
        bound = 0
        for slot in open_slots[index:]:
            candidates, _ = enumerated[slot["slot_ref"]]
            if candidates:
                bound += candidates[0]["score"]
        return bound

    visit(0, {}, Counter())

    lineup: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    total_added_load = 0
    for slot in open_slots:
        chosen = (best or {}).get("chosen", {}).get(slot["slot_ref"]) if best else None
        if chosen is None:
            continue
        lineup.append(
            {
                "slot_ref": chosen["slot_ref"],
                "kind": slot.get("kind"),
                "athlete_ids": list(chosen["athlete_ids"]),
                "score": chosen["score"],
                "synergy": chosen["synergy"],
                "synergy_bonus": chosen["synergy_bonus"],
                "per_athlete": chosen["per_athlete"],
            }
        )
        selected_ids.update(chosen["athlete_ids"])
        total_added_load += appearance_load * len(chosen["athlete_ids"])

    not_selected = _explain_omissions(open_slots, enumerated, snapshot, selected_ids, lineup)
    evidence_gaps = _evidence_gaps(open_slots, snapshot, selected_ids)
    return {
        "feasible": best is not None,
        "lineup": lineup,
        "score": (best or {}).get("score", 0),
        "occupied_slots": sorted(snapshot["occupied_slots"]),
        "empty_slots": [s["slot_ref"] for s in open_slots if not any(x["slot_ref"] == s["slot_ref"] for x in lineup)],
        "total_added_load": total_added_load,
        "team_load_cap": team_cap,
        "not_selected": not_selected,
        "evidence_gaps": evidence_gaps,
    }


def _explain_omissions(open_slots, enumerated, snapshot, selected_ids, lineup) -> list[dict[str, Any]]:
    chosen_by_slot = {item["slot_ref"]: item for item in lineup}
    rows: dict[str, dict[str, Any]] = {}

    def row_for(athlete_id: str) -> dict[str, Any]:
        return rows.setdefault(
            athlete_id, {"athlete_id": athlete_id, "reasons": set(), "best_score": None, "best_rank": None}
        )

    for slot in open_slots:
        candidates, blocked = enumerated[slot["slot_ref"]]
        winner = chosen_by_slot.get(slot["slot_ref"])
        winner_score = winner["score"] if winner else None
        for athlete_id, reasons in blocked.items():
            row_for(athlete_id)["reasons"].update(reasons)
        if winner is None:
            # 该盘在全局容量压力下被迫留空：所有可行候选都受全队负荷挤压
            for candidate in candidates:
                for athlete_id in candidate["athlete_ids"]:
                    if athlete_id not in selected_ids:
                        row_for(athlete_id)["reasons"].add(("team_load_pressure", ""))
            continue
        winner_tuple = tuple(winner["athlete_ids"]) if winner else None
        for rank, candidate in enumerate(candidates, start=1):
            if tuple(candidate["athlete_ids"]) == winner_tuple:
                continue  # 胜者组合自身
            for athlete_id in candidate["athlete_ids"]:
                if athlete_id in selected_ids:
                    continue
                row = row_for(athlete_id)
                if candidate["score"] > winner_score:
                    # 本盘更优的组合在全局阵容权衡中为其他盘次让步
                    row["reasons"].add(("lineup_tradeoff", slot["slot_ref"]))
                else:
                    row["reasons"].add(("ranked_below_capacity", str(rank)))
                if row["best_score"] is None or candidate["score"] > row["best_score"]:
                    row["best_score"] = candidate["score"]
                    row["best_rank"] = rank

    result = []
    for athlete_id in sorted(rows):
        if athlete_id in selected_ids:
            continue
        row = rows[athlete_id]
        result.append(
            {
                "athlete_id": athlete_id,
                "reasons": [{"code": code, "detail": detail} for code, detail in sorted(row["reasons"])],
                "best_candidate_score": row["best_score"],
            }
        )
    return result


def _evidence_gaps(open_slots, snapshot, selected_ids) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for athlete_id in sorted(snapshot["athletes"]):
        for slot in open_slots:
            _enough, missing = _has_enough_evidence(snapshot["athletes"][athlete_id]["profile"], slot, snapshot["policy"])
            for attribute in missing:
                gaps.append(
                    {
                        "athlete_id": athlete_id,
                        "slot_ref": slot["slot_ref"],
                        "attribute": attribute,
                        "in_lineup": athlete_id in selected_ids,
                    }
                )
    return gaps


def reason_text(code: str, detail: str = "") -> str:
    templates = {
        "clearance_invalid": f"缺少在 {detail} 范围内有效的健康许可",
        "load_unknown": "近期负荷数据缺失，无法核算",
        "over_personal_load": f"近期负荷指数 {detail} 已使新增出场突破个人上限",
        "withdrawn": "退赛已生效",
        "evidence_gate_failed": "关键属性证据样本不足且政策要求硬门槛",
        "ranked_below_capacity": f"综合评分在名额排序中靠后（候选序位 {detail}）",
        "lineup_tradeoff": f"在 {detail} 盘次个人评分更高，但为全队负荷与多盘次整体最优而让步",
        "team_load_pressure": "全队负荷上限下该盘次被迫留空，候选均受容量挤压",
    }
    return templates.get(code, code)


def selection_reasons(lineup: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把入选队员的分数构成翻译为可向教练组解释的中文要点。"""
    explanations: list[dict[str, Any]] = []
    for item in lineup:
        for athlete_id, detail in item["per_athlete"].items():
            c = detail["components"]
            points = [f"短期能力评分 {c['result']}"]
            if c["opponent_style"]:
                points.append(f"对 {c['opponent_style']} 打法对阵评分 {c['style_matchup']}")
            points.append(f"培养缺口价值 {c['development']}")
            if c["density_bonus"]:
                points.append(f"近 30 天仅 {c['recent_appearances']} 场实战，密度补偿 +{c['density_bonus']}")
            explanations.append(
                {
                    "athlete_id": athlete_id,
                    "slot_ref": item["slot_ref"],
                    "score": detail["score"],
                    "reasons": points,
                }
            )
        if item.get("synergy"):
            explanations.append(
                {
                    "athlete_id": "/".join(item["athlete_ids"]),
                    "slot_ref": item["slot_ref"],
                    "score": item["score"],
                    "reasons": [f"登记搭档组合默契分 {item['synergy']}，协同加成 +{item['synergy_bonus']}"],
                }
            )
    return explanations
