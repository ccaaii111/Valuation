"""Hand-checked regressions for the DCF engine.

    python tests/test_dcf.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.model.dcf import apply_overrides, cross_check, discount_rates, evaluate, inputs_from_ladder, matrix  # noqa: E402

YEARS = [2026, 2027, 2028, 2029, 2030]
FAILURES = 0

# Flat-plateau case, chosen so every intermediate is computable by hand:
# revenue 1000, EBIT margin 30% -> EBIT 300, NOPAT 225, D&A == capex, no working-capital swing.
# PV(explicit) = 225 * (1 - 1.075^-5)/0.075 = 910.3242
# TV = 225 * 1.025 / (0.075 - 0.025) = 4612.50 ; PV(TV) = 4612.50 * 1.075^-5 = 3212.8768
# Equity = 4123.2010 ; /100 shares = 41.2320 元
EXPECTED_PER_SHARE = 41.2320


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def close(a: float | None, b: float, tol: float = 1e-3) -> bool:
    return a is not None and abs(a - b) < tol


def base_inputs(**overrides) -> dict:
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
        "mid_year_discount": False,
        "total_shares": 100.0,
        "price": 40.0,
        "roic_ltm": 0.20,
    }
    inputs.update(overrides)
    return inputs


def test_hand_computed_gordon_fcff() -> None:
    result = evaluate(base_inputs(), "fcff", "gordon")
    check("显性期现值 = 910.3242", close(result.pv_explicit, 910.3242, 0.01), f"{result.pv_explicit:.4f}")
    check("Gordon 终值 = 4612.50", close(result.terminal_value, 4612.50, 0.01), f"{result.terminal_value:.4f}")
    check("终值现值 = 3212.8768", close(result.pv_terminal, 3212.8768, 0.02), f"{result.pv_terminal:.4f}")
    check("企业价值 = 4123.2010", close(result.enterprise_value, 4123.2010, 0.02), f"{result.enterprise_value:.4f}")
    check("每股 = 41.2320", close(result.per_share, EXPECTED_PER_SHARE, 0.002), f"{result.per_share:.4f}")
    check("每年 FCFF 恒为 225", all(close(l.fcff, 225.0, 1e-9) for l in result.lines))
    check("折现年数 5", len(result.lines) == 5)
    check("上行空间 = 3.08%", close(result.upside, 41.2320 / 40 - 1.0, 1e-3), f"{result.upside:.4f}")


def test_fcff_equals_fcfe_without_debt() -> None:
    """Zero debt, zero minority, zero net borrowing: the two calibers must collapse to the same number."""
    fcff = evaluate(base_inputs(), "fcff", "gordon")
    fcfe = evaluate(base_inputs(), "fcfe", "gordon", rate=0.075)
    check("无杠杆时两口径每股一致", close(fcff.per_share, fcfe.per_share or 0, 1e-9), f"{fcff.per_share} vs {fcfe.per_share}")
    check("无杠杆时两口径终值一致", close(fcff.terminal_value, fcfe.terminal_value or 0, 1e-9))


def test_leverage_widens_the_two_calibers() -> None:
    inputs = base_inputs(interest_bearing_debt=800.0, net_debt=800.0, cost_of_debt_pretax=0.05, net_borrowing=50.0)
    fcff = evaluate(inputs, "fcff", "gordon")
    fcfe = evaluate(inputs, "fcfe", "gordon", rate=0.10)
    check("FCFE = FCFF - 税后利息 + 净借款", close(fcfe.lines[0].fcfe, fcff.lines[0].fcff - 800 * 0.05 * 0.75 + 50.0, 1e-9), f"{fcfe.lines[0].fcfe:.2f}")
    no_borrow = evaluate(base_inputs(interest_bearing_debt=800.0, net_debt=800.0, cost_of_debt_pretax=0.05), "fcfe", "gordon", rate=0.10)
    check("不新增借款时 FCFE 低于 NOPAT 口径", close(no_borrow.lines[0].fcfe, 195.0, 1e-9), f"{no_borrow.lines[0].fcfe:.2f}")
    check("FCFF 扣净债务、FCFE 不扣", fcff.equity_value is not None and fcfe.equity_value is not None and fcfe.enterprise_value is None)


def test_mid_year_convention() -> None:
    annual = evaluate(base_inputs(), "fcff", "gordon", mid_year=False)
    mid = evaluate(base_inputs(), "fcff", "gordon", mid_year=True)
    ratio = mid.per_share / annual.per_share
    check("年中折现抬升约 (1+r)^0.5", close(ratio, 1.075 ** 0.5, 1e-3), f"{ratio:.5f}")
    check("首年折现期 0.5", close(mid.lines[0].t, 0.5, 1e-12))
    check("终值折现期 4.5", close(mid.lines[-1].t, 4.5, 1e-12) and close(mid.pv_terminal / mid.terminal_value, 1.075 ** -4.5, 1e-9))


def test_terminal_growth_above_rate_is_rejected() -> None:
    result = evaluate(base_inputs(terminal_growth=0.08), "fcff", "gordon")
    check("g ≥ r 时不出值", result.per_share is None)
    check("给出明确告警", any("≥" in w for w in result.warnings), str(result.warnings))


def test_uem_penalises_growth_below_roic() -> None:
    """The invariant the panel needs: with ROIC below the discount rate, faster growth must be worth less under UEM."""
    slow = evaluate(base_inputs(roic_ltm=0.04), "fcff", "uem", rate=0.075)
    fast = evaluate(base_inputs(roic_ltm=0.04, terminal_growth=0.06), "fcff", "uem", rate=0.075)
    gordon_fast = evaluate(base_inputs(terminal_growth=0.06), "fcff", "gordon", rate=0.075)
    gordon_slow = evaluate(base_inputs(), "fcff", "gordon", rate=0.075)
    check("UEM 下更高永续增长反而更不值钱", (fast.per_share or 0) < (slow.per_share or 0), f"{fast.per_share} < {slow.per_share}")
    check("Gordon 下更高永续增长更值钱", (gordon_fast.per_share or 0) > (gordon_slow.per_share or 0))
    check("UEM 再投资率 = g/ROIC", close(slow.terminal_value, 225 * 1.025 * (1 - 0.025 / 0.04) / 0.05, 0.01), f"{slow.terminal_value:.2f}")
    check("ROIC<WACC 时给出对照告警", any("ROIC" in w for w in fast.warnings))


def test_exit_multiples() -> None:
    ev = evaluate(base_inputs(exit_ev_ebitda=5.0), "fcff", "exit_multiple")
    check("EV/EBITDA 终值 = (300+50)*5", close(ev.terminal_value, 1750.0, 1e-9), f"{ev.terminal_value}")
    pe = evaluate(base_inputs(exit_pe={2030: 12.0}), "fcfe", "exit_multiple", rate=0.075)
    check("退出市盈率用净利润而非 NOPAT", close(pe.terminal_value, 225.0 * 12.0, 1e-9), f"{pe.terminal_value}")
    levered = evaluate(
        base_inputs(interest_bearing_debt=800.0, cost_of_debt_pretax=0.05, exit_pe={2030: 12.0}),
        "fcfe", "exit_multiple", rate=0.075,
    )
    check("有息负债压低退出市盈率终值", levered.terminal_value < pe.terminal_value, f"{levered.terminal_value:.1f} < {pe.terminal_value:.1f}")


def test_equity_bridge_and_warnings() -> None:
    plain = evaluate(base_inputs(), "fcff", "gordon")
    bridged = evaluate(base_inputs(net_debt=1000.0, minority_interest=200.0), "fcff", "gordon")
    check("桥 = EV - 净债务 - 少数股东", close(bridged.equity_value, plain.enterprise_value - 1200.0, 1e-9), f"{bridged.equity_value:.4f}")

    heavy_tv = evaluate(base_inputs(), "fcff", "gordon")
    share = heavy_tv.pv_terminal / heavy_tv.enterprise_value
    check("终值占比 77.9% 触发依赖告警", share > 0.75 and any("终值占" in w for w in heavy_tv.warnings), f"{share:.1%}")

    negative = evaluate(base_inputs(capex_to_revenue_ltm=0.50), "fcff", "gordon")
    check("负自由现金流被告警", any("负自由现金流" in w for w in negative.warnings), str(negative.warnings))


def test_thin_discount_spread_is_flagged_not_silenced() -> None:
    thin = evaluate(base_inputs(), "fcff", "gordon", rate=0.045)  # r−g = 2pp
    check("薄价差把放大倍数写进告警", any("放大 50 倍" in w for w in thin.warnings), str(thin.warnings))
    wide = evaluate(base_inputs(), "fcff", "gordon")  # r−g = 5pp
    check("常规价差不触发", not any("极度敏感" in w for w in wide.warnings), str(wide.warnings))
    check("薄价差照常出每股值", thin.per_share is not None and thin.per_share > wide.per_share,
          f"{thin.per_share} vs {wide.per_share}")
    exit_tv = evaluate(base_inputs(exit_pe={y: 12.0 for y in YEARS}), "fcff", "exit_multiple", rate=0.045)
    check("非 Gordon 终值不谈价差放大", not any("极度敏感" in w for w in exit_tv.warnings), str(exit_tv.warnings))


def test_matrix_and_cross_check_run() -> None:
    grid = matrix(base_inputs(exit_pe={y: 12.0 for y in YEARS}, roic_ltm=0.20))
    check("矩阵含 6 个组合", len(grid) == 6, str(sorted(grid)))
    check("每个组合都有每股值或告警", all(v["per_share"] is not None or v["warnings"] for v in grid.values()))

    notes = cross_check(base_inputs())
    check("零杠杆两口径现金流现值自洽", any("自洽" in n for n in notes), str(notes))

    bridged = cross_check(base_inputs(net_debt=-500.0, minority_interest=50.0))
    check("每股差值即桥接项 4.50 元", any("桥接项 4.50" in n for n in bridged), str(bridged))

    levered = cross_check(base_inputs(net_borrowing=200.0))
    check("持续净借款被判定为口径背离", any("背离" in n for n in levered), str(levered))

    mismatched = cross_check(base_inputs(wacc=0.12))
    check("只改 WACC 不改股权成本被单独诊断", any("已不匹配" in n for n in mismatched), str(mismatched))

    blocked = cross_check(base_inputs(wacc=None))
    check("缺折现率时校验拒绝而非沉默", any("未完成" in n for n in blocked), str(blocked))

    forced = cross_check(base_inputs(), rate=0.12)
    check("校验与标题用同一折现率，不改演另一情景", any("FCFF 21.89 vs FCFE 21.89" in n for n in forced), str(forced))


def test_cross_check_does_not_blame_a_working_cascade() -> None:
    """A high-leverage firm legitimately discounts FCFF at a WACC far below cost of equity.

    On real 中国石化 data the old wording read "级联回退到了基线存量 WACC" while the cascade had in fact
    produced the rate — an accusation of a data defect where there was none.
    """
    inputs = base_inputs(risk_free=0.02, beta=1.0, erp=0.06, equity_weight=0.5)
    cascade_equity, cascade_wacc = discount_rates(inputs)
    check("原始项齐全时 WACC 来自级联", close(cascade_wacc, 0.05875, 1e-6), f"{cascade_wacc}")
    notes = cross_check(inputs)
    check("现值背离被判为口径差异而非数据错误",
          any("级联正常" in n for n in notes) and not any("已不匹配" in n for n in notes), str(notes))
    check("结论仍以 FCFF 为准", any("以 FCFF 为准" in n for n in notes), str(notes))


def test_apply_overrides_merges_year_maps() -> None:
    ladder = {"groups": [{"id": "g", "title": "t", "rows": [
        {"key": "revenue_growth_path", "label": "k", "unit": "pct", "baseline": {"2026": 0.1, "2027": 0.08}, "source": "s"},
        {"key": "terminal_growth", "label": "k", "unit": "pct", "baseline": 0.025, "source": "s"},
    ]}]}
    inputs = inputs_from_ladder(ladder)
    merged = apply_overrides(inputs, {"revenue_growth_path": {"2027": 0.02}})
    check("改单年不清空其他年", merged["revenue_growth_path"] == {"2026": 0.1, "2027": 0.02}, str(merged["revenue_growth_path"]))
    check("合并不写回基线", inputs["revenue_growth_path"]["2027"] == 0.08)
    check("标量行直接替换", apply_overrides(inputs, {"terminal_growth": 0.03})["terminal_growth"] == 0.03)
    check("未知年度被拒绝", _raises(lambda: apply_overrides(inputs, {"revenue_growth_path": {"2031": 0.0}})))
    check("未知假设行被拒绝", _raises(lambda: apply_overrides(inputs, {"temrinal_growth": 0.03})))


# rf 1.5% + β 1.0 × ERP 6.0% = 7.5%，权益权重 1.0 让债务腿整条消失，正好复现存量折现率。
RATE_PRIMITIVES = {"risk_free": 0.015, "beta": 1.0, "erp": 0.06, "equity_weight": 1.0}


def test_discount_rate_cascade() -> None:
    inputs = base_inputs(**RATE_PRIMITIVES)
    equity, wacc = discount_rates(inputs)
    check("级联复现存量股权成本", close(equity, 0.075, 1e-9), str(equity))
    check("级联复现存量 WACC", close(wacc, 0.075, 1e-9), str(wacc))
    check("级联后每股仍等于手算", close(evaluate(inputs, "fcff", "gordon").per_share, EXPECTED_PER_SHARE, 0.002))

    levered = dict(inputs, equity_weight=0.6, cost_of_debt_pretax=0.05)
    check("WACC 按权重与税盾重算", close(discount_rates(levered)[1], 0.6 * 0.075 + 0.4 * 0.05 * 0.75, 1e-9),
          f"{discount_rates(levered)[1]:.6f}")
    high_tax = dict(levered, tax_rate_ltm=0.40)
    check("改税率只动 WACC 不动股权成本", close(discount_rates(high_tax)[0], 0.075, 1e-9)
          and discount_rates(high_tax)[1] < discount_rates(levered)[1])

    beta_up = apply_overrides(inputs, {"beta": 1.2})
    check("β 改动进折现率", close(evaluate(beta_up, "fcff", "gordon").rate, 0.015 + 1.2 * 0.06, 1e-9))
    check("β 改动改变每股", evaluate(beta_up, "fcff", "gordon").per_share < evaluate(inputs, "fcff", "gordon").per_share)
    pinned = evaluate(beta_up, "fcff", "gordon", rate=0.075)
    check("--rate 仍优先于级联", close(pinned.rate, 0.075) and close(pinned.per_share, EXPECTED_PER_SHARE, 0.002))

    for key in ("wacc", "cost_of_equity"):
        check(f"{key} 拒绝直接编辑", _raises(lambda k=key: apply_overrides(inputs, {k: 0.09}), "级联结果"))
    check("缺原始项时回退存量折现率", close(evaluate(base_inputs(), "fcff", "gordon").rate, 0.075))


def test_missing_inputs_degrade_loudly() -> None:
    check("缺增速路径不出值", evaluate({}, "fcff", "gordon").per_share is None)
    check("缺折现率告警", any("折现率" in w for w in evaluate(base_inputs(wacc=None), "fcff", "gordon").warnings))
    check("未知口径报错", _raises(lambda: evaluate(base_inputs(), "ddm", "gordon")))
    check("未知终值报错", _raises(lambda: evaluate(base_inputs(), "fcff", "sandpit")))


def _raises(fn, contains: str = "") -> bool:
    try:
        fn()
    except ValueError as exc:
        return not contains or contains in str(exc)
    return False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
