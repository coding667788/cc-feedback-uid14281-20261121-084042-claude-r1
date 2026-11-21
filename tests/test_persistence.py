"""update / SQLite 测试：原子切换、历史判定保留、过期拦截与故障后重启。"""

from __future__ import annotations

import sqlite3

from tests._helpers import CliTestCase


class PersistenceTests(CliTestCase):

    def _register_and_switch(self, payload: dict, db: str):
        case_path = self.write_case("case.json", payload)
        code, out = self.run_cli("plan", case_path, "--db", db)
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("update", case_path, "--db", db)
        self.assertEqual(code, 0, out)
        return case_path, out

    def test_update_atomic_switch_keeps_historical_decisions(self):
        db = self.db_path()
        payload = self.base_case()
        _, out = self._register_and_switch(payload, db)
        switch = out["switch"]
        self.assertTrue(switch["switched"])
        self.assertEqual(switch["current_object_version"], 2)
        self.assertEqual(switch["current_vocab_version"], "v2")
        self.assertEqual(switch["old_vocab_version_retained"], "v1")
        # 4 对象 × 2 角色 = 8 条旧判定保留，8 条新判定写入
        self.assertEqual(switch["historical_decisions_kept"], 8)
        self.assertEqual(switch["new_decisions_written"], 8)
        self.assertEqual(out["decisions_on_disk"], 16)

        # 直接查库：旧版本判定原样保留且角色结论与输入一致
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        old_rows = conn.execute(
            "SELECT object_id, role_id, allow FROM decisions "
            "WHERE vocab_version='v1' ORDER BY object_id, role_id").fetchall()
        self.assertEqual(len(old_rows), 8)
        # r-analyst 对 obj-3（含 D）旧结论为拒绝
        deny = next(r for r in old_rows
                    if r["object_id"] == "obj-3" and r["role_id"] == "r-analyst")
        self.assertEqual(deny["allow"], 0)
        # 当前指针指向 v2
        cur = conn.execute(
            "SELECT vocab_version, object_version FROM "
            "current_object_version").fetchone()
        self.assertEqual(tuple(cur), ("v2", 2))
        conn.close()

    # 题目点名场景 6：故障后重启（幂等重放，库状态不被二次破坏）
    def test_restart_after_crash_is_idempotent(self):
        db = self.db_path()
        payload = self.base_case()
        case_path, first = self._register_and_switch(payload, db)

        # 模拟进程重启后再次执行同一 update
        code, second = self.run_cli("update", case_path, "--db", db)
        self.assertEqual(code, 0)
        self.assertTrue(second["switch"]["idempotent_replay"])
        self.assertFalse(second["switch"]["switched"])
        # 对象版本不增长，历史判定不重复
        self.assertEqual(second["switch"]["current_object_version"], 2)
        self.assertEqual(second["decisions_on_disk"], 16)

        # 再重启一次，结果完全一致
        code, third = self.run_cli("update", case_path, "--db", db)
        self.assertEqual(third["switch"], second["switch"])

    def test_simulated_rollback_leaves_consistent_state(self):
        """直接在库上制造一个中途失败的事务，验证 SQLite 回滚后库仍一致。"""
        db = self.db_path()
        payload = self.base_case()
        case_path = self.write_case("case.json", payload)
        code, _ = self.run_cli("plan", case_path, "--db", db)
        self.assertEqual(code, 0)

        # 打开第二个连接，BEGIN 后故意出错回滚，模拟更新进程崩溃
        conn = sqlite3.connect(db)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO object_versions(case_id, object_id, object_version, "
            "vocab_version, atom_ids_json) VALUES('unit', 'ghost', 99, 'v2', '[]')")
        conn.rollback()
        conn.close()

        # 正式 update 仍能成功，且不存在幽灵行
        code, out = self.run_cli("update", case_path, "--db", db)
        self.assertEqual(code, 0)
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM object_versions WHERE object_id='ghost'").fetchone()[0]
        self.assertEqual(n, 0)
        conn.close()

    def test_update_requires_registered_plan(self):
        db = self.db_path()
        payload = self.base_case()
        case_path = self.write_case("case.json", payload)
        # 未先 plan --db 直接 update
        code, out = self.run_cli("update", case_path, "--db", db)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_PLAN_NOT_FOUND")

    def test_update_rejects_expired_plan(self):
        db = self.db_path()
        payload = self.base_case()
        payload["now"] = 5000
        case_path = self.write_case("expired.json", payload)
        # 过期计划无法登记为可用切换：先直接写库再 update，验证 update 侧拦截
        code, out = self.run_cli("plan", case_path, "--db", db)
        # plan 的求解本身成功，但 update 必须因过期拒绝
        self.assertEqual(code, 0)
        code, out = self.run_cli("update", case_path, "--db", db)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_PLAN_EXPIRED")
        # 库中没有产生 v2 对象版本
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM object_versions WHERE vocab_version='v2'").fetchone()[0]
        self.assertEqual(n, 0)
        conn.close()

    def test_input_drift_is_rejected(self):
        db = self.db_path()
        payload = self.base_case()
        case_path = self.write_case("case.json", payload)
        code, _ = self.run_cli("plan", case_path, "--db", db)
        self.assertEqual(code, 0)
        # 修改输入中的角色授权但保持 case_id，形成快照漂移
        drifted = self.base_case()
        drifted["roles"][0]["authorized_atoms"] = ["A"]
        drift_path = self.write_case("case.json", drifted)
        code, out = self.run_cli("update", drift_path, "--db", db)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_DB_STATE")

    def test_update_without_db_flag_is_usage_error(self):
        case_path = self.write_case("case.json", self.base_case())
        code, out = self.run_cli("update", case_path)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_MISSING")


class PersistenceContentTests(CliTestCase):
    """直接调用 storage 层，核对落库内容与迁移掩码一致。"""

    def test_stored_new_classification_matches_mask(self):
        from solution.models import parse_case
        from solution import storage
        payload = self.base_case()
        case = parse_case(payload)
        conn = storage.connect(self.db_path("direct.sqlite"))
        sel = {s.old_label: s.candidate_id for s in case.plan.selection}
        masks = {}
        for obj in case.objects:
            m = 0
            for lid in obj.old_label_ids:
                cand = next(c for c in case.mapping_map[lid].candidates
                            if c.id == sel[lid])
                m |= cand.atom_mask
            masks[obj.id] = m
        storage.save_plan(conn, payload, case, sel, 1, 4)
        summary = storage.apply_update(conn, payload, case, masks, sel)
        self.assertEqual(summary["current_vocab_version"], "v2")
        decisions = storage.list_decisions(conn, case.case_id)
        # 新旧版本各 8 条，按对象版本排序
        versions = {(d["object_version"], d["vocab_version"]) for d in decisions}
        self.assertEqual(versions, {(1, "v1"), (2, "v2")})
        conn.close()
