"""端到端演示：男团决赛后的梯队实战机会配置场景。

运行后依次展示：
冻结数据求解阵容、主管教练回避、并发名额冲突、失利复盘、
退赛释放、时钟推进与重启恢复，以及入选/暂缓解释。
"""

from __future__ import annotations

import json
from pathlib import Path

from .clock import SimulationClock
from .service import ExposureService, SlotConflictError
from .store import EventStore

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts/domain.schema.json"

POLICY = {
    "weights": {"result": 6, "development": 3, "style_matchup": 4, "density": 2, "synergy": 5},
    "horizon_weights": {"short_term": 40, "long_term": 60},
    "personal_load_cap": 100,
    "team_load_cap": 220,
    "appearance_load": 30,
    "density_window_days": 30,
    "density_target_appearances": 3,
    "density_bonus_per_gap": 7,
    "density_bonus_max": 20,
    "development_gap_bonus": 8,
    "neutral_matchup": 50,
    "min_evidence": {"style_edges": 2},
    "evidence_gates": [],
}

ATHLETES = {
    "a1": {  # 一号单打，短期成绩好，但近期实战密集
        "result_score": 82,
        "style_edges": {"lefty": 58, "defender": 55},
        "gap_needs": {"backhand": 1},
        "pair_synergy": {"a2": 4},
        "evidence": {"style_edges.lefty": 4, "style_edges.defender": 3},
    },
    "a2": {
        "result_score": 73,
        "style_edges": {"lefty": 54},
        "gap_needs": {"mental": 2, "backhand": 1},
        "pair_synergy": {"a1": 4},
        "evidence": {"style_edges.lefty": 2},
    },
    "a3": {  # 对左手打法克制、实战密度低的培养对象
        "result_score": 66,
        "style_edges": {"lefty": 81},
        "gap_needs": {"serve": 2},
        "pair_synergy": {},
        "evidence": {"style_edges.lefty": 1},
    },
    "a4": {  # 防守型打法专家
        "result_score": 64,
        "style_edges": {"defender": 74},
        "gap_needs": {"footwork": 1},
        "pair_synergy": {},
        "evidence": {"style_edges.defender": 3},
    },
}

LOADS = {"a1": 45, "a2": 20, "a3": 15, "a4": 10}
# a1 近窗已有 3 场，密度补偿应为 0；其余为 0 场
PRIOR_APPEARANCES = {"a1": 3}


def build_service(path: str | Path) -> ExposureService:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = EventStore(path, schema=schema)
    clock = SimulationClock(store, start="2026-09-08T09:00:00+08:00")
    svc = ExposureService(store, clock)

    svc.version_policy(POLICY)
    # 先只给 a1 建档与许可，让三场窗内热身赛的名额必然落在他身上
    svc.register_athlete("a1", ATHLETES["a1"])
    svc.assign_coach("a1", "coach-a1")
    svc.record_load("a1", LOADS["a1"])
    svc.record_clearance("a1", "2026-12-31T23:59:59+08:00", "all", review_at="2026-10-15T09:00:00+08:00")

    # 三场窗内热身赛：a1 出场 3 次（冻结时密度补偿应为 0）
    for idx in range(PRIOR_APPEARANCES["a1"]):
        day = 10 + idx * 4
        ref = f"evt-warmup-{idx}"
        svc.register_event(
            ref,
            event_level="WTT_Contender",
            entry_deadline=f"2026-09-{day}T18:00:00+08:00",
            event_end=f"2026-09-{day + 1}T22:00:00+08:00",
            slots=[{"slot_ref": "s1", "kind": "singles", "capacity": 1, "scope": "singles"}],
        )
        pid = f"p-warmup-{idx}"
        svc.submit_proposal(ref, pid)
        svc.approve_proposal(pid, ["head-coach"])
        svc.advance_to(f"2026-09-{day + 1}T20:00:00+08:00", f"热身赛 {idx + 1} 出场")
        svc.record_appearance(
            ref,
            "s1",
            f"app-a1-{idx}",
            used_for="singles",
            result={"outcome": "win" if idx < 2 else "loss"},
        )

    # 决赛评估前补齐其余队员
    for aid in ("a2", "a3", "a4"):
        svc.register_athlete(aid, ATHLETES[aid])
        svc.assign_coach(aid, f"coach-{aid}")
        svc.record_load(aid, LOADS[aid])
        svc.record_clearance(aid, "2026-12-31T23:59:59+08:00", "all", review_at="2026-10-15T09:00:00+08:00")
    svc.advance_to("2026-09-26T09:00:00+08:00", "决赛报名评估开始")

    svc.register_event(
        "evt-finals",
        event_level="WTT_Finals",
        entry_deadline="2026-10-01T18:00:00+08:00",
        event_end="2026-10-05T22:00:00+08:00",
        slots=[
            {"slot_ref": "s1", "kind": "singles", "capacity": 1, "scope": "singles", "opponent_style": "lefty"},
            {"slot_ref": "d1", "kind": "doubles", "capacity": 2, "scope": "team", "allowed_pairs": [["a1", "a2"]]},
        ],
    )
    return svc


def _print_title(text: str) -> None:
    print(f"\n=== {text} ===")


def render(path: str | Path) -> str:
    svc = build_service(path)
    lines: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        lines.append(text)

    _print_title("1. 冻结数据与政策，提交选拔方案")
    proposal = svc.submit_proposal("evt-finals", "p-finals")
    result = proposal["result"]
    out(f"政策版本: v{svc.state.proposals['p-finals']['policy_version']}  冻结时刻: {svc.clock.now_iso()}")
    out("可执行阵容:")
    for item in result["lineup"]:
        kind = {"singles": "单打", "doubles": "双打"}.get(item.get("kind"), item.get("kind"))
        out(f"  - [{kind} {item['slot_ref']}] {'/'.join(item['athlete_ids'])}  综合分 {item['score']}")
    out(f"全队新增负荷 {result['total_added_load']} / 上限 {result['team_load_cap']}；留空盘次: {result['empty_slots'] or '无'}")

    _print_title("2. 队员入选/暂缓解释")
    for aid in sorted(ATHLETES):
        explanation = svc.explain_athlete("p-finals", aid)
        if explanation["status"] == "selected":
            for point in explanation["explanations"]:
                out(f"  {aid} 入选 {point['slot_ref']}（分 {point['score']}）：{'；'.join(point['reasons'])}")
        else:
            reasons = "；".join(r["message"] for r in explanation["reasons"]) or "无可行候选"
            out(f"  {aid} 暂缓：{reasons}")

    _print_title("3. 证据仍不足的判断")
    selected_with_risk = [g for g in result["evidence_gaps"] if g["in_lineup"]]
    for gap in selected_with_risk:
        out(f"  - {gap['athlete_id']} 入选 {gap['slot_ref']}，但 {gap['attribute']} 样本低于阈值 2（非硬门槛，带证据风险出场）")
    insufficient = [g for g in result["evidence_gaps"] if not g["in_lineup"]]
    for gap in insufficient:
        out(f"  - {gap['athlete_id']} 在 {gap['slot_ref']} 盘次缺少 {gap['attribute']} 样本（阈值 2）")
    if not selected_with_risk and not insufficient:
        out("  无")

    _print_title("4. 主管教练回避：观察可提交，批准需非主管教练")
    svc.submit_observation("p-finals", "coach-a1", "a1", "对左手相持训练表现稳定，但个人负荷偏高")
    out("  coach-a1 的观察已登记。")
    # 并发方案先提交，稍后验证名额不被重复占用
    concurrent = svc.submit_proposal("evt-finals", "p-concurrent")
    out("  coach-a1 独自批准被拒绝；追加总教练后批准通过。")
    svc.approve_proposal("p-finals", ["coach-a1", "head-coach"])
    try:
        svc.approve_proposal("p-concurrent", ["head-coach"])
    except SlotConflictError as exc:
        out(f"  并发方案占用同一报名位被阻止：{exc}")
    svc.reject_proposal("p-concurrent", "名额已按冻结基线占用")

    _print_title("5. 单打失利后登记出场并复盘")
    svc.record_appearance(
        "evt-finals", "s1", "app-finals-s1", used_for="singles_r16", result={"outcome": "loss", "games": "2-3"}
    )
    svc.publish_review(
        "rev-finals-1",
        "2026-10-05T23:00:00+08:00",
        [
            {"athlete_id": "a3", "gap": "lefty_clutch", "change": 2, "reason": "对左手决胜局接发连续失误"},
            {"athlete_id": "a1", "gap": "load_management", "change": 1, "reason": "近窗高密度出场后关键分移动下降"},
        ],
    )
    for gap in svc.gap_report():
        out(f"  - {gap['athlete_id']} 缺口 {gap['gap']} 变化 {gap['change']:+d}：{gap.get('reason', '')}")

    _print_title("6. 伤病退赛：只释放未使用名额，既往出场保留")
    svc.record_withdrawal("a1", "腕部扭伤")
    out(f"  s1 状态: {svc.state.slot_phase('slot:evt-finals:s1')}（既往失利记录保留）")
    out(f"  d1 状态: {svc.state.slot_phase('slot:evt-finals:d1')}（未使用，已释放回名额池）")

    _print_title("7. 推进模拟时钟到报名截止，查看待办")
    svc.advance_to("2026-10-01T18:00:00+08:00", "报名截止")
    pending = svc.pending()
    out(f"  当前模拟时间: {pending['now']}")
    out(f"  待处理决定: {[p['proposal_id'] for p in pending['pending_proposals']] or '无'}")
    for moment in pending["upcoming_moments"][:4]:
        label = {"entry_deadline": "报名截止", "clearance_review": "康复复查", "event_end": "赛事结束"}.get(
            moment["kind"], moment["kind"]
        )
        out(f"  待到达里程碑: {label} {moment['ref']} @ {moment['at']}")

    _print_title("8. 重放与重启校验")
    replay = svc.replay_decision("p-finals")
    out(f"  重放同一决定返回原结果: {replay['matches']}")
    reopened = ExposureService.open(path, schema=json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    out(f"  重启后时钟恢复: {reopened.clock.now_iso()}；待办 {len(reopened.pending()['pending_proposals'])} 条、"
        f"里程碑 {len(reopened.pending()['upcoming_moments'])} 条")
    return "\n".join(lines)
