"""Regression tests for 单行敏感性归因 — the semantics the assumption panel displays.

    python tests/test_attribution.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.attribution import attribute, flat_rows  # noqa: E402
from backend.model.dcf import ENGINE_INPUTS, apply_overrides, evaluate, inputs_from_ladder  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


LADDER = synthetic_ladder()
INPUTS = inputs_from_ladder(LADDER)


def row_of(report: dict, key: str) -> dict:
    return next(r for r in report["rows"] if r["key"] == key)


def test_untouched_ladder_is_the_baseline() -> None:
    report = attribute(LADDER)
    check("无修正时逐行均标记为未改动", all(not r["changed"] for r in report["rows"]))
    check("无修正时改动行数为 0", report["edited_row_count"] == 0)
    check("无修正时每股值等于直接计算", report["per_share"] == round(evaluate(INPUTS).per_share, 4))
    check("无修正时归因合计为 0", report["contribution_sum_pct"] in (0.0, None), str(report["contribution_sum_pct"]))
    check("每一行都可被面板读取", len(report["rows"]) == len(flat_rows(LADDER)), f"{len(report['rows'])} 行")


def test_single_edit_contribution_equals_total_change() -> None:
    report = attribute(LADDER, {"terminal_growth": 0.015})
    row = row_of(report, "terminal_growth")
    check("单行改动被标记", row["changed"] and row["editable"])
    check("单行归因等于总变动", abs(row["contribution_pct"] - report["total_change_pct"]) < 1e-6,
          f"{row['contribution_pct']} vs {report['total_change_pct']}")
    check("回退该行即回到基线", abs(row["value_if_reverted"] - report["baseline_per_share"]) < 1e-6,
          f"{row['value_if_reverted']} vs {report['baseline_per_share']}")
    check("改动百分比按基线计算", abs(row["change_pct"] - (0.015 / INPUTS["terminal_growth"] - 1.0)) < 1e-6)


def test_two_edits_are_not_additive() -> None:
    report = attribute(LADDER, {"terminal_growth": 0.015, "erp": INPUTS["erp"] + 0.01})
    total = report["total_change_pct"]
    check("两行改动计数", report["edited_row_count"] == 2)
    check("归因合计与总变动不相等（不可加）", abs(report["contribution_sum_pct"] - total) > 1e-4,
          f"合计 {report['contribution_sum_pct']} vs 总 {total}")


def test_year_map_reports_per_year_change() -> None:
    report = attribute(LADDER, {"revenue_growth_path": {"2029": 0.0}})
    row = row_of(report, "revenue_growth_path")
    moved = {k: v for k, v in row["change_pct"].items() if v}
    check("只改一年时其余年改动为 0", moved == {"2029": -1.0}, str(moved))
    check("未点名的年份保留基线值", row["current"]["2026"] == INPUTS["revenue_growth_path"]["2026"])
    check("该改动确实压低了价值", report["per_share"] < report["baseline_per_share"])


def test_rate_primitives_cascade_into_value() -> None:
    report = attribute(LADDER, {"beta": 1.4})
    row = row_of(report, "beta")
    now_equity, now_wacc = (series[1] for series in (report["rates"]["cost_of_equity"], report["rates"]["wacc"]))
    check("β 行现在是引擎输入", row["editable"] is True)
    check("β 改动被定价", row["contribution_pct"] is not None and row["contribution_pct"] != 0.0, str(row["contribution_pct"]))
    check("β 改动登记为已定价", report["unpriced_edits"] == [], str(report["unpriced_edits"]))
    check("股权成本随 β 重算", abs(now_equity - (INPUTS["risk_free"] + 1.4 * INPUTS["erp"])) < 1e-9, f"{now_equity}")
    check("基线股权成本等于存量", abs(report["rates"]["cost_of_equity"][0] - INPUTS["cost_of_equity"]) < 1e-9)
    check("WACC 随 β 一起动", abs(now_wacc - report["rates"]["wacc"][0]) > 1e-6, f"{report['rates']['wacc']}")
    check("回退 β 即回到基线折现率", abs(row["rate_if_reverted"] - INPUTS["wacc"]) < 1e-9, f"{row['rate_if_reverted']}")
    check("改 β 后每股值随折现率下移", report["per_share"] < report["baseline_per_share"])

    tax = attribute(LADDER, {"tax_rate_ltm": INPUTS["tax_rate_ltm"] + 0.10})
    check("税率改动只动 WACC 不动股权成本", tax["rates"]["cost_of_equity"][0] == tax["rates"]["cost_of_equity"][1],
          str(tax["rates"]))
    check("税率改动进 WACC", tax["rates"]["wacc"][1] != INPUTS["wacc"], str(tax["rates"]["wacc"]))


def test_unpriced_edits_are_declared() -> None:
    report = attribute(LADDER, {"da_to_revenue_hist": 0.99})
    row = row_of(report, "da_to_revenue_hist")
    check("未被引擎读取的行标记为不可编辑", row["editable"] is False)
    check("登记为未定价", report["unpriced_edits"] == ["da_to_revenue_hist"], str(report["unpriced_edits"]))
    check("未定价行不给归因", row["contribution_pct"] is None)
    check("未定价时每股值不变", report["per_share"] == report["baseline_per_share"])


def test_derived_rows_are_not_editable() -> None:
    report = attribute(LADDER)
    for key in ("wacc", "cost_of_equity"):
        row = row_of(report, key)
        check(f"{key} 行标注为级联", row["kind"] == "derived" and row["editable"] is False, str(row["kind"]))
        check(f"{key} 来源写明级联", row["source"].startswith("级联"), row["source"])


def test_run_conventions_propagate() -> None:
    for rate in (None, 0.09):
        for mid_year in (None, False):
            report = attribute(LADDER, {"terminal_growth": 0.02}, mid_year=mid_year, rate=rate)
            # The panel's number must match what the engine produces from the merged-and-re-derived inputs,
            # not from a hand-patched flat dict — the cascade is part of the口径 now.
            direct = evaluate(apply_overrides(INPUTS, {"terminal_growth": 0.02}), "fcff", "gordon", rate=rate, mid_year=mid_year)
            check(
                f"面板口径同步 rate={rate} mid_year={mid_year}",
                abs(report["per_share"] - direct.per_share) < 1e-3,
                f"{report['per_share']} vs {round(direct.per_share, 4)}",
            )
    check("归因表带出折现明细行", len(attribute(LADDER)["schedule"]) == len(INPUTS["revenue_growth_path"]))
    check("可编辑判定与引擎输入集合一致", {r["key"] for r in attribute(LADDER)["rows"] if r["editable"]} == set(ENGINE_INPUTS))


def test_derived_badge_tracks_the_engine_not_the_archive() -> None:
    """级联牌只说「这次改动把它挪了」：存档取整留下的最后一位差，不该在零改动时把两行点亮。"""
    archived = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in archived["groups"]:
        for row in group["rows"]:
            if row["key"] in ("wacc", "cost_of_equity"):
                row["baseline"] = round(row["baseline"] + 1e-6, 8)
    check("零改动时一个级联牌都不挂", not any(r["recomputed"] for r in attribute(archived)["rows"]))
    quiet = row_of(attribute(archived), "wacc")
    check("存档最后一位的舍入不在改动列读成假百分比", quiet["change_pct"] in (0.0, None), str(quiet["change_pct"]))
    only_g = attribute(LADDER, {"terminal_growth": 0.02})
    for key in ("wacc", "cost_of_equity"):
        check(f"只改 g 时 {key} 不挂级联牌（它没动）", not row_of(only_g, key)["recomputed"],
              f"{row_of(only_g, key)['baseline']} vs {row_of(only_g, key)['current']}")
    beta = attribute(LADDER, {"beta": 1.4})
    for key in ("wacc", "cost_of_equity"):
        check(f"改 β 时 {key} 挂上级联牌", row_of(beta, key)["recomputed"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
