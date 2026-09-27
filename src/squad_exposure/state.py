"""把只追加事件流归约为当前状态，并为选拔决策生成冻结快照。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .clock import parse_ts

POLICY_AGGREGATE_ID = "policy:main"


@dataclass
class State:
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    profile_versions: dict[str, int] = field(default_factory=dict)
    clearances: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    loads: dict[str, dict[str, Any]] = field(default_factory=dict)
    coaches: dict[str, str] = field(default_factory=dict)
    policies: list[dict[str, Any]] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposals: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # slot 标识 -> 状态记录（awarded / consumed / released 的完整轨迹）
    slots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    appearances: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    withdrawals: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    gaps: dict[str, dict[str, Any]] = field(default_factory=dict)

    def apply(self, event: dict[str, Any]) -> None:
        et = event["event_type"]
        agg = event["aggregate_id"]
        p = event["payload"]
        if et == "PROFILE_VERSIONED":
            self.profiles[agg] = deepcopy(p["profile"])
            self.profile_versions[agg] = event["version"]
        elif et == "CLEARANCE_RECORDED":
            self.clearances.setdefault(agg, []).append(
                {
                    "status": p.get("status", "granted"),
                    "valid_until": p["valid_until"],
                    "scope": p["scope"],
                    "review_at": p.get("review_at"),
                    "recorded_at": event["occurred_at"],
                    "version": event["version"],
                }
            )
        elif et == "LOAD_RECORDED":
            self.loads[agg] = {"load_index": p["load_index"], "as_of": p["as_of"], "version": event["version"]}
        elif et == "COACH_ASSIGNED":
            self.coaches[agg] = p["coach_id"]
        elif et == "POLICY_VERSIONED":
            self.policies.append({"version": event["version"], "ruleset": deepcopy(p["ruleset"])})
        elif et == "EVENT_REGISTERED":
            self.events[agg] = {
                **deepcopy(p),
                "version": event["version"],
            }
        elif et == "PROPOSAL_SUBMITTED":
            self.proposals[agg] = {
                "status": "pending",
                "submitted_at": event["occurred_at"],
                **deepcopy(p),
                "version": event["version"],
            }
        elif et == "OBSERVATION_SUBMITTED":
            self.observations.setdefault(agg, []).append(
                {
                    "coach_id": p["coach_id"],
                    "athlete_id": p["athlete_id"],
                    "note": p["note"],
                    "at": event["occurred_at"],
                }
            )
        elif et == "PROPOSAL_APPROVED":
            self.proposals[agg]["status"] = "approved"
            self.proposals[agg]["approver_ids"] = list(p["approver_ids"])
            self.proposals[agg]["approved_at"] = event["occurred_at"]
        elif et == "PROPOSAL_REJECTED":
            self.proposals[agg]["status"] = "rejected"
            self.proposals[agg]["reject_reason"] = p["reason"]
        elif et == "LINEUP_FROZEN":
            self.proposals[agg]["frozen_lineup"] = deepcopy(p["lineup"])
            self.proposals[agg]["frozen_at"] = event["occurred_at"]
        elif et == "SLOT_AWARDED":
            self.slots.setdefault(agg, []).append(
                {
                    "phase": "awarded",
                    "slot_ref": p["slot_ref"],
                    "athlete_ids": list(p["athlete_ids"]),
                    "proposal_ref": p.get("proposal_ref"),
                    "policy_version": p["policy_version"],
                    "at": event["occurred_at"],
                }
            )
        elif et == "SLOT_CONSUMED":
            self.slots.setdefault(agg, []).append(
                {
                    "phase": "consumed",
                    "slot_ref": p["slot_ref"],
                    "appearance_id": p["used_for"],
                    "used_for": p["used_for"],
                    "at": event["occurred_at"],
                }
            )
            appearance = deepcopy(p.get("appearance", {}))
            appearance.setdefault("at", event["occurred_at"])
            for athlete_id in self._slot_athletes(agg):
                self.appearances.setdefault(athlete_id, []).append(deepcopy(appearance))
        elif et == "SLOT_RELEASED":
            self.slots.setdefault(agg, []).append(
                {"phase": "released", "slot_ref": p["slot_ref"], "reason": p["reason"], "at": event["occurred_at"]}
            )
        elif et == "WITHDRAWAL_RECORDED":
            self.withdrawals.setdefault(p["athlete_id"], []).append(
                {
                    "event_id": event["event_id"],
                    "reason": p["reason"],
                    "effective_from": p["effective_from"],
                    "at": event["occurred_at"],
                }
            )
        elif et == "REVIEW_PUBLISHED":
            review = {
                "review_id": agg,
                "evidence_cutoff": p["evidence_cutoff"],
                "gap_changes": deepcopy(p["gap_changes"]),
                "at": event["occurred_at"],
            }
            self.reviews.append(review)
            for change in p["gap_changes"]:
                key = f"{change['athlete_id']}|{change['gap']}"
                self.gaps[key] = deepcopy(change)

    def _slot_athletes(self, slot_aggregate_id: str) -> list[str]:
        history = self.slots.get(slot_aggregate_id, [])
        for entry in reversed(history):
            if entry["phase"] == "awarded":
                return list(entry["athlete_ids"])
        return []

    # ---- 读侧查询 ----

    def slot_phase(self, slot_aggregate_id: str) -> str | None:
        history = self.slots.get(slot_aggregate_id)
        return history[-1]["phase"] if history else None

    def active_clearance(self, athlete_id: str, scope: str, now_iso: str) -> dict[str, Any] | None:
        now = parse_ts(now_iso)
        for c in reversed(self.clearances.get(athlete_id, [])):
            if c["status"] != "granted":
                return None
            if parse_ts(c["valid_until"]) <= now:
                return None
            if c["scope"] not in ("all", scope):
                return None
            return c
        return None

    def current_policy(self) -> dict[str, Any] | None:
        return self.policies[-1] if self.policies else None

    def build_snapshot(self, event_ref: str, now_iso: str, slot_versions: dict[str, int]) -> dict[str, Any]:
        """冻结选拔所依据的全部事实；求解器只允许读取这个快照。"""
        event = self.events[event_ref]
        athletes: dict[str, Any] = {}
        for athlete_id, profile in self.profiles.items():
            athletes[athlete_id] = {
                "profile_version": self.profile_versions[athlete_id],
                "profile": deepcopy(profile),
                "load": deepcopy(self.loads.get(athlete_id)),
                "coach_id": self.coaches.get(athlete_id),
                "clearance_scope_all": self.active_clearance(athlete_id, "all", now_iso),
                "clearance_scope_singles": self.active_clearance(athlete_id, "singles", now_iso),
                "clearance_scope_team": self.active_clearance(athlete_id, "team", now_iso),
                "appearances": deepcopy(self.appearances.get(athlete_id, [])),
                "observations": len(
                    [o for obs in self.observations.values() for o in obs if o["athlete_id"] == athlete_id]
                ),
            }
        occupied_slots: dict[str, dict[str, Any]] = {}
        for slot in event["slots"]:
            slot_ref = slot["slot_ref"]
            history = self.slots.get(_slot_id(event_ref, slot_ref), [])
            if history and history[-1]["phase"] == "awarded":
                occupied_slots[slot_ref] = {
                    "athlete_ids": self._slot_athletes(_slot_id(event_ref, slot_ref)),
                    "at": history[-1]["at"],
                }
        withdrawn_active = sorted(
            athlete_id
            for athlete_id, records in self.withdrawals.items()
            if records and parse_ts(records[-1]["effective_from"]) <= parse_ts(now_iso)
        )
        return {
            "frozen_at": now_iso,
            "event_ref": event_ref,
            "event": deepcopy(event),
            "policy_version": self.current_policy()["version"],
            "policy": deepcopy(self.current_policy()["ruleset"]),
            "athletes": athletes,
            "occupied_slots": occupied_slots,
            "withdrawn_active": withdrawn_active,
            "slot_versions": deepcopy(slot_versions),
        }


def _slot_id(event_ref: str, slot_ref: str) -> str:
    return f"slot:{event_ref}:{slot_ref}"


def reduce_events(events: list[dict[str, Any]]) -> State:
    """按事件流的追加（因果）顺序归约；同批事件时间戳相同，不能按时间戳重排。"""
    state = State()
    for event in events:
        state.apply(event)
    return state
