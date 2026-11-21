"""audit 命令：对既定方案逐对象逐角色核对。

反例“最小可观测”的含义
======================
一旦发现违反约束，返回第一个（按对象 ID、再按角色 ID 排序的）最小反例：

* 漏标（underlabel）：对象迁移后分类缺少其原有效集合中的原子；
* 兼容（role_decision_changed）：某登记角色对该对象的允许结论发生变化，
  并附上新旧结论与责任原子；
* 计划过期：``now > valid_until``，先于内容检查判定。

全量核对结果同时放在 ``per_object`` 中，便于人工复查；反例本身只暴露
定位问题所需的最少字段。
"""

from __future__ import annotations

from .errors import E_PLAN_EXPIRED, E_PLAN_INVALID, ValidationError
from .models import Case, PlanBlock, mask_to_atom_ids
from .planner import _cap_masks


def _selection_map(plan: PlanBlock) -> dict[str, str]:
    return {s.old_label: s.candidate_id for s in plan.selection}


def evaluate(case: Case) -> dict:
    """按 case.plan 计算逐对象逐角色核对结果（不做过期判定）。"""
    assert case.plan is not None
    sel = _selection_map(case.plan)
    caps = _cap_masks(case)
    violations: list[dict] = []
    per_object: list[dict] = []

    for obj in case.objects:
        new_mask = 0
        chain: list[str] = []
        for lid in obj.old_label_ids:
            cid = sel[lid]
            cand = next(c for c in case.mapping_map[lid].candidates
                        if c.id == cid)
            new_mask |= cand.atom_mask
            chain.append(cid)

        missing = obj.old_mask & ~new_mask
        extra = new_mask & ~obj.old_mask
        role_rows = []
        for role in case.roles:
            old_allow = (obj.old_mask & ~role.auth_mask) == 0
            new_allow = (new_mask & ~role.auth_mask) == 0
            row = {
                "role": role.id,
                "old_allow": old_allow,
                "new_allow": new_allow,
                "unchanged": old_allow == new_allow,
            }
            role_rows.append(row)
            if old_allow != new_allow:
                # 责任原子：新分类中落在角色授权集合之外的部分
                blame = new_mask & ~role.auth_mask
                violations.append({
                    "kind": "role_decision_changed",
                    "object": obj.id,
                    "role": role.id,
                    "old_allow": old_allow,
                    "new_allow": new_allow,
                    "responsible_atoms": mask_to_atom_ids(blame, case),
                    "path": f"$.plan.selection",
                })
        if missing:
            violations.append({
                "kind": "underlabel",
                "object": obj.id,
                "missing_atoms": mask_to_atom_ids(missing, case),
                "path": f"$.objects[?id=={obj.id!r}]",
            })

        per_object.append({
            "object": obj.id,
            "old_atoms": mask_to_atom_ids(obj.old_mask, case),
            "new_atoms": mask_to_atom_ids(new_mask, case),
            "missing_atoms": mask_to_atom_ids(missing, case),
            "extra_atoms": mask_to_atom_ids(extra, case),
            "extra_count": extra.bit_count(),
            "cap_atoms": mask_to_atom_ids(caps[obj.id], case),
            "candidate_chain": chain,
            "role_decisions": role_rows,
        })

    total_extra = sum(row["extra_count"] for row in per_object)
    return {
        "ok": not violations,
        "violation_count": len(violations),
        "minimal_counterexample": violations[0] if violations else None,
        "all_violations": violations,
        "total_extra_atoms": total_extra,
        "per_object": per_object,
    }


def run_audit(case: Case) -> dict:
    """audit 入口：过期优先判定，随后做内容核对。"""
    if case.plan is None:
        raise ValidationError(E_PLAN_INVALID, "audit 需要 $.plan 既定方案",
                              "$.plan")
    if case.now > case.plan.valid_until:
        raise ValidationError(
            E_PLAN_EXPIRED,
            f"计划 {case.plan.plan_id!r} 已过期：now={case.now} > "
            f"valid_until={case.plan.valid_until}",
            "$.plan.valid_until",
        )
    return evaluate(case)
