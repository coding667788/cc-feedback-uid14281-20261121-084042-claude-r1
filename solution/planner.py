"""plan 命令：掩码 + 支配剪枝的前向 DP 求解器。

问题形式化（符号与题目一致）
============================
* 每个原子敏感类别占 1 个 bit；标签/对象/角色的集合全部用同一张位表的整数掩码
  表示，于是：

  - 标签并集（对象有效分类）       ``A | B``
  - 子集判定（角色允许读取）       ``(class_mask & ~auth_mask) == 0``
  - 超标原子                       ``(new_mask & ~old_mask)`` 的 ``bit_count``
  - 漏标判定                       ``(old_mask & ~new_mask) != 0``

* 旧标签 ``l`` 必须从它的至多 3 个候选映射中选且仅选一个；同一旧标签在所有对象
  上共用该选择（不允许逐对象选）。
* 对象 ``o`` 迁移后有效分类 ``C(o) = OR 所选候选的原子掩码``。
* 硬约束：
  1. 不漏标：``old_mask(o) ⊆ C(o)``；
  2. 兼容性：对每个登记角色 r，``old_mask(o) ⊆ auth(r) ⇔ C(o) ⊆ auth(r)``。
     令 ``Cap(o) = AND{r : old_mask(o) ⊆ auth(r)} auth(r)``（旧消费者允许集合的
     交集；没有任何角色能读时取全 1 掩码，该对象不产生兼容约束），约束 2 等价于
     ``C(o) ⊆ Cap(o)``。
* 目标（字典序最小化）：
  1. 总额外类别数 ``Σ_o popcount(C(o) \\ old_mask(o))``——同一额外原子在同一
     对象上只计一次（掩码并集天然去重，这正是测试要求的语义）；
  2. 所有映射使用的新标签个数之和（同一新标签被不同旧标签的映射使用分别计数，
     即候选 ``width`` 之和）；
  3. 决胜键：旧标签按 ID 排序后，把每个所选候选的新标签 ID 有序元组依次拼接，
     对该序列取字典序最小（即“先按旧标签 ID、再按新标签 ID”决胜）；仍持平
     时用候选 ID 兜底，保证同一逻辑输入永远得到同一结果。

掩码复用
========
解析阶段所有集合已压到同一张位表上，DP 状态里每个对象只存一个整数（覆盖掩码）；
标签并集、子集/漏标/超标判定全是单条大整数位运算（C 级实现）。额外代价按
``popcount(cover \\ old_mask)`` 增量维护，无需保存原子列表。

剪枝（前向 DP + 状态合并）
==========================
按旧标签 ID 排序逐标签推进，DP 状态 = (每对象覆盖掩码元组, 已产生额外代价,
宽度和, 选择路径)。四类剪枝：

* **候选预筛（兼容掩码剪枝）**：候选原子一旦超出任一相关对象的 ``Cap(o)``，
  因为覆盖只增不减，该候选在任何前缀下都非法，直接剔除；
* **后缀覆盖剪枝**：剩余标签全部取最广候选仍补不齐某对象漏标 → 状态淘汰
  （``suffix_cover`` 预计算，O(对象数) 位运算判定）；
* **支配合并（核心剪枝）**：同一阶段两个状态若对象覆盖掩码完全相同，则后续
  决策空间一致，只保留 (额外代价, 宽度, 字典序路径) 更优者，另一个直接合并；
* **字典序前缀剪枝**：同覆盖状态在宽度/代价持平时，用已确定的选择前缀做
  字典序比较，劣者不展开。

16 旧标签 × 每标签至多 3 候选，朴素枚举为 3^16 ≈ 4.3e7 组合；经上述合并后
实际展开状态数通常下降 1~2 个数量级，纯标准库 CPU 秒级完成。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import E_INFEASIBLE, ValidationError
from .models import Case, Candidate, Mapping, mask_to_atom_ids


@dataclass
class SolveResult:
    feasible: bool
    selection: dict[str, str]          # old_label -> candidate_id
    total_extra: int
    total_new_labels: int
    nodes_visited: int
    pruned_cover: int
    pruned_cap: int
    pruned_bound: int
    reason: dict | None = None         # 无解时的最小诊断
    objects: list[dict] | None = None  # 逐对象迁移结果


def _cap_masks(case: Case) -> dict[str, int]:
    """计算每个对象的 Cap(o)：当前可读角色授权集合的交集。"""
    full = (1 << len(case.atoms)) - 1
    caps: dict[str, int] = {}
    for obj in case.objects:
        cap = full
        readers = 0
        for role in case.roles:
            if (obj.old_mask & ~role.auth_mask) == 0:
                cap &= role.auth_mask
                readers += 1
        caps[obj.id] = cap if readers else full
    return caps


def _union_candidates(mapping: Mapping) -> int:
    mask = 0
    for c in mapping.candidates:
        mask |= c.atom_mask
    return mask


def _diagnose(case: Case, caps: dict[str, int]) -> dict:
    """无解时给出最小可观测诊断：逐对象检查可达的最优覆盖。"""
    best_cover = {m.old_label: _union_candidates(m) for m in case.mappings}
    for obj in case.objects:
        reach = 0
        for lid in obj.old_label_ids:
            reach |= best_cover[lid]
        missing = obj.old_mask & ~reach
        if missing:
            return {
                "type": "uncovered_atom",
                "object": obj.id,
                "atoms": mask_to_atom_ids(missing, case),
                "detail": "即使选用覆盖最广的候选，仍有原子类别漏标",
            }
        overflow = reach & ~caps[obj.id]
        if overflow:
            return {
                "type": "role_capacity_exceeded",
                "object": obj.id,
                "atoms": mask_to_atom_ids(overflow, case),
                "detail": "覆盖漏标所需的候选必然引入未授权原子，"
                          "会改变至少一个登记角色的允许结论",
            }
    return {"type": "no_feasible_combination",
            "detail": "单对象约束均可满足，但不存在全局一致的候选组合"}


@dataclass
class _State:
    covers: tuple[int, ...]   # 每个对象当前覆盖掩码
    extra: int                # 截至当前的总额外类别数
    width: int                # 截至当前的新标签宽度和
    picks: tuple[int, ...]    # 每个已处理标签选择的候选下标

    def key(self) -> tuple:
        # 支配合并键：覆盖掩码相同 => 后续可行决策空间完全一致
        return self.covers


def solve(case: Case) -> SolveResult:
    """对已校验的 Case 求字典序最优迁移方案，无解时返回带诊断的 SolveResult。"""
    mappings = list(case.mappings)  # 已按 old_label ID 排序
    n = len(mappings)
    caps = _cap_masks(case)
    n_obj = len(case.objects)
    old_masks = [o.old_mask for o in case.objects]
    cap_list = [caps[o.id] for o in case.objects]

    # 每个旧标签影响哪些对象
    touched: list[list[int]] = []
    for m in mappings:
        touched.append([i for i, o in enumerate(case.objects)
                        if m.old_label in o.old_label_ids])

    # suffix_cover[k]：第 k..n-1 个标签全部取最广候选时能提供的原子并集
    suffix_cover = [0] * (n + 1)
    for k in range(n - 1, -1, -1):
        suffix_cover[k] = suffix_cover[k + 1] | _union_candidates(mappings[k])

    # 候选预筛：超出相关对象 Cap 的候选在任何前缀下都非法
    viable: list[list[Candidate]] = []
    cap_pruned = 0
    for k, m in enumerate(mappings):
        row: list[Candidate] = []
        for cand in m.candidates:
            if any(cand.atom_mask & ~cap_list[oi] for oi in touched[k]):
                cap_pruned += 1
                continue
            row.append(cand)
        viable.append(row)

    if any(not row for row in viable):
        return SolveResult(False, {}, 0, 0, 0, 0, cap_pruned, 0,
                           _diagnose(case, caps))

    # 候选按 (宽度, 新标签 ID 元组, 候选 ID) 排序，保证字典序最优路径先生成
    orders: list[list[int]] = []
    for k in range(n):
        m = mappings[k]
        orders.append(sorted(
            range(len(viable[k])),
            key=lambda i: (viable[k][i].width, viable[k][i].new_label_ids,
                           viable[k][i].id)))

    stats = {"nodes": 0, "cover": 0, "bound": 0}
    # 初始状态：所有对象覆盖为 0。同一覆盖键下保留一个 Pareto 前沿：
    # 路径按 (额外代价, 宽度) 严格支配才丢弃；代价宽度相同时两条路径都保留，
    # 因为字典序决胜只能在完整选择向量上做，过早合并会误杀全局字典序最优解。
    frontier: dict[tuple, list[_State]] = {
        tuple([0] * n_obj): [_State(tuple([0] * n_obj), 0, 0, ())]
    }

    for k in range(n):
        m = mappings[k]
        nxt: dict[tuple, list[_State]] = {}
        suffix = suffix_cover[k + 1]

        for states in frontier.values():
            for state in states:
                for vi in orders[k]:
                    stats["nodes"] += 1
                    cand = viable[k][vi]
                    cidx = m.candidates.index(cand)
                    covers = list(state.covers)
                    added = 0
                    for oi in touched[k]:
                        before = covers[oi]
                        after = before | cand.atom_mask
                        added += ((after & ~old_masks[oi]).bit_count()
                                  - (before & ~old_masks[oi]).bit_count())
                        covers[oi] = after

                    # 后缀覆盖剪枝：剩余标签补不齐漏标 => 淘汰
                    hopeless = any(
                        old_masks[oi] & ~(covers[oi] | suffix)
                        for oi in touched[k]
                    )
                    if hopeless:
                        stats["cover"] += 1
                        continue

                    new_state = _State(
                        tuple(covers),
                        state.extra + added,
                        state.width + cand.width,
                        state.picks + (cidx,),
                    )
                    bucket = nxt.setdefault(new_state.key(), [])
                    # 严格支配剪枝：同覆盖下仅当 (额外, 宽度) 被严格占优才淘汰。
                    # 代价与宽度相等的路径必须全部留存——字典序决胜只能在终局
                    # 的完整选择向量上比较，中间阶段无法安全丢弃。
                    if any(kept.extra <= new_state.extra
                           and kept.width <= new_state.width
                           and (kept.extra, kept.width)
                           != (new_state.extra, new_state.width)
                           for kept in bucket):
                        stats["bound"] += 1
                        continue
                    # 新路径严格支配的旧路径移除；同代价同宽度路径并列保留
                    bucket[:] = [s for s in bucket
                                 if not (new_state.extra <= s.extra
                                         and new_state.width <= s.width
                                         and (new_state.extra, new_state.width)
                                         != (s.extra, s.width))]
                    bucket.append(new_state)
        frontier = nxt
        if not frontier:
            return SolveResult(False, {}, 0, 0, stats["nodes"],
                               stats["cover"], cap_pruned, stats["bound"],
                               _diagnose(case, caps))

    # 终局：兼容约束已由候选预筛保证；这里做漏标终检并按完整目标
    # (额外代价, 宽度, 新标签 ID 决胜序列, 候选 ID 兜底序列) 取全局最优。
    def _tie_key(state: _State) -> tuple:
        new_label_sig = tuple(
            mappings[i].candidates[state.picks[i]].new_label_ids
            for i in range(n)
        )
        cand_id_sig = tuple(
            mappings[i].candidates[state.picks[i]].id for i in range(n)
        )
        return (state.extra, state.width, new_label_sig, cand_id_sig)

    best: _State | None = None
    best_key: tuple | None = None
    for states in frontier.values():
        for state in states:
            if any(old_masks[oi] & ~state.covers[oi] for oi in range(n_obj)):
                stats["cover"] += 1
                continue
            key = _tie_key(state)
            if best_key is None or key < best_key:
                best_key = key
                best = state

    if best is None:
        return SolveResult(False, {}, 0, 0, stats["nodes"],
                           stats["cover"], cap_pruned, stats["bound"],
                           _diagnose(case, caps))

    selection = {
        mappings[i].old_label: mappings[i].candidates[best.picks[i]].id
        for i in range(n)
    }
    objects_view = _build_objects_view(case, selection, caps)
    return SolveResult(
        feasible=True,
        selection=selection,
        total_extra=best.extra,
        total_new_labels=best.width,
        nodes_visited=stats["nodes"],
        pruned_cover=stats["cover"],
        pruned_cap=cap_pruned,
        pruned_bound=stats["bound"],
        objects=objects_view,
    )


def _build_objects_view(case: Case, selection: dict[str, str],
                        caps: dict[str, int]) -> list[dict]:
    """生成逐对象的迁移后分类与角色允许结论（供 plan/audit 复用）。"""
    view: list[dict] = []
    for obj in case.objects:
        new_mask = 0
        used: list[str] = []
        for lid in obj.old_label_ids:
            cid = selection[lid]
            cand = next(c for c in case.mapping_map[lid].candidates
                        if c.id == cid)
            new_mask |= cand.atom_mask
            used.append(cid)
        role_decisions = []
        for role in case.roles:
            old_allow = (obj.old_mask & ~role.auth_mask) == 0
            new_allow = (new_mask & ~role.auth_mask) == 0
            role_decisions.append({
                "role": role.id,
                "old_allow": old_allow,
                "new_allow": new_allow,
                "unchanged": old_allow == new_allow,
            })
        view.append({
            "object": obj.id,
            "old_atoms": mask_to_atom_ids(obj.old_mask, case),
            "new_atoms": mask_to_atom_ids(new_mask, case),
            "extra_atoms": mask_to_atom_ids(new_mask & ~obj.old_mask, case),
            "extra_count": (new_mask & ~obj.old_mask).bit_count(),
            "missing_atoms": mask_to_atom_ids(obj.old_mask & ~new_mask, case),
            "candidate_chain": used,
            "role_decisions": role_decisions,
        })
    return view


def require_feasible(result: SolveResult) -> None:
    """plan 命令在无解时抛出可定位业务错误。"""
    if not result.feasible:
        raise ValidationError(E_INFEASIBLE,
                              "不存在满足不漏标与角色兼容性的候选组合",
                              "$.mappings")
