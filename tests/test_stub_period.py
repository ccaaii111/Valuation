"""首年残期：报表截止日之后的那一段时间，金额与折现期都只算那一段。

    python tests/test_stub_period.py

判据（docs/caliber.md §7 ⑤–⑦）：残期 = 基准日所在月之后还剩的整月数 ÷ 12；首年各列金额 × 残期；
折现期 t_首年 = 残期/2（年中）或残期（年末），t_j = 残期 + (j−1) − 0.5（年中）或 + (j−1)（年末），
t_终值 = 残期 + N − 1 − 0.5（年中）或 残期 + N − 1（年末）。没有截止日时残期取 1.0，整套数字
必须与引入残期之前逐字一致——那是下面第一组回归钉子的全部意义。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.model.dcf import (  # noqa: E402
    _as_of_day, apply_overrides, evaluate, inputs_from_ladder, stub_fraction,
)

YEARS = [2026, 2027, 2028, 2029, 2030]
FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def close(a: float | None, b: float, tol: float = 1e-6) -> bool:
    return a is not None and abs(a - b) < tol


def base_inputs(**overrides) -> dict:
    """与 test_dcf 同一块平板：营收 1000、EBIT 率 30%、D&A=CapEx、无营运资本变动、无债务、r=7.5%。
    每一年的 FCFF 都是 225，所以残期一乘就只有一个 112.5，手算无需查表。"""
    inputs = {
        "revenue_base": 1000.0,
        "revenue_growth_path": {year: 0.0 for year in YEARS},
        "consensus_revenue": {},
        "ebit_margin_path": {year: 0.30 for year in YEARS},
        "ebit_margin_ltm": 0.30,
        "tax_rate_ltm": 0.25,
        "da_to_revenue_ltm": 0.05,
        "capex_to_revenue_ltm": 0.05,
        "nwc_to_revenue_delta": 0.0,
        "interest_bearing_debt": 0.0,
        "cost_of_debt_pretax": 0.05,
        "net_borrowing": 0.0,
        "net_debt": 0.0,
        "minority_interest": 0.0,
        "wacc": 0.075,
        "cost_of_equity": 0.075,
        "terminal_growth": 0.025,
        "mid_year_discount": True,
        "total_shares": 100.0,
        "price": 40.0,
        "roic_ltm": 0.20,
    }
    inputs.update(overrides)
    return inputs


def test_month_mapping() -> None:
    """标准报告期落在季末，所以残期只能是 9/12、6/12、3/12；年报后首年已是下一个整年。"""
    cases = [
        ("2026-03-31", 2026, 0.75),
        ("2026-06-30", 2026, 0.5),
        ("2026-09-30", 2026, 0.25),
        ("2025-12-31", 2026, 1.0),   # 截止日在首年之前：整年都在未来
        ("2026-12-31", 2026, 1.0),   # 首年整体已过：留给新鲜度拦，这里不造零窗口
        ("2027-06-30", 2026, 1.0),   # 同上，且更晚
        (None, 2026, 1.0),           # 手搓 inputs、老批次
        ("不是一天", 2026, 1.0),     # 截止日读不出
    ]
    for stamp, first, want in cases:
        got = stub_fraction(_as_of_day(stamp), first)
        check(f"{stamp} 首年{first} → {want}", close(got, want), f"{got}")
    # 非常规截止日（并购重组的期中截止）：所在月不算，7 月整月归入已走过的一段
    check("2026-07-15 → 剩 5 个整月", close(stub_fraction(_as_of_day("2026-07-15"), 2026), 5 / 12))


def test_no_cutoff_is_byte_identical_to_the_old_engine() -> None:
    """没有截止日就没有残期：这套数与引入 stub 之前的手算完全相同。"""
    annual = evaluate(base_inputs(mid_year_discount=False), "fcff", "gordon")
    mid = evaluate(base_inputs(), "fcff", "gordon")
    check("年末折现每股 = 41.2320", close(annual.per_share, 41.2320, 1e-3), f"{annual.per_share:.4f}")
    check("年末折现 t = 1..5", [l.t for l in annual.lines] == [1.0, 2.0, 3.0, 4.0, 5.0], str([l.t for l in annual.lines]))
    check("年中折现 t = 0.5..4.5", [l.t for l in mid.lines] == [0.5, 1.5, 2.5, 3.5, 4.5], str([l.t for l in mid.lines]))
    check("终值折现期 4.5（年中）", close(mid.params["t_terminal"], 4.5), f"{mid.params['t_terminal']}")
    check("终值折现期 5（年末）", close(annual.params["t_terminal"], 5.0), f"{annual.params['t_terminal']}")
    check("残期取 1.0", close(mid.params["stub"], 1.0) and close(annual.params["stub"], 1.0))
    check("每年 FCFF 仍是 225", all(close(l.fcff, 225.0) for l in mid.lines))
    check("每行残期都是 1.0", all(close(l.stub, 1.0) for l in mid.lines))


def test_first_year_shrinks_and_the_rest_do_not() -> None:
    result = evaluate(base_inputs(report_as_of="2026-06-30"), "fcff", "gordon")
    check("首年 FCFF = 225 × 0.5", close(result.lines[0].fcff, 112.5), f"{result.lines[0].fcff}")
    check("第二年 FCFF 仍是整年 225", close(result.lines[1].fcff, 225.0), f"{result.lines[1].fcff}")
    check("首年营收也缩（884 之类的半年报出 500）", close(result.lines[0].revenue, 500.0), f"{result.lines[0].revenue}")
    check("首年 EBIT 率不缩（比率不是钱）", close(result.lines[0].ebit_margin, 0.30), f"{result.lines[0].ebit_margin}")
    check("首年行残期 0.5、其余 1.0", close(result.lines[0].stub, 0.5) and all(close(l.stub, 1.0) for l in result.lines[1:]))
    check("那句 stub 警告不再无条件出现", not any("stub" in w or "首年预测" in w for w in result.warnings), str(result.warnings))


def test_discount_axis_follows_the_window() -> None:
    """年中：首年站在窗口中点，其后各年接在窗口尾巴上；年末：首年就站在窗口末端。"""
    mid = evaluate(base_inputs(report_as_of="2026-06-30"), "fcff", "gordon")
    annual = evaluate(base_inputs(report_as_of="2026-06-30"), "fcff", "gordon", mid_year=False)
    check("年中 t = 0.25, 1.0, 2.0, 3.0, 4.0", [l.t for l in mid.lines] == [0.25, 1.0, 2.0, 3.0, 4.0], str([l.t for l in mid.lines]))
    check("年末 t = 0.5, 1.5, 2.5, 3.5, 4.5", [l.t for l in annual.lines] == [0.5, 1.5, 2.5, 3.5, 4.5], str([l.t for l in annual.lines]))
    check("年中终值期 = 0.5 + 5 − 1 − 0.5 = 4.0", close(mid.params["t_terminal"], 4.0), f"{mid.params['t_terminal']}")
    check("年末终值期 = 0.5 + 5 − 1 = 4.5", close(annual.params["t_terminal"], 4.5), f"{annual.params['t_terminal']}")
    q3 = evaluate(base_inputs(report_as_of="2026-09-30"), "fcff", "gordon")
    check("三季报后窗口只剩 3 个月", close(q3.params["stub"], 0.25))
    check("三季报后首年 t = 0.125", close(q3.lines[0].t, 0.125), f"{q3.lines[0].t}")
    check("三季报后首年 FCFF = 225 × 0.25", close(q3.lines[0].fcff, 56.25), f"{q3.lines[0].fcff}")


def test_window_only_shortens_the_first_year_never_the_growth_base() -> None:
    """ΔNWC 的上年基数必须是整年营收：首年被缩过就等于凭空给第二年造出一段增长。"""
    inputs = base_inputs(report_as_of="2026-06-30", nwc_to_revenue_delta=0.2,
                         revenue_growth_path={2026: 0.10, **{y: 0.0 for y in YEARS[1:]}})
    result = evaluate(inputs, "fcff", "gordon")
    check("首年整年营收 1100 → 窗口内 550", close(result.lines[0].revenue, 550.0), f"{result.lines[0].revenue}")
    check("第二年营收 1100（增速为 0）", close(result.lines[1].revenue, 1100.0), f"{result.lines[1].revenue}")
    check("第二年 ΔNWC = 0（不因为首年缩过而虚增）", close(result.lines[1].nwc_change, 0.0), f"{result.lines[1].nwc_change}")
    check("首年 ΔNWC = 0.2 × 100 × 0.5", close(result.lines[0].nwc_change, 10.0), f"{result.lines[0].nwc_change}")


def test_ladder_passes_the_report_date_through() -> None:
    """截止日存在批次顶层，不在任何假设行里；它必须跟着 inputs 走到引擎，且不接受人工改。"""
    ladder = {"as_of": "2026-09-30", "groups": [{"rows": [{"key": "wacc", "baseline": 0.075}]}]}
    inputs = inputs_from_ladder(ladder)
    check("inputs 带上 as_of", inputs.get("report_as_of") == "2026-09-30", str(inputs.get("report_as_of")))
    priced = evaluate(base_inputs(report_as_of="2026-09-30"), "fcff", "gordon")
    check("缺 as_of 的老批次不会崩", close(evaluate(base_inputs(), "fcff", "gordon").params["stub"], 1.0))
    check("截止日不是整年就影响估值", close(priced.params["stub"], 0.25))
    try:
        apply_overrides(base_inputs(report_as_of="2026-06-30"), {"report_as_of": "2026-12-31"})
        check("改截止日被拒", False, "竟然改成功了")
    except ValueError as exc:
        check("改截止日被拒并说明理由", "报表截止日" in str(exc), str(exc)[:60])


def test_cli_schedule_marks_the_stub_row() -> None:
    """CLI 那张逐年表也得自己交代首年为什么只有一半，否则看着像取数取漏了。"""
    from run_valuation import print_schedule

    def rendered(**overrides) -> list[str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_schedule(evaluate(base_inputs(**overrides), "fcff", "gordon"))
        return buffer.getvalue().splitlines()

    stubbed = rendered(report_as_of="2026-06-30")
    check("首年行带 * 且折现期写成 0.25", stubbed[1].startswith("2026*") and "0.25" in stubbed[1], stubbed[1][:26])
    check("其余年份不带 *", not stubbed[2].startswith("2027*"), stubbed[2][:26])
    check("表下有残期说明并写明截止日", any("只剩 0.50 年" in l and "2026-06-30" in l for l in stubbed),
          next((l for l in stubbed if "只剩" in l), "没有这行"))
    plain = rendered()
    check("没有截止日时表里不带星号也不提残期", not any("*" in l or "只剩" in l for l in plain), plain[1][:26])


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
