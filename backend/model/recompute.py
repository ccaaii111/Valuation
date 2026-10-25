"""改一个节点，整条链该跟着动的机器。

`build_ladder()` 在取数那一刻把派生行算成常数，`DERIVES` 记下的是「当时谁喂了谁」。这张表把其中
**只靠已存行就能重导**的那些边接回运行时：面板、推导图、CLI、归因改了任一上游，这里按拓扑序把下游
逐行重算——「改了永续增速而营收增速路径不动」这种图上明明有线、改了却什么都不发生的事不再出现。

三条规矩：

1. 人工改过的行是常量。它自己不重导，它的下游改从它的新值算——改了就是当输入用，页面不再替你猜。
2. 只重算被改动波及的下游。没被碰的分支保持基线里的数字，面板不会看见无缘由的漂移。
3. 原料要么在行里，要么在这一行的 `raw` 里。利息费用 LTM、上年同期有息负债、五年年报营收的端点、
   LTM 的 EBIT 与净资产这些只存在于原始报表，建链那一刻被 `assumptions` 存进 `raw`（单位与行一致：亿元），
   这条边才有权活到运行时。`risk_free` 的窗口中枢、三种永续增速情形、营运资本差分没有原料可存
   ——它们只在建链时算过一次，图上画虚线（见 docs/caliber.md §4.6「哪条边当场算」）。缺 `raw` 的老批次同理退回虚线，
   一个数字都不会漂。
"""
from __future__ import annotations

from typing import Any, Callable

from backend.model.assumptions import (
    DERIVES,
    PRETAX_COST_OF_DEBT,
    build_margin_path,
    build_revenue_path,
    glide_growth,
    implied_ev_ebitda,
    measure_cost_of_debt,
    rekey_years,
    resolve_horizon,
    row_years,
)

YI = 1e8
# inputs 里的保留键：`dcf.inputs_from_ladder` 把各行的 raw 收在这里。它不是假设行，
# 所以 `dcf.apply_overrides` 拒绝任何下划线开头的修正。
RAW = "_raw"


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _raw_for(inputs: dict, key: str) -> dict | None:
    raw = inputs.get(RAW)
    return raw.get(key) if isinstance(raw, dict) else None


def _horizon_years(stored: dict[int, float], anchors: dict[int, float], target: float | None) -> list[int]:
    """这条路径该覆盖哪几年。

    有「显性预测期」那一行就按它现算（年数是人的选择，改了它整段年份数跟着换），算法与建链时同一条
    `resolve_horizon`，所以新建的批次在这里跑出来的是同一个年份集合。老批次的基线没这个键（读成 None）
    → 沿用这一行自己已存的那些年，历史批次一年都不动。
    """
    if target is None:
        return sorted(set(stored) | set(anchors))
    horizon, _ = resolve_horizon(len(anchors), target)
    # 没有锚点时显性期起点在「最后年报营收那一年」之后，那一年不在行里，但从已存路径的首年反推得到。
    anchor_end = max(anchors) if anchors else min(stored) - 1
    tail = max(0, horizon - len(anchors))
    return sorted(anchors) + [anchor_end + offset for offset in range(1, tail + 1)]


def derive_revenue_growth_path(inputs: dict) -> dict[str, float] | None:
    """锚点年按相邻营收之比，锚点之后线性回落到级联用的 g。

    年份集合按「显性预测期」那一行现算：改 g 换的是回落的终点，改年数换的是这条路走几年。
    一致预期为空的公司也照样能重导——那一路的起点是 `revenue_cagr_5y`，同样全在行里。
    """
    stored = row_years(inputs.get("revenue_growth_path"))
    anchors = row_years(inputs.get("consensus_revenue"))
    terminal = _number(inputs.get("terminal_growth"))
    if not stored or terminal is None:
        return None
    requested = _number(inputs.get("horizon_target"))
    years = _horizon_years(stored, anchors, requested)
    growth = glide_growth(
        anchors, _number(inputs.get("revenue_base")), _number(inputs.get("revenue_cagr_5y")), terminal, years
    )
    if set(years) - set(growth):
        # 该覆盖的年份里有一个没算出数：2026-09-21 那几个批次还没有「最后年报营收」这一行，
        # 第一个预估年没有基数可比，glide 只能把它跳过。这种批次上这条边本就不通——宁可一个数都不动，
        # 也不能交出一条短一年的路径：短掉的那一年在引擎里就真的没了。
        return None
    return rekey_years({year: round(value, 6) for year, value in growth.items()}, inputs.get("revenue_growth_path"))


def derive_horizon_years(inputs: dict) -> int | None:
    """显性预测年数 = 增速行覆盖的年份数：这一行只把那个数说给人看，所以它必须跟着被改宽的路径走。

    增速行为空时不重导（None）——建链时它会兜出一个年数，行里没有年份可数，硬报 0 是假数。
    """
    return len(row_years(inputs.get("revenue_growth_path"))) or None


def derive_revenue_path(inputs: dict) -> dict[str, float] | None:
    """显示行：增速路径与锚点摊出来的逐年营收（亿元）。引擎内部一直在算它，这一行只是把结果写给人看。"""
    growth = row_years(inputs.get("revenue_growth_path"))
    base = _number(inputs.get("revenue_base"))
    if not growth or base is None:
        return None
    path = build_revenue_path(growth, row_years(inputs.get("consensus_revenue")), base)
    return rekey_years({year: round(value, 4) for year, value in path.items()}, inputs.get("revenue_path"))


def derive_ebit_margin_path(inputs: dict) -> dict[str, float] | None:
    """显性期 EBIT 率：覆盖到的年份取一致预期，其后向近五年均值回落；年份跟着增速路径走。"""
    growth = row_years(inputs.get("revenue_growth_path"))
    if not growth:
        return None
    path = build_margin_path(
        sorted(growth), row_years(inputs.get("ebit_margin_consensus")),
        _number(inputs.get("ebit_margin_hist")), _number(inputs.get("ebit_margin_start")),
    )
    return rekey_years({year: round(value, 6) for year, value in path.items()}, inputs.get("ebit_margin_path"))


def derive_net_debt(inputs: dict) -> float | None:
    debt = _number(inputs.get("interest_bearing_debt"))
    cash = _number(inputs.get("cash_and_financial_assets"))
    if debt is None or cash is None:
        return None
    return round(debt - cash, 4)


def derive_equity_weight(inputs: dict) -> float | None:
    """权益权重 = 总市值 /(总市值 + 有息负债)：改市值或改债务都该换折现率里的权重。"""
    market_cap = _number(inputs.get("market_cap"))
    debt = _number(inputs.get("interest_bearing_debt"))
    if market_cap is None or debt is None:
        return None
    total = market_cap + debt
    return round(market_cap / total, 6) if total else None


def derive_exit_ev_ebitda(inputs: dict) -> float | None:
    """现价已经隐含的 EV/EBITDA：分子分母全是链上的行，所以这条边天生就能当场算。"""
    value = implied_ev_ebitda(
        market_cap=_number(inputs.get("market_cap")),
        revenue=_number(inputs.get("revenue_ltm")),
        ebit_margin=_number(inputs.get("ebit_margin_ltm")),
        da_ratio=_number(inputs.get("da_to_revenue_ltm")),
        net_debt=_number(inputs.get("net_debt")),
        minority=_number(inputs.get("minority_interest")),
    )
    return None if value is None else round(value, 2)


def derive_pe_ttm(inputs: dict) -> float | None:
    """滚动市盈率 = 总市值 ÷ 归母净利润 LTM（两行同单位，比值无量纲）。

    分母非正就出不了数，也不回退行情通道原值——那个数在 push2 上是动态市盈率，两套口径不能躺在同一行。
    """
    cap = _number(inputs.get("market_cap"))
    profit = _number(inputs.get("net_profit_ltm"))
    if not cap or profit is None or profit <= 0:
        return None
    return round(cap / profit, 2)


def derive_revenue_cagr_5y(inputs: dict) -> float | None:
    """近 5 年营收 CAGR：终点是「最后年报营收」那一行（可能被改了），起点是建链时存下的那一年。

    这里不用 `periods.cagr`——它要的是一整条年报序列，而那条序列只活在报表里；这一行存了端点，
    公式照 `cagr` 的样子手写：比值开 (末年−首年) 次方根。
    """
    raw = _raw_for(inputs, "revenue_cagr_5y")
    first = _number(raw.get("first_revenue")) if raw else None
    last = _number(inputs.get("revenue_base"))
    if not raw or not first or not last:
        return None
    span = (raw.get("last_year") or 0) - (raw.get("first_year") or 0)
    if span <= 0:
        return None
    return round((last / first) ** (1.0 / span) - 1.0, 6)


def _ltm_ratio(inputs: dict, row: str, numerator: str) -> float | None:
    """LTM 比率行：分子是建链时存下的那个绝对额（亿元），分母是当前的 LTM 营收行。"""
    raw = _raw_for(inputs, row)
    top = _number(raw.get(numerator)) if raw else None
    bottom = _number(inputs.get("revenue_ltm"))
    if top is None or not bottom:
        return None
    return round(top / bottom, 6)


def derive_ebit_margin_ltm(inputs: dict) -> float | None:
    return _ltm_ratio(inputs, "ebit_margin_ltm", "ebit_ltm")


def derive_ebit_margin_start(inputs: dict) -> float | None:
    """显性期首年 EBIT 率：最早那个预估年有预期就用它，否则退回 LTM——与建链时同一条择一。

    「首年」读的是 `consensus_revenue` 的最早一年，不是 EBIT 率行的最早一年：建链时用的就是预估年集合，
    换成后者会在「有营收预期、那年缺利润预期」的公司上算出另一年的率。
    这一行不领原料，纯按行取值，所以天生是活的；但 `ebit_margin_ltm` 从 M2 起会被营收改动拖着走，
    没有锚点的公司若不同步重导这里，路径首年就会停在一个新 LTM 从没参与过的数上。
    """
    margins = row_years(inputs.get("ebit_margin_consensus"))
    anchor_years = sorted(row_years(inputs.get("consensus_revenue")))
    if anchor_years and anchor_years[0] in margins:
        return round(margins[anchor_years[0]], 6)
    ltm = _number(inputs.get("ebit_margin_ltm"))
    return None if ltm is None else round(ltm, 6)


def derive_da_to_revenue_ltm(inputs: dict) -> float | None:
    """折旧摊销比率：退过锚的那一批，分母是同一窗口的营收，不是「营收 LTM」那一行。

    季报没编现金流量表补充资料时，建链会把窗口退到最近真报了数的一期，分子分母同取那段十二个月。
    改营收 LTM 仍然拖着这一行走，但缩放的是那段窗口营收：拿全窗营收去除退锚的分子就成了跨窗口相除，
    而把分母钉死又会让这条边在图上明明是活的、改了却不认账。
    """
    raw = _raw_for(inputs, "da_to_revenue_ltm")
    window = _number(raw.get("da_window_revenue")) if raw else None
    if window is None:
        return _ltm_ratio(inputs, "da_to_revenue_ltm", "da_ltm")
    top = _number(raw.get("da_ltm"))
    if top is None:
        return None
    built = _number(raw.get("revenue_ltm"))
    current = _number(inputs.get("revenue_ltm"))
    if built and current is not None:
        window = window * current / built
    return round(top / window, 6) if window else None


def derive_capex_to_revenue_ltm(inputs: dict) -> float | None:
    return _ltm_ratio(inputs, "capex_to_revenue_ltm", "capex_ltm")


def derive_cost_of_debt_pretax(inputs: dict) -> float | None:
    """税前债务成本：分子（利息费用 LTM）存着，分母跟着被改的有息负债走，护栏照建链时那一套。

    实测被护栏拒掉时用惯例值——这与 `build_ladder` 的择一同一条规则，所以「把债务改成零」读到的是
    3.10%，而不是一个越出区间的噪声比。
    """
    raw = _raw_for(inputs, "cost_of_debt_pretax")
    interest = _number(raw.get("interest_ltm")) if raw else None
    debt = _number(inputs.get("interest_bearing_debt"))
    if interest is None or debt is None:
        return None
    prior = _number(raw.get("prior_debt"))
    revenue = _number(inputs.get("revenue_ltm"))
    # raw 与行同单位（亿元），`measure_cost_of_debt` 的护栏按元判，所以全部换回元再交给它。
    # 无息公司按建链同一条规则处理：当期债务为零就没有分母，不因「去年还有」而继续实测。
    avg = None if not debt else ((debt + prior) * YI / 2 if prior else debt * YI)
    measured, _ = measure_cost_of_debt(interest * YI, avg, None if revenue is None else revenue * YI)
    return round(PRETAX_COST_OF_DEBT if measured is None else measured, 6)


def derive_roic_ltm(inputs: dict) -> float | None:
    """ROIC = EBIT×(1−税率) / (净资产+有息负债−现金)：分子与净资产存着，债务、现金、税率用当前行。"""
    raw = _raw_for(inputs, "roic_ltm")
    ebit = _number(raw.get("ebit_ltm")) if raw else None
    if ebit is None:
        return None
    tax = _number(inputs.get("tax_rate_ltm"))
    if tax is None:
        tax = _number(inputs.get("tax_rate_hist")) or 0.0
    invested = (
        (_number(raw.get("total_equity")) or 0.0)
        + (_number(inputs.get("interest_bearing_debt")) or 0.0)
        - (_number(inputs.get("cash_and_financial_assets")) or 0.0)
    )
    if not invested:
        return None
    return round(ebit * (1.0 - tax) / invested, 6)


# 行 key → 重导函数。函数只读 `inputs`（链上各行的当前值 + 各自的 raw），不碰原始报表。
DERIVE: dict[str, Callable[[dict], Any]] = {
    "revenue_growth_path": derive_revenue_growth_path,
    "horizon_years": derive_horizon_years,
    "revenue_path": derive_revenue_path,
    "revenue_cagr_5y": derive_revenue_cagr_5y,
    "ebit_margin_path": derive_ebit_margin_path,
    "ebit_margin_ltm": derive_ebit_margin_ltm,
    "ebit_margin_start": derive_ebit_margin_start,
    "da_to_revenue_ltm": derive_da_to_revenue_ltm,
    "capex_to_revenue_ltm": derive_capex_to_revenue_ltm,
    "cost_of_debt_pretax": derive_cost_of_debt_pretax,
    "roic_ltm": derive_roic_ltm,
    "net_debt": derive_net_debt,
    "equity_weight": derive_equity_weight,
    "exit_ev_ebitda": derive_exit_ev_ebitda,
    "pe_ttm": derive_pe_ttm,
}

_CHILDREN: dict[str, list[str]] = {}
for _child, _parents in DERIVES.items():
    for _parent in _parents:
        _CHILDREN.setdefault(_parent, []).append(_child)


def downstream_of(keys: Any) -> set[str]:
    """改动会波及到的所有下游行（含间接）。"""
    seen: set[str] = set()
    queue = list(keys)
    while queue:
        node = queue.pop()
        for child in _CHILDREN.get(node, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def _depth(key: str, trail: frozenset[str] = frozenset()) -> int:
    """离最近上游的距离，用来排重算顺序；环由 trail 挡住（DERIVES 本就无环，这里只是不许它悄悄长出来）。"""
    if key in trail:
        raise ValueError(f"derives 图上有环：{key}")
    parents = [parent for parent in DERIVES.get(key, ()) if parent in DERIVE]
    if not parents:
        return 0
    return 1 + max(_depth(parent, trail | {key}) for parent in parents)


_ORDER: tuple[str, ...] = tuple(sorted(DERIVE, key=lambda key: (_depth(key), key)))


def recompute_derived(inputs: dict, manual: Any) -> dict:
    """把 `inputs` 里被改动波及的派生行重算一遍；`manual` 点名的那些行原样不动。"""
    targets = downstream_of(manual) & set(DERIVE) - set(manual)
    if not targets:
        return inputs
    out = dict(inputs)
    for key in _ORDER:
        if key not in targets:
            continue
        value = DERIVE[key](out)
        if value is not None:
            out[key] = value
    return out


# 靠原料（raw）才活着的那几行，以及各自**必须**用到的原料字段：字段不齐就是这条边不通，一个数都不会动。
# 列出来是为了让视图能按**这一批次**决定实线虚线，而不是按代码里有没有 derive()——同一份代码服务老存档、
# 或服务一家报表里根本没有利息支出的公司时，那几条边确实是不通的。
NEEDS_RAW = {
    "revenue_cagr_5y": ("first_year", "first_revenue", "last_year"),
    "ebit_margin_ltm": ("ebit_ltm",),
    "da_to_revenue_ltm": ("da_ltm",),
    "capex_to_revenue_ltm": ("capex_ltm",),
    "cost_of_debt_pretax": ("interest_ltm",),
    "roic_ltm": ("ebit_ltm",),
}


def has_material(inputs: dict, key: str) -> bool:
    raw = _raw_for(inputs, key) or {}
    return all(raw.get(field) is not None for field in NEEDS_RAW.get(key, ())) and bool(raw)


def live_rows(inputs: dict) -> frozenset[str]:
    """这一份 inputs 里当场真会重算的行（不含 cost_of_equity/wacc，那两行由折现率级联管）。"""
    return frozenset(key for key in DERIVE if key not in NEEDS_RAW or has_material(inputs, key))
