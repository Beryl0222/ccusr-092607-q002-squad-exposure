"""梯队实战机会配置簿应用服务。

所有命令都走事件存储，服务本身不保存可变状态：重启时重放事件流即可
恢复时钟、待批提案和名额占用情况。

核心工作流：
1. 注册政策版本与赛事（级别、截止时间、名额、全队负荷上限）；
2. 运动员资料/健康许可/教练观察按版本追加；
3. 提交阵容提案时冻结采用的数据快照、政策版本与裁决结果；
4. 审批执行教练回避：日常带训教练不能批准自己队员的名额；
5. 批准瞬间占用报名位；退赛或伤病只能释放尚未使用的名额；
6. 出场与结果一经记录不可改写，后来的评级变化不覆盖历史；
7. 赛后复盘以增量 ``gap_changes`` 改写培养缺口，并可回看差异。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Sequence

from .clock import CLOCK_AGGREGATE, CLOCK_ID, SimulationClock
from .projection import State, apply_event, replay
from .selection import (
    REASON_TEXT,
    Evaluation,
    build_snapshot,
    evaluate_snapshot,
    explain,
)
from .store import EventStore
from .time_utils import now_iso, parse_iso, to_jsonable


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _slot_key(event_ref: str, slot_ref: str) -> str:
    return f"{event_ref}#{slot_ref}"


class SquadExposureService:
    def __init__(self, store: EventStore, clock: SimulationClock | None = None):
        self.store = store
        self.state: State = replay(store.load())
        if self.state.clock is not None:
            self.clock = SimulationClock(parse_iso(self.state.clock))
        elif clock is not None:
            self.clock = clock
        else:
            self.clock = SimulationClock.start()
            # 启动时钟也落一条事件，保证重启可还原。
            self._append(CLOCK_AGGREGATE, CLOCK_ID, {"simulated_at": self.clock.iso})

    # ---- 内部 -----------------------------------------------------------------

    def _append(
        self,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        event_type: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        version = 1 + sum(
            1
            for e in self.store.load()
            if e["aggregate_type"] == aggregate_type and e["aggregate_id"] == aggregate_id
        )
        event = {
            "event_id": event_id or f"{aggregate_id}-v{version}-{uuid.uuid4().hex[:8]}",
            "event_type": event_type or payload_event_type(aggregate_type),
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.iso,
            "version": version,
            "payload": to_jsonable(payload),
        }
        outcome = self.store.append(event)
        if not outcome.deduped:
            # 仅当确实是新事实时才推进投影；幂等命中直接返回既有事件。
            apply_event(self.state, outcome.event)
        return outcome.event

    # ---- 时钟 -----------------------------------------------------------------

    def advance_clock(self, to: datetime | str) -> str:
        target = parse_iso(to) if isinstance(to, str) else to
        self.clock = self.clock.advance_to(target)
        self._append(CLOCK_AGGREGATE, CLOCK_ID, {"simulated_at": self.clock.iso})
        return self.clock.iso

    def advance_to_deadline(self, event_ref: str) -> str:
        return self.advance_clock(self.state.events[event_ref]["entry_deadline"])

    def advance_to_event_end(self, event_ref: str) -> str:
        competition = self.state.events[event_ref]
        return self.advance_clock(competition.get("competition_end", competition["entry_deadline"]))

    def advance_to_rehab_review(self, athlete_id: str) -> str:
        clearance = self.state.athletes[athlete_id].clearance
        if not clearance or not clearance.get("review_at"):
            raise ServiceError("no_rehab_review", "该队员没有待完成的康复复查安排")
        return self.advance_clock(clearance["review_at"])

    @property
    def now(self) -> str:
        return self.clock.iso

    # ---- 政策与赛事 -----------------------------------------------------------

    def register_policy(self, policy_id: str, policy_version: str, rules: dict[str, Any]) -> str:
        if any(p["policy_version"] == policy_version for p in self.state.policies.values()):
            raise ServiceError("policy_version_exists", f"政策版本已存在: {policy_version}")
        self._append(
            "policy",
            policy_id,
            {"policy_version": policy_version, "rules": to_jsonable(rules)},
            event_type="POLICY_PUBLISHED",
        )
        return policy_version

    def latest_policy_version(self) -> str:
        versions = sorted(p["policy_version"] for p in self.state.policies.values())
        if not versions:
            raise ServiceError("no_policy", "尚未注册任何选拔政策")
        return versions[-1]

    def register_event(
        self,
        event_ref: str,
        event_level: str,
        entry_deadline: datetime | str,
        competition_end: datetime | str,
        slots: Sequence[dict[str, Any]],
        team_load_cap: int | None = None,
    ) -> None:
        if event_ref in self.state.events:
            raise ServiceError("event_exists", f"赛事已注册: {event_ref}")
        refs = [s["slot_ref"] for s in slots]
        if len(refs) != len(set(refs)):
            raise ServiceError("duplicate_slot_definition", "赛事报名位标识重复")
        payload = {
            "event_level": event_level,
            "entry_deadline": _iso(entry_deadline),
            "competition_end": _iso(competition_end),
            "slots": to_jsonable(slots),
        }
        if team_load_cap is not None:
            payload["team_load_cap"] = team_load_cap
        self._append(
            "competition_event",
            event_ref,
            payload,
            event_type="EVENT_REGISTERED",
        )

    # ---- 运动员资料 -----------------------------------------------------------

    def version_profile(self, athlete_id: str, **fields: Any) -> int:
        """写入技术画像新版本。recent_load_minutes / opponent_matchups /
        pairs / gaps / coach_id / name 等整体随版本冻结。"""
        payload = to_jsonable(fields)
        self._append(
            "athlete_profile",
            athlete_id,
            payload,
            event_type="PROFILE_VERSIONED",
        )
        return self.state.athletes[athlete_id].version

    def record_clearance(
        self,
        athlete_id: str,
        status: str,
        valid_until: datetime | str,
        scope: str | Sequence[str] = "all",
        review_at: datetime | str | None = None,
        note: str | None = None,
    ) -> None:
        if status not in ("cleared", "restricted", "rehab"):
            raise ServiceError("bad_clearance_status", "许可状态必须是 cleared/restricted/rehab")
        payload: dict[str, Any] = {
            "status": status,
            "valid_until": _iso(valid_until),
            "scope": scope,
        }
        if review_at is not None:
            payload["review_at"] = _iso(review_at)
        if note:
            payload["note"] = note
        self._append(
            "athlete_profile",
            athlete_id,
            payload,
            event_type="CLEARANCE_RECORDED",
        )

    def record_observation(
        self,
        athlete_id: str,
        coach_id: str,
        note: str,
        observed_at: datetime | str | None = None,
        event_id: str | None = None,
    ) -> None:
        self._require_athlete(athlete_id)
        payload = {
            "coach_id": coach_id,
            "note": note,
            "observed_at": _iso(observed_at or self.clock.current),
        }
        self._append(
            "athlete_profile",
            athlete_id,
            payload,
            event_type="OBSERVATION_RECORDED",
            event_id=event_id,
        )

    # ---- 提案、冻结与审批 ------------------------------------------------------

    def submit_proposal(
        self,
        event_ref: str,
        entries: Sequence[dict[str, Any]],
        policy_version: str | None = None,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        if event_ref not in self.state.events:
            raise ServiceError("unknown_event", f"赛事未注册: {event_ref}")
        if not entries:
            raise ServiceError("empty_proposal", "方案至少包含一个报名条目")
        policy_version = policy_version or self.latest_policy_version()
        competition = self.state.events[event_ref]

        # 截止时间：以模拟时钟为准。
        if parse_iso(self.clock.iso) > parse_iso(competition["entry_deadline"]):
            raise ServiceError("past_deadline", REASON_TEXT["past_deadline"])

        for aid in {e["athlete_id"] for e in entries} | {
            e.get("partner_id") for e in entries if e.get("partner_id")
        }:
            self._require_athlete(aid)

        # 并发方案不得重复占用同一报名位。
        claimed = self._claimed_slots(event_ref)
        for entry in entries:
            key = _slot_key(event_ref, entry["slot_ref"])
            if key not in self.state.slots:
                raise ServiceError("unknown_slot", f"{REASON_TEXT['unknown_slot']}: {entry['slot_ref']}")
            if key in claimed:
                raise ServiceError(
                    "slot_already_claimed",
                    f"报名位已被其他在途方案占用: {entry['slot_ref']}（方案 {claimed[key]}）",
                )
            slot = self.state.slots[key]
            if slot.status in ("awarded", "used"):
                raise ServiceError(
                    "slot_unavailable", f"报名位 {entry['slot_ref']} 已占用/已出场"
                )

        snapshot = build_snapshot(
            self.state, event_ref, list(entries), policy_version, self.clock.iso
        )
        evaluation = evaluate_snapshot(snapshot)
        proposal_id = proposal_id or f"proposal-{uuid.uuid4().hex[:10]}"
        if proposal_id in self.state.proposals:
            raise ServiceError("proposal_exists", f"方案标识已存在: {proposal_id}")

        payload = {
            "event_ref": event_ref,
            "frozen_policy_version": policy_version,
            "entries": to_jsonable(entries),
            "snapshot": snapshot,
            "evaluation": evaluation.to_dict(),
        }
        self._append(
            "lineup_proposal",
            proposal_id,
            payload,
            event_type="PROPOSAL_SUBMITTED",
        )
        return {"proposal_id": proposal_id, "evaluation": evaluation.to_dict()}

    def pending_decisions(self) -> list[dict[str, Any]]:
        """重启后据此继续处理待批决定。"""
        return [
            {
                "proposal_id": p.proposal_id,
                "event_ref": p.submitted["event_ref"],
                "feasible": p.submitted["evaluation"]["feasible"],
                "fingerprint": p.submitted["evaluation"]["fingerprint"],
                "athlete_ids": sorted({e["athlete_id"] for e in p.submitted["entries"]}),
            }
            for p in self.state.pending_proposals()
        ]

    def decide_proposal(
        self,
        proposal_id: str,
        decision: str,
        decided_by: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise ServiceError("bad_decision", "决定必须是 approved 或 rejected")
        proposal = self.state.proposals.get(proposal_id)
        if proposal is None or proposal.submitted is None:
            raise ServiceError("unknown_proposal", f"方案不存在: {proposal_id}")
        if proposal.decision is not None:
            raise ServiceError(
                "decision_locked", f"方案已有终局决定: {proposal.decision['decision']}"
            )

        submitted = proposal.submitted
        athlete_ids = {e["athlete_id"] for e in submitted["entries"]}
        # 教练回避：任何一名入选队员的日常带训教练都不能批准该方案。
        conflict_athletes = [
            aid
            for aid in athlete_ids
            if self.state.athletes[aid].profile.get("coach_id") == decided_by
        ]
        if conflict_athletes:
            raise ServiceError(
                "coach_conflict",
                f"审批人是队员日常带训教练，不能独自批准名额: {', '.join(sorted(conflict_athletes))}",
            )

        # 重放同一决定返回原结果：用冻结快照重新裁决并核对指纹。
        replayed = evaluate_snapshot(submitted["snapshot"])
        if replayed.fingerprint != submitted["evaluation"]["fingerprint"]:
            raise ServiceError("snapshot_tampered", "冻结数据指纹不一致，裁决不可信")
        if decision == "approved" and not replayed.feasible:
            raise ServiceError(
                "proposal_infeasible",
                "方案未通过硬约束，不能批准："
                + "；".join(
                    REASON_TEXT.get(r, r)
                    for held in replayed.held.values()
                    for r in held
                    if r in REASON_TEXT
                ),
            )

        event_ref = submitted["event_ref"]
        if decision == "approved":
            # 批准前再次确认报名位仍未被占用（时间线靠后到达的释放/占用）。
            for entry in submitted["entries"]:
                slot = self.state.slots[_slot_key(event_ref, entry["slot_ref"])]
                if slot.status in ("awarded", "used"):
                    raise ServiceError(
                        "slot_unavailable", f"报名位 {entry['slot_ref']} 已被占用"
                    )

        payload = {
            "decision": decision,
            "decided_by": decided_by,
            "frozen_policy_version": submitted["frozen_policy_version"],
            "fingerprint": submitted["evaluation"]["fingerprint"],
        }
        if rationale:
            payload["rationale"] = rationale
        self._append(
            "lineup_proposal",
            proposal_id,
            payload,
            event_type="DECISION_RECORDED",
        )

        awarded: list[str] = []
        if decision == "approved":
            # 每个报名位只产生一个授予事件；双打位携带双方队员。
            entries_by_slot: dict[str, list[dict[str, Any]]] = {}
            for entry in submitted["entries"]:
                entries_by_slot.setdefault(entry["slot_ref"], []).append(entry)
            for slot_ref, slot_entries in entries_by_slot.items():
                key = _slot_key(event_ref, slot_ref)
                athlete_ids = [e["athlete_id"] for e in slot_entries]
                discipline = slot_entries[0].get("discipline", "singles")
                self._append(
                    "competition_slot",
                    key,
                    {
                        "policy_version": submitted["frozen_policy_version"],
                        "slot_ref": slot_ref,
                        "event_ref": event_ref,
                        "proposal_ref": proposal_id,
                        "athlete_id": athlete_ids[0],
                        "athlete_ids": athlete_ids,
                        "discipline": discipline,
                        "opponent_style": slot_entries[0].get("opponent_style"),
                    },
                    event_type="SLOT_AWARDED",
                )
                awarded.append(slot_ref)
        return {"proposal_id": proposal_id, "decision": decision, "awarded_slots": awarded}

    # ---- 出场、释放与复盘 ------------------------------------------------------

    def release_slot(self, event_ref: str, slot_ref: str, reason: str) -> None:
        """退赛或伤病：只能释放尚未使用的机会。"""
        if reason not in ("withdrawal", "injury", "scratched"):
            raise ServiceError("bad_release_reason", "释放原因必须是 withdrawal/injury/scratched")
        key = _slot_key(event_ref, slot_ref)
        slot = self.state.slots.get(key)
        if slot is None or slot.status not in ("awarded", "released"):
            if slot is not None and slot.status == "used":
                raise ServiceError(
                    "appearance_locked", "该报名位已出场，既往出场不能因退赛撤回"
                )
            raise ServiceError("slot_not_awarded", "名额尚未授予，无需释放")
        self._append(
            "competition_slot",
            key,
            {"slot_ref": slot_ref, "event_ref": event_ref, "reason": reason},
            event_type="SLOT_RELEASED",
        )

    def record_appearance(
        self,
        event_ref: str,
        slot_ref: str,
        result: str,
        score_line: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """记录实际出场与结果。写入即不可变：重放同一 event_id 幂等，
        任何改写尝试都会被服务拒绝。"""
        if result not in ("win", "loss", "walkover"):
            raise ServiceError("bad_result", "结果必须是 win/loss/walkover")
        key = _slot_key(event_ref, slot_ref)
        slot = self.state.slots.get(key)
        if slot is None:
            raise ServiceError("unknown_slot", "报名位不存在")
        if slot.status == "used":
            if event_id and slot.appearance and slot.appearance.get("result") == result:
                return  # 重放幂等
            raise ServiceError(
                "appearance_locked", "出场记录已冻结，后来评级不能覆盖既往结果"
            )
        if slot.status != "awarded":
            raise ServiceError("slot_not_awarded", "名额未授予或已释放，不能记出场")
        payload: dict[str, Any] = {
            "slot_ref": slot_ref,
            "event_ref": event_ref,
            "athlete_id": slot.athlete_id,
            "result": result,
        }
        if score_line:
            payload["score_line"] = score_line
        self._append(
            "competition_slot",
            key,
            payload,
            event_type="APPEARANCE_RECORDED",
            event_id=event_id,
        )

    def publish_review(
        self,
        review_id: str,
        gap_changes: Sequence[dict[str, Any]],
        evidence_cutoff: datetime | str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """赛后复盘：gap_changes 为增量（delta 可正可负），不做整表覆盖。"""
        if not gap_changes:
            raise ServiceError("empty_review", "复盘至少包含一条培养缺口变化")
        for change in gap_changes:
            for field_name in ("athlete_id", "gap_id"):
                if field_name not in change:
                    raise ServiceError("bad_gap_change", f"缺口变化缺少 {field_name}")
            self._require_athlete(change["athlete_id"])
        payload: dict[str, Any] = {
            "evidence_cutoff": _iso(evidence_cutoff or self.clock.current),
            "gap_changes": to_jsonable(gap_changes),
        }
        if summary:
            payload["summary"] = summary

        before = {
            aid: {gid: g["severity"] for gid, g in self.state.athletes[aid].gaps.items()}
            for aid in {c["athlete_id"] for c in gap_changes}
            if aid in self.state.athletes
        }
        self._append(
            "development_gap",
            review_id,
            payload,
            event_type="REVIEW_PUBLISHED",
        )
        diff = []
        for change in gap_changes:
            aid = change["athlete_id"]
            gid = change["gap_id"]
            after = self.state.athletes[aid].gaps.get(gid, {}).get("severity")
            diff.append(
                {
                    "athlete_id": aid,
                    "gap_id": gid,
                    "label": change.get("label"),
                    "delta": change.get("delta", 0.0),
                    "severity_before": before.get(aid, {}).get(gid, 0.0),
                    "severity_after": after,
                    "evidence": change.get("evidence"),
                }
            )
        return {"review_id": review_id, "changes": diff}

    # ---- 查询与解释 -----------------------------------------------------------

    def frozen_evaluation(self, proposal_id: str) -> Evaluation:
        proposal = self.state.proposals.get(proposal_id)
        if proposal is None:
            raise ServiceError("unknown_proposal", f"方案不存在: {proposal_id}")
        return evaluate_snapshot(proposal.submitted["snapshot"])

    def explain(self, proposal_id: str, athlete_id: str) -> dict[str, Any]:
        return explain(self.frozen_evaluation(proposal_id), athlete_id)

    def executable_roster(self, event_ref: str, proposal_id: str | None = None) -> dict[str, Any]:
        """给出可执行阵容：以已批准并仍处于 awarded 的名额为准。"""
        slots = [
            s
            for s in self.state.slots.values()
            if s.event_ref == event_ref
            and (proposal_id is None or s.proposal_ref == proposal_id)
            and s.status in ("awarded", "used")
        ]
        entries = sorted(slots, key=lambda s: s.slot_ref)
        return {
            "event_ref": event_ref,
            "proposal_ref": proposal_id,
            "roster": [
                {
                    "slot_ref": s.slot_ref,
                    "athlete_id": s.athlete_id,
                    "athlete_ids": list(s.athlete_ids or [s.athlete_id]),
                    "discipline": s.discipline,
                    "status": s.status,
                    "result": (s.appearance or {}).get("result") if s.appearance else None,
                }
                for s in entries
            ],
        }

    def gap_report(self, athlete_id: str) -> dict[str, Any]:
        self._require_athlete(athlete_id)
        athlete = self.state.athletes[athlete_id]
        return {
            "athlete_id": athlete_id,
            "gaps": sorted(athlete.gaps.values(), key=lambda g: -g.get("severity", 0.0)),
        }

    def evidence_report(self, event_ref: str | None = None) -> dict[str, Any]:
        """列出仍缺少足够证据的判断：对手交锋样本、近期教练观察。"""
        policy_version = self.latest_policy_version()
        rules = next(p for p in self.state.policies.values() if p["policy_version"] == policy_version)["rules"]
        min_matchup = int(rules.get("min_matchup_sample", 1))
        min_obs = int(rules.get("min_observations", 0))
        styles = None
        if event_ref:
            styles = {s.get("opponent_style") for s in self.state.events[event_ref]["slots"]}
            styles.discard(None)
        insufficient: list[dict[str, Any]] = []
        for aid, athlete in self.state.athletes.items():
            matchups = athlete.profile.get("opponent_matchups", {})
            targets = styles or set(matchups)
            thin = [
                {"opponent_style": style, "sample": int(matchups.get(style, {}).get("sample", 0))}
                for style in sorted(t for t in targets if t)
                if int(matchups.get(style, {}).get("sample", 0)) < min_matchup
            ]
            obs_count = len(athlete.observations)
            if thin or obs_count < min_obs:
                insufficient.append(
                    {
                        "athlete_id": aid,
                        "thin_matchups": thin,
                        "observations": obs_count,
                        "observations_required": min_obs,
                    }
                )
        return {"policy_version": policy_version, "insufficient_evidence": insufficient}

    # ---- 辅助 -----------------------------------------------------------------

    def _require_athlete(self, athlete_id: str) -> None:
        if athlete_id not in self.state.athletes:
            raise ServiceError("unknown_athlete", f"运动员资料不存在: {athlete_id}")

    def _claimed_slots(self, event_ref: str) -> dict[str, str]:
        claimed: dict[str, str] = {}
        for proposal in self.state.proposals.values():
            if proposal.status != "submitted" or not proposal.submitted:
                continue
            if proposal.submitted["event_ref"] != event_ref:
                continue
            for entry in proposal.submitted["entries"]:
                claimed[_slot_key(event_ref, entry["slot_ref"])] = proposal.proposal_id
        return claimed


def payload_event_type(aggregate_type: str) -> str:
    fallback = {
        "clock": "CLOCK_ADVANCED",
    }
    if aggregate_type in fallback:
        return fallback[aggregate_type]
    raise ValueError(f"需要显式 event_type: {aggregate_type}")


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return now_iso(value)
    parse_iso(value)  # 校验带时区
    return value
