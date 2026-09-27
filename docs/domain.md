# 领域约定

表达梯队能力证据、比赛名额、组合约束和培养目标的基础事实。本仓库在事件信封契约之上，
另提供只追加事件存储、重放投影、可推进模拟时钟与选拔应用服务（见 `docs/service.md`）。

## 聚合与事件

聚合：

- `athlete_profile`：技术画像、健康许可、教练观察、培养缺口。
- `policy`：按版本冻结的选拔政策（负荷上限、样本阈值、长短期权重）。
- `competition_event`：赛事级别、报名截止、赛事结束、报名位与全队负荷上限。
- `lineup_proposal`：一次阵容提案，携带冻结数据快照与裁决结果。
- `competition_slot`：单个报名位的授予、释放、出场状态。
- `development_gap`：赛后复盘流，按增量改写培养缺口。
- `clock`：模拟时钟，推进动作本身也是事实。

事件类型：`PROFILE_VERSIONED`、`CLEARANCE_RECORDED`、`OBSERVATION_RECORDED`、
`POLICY_PUBLISHED`、`EVENT_REGISTERED`、`PROPOSAL_SUBMITTED`、`DECISION_RECORDED`、
`SLOT_AWARDED`、`SLOT_RELEASED`、`APPEARANCE_RECORDED`、`REVIEW_PUBLISHED`、`CLOCK_ADVANCED`。

所有发生时间都必须携带时区，聚合版本号从 1 开始严格递增，基础校验不会改写调用方输入。

## 事件载荷

- `CLEARANCE_RECORDED`：`valid_until`、`scope`；可附 `status`（cleared/restricted/rehab）、`review_at`。
- `OBSERVATION_RECORDED`：`coach_id`、`observed_at`、观察内容。
- `POLICY_PUBLISHED`：`policy_version`、`rules`。
- `EVENT_REGISTERED`：`event_level`、`entry_deadline`、`slots`，可附 `competition_end`、`team_load_cap`。
- `PROPOSAL_SUBMITTED`：`event_ref`、`frozen_policy_version`、`entries`，服务实现另冻结 `snapshot` 与 `evaluation`。
- `DECISION_RECORDED`：`decision`（approved/rejected）、`decided_by`、冻结指纹。
- `SLOT_AWARDED`：`policy_version`、`slot_ref`，另含 `event_ref`、`proposal_ref`、出场队员。
- `SLOT_RELEASED`：`slot_ref`、`reason`（withdrawal/injury/scratched）。
- `APPEARANCE_RECORDED`：`slot_ref`、`result`（win/loss/walkover）。
- `REVIEW_PUBLISHED`：`evidence_cutoff`、`gap_changes`（增量 delta，非整表覆盖）。
- `CLOCK_ADVANCED`：`simulated_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由 `squad_exposure` 服务负责；契约层只定义
可稳定交换的基础事实。
