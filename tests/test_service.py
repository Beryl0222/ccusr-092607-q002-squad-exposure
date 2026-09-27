import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from squad_exposure.clock import SimulationClock
from squad_exposure.selection import reason_text, solve_lineup
from squad_exposure.service import ExposureService, ServiceError, SlotConflictError
from squad_exposure.store import AggregateVersionError, EventConflictError, EventStore


def make_service() -> tuple[ExposureService, Path]:
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "events.jsonl"
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    store = EventStore(path, schema=schema)
    clock = SimulationClock(store)
    return ExposureService(store, clock), path


POLICY = {
    "weights": {"result": 6, "development": 3, "style_matchup": 4, "density": 2, "synergy": 5},
    "horizon_weights": {"short_term": 40, "long_term": 60},
    "personal_load_cap": 100,
    "team_load_cap": 200,
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

EVENT_SLOTS = [
    {"slot_ref": "s1", "kind": "singles", "capacity": 1, "scope": "singles", "opponent_style": "lefty"},
    {
        "slot_ref": "d1",
        "kind": "doubles",
        "capacity": 2,
        "scope": "team",
        "allowed_pairs": [["a1", "a2"], ["a2", "a3"]],
    },
]


def build_world(svc: ExposureService, *, policy=None, athletes=None) -> None:
    svc.version_policy(policy or POLICY)
    data = athletes or {
        "a1": {
            "result_score": 80,
            "style_edges": {"lefty": 60},
            "gap_needs": {"backhand": 1},
            "pair_synergy": {"a2": 4},
            "evidence": {"style_edges.lefty": 3},
        },
        "a2": {
            "result_score": 70,
            "style_edges": {"lefty": 55},
            "gap_needs": {"mental": 2},
            "pair_synergy": {"a1": 4, "a3": 1},
            "evidence": {"style_edges.lefty": 2},
        },
        "a3": {
            "result_score": 60,
            "style_edges": {"lefty": 80},
            "gap_needs": {"serve": 2},
            "pair_synergy": {"a2": 1},
            "evidence": {"style_edges.lefty": 1},
        },
    }
    for aid, profile in data.items():
        svc.register_athlete(aid, profile)
        svc.assign_coach(aid, f"coach-{aid}")
        svc.record_load(aid, 10)
        svc.record_clearance(aid, "2026-12-31T23:59:59+08:00", "all")
    svc.register_event(
        "evt-1",
        event_level="WTT_Finals",
        entry_deadline="2026-10-01T18:00:00+08:00",
        event_end="2026-10-05T22:00:00+08:00",
        slots=EVENT_SLOTS,
    )


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.path = make_service()
        build_world(self.svc)

    def test_lineup_respects_pairs_and_is_executable(self) -> None:
        out = self.svc.submit_proposal("evt-1", "p-1")
        refs = {item["slot_ref"]: item["athlete_ids"] for item in out["result"]["lineup"]}
        self.assertEqual(set(refs), {"s1", "d1"})
        self.assertEqual(len(refs["s1"]), 1)
        self.assertIn(refs["d1"], [["a1", "a2"], ["a2", "a3"]])

    def test_daily_coach_cannot_solely_approve(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        # 主管教练可以提交观察
        self.svc.submit_observation("p-1", "coach-a1", "a1", "状态良好")
        with self.assertRaises(ServiceError):
            self.svc.approve_proposal("p-1", ["coach-a1"])
        approved = self.svc.approve_proposal("p-1", ["coach-a1", "head-coach"])
        self.assertEqual(approved["occupied"], 2)
        with self.assertRaises(ServiceError):  # 不能重复批准
            self.svc.approve_proposal("p-1", ["head-coach"])

    def test_concurrent_proposals_cannot_double_occupy_slot(self) -> None:
        p1 = self.svc.submit_proposal("evt-1", "p-1")
        p2 = self.svc.submit_proposal("evt-1", "p-2")
        self.svc.approve_proposal("p-1", ["head-coach"])
        with self.assertRaises(SlotConflictError):
            self.svc.approve_proposal("p-2", ["head-coach"])
        # 第二个方案可以拒绝，不影响已冻结阵容
        self.svc.reject_proposal("p-2", "名额已占用")
        self.assertEqual(self.svc.state.proposals["p-1"]["status"], "approved")

    def test_replay_same_decision_returns_same_result(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        replay = self.svc.replay_decision("p-1")
        self.assertTrue(replay["matches"])
        self.assertEqual(replay["frozen"], replay["replayed"])

    def test_restart_continues_pending_decisions(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        reopened = ExposureService.open(self.path)
        pending = reopened.pending()
        self.assertEqual([p["proposal_id"] for p in pending["pending_proposals"]], ["p-1"])
        self.assertTrue(pending["upcoming_moments"])
        # 重启后仍可完成批准
        reopened.approve_proposal("p-1", ["head-coach"])
        self.assertEqual(reopened.state.proposals["p-1"]["status"], "approved")

    def test_withdrawal_releases_only_unused_slots(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        self.svc.approve_proposal("p-1", ["head-coach"])
        # s1（单打）先出场被消耗
        singles = [i for i in self.svc.state.proposals["p-1"]["lineup"] if i["slot_ref"] == "s1"][0]
        self.svc.record_appearance(
            "evt-1", "s1", "app-1", used_for="singles_r32", result={"outcome": "win", "games": "3-1"}
        )
        doubles = [i for i in self.svc.state.proposals["p-1"]["lineup"] if i["slot_ref"] == "d1"][0]
        withdrawn = doubles["athlete_ids"][0]
        result = self.svc.record_withdrawal(withdrawn, "踝伤")
        self.assertIn(f"slot:evt-1:d1", result["released_slots"])
        self.assertNotIn("slot:evt-finals:s1", result["released_slots"])
        self.assertEqual(self.svc.state.slot_phase("slot:evt-1:s1"), "consumed")
        self.assertEqual(self.svc.state.slot_phase("slot:evt-1:d1"), "released")
        # 既往出场结果仍然保留
        history = self.svc.state.appearances[singles["athlete_ids"][0]]
        self.assertTrue(any(a["appearance_id"] == "app-1" for a in history))

    def test_future_effective_withdrawal_releases_on_clock_advance(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        self.svc.approve_proposal("p-1", ["head-coach"])
        doubles = [i for i in self.svc.state.proposals["p-1"]["lineup"] if i["slot_ref"] == "d1"][0]
        target = doubles["athlete_ids"][0]
        # 伤病在两天后复查生效，登记当下不释放
        self.svc.record_withdrawal(target, "复查后视情况", effective_from="2026-09-28T09:00:00+08:00")
        self.assertEqual(self.svc.state.slot_phase("slot:evt-1:d1"), "awarded")
        self.svc.advance_to("2026-09-28T09:00:00+08:00", "康复复查")
        self.assertEqual(self.svc.state.slot_phase("slot:evt-1:d1"), "released")

    def test_past_appearances_are_not_overwritten(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        self.svc.approve_proposal("p-1", ["head-coach"])
        singles = [i for i in self.svc.state.proposals["p-1"]["lineup"] if i["slot_ref"] == "s1"][0]
        aid = singles["athlete_ids"][0]
        self.svc.record_appearance("evt-1", "s1", "app-1", used_for="r32", result={"outcome": "loss"})
        # 之后画像更新、复盘发布，都不应改写既往出场
        self.svc.update_profile(aid, {**self.svc.state.profiles[aid], "result_score": 99})
        self.svc.publish_review("rev-1", "2026-10-06T00:00:00+08:00", [{"athlete_id": aid, "gap": "g1", "change": 1}])
        appearance = self.svc.state.appearances[aid][0]
        self.assertEqual(appearance["appearance_id"], "app-1")
        self.assertEqual(appearance["result"]["outcome"], "loss")

    def test_review_changes_gaps_additively(self) -> None:
        self.svc.publish_review(
            "rev-1",
            "2026-10-06T00:00:00+08:00",
            [{"athlete_id": "a1", "gap": "backhand", "change": 2, "reason": "对左手反手失分"}],
        )
        self.svc.publish_review(
            "rev-2",
            "2026-10-20T00:00:00+08:00",
            [{"athlete_id": "a1", "gap": "backhand", "change": -1, "reason": "训练补齐"}],
        )
        gaps = self.svc.gap_report()
        self.assertEqual(len(gaps), 1)  # 同键只保留最新判断
        self.assertEqual(gaps[0]["change"], -1)
        self.assertEqual(len(self.svc.state.reviews), 2)  # 复盘事件本身全部保留

    def test_explain_selected_and_held_back(self) -> None:
        self.svc.submit_proposal("evt-1", "p-1")
        selected = {a for i in self.svc.state.proposals["p-1"]["lineup"] for a in i["athlete_ids"]}
        for aid in selected:
            explanation = self.svc.explain_athlete("p-1", aid)
            self.assertEqual(explanation["status"], "selected")
            self.assertTrue(explanation["explanations"])
        not_chosen = set(self.svc.state.profiles) - selected
        for aid in not_chosen:
            explanation = self.svc.explain_athlete("p-1", aid)
            self.assertEqual(explanation["status"], "held_back")
            self.assertTrue(explanation["reasons"])


class ConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = make_service()
        build_world(self.svc)

    def test_invalid_clearance_blocks_selection(self) -> None:
        self.svc.record_clearance("a3", "2026-09-01T00:00:00+08:00", "all")  # 已过期
        out = self.svc.submit_proposal("evt-1", "p-1")
        explanation = self.svc.explain_athlete("p-1", "a3")
        self.assertTrue(any("健康许可" in r["message"] for r in explanation["reasons"]))

    def test_personal_load_cap_blocks(self) -> None:
        self.svc.record_load("a1", 90)  # +30 出场负荷将超过 100
        out = self.svc.submit_proposal("evt-1", "p-1")
        chosen = {a for i in out["result"]["lineup"] for a in i["athlete_ids"]}
        self.assertNotIn("a1", chosen)
        explanation = self.svc.explain_athlete("p-1", "a1")
        self.assertTrue(any("个人上限" in r["message"] for r in explanation["reasons"]))

    def test_team_load_cap_forces_empty_slot(self) -> None:
        # 全队上限收紧到只能上一个单打，双打盘必须留空
        tight = {**POLICY, "team_load_cap": 35}
        svc, _ = make_service()
        build_world(svc, policy=tight)
        out = svc.submit_proposal("evt-1", "p-1")
        self.assertEqual([i["slot_ref"] for i in out["result"]["lineup"]], ["s1"])
        self.assertIn("d1", out["result"]["empty_slots"])

    def test_evidence_gate_marks_insufficient_evidence(self) -> None:
        # a3 对 lefty 只有 1 份样本；设为硬门槛后不得入选单打
        strict = {**POLICY, "evidence_gates": ["style_edges"]}
        svc, _ = make_service()
        build_world(svc, policy=strict)
        out = svc.submit_proposal("evt-1", "p-1")
        chosen = {a for i in out["result"]["lineup"] if i["slot_ref"] == "s1" for a in i["athlete_ids"]}
        self.assertNotIn("a3", chosen)
        flagged = [g for g in out["result"]["evidence_gaps"] if g["athlete_id"] == "a3"]
        self.assertTrue(flagged)

    def test_deadline_blocks_submission(self) -> None:
        self.svc.advance_to("2026-10-02T00:00:00+08:00", "越过报名截止")
        with self.assertRaises(ServiceError):
            self.svc.submit_proposal("evt-1", "late")

    def test_solver_is_pure_and_deterministic(self) -> None:
        out1 = self.svc.submit_proposal("evt-1", "p-1")
        snapshot = self.svc.state.proposals["p-1"]["data_snapshot"]
        again = solve_lineup(snapshot)
        self.assertEqual(
            [(i["slot_ref"], tuple(i["athlete_ids"])) for i in out1["result"]["lineup"]],
            [(i["slot_ref"], tuple(i["athlete_ids"])) for i in again["lineup"]],
        )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.path = make_service()
        build_world(self.svc)

    def test_same_event_id_same_content_is_idempotent(self) -> None:
        event = self.svc.store.events()[0]
        count_before = len(self.svc.store.events())
        self.svc.store.append(event)  # 重放同一事件
        self.assertEqual(len(self.svc.store.events()), count_before)

    def test_same_id_different_content_is_isolated(self) -> None:
        event = dict(self.svc.store.events()[0])
        event["occurred_at"] = "2027-01-01T00:00:00+08:00"
        with self.assertRaises(EventConflictError):
            self.svc.store.append(event)

    def test_same_aggregate_id_different_lineup_is_isolated(self) -> None:
        # 不同赛事使用相同 slot 标识：聚合 id 含赛事前缀，互不串台
        self.svc.register_event(
            "evt-2",
            event_level="WTT_Contender",
            entry_deadline="2026-11-01T18:00:00+08:00",
            event_end="2026-11-05T22:00:00+08:00",
            slots=[{"slot_ref": "s1", "kind": "singles", "capacity": 1, "scope": "singles"}],
        )
        self.svc.submit_proposal("evt-1", "p-evt1")
        self.svc.submit_proposal("evt-2", "p-evt2")
        self.svc.approve_proposal("p-evt1", ["head-coach"])
        self.svc.approve_proposal("p-evt2", ["head-coach"])
        self.assertEqual(self.svc.state.slot_phase("slot:evt-1:s1"), "awarded")
        self.assertEqual(self.svc.state.slot_phase("slot:evt-2:s1"), "awarded")

    def test_aggregate_versions_are_contiguous(self) -> None:
        events = self.svc.store.events("a1")
        versions = [e["version"] for e in events]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))


class ClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.path = make_service()

    def test_clock_only_moves_forward_and_persists(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.advance_to("2026-09-01T00:00:00+08:00", "试图回退")
        self.svc.advance_to("2026-10-01T18:00:00+08:00", "报名截止")
        reopened = ExposureService.open(self.path)
        self.assertEqual(reopened.clock.now_iso(), "2026-10-01T10:00:00Z")

    def test_reason_text_covers_all_codes(self) -> None:
        for code in [
            "clearance_invalid",
            "load_unknown",
            "over_personal_load",
            "withdrawn",
            "evidence_gate_failed",
            "ranked_below_capacity",
            "lineup_tradeoff",
            "team_load_pressure",
        ]:
            self.assertNotEqual(reason_text(code, "x"), code)


if __name__ == "__main__":
    unittest.main()
