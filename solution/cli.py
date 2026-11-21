"""``python -m solution <命令> CASE.json [--db DB.sqlite]`` 入口实现。

命令
====
* ``plan``   : 求字典序最优候选选择；带 ``--db`` 时同时登记计划；
* ``audit``  : 对 ``$.plan`` 既定方案逐对象逐角色核对（纯计算，不写库）；
* ``update`` : 必须带 ``--db``，在单事务内原子切换对象版本并保留历史判定。

标准输出一律为 UTF-8 JSON；结构/业务错误输出
``{"error": {code, message, path}}`` 并以退出码 2 结束，正常结果退出码 0。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import audit as audit_mod
from . import planner, storage
from .errors import ValidationError
from .models import mask_to_atom_ids, parse_case


def _load_case(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ValidationError("E_IO", f"输入文件不存在：{path}", "$")
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "E_JSON",
            f"JSON 解析失败：{exc.msg}",
            f"$(line {exc.lineno}, col {exc.colno})",
        )
    return raw, parse_case(raw)


def _new_masks(case, selection: dict[str, str]) -> dict[str, int]:
    masks: dict[str, int] = {}
    for obj in case.objects:
        mask = 0
        for lid in obj.old_label_ids:
            cid = selection[lid]
            cand = next(c for c in case.mapping_map[lid].candidates
                        if c.id == cid)
            mask |= cand.atom_mask
        masks[obj.id] = mask
    return masks


def _plan_payload(case, result) -> dict:
    return {
        "command": "plan",
        "case_id": case.case_id,
        "status": "optimal",
        "objective": {
            "total_extra_atoms": result.total_extra,
            "total_new_labels": result.total_new_labels,
        },
        "selection": [
            {"old_label": lid, "candidate_id": result.selection[lid]}
            for lid in sorted(result.selection)
        ],
        "objects": result.objects,
        "search_stats": {
            "nodes_visited": result.nodes_visited,
            "pruned_by_cover": result.pruned_cover,
            "pruned_by_role_capacity": result.pruned_cap,
            "pruned_by_bound": result.pruned_bound,
        },
    }


def cmd_plan(args) -> dict:
    raw, case = _load_case(args.case_file)
    result = planner.solve(case)
    planner.require_feasible(result)
    payload = _plan_payload(case, result)
    if args.db:
        if case.plan is None:
            raise ValidationError(
                "E_MISSING",
                "plan --db 需要输入中包含 $.plan（plan_id/有效期/selection）",
                "$.plan")
        conn = storage.connect(args.db)
        try:
            stored = storage.save_plan(
                conn, raw, case, result.selection,
                result.total_extra, result.total_new_labels)
        finally:
            conn.close()
        payload["persistence"] = stored
    return payload


def cmd_audit(args) -> dict:
    _, case = _load_case(args.case_file)
    report = audit_mod.run_audit(case)
    return {"command": "audit", "case_id": case.case_id, **report}


def cmd_update(args) -> dict:
    if not args.db:
        raise ValidationError("E_MISSING", "update 必须带 --db DB.sqlite", "$")
    raw, case = _load_case(args.case_file)
    if case.plan is None:
        raise ValidationError("E_MISSING", "update 需要 $.plan 既定方案",
                              "$.plan")
    # 切换前先做独立审计（过期/漏标/兼容性），不通过则拒绝写入
    audit_mod.run_audit(case)
    selection = {s.old_label: s.candidate_id for s in case.plan.selection}
    new_masks = _new_masks(case, selection)
    conn = storage.connect(args.db)
    try:
        summary = storage.apply_update(conn, raw, case, new_masks, selection)
        decisions = storage.list_decisions(conn, case.case_id)
    finally:
        conn.close()
    return {
        "command": "update",
        "case_id": case.case_id,
        "switch": summary,
        "object_classifications": [
            {
                "object": obj.id,
                "new_vocab_version": case.new_version,
                "new_atoms": mask_to_atom_ids(new_masks[obj.id], case),
            }
            for obj in case.objects
        ],
        "decisions_on_disk": len(decisions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="安全分类词表版本迁移校验器")
    parser.add_argument("command", choices=["plan", "audit", "update"])
    parser.add_argument("case_file")
    parser.add_argument("--db", dest="db", default=None,
                        help="SQLite 库路径（plan 登记/update 切换时必填）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            payload = cmd_plan(args)
        elif args.command == "audit":
            payload = cmd_audit(args)
        else:
            payload = cmd_update(args)
    except ValidationError as exc:
        sys.stdout.write(json.dumps(
            {"error": exc.to_payload()}, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 2
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    # audit 发现违规：报告照常输出（含最小反例），但退出码非零以便脚本门禁
    if args.command == "audit" and payload.get("ok") is False:
        return 2
    return 0
