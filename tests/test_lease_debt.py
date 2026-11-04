"""Regression tests for the lease-as-debt caliber, runnable with plain python.

    python tests/test_lease_debt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import LEASE_DEBT_KEYS, _sum, build_ladder  # noqa: E402
from backend.model.dcf import apply_overrides, evaluate, inputs_from_ladder  # noqa: E402
from backend.model.periods import ltm, ltm_flow, twelve_months_ago  # noqa: E402
from fixtures import LEASE_LIAB, YI, synthetic_ladder, synthetic_snapshot  # noqa: E402

LADDER = synthetic_ladder()
BASE = inputs_from_ladder(LADDER)
EXCLUDED = inputs_from_ladder(synthetic_ladder(include_lease_debt=False))


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0


def test_lease_enters_the_debt_base() -> None:
    check("租赁负债计入有息负债", BASE["interest_bearing_debt"] == 200 + LEASE_LIAB[2025], str(BASE["interest_bearing_debt"]))
    check("净债务随之变小", BASE["net_debt"] == 200 + LEASE_LIAB[2025] - 120, str(BASE["net_debt"]))
    check("开关关掉后回到旧口径", EXCLUDED["interest_bearing_debt"] == 200.0, str(EXCLUDED["interest_bearing_debt"]))
    check("排除时不产出租赁净融资行", "lease_financing" not in EXCLUDED, str(sorted(k for k in EXCLUDED if "lease" in k)))
    row = next(r for g in LADDER["groups"] for r in g["rows"] if r["key"] == "interest_bearing_debt")
    check("有息负债行写明含租赁", "租赁负债" in row["source"], row["source"])


def test_lease_financing_is_the_twelve_month_delta() -> None:
    check("净融资等于同比变化", BASE["lease_financing"] == LEASE_LIAB[2025] - LEASE_LIAB[2024], str(BASE["lease_financing"]))
    rows = [{"REPORT_DATE": "2025-06-30", "LEASE_LIAB": 1563.05e8}, {"REPORT_DATE": "2025-12-31", "LEASE_LIAB": 1574.07e8},
            {"REPORT_DATE": "2026-06-30", "LEASE_LIAB": 1563.05e8}]
    check("取同季末而非上年末", twelve_months_ago(rows, *LEASE_DEBT_KEYS) == 1563.05e8, "应命中 2025-06-30")
    check("缺同期日期时返回 None", twelve_months_ago(rows[:1], *LEASE_DEBT_KEYS) is None)
    check("报表无该科目时不编造", _sum({"SHORT_LOAN": 1.0}, LEASE_DEBT_KEYS) is None)


def test_lease_financing_only_moves_fcfe() -> None:
    with_lease = evaluate({**BASE, "lease_financing": 100.0}, "fcfe", "gordon", rate=0.09)
    without = evaluate({**BASE, "lease_financing": 0.0}, "fcfe", "gordon", rate=0.09)
    tax, kd = with_lease.params["tax"], with_lease.params["cost_of_debt"]
    deltas = [p.fcfe - q.fcfe for p, q in zip(with_lease.lines, without.lines)]
    expected = [100.0 - 100.0 * (1.0 + i) * kd * (1.0 - tax) for i in range(len(deltas))]
    check("FCFE 多出租赁净融资，也多付这笔债务的税后利息", all(abs(d - e) < 1e-6 for d, e in zip(deltas, expected)),
          f"{[round(d, 6) for d in deltas]}")
    fcff_with = evaluate({**BASE, "lease_financing": 100.0}, "fcff", "gordon")
    fcff_plain = evaluate({**BASE, "lease_financing": 0.0}, "fcff", "gordon")
    check("FCFF 不受租赁净融资影响", fcff_with.per_share == fcff_plain.per_share, f"{fcff_with.per_share} vs {fcff_plain.per_share}")


def test_debt_base_shares_one_interest_bill() -> None:
    """The lease sits in 有息负债, so interest accrues on it and it is not financed twice."""
    levered = evaluate(BASE, "fcfe", "gordon", rate=0.09)
    plain = evaluate(EXCLUDED, "fcfe", "gordon", rate=0.09)
    interest = levered.lines[0].interest - plain.lines[0].interest
    expected = ((BASE["interest_bearing_debt"] - EXCLUDED["interest_bearing_debt"]) + BASE["lease_financing"]) \
        * BASE["cost_of_debt_pretax"]
    check("利息按含租赁的债务基数与当年租赁净融资计", abs(interest - expected) < 1e-6, f"利息差 {interest:.3f} vs {expected:.3f}")


def test_bond_issuance_enters_net_financing() -> None:
    snapshot = synthetic_snapshot()
    for row in snapshot["cashflow"]:
        row["ISSUE_BOND"] = 7 * YI if row["REPORT_DATE"] == "2025-12-31" else 0.0
    built = build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.0)
    row = next(r for r in built.rows() if r.key == "net_borrowing")
    check("发债收现并入净债务融资", row.baseline == 20 - 15 + 7, str(row.baseline))
    check("标签不再是只算借款", row.label == "净债务融资", row.label)
    check("凭证写明三笔收付", "发债收现 7.00 亿元" in row.evidence, row.evidence)


def test_bond_roll_treats_blank_as_no_issuance() -> None:
    """002156 的形态：本年三季报报了 5 亿发债，上年年报与上年同期留空。"""
    rows = [
        {"REPORT_DATE": "2026-09-30", "ISSUE_BOND": 5 * YI},
        {"REPORT_DATE": "2025-12-31"},
        {"REPORT_DATE": "2025-09-30"},
    ]
    check("空值按没发生处理，滚动仍出数", ltm_flow(rows, "ISSUE_BOND") == 5 * YI, str(ltm_flow(rows, "ISSUE_BOND")))
    check("严格口径会把这十二个月判成缺失", ltm(rows, "ISSUE_BOND") is None)
    check("本期也没报数时不出数",
          ltm_flow([{"REPORT_DATE": "2026-09-30"}, {"REPORT_DATE": "2025-12-31", "ISSUE_BOND": 3 * YI}], "ISSUE_BOND") is None)


def test_lease_financing_is_editable() -> None:
    bumped = apply_overrides(BASE, {"lease_financing": 40.0})
    check("面板可改租赁净融资", bumped["lease_financing"] == 40.0, str(bumped.get("lease_financing")))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
