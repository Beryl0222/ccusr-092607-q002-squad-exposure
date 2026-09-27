# 应用服务语义

`SquadExposureService` 只通过追加事件工作，自身不持有可变状态：重启时重放事件流即可
恢复时钟、待批提案与名额占用。

## 时间线推进

- 服务不读系统时间，所有"现在"来自可推进的模拟时钟（`clock.py`）。
- `advance_clock`、`advance_to_deadline`、`advance_to_rehab_review`、`advance_to_event_end`
  把日期推到报名截止、康复复查或赛事结束；时钟只能前进，每次推进落 `CLOCK_ADVANCED`。

## 冻结选拔

- `submit_proposal` 在提交瞬间冻结：采用的政策版本、赛事定义、相关队员画像/许可/观察
  的当前内容，以及纯函数裁决结果与快照指纹（sha256 前 16 位）。
- 审批时用冻结快照重新裁决并核对指纹：重放同一决定必然得到同一结果；之后发布的新政策、
  新画像版本都不影响既有方案。
- 晚于报名截止的提交被拒绝。

## 资格硬约束与软信号

硬约束（不满足则条目暂缓、方案不可批准）：许可状态与 scope、许可有效期覆盖赛事结束、
康复复查未到、个人近期负荷叠加赛程上限、双打互相点名且画像互登记且共同样本达标、
报名位存在且方案内不重复、全队总负荷上限。

软信号（不阻断，进入解释与证据报告）：对指定对手打法的交锋样本不足、近期教练观察不足。

评分按冻结政策中的权重组合短期成绩价值（含样本置信度）与长期培养价值（对应该对手类型
的培养缺口严重度）。

## 审批回避

- 日常带训教练可以提交 `OBSERVATION_RECORDED`，但不能批准任何一名其日常负责队员的名额
  （`coach_conflict`）。
- 决定为终局：已决方案不可再改；不可行方案不能批准。

## 名额生命周期与不可变历史

```
open ──(方案在途占用，未授予)──▶ open
open ──approved──▶ awarded ──record_appearance──▶ used   (永久)
                   awarded ──release(withdrawal/injury)──▶ released ──▶ 可被新方案再次申请
```

- 在途方案占用报名位，并发方案不得重复占用同一报名位。
- 退赛或伤病只释放**尚未使用**的机会；已出场（used）的释放请求被拒绝。
- 出场与结果一经记录即冻结，后来的画像升版/评级调整不覆盖历史结果。

## 复盘与培养缺口

- `REVIEW_PUBLISHED` 的 `gap_changes` 是增量（delta 可正可负），投影逐次累加，
  并保留证据笔记与证据截止时间；一次失利如何改变培养缺口可直接看前后严重度对比。
- `gap_report` 给出队员当前缺口；`evidence_report` 列出对手样本与观察仍不足的判断。

## 存储保证

- `event_id` 相同且内容相同：幂等返回，不重复落库。
- `event_id` 相同但内容不同：抛出 `EventConflict`，输入隔离到 quarantine 目录，
  已提交事件流不受污染（"标识相同而阵容内容不同则隔离"）。
- 同一聚合版本号必须严格 +1，防止并发覆盖。

## 主要 API

```python
svc.register_policy(policy_id, version, rules)
svc.register_event(event_ref, level, deadline, end, slots, team_load_cap)
svc.version_profile(athlete_id, **profile)
svc.record_clearance(athlete_id, status, valid_until, scope, review_at)
svc.record_observation(athlete_id, coach_id, note)
svc.submit_proposal(event_ref, entries, policy_version=None)   # 返回冻结裁决
svc.pending_decisions()                                       # 重启后续办
svc.decide_proposal(proposal_id, "approved"|"rejected", decided_by)
svc.release_slot(event_ref, slot_ref, "injury"|"withdrawal"|"scratched")
svc.record_appearance(event_ref, slot_ref, "win"|"loss"|"walkover")
svc.publish_review(review_id, gap_changes)
svc.explain(proposal_id, athlete_id)      # 为何入选/暂缓
svc.executable_roster(event_ref, proposal_id)
svc.gap_report(athlete_id)
svc.evidence_report(event_ref)
```
