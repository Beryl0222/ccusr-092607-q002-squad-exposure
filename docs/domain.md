# 领域约定

表达梯队能力证据、比赛名额、组合约束和培养目标的基础事件。

聚合对象包括`athlete_profile`、`competition_slot`、`lineup_proposal`、`development_gap`。事件类型包括`PROFILE_VERSIONED`、`CLEARANCE_RECORDED`、`PROPOSAL_SUBMITTED`、`SLOT_AWARDED`、`REVIEW_PUBLISHED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `CLEARANCE_RECORDED`：载荷还需包含 `valid_until`, `scope`。
- `SLOT_AWARDED`：载荷还需包含 `policy_version`, `slot_ref`。
- `REVIEW_PUBLISHED`：载荷还需包含 `evidence_cutoff`, `gap_changes`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
