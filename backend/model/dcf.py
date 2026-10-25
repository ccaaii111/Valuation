"""FCFF / FCFE discounted cash flow engine.

Everything is in 亿元 except rates, multiples and per-share values (元/股), matching the units
the assumption ladder stores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from backend.model import recompute
from backend.model.assumptions import build_revenue_path, derive_rates, implied_ev_ebitda

TV_SHARE_WARNING = 0.75
# r−g 薄到一定程度时，Gordon 的 1/(r−g) 就成了主角：1.68pp 的价差等于把最后一期现金流放大 59 倍。
# 实测 β 会把价差压薄到这种程度，所以不拦结果、只标警告——数字照出，让人看到敏感性在哪。
THIN_SPREAD_WARNING = 0.03

# Keys the panel lets a human edit and whose edits reach the price. Most are read by evaluate() directly;
# `horizon_target` gets there one hop away — recompute re-cuts the year set of revenue_growth_path from it.
# A test asserts every one of them exists in a generated baseline, which catches ladder/engine name drift.
# cost_of_equity and wacc are deliberately absent: they are outputs of the rate cascade, not knobs.
ENGINE_INPUTS = frozenset(
    {
        "revenue_base",
        "revenue_growth_path",
        "horizon_target",
        "consensus_revenue",
        "revenue_ltm",
        "ebit_margin_path",
        "ebit_margin_ltm",
        "tax_rate_ltm",
        "tax_rate_hist",
        "da_to_revenue_ltm",
        "capex_to_revenue_ltm",
        "capex_to_revenue_hist",
        "nwc_to_revenue_delta",
        "interest_bearing_debt",
        "net_debt",
        "net_borrowing",
        "lease_financing",
        "minority_interest",
        "risk_free",
        "beta",
        "erp",
        "equity_weight",
        "cost_of_debt_pretax",
        "terminal_growth",
        "exit_pe",
        "exit_ev_ebitda",
        "pe_ttm",
        "roic_ltm",
        "mid_year_discount",
        "total_shares",
        "price",
        "market_cap",
    }
)

DERIVED_INPUTS = frozenset({"cost_of_equity", "wacc"})

# 不是假设、是这批事实的那一格：报表截止日。引擎用它算首年残期，面板与 CLI 都不该让人改它——
# 挪走截止日就等于把现金流时间轴从资产负债表上摘下来，桥接项和折现期会各说一套。
FIXED_INPUTS = frozenset({"report_as_of"})

# 改上游就当场重算的行：cost_of_equity / wacc 由折现率级联算，其余由 `recompute.DERIVE` 重导。
# 推导图拿它分实线与虚线（实线＝改了就生效，虚线＝只在建链那一刻算过），
# tests/test_recompute.py 把这张归属表钉住：新加一条派生边却忘了配 derive()，图上就多发一条骗人的线。
RUNTIME_EDGES = frozenset(recompute.DERIVE) | DERIVED_INPUTS


@dataclass
class YearLine:
    year: int
    t: float
    revenue: float
    ebit_margin: float
    ebit: float
    nopat: float
    interest: float
    net_income: float
    da: float
    capex: float
    nwc_change: float
    fcff: float
    fcfe: float
    discount_factor: float
    pv_fcff: float
    pv_fcfe: float
    # 首年距离报表截止日不足一整年时，这一年的现金流窗口只有这个比例；其余年份恒为 1.0。
    stub: float = 1.0

    def to_json(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class DcfResult:
    method: str
    terminal: str
    rate: float | None
    mid_year: bool
    lines: list[YearLine] = field(default_factory=list)
    pv_explicit: float = 0.0
    terminal_value: float | None = None
    pv_terminal: float = 0.0
    enterprise_value: float | None = None
    equity_value: float | None = None
    per_share: float | None = None
    upside: float | None = None
    warnings: list[str] = field(default_factory=list)
    # The scalars evaluate() settled on after all its fallbacks (税率/比率/债务成本/终值参数…). The
    # derivation graph quotes them inside each step's formula, so a displayed formula can never
    # disagree with the number the engine actually multiplied.
    params: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "method": self.method,
            "terminal": self.terminal,
            "rate": _r4(self.rate),
            "mid_year": self.mid_year,
            "lines": [line.to_json() for line in self.lines],
            "pv_explicit": _r4(self.pv_explicit),
            "terminal_value": _r4(self.terminal_value),
            "pv_terminal": _r4(self.pv_terminal),
            "enterprise_value": _r4(self.enterprise_value),
            "equity_value": _r4(self.equity_value),
            "per_share": _r4(self.per_share),
            "upside": _r4(self.upside),
            "params": {key: _r4(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value for key, value in self.params.items()},
            "warnings": self.warnings,
        }


def _r4(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def inputs_from_ladder(ladder: dict) -> dict:
    flat: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    for group in ladder.get("groups", []):
        for row in group.get("rows", []):
            flat[row["key"]] = row["baseline"]
            if row.get("raw"):
                raw[row["key"]] = row["raw"]
    # 建链那一刻的原料搭在同一份 inputs 上：`recompute` 只读 inputs，拿不到原始报表，
    # 而 `apply_overrides` 是面板/CLI/归因共用的唯一入口——原料另走一条路就会在那四处分叉。
    # 下划线开头是保留键：它不是假设行，面板与逐行归因都按行 key 取值，永远不会读到它。
    flat["_raw"] = raw
    # 报表截止日不是假设行，它在批次文档的顶层；引擎要靠它算首年残期，所以在这里并到同一份 inputs 上。
    flat["report_as_of"] = ladder.get("as_of")
    return flat


def apply_overrides(inputs: dict, overrides: dict) -> dict:
    """Merge manual edits onto the machine baseline, then re-derive everything the edits reach.

    Year-map rows merge per year, so editing 2030 does not wipe the rest of the path. A year that the
    baseline does not already carry is rejected: it is almost always a typo, and silently dropping it
    would let the human believe an edit landed when it never did.

    被人工改过的行到此为止是常量；它下游那些「链上本来就算好的行」由 `recompute` 重导（见该模块）。
    这一步放在这里而不是各调用点各写一遍：面板、推导图、CLI、归因都要同一条重算路，分叉就是新 bug。

    显性预测期改的是**年份集合本身**，所以同批改动里点名了某个新年份时，先按标量改动跑一遍重导，
    让那条路径先变宽——否则「把显性期拉到 12 年，再把末年按我的数写死」会撞在基线只有 10 年上，
    被下面那句「基线里没有这个年度」当场拒掉。
    """
    out = dict(inputs)
    scalars = {key: value for key, value in overrides.items() if not isinstance(value, dict)}
    # 这一遍只用来问「这份改动之下这条链该有哪几年」，产出的数不进最终结果（下面逐年修正另有归宿）。
    grown = recompute.recompute_derived({**out, **scalars}, set(scalars))
    for key, value in overrides.items():
        if key.startswith("_"):
            raise ValueError(f"{key} 是引擎内部键（原料/元数据），不是假设行，不能改")
        if key in FIXED_INPUTS:
            raise ValueError(f"{key} 是这批的报表截止日，不能改：首年残期由它算，改了它折现期就和资产负债表的时点对不上")
        if key not in inputs:
            raise ValueError(f"未知假设行 {key}，引擎与假设链都不认识这个键")
        if key in DERIVED_INPUTS:
            raise ValueError(
                f"{key} 是级联结果，不能直接改：改 无风险利率/β/股权风险溢价/权益权重/税前债务成本/税率，"
                "或用 --rate 强制指定折现率"
            )
        base = out.get(key)
        if isinstance(base, dict) and isinstance(value, dict):
            seed = base
            widened = grown.get(key)
            # 年份集合被这份改动改宽或改窄了：基线那几个值是照旧年数回落出来的，
            # 拼到新年数上就成了哪一年都不认识的曲线，所以整行换成新链，人点名的年份再压上去。
            if isinstance(widened, dict) and {str(y) for y in widened} != {str(y) for y in base}:
                seed = widened
            # Year keys arrive as strings from JSON and as ints from in-process callers; match on text.
            by_text = {str(item): item for item in seed}
            unknown = [str(item) for item in value if str(item) not in by_text]
            if unknown:
                raise ValueError(f"{key} 不存在这些年度/条目 {unknown}，可用: {sorted(by_text)}")
            out[key] = {**seed, **{by_text[str(k)]: v for k, v in value.items()}}
        else:
            out[key] = value
    return recompute.recompute_derived(out, set(overrides))


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:+.2%}" if unit == "pct" else f"{value:,.2f}"
    return str(value)


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) else None


def _tax_rate(inputs: dict) -> float:
    tax = _num(inputs.get("tax_rate_ltm"))
    if tax is not None:
        return tax
    return _num(inputs.get("tax_rate_hist")) or 0.25


def discount_rates(inputs: dict) -> tuple[float | None, float | None]:
    """(cost of equity, WACC) implied by the primitives in `inputs`.

    The stored rows are only a fallback: they carry the rate for the case where a primitive is
    unavailable (no risk-free fetch), and the case where the ladder was built by an older engine.
    """
    derived_equity, derived_wacc = derive_rates(
        _num(inputs.get("risk_free")),
        _num(inputs.get("beta")),
        _num(inputs.get("erp")),
        _num(inputs.get("equity_weight")),
        _num(inputs.get("cost_of_debt_pretax")),
        _tax_rate(inputs),
    )
    stored_equity = _num(inputs.get("cost_of_equity"))
    stored_wacc = _num(inputs.get("wacc"))
    return (
        derived_equity if derived_equity is not None else stored_equity,
        derived_wacc if derived_wacc is not None else stored_wacc,
    )


def _year_map(value: Any) -> dict[int, float]:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in value.items():
        try:
            year = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(item, (int, float)):
            out[year] = float(item)
    return out


def _forward_filled(years: list[int], series: dict[int, float], fallback: float | None) -> dict[int, float]:
    out: dict[int, float] = {}
    last = fallback
    for year in years:
        last = series.get(year, last)
        out[year] = last if last is not None else 0.0
    return out


def _as_of_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def stub_fraction(as_of: date | None, first_year: int) -> float:
    """预测首年还剩多少年，按整月计：基准日所在那个月不算（报告期截止日就停在那一天的月末）。

    标准报告期就落在季末，所以结果只会是 9/12、6/12、3/12 这几种：一季报后剩 9 个月、
    中报后 6 个月、三季报后 3 个月、年报后首年已是下一个完整年度（= 1.0）。
    回到 1.0 的另外两种：截止日早于首年年初（整年都在未来）、晚于首年年末（这条预测路径整体过期，
    该由新鲜度那一层拦，不在这里造出一个零窗口）。
    """
    if as_of is None or as_of.year != first_year:
        return 1.0
    months = 12 - as_of.month
    return months / 12.0 if months else 1.0


def evaluate(
    inputs: dict,
    method: str = "fcff",
    terminal: str = "gordon",
    rate: float | None = None,
    mid_year: bool | None = None,
) -> DcfResult:
    method = method.lower()
    terminal = terminal.lower()
    if method not in ("fcff", "fcfe"):
        raise ValueError(f"未知现金流口径: {method}")
    if terminal not in ("gordon", "exit_multiple", "uem"):
        raise ValueError(f"未知终值方法: {terminal}")

    warnings: list[str] = []
    growth = _year_map(inputs.get("revenue_growth_path"))
    if not growth:
        return DcfResult(method, terminal, None, bool(mid_year), warnings=["无营收增速路径，无法估值"])

    anchors = {int(k): v for k, v in _year_map(inputs.get("consensus_revenue")).items()}
    base = _num(inputs.get("revenue_base"))
    revenue_series = build_revenue_path(growth, anchors, base)
    years = sorted(growth)
    revenue = {year: revenue_series.get(year) for year in years}
    if any(revenue.get(year) is None for year in years):
        return DcfResult(method, terminal, None, bool(mid_year), warnings=["营收路径不完整（缺最后年报营收或锚点）"])

    tax = _num(inputs.get("tax_rate_ltm"))
    if tax is None:
        tax = _tax_rate(inputs)
        warnings.append(f"有效税率缺失，回退 {tax:.1%}")

    # The discount rate is recomputed from rf/β/ERP/权益权重/债务成本/税率 on every run, so a manual
    # edit anywhere in that chain moves the rate the whole valuation prices with. The stored rows only
    # serve as fallback when a primitive is missing; an explicit `rate=` argument still wins.
    cost_of_equity, wacc = discount_rates(inputs)
    if rate is None:
        rate = wacc if method == "fcff" else cost_of_equity
    if rate is None:
        return DcfResult(method, terminal, None, bool(mid_year), warnings=[f"{method.upper()} 缺折现率，无法计算"])
    if mid_year is None:
        mid_year = bool(inputs.get("mid_year_discount", False))
    da_ratio = _num(inputs.get("da_to_revenue_ltm")) or 0.0
    capex_ratio = _num(inputs.get("capex_to_revenue_ltm"))
    if capex_ratio is None:
        capex_ratio = _num(inputs.get("capex_to_revenue_hist")) or 0.0
    nwc_ratio = _num(inputs.get("nwc_to_revenue_delta")) or 0.0
    cost_of_debt = _num(inputs.get("cost_of_debt_pretax")) or 0.0
    # Financial debt from the cash-flow statement plus lease liabilities from the balance sheet: both are
    # debt-financed inflows the equity holder receives, and neither is an interest cost yet.
    net_financing = (_num(inputs.get("net_borrowing")) or 0.0) + (_num(inputs.get("lease_financing")) or 0.0)
    interest_base = _num(inputs.get("interest_bearing_debt")) or 0.0

    margins = _forward_filled(years, _year_map(inputs.get("ebit_margin_path")), _num(inputs.get("ebit_margin_ltm")))
    if all(v == 0 for v in margins.values()):
        warnings.append("EBIT 率全为 0，请检查 ebit_margin_path")

    terminal_growth = _num(inputs.get("terminal_growth"))
    if terminal_growth is None:
        warnings.append("永续增长率缺失，取 2.5%")
        terminal_growth = 0.025
    if terminal_growth <= -1.0:
        return DcfResult(
            method, terminal, rate, mid_year,
            warnings=[f"永续增长率 {terminal_growth:.2%} 低于 −100%，终值会翻成负数，无意义"],
        )
    # g ≥ r 只作废 Gordon 与 UEM（两者的分母就是 r−g）。退出倍数的终值是 EBITDA×倍数，与 g 无关，
    # 一律早退等于把这条口径也一起锁死，面板的 3×2 矩阵会空出一整列。
    if terminal_growth >= rate and terminal != "exit_multiple":
        return DcfResult(
            method, terminal, rate, mid_year,
            warnings=[f"永续增长率 {terminal_growth:.2%} ≥ 折现率 {rate:.2%}，Gordon 终值无意义"],
        )

    prior_revenue = base if base else revenue[years[0]] / (1.0 + growth[years[0]])
    # 折现轴站在报表截止日上：资产负债表（净债务、少数股东权益）就是那天的余额，所以 t=0 必须是那天，
    # 而预测首年通常已经走过一截。残期就是首年剩下的那一段时间——金额与时点都只算这一段，
    # 已经过去的半年现金流躺在截止日的现金里，再折一遍就是把同一笔钱数了两次。
    as_of = _as_of_day(inputs.get("report_as_of"))
    stub = stub_fraction(as_of, years[0])
    lines: list[YearLine] = []
    for index, year in enumerate(years):
        rev = revenue[year]
        ebit = rev * margins[year]
        nopat = ebit * (1.0 - tax)
        da = rev * da_ratio
        capex = rev * capex_ratio
        nwc_change = nwc_ratio * (rev - prior_revenue)
        prior_revenue = rev
        fcff = nopat + da - capex - nwc_change
        interest = interest_base * cost_of_debt
        net_income = (ebit - interest) * (1.0 - tax)
        fcfe = net_income + da - capex - nwc_change + net_financing
        window = stub if index == 0 else 1.0
        t = stub + index - (window / 2.0 if mid_year else 0.0)
        factor = 1.0 / (1.0 + rate) ** t
        lines.append(
            YearLine(year, t, rev * window, margins[year], ebit * window, nopat * window, interest * window,
                     net_income * window, da * window, capex * window, nwc_change * window, fcff * window,
                     fcfe * window, factor, fcff * window * factor, fcfe * window * factor, window)
        )

    pv_explicit = sum(line.pv_fcff if method == "fcff" else line.pv_fcfe for line in lines)
    last = lines[-1]
    t_terminal = stub + len(lines) - 1 - (0.5 if mid_year else 0.0)

    terminal_value: float | None
    terminal_params: dict[str, Any] = {}
    if terminal == "gordon":
        flow = (last.fcff if method == "fcff" else last.fcfe) * (1.0 + terminal_growth)
        terminal_value = flow / (rate - terminal_growth)
    elif terminal == "uem":
        roic = _num(inputs.get("roic_ltm"))
        if not roic or roic <= 0:
            return DcfResult(method, terminal, rate, mid_year, lines, warnings=["UEM 需要正的 ROIC"])
        # 负增长不需要再投资：g/ROIC 为负会把 (1−再投资率) 抬到 1 以上，等于向收缩中的公司收增长红利。
        reinvestment = max(0.0, min(1.0, terminal_growth / roic))
        nopat_next = last.nopat * (1.0 + terminal_growth)
        terminal_value = nopat_next * (1.0 - reinvestment) / (rate - terminal_growth)
        terminal_params = {"roic": roic, "reinvestment": reinvestment}
        warnings.append(f"UEM 再投资率 {reinvestment:.1%}（= g / ROIC）")
    else:  # exit_multiple
        if method == "fcff":
            multiple = _num(inputs.get("exit_ev_ebitda"))
            if multiple is None:
                multiple = _implied_ev_ebitda(inputs)
                warnings.append(f"退出倍数取当前隐含 EV/EBITDA {multiple:.2f}x" if multiple else "无 EV/EBITDA 可用")
            if not multiple:
                return DcfResult(method, terminal, rate, mid_year, lines, warnings=["退出倍数法缺 EV/EBITDA"])
            terminal_value = (last.ebit + last.da) * multiple
            terminal_params = {"exit_multiple": multiple, "multiple_row": "exit_ev_ebitda"}
        else:
            pe = _year_map(inputs.get("exit_pe"))
            multiple = pe.get(last.year) or (_num(inputs.get("pe_ttm")))
            # 只拦 None 和 0 不够：亏损公司通道原值是负倍数，负号 × 负净利会折出一个正终值。
            if not multiple or multiple <= 0:
                return DcfResult(method, terminal, rate, mid_year, lines, warnings=["退出市盈率不可得（缺失或非正）"])
            terminal_value = last.net_income * multiple
            terminal_params = {
                "exit_multiple": multiple,
                "multiple_row": "exit_pe" if pe.get(last.year) else "pe_ttm",
            }

    pv_terminal = terminal_value / (1.0 + rate) ** t_terminal
    total = pv_explicit + pv_terminal

    if method == "fcff":
        net_debt = _num(inputs.get("net_debt")) or 0.0
        minority = _num(inputs.get("minority_interest")) or 0.0
        enterprise_value: float | None = total
        equity_value = total - net_debt - minority
    else:
        enterprise_value = None
        equity_value = total

    shares = _num(inputs.get("total_shares"))
    per_share = equity_value / shares if shares else None
    price = _num(inputs.get("price"))
    upside = (per_share / price - 1.0) if (per_share is not None and price) else None

    if pv_terminal / total > TV_SHARE_WARNING and total > 0:
        warnings.append(f"终值占 {method.upper()} 现值的 {pv_terminal / total:.1%}，结论高度依赖永续假设")
    spread = rate - terminal_growth
    if terminal == "gordon" and 0 < spread < THIN_SPREAD_WARNING:
        warnings.append(
            f"折现率 {rate:.2%} 与永续增速 {terminal_growth:.2%} 只差 {spread:.2%}："
            f"Gordon 终值把最后一期现金流放大 {1.0 / spread:.0f} 倍，每股价值对 β/ERP/永续增速极度敏感"
        )
    if terminal == "exit_multiple" and spread <= 0:
        warnings.append(
            f"永续增长率 {terminal_growth:.2%} ≥ 折现率 {rate:.2%}：退出倍数不受影响，"
            "但显性期增速按该 g 回落，结论请当作不可比价"
        )
    roic = _num(inputs.get("roic_ltm"))
    if roic is not None and roic < rate:
        warnings.append(
            f"ROIC {roic:.2%} < 折现率 {rate:.2%}：UEM 终值会惩罚增长，Gordon 终值不会，两者差距即为增长的价值毁灭"
        )
    if any(line.fcff < 0 for line in lines):
        warnings.append("显性期出现负自由现金流")
    if equity_value is not None and equity_value <= 0:
        warnings.append("股权价值为负，模型不支持负价格结论")

    return DcfResult(
        method=method,
        terminal=terminal,
        rate=rate,
        mid_year=bool(mid_year),
        lines=lines,
        pv_explicit=pv_explicit,
        terminal_value=terminal_value,
        pv_terminal=pv_terminal,
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        per_share=per_share,
        upside=upside,
        warnings=warnings,
        params={
            "tax": tax,
            "da_ratio": da_ratio,
            "capex_ratio": capex_ratio,
            "nwc_ratio": nwc_ratio,
            "interest_base": interest_base,
            "cost_of_debt": cost_of_debt,
            "net_financing": net_financing,
            "terminal_growth": terminal_growth,
            "t_terminal": t_terminal,
            "stub": stub,
            "report_as_of": inputs.get("report_as_of"),
            "net_debt": _num(inputs.get("net_debt")) or 0.0,
            "minority": _num(inputs.get("minority_interest")) or 0.0,
            "shares": shares,
            "price": price,
            **terminal_params,
        },
    )


def _implied_ev_ebitda(inputs: dict) -> float | None:
    """Fallback when the ladder row is absent, e.g. hand-built inputs in tests."""
    return implied_ev_ebitda(
        market_cap=_num(inputs.get("market_cap")),
        revenue=_num(inputs.get("revenue_ltm")),
        ebit_margin=_num(inputs.get("ebit_margin_ltm")),
        da_ratio=_num(inputs.get("da_to_revenue_ltm")),
        net_debt=_num(inputs.get("net_debt")),
        minority=_num(inputs.get("minority_interest")),
    )


COMBINATIONS = [(m, t) for m in ("fcff", "fcfe") for t in ("gordon", "exit_multiple", "uem")]


def matrix(inputs: dict, mid_year: bool | None = None, rate: float | None = None) -> dict[str, dict]:
    """Per-share value for every 口径 × 终值 combination; the cross-check the panel header shows."""
    out: dict[str, dict] = {}
    for method, terminal in COMBINATIONS:
        result = evaluate(inputs, method=method, terminal=terminal, rate=rate, mid_year=mid_year)
        out[f"{method}:{terminal}"] = {
            "per_share": _r4(result.per_share),
            "rate": _r4(result.rate),
            "warnings": result.warnings,
        }
    return out


def cross_check(inputs: dict, tolerance: float = 0.05, rate: float | None = None, mid_year: bool | None = None) -> list[str]:
    """FCFF and FCFE must price the SAME operating cash flows; only the financing bridge may differ.

    equity(FCFF) = PV(FCFF flows) + bridge, bridge = -net_debt - minority, and equity(FCFE) = PV(FCFE flows).
    So the flow difference, not the per-share difference, is the real consistency test. It is checked both
    undiscounted (a cash-flow definition problem: interest / net_borrowing) and discounted (a rate problem:
    WACC and cost of equity that no longer describe one capital structure).

    rate / mid_year must match the headline run, otherwise the check quietly prices a different scenario.
    """
    notes: list[str] = []
    fcff = evaluate(inputs, "fcff", "gordon", rate=rate, mid_year=mid_year)
    fcfe = evaluate(inputs, "fcfe", "gordon", rate=rate, mid_year=mid_year)
    if not (fcff.lines and fcfe.lines):
        return [f"口径交叉校验未完成: {(fcff.warnings or fcfe.warnings)[:1]}"]

    pv_fcff = fcff.pv_explicit + fcff.pv_terminal
    pv_fcfe = fcfe.pv_explicit + fcfe.pv_terminal
    raw_fcff = sum(line.fcff for line in fcff.lines)
    raw_fcfe = sum(line.fcfe for line in fcfe.lines)
    raw_gap = (raw_fcff - raw_fcfe) / raw_fcff if raw_fcff else 0.0
    pv_gap = (pv_fcff - pv_fcfe) / pv_fcff if pv_fcff else 0.0
    bridge = -(_num(inputs.get("net_debt")) or 0.0) - (_num(inputs.get("minority_interest")) or 0.0)
    shares = _num(inputs.get("total_shares"))
    bridge_ps = bridge / shares if shares else None
    if bridge_ps == 0:  # -0.0 prints as "-0.00" and reads like a sign error
        bridge_ps = 0.0

    if abs(raw_gap) > tolerance:
        # 取末年而不是首年：首年被残期缩过，只有每一整年的利息额能代表「每年」那句话。
        after_tax_interest = fcff.lines[-1].interest * (1 - (_num(inputs.get("tax_rate_ltm")) or 0.0))
        borrowing = _num(inputs.get("net_borrowing")) or 0.0
        lease = _num(inputs.get("lease_financing")) or 0.0
        financing = f"净借款 {_fmt(borrowing)} + 租赁净融资 {_fmt(lease)} 亿元"
        notes.append(
            f"两条口径名义现金流背离 {raw_gap:+.1%}（超过 {tolerance:.0%}）：每年 {financing} 被 FCFE 计为流入，每年税后利息 "
            f"{_fmt(after_tax_interest)} 亿元只在 FCFE 扣除。"
            "若终值期不再新增借款，此背离即为 FCFE 高估，应以 FCFF 为准"
        )
    elif abs(pv_gap) > tolerance:
        cascade_equity, cascade_wacc = derive_rates(
            _num(inputs.get("risk_free")),
            _num(inputs.get("beta")),
            _num(inputs.get("erp")),
            _num(inputs.get("equity_weight")),
            _num(inputs.get("cost_of_debt_pretax")),
            _tax_rate(inputs),
        )
        if rate is not None:
            notes.append(
                f"名义现金流只差 {raw_gap:+.2%}，但现值差 {pv_gap:+.1%}：--rate 把两条口径都折在同一个 "
                f"{rate:.2%}，现值一致性检验在此场景不成立"
            )
        elif cascade_equity is None or cascade_wacc is None:
            notes.append(
                f"名义现金流只差 {raw_gap:+.2%}，但现值差 {pv_gap:+.1%}：WACC {_fmt(fcff.rate, 'pct')} 与股权成本 "
                f"{_fmt(inputs.get('cost_of_equity'), 'pct')} 已不匹配，级联回退到了基线存量 WACC"
                "（多为缺 无风险利率/β/股权风险溢价/权益权重）"
            )
        else:
            # A complete cascade means the two rates DO describe one capital structure. The gap is then
            # arithmetic: a levered firm discounts at WACC below cost of equity, and Gordon amplifies it.
            notes.append(
                f"名义现金流只差 {raw_gap:+.2%}，但现值差 {pv_gap:+.1%}：FCFF 折在 WACC {_fmt(fcff.rate, 'pct')}、"
                f"FCFE 折在股权成本 {_fmt(fcfe.rate, 'pct')}，级联正常；杠杆越高终值倍数差越大，"
                "此背离来自资本结构假设本身而非取数错误，结果应以 FCFF 为准"
            )
    else:
        notes.append(
            f"两条口径自洽（名义 {raw_gap:+.2%} / 现值 {pv_gap:+.2%}），每股差值即桥接项 {_fmt(bridge_ps)} 元"
        )
    if fcff.per_share and fcfe.per_share:
        notes.append(f"每股 FCFF {fcff.per_share:.2f} vs FCFE {fcfe.per_share:.2f}")
    return notes


BRIDGE_GROUP = "valuation"
BRIDGE_TITLE = "估值：引擎步骤"


def valuation_bridge(result: DcfResult, known: Iterable[str] | None = None) -> dict:
    """把引擎最后几步摊成图上节点：值全部读自已算好的 DcfResult，绝不为了画图重跑一次 evaluate。

    公式里代的是 `result.params`——引擎在所有回退之后真正乘过的那批标量，所以显示式与它旁边的数字
    不会各说一套。known 传这批基线已有的行名：上游只认图里真有的节点，老批次没有租赁行就不画那条边，
    与推导图骨架同一纪律。步骤集合随 口径 × 终值 变（FCFE 没有企业价值，退出倍数不看永续增速）。
    """
    lines = result.lines
    # 引擎拒绝定价（缺 ROIC、缺退出倍数、g≥r、缺折现率…）时 lines 可能有值而各合计根本没算，
    # 字段停在默认 0.0／None。摊到图上就是一排理直气壮的 0——那是假账，不是「链条短」。
    # 警示文字由面板与页面单独呈现，估值段整段留空。
    if result.equity_value is None or not lines:
        return {"nodes": [], "edges": [], "group": {"id": BRIDGE_GROUP, "title": BRIDGE_TITLE}}
    params = result.params or {}
    is_fcff = result.method == "fcff"
    flow = "FCFF" if is_fcff else "FCFE"
    rate_row = "wacc" if is_fcff else "cost_of_equity"
    rate = result.rate or 0.0
    years = [line.year for line in lines]
    span = f"{years[0]}–{years[-1]}"
    nodes: dict[str, dict] = {}

    def resolves(source: str) -> bool:
        if source in nodes:
            return True  # 段内：先前加过的步骤
        if source.startswith("step_"):
            return False  # 段内但值为 None 被跳过——不画向虚无
        return known is None or source in known

    def add(key: str, label: str, value: float | None, unit: str, formula: str, upstream: list[str],
            series: dict[str, float] | None = None) -> None:
        if value is None:
            return
        nodes[key] = {
            "key": key, "label": label, "unit": unit, "group": BRIDGE_GROUP, "group_title": BRIDGE_TITLE,
            "kind": "engine", "baseline": _r4(value), "formula": formula, "series": series,
            "value_type": "number", "editable": False, "derived": True,
            "upstream": [source for source in upstream if resolves(source)],
        }

    def yearly(attribute: str) -> dict[str, float]:
        return {str(line.year): _r4(getattr(line, attribute)) for line in lines}

    def total(attribute: str) -> float:
        return sum(getattr(line, attribute) for line in lines)

    tax = params.get("tax") or 0.0
    stub = params.get("stub") or 1.0
    # 首年被残期缩过，图上的合计就必须自己说清这件事，否则「2026 营收 876 亿」看着像取数错了。
    stub_note = "" if stub >= 1.0 else f"；首年只剩 {stub:.2f} 年（报表截至 {params.get('report_as_of')}），各列金额按这一比例计"
    add("step_revenue", "显性期营收合计", total("revenue"), "亿元",
        f"本年营收 = 上年营收 × (1 + 当年增速)；有机构一致预期的年份直接取预期值作锚点（{span}）{stub_note}",
        ["revenue_growth_path", "consensus_revenue", "revenue_base"], yearly("revenue"))
    add("step_ebit", "EBIT 合计", total("ebit"), "亿元",
        "EBIT_t = 当年营收 × 当年 EBIT 率（路径缺年份沿用最近一年）",
        ["step_revenue", "ebit_margin_path", "ebit_margin_ltm"], yearly("ebit"))
    add("step_nopat", "NOPAT 合计", total("nopat"), "亿元",
        f"NOPAT_t = EBIT_t × (1 − 有效税率 {tax:.2%})",
        ["step_ebit", "tax_rate_ltm", "tax_rate_hist"], yearly("nopat"))
    add("step_da", "折旧摊销合计", total("da"), "亿元",
        f"D&A_t = 营收_t × 折旧摊销率 {params.get('da_ratio', 0.0):.2%}",
        ["step_revenue", "da_to_revenue_ltm", "da_to_revenue_hist"], yearly("da"))
    add("step_capex", "资本支出合计", total("capex"), "亿元",
        f"CapEx_t = 营收_t × 资本支出率 {params.get('capex_ratio', 0.0):.2%}",
        ["step_revenue", "capex_to_revenue_ltm", "capex_to_revenue_hist"], yearly("capex"))
    add("step_nwc", "营运资本增加合计", total("nwc_change"), "亿元",
        f"ΔNWC_t = 营运资本率 {params.get('nwc_ratio', 0.0):.2%} × (营收_t − 营收_(t−1))；首年接最后年报营收",
        ["step_revenue", "nwc_to_revenue_delta"], yearly("nwc_change"))

    if is_fcff:
        add(f"step_{flow.lower()}", f"{flow} 合计", total("fcff"), "亿元",
            f"{flow}_t = NOPAT_t + D&A_t − CapEx_t − ΔNWC_t",
            ["step_nopat", "step_da", "step_capex", "step_nwc"], yearly("fcff"))
    else:
        add("step_interest", "利息（税前）合计", total("interest"), "亿元",
            f"利息 = 有息负债 {params.get('interest_base', 0.0):,.2f} 亿 × 税前债务成本 "
            f"{params.get('cost_of_debt', 0.0):.2%}，显性期每年同额{stub_note}",
            ["interest_bearing_debt", "cost_of_debt_pretax"], yearly("interest"))
        add("step_net_income", "税后净利合计", total("net_income"), "亿元",
            f"净利_t = (EBIT_t − 利息) × (1 − 税率 {tax:.2%})",
            ["step_ebit", "step_interest", "tax_rate_ltm", "tax_rate_hist"], yearly("net_income"))
        add("step_financing", "债务净融资（每年）", params.get("net_financing"), "亿元",
            "净借款流入 + 租赁净融资：FCFE 口径下这是股东当年实际增减的借来资金"
            + ("（每年同额，首年按残期折算）" if stub < 1.0 else ""),
            ["net_borrowing", "lease_financing"])
        add(f"step_{flow.lower()}", f"{flow} 合计", total("fcfe"), "亿元",
            f"{flow}_t = 净利_t + D&A_t − CapEx_t − ΔNWC_t + 债务净融资",
            ["step_net_income", "step_da", "step_capex", "step_nwc", "step_financing"], yearly("fcfe"))

    pv_row = f"pv_{flow.lower()}"
    if stub < 1.0:
        shown = ", ".join(f"{line.t:g}" for line in lines[:2])
        t_explicit = (f"{shown}, …（{'年中' if result.mid_year else '年末'}折现；折现轴从报表截至 "
                      f"{params.get('report_as_of')} 起算，首年只剩 {stub:.2f} 年）")
    else:
        t_explicit = "0.5, 1.5, …（年中折现）" if result.mid_year else "1, 2, …（年末折现）"
    add("step_pv_explicit", "显性期现值合计", result.pv_explicit, "亿元",
        f"Σ {span}：{flow}_t ÷ (1 + r)^t，r = {rate:.2%}（{'WACC' if is_fcff else '股权成本'}），t = {t_explicit}",
        [f"step_{flow.lower()}", rate_row], yearly(pv_row))

    growth = params.get("terminal_growth") or 0.0
    if result.terminal == "gordon":
        add("step_terminal_value", "终值（永续）", result.terminal_value, "亿元",
            f"末年{flow} × (1 + g) ÷ (r − g)，g = {growth:.2%}，r − g = {rate - growth:.2%}",
            [f"step_{flow.lower()}", "terminal_growth", rate_row])
    elif result.terminal == "uem":
        add("step_terminal_value", "终值（UEM）", result.terminal_value, "亿元",
            f"末年NOPAT × (1 + g) × (1 − 再投资率 {params.get('reinvestment', 0.0):.1%}) ÷ (r − g)；"
            f"再投资率 = g ÷ ROIC({params.get('roic', 0.0):.2%})，g = {growth:.2%}",
            ["step_nopat", "terminal_growth", "roic_ltm", rate_row])
    elif params.get("exit_multiple"):
        multiple = params["exit_multiple"]
        if is_fcff:
            add("step_terminal_value", "终值（退出倍数）", result.terminal_value, "亿元",
                f"末年 EBITDA（EBIT + D&A）× EV/EBITDA {multiple:.2f}x",
                ["step_ebit", "step_da", "exit_ev_ebitda"])
        else:
            add("step_terminal_value", "终值（退出倍数）", result.terminal_value, "亿元",
                f"末年税后净利 × 退出市盈率 {multiple:.2f}x（倍数取自「{params.get('multiple_row')}」）",
                ["step_net_income", str(params.get("multiple_row"))])

    add("step_pv_terminal", "终值现值", result.pv_terminal, "亿元",
        f"终值 ÷ (1 + r)^{params.get('t_terminal', float(len(lines))):g}，r = {rate:.2%}",
        ["step_terminal_value", rate_row])
    add("step_enterprise_value" if is_fcff else "step_equity_value",
        "企业价值 EV" if is_fcff else "股权价值",
        result.enterprise_value if is_fcff else result.equity_value, "亿元",
        f"显性期现值合计 + 终值现值（{flow} 折出来的是全投资人的钱，故先落企业价值）" if is_fcff
        else "显性期现值合计 + 终值现值：FCFE 折的就是股东的钱，不再桥接净债务",
        ["step_pv_explicit", "step_pv_terminal"])

    bridge_from = "step_enterprise_value" if is_fcff else "step_equity_value"
    if is_fcff:
        add("step_equity_value", "股权价值", result.equity_value, "亿元",
            f"EV − (净债务 {params.get('net_debt', 0.0):,.2f} 亿) − (少数股东权益 "
            f"{params.get('minority', 0.0):,.2f} 亿)，净债务为负即净现金",
            [bridge_from, "net_debt", "minority_interest"])

    shares = params.get("shares")
    add("step_per_share", "每股价值", result.per_share, "元",
        f"股权价值 ÷ 总股本 {shares:,.2f} 亿股" if shares else "股权价值 ÷ 总股本（缺总股本行则算不出）",
        ["step_equity_value", "total_shares"])
    price = params.get("price")
    add("step_upside", "相对现价", result.upside, "pct",
        f"每股价值 ÷ 现价 {price:,.2f} 元 − 1" if price else "每股价值 ÷ 现价 − 1（缺现价）",
        ["step_per_share", "price"])

    return {
        "nodes": list(nodes.values()),
        "edges": [{"from": source, "to": key} for key, node in nodes.items() for source in node["upstream"]],
        "group": {"id": BRIDGE_GROUP, "title": BRIDGE_TITLE},
    }
