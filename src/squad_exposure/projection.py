"""把事件流重放成读模型。

投影只做状态归并，不做业务裁决。关键不变量：
- 出场记录（``APPEARANCE_RECORDED``）一旦形成即永久保留，
  后续许可降级或评级调整都不会覆盖历史结果；
- 名额状态：awarded → used（已出场）/ released（退赛或伤病释放未使用机会）；
- 培养缺口由历次复盘的 ``gap_changes`` 累积，不做整表覆盖；
- 观察意见只追加；教练画像取资料最新版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AthleteState:
    athlete_id: str
    version: int = 0
    profile: dict[str, Any] = field(default_factory=dict)
    clearance: dict[str, Any] | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    gaps: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SlotState:
    slot_key: str
    event_ref: str
    slot_ref: str
    status: str = "open"  # open / awarded / used / released
    proposal_ref: str | None = None
    athlete_id: str | None = None
    athlete_ids: list[str] = field(default_factory=list)
    discipline: str | None = None
    award_payload: dict[str, Any] = field(default_factory=dict)
    appearance: dict[str, Any] | None = None
    release: dict[str, Any] | None = None


@dataclass
class ProposalState:
    proposal_id: str
    version: int = 0
    submitted: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        if self.decision is None:
            return "submitted"
        return str(self.decision["decision"])


@dataclass
class State:
    athletes: dict[str, AthleteState] = field(default_factory=dict)
    policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposals: dict[str, ProposalState] = field(default_factory=dict)
    slots: dict[str, SlotState] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    clock: str | None = None

    # ---- 便捷视图 -------------------------------------------------------------

    def athletes_for_coach(self, coach_id: str) -> list[str]:
        return [
            aid
            for aid, athlete in self.athletes.items()
            if athlete.profile.get("coach_id") == coach_id
        ]

    def pending_proposals(self) -> list[ProposalState]:
        return [p for p in self.proposals.values() if p.status == "submitted"]

    def open_slots(self, event_ref: str) -> list[SlotState]:
        return [
            s
            for s in self.slots.values()
            if s.event_ref == event_ref and s.status in ("open", "released")
        ]


def _slot_key(event_ref: str, slot_ref: str) -> str:
    return f"{event_ref}#{slot_ref}"


def apply_event(state: State, event: dict[str, Any]) -> None:
    etype = event["event_type"]
    agg = event["aggregate_type"]
    agg_id = event["aggregate_id"]
    payload = event["payload"]

    if etype == "CLOCK_ADVANCED":
        state.clock = payload["simulated_at"]
        return

    if agg == "athlete_profile":
        athlete = state.athletes.setdefault(agg_id, AthleteState(athlete_id=agg_id))
        if etype == "PROFILE_VERSIONED":
            athlete.version = event["version"]
            athlete.profile = dict(payload)
            for gap in payload.get("gaps", []):
                athlete.gaps[gap["gap_id"]] = dict(gap)
        elif etype == "CLEARANCE_RECORDED":
            athlete.clearance = dict(payload)
        elif etype == "OBSERVATION_RECORDED":
            athlete.observations.append(dict(payload))
        elif etype == "REVIEW_PUBLISHED":
            _apply_gap_changes(athlete, payload)
            state.reviews.append({"aggregate_id": agg_id, **payload})
        return

    if agg == "policy" and etype == "POLICY_PUBLISHED":
        state.policies[agg_id] = {"version": event["version"], **payload}
        return

    if agg == "competition_event" and etype == "EVENT_REGISTERED":
        state.events[agg_id] = {
            "event_id": agg_id,
            "version": event["version"],
            **payload,
        }
        for slot in payload.get("slots", []):
            key = _slot_key(agg_id, slot["slot_ref"])
            state.slots.setdefault(
                key,
                SlotState(slot_key=key, event_ref=agg_id, slot_ref=slot["slot_ref"]),
            )
        return

    if agg == "lineup_proposal":
        proposal = state.proposals.setdefault(agg_id, ProposalState(proposal_id=agg_id))
        proposal.version = event["version"]
        if etype == "PROPOSAL_SUBMITTED":
            proposal.submitted = dict(payload)
        elif etype == "DECISION_RECORDED":
            proposal.decision = dict(payload)
        return

    if agg == "competition_slot":
        slot = state.slots[agg_id]
        if etype == "SLOT_AWARDED":
            slot.status = "awarded"
            slot.proposal_ref = payload.get("proposal_ref")
            slot.athlete_id = payload.get("athlete_id")
            slot.athlete_ids = list(payload.get("athlete_ids") or [payload.get("athlete_id")])
            slot.discipline = payload.get("discipline")
            slot.award_payload = dict(payload)
        elif etype == "SLOT_RELEASED":
            # 已出场（used）的机会不能被释放；投影直接拒绝该非法状态迁移。
            if slot.status == "used":
                raise ValueError(f"名额 {agg_id} 已出场，历史出场不可撤回")
            slot.status = "released"
            slot.release = dict(payload)
        elif etype == "APPEARANCE_RECORDED":
            slot.status = "used"
            slot.appearance = dict(payload)
        return

    if agg == "development_gap" and etype == "REVIEW_PUBLISHED":
        for change in payload.get("gap_changes", []):
            athlete = state.athletes.setdefault(
                change["athlete_id"], AthleteState(athlete_id=change["athlete_id"])
            )
            _apply_gap_changes(athlete, payload, only=change["athlete_id"])
        state.reviews.append({"aggregate_id": agg_id, **payload})


def _apply_gap_changes(
    athlete: AthleteState, payload: dict[str, Any], only: str | None = None
) -> None:
    for change in payload.get("gap_changes", []):
        if only is not None and change["athlete_id"] != only:
            continue
        gap_id = change["gap_id"]
        gap = athlete.gaps.setdefault(
            gap_id,
            {"gap_id": gap_id, "label": change.get("label", gap_id), "severity": 0.0, "evidence": []},
        )
        if change.get("label"):
            gap["label"] = change["label"]
        gap["severity"] = round(gap.get("severity", 0.0) + change.get("delta", 0.0), 4)
        note = change.get("evidence")
        if note:
            gap.setdefault("evidence", []).append(
                {"evidence_cutoff": payload.get("evidence_cutoff"), "note": note}
            )


def replay(events: list[dict[str, Any]]) -> State:
    state = State()
    for event in events:
        apply_event(state, event)
    return state
