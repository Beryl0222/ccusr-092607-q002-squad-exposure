"""测试共用夹具：一支五人梯队 + 一站赛事 + 一版选拔政策。"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from squad_exposure.clock import SimulationClock
from squad_exposure.service import SquadExposureService
from squad_exposure.store import EventStore

CST = timezone(timedelta(hours=8))

# 队员 / 教练
A1 = "athlete-lin"
A2 = "athlete-chen"
A3 = "athlete-wang"
A4 = "athlete-zhao"
A5 = "athlete-sun"
COACH_A = "coach-li"
COACH_B = "coach-zhou"
DIRECTOR = "director-gao"

EVENT = "open-2026-finals"
POLICY_ID = "selection-policy"
POLICY_V1 = "2026.09"

RULES = {
    "max_individual_load_minutes": 300,
    "team_load_cap": 420,
    "min_pair_matches": 3,
    "min_matchup_sample": 2,
    "min_observations": 1,
    "observation_lookback_days": 30,
    "weight_short_term": 0.6,
    "weight_long_term": 0.4,
}

SLOTS = [
    {"slot_ref": "MS1", "discipline": "singles", "planned_minutes": 120, "opponent_style": "attacker"},
    {"slot_ref": "MS2", "discipline": "singles", "planned_minutes": 120, "opponent_style": "defender"},
    {"slot_ref": "MD1", "discipline": "doubles", "planned_minutes": 90, "opponent_style": "attacker"},
]

START = datetime(2026, 9, 26, 9, 0, tzinfo=CST)
DEADLINE = datetime(2026, 10, 1, 18, 0, tzinfo=CST)
EVENT_END = datetime(2026, 10, 5, 20, 0, tzinfo=CST)
CLEAR_UNTIL = datetime(2026, 12, 31, tzinfo=CST)
REHAB_REVIEW = datetime(2026, 10, 3, 10, 0, tzinfo=CST)


def build_service(tmp: str | Path | None = None) -> tuple[SquadExposureService, Path]:
    tmp_dir = Path(tmp or tempfile.mkdtemp())
    store = EventStore.open(tmp_dir / "events.jsonl", quarantine_dir=tmp_dir / "quarantine")
    clock = SimulationClock.start(START)
    svc = SquadExposureService(store, clock)

    svc.register_policy(POLICY_ID, POLICY_V1, RULES)
    svc.register_event(EVENT, "world_tour", DEADLINE, EVENT_END, SLOTS, team_load_cap=420)

    # 林：单打主力，对进攻型对手样本充足，带一名防守型缺口。
    svc.version_profile(
        A1,
        name="林越",
        coach_id=COACH_A,
        recent_load_minutes=120,
        opponent_matchups={
            "attacker": {"sample": 5, "value": 0.85},
            "defender": {"sample": 1, "value": 0.4},
        },
        pairs=[],
        gaps=[
            {"gap_id": "gap-defender-rhythm", "label": "对防守型节奏适应", "severity": 0.7,
             "trainable_against": "defender"}
        ],
    )
    # 陈：康复期，复查日在赛事结束前但晚于报名截止。
    svc.version_profile(
        A2,
        name="陈屿",
        coach_id=COACH_A,
        recent_load_minutes=40,
        opponent_matchups={"attacker": {"sample": 3, "value": 0.7}},
        pairs=[],
        gaps=[],
    )
    # 王/赵：固定双打组合，互相登记且有 4 场共同样本。
    svc.version_profile(
        A3,
        name="王澈",
        coach_id=COACH_B,
        recent_load_minutes=90,
        opponent_matchups={"attacker": {"sample": 4, "value": 0.66}},
        pairs=[{"partner_id": A4, "matches": 4}],
        gaps=[],
    )
    svc.version_profile(
        A4,
        name="赵启",
        coach_id=COACH_B,
        recent_load_minutes=90,
        opponent_matchups={"attacker": {"sample": 4, "value": 0.66}},
        pairs=[{"partner_id": A3, "matches": 4}],
        gaps=[],
    )
    # 孙：近期负荷已经逼近上限。
    svc.version_profile(
        A5,
        name="孙诺",
        coach_id=COACH_B,
        recent_load_minutes=260,
        opponent_matchups={"defender": {"sample": 3, "value": 0.55}},
        pairs=[],
        gaps=[],
    )

    svc.record_clearance(A1, "cleared", CLEAR_UNTIL, scope="all")
    svc.record_clearance(A2, "rehab", CLEAR_UNTIL, scope="all", review_at=REHAB_REVIEW)
    svc.record_clearance(A3, "cleared", CLEAR_UNTIL, scope=["world_tour"])
    svc.record_clearance(A4, "cleared", CLEAR_UNTIL, scope=["world_tour"])
    svc.record_clearance(A5, "cleared", CLEAR_UNTIL, scope="all")

    for aid, coach in ((A1, COACH_A), (A3, COACH_B), (A4, COACH_B), (A5, COACH_B)):
        svc.record_observation(aid, coach, "近四周训练观察：状态稳定", observed_at=START)

    return svc, tmp_dir


def reopen(tmp_dir: Path) -> SquadExposureService:
    """模拟重启：不传时钟，完全由事件流还原。"""
    store = EventStore.open(tmp_dir / "events.jsonl", quarantine_dir=tmp_dir / "quarantine")
    return SquadExposureService(store)
