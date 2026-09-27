# 梯队实战机会配置簿

把运动员健康许可、技术画像、近期负荷、对手类型、搭档组合、赛事级别、报名名额、
教练回避、培养目标与赛后复盘按版本保存，并在选拔瞬间冻结所采用的数据与政策。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/squad_exposure/`
  - `contracts.py`：基础契约校验。
  - `store.py`：只追加 JSONL 事件存储（event_id 幂等、冲突隔离、聚合版本）。
  - `clock.py`：可推进到报名截止/康复复查/赛事结束的模拟时钟。
  - `projection.py`：事件流重放读模型（出场不可变、名额状态、缺口累积）。
  - `selection.py`：纯函数资格裁决、评分与入选/暂缓解释（基于冻结快照）。
  - `service.py`：应用服务——提案冻结、教练回避审批、名额授予/释放、复盘、重启续办。
- `examples/scenario.py`：端到端演示。
- `tests/`：契约、存储/时钟、选拔工作流、复盘解释共 34 个用例。
- `docs/domain.md`、`docs/service.md`：领域对象、事件与服务语义。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m squad_exposure.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 端到端演示

```bash
PYTHONPATH=src python3 examples/scenario.py
```

演示覆盖：康复队员暂缓与解释、冻结指纹、日常带训教练审批被拒、并发报名位占用被拒、
批准授予、已出场名额拒绝释放、失利后的培养缺口增量、证据不足清单，以及重启后续办。
