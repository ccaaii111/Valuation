"""Regression tests for the measured pretax cost of debt, runnable with plain python.

    python tests/test_cost_of_debt.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import (  # noqa: E402
    PRETAX_COST_OF_DEBT,
    build_ladder,
    debt_total,
    measure_cost_of_debt,
)
from backend.model.dcf import evaluate, inputs_from_ladder  # noqa: E402
from backend.model import periods as P  # noqa: E402
from fixtures import YI, synthetic_snapshot  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0


def ladder(interest_expense: float | None = None, *, balance=None, include_lease_debt: bool = True) -> dict:
    """Same JSON round-trip the stored baselines go through; `balance` rewrites the balance rows."""
    snapshot = synthetic_snapshot(interest_expense=interest_expense)
    if balance is not None:
        snapshot["balance"] = balance(list(snapshot["balance"]))
    built = build_ladder(
        snapshot, snapshot["consensus"], snapshot["quote"], 0.02, include_lease_debt=include_lease_debt,
        beta_manual=1.00,  # 本套件不测 β；没有价格快照就得显式给一个，见 fixtures.synthetic_ladder
    )
    return json.loads(json.dumps(built.to_json(), ensure_ascii=False))


def row(lad: dict, key: str) -> dict:
    return next(r for g in lad["groups"] for r in g["rows"] if r["key"] == key)


def close(value, expected: float) -> bool:
    return value is not None and abs(value - expected) < 5e-7


# 债务：2025 年 30+170+25=225 亿，2024 年 220 亿，平均 222.5 亿；营收 1000 亿 → 护栏取 5 亿。
# 利息 8.9 亿 ÷ 222.5 亿 = 4.00%；剔除租赁后分母 200 亿 → 4.45%。
MEASURED = 8.9


def test_debt_total_follows_the_lease_switch() -> None:
    head = P.latest(synthetic_snapshot()["balance"])
    check("含租赁的债务口径", close(debt_total(head), 225 * YI), str(debt_total(head) / YI))
    check("剔除租赁后少一档", close(debt_total(head, False), 200 * YI), str(debt_total(head, False) / YI))
    check("无科目行不编造", debt_total(None) is None, str(debt_total(None)))
    check("全空行返回 None", debt_total({"MONETARYFUNDS": 1.0}) is None)


def test_measured_kd_replaces_the_convention() -> None:
    lad = ladder(MEASURED)
    r = row(lad, "cost_of_debt_pretax")
    check("实测值入表", close(r["baseline"], 0.04), str(r["baseline"]))
    check("实测行算数据而非惯例", r["kind"] == "data", r["kind"])
    check("来源写明分子分母", "利息费用" in r["source"] and "平均有息负债" in r["source"], r["source"])
    check("凭证留痕含资本化利息偏差", "未扣除资本化利息" in r["evidence"], r["evidence"])
    plain = row(ladder(), "cost_of_debt_pretax")
    check("无利息科目时沿用惯例值", close(plain["baseline"], PRETAX_COST_OF_DEBT), str(plain["baseline"]))
    check("惯例行仍标为 convention", plain["kind"] == "convention", plain["kind"])
    check("回退说明写进凭证", "实测被拒" in plain["evidence"] and "没有利息支出科目" in plain["evidence"], plain["evidence"])
    check("说明只标档位不复述拒绝理由", "本行取惯例值" in plain["note"] and "实测被拒" not in plain["note"], plain["note"])


def test_wacc_consumes_the_measured_kd() -> None:
    lad = ladder(MEASURED)
    weight = row(lad, "equity_weight")["baseline"]
    expected = weight * 0.08 + (1 - weight) * 0.04 * 0.75
    check("WACC 随实测债务成本重算", close(row(lad, "wacc")["baseline"], expected),
          f"{row(lad, 'wacc')['baseline']} vs {round(expected, 6)}")
    check("股权成本不受债务成本影响", close(row(lad, "cost_of_equity")["baseline"], 0.08),
          str(row(lad, "cost_of_equity")["baseline"]))
    check("权益权重大于 0.9", weight > 0.9, str(weight))


def test_engine_sees_the_measured_kd() -> None:
    measured = inputs_from_ladder(ladder(MEASURED))
    convention = inputs_from_ladder(ladder())
    check("实测值流入 DCF", close(measured["cost_of_debt_pretax"], 0.04), str(measured["cost_of_debt_pretax"]))
    fcff = evaluate(measured, "fcff", "gordon")
    lower = evaluate({**measured, "cost_of_debt_pretax": 0.02}, "fcff", "gordon")
    check("债务成本越高每股越低", fcff.per_share < lower.per_share, f"{fcff.per_share} vs {lower.per_share}")
    check("两条口径的每股确实不同", convention["cost_of_debt_pretax"] != measured["cost_of_debt_pretax"])


def test_guards_reject_bad_ratios() -> None:
    debt = 222.5 * YI
    cases = [
        ("分子缺失", (None, debt, 1000 * YI), "没有利息支出科目"),
        ("分子为负", (-1.0 * YI, debt, 1000 * YI), "非正"),
        ("分子为零", (0.0, debt, 1000 * YI), "非正"),
        ("分母缺失", (8.9 * YI, None, 1000 * YI), "没有有息负债可作分母"),
        ("分母为零", (8.9 * YI, 0.0, 1000 * YI), "没有有息负债可作分母"),
        ("负债近零（营收护栏）", (8.9 * YI, 4.0 * YI, 1000 * YI), "低于护栏"),
        ("负债近零（绝对护栏）", (8.9 * YI, 0.5 * YI, 20 * YI), "低于护栏"),
        ("比值偏高", (30.0 * YI, debt, 1000 * YI), "越出"),
        ("比值偏低", (1.0 * YI, debt, 1000 * YI), "越出"),
    ]
    for label, args, reason in cases:
        value, evidence = measure_cost_of_debt(*args)
        check(f"护栏拦下：{label}", value is None and reason in evidence, evidence)
    ok, evidence = measure_cost_of_debt(8.9 * YI, debt, None)
    check("缺营收时仍按绝对护栏放行", close(ok, 0.04) and "未扣除" in evidence, evidence)
    at_floor, at_floor_note = measure_cost_of_debt(0.2 * YI, 5.0 * YI, 1000 * YI)
    check("恰好触及护栏仍采纳", close(at_floor, 0.04), f"{at_floor} {at_floor_note}")
    at_high, _ = measure_cost_of_debt(0.005 * debt, debt, 1000 * YI)
    check("区间下界恰好采纳", close(at_high, 0.005), str(at_high))


def test_near_zero_debt_company_falls_back() -> None:
    thin = lambda rows: [dict(r, SHORT_LOAN=0.4 * YI, LONG_LOAN=None, LEASE_LIAB=None) for r in rows]  # noqa: E731
    r = row(ladder(MEASURED, balance=thin), "cost_of_debt_pretax")
    check("近零负债不产出离谱利率", close(r["baseline"], PRETAX_COST_OF_DEBT), str(r["baseline"]))
    check("拒绝理由写进凭证", "低于护栏" in r["evidence"], r["evidence"])
    check("拒绝时仍标为 convention", r["kind"] == "convention", r["kind"])


def test_missing_prior_year_uses_period_end() -> None:
    only_head = lambda rows: [r for r in rows if "2024-12-31" not in str(r["REPORT_DATE"])]  # noqa: E731
    lad = ladder(9.0, balance=only_head)
    r = row(lad, "cost_of_debt_pretax")
    check("单点分母=期末值", close(r["baseline"], 9.0 / 225), str(r["baseline"]))
    check("凭证说清分母退化", "缺上年同期，分母取期末值" in r["evidence"], r["evidence"])


def test_denominator_follows_the_lease_switch() -> None:
    check("含租赁 4.00%", close(row(ladder(MEASURED), "cost_of_debt_pretax")["baseline"], 0.04))
    ex = row(ladder(MEASURED, include_lease_debt=False), "cost_of_debt_pretax")["baseline"]
    check("剔除租赁 4.45%", close(ex, MEASURED / 200), str(ex))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
