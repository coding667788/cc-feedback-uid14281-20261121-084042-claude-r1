#!/usr/bin/env bash
# 一键复现：在临时库上依次执行 plan / audit / update（含幂等重放）。
# 用法：bash run_examples.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DB="$ROOT/.runtime-tmp/demo_repro.sqlite"
mkdir -p "$ROOT/.runtime-tmp"
rm -f "$DB"

echo "== 1. plan（纯计算，最优候选选择） =="
python -m solution plan "$ROOT/examples/demo_case.json"

echo
echo "== 2. plan --db（登记计划到 SQLite） =="
python -m solution plan "$ROOT/examples/demo_case.json" --db "$DB"

echo
echo "== 3. audit（逐对象逐角色核对） =="
python -m solution audit "$ROOT/examples/demo_case.json"

echo
echo "== 4. update --db（原子切换，保留历史判定） =="
python -m solution update "$ROOT/examples/demo_case.json" --db "$DB"

echo
echo "== 5. update 重放（故障后重启，幂等不二次写入） =="
python -m solution update "$ROOT/examples/demo_case.json" --db "$DB"

echo
echo "== 6. 无解用例（退出码 2 + E_INFEASIBLE） =="
python -m solution plan "$ROOT/examples/infeasible_case.json" || true

echo
echo "== 7. 过期计划（退出码 2 + E_PLAN_EXPIRED） =="
python -m solution audit "$ROOT/examples/expired_plan_case.json" || true

echo
echo "== 8. 单元测试 =="
python -m unittest discover -s tests -v
