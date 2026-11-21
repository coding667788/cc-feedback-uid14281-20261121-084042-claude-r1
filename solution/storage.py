"""SQLite 持久化与 update 原子切换。

库结构（schema_version=1）
==========================
* ``meta``                : 单行库元数据；
* ``cases``               : 输入快照（规范化 JSON + 哈希），用于检测状态漂移；
* ``vocab_versions``      : 词表版本（old/new 各一行）；
* ``label_atoms``         : 标签 → 原子类别（版本化）；
* ``object_versions``     : 对象在每个词表版本下的有效分类与对象版本号；
* ``role_auths``          : 角色授权集合（版本化登记）；
* ``plans``               : 已登记计划（含状态、有效期、选择）；
* ``decisions``           : 历史判定（对象版本 × 角色 × 词表版本，只增不改）。

原子性与崩溃恢复
================
所有写操作在单个 ``BEGIN IMMEDIATE`` 事务内完成，靠 SQLite 的 rollback journal
保证：进程在事务中途被杀死后，重新打开库时未提交事务自动回滚，库回到上一个
一致状态，重新执行同一命令即可继续（幂等）。``update`` 只新增对象版本与判定
行并翻转指针，绝不删除旧版本行，因此旧版本的历史判定完整保留。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .errors import (
    E_DB_STATE,
    E_PLAN_EXPIRED,
    E_PLAN_NOT_FOUND,
    ValidationError,
)
from .models import Case, mask_to_atom_ids

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab_versions (
    case_id TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('old', 'new')),
    PRIMARY KEY (case_id, version)
);
CREATE TABLE IF NOT EXISTS label_atoms (
    case_id TEXT NOT NULL,
    version TEXT NOT NULL,
    label_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY (case_id, version, label_id, atom_id)
);
CREATE TABLE IF NOT EXISTS object_versions (
    case_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_version INTEGER NOT NULL,
    vocab_version TEXT NOT NULL,
    atom_ids_json TEXT NOT NULL,
    PRIMARY KEY (case_id, object_id, object_version)
);
CREATE TABLE IF NOT EXISTS current_object_version (
    case_id TEXT PRIMARY KEY,
    object_version INTEGER NOT NULL,
    vocab_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_auths (
    case_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY (case_id, role_id, atom_id)
);
CREATE TABLE IF NOT EXISTS plans (
    case_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('registered', 'committed', 'expired')),
    selection_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    committed_at INTEGER,
    PRIMARY KEY (case_id, plan_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    case_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_version INTEGER NOT NULL,
    vocab_version TEXT NOT NULL,
    role_id TEXT NOT NULL,
    allow INTEGER NOT NULL,
    atom_ids_json TEXT NOT NULL,
    PRIMARY KEY (case_id, object_id, object_version, role_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）库并初始化 schema。"""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def canonical_hash(raw: dict) -> str:
    """对原始输入做规范化哈希：同一逻辑输入 ⇒ 同一哈希。"""
    blob = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _ensure_case(conn: sqlite3.Connection, raw: dict, case: Case) -> str:
    """登记/校验输入快照，返回 input_hash；漂移时抛 E_DB_STATE。"""
    h = canonical_hash(raw)
    row = conn.execute(
        "SELECT input_hash FROM cases WHERE case_id = ?", (case.case_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO cases(case_id, input_hash, snapshot_json, updated_at) "
            "VALUES(?, ?, ?, ?)",
            (case.case_id, h,
             json.dumps(raw, ensure_ascii=False, sort_keys=True), case.now),
        )
    elif row["input_hash"] != h:
        raise ValidationError(
            E_DB_STATE,
            f"case {case.case_id!r} 的输入与库中已登记快照不一致，"
            "拒绝在漂移状态下写入",
            "$",
        )
    return h


def _write_fixtures(conn: sqlite3.Connection, case: Case) -> None:
    """幂等写入词表版本、标签、角色授权与旧版对象分类。"""
    conn.execute(
        "INSERT OR IGNORE INTO vocab_versions(case_id, version, kind) "
        "VALUES(?, ?, 'old')", (case.case_id, case.old_version))
    conn.execute(
        "INSERT OR IGNORE INTO vocab_versions(case_id, version, kind) "
        "VALUES(?, ?, 'new')", (case.case_id, case.new_version))

    for version, labels in ((case.old_version, case.old_labels),
                            (case.new_version, case.new_labels)):
        for label in labels:
            for ord_, aid in enumerate(label.atom_ids):
                conn.execute(
                    "INSERT OR IGNORE INTO label_atoms"
                    "(case_id, version, label_id, atom_id, ord) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (case.case_id, version, label.id, aid, ord_))

    for role in case.roles:
        for ord_, aid in enumerate(role.auth_atom_ids):
            conn.execute(
                "INSERT OR IGNORE INTO role_auths(case_id, role_id, atom_id, ord)"
                " VALUES(?, ?, ?, ?)",
                (case.case_id, role.id, aid, ord_))

    # 旧版本对象：对象版本号固定为 1
    for obj in case.objects:
        conn.execute(
            "INSERT OR IGNORE INTO object_versions"
            "(case_id, object_id, object_version, vocab_version, atom_ids_json) "
            "VALUES(?, ?, 1, ?, ?)",
            (case.case_id, obj.id, case.old_version,
             json.dumps(mask_to_atom_ids(obj.old_mask, case),
                        ensure_ascii=False)))
    conn.execute(
        "INSERT OR IGNORE INTO current_object_version"
        "(case_id, object_version, vocab_version) VALUES(?, 1, ?)",
        (case.case_id, case.old_version))

    # 旧版本历史判定（只增不改）
    for obj in case.objects:
        for role in case.roles:
            allow = (obj.old_mask & ~role.auth_mask) == 0
            conn.execute(
                "INSERT OR IGNORE INTO decisions"
                "(case_id, object_id, object_version, vocab_version, role_id, "
                "allow, atom_ids_json) VALUES(?, ?, 1, ?, ?, ?, ?)",
                (case.case_id, obj.id, case.old_version, role.id,
                 1 if allow else 0,
                 json.dumps(mask_to_atom_ids(obj.old_mask, case),
                            ensure_ascii=False)))


def save_plan(conn: sqlite3.Connection, raw: dict, case: Case,
              selection: dict[str, str], total_extra: int,
              total_new_labels: int) -> dict:
    """登记 plan 结果（事务内调用）；同 plan_id 重复提交走幂等校验。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        h = _ensure_case(conn, raw, case)
        _write_fixtures(conn, case)
        assert case.plan is not None
        plan = case.plan
        sel_json = json.dumps(
            [{"old_label": k, "candidate_id": selection[k]}
             for k in sorted(selection)],
            ensure_ascii=False, sort_keys=True)
        row = conn.execute(
            "SELECT status, selection_json FROM plans WHERE case_id = ? "
            "AND plan_id = ?", (case.case_id, plan.plan_id)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO plans(case_id, plan_id, created_at, valid_until, "
                "status, selection_json, input_hash, committed_at) "
                "VALUES(?, ?, ?, ?, 'registered', ?, ?, NULL)",
                (case.case_id, plan.plan_id, plan.created_at,
                 plan.valid_until, sel_json, h))
        elif row["selection_json"] != sel_json:
            raise ValidationError(
                E_DB_STATE,
                f"plan_id {plan.plan_id!r} 已登记且选择不同，拒绝覆盖",
                "$.plan.plan_id")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"case_id": case.case_id, "plan_id": plan.plan_id,
            "status": "registered", "input_hash": h,
            "total_extra_atoms": total_extra,
            "total_new_labels": total_new_labels}


def apply_update(conn: sqlite3.Connection, raw: dict, case: Case,
                 new_masks: dict[str, int], selection: dict[str, str]) -> dict:
    """按词表版本与对象版本原子切换，保留全部历史判定。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_case(conn, raw, case)
        _write_fixtures(conn, case)
        assert case.plan is not None
        plan = case.plan

        row = conn.execute(
            "SELECT status, valid_until, selection_json FROM plans "
            "WHERE case_id = ? AND plan_id = ?",
            (case.case_id, plan.plan_id)).fetchone()
        if row is None:
            raise ValidationError(
                E_PLAN_NOT_FOUND,
                f"计划 {plan.plan_id!r} 尚未通过 plan --db 登记，无法切换",
                "$.plan.plan_id")
        sel_json = json.dumps(
            [{"old_label": k, "candidate_id": selection[k]}
             for k in sorted(selection)],
            ensure_ascii=False, sort_keys=True)
        if row["selection_json"] != sel_json:
            raise ValidationError(
                E_DB_STATE,
                f"计划 {plan.plan_id!r} 的选择与库中登记内容不一致",
                "$.plan.selection")
        if case.now > row["valid_until"]:
            conn.execute(
                "UPDATE plans SET status = 'expired' WHERE case_id = ? "
                "AND plan_id = ?", (case.case_id, plan.plan_id))
            conn.commit()
            raise ValidationError(
                E_PLAN_EXPIRED,
                f"计划 {plan.plan_id!r} 已过期：now={case.now} > "
                f"valid_until={row['valid_until']}",
                "$.plan.valid_until")

        cur = conn.execute(
            "SELECT object_version, vocab_version FROM current_object_version "
            "WHERE case_id = ?", (case.case_id,)).fetchone()
        already = (cur is not None
                   and cur["vocab_version"] == case.new_version)
        if already:
            # 崩溃重启后的幂等重放：切换已提交，直接回报现状
            conn.commit()
            return _switch_summary(conn, case, plan.plan_id,
                                   cur["object_version"], idempotent=True)

        next_version = (cur["object_version"] if cur else 1) + 1

        for obj in case.objects:
            new_mask = new_masks[obj.id]
            atom_ids = mask_to_atom_ids(new_mask, case)
            conn.execute(
                "INSERT INTO object_versions(case_id, object_id, "
                "object_version, vocab_version, atom_ids_json) "
                "VALUES(?, ?, ?, ?, ?)",
                (case.case_id, obj.id, next_version,
                 case.new_version,
                 json.dumps(atom_ids, ensure_ascii=False)))
            # 新版本逐角色判定（历史行只增不改）
            for role in case.roles:
                allow = (new_mask & ~role.auth_mask) == 0
                conn.execute(
                    "INSERT OR IGNORE INTO decisions"
                    "(case_id, object_id, object_version, vocab_version, "
                    "role_id, allow, atom_ids_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (case.case_id, obj.id, next_version,
                     case.new_version, role.id, 1 if allow else 0,
                     json.dumps(atom_ids, ensure_ascii=False)))

        conn.execute(
            "UPDATE current_object_version SET object_version = ?, "
            "vocab_version = ? WHERE case_id = ?",
            (next_version, case.new_version, case.case_id))
        conn.execute(
            "UPDATE plans SET status = 'committed', committed_at = ? "
            "WHERE case_id = ? AND plan_id = ?",
            (case.now, case.case_id, plan.plan_id))
        conn.commit()
        return _switch_summary(conn, case, plan.plan_id, next_version,
                               idempotent=False)
    except Exception:
        conn.rollback()
        raise


def _switch_summary(conn: sqlite3.Connection, case: Case, plan_id: str,
                    object_version: int, idempotent: bool) -> dict:
    kept = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE case_id = ? "
        "AND vocab_version = ?",
        (case.case_id, case.old_version)).fetchone()["n"]
    added = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE case_id = ? "
        "AND vocab_version = ? AND object_version = ?",
        (case.case_id, case.new_version, object_version)).fetchone()["n"]
    return {
        "case_id": case.case_id,
        "plan_id": plan_id,
        "switched": not idempotent,
        "idempotent_replay": idempotent,
        "current_object_version": object_version,
        "current_vocab_version": case.new_version,
        "old_vocab_version_retained": case.old_version,
        "historical_decisions_kept": kept,
        "new_decisions_written": added,
    }


def list_decisions(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    """读取全部历史判定（供测试与人工核查，按版本/对象/角色排序）。"""
    rows = conn.execute(
        "SELECT object_id, object_version, vocab_version, role_id, allow, "
        "atom_ids_json FROM decisions WHERE case_id = ? "
        "ORDER BY object_version, object_id, role_id", (case_id,)).fetchall()
    return [
        {
            "object": r["object_id"],
            "object_version": r["object_version"],
            "vocab_version": r["vocab_version"],
            "role": r["role_id"],
            "allow": bool(r["allow"]),
            "atom_ids": json.loads(r["atom_ids_json"]),
        }
        for r in rows
    ]
