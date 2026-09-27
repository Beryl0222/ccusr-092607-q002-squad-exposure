"""端到端演示：男团决赛后的梯队实战机会配置。

运行：
    PYTHONPATH=src python3 examples/scenario.py

脚本使用临时事件库，完整走一遍：冻结选拔 → 教练回避 → 批准占用 →
释放未使用名额 → 出场失利 → 复盘培养缺口 → 解释与证据报告 → 重启续办。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures_support import (  # noqa: E402
    A1,
    A2,
    A3,
    A4,
    COACH_A,
    DIRECTOR,
    EVENT,
    build_service,
    reopen,
)
from squad_exposure.service import ServiceError  # noqa: E402


def show(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="squad-demo-"))
    svc, _ = build_service(tmp)

    # 1) 康复中的陈屿：方案可提交，但硬约束暂缓，解释给出原因。
    held = svc.submit_proposal(
        EVENT, [{"slot_ref": "MS1", "athlete_id": A2, "discipline": "singles"}],
        proposal_id="demo-hold",
    )
    show("暂缓：康复复查未到", svc.explain("demo-hold", A2)["entries"][0]["reasons"])
    # 不可行方案由主管驳回，报名位解除在途占用（机会尚未授予，不产生释放事件）。
    svc.decide_proposal("demo-hold", "rejected", DIRECTOR, rationale="等待康复复查")

    # 2) 林越单打 + 王澈/赵启双打：可行方案。
    entries = [
        {"slot_ref": "MS1", "athlete_id": A1, "discipline": "singles"},
        {"slot_ref": "MD1", "athlete_id": A3, "partner_id": A4, "discipline": "doubles"},
        {"slot_ref": "MD1", "athlete_id": A4, "partner_id": A3, "discipline": "doubles"},
    ]
    plan = svc.submit_proposal(EVENT, entries, proposal_id="demo-final")
    show("冻结裁决（指纹与评分）", {
        "feasible": plan["evaluation"]["feasible"],
        "fingerprint": plan["evaluation"]["fingerprint"],
        "team_load_minutes": plan["evaluation"]["team_load_minutes"],
        "selected": plan["evaluation"]["selected"],
    })

    # 3) 日常带训教练不能批准自己的队员。
    try:
        svc.decide_proposal("demo-final", "approved", COACH_A)
    except ServiceError as exc:
        show("教练回避被拒", {"code": exc.code, "message": exc.message})

    # 4) 并发方案不能重复占用同一报名位。
    try:
        svc.submit_proposal(
            EVENT, [{"slot_ref": "MS1", "athlete_id": A3, "discipline": "singles"}],
            proposal_id="demo-race",
        )
    except ServiceError as exc:
        show("并发占用被拒", {"code": exc.code, "message": exc.message})

    # 5) 项目主管批准，名额落位。
    svc.decide_proposal("demo-final", "approved", DIRECTOR, rationale="兼顾成绩与双打厚度")
    show("可执行阵容", svc.executable_roster(EVENT, "demo-final"))

    # 6) 推进到赛事结束；林越负于进攻型对手。
    svc.advance_to_event_end(EVENT)
    svc.record_appearance(EVENT, "MS1", "loss", score_line="1-2", event_id="demo-app-1")

    # 7) 出场后不能再因伤病释放该位。
    try:
        svc.release_slot(EVENT, "MS1", "injury")
    except ServiceError as exc:
        show("已出场名额拒绝释放", {"code": exc.code})

    # 8) 复盘：一次失利抬高的培养缺口（前后对比）。
    review = svc.publish_review(
        "demo-review",
        [{
            "athlete_id": A1,
            "gap_id": "gap-attacker-clutch",
            "label": "对进攻型关键分把握",
            "delta": 0.5,
            "evidence": "决胜局后段连续失误",
        }],
        summary="男团决赛复盘",
    )
    show("失利改变的培养缺口", review["changes"])

    # 9) 仍缺少足够证据的判断。
    show("证据不足清单", svc.evidence_report(EVENT))

    # 10) 重启：待办与时钟全部由事件流还原。
    revived = reopen(tmp)
    ms1 = next(
        row for row in revived.executable_roster(EVENT, "demo-final")["roster"]
        if row["slot_ref"] == "MS1"
    )
    show("重启后状态", {
        "now": revived.now,
        "pending": revived.pending_decisions(),
        "immutable_result": ms1["result"],
    })


if __name__ == "__main__":
    main()
