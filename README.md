# 安全分类词表版本迁移校验器

为内部安全分类词表的版本迁移提供 **plan（求最优映射）/ audit（核对既定方案）/
update（SQLite 原子切换）** 三个命令。纯 Python 3.12 标准库实现，CPU 运行，
无第三方依赖；所有夹具均为本地虚构数据。

- 运行环境：Windows 原生 Python 3.12（在 3.12.8 验证），同样兼容 Linux/macOS。
- 命令形态：`python -m solution <命令> CASE.json [--db DB.sqlite]`
- 标准输出：UTF-8 JSON；同一逻辑输入永远产生同一结果（键名稳定、列表有序）。
- 结构/业务错误：输出 `{"error": {code, message, path}}`，退出码 `2`；
  正常结果退出码 `0`；`audit` 发现内容违规时输出完整反例报告并以退出码 `2` 结束。

---

## 1. 领域模型与约束

| 概念 | 定义 |
|---|---|
| 原子敏感类别 `atoms` | 不可再分的最小类别，全词表共享一张位表 |
| 标签 `old_labels` / `new_labels` | 标签对应一个**无重复原子的集合**；标签之间允许原子重叠 |
| 候选映射 `mappings[].candidates` | 每个旧标签必须从至多 3 个候选中**选且仅选一个**；候选是一组新标签 |
| 对象 `objects` | 对象挂若干旧标签；其**原有效敏感集合 = 所挂旧标签原子集合的并集** |
| 角色 `roles` | 声明可读原子集合；**仅当对象有效分类是授权集合的子集时允许读取** |
| 计划 `plan` | 对每个旧标签选定一个候选 ID，含有效期 `valid_until` |

迁移后对象 `o` 的有效分类：

```
C(o) = ⋃ (所选候选引用的新标签原子集合)
```

### 两条硬约束

1. **不漏标（安全）**：`old_mask(o) ⊆ C(o)`，即旧有效集合中的每个原子在迁移后
   仍然存在。
2. **角色兼容**：对输入中登记的**每一个**角色 `r` 与**每一个**对象 `o`，
   读取允许结论不得改变：

   ```
   old_mask(o) ⊆ auth(r)  ⇔  C(o) ⊆ auth(r)
   ```

   令 `Cap(o) = ⋂{ auth(r) : old_mask(o) ⊆ auth(r) }`（当前能读该对象的角色
   授权集合的交集；没有任何角色能读时取全集，该对象不产生兼容约束），则兼容
   约束等价于 `C(o) ⊆ Cap(o)`。

允许超标（`C(o)` 比旧集合多出原子），但要在满足两条硬约束的前提下最小化代价。

### 优化目标（严格字典序）

1. **总额外类别数**：`Σ_o |C(o) \ old_mask(o)|` 最小。
   同一额外原子在同一对象上**只计一次**——掩码并集天然去重，例如两个旧标签
   的候选都把原子 `X` 引入同一对象，`X` 在该对象上只贡献 1 次代价。
2. **所有映射使用的新标签个数之和**最小；同一新标签被不同旧标签的映射使用时
   **分别计数**（即各候选 `new_labels` 长度之和）。
3. 仍持平时，旧标签按 ID 排序，逐位比较所选候选的**新标签 ID 有序元组**
   （“先按旧标签 ID、再按新标签 ID”决胜）；再持平用候选 ID 兜底，保证结果唯一。

**一个旧标签在所有对象上使用同一个候选**，不允许逐对象选择——这是全局组合
优化而不是逐对象独立决策。

---

## 2. 掩码复用与剪枝（实现要点）

解析阶段把每个原子类别按 `atoms` 顺序映射到一个 bit，标签、对象、角色全部
压到**同一张位表**的 Python 大整数上：

| 集合运算 | 掩码实现 |
|---|---|
| 标签并集（对象有效分类） | `a | b` |
| 子集判定（角色允许） | `(class_mask & ~auth_mask) == 0` |
| 超标原子 | `(new_mask & ~old_mask)`，`int.bit_count()` 计数 |
| 漏标判定 | `(old_mask & ~new_mask) != 0` |

求解器（`solution/planner.py`）是按旧标签 ID 排序推进的**前向 DP + 剪枝**：

- **兼容掩码预筛**：候选原子一旦超出任一相关对象的 `Cap(o)`，因为覆盖只增不减，
  该候选在任何前缀下都非法，直接剔除。
- **后缀覆盖剪枝**：预计算 `suffix_cover[k]`（剩余标签全部取最广候选的并集）；
  当前覆盖加上后缀仍补不齐某对象漏标时，状态立即淘汰。
- **严格支配剪枝（状态合并）**：DP 状态以“每对象覆盖掩码元组”为键。同键的两条
  路径仅当 `(额外代价, 新标签宽度)` 被**严格**占优才丢弃；代价/宽度相同的路径
  全部保留到终局——因为第三目标只能在完整选择向量上比较，中途按字典序合并会
  误杀全局最优解。终局再按完整三级目标取唯一最优。
- **增量代价**：每个对象的超标计数随覆盖掩码增量维护，无需保存原子列表。

规模上限 16 个旧标签 × 每标签至多 3 候选（朴素枚举 3^16 ≈ 4.3×10⁷），实测
真实分布用例秒级完成；求解器的正确性由 400 组随机用例与暴力枚举逐案比对
（含三级目标的完整决胜）保证，脚本见 `.runtime-tmp/fuzz_planner.py`。

---

## 3. 输入 CASE.json 定义

顶层字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `case_id` | string | 否（默认 `"default"`） | 用例/库内主键 |
| `now` | 非负整数 | 是 | 当前逻辑时钟，用于计划过期判定 |
| `old_version` / `new_version` | string | 否（默认 `v1`/`v2`） | 词表版本号 |
| `atoms` | string[] | 是 | 原子类别 ID，非空、不可重复 |
| `old_labels` | object[] | 是 | 1–16 个，元素 `{id, atoms[]}`，`atoms` 非空无重复 |
| `new_labels` | object[] | 是 | 元素结构同上，标签间允许原子重叠 |
| `mappings` | object[] | 是 | 每个旧标签恰好一条，1–3 个候选 |
| `objects` | object[] | 是 | 元素 `{id, labels[]}`，引用已登记旧标签 |
| `roles` | object[] | 是 | 至少一个，元素 `{id, authorized_atoms[]}`，授权集可为空 |
| `plan` | object | audit/update 必填 | 见下 |

`mappings[]`：`{ "old_label": <旧标签ID>, "candidates": [ {"id", "new_labels[]"} ... ] }`

`plan`：

```json
{
  "plan_id": "plan-demo-1",
  "created_at": 900,
  "valid_until": 2000,
  "selection": [ {"old_label": "L1", "candidate_id": "L1-m1"} ]
}
```

`selection` 必须覆盖全部旧标签，且每个 `candidate_id` 必须是该旧标签名下登记
的候选。`now > valid_until` 即视为过期。

### 结构错误码（均带 JSONPath 风格 `path`）

| code | 触发条件 |
|---|---|
| `E_TYPE` | 字段类型错误 |
| `E_MISSING` | 必填字段缺失 / 计划未覆盖全部旧标签 |
| `E_UNKNOWN` | 出现未声明字段 |
| `E_EMPTY` | 集合/字符串为空但不允许 |
| `E_DUPLICATE` | 唯一键或集合内元素重复 |
| `E_REF` | 引用了未登记的标签/原子/候选 |
| `E_RANGE` | 候选数 > 3、旧标签 > 16、时间倒挂等 |
| `E_JSON` / `E_IO` | JSON 语法错误（带行列号）/ 文件不存在 |
| `E_INFEASIBLE` | plan 不存在满足硬约束的组合（`path=$.mappings`） |
| `E_PLAN_EXPIRED` | 计划过期（`path=$.plan.valid_until`） |
| `E_PLAN_NOT_FOUND` | update 前未通过 `plan --db` 登记 |
| `E_DB_STATE` | 输入与库内快照哈希漂移，拒绝写入 |

错误信封示例：

```json
{
  "error": {
    "code": "E_INFEASIBLE",
    "message": "不存在满足不漏标与角色兼容性的候选组合",
    "path": "$.mappings"
  }
}
```

---

## 4. 输出定义

### 4.1 `plan`

```json
{
  "command": "plan",
  "case_id": "demo-migration",
  "status": "optimal",
  "objective": { "total_extra_atoms": 1, "total_new_labels": 4 },
  "selection": [ {"old_label": "L1", "candidate_id": "L1-m1"} ],
  "objects": [
    {
      "object": "obj-3",
      "old_atoms": ["B", "D"],
      "new_atoms": ["A", "B", "D"],
      "extra_atoms": ["A"],
      "extra_count": 1,
      "missing_atoms": [],
      "candidate_chain": ["L2-m1", "L4-m1"],
      "role_decisions": [
        {"role": "r-admin", "old_allow": true, "new_allow": true, "unchanged": true},
        {"role": "r-analyst", "old_allow": false, "new_allow": false, "unchanged": true}
      ]
    }
  ],
  "search_stats": {
    "nodes_visited": 11,
    "pruned_by_cover": 0,
    "pruned_by_role_capacity": 0,
    "pruned_by_bound": 1
  }
}
```

带 `--db` 时额外返回 `persistence`（登记结果 + 输入规范化哈希）。

### 4.2 `audit`

```json
{
  "command": "audit",
  "case_id": "demo-migration",
  "ok": true,
  "violation_count": 0,
  "minimal_counterexample": null,
  "all_violations": [],
  "total_extra_atoms": 1,
  "per_object": [ { "object": "...", "missing_atoms": [], "extra_atoms": [],
                    "extra_count": 0, "cap_atoms": [], "candidate_chain": [],
                    "role_decisions": [] } ]
}
```

违规时 `ok=false`，`minimal_counterexample` 是按对象 ID、角色 ID 排序后的
**第一个最小反例**，两类：

- `underlabel`：`{kind, object, missing_atoms, path}`；
- `role_decision_changed`：`{kind, object, role, old_allow, new_allow,
  responsible_atoms, path}`，其中 `responsible_atoms` 是新分类中落在角色
  授权集合之外、直接导致结论翻转的原子。

计划过期优先于内容检查，直接返回 `E_PLAN_EXPIRED` 错误信封。

### 4.3 `update --db`

```json
{
  "command": "update",
  "case_id": "demo-migration",
  "switch": {
    "case_id": "demo-migration",
    "plan_id": "plan-demo-1",
    "switched": true,
    "idempotent_replay": false,
    "current_object_version": 2,
    "current_vocab_version": "tax-v2",
    "old_vocab_version_retained": "tax-v1",
    "historical_decisions_kept": 8,
    "new_decisions_written": 8
  },
  "object_classifications": [ {"object": "obj-1",
      "new_vocab_version": "tax-v2", "new_atoms": ["A"]} ],
  "decisions_on_disk": 16
}
```

---

## 5. SQLite 持久化与原子切换

库表（`solution/storage.py`，`schema_version=1`）：

- `cases`：输入规范化 JSON 与 SHA-256 哈希，检测快照漂移；
- `vocab_versions` / `label_atoms`：版本化词表与标签原子；
- `object_versions`：每个对象在每个词表版本下的分类与对象版本号；
- `current_object_version`：当前指针（对象版本 + 词表版本）；
- `role_auths`：角色授权集合；
- `plans`：计划登记（`registered/committed/expired`）与选择；
- `decisions`：**只增不改**的历史判定（对象版本 × 角色 × 词表版本）。

`update` 在单个 `BEGIN IMMEDIATE` 事务内完成：校验计划与快照 → 写入新版本
对象分类与逐角色判定 → 翻转当前指针 → 计划置 `committed`。进程在事务中途
崩溃时，SQLite rollback journal 自动回滚到上一个一致状态；重新执行同一命令
走幂等重放路径（`idempotent_replay=true`），不会重复写入、不改变对象版本号。
旧版本的对象分类与历史判定行永远保留。

---

## 6. 复现命令

```bash
# 仓库根目录下
python -m solution plan  examples/demo_case.json
python -m solution audit examples/demo_case.json
python -m solution plan  examples/demo_case.json --db state.sqlite
python -m solution update examples/demo_case.json --db state.sqlite
python -m solution update examples/demo_case.json --db state.sqlite   # 幂等重放

# 无解 / 过期
python -m solution plan  examples/infeasible_case.json                # 退出码 2
python -m solution audit examples/expired_plan_case.json              # 退出码 2

# 一键复现（plan→audit→update→重放→错误用例→测试）
bash run_examples.sh

# 单元测试（40 个断言用例）
python -m unittest discover -s tests -v
```

样例文件：

| 文件 | 场景 |
|---|---|
| `examples/demo_case.json` | 可行迁移：含改名、合并标签与一次**被迫超标**（obj-3 被强制引入 A，总代价 1） |
| `examples/infeasible_case.json` | 唯一候选必然引入角色未授权原子 → 无解 |
| `examples/expired_plan_case.json` | `now > valid_until` → 过期计划 |

---

## 7. 目录结构

```
solution/
  __init__.py
  __main__.py    # python -m solution 转发
  cli.py         # 命令行、JSON 信封、退出码
  errors.py      # 错误码与可定位 ValidationError
  models.py      # CASE.json 结构校验 + 位掩码数据模型
  planner.py     # 前向 DP + 掩码/支配剪枝求解器
  audit.py       # 逐对象逐角色核对与最小反例
  storage.py     # SQLite schema、事务、原子切换、历史保留
tests/           # unittest 用例（40 个），可用 discover 直接运行
examples/        # 本地虚构夹具
run_examples.sh  # 端到端复现脚本
```

## 8. 测试覆盖要点

- 两个旧标签把同一额外原子引入同一对象时，该原子只计一次代价；
- 角色未授权导致额外原子无法加入（候选被剪枝 → 无解），以及纯漏标无解；
- 改名但语义完全一致：零额外代价、零角色结论变化；
- 三级优化目标分别有独立用例（含决胜不依赖候选登记顺序）；
- 无解诊断、过期计划、未登记计划、输入快照漂移；
- update 原子切换、历史判定保留、模拟事务回滚、故障后幂等重启；
- 结构错误码与 JSONPath 字段定位、UTF-8 JSON 输出确定性。
