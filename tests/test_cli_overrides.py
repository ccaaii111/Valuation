"""Tests for the CLI override loader: 裸 map 和面板「保存修正」写出的 manual.json 包装都要认。

No network, no stored run — the wrapper documents go to a temp file, since loading from a path is
half of what's under test.

    python tests/test_cli_overrides.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.dcf import apply_overrides, inputs_from_ladder  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402
from run_valuation import load_overrides  # noqa: E402

FAILURES = 0
TMP = Path(tempfile.mkdtemp(prefix="qoder-cli-overrides-"))


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def _path(name: str, document: dict) -> str:
    file = TMP / name
    file.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return str(file)


def test_absent_input_and_bare_map() -> None:
    check("不给修正时为空", load_overrides(None) == ({}, {}) and load_overrides("") == ({}, {}))
    check("裸 map 原样返回", load_overrides('{"terminal_growth": 0.03}') == ({"terminal_growth": 0.03}, {}))


def test_manual_wrapper_unwraps_to_its_rows() -> None:
    path = _path(
        "manual.json",
        {
            "baseline": "20260923-213848",
            "saved_at": "2026-09-23T14:15:01Z",
            "overrides": {"total_shares": 42.69771, "revenue_ltm": 500.0},
            "method": "fcfe",
            "terminal": "uem",
            "rate": 0.09,
            "mid_year": None,
            "per_share": 50.0215,
        },
    )
    overrides, document = load_overrides(path)
    check("只取 overrides 行", overrides == {"total_shares": 42.69771, "revenue_ltm": 500.0}, str(overrides))
    check("包装字段另存不误并入", document.get("method") == "fcfe" and "overrides" not in document)
    inputs = apply_overrides(inputs_from_ladder(synthetic_ladder()), overrides)
    check("解出的行能进引擎", inputs["total_shares"] == 42.69771, str(inputs.get("total_shares")))


def test_wrapper_without_unwrap_would_be_rejected() -> None:
    """The loader earns its keep here: the wrapper's own keys are not ladder rows."""
    document = {
        "baseline": "x",
        "overrides": {"terminal_growth": 0.03},
        "method": "fcff",
        "terminal": "gordon",
        "rate": None,
        "mid_year": None,
        "per_share": 1.0,
    }
    raised = False
    try:
        apply_overrides(inputs_from_ladder(synthetic_ladder()), document)
    except ValueError:
        raised = True
    check("整份文档当裸 map 会被拒", raised)


def test_wrapper_shape_is_detected_by_the_overrides_key() -> None:
    """Any document whose `overrides` is an object reads as a saved panel scenario."""
    overrides, document = load_overrides('{"overrides": {"terminal_growth": 0.02}, "baseline": "y"}')
    check("认结构的判据是 overrides 为对象", overrides == {"terminal_growth": 0.02} and document["baseline"] == "y")
    bare, note = load_overrides('{"terminal_growth": 0.02}')
    check("没有 overrides 键就不解包", bare == {"terminal_growth": 0.02} and note == {})


if __name__ == "__main__":
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(f"[{name}]")
                fn()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
