"""Unit tests for the risk-free parser — no network, akshare is never imported.

    python tests/test_macro.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data.macro import MAX_PLAUSIBLE_PCT, RAW_NAME, SERIES, _numeric, parse  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def payload(*rows: tuple[str, float | None]) -> dict:
    return {"source": "akshare bond_zh_us_rate", "column": SERIES, "rows": [{"日期": d, SERIES: v} for d, v in rows]}


def test_percent_scaled_decimal() -> None:
    value, source = parse(payload(("2026-09-18", 1.6829), ("2026-09-19", 1.6850)))
    check("百分点换算为小数", value == 0.01685, str(value))
    check("来源含观测日期", "2026-09-19" in source and source.startswith("akshare"), source)
    check("来源保留原始百分点读数", "1.6850%" in source, source)


def test_date_order_ignored() -> None:
    value, source = parse(payload(("2026-09-19", 1.7), ("2026-09-18", 1.6), ("2026-09-17", 1.5)))
    check("乱序行取最新日期", value == 0.017, f"{value} {source}")


def test_trailing_gaps_skipped() -> None:
    value, _ = parse(payload(("2026-09-17", 1.62), ("2026-09-18", None), ("2026-09-19", None)))
    check("最新日期缺失时回退上一观测", value == 0.0162, str(value))


def test_zero_is_a_real_observation() -> None:
    value, source = parse(payload(("2026-09-19", 0.0)))
    check("零利率返回 0.0 而非 None", value == 0.0, source)


def test_no_rows() -> None:
    for label, body in (("空行", payload()), ("缺列", {"rows": [{"日期": "2026-09-19"}]}), ("空载荷", {})):
        value, source = parse(body)
        check(f"{label}返回 unavailable", value is None and source.startswith("unavailable"), source)


def test_unit_shift_refused() -> None:
    value, source = parse(payload(("2026-09-19", MAX_PLAUSIBLE_PCT * 10)))
    check("超界读数拒绝猜测", value is None and "单位" in source, source)


def test_numeric_coercion() -> None:
    check("字符串数值可解析", _numeric("1.6829") == 1.6829)
    check("None 归零失败", _numeric(None) is None)
    check("NaN 被挡下", _numeric(float("nan")) is None)


def test_raw_name_is_snapshot_key() -> None:
    """The run store writes this name into manifest.json — renaming it silently breaks replay."""
    check("快照键为 risk_free", RAW_NAME == "risk_free", RAW_NAME)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
