"""结构校验错误码、字段定位与输出确定性测试。"""

from __future__ import annotations

import json

from solution.errors import ValidationError
from solution.models import parse_case

from tests._helpers import CliTestCase


class ValidationTests(CliTestCase):

    def _expect(self, raw: dict, code: str, path_substr: str):
        with self.assertRaises(ValidationError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, code)
        self.assertIn(path_substr, ctx.exception.path)
        return ctx.exception

    def test_missing_top_level_field_has_location(self):
        payload = self.base_case()
        del payload["roles"]
        self._expect(payload, "E_MISSING", "$.roles")

    def test_wrong_type_reports_type_and_path(self):
        payload = self.base_case()
        payload["now"] = "soon"
        self._expect(payload, "E_TYPE", "$.now")

    def test_unknown_field_is_rejected(self):
        payload = self.base_case()
        payload["objects"][0]["surprise"] = 1
        self._expect(payload, "E_UNKNOWN", "surprise")

    def test_duplicate_object_id(self):
        payload = self.base_case()
        payload["objects"].append({"id": "obj-1", "labels": ["L1"]})
        self._expect(payload, "E_DUPLICATE", "$.objects")

    def test_dangling_label_reference(self):
        payload = self.base_case()
        payload["objects"][0]["labels"] = ["L999"]
        self._expect(payload, "E_REF", "$.objects[0].labels[0]")

    def test_dangling_atom_reference(self):
        payload = self.base_case()
        payload["old_labels"][0]["atoms"] = ["ZZZ"]
        self._expect(payload, "E_REF", "$.old_labels[0].atoms[0]")

    def test_too_many_candidates(self):
        payload = self.base_case()
        m = next(m for m in payload["mappings"] if m["old_label"] == "L1")
        # 已有 2 个候选，再补 2 个达到 4 个，超过上限 3
        m["candidates"].append({"id": "extra-1", "new_labels": ["N1"]})
        m["candidates"].append({"id": "extra-2", "new_labels": ["N1"]})
        self._expect(payload, "E_RANGE", "$.mappings[0].candidates")

    def test_too_many_old_labels(self):
        payload = self.base_case()
        payload["atoms"] = [f"A{i}" for i in range(20)]
        payload["old_labels"] = [
            {"id": f"L{i}", "atoms": [f"A{i % 20}"]} for i in range(17)]
        payload["new_labels"] = [
            {"id": f"N{i}", "atoms": [f"A{i % 20}"]} for i in range(17)]
        payload["mappings"] = [
            {"old_label": f"L{i}",
             "candidates": [{"id": f"{i}-c", "new_labels": [f"N{i}"]}]}
            for i in range(17)]
        payload["objects"] = [{"id": "o1", "labels": ["L0"]}]
        self._expect(payload, "E_RANGE", "$.old_labels")

    def test_plan_candidate_not_in_mapping(self):
        payload = self.base_case()
        payload["plan"]["selection"][0]["candidate_id"] = "nope"
        self._expect(payload, "E_REF", "$.plan.selection[0].candidate_id")

    def test_plan_missing_old_label(self):
        payload = self.base_case()
        payload["plan"]["selection"].pop()
        self._expect(payload, "E_MISSING", "$.plan.selection")

    def test_malformed_json_reports_line_and_column(self):
        path = self.write_case("bad.json", {})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"now": 1, "atoms": [}\n]')
        code, out = self.run_cli("plan", path)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_JSON")
        self.assertIn("line", out["error"]["path"])

    def test_missing_file(self):
        code, out = self.run_cli("plan", "D:/nonexistent/case.json")
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_IO")

    def test_overlapping_new_labels_are_allowed(self):
        # 新标签间允许原子重叠，结构校验不得拒绝
        payload = {
            "case_id": "ov", "now": 1,
            "atoms": ["A", "B"],
            "old_labels": [{"id": "L1", "atoms": ["A"]}],
            "new_labels": [
                {"id": "N1", "atoms": ["A", "B"]},
                {"id": "N2", "atoms": ["B"]},
            ],
            "mappings": [{
                "old_label": "L1",
                "candidates": [{"id": "c1", "new_labels": ["N1", "N2"]}],
            }],
            "objects": [{"id": "o1", "labels": ["L1"]}],
            "roles": [{"id": "r", "authorized_atoms": ["A", "B"]}],
        }
        case = parse_case(payload)  # 不抛异常即通过
        self.assertEqual(len(case.new_labels), 2)


class OutputContractTests(CliTestCase):

    def test_stdout_is_utf8_json_and_deterministic(self):
        path = self.write_case("d.json", self.base_case())
        c1, o1 = self.run_cli("plan", path)
        c2, o2 = self.run_cli("plan", path)
        self.assertEqual((c1, c2), (0, 0))
        self.assertEqual(json.dumps(o1, sort_keys=True, ensure_ascii=False),
                         json.dumps(o2, sort_keys=True, ensure_ascii=False))
        # selection 按旧标签 ID 排序
        olds = [s["old_label"] for s in o1["selection"]]
        self.assertEqual(olds, sorted(olds))

    def test_error_envelope_shape(self):
        path = self.write_case("e.json", self.base_case())
        # 破坏文件内容触发结构错误
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"now": 1}')
        code, out = self.run_cli("audit", path)
        self.assertEqual(code, 2)
        self.assertEqual(set(out["error"]), {"code", "message", "path"})

    def test_plan_reports_search_stats(self):
        path = self.write_case("s.json", self.base_case())
        _, out = self.run_cli("plan", path)
        stats = out["search_stats"]
        for key in ("nodes_visited", "pruned_by_cover",
                    "pruned_by_role_capacity", "pruned_by_bound"):
            self.assertIn(key, stats)
            self.assertIsInstance(stats[key], int)
