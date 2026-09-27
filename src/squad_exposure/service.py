"""应用服务：选拔工作流、回避审批、名额占用、释放语义、重放与解释。

所有状态都来自只追加事件流：构造服务时整段重放，因此进程重启后可以继续
处理待批准方案与未到达的里程碑。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .clock import SimulationClock, parse_ts
from .selection import reason_text, selection_reasons, solve_lineup
from .state import POLICY_AGGREGATE_ID, State, reduce_events
from .store import AggregateVersionError, EventStore


class ServiceError(RuntimeError):
    """工作流规则被违反。"""


class SlotConflictError(ServiceError):
    """报名位在本方案冻结后被并发方案占用。"""


def slot_aggregate_id(event_ref: str, slot_ref: str) -> str:
    return f"slot:{event_ref}:{slot_ref}"


class ExposureService:
    def __init__(self, store: EventStore, clock: SimulationClock | None = None) -> None:
        self.store = store
        self.clock = clock or SimulationClock(store)
        self._refresh()

    @classmethod
    def open(cls, path: str, schema: Mapping[str, Any] | None = None) -> "ExposureService":
        store = EventStore(path, schema=schema)
        return cls(store)

    def _refresh(self) -> None:
        self.state: State = reduce_events(self.store.events())

    # ---- 基础事实录入 ----

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: str | None = None,
        expected_version: int | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        version = self.store.next_version(aggregate_id)
        event = {
            "event_id": event_id or f"{event_type.lower()}-{aggregate_id}-v{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or self.clock.now_iso(),
            "version": version,
            "payload": dict(payload),
        }
        stored = self.store.append(event, expected_version=expected_version)
        self._refresh()
        return stored

    def register_athlete(self, athlete_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        if athlete_id in self.state.profiles:
            raise ServiceError(f"运动员已建档，更新画像请使用 update_profile: {athlete_id}")
        return self._emit("PROFILE_VERSIONED", "athlete_profile", athlete_id, {"profile": dict(profile)})

    def update_profile(self, athlete_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        if athlete_id not in self.state.profiles:
            raise ServiceError(f"运动员尚未建档: {athlete_id}")
        return self._emit("PROFILE_VERSIONED", "athlete_profile", athlete_id, {"profile": dict(profile)})

    def record_clearance(
        self,
        athlete_id: str,
        valid_until: str,
        scope: str,
        *,
        status: str = "granted",
        review_at: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"valid_until": valid_until, "scope": scope, "status": status}
        if review_at:
            payload["review_at"] = review_at
        return self._emit("CLEARANCE_RECORDED", "athlete_profile", athlete_id, payload)

    def record_load(self, athlete_id: str, load_index: int, as_of: str | None = None) -> dict[str, Any]:
        return self._emit(
            "LOAD_RECORDED",
            "athlete_profile",
            athlete_id,
            {"load_index": int(load_index), "as_of": as_of or self.clock.now_iso()},
        )

    def assign_coach(self, athlete_id: str, coach_id: str) -> dict[str, Any]:
        return self._emit("COACH_ASSIGNED", "athlete_profile", athlete_id, {"coach_id": coach_id})

    def version_policy(self, ruleset: Mapping[str, Any]) -> dict[str, Any]:
        return self._emit("POLICY_VERSIONED", "selection_policy", POLICY_AGGREGATE_ID, {"ruleset": dict(ruleset)})

    def register_event(
        self,
        event_ref: str,
        *,
        event_level: str,
        entry_deadline: str,
        event_end: str,
        slots: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if event_ref in self.state.events:
            raise ServiceError(f"赛事已登记: {event_ref}")
        if parse_ts(event_end) < parse_ts(entry_deadline):
            raise ServiceError("赛事结束时间不得早于报名截止时间")
        refs = [s["slot_ref"] for s in slots]
        if len(refs) != len(set(refs)):
            raise ServiceError("报名位标识在同一赛事内不得重复")
        return self._emit(
            "EVENT_REGISTERED",
            "competition_event",
            event_ref,
            {
                "event_level": event_level,
                "entry_deadline": entry_deadline,
                "event_end": event_end,
                "slots": [dict(s) for s in slots],
            },
        )

    def record_withdrawal(self, athlete_id: str, reason: str, effective_from: str | None = None) -> dict[str, Any]:
        """退赛/伤病登记。生效时刻到达后，只释放尚未使用（awarded 未 consumed）的机会。"""
        effective = effective_from or self.clock.now_iso()
        stored = self._emit(
            "WITHDRAWAL_RECORDED",
            "athlete_profile",
            athlete_id,
            {"athlete_id": athlete_id, "reason": reason, "effective_from": effective},
        )
        release = self.reconcile_due_effects()
        return {
            "withdrawal_event": stored["event_id"],
            "effective_from": effective,
            "released_slots": release["released_slots"],
        }

    def reconcile_due_effects(self) -> dict[str, Any]:
        """让已到生效时刻的退赛/伤病释放仍处于 awarded 的名额；已 consumed 的不出场记录保持不变。"""
        now = self.clock.now
        planned: list[tuple[str, str, str, dict[str, Any]]] = []
        released: list[str] = []
        for athlete_id, records in self.state.withdrawals.items():
            latest = records[-1]
            if parse_ts(latest["effective_from"]) > now:
                continue
            for event_ref, spec in self.state.events.items():
                for slot in spec["slots"]:
                    slot_id = slot_aggregate_id(event_ref, slot["slot_ref"])
                    history = self.state.slots.get(slot_id, [])
                    if not history or history[-1]["phase"] != "awarded":
                        continue  # 已使用（consumed）或已释放（released）都不动
                    if athlete_id not in self.state._slot_athletes(slot_id):
                        continue
                    planned.append(
                        (
                            "SLOT_RELEASED",
                            "competition_slot",
                            slot_id,
                            {
                                "slot_ref": slot["slot_ref"],
                                "reason": f"{latest['reason']}（退赛/伤病释放未使用名额）",
                                "withdrawal_event_id": latest["event_id"],
                            },
                        )
                    )
                    released.append(slot_id)
        if planned:
            batch, _ = self._plan_batch(planned)
            self.store.append_many(batch)
            self._refresh()
        return {"released_slots": released}

    def advance_to(self, target: str, reason: str) -> dict[str, Any]:
        """推进模拟时钟，并在新时刻对账生效中的退赛释放。"""
        stored = self.clock.advance_to(target, reason)
        self.reconcile_due_effects()
        return stored

    # ---- 选拔：冻结、提交、观察、审批 ----

    def submit_proposal(self, event_ref: str, proposal_id: str | None = None) -> dict[str, Any]:
        if event_ref not in self.state.events:
            raise ServiceError(f"赛事未登记: {event_ref}")
        if not self.state.policies:
            raise ServiceError("尚未发布任何选拔政策版本")
        spec = self.state.events[event_ref]
        now_dt = self.clock.now
        if now_dt > parse_ts(spec["entry_deadline"]):
            raise ServiceError(f"已过报名截止 {spec['entry_deadline']}，不能再提交方案")
        now = self.clock.now_iso()
        proposal_id = proposal_id or f"proposal:{event_ref}:{self.clock.now.strftime('%Y%m%d%H%M%S')}"
        if proposal_id in self.state.proposals:
            raise ServiceError(f"方案标识已存在: {proposal_id}")

        slot_versions = {
            slot["slot_ref"]: self.store.current_version(slot_aggregate_id(event_ref, slot["slot_ref"]))
            for slot in spec["slots"]
        }
        snapshot = self.state.build_snapshot(event_ref, now, slot_versions)
        result = solve_lineup(snapshot)

        payload = {
            "event_ref": event_ref,
            "policy_version": snapshot["policy_version"],
            "data_snapshot": snapshot,
            "lineup": result["lineup"],
            "decision": {k: v for k, v in result.items() if k not in ("lineup",)},
        }
        stored = self._emit("PROPOSAL_SUBMITTED", "lineup_proposal", proposal_id, payload)
        return {"proposal_id": proposal_id, "event": stored, "result": result}

    def submit_observation(self, proposal_id: str, coach_id: str, athlete_id: str, note: str) -> dict[str, Any]:
        proposal = self._require_pending(proposal_id)
        return self._emit(
            "OBSERVATION_SUBMITTED",
            "lineup_proposal",
            proposal_id,
            {"coach_id": coach_id, "athlete_id": athlete_id, "note": note},
        )

    def approve_proposal(self, proposal_id: str, approver_ids: Sequence[str]) -> dict[str, Any]:
        """批准并占用报名位；日常教练回避独自批准，并发占用整批失败。"""
        proposal = self._require_pending(proposal_id)
        approvers = list(dict.fromkeys(approver_ids))
        if not approvers:
            raise ServiceError("至少需要一名批准人")
        if not proposal["lineup"]:
            raise ServiceError("方案没有可执行阵容（报名位均已占用或无人具备资格），不能批准")
        snapshot = proposal["data_snapshot"]
        selected = {aid for item in proposal["lineup"] for aid in item["athlete_ids"]}
        selected_coaches = {snapshot["athletes"][aid].get("coach_id") for aid in selected}
        selected_coaches.discard(None)
        non_recused = [a for a in approvers if a not in selected_coaches]
        if not non_recused:
            offenders = sorted(set(approvers) & selected_coaches)
            raise ServiceError(
                "存在日常训练回避关系：入选队员的主管教练不能独自批准名额，"
                f"需追加非主管教练批准（待回避: {'、'.join(offenders)}）"
            )

        event_ref = proposal["event_ref"]
        planned: list[tuple[str, str, str, dict[str, Any]]] = [
            ("PROPOSAL_APPROVED", "lineup_proposal", proposal_id, {"approver_ids": approvers})
        ]
        frozen_versions: dict[str, int] = {}
        for item in proposal["lineup"]:
            slot_id = slot_aggregate_id(event_ref, item["slot_ref"])
            frozen_version = snapshot["slot_versions"][item["slot_ref"]]
            current = self.store.current_version(slot_id)
            if current != frozen_version:
                raise SlotConflictError(
                    f"报名位 {item['slot_ref']} 已被并发方案占用（冻结时 v{frozen_version}，当前 v{current}）"
                )
            frozen_versions[slot_id] = current
            planned.append(
                (
                    "SLOT_AWARDED",
                    "competition_slot",
                    slot_id,
                    {
                        "policy_version": proposal["policy_version"],
                        "slot_ref": item["slot_ref"],
                        "athlete_ids": list(item["athlete_ids"]),
                        "proposal_ref": proposal_id,
                    },
                )
            )
        planned.append(
            (
                "LINEUP_FROZEN",
                "lineup_proposal",
                proposal_id,
                {"lineup": proposal["lineup"], "proposal_ref": proposal_id},
            )
        )
        batch, expected = self._plan_batch(planned)
        # 报名位必须严格建立在冻结时观察到的基线上
        expected.update(frozen_versions)
        try:
            self.store.append_many(batch, expected_versions=expected)
        except AggregateVersionError as exc:  # 并发审批在落盘时兜底
            raise SlotConflictError(str(exc)) from exc
        self._refresh()
        return {"proposal_id": proposal_id, "approver_ids": approvers, "occupied": len(proposal["lineup"])}

    def reject_proposal(self, proposal_id: str, reason: str) -> dict[str, Any]:
        self._require_pending(proposal_id)
        stored = self._emit("PROPOSAL_REJECTED", "lineup_proposal", proposal_id, {"reason": reason})
        return {"proposal_id": proposal_id, "event": stored}

    # ---- 出场、复盘 ----

    def record_appearance(
        self,
        event_ref: str,
        slot_ref: str,
        appearance_id: str,
        *,
        used_for: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        slot_id = slot_aggregate_id(event_ref, slot_ref)
        if self.state.slot_phase(slot_id) != "awarded":
            raise ServiceError(f"报名位不在已授予未使用状态，不能登记出场: {slot_id}")
        payload = {
            "slot_ref": slot_ref,
            "used_for": used_for,
            "appearance": {"appearance_id": appearance_id, "result": dict(result)},
        }
        return self._emit("SLOT_CONSUMED", "competition_slot", slot_id, payload)

    def publish_review(
        self, review_id: str, evidence_cutoff: str, gap_changes: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        for change in gap_changes:
            for key in ("athlete_id", "gap", "change"):
                if key not in change:
                    raise ServiceError(f"复盘缺口变更缺少字段 {key}")
        return self._emit(
            "REVIEW_PUBLISHED",
            "development_gap",
            review_id,
            {"evidence_cutoff": evidence_cutoff, "gap_changes": [dict(c) for c in gap_changes]},
        )

    # ---- 重放、解释、恢复 ----

    def replay_decision(self, proposal_id: str) -> dict[str, Any]:
        """用冻结快照重新求解；结果必须与冻结阵容逐位一致。"""
        proposal = self.state.proposals[proposal_id]
        replay = solve_lineup(proposal["data_snapshot"])
        frozen = [(item["slot_ref"], tuple(item["athlete_ids"])) for item in proposal["lineup"]]
        redone = [(item["slot_ref"], tuple(item["athlete_ids"])) for item in replay["lineup"]]
        return {
            "proposal_id": proposal_id,
            "matches": frozen == redone,
            "frozen": frozen,
            "replayed": redone,
            "result": replay,
        }

    def explain_athlete(self, proposal_id: str, athlete_id: str) -> dict[str, Any]:
        proposal = self.state.proposals[proposal_id]
        selected_slots = [
            item["slot_ref"] for item in proposal["lineup"] if athlete_id in item["athlete_ids"]
        ]
        if selected_slots:
            points = [
                explanation
                for explanation in selection_reasons(proposal["lineup"])
                if explanation["athlete_id"] == athlete_id
            ]
            evidence = [
                gap
                for gap in proposal["decision"]["evidence_gaps"]
                if gap["athlete_id"] == athlete_id and gap["in_lineup"]
            ]
            return {
                "athlete_id": athlete_id,
                "status": "selected",
                "slots": selected_slots,
                "explanations": points,
                "evidence_gaps": evidence,
            }
        rows = [row for row in proposal["decision"]["not_selected"] if row["athlete_id"] == athlete_id]
        evidence = [
            gap
            for gap in proposal["decision"]["evidence_gaps"]
            if gap["athlete_id"] == athlete_id and not gap["in_lineup"]
        ]
        return {
            "athlete_id": athlete_id,
            "status": "held_back",
            "reasons": [
                {"code": r["code"], "message": reason_text(r["code"], r["detail"])}
                for row in rows
                for r in row["reasons"]
            ],
            "evidence_gaps": evidence,
        }

    def gap_report(self) -> list[dict[str, Any]]:
        """一次失利后培养缺口的当前判断（追加式，不删除历史）。"""
        return sorted(self.state.gaps.values(), key=lambda c: (c["athlete_id"], c["gap"]))

    def pending(self) -> dict[str, Any]:
        """重启后继续工作所需的两张清单：待处理决定与待到达里程碑。"""
        pending_proposals = [
            {"proposal_id": pid, "event_ref": p["event_ref"], "submitted_at": p["submitted_at"]}
            for pid, p in sorted(self.state.proposals.items())
            if p["status"] == "pending"
        ]
        now = self.clock.now
        moments: list[dict[str, Any]] = []
        for event_ref, spec in sorted(self.state.events.items()):
            decided = any(
                p["status"] == "approved" and p["event_ref"] == event_ref
                for p in self.state.proposals.values()
            )
            if not decided and parse_ts(spec["entry_deadline"]) >= now:
                moments.append({"kind": "entry_deadline", "ref": event_ref, "at": spec["entry_deadline"]})
            awarded_unused = any(
                history and history[-1]["phase"] == "awarded"
                for slot in spec["slots"]
                for history in [self.state.slots.get(slot_aggregate_id(event_ref, slot["slot_ref"]), [])]
            )
            if awarded_unused and parse_ts(spec["event_end"]) >= now:
                moments.append({"kind": "event_end", "ref": event_ref, "at": spec["event_end"]})
        for athlete_id, records in sorted(self.state.clearances.items()):
            latest = records[-1] if records else None
            if latest and latest.get("review_at") and parse_ts(latest["review_at"]) >= now:
                moments.append({"kind": "clearance_review", "ref": athlete_id, "at": latest["review_at"]})
        moments.sort(key=lambda m: m["at"])
        return {"now": self.clock.now_iso(), "pending_proposals": pending_proposals, "upcoming_moments": moments}

    # ---- 内部工具 ----

    def _require_pending(self, proposal_id: str) -> dict[str, Any]:
        if proposal_id not in self.state.proposals:
            raise ServiceError(f"方案不存在: {proposal_id}")
        proposal = self.state.proposals[proposal_id]
        if proposal["status"] != "pending":
            raise ServiceError(f"方案当前状态为 {proposal['status']}，不能重复处理")
        return proposal

    def _build_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "event_id": f"{event_type.lower()}-{aggregate_id}-v{self.store.next_version(aggregate_id)}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.now_iso(),
            "version": self.store.next_version(aggregate_id),
            "payload": dict(payload),
        }

    def _plan_batch(
        self, planned: Sequence[tuple[str, str, str, Mapping[str, Any]]]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """为一批事件分配每聚合连续版本；返回事件列表与各聚合的乐观基线。"""
        next_version = {
            aggregate_id: self.store.current_version(aggregate_id) + 1
            for _, _, aggregate_id, _ in planned
        }
        baselines = {
            aggregate_id: self.store.current_version(aggregate_id)
            for aggregate_id in next_version
        }
        batch: list[dict[str, Any]] = []
        for event_type, aggregate_type, aggregate_id, payload in planned:
            version = next_version[aggregate_id]
            batch.append(
                {
                    "event_id": f"{event_type.lower()}-{aggregate_id}-v{version}",
                    "event_type": event_type,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "occurred_at": self.clock.now_iso(),
                    "version": version,
                    "payload": dict(payload),
                }
            )
            next_version[aggregate_id] += 1
        return batch, baselines
