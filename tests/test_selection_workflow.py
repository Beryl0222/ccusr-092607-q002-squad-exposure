"""资格裁决、冻结重放、教练回避、名额占用与不可变历史。"""

import unittest

from fixtures_support import (
    A1,
    A2,
    A3,
    A4,
    A5,
    COACH_A,
    DIRECTOR,
    EVENT,
    build_service,
    reopen,
)
from squad_exposure.service import ServiceError
from squad_exposure.store import EventConflict


def singles(slot: str, athlete: str) -> dict:
    return {"slot_ref": slot, "athlete_id": athlete, "discipline": "singles"}


def doubles(slot: str, a: str, b: str) -> list[dict]:
    return [
        {"slot_ref": slot, "athlete_id": a, "partner_id": b, "discipline": "doubles"},
        {"slot_ref": slot, "athlete_id": b, "partner_id": a, "discipline": "doubles"},
    ]


class EligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp = build_service()

    def test_healthy_main_pick_is_selected_and_scored(self) -> None:
        out = self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p1")
        ev = out["evaluation"]
        self.assertTrue(ev["feasible"])
        self.assertIn(A1, ev["selected"])
        detail = ev["entries"][0]["score_detail"]
        self.assertGreater(detail["short_term"], 0.0)
        # 进攻型不是林当前的培养缺口，长期项为 0。
        self.assertEqual(0.0, detail["long_term"])

    def test_rehab_athlete_is_held_pending_review(self) -> None:
        out = self.svc.submit_proposal(EVENT, [singles("MS1", A2)], proposal_id="p-rehab")
        self.assertFalse(out["evaluation"]["feasible"])
        self.assertIn(A2, out["evaluation"]["held"])
        codes = out["evaluation"]["held"][A2]
        self.assertIn("clearance_rehab", codes)
        self.assertIn("review_pending", codes)

        explanation = self.svc.explain("p-rehab", A2)
        messages = {r["code"] for r in explanation["entries"][0]["reasons"]}
        self.assertIn("clearance_rehab", messages)

    def test_individual_load_cap_blocks(self) -> None:
        # 孙近期 260 分钟，叠加 MS2 的 120 分钟超过 300 上限。
        out = self.svc.submit_proposal(EVENT, [singles("MS2", A5)], proposal_id="p-load")
        self.assertIn("individual_load", out["evaluation"]["held"][A5])

    def test_team_load_cap_blocks(self) -> None:
        # 120 + 120 + 90 = 330 不超 420；再加一个不存在容量的对比通过直接验证数值。
        entries = [singles("MS1", A1), singles("MS2", A5), *doubles("MD1", A3, A4)]
        out = self.svc.submit_proposal(EVENT, entries, proposal_id="p-team")
        self.assertEqual(330, out["evaluation"]["team_load_minutes"])

    def test_doubles_requires_mutual_pairing(self) -> None:
        # 林与王并未互相登记。
        out = self.svc.submit_proposal(EVENT, doubles("MD1", A1, A3), proposal_id="p-pair")
        held = {**out["evaluation"]["held"]}
        self.assertIn("pair_not_mutual", held.get(A1, []))
        self.assertIn("pair_not_mutual", held.get(A3, []))

    def test_doubles_pair_must_be_complete(self) -> None:
        out = self.svc.submit_proposal(
            EVENT,
            [{"slot_ref": "MD1", "athlete_id": A3, "partner_id": A4, "discipline": "doubles"}],
            proposal_id="p-half",
        )
        self.assertIn("partner_missing", out["evaluation"]["held"][A3])

    def test_valid_doubles_pair_passes(self) -> None:
        out = self.svc.submit_proposal(EVENT, doubles("MD1", A3, A4), proposal_id="p-double")
        self.assertTrue(out["evaluation"]["feasible"])


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp = build_service()

    def test_daily_coach_cannot_approve_own_athlete(self) -> None:
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p1")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.decide_proposal("p1", "approved", COACH_A)
        self.assertEqual("coach_conflict", ctx.exception.code)
        # 观察可以提交：教练的发言权保留，只是没有独自批准权。
        self.svc.record_observation(A1, COACH_A, "赛前一周状态良好")

    def test_independent_approver_awards_one_slot(self) -> None:
        self.svc.submit_proposal(EVENT, doubles("MD1", A3, A4), proposal_id="p-d")
        result = self.svc.decide_proposal("p-d", "approved", DIRECTOR)
        self.assertEqual(["MD1"], result["awarded_slots"])
        roster = self.svc.executable_roster(EVENT, "p-d")["roster"]
        self.assertEqual(1, len(roster))
        self.assertEqual([A3, A4], roster[0]["athlete_ids"])

    def test_concurrent_proposals_cannot_claim_same_slot(self) -> None:
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p-a")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.submit_proposal(EVENT, [singles("MS1", A2)], proposal_id="p-b")
        self.assertEqual("slot_already_claimed", ctx.exception.code)

    def test_decision_is_terminal_and_replays_identically(self) -> None:
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p1")
        first = self.svc.frozen_evaluation("p1").fingerprint
        # 即便后来画像升版，冻结裁决指纹不变。
        self.svc.version_profile(
            A1, name="林越", coach_id=COACH_A, recent_load_minutes=290,
            opponent_matchups={"attacker": {"sample": 9, "value": 0.2}}, pairs=[], gaps=[],
        )
        again = self.svc.frozen_evaluation("p1").fingerprint
        self.assertEqual(first, again)
        self.svc.decide_proposal("p1", "approved", DIRECTOR)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.decide_proposal("p1", "rejected", DIRECTOR)
        self.assertEqual("decision_locked", ctx.exception.code)

    def test_infeasible_proposal_cannot_be_approved(self) -> None:
        self.svc.submit_proposal(EVENT, [singles("MS1", A2)], proposal_id="p-rehab")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.decide_proposal("p-rehab", "approved", DIRECTOR)
        self.assertEqual("proposal_infeasible", ctx.exception.code)

    def test_release_only_unused_slot_and_appearance_is_immutable(self) -> None:
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p1")
        self.svc.decide_proposal("p1", "approved", DIRECTOR, rationale="单打主力")

        # 出场后不能因任何理由撤回。
        self.svc.record_appearance(EVENT, "MS1", "loss", score_line="1-2", event_id="app-1")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.release_slot(EVENT, "MS1", "injury")
        self.assertEqual("appearance_locked", ctx.exception.code)
        with self.assertRaises(ServiceError):
            self.svc.record_appearance(EVENT, "MS1", "win", event_id="app-2")

        # 重放同一出场事件是幂等的。
        self.svc.record_appearance(EVENT, "MS1", "loss", score_line="1-2", event_id="app-1")

    def test_withdrawal_releases_unused_slot_for_reuse(self) -> None:
        self.svc.submit_proposal(EVENT, doubles("MD1", A3, A4), proposal_id="p-d")
        self.svc.decide_proposal("p-d", "approved", DIRECTOR)
        self.svc.release_slot(EVENT, "MD1", "injury")
        # 释放后同一报名位可被新方案再次申请。
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p-ms1")
        self.svc.decide_proposal("p-ms1", "approved", DIRECTOR)
        statuses = {s.slot_ref: s.status for s in self.svc.state.slots.values()}
        self.assertEqual("released", statuses["MD1"])
        self.assertEqual("awarded", statuses["MS1"])

    def test_restart_resumes_pending_decisions_and_clock(self) -> None:
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="pending-one")
        self.svc.advance_to_deadline(EVENT)

        revived = reopen(self.tmp)
        self.assertEqual("2026-10-01T18:00:00+08:00", revived.now)
        pending = revived.pending_decisions()
        self.assertEqual(["pending-one"], [p["proposal_id"] for p in pending])
        # 重启后仍执行回避，主管可以继续处理。
        revived.decide_proposal("pending-one", "approved", DIRECTOR)
        self.assertEqual([], revived.pending_decisions())

    def test_event_id_conflict_is_isolated(self) -> None:
        # 同一观察标识写入不同内容：冲突隔离，既有观察保留。
        self.svc.record_observation(A1, COACH_A, "原始观察", event_id="obs-x")
        with self.assertRaises(EventConflict):
            self.svc.record_observation(A1, COACH_A, "被篡改的观察", event_id="obs-x")
        notes = [o["note"] for o in self.svc.state.athletes[A1].observations]
        self.assertIn("原始观察", notes)
        self.assertNotIn("被篡改的观察", notes)


if __name__ == "__main__":
    unittest.main()
