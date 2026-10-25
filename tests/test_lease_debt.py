"""Regression tests for the lease-as-debt caliber, runnable with plain python.

    python tests/test_lease_debt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import LEASE_DEBT_KEYS, _sum  # noqa: E402
from backend.model.dcf import apply_overrides, evaluate, inputs_from_ladder  # noqa: E402
from backend.model.periods import twelve_months_ago  # noqa: E402
from fixtures import LEASE_LIAB, synthetic_ladder  # noqa: E402

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
    check("FCFE 每年多出租赁净融资", all(abs(p.fcfe - q.fcfe - 100.0) < 1e-6 for p, q in zip(with_lease.lines, without.lines)),
          f"{[round(p.fcfe - q.fcfe, 6) for p, q in zip(with_lease.lines, without.lines)]}")
    fcff_with = evaluate({**BASE, "lease_financing": 100.0}, "fcff", "gordon")
    fcff_plain = evaluate({**BASE, "lease_financing": 0.0}, "fcff", "gordon")
    check("FCFF 不受租赁净融资影响", fcff_with.per_share == fcff_plain.per_share, f"{fcff_with.per_share} vs {fcff_plain.per_share}")


def test_debt_base_shares_one_interest_bill() -> None:
    """The lease sits in 有息负债, so interest accrues on it and it is not financed twice."""
    levered = evaluate(BASE, "fcfe", "gordon", rate=0.09)
    plain = evaluate(EXCLUDED, "fcfe", "gordon", rate=0.09)
    interest = levered.lines[0].interest - plain.lines[0].interest
    expected = (BASE["interest_bearing_debt"] - EXCLUDED["interest_bearing_debt"]) * BASE["cost_of_debt_pretax"]
    check("每年利息按含租赁的债务基数计提", abs(interest - expected) < 1e-6, f"利息差 {interest:.3f} vs {expected:.3f}")


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
