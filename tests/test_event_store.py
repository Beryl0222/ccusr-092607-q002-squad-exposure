"""事件存储、时钟与冻结隔离的基础性质。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixtures_support import (
    A2,
    EVENT,
    build_service,
)
from squad_exposure.clock import SimulationClock
from squad_exposure.store import (
    AggregateVersionConflict,
    EventConflict,
    EventStore,
)

CST = timezone(timedelta(hours=8))


def _event(event_id="e1", agg="athlete_profile", agg_id="a1", version=1, **payload):
    return {
        "event_id": event_id,
        "event_type": "CLEARANCE_RECORDED",
        "aggregate_type": agg,
        "aggregate_id": agg_id,
        "occurred_at": "2026-09-26T09:00:00+08:00",
        "version": version,
        "payload": {"valid_until": "2026-12-31T00:00:00+08:00", "scope": "all", **payload},
    }


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = EventStore.open(self.tmp / "events.jsonl", quarantine_dir=self.tmp / "q")

    def test_versions_increment_per_aggregate(self) -> None:
        self.store.append(_event(version=1))
        self.store.append(_event(event_id="e2", version=2, note="第二次"))
        with self.assertRaises(AggregateVersionConflict):
            self.store.append(_event(event_id="e3", version=2))

    def test_same_event_id_is_idempotent(self) -> None:
        first = self.store.append(_event())
        again = self.store.append(_event())
        self.assertTrue(again.deduped)
        self.assertEqual(first.event, again.event)
        lines = (self.tmp / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(lines))

    def test_same_id_different_content_is_quarantined(self) -> None:
        self.store.append(_event())
        with self.assertRaises(EventConflict) as ctx:
            self.store.append(_event(scope=["world_tour"]))
        quarantine = ctx.exception.quarantine_path
        self.assertIsNotNone(quarantine)
        saved = json.loads(quarantine.read_text(encoding="utf-8"))
        self.assertEqual(["world_tour"], saved["payload"]["scope"])
        # 已提交的事件流保持原样，未被污染。
        lines = (self.tmp / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        committed = json.loads(lines[0])
        self.assertEqual("all", committed["payload"]["scope"])

    def test_contract_violation_rejected(self) -> None:
        bad = _event()
        del bad["payload"]["scope"]
        with self.assertRaises(Exception):
            self.store.append(bad)


class ClockTests(unittest.TestCase):
    def test_clock_cannot_go_backwards(self) -> None:
        clock = SimulationClock.start(datetime(2026, 9, 26, tzinfo=CST))
        moved = clock.advance_to(datetime(2026, 10, 1, tzinfo=CST))
        with self.assertRaises(ValueError):
            moved.advance_to(datetime(2026, 9, 30, tzinfo=CST))

    def test_clock_requires_timezone(self) -> None:
        with self.assertRaises(ValueError):
            SimulationClock.start(datetime(2026, 9, 26))

    def test_service_advances_to_milestones(self) -> None:
        svc, _ = build_service()
        svc.advance_to_deadline("open-2026-finals")
        self.assertEqual("2026-10-01T18:00:00+08:00", svc.now)
        # 恰好停在截止时刻仍可提交；再往后走一分钟则过截止。
        svc.advance_clock(datetime(2026, 10, 1, 18, 1, tzinfo=CST))
        from squad_exposure.service import ServiceError

        with self.assertRaises(ServiceError) as ctx:
            svc.submit_proposal(
                "open-2026-finals",
                [{"slot_ref": "MS1", "athlete_id": "athlete-lin"}],
            )
        self.assertEqual("past_deadline", ctx.exception.code)


    def test_advance_to_rehab_review_then_clearance_unblocks(self) -> None:
        from squad_exposure.selection import build_snapshot, evaluate_snapshot

        svc, _ = build_service()
        svc.submit_proposal(
            EVENT, [{"slot_ref": "MS1", "athlete_id": A2, "discipline": "singles"}],
            proposal_id="p-before-review",
        )
        held_before = svc.frozen_evaluation("p-before-review").held[A2]
        self.assertIn("review_pending", held_before)

        # 推进时钟到康复复查日，复查通过后许可改为 cleared。
        svc.advance_to_rehab_review(A2)
        svc.record_clearance(A2, "cleared", "2026-12-31T00:00:00+08:00", scope="all")
        # 用当前数据重裁一份新快照：康复原因消失（报名截止约束另算）。
        snapshot = build_snapshot(
            svc.state, EVENT,
            [{"slot_ref": "MS1", "athlete_id": A2, "discipline": "singles"}],
            "2026.09", svc.now,
        )
        replayed = evaluate_snapshot(snapshot)
        self.assertNotIn("clearance_rehab", replayed.held.get(A2, []))


if __name__ == "__main__":
    unittest.main()
