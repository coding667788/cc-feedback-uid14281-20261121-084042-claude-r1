"""audit 命令测试：过期优先、漏标反例、角色结论变化反例与最小性。"""

from __future__ import annotations

import copy

from tests._helpers import CliTestCase


class AuditCliTests(CliTestCase):

    def test_audit_ok_for_valid_plan(self):
        path = self.write_case("ok.json", self.base_case())
        code, payload = self.run_cli("audit", path)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["minimal_counterexample"])
        self.assertEqual(payload["violation_count"], 0)
        self.assertEqual(payload["total_extra_atoms"], 1)
        # 逐对象逐角色结论都在
        for row in payload["per_object"]:
            self.assertTrue(all(d["unchanged"] for d in row["role_decisions"]))

    # 题目点名场景 5：过期计划
    def test_audit_rejects_expired_plan_first(self):
        payload = self.base_case()
        payload["now"] = 5000  # 晚于 valid_until=2000
        path = self.write_case("expired.json", payload)
        code, out = self.run_cli("audit", path)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_PLAN_EXPIRED")
        self.assertEqual(out["error"]["path"], "$.plan.valid_until")

    def test_audit_reports_underlabel_counterexample(self):
        payload = self.base_case()
        # 把既定方案改成漏标：L1 选一个不含 A 的候选（N3={B}）
        payload["mappings"][0]["candidates"].append(
            {"id": "L1-bad", "new_labels": ["N3"]})
        for sel in payload["plan"]["selection"]:
            if sel["old_label"] == "L1":
                sel["candidate_id"] = "L1-bad"
        path = self.write_case("under.json", payload)
        # 内容违规：完整反例报告照常输出，退出码非零便于门禁
        code, out = self.run_cli("audit", path)
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        ce = out["minimal_counterexample"]
        self.assertEqual(ce["kind"], "underlabel")
        self.assertIn("A", ce["missing_atoms"])
        # 最小性：反例只引用一个对象
        self.assertEqual(len({ce["object"]}), 1)
        # 直接调内部 evaluate 得到同样结论
        from solution.models import parse_case
        from solution.audit import evaluate
        report = evaluate(parse_case(payload))
        self.assertFalse(report["ok"])
        self.assertEqual(report["minimal_counterexample"]["kind"],
                         "underlabel")

    def test_audit_reports_role_decision_change_with_responsible_atoms(self):
        payload = self.base_case()
        payload["objects"] = [{"id": "obj-x", "labels": ["L1"]}]
        payload["roles"] = [{"id": "r-tight", "authorized_atoms": ["A"]}]
        # 既定方案选 N5={A,D}：r-tight 旧允许、新拒绝
        payload["mappings"][0]["candidates"] = [
            {"id": "L1-wide", "new_labels": ["N5"]}]
        # 计划必须覆盖全部旧标签；其余标签保留各自默认候选
        payload["plan"]["selection"] = [
            {"old_label": "L1", "candidate_id": "L1-wide"},
            {"old_label": "L2", "candidate_id": "L2-m1"},
            {"old_label": "L3", "candidate_id": "L3-m1"},
            {"old_label": "L4", "candidate_id": "L4-m1"}]
        from solution.models import parse_case
        from solution.audit import evaluate
        report = evaluate(parse_case(payload))
        self.assertFalse(report["ok"])
        ce = report["minimal_counterexample"]
        self.assertEqual(ce["kind"], "role_decision_changed")
        self.assertEqual(ce["object"], "obj-x")
        self.assertEqual(ce["role"], "r-tight")
        self.assertTrue(ce["old_allow"])
        self.assertFalse(ce["new_allow"])
        self.assertEqual(ce["responsible_atoms"], ["D"])

    def test_audit_deterministic_ordering(self):
        payload = self.base_case()
        from solution.models import parse_case
        from solution.audit import evaluate
        r1 = evaluate(parse_case(copy.deepcopy(payload)))
        r2 = evaluate(parse_case(copy.deepcopy(payload)))
        self.assertEqual(
            [(o["object"]) for o in r1["per_object"]],
            [(o["object"]) for o in r2["per_object"]])
