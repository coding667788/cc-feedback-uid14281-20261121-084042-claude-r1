"""测试公共工具：构造 CASE 字典、调用 CLI、管理临时 SQLite 库。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from solution.cli import main as cli_main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTestCase(unittest.TestCase):
    """通过 ``python -m solution`` 子进程跑命令，验证真实命令行契约。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_case(self, name: str, payload: dict) -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def db_path(self, name: str = "state.sqlite") -> str:
        return os.path.join(self.tmpdir, name)

    def run_cli(self, *args: str) -> tuple[int, dict]:
        # 进程内调用 CLI 入口：unittest 的 sys.path 已含仓库根，且与
        # `python -I -m solution` 走同一条 main() 代码路径（同一 JSON 契约）。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(list(args))
        payload = json.loads(buf.getvalue())
        return code, payload

    # ---- 常用夹具 --------------------------------------------------------
    def base_case(self, include_plan: bool = True, **overrides) -> dict:
        payload = {
            "case_id": "unit",
            "now": 1000,
            "old_version": "v1",
            "new_version": "v2",
            "atoms": ["A", "B", "C", "D"],
            "old_labels": [
                {"id": "L1", "atoms": ["A"]},
                {"id": "L2", "atoms": ["B"]},
                {"id": "L3", "atoms": ["C"]},
                {"id": "L4", "atoms": ["D"]},
            ],
            "new_labels": [
                {"id": "N1", "atoms": ["A"]},
                {"id": "N2", "atoms": ["B", "C"]},
                {"id": "N3", "atoms": ["B"]},
                {"id": "N4", "atoms": ["C"]},
                {"id": "N5", "atoms": ["A", "D"]},
            ],
            "mappings": [
                {"old_label": "L1", "candidates": [
                    {"id": "L1-m1", "new_labels": ["N1"]},
                    {"id": "L1-m2", "new_labels": ["N5"]}]},
                {"old_label": "L2", "candidates": [
                    {"id": "L2-m1", "new_labels": ["N3"]},
                    {"id": "L2-m2", "new_labels": ["N2"]}]},
                {"old_label": "L3", "candidates": [
                    {"id": "L3-m1", "new_labels": ["N4"]},
                    {"id": "L3-m2", "new_labels": ["N2"]}]},
                {"old_label": "L4", "candidates": [
                    {"id": "L4-m1", "new_labels": ["N5"]}]},
            ],
            "objects": [
                {"id": "obj-1", "labels": ["L1"]},
                {"id": "obj-2", "labels": ["L2", "L3"]},
                {"id": "obj-3", "labels": ["L2", "L4"]},
                {"id": "obj-4", "labels": ["L3"]},
            ],
            "roles": [
                {"id": "r-analyst", "authorized_atoms": ["A", "B", "C"]},
                {"id": "r-admin",
                 "authorized_atoms": ["A", "B", "C", "D"]},
            ],
            "plan": {
                "plan_id": "plan-1",
                "created_at": 900,
                "valid_until": 2000,
                "selection": [
                    {"old_label": "L1", "candidate_id": "L1-m1"},
                    {"old_label": "L2", "candidate_id": "L2-m1"},
                    {"old_label": "L3", "candidate_id": "L3-m1"},
                    {"old_label": "L4", "candidate_id": "L4-m1"},
                ],
            },
        }
        if not include_plan:
            del payload["plan"]
        payload.update(overrides)
        return payload
