# 领域约定

表达梯队能力证据、比赛名额、组合约束、培养目标、健康许可、回避审批与赛后复盘的基础事实。所有状态都由只追加事件流归约得到，进程重启后整段重放即可恢复。

## 聚合与事件

聚合：`athlete_profile`、`competition_slot`、`lineup_proposal`、`development_gap`、`selection_policy`、`competition_event`、`simulation_clock`。

| 事件 | 含义 |
| --- | --- |
| `PROFILE_VERSIONED` | 技术画像版本化（`profile`），新版本不覆盖历史出场与复盘 |
| `CLEARANCE_RECORDED` | 健康许可，需 `valid_until`、`scope`（`all`/`singles`/`team`），可带 `review_at` 康复复查时刻 |
| `LOAD_RECORDED` | 近期负荷指数（`load_index`、`as_of`） |
| `COACH_ASSIGNED` | 运动员的日常主管教练（`coach_id`），用于批准回避 |
| `POLICY_VERSIONED` | 选拔政策版本（`ruleset`：权重、短长期配比、负荷上限、证据阈值） |
| `EVENT_REGISTERED` | 赛事与报名位（级别、报名截止、结束时间、`slots`：单打/双打/团体关键盘、容量、scope、对手打法、许可搭档组合） |
| `PROPOSAL_SUBMITTED` | 方案提交：载荷冻结 `policy_version`、`data_snapshot`（画像/许可/负荷/出场/已占名额及各自版本）与求出的 `lineup` |
| `OBSERVATION_SUBMITTED` | 日常教练可提交的观察，但不构成批准 |
| `PROPOSAL_APPROVED` / `PROPOSAL_REJECTED` | 批准人列表必须包含非主管教练；拒绝需理由 |
| `SLOT_AWARDED` / `SLOT_CONSUMED` / `SLOT_RELEASED` | 名额生命周期：授予 → 出场消耗；退赛/伤病只允许把 awarded 释放，consumed 永不改写 |
| `WITHDRAWAL_RECORDED` | 退赛或伤病，带 `effective_from`；未来生效的在时钟推进到点后释放名额 |
| `LINEUP_FROZEN` | 批准后冻结最终阵容，与方案中的求解结果一致 |
| `CLOCK_ADVANCED` | 模拟时钟只进不退，重启从该事件恢复当前时刻 |
| `REVIEW_PUBLISHED` | 赛后复盘，需 `evidence_cutoff` 与 `gap_changes`（按 队员×缺口 追加最新判断，历史复盘保留） |

所有发生时间必须携带时区；版本号在每个聚合内从 1 连续递增；基础校验不改写调用方输入。

## 关键语义

- **冻结**：方案只引用提交时刻的数据快照与政策版本；求解器是纯函数，重放同一快照逐位返回原阵容。
- **回避**：入选队员的日常主管教练可以提交观察，但批准人列表中至少有一名非主管教练，否则拒绝。
- **并发隔离**：批准时校验各报名位的冻结基线版本；两个方案不能重复占用同一报名位。相同 `event_id` 内容不同直接报冲突隔离；不同赛事即使报名位标识相同，聚合标识也按赛事前缀隔离。
- **约束同时满足**：个人资格（许可 scope、个人负荷、退赛生效、证据硬门槛）、组合约束（双打/关键盘仅取许可搭档）、全队负荷上限、报名位唯一性。
- **释放语义**：退赛/伤病仅释放 awarded 未 consumed 的名额；既往出场与结果不可变，后续画像评级和复盘不覆盖历史。
- **模拟时间**：可推进到报名截止、康复复查、赛事结束；`pending()` 返回待处理方案与待到达里程碑，重启后继续。

上层服务负责业务幂等、冲突隔离与状态推进；本仓库的契约层只定义可稳定交换的基础事实。
