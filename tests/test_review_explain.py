"""赛后复盘增量、失利对培养缺口的影响、证据充分性报告与解释。"""

import unittest
from datetime import datetime, timedelta, timezone

from fixtures_support import (
    A1,
    A2,
    COACH_A,
    DIRECTOR,
    EVENT,
    build_service,
    reopen,
)
from squad_exposure.service import ServiceError

CST = timezone(timedelta(hours=8))


def singles(slot: str, athlete: str) -> dict:
    return {"slot_ref": slot, "athlete_id": athlete, "discipline": "singles"}


class ReviewAndGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp = build_service()

    def test_loss_raises_gap_and_later_profile_does_not_rewrite_history(self) -> None:
        # 林打进攻型对手，失利。
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p1")
        self.svc.decide_proposal("p1", "approved", DIRECTOR)
        self.svc.advance_to_event_end(EVENT)
        self.svc.record_appearance(EVENT, "MS1", "loss", score_line="1-2", event_id="app-ms1")

        review = self.svc.publish_review(
            "review-finals",
            [
                {
                    "athlete_id": A1,
                    "gap_id": "gap-attacker-clutch",
                    "label": "对进攻型关键分把握",
                    "delta": 0.5,
                    "evidence": "决赛决胜局 6 分中得 2 分",
                }
            ],
            evidence_cutoff=datetime(2026, 10, 5, 20, 0, tzinfo=CST),
            summary="男团决赛复盘",
        )
        change = review["changes"][0]
        self.assertEqual(0.0, change["severity_before"])
        self.assertEqual(0.5, change["severity_after"])

        # 此后再发一版画像（评级调整）不能覆盖历史出场结果。
        self.svc.version_profile(
            A1, name="林越", coach_id=COACH_A, recent_load_minutes=0,
            opponent_matchups={"attacker": {"sample": 6, "value": 0.9}}, pairs=[], gaps=[],
        )
        roster = self.svc.executable_roster(EVENT, "p1")["roster"][0]
        self.assertEqual("loss", roster["result"])
        # 复盘累积的缺口仍然保留，没有被空 gaps 的画像抹掉。
        gaps = {g["gap_id"]: g["severity"] for g in self.svc.gap_report(A1)["gaps"]}
        self.assertEqual(0.5, gaps["gap-attacker-clutch"])

    def test_gap_deltas_accumulate_and_survive_restart(self) -> None:
        self.svc.publish_review(
            "review-1",
            [{"athlete_id": A1, "gap_id": "g-x", "label": "X", "delta": 0.3}],
        )
        self.svc.publish_review(
            "review-2",
            [{"athlete_id": A1, "gap_id": "g-x", "delta": -0.1, "evidence": "加练见效"}],
        )
        revived = reopen(self.tmp)
        gaps = {g["gap_id"]: g["severity"] for g in revived.gap_report(A1)["gaps"]}
        self.assertEqual(0.2, gaps["g-x"])

    def test_explain_shows_selection_and_hold(self) -> None:
        # 入选解释：含评分与冻结政策版本。
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p-in")
        explanation = self.svc.explain("p-in", A1)
        self.assertEqual("selected", explanation["entries"][0]["status"])
        self.assertEqual("2026.09", explanation["frozen_policy_version"])
        self.assertTrue(explanation["fingerprint"])

        # 暂缓解释：康复中的陈屿。
        self.svc.submit_proposal(EVENT, [singles("MS2", A2)], proposal_id="p-hold")
        held = self.svc.explain("p-hold", A2)
        self.assertEqual("held", held["entries"][0]["status"])
        self.assertTrue(held["entries"][0]["reasons"])

    def test_evidence_report_flags_thin_matchups_and_missing_observations(self) -> None:
        report = self.svc.evidence_report(EVENT)
        by_athlete = {row["athlete_id"]: row for row in report["insufficient_evidence"]}
        # 陈屿没有观察记录；林对防守型对手样本仅 1，低于阈值 2。
        self.assertIn(A2, by_athlete)
        lin = by_athlete[A1]
        styles = {m["opponent_style"] for m in lin["thin_matchups"]}
        self.assertIn("defender", styles)

    def test_policy_freeze_ignores_newer_policy(self) -> None:
        # 截止前再发布一版更宽松的政策；已提交方案仍按旧版裁决。
        self.svc.submit_proposal(EVENT, [singles("MS1", A1)], proposal_id="p-old")
        self.svc.register_policy(
            "selection-policy", "2026.10",
            {"max_individual_load_minutes": 999, "team_load_cap": 9999,
             "min_pair_matches": 0, "min_matchup_sample": 99,
             "min_observations": 0, "weight_short_term": 1.0, "weight_long_term": 0.0},
        )
        evaluation = self.svc.frozen_evaluation("p-old")
        self.assertEqual("2026.09", evaluation.frozen_policy_version)

    def test_review_requires_real_athletes(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish_review(
                "review-bad", [{"athlete_id": "ghost", "gap_id": "g", "delta": 1.0}]
            )
        self.assertEqual("unknown_athlete", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
