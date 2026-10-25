"""Run every model test suite:  python tests/run_all.py

Python 那几套件只看算术与接口，看不见页面渲染出来的 HTML——收尾再跑一次 DOM 哨兵 `tests/dom_derivation_check.js`。
它要面板在跑、`runs/` 里有 600519 的批次；这两样不齐时它退出码 2，这里就当跳过，不算失败（离线开发不该被面板绑住）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SUITES = sorted(p.name for p in Path(__file__).parent.glob("test_*.py"))
HERE = Path(__file__).resolve().parent

failed = []
for name in SUITES:
    print(f"\n===== {name} =====")
    if subprocess.run([sys.executable, str(HERE / name)]).returncode:
        failed.append(name)

print("\n===== dom_derivation_check.js =====")
node = shutil.which("node")
if not node:
    print("跳过：没找到 node")
    dom_note = "没找到 node"
else:
    code = subprocess.run([node, str(HERE / "dom_derivation_check.js")]).returncode
    dom_note = {0: "通过", 1: "有 FAIL"}.get(code, "跳过：面板没开或这批不存在")
    if code == 1:
        failed.append("dom_derivation_check.js")

print(f"\n执行 {len(SUITES)} 个套件: {', '.join(SUITES)}")
print(f"DOM 哨兵: {dom_note}")
print(f"失败套件: {failed or '无'}")
raise SystemExit(1 if failed else 0)
