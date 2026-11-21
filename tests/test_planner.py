"""plan 求解器测试：代价口径、角色约束、改名等价、无解与目标决胜。"""

from __future__ import annotations

import copy

from solution.models import parse_case
from solution.planner import solve

from tests._helpers import CliTestCase


class PlannerTests(CliTestCase):

    # ------------------------------------------------------------------
    # 题目点名场景 1：两个旧标签引入同一额外类别，在同一对象上只计一次代价
    # ------------------------------------------------------------------
    def test_shared_extra_atom_counted_once_per_object(self):
        payload = self.base_case(include_plan=False)
        # L2 与 L3 的“宽候选”都含 N2={B,C}；对象 obj-2 同时带 L2、L3。
        # 若两者都选宽候选，C 对 L2 是额外、B 对 L3 是额外，但 N2 的并集
        # 在 obj-2 上只是 {B,C}（其旧集合本身就是 {B,C}），额外代价应为 0，
        # 而不是按标签重复计 2。
        payload["mappings"][1]["candidates"] = [
            {"id": "L2-m1", "new_labels": ["N3"]},
            {"id": "L2-wide", "new_labels": ["N2"]},
        ]
        payload["mappings"][2]["candidates"] = [
            {"id": "L3-m1", "new_labels": ["N4"]},
            {"id": "L3-wide", "new_labels": ["N2"]},
        ]
        case = parse_case(payload)
        result = solve(case)
        self.assertTrue(result.feasible)
        obj2 = next(o for o in result.objects if o["object"] == "obj-2")
        self.assertEqual(obj2["extra_atoms"], [])
        self.assertEqual(obj2["extra_count"], 0)
        # 全对象总代价：仅 obj-3 因 L4→N5 被迫超标 A，计 1
        self.assertEqual(result.total_extra, 1)

        # 反证：若按标签重复计数，选两个 wide 的局部代价会被误算为 2；
        # 直接用掩码口径再算一次确认
        wide_pick = {"L1": "L1-m1", "L2": "L2-wide", "L3": "L3-wide",
                     "L4": "L4-m1"}
        case2 = parse_case(payload)
        masks = {}
        for obj in case2.objects:
            m = 0
            for lid in obj.old_label_ids:
                cid = wide_pick[lid]
                cand = next(c for c in case2.mapping_map[lid].candidates
                            if c.id == cid)
                m |= cand.atom_mask
            masks[obj.id] = m
        obj2_mask = masks["obj-2"]
        self.assertEqual(
            (obj2_mask & ~case2.object_map["obj-2"].old_mask).bit_count(), 0)

    # ------------------------------------------------------------------
    # 题目点名场景 2：角色没有授权导致额外类别不能加
    # ------------------------------------------------------------------
    def test_unauthorized_extra_atom_makes_candidate_infeasible(self):
        payload = self.base_case(include_plan=False)
        # r-analyst 只能读 A,B,C；构造对象 obj-x 旧集合 {A}，r-analyst 可读。
        # 唯一候选把它映射到含 D 的新标签 → 新集合含 D，r-analyst 结论
        # 由允许变拒绝，兼容性被破坏 → 无解。
        payload["objects"] = [{"id": "obj-x", "labels": ["L1"]}]
        payload["roles"] = [
            {"id": "r-analyst", "authorized_atoms": ["A"]},
        ]
        payload["mappings"][0]["candidates"] = [
            {"id": "L1-only", "new_labels": ["N5"]},   # N5 = {A,D}
        ]
        case = parse_case(payload)
        result = solve(case)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason["type"], "role_capacity_exceeded")
        self.assertEqual(result.reason["object"], "obj-x")
        self.assertIn("D", result.reason["atoms"])

    def test_underlabel_alone_is_infeasible(self):
        payload = self.base_case(include_plan=False)
        # L1 的候选只覆盖 B，丢掉旧原子 A → 漏标
        payload["objects"] = [{"id": "obj-x", "labels": ["L1"]}]
        payload["mappings"][0]["candidates"] = [
            {"id": "L1-lose", "new_labels": ["N3"]},  # N3 = {B}
        ]
        case = parse_case(payload)
        result = solve(case)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason["type"], "uncovered_atom")
        self.assertEqual(result.reason["atoms"], ["A"])

    # ------------------------------------------------------------------
    # 题目点名场景 3：已改名但语义完全一致 → 零额外代价、零角色结论变化
    # ------------------------------------------------------------------
    def test_rename_with_identical_semantics_is_free(self):
        payload = {
            "case_id": "rename",
            "now": 1,
            "atoms": ["A", "B"],
            "old_labels": [{"id": "OLD-PII", "atoms": ["A", "B"]}],
            "new_labels": [{"id": "NEW-PII", "atoms": ["A", "B"]}],
            "mappings": [{
                "old_label": "OLD-PII",
                "candidates": [
                    {"id": "rename-1", "new_labels": ["NEW-PII"]},
                ],
            }],
            "objects": [{"id": "o1", "labels": ["OLD-PII"]}],
            "roles": [{"id": "r1", "authorized_atoms": ["A", "B"]},
                      {"id": "r2", "authorized_atoms": ["A"]}],
        }
        case = parse_case(payload)
        result = solve(case)
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_extra, 0)
        self.assertEqual(result.total_new_labels, 1)
        self.assertEqual(result.selection, {"OLD-PII": "rename-1"})
        view = result.objects[0]
        self.assertEqual(view["old_atoms"], view["new_atoms"])
        self.assertTrue(all(r["unchanged"] for r in view["role_decisions"]))

    # ------------------------------------------------------------------
    # 题目点名场景 4：无解
    # ------------------------------------------------------------------
    def test_infeasible_when_every_choice_breaks_a_role(self):
        payload = self.base_case(include_plan=False)
        payload["objects"] = [{"id": "obj-x", "labels": ["L1"]}]
        payload["roles"] = [{"id": "r-locked", "authorized_atoms": ["A"]}]
        # 两个候选：N1={A} 可行；这里把唯一可行候选删掉，全部含 D
        payload["mappings"][0]["candidates"] = [
            {"id": "b1", "new_labels": ["N5"]},       # {A,D}
        ]
        case = parse_case(payload)
        result = solve(case)
        self.assertFalse(result.feasible)
        self.assertIsNotNone(result.reason)

    # ------------------------------------------------------------------
    # 优化目标：先额外代价，再新标签宽度，最后新标签 ID 字典序
    # ------------------------------------------------------------------
    def test_primary_objective_minimizes_total_extra(self):
        payload = self.base_case(include_plan=False)
        case = parse_case(payload)
        result = solve(case)
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_extra, 1)
        # 逐对象核对额外原子
        per = {o["object"]: o for o in result.objects}
        self.assertEqual(per["obj-3"]["extra_atoms"], ["A"])
        for oid in ("obj-1", "obj-2", "obj-4"):
            self.assertEqual(per[oid]["extra_count"], 0)

    def test_secondary_objective_minimizes_new_label_width(self):
        payload = {
            "case_id": "width",
            "now": 1,
            "atoms": ["A"],
            "old_labels": [{"id": "L1", "atoms": ["A"]}],
            "new_labels": [
                {"id": "NA", "atoms": ["A"]},
                {"id": "NB", "atoms": ["A"]},
            ],
            "mappings": [{
                "old_label": "L1",
                "candidates": [
                    {"id": "two-labels", "new_labels": ["NA", "NB"]},
                    {"id": "one-label", "new_labels": ["NB"]},
                ],
            }],
            "objects": [{"id": "o1", "labels": ["L1"]}],
            "roles": [{"id": "r", "authorized_atoms": ["A"]}],
        }
        result = solve(parse_case(payload))
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_extra, 0)
        # 两候选额外代价同为 0，宽度 1 优于 2
        self.assertEqual(result.selection, {"L1": "one-label"})
        self.assertEqual(result.total_new_labels, 1)

    def test_tertiary_tie_break_uses_new_label_ids(self):
        payload = {
            "case_id": "tie",
            "now": 1,
            "atoms": ["A"],
            "old_labels": [{"id": "L1", "atoms": ["A"]}],
            "new_labels": [
                {"id": "ZZZ", "atoms": ["A"]},
                {"id": "AAA", "atoms": ["A"]},
            ],
            "mappings": [{
                "old_label": "L1",
                # 注意候选登记顺序：宽 ID 在前，验证决胜不依赖登记顺序
                "candidates": [
                    {"id": "cand-z", "new_labels": ["ZZZ"]},
                    {"id": "cand-a", "new_labels": ["AAA"]},
                ],
            }],
            "objects": [{"id": "o1", "labels": ["L1"]}],
            "roles": [{"id": "r", "authorized_atoms": ["A"]}],
        }
        result = solve(parse_case(payload))
        self.assertTrue(result.feasible)
        # 代价/宽度完全持平，按新标签 ID 字典序 AAA < ZZZ
        self.assertEqual(result.selection, {"L1": "cand-a"})

    def test_deterministic_across_runs(self):
        payload = self.base_case(include_plan=False)
        r1 = solve(parse_case(copy.deepcopy(payload)))
        r2 = solve(parse_case(copy.deepcopy(payload)))
        self.assertEqual(r1.selection, r2.selection)
        self.assertEqual(r1.total_extra, r2.total_extra)
        self.assertEqual(r1.total_new_labels, r2.total_new_labels)

    # ------------------------------------------------------------------
    # 掩码语义：标签重叠并集、无角色可读对象不产生兼容约束
    # ------------------------------------------------------------------
    def test_overlapping_labels_union_and_mask_dedup(self):
        payload = {
            "case_id": "overlap",
            "now": 1,
            "atoms": ["A", "B"],
            "old_labels": [
                {"id": "L1", "atoms": ["A", "B"]},
                {"id": "L2", "atoms": ["B"]},
            ],
            "new_labels": [
                {"id": "N1", "atoms": ["A", "B"]},
                {"id": "N2", "atoms": ["B"]},
            ],
            "mappings": [
                {"old_label": "L1", "candidates": [
                    {"id": "m1", "new_labels": ["N1"]}]},
                {"old_label": "L2", "candidates": [
                    {"id": "m2", "new_labels": ["N2"]}]},
            ],
            "objects": [{"id": "o1", "labels": ["L1", "L2"]}],
            "roles": [{"id": "r", "authorized_atoms": ["A", "B"]}],
        }
        result = solve(parse_case(payload))
        self.assertTrue(result.feasible)
        view = result.objects[0]
        self.assertEqual(view["old_atoms"], ["A", "B"])
        self.assertEqual(view["new_atoms"], ["A", "B"])
        self.assertEqual(view["extra_count"], 0)

    def test_object_no_role_can_read_has_no_compat_constraint(self):
        payload = self.base_case(include_plan=False)
        # 唯一角色无权读任何含 A 的对象；对象带 L1={A}，旧结论本就是拒绝。
        # 迁移后即使超标也不能把“拒绝”变成别的（拒绝→拒绝仍兼容）。
        payload["objects"] = [{"id": "obj-x", "labels": ["L1"]}]
        payload["roles"] = [{"id": "r-none", "authorized_atoms": []}]
        payload["mappings"][0]["candidates"] = [
            {"id": "only-wide", "new_labels": ["N5"]},  # {A,D}
        ]
        result = solve(parse_case(payload))
        self.assertTrue(result.feasible)
        view = result.objects[0]
        decision = view["role_decisions"][0]
        self.assertFalse(decision["old_allow"])
        self.assertFalse(decision["new_allow"])
        self.assertTrue(decision["unchanged"])
