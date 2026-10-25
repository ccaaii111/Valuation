"""把假设链摊成一张有层级的推导图——只换画法，不新算任何数字。

边来自 `assumptions.DERIVES`：建链那一刻的**出处**关系。它和引擎运行时的重算是两件事，图上必须分得开，
否则用户会以为改 `revenue_path` 的上游能回头改它——它不会。`recompute.DERIVE` 管的就是「此刻还能不能
当场重算」这一层，节点上的 `live` 把它搬进图里：活边实线，只在建链时算过的那条虚线。

`live` 是**这一批次**的属性而不是代码的属性：原料齐不齐看 baseline.json 里有没有 `raw`，同一份代码
服务 M2 之前存的老批次时，那六条边就是不通的。

层号（=列位）在这里算而不是在浏览器里算：面板的行序**不是**拓扑序（`terminal_growth` 在终值组里，
却是显性期 `revenue_growth_path` 的上游），借行序画会画出从右往左的线。

`FORMULAS` 是符号式，`SUBSTITUTIONS` 只声明「哪个符号是图上哪一行」——代进去的数由页面用自己的
`fmt()` 取当前值，所以这里仍然不出现任何算术，放大页的数字与图上那一格必然同源。
"""
from __future__ import annotations

import re

from backend.model.assumptions import (
    GROWTH_WORST_WINDOW,
    HORIZON_MAX_YEARS,
    HORIZON_MIN_YEARS,
    HORIZON_TARGET_YEARS,
    MARGIN_LOOKBACK,
    PRETAX_COST_OF_DEBT,
    SUSTAINABLE_WINDOW,
    TERMINAL_GROWTH,
)
from backend.model.attribution import flat_rows
from backend.model.dcf import DERIVED_INPUTS, inputs_from_ladder
from backend.model.recompute import live_rows

# 每行「这一格上的数是怎么来的」一句话写清，图上与侧栏共用。只作显示：改它不动任何数字，
# 也不进 baseline.json——tests/test_derivation_graph.py 要求凡有上游的行必须有式子，漏填即红。
# 常数从 assumptions 里引，惯例值调了公式不会烂。
FORMULAS: dict[str, str] = {
    "revenue_cagr_5y": f"CAGR = (末年营收 ÷ 首年营收)^(1/年数) − 1，取近 {MARGIN_LOOKBACK} 年年报",
    "anchor_source": "有机构一致预期营收的年份＝锚点年；一年都没有＝整段回退到历史增速",
    "revenue_growth_path": "有锚年：g = 预期营收 ÷ 上年营收 − 1；无锚年：自末锚增速线性回落到永续 g，走完的年份数＝显性预测期",
    "revenue_path": "营收_t = 营收_(t−1) × (1 + g_t)；落在锚点年的直接取锚点值",
    "ebit_margin_ltm": "EBIT 率 = (营业利润 + 财务费用中的利息费用 − 公允价值变动收益 − 非经常性损益剔除额) ÷ LTM 营业总收入",
    "ebit_margin_hist": "近 5 年年报的 (调整后 EBIT − 各年非经常性损益剔除额) ÷ 各年营业总收入，取均值",
    "ebit_margin_start": "一致预期首年 营业利润 ÷ 营收；缺该年预期时退回上一行的 EBIT 率 LTM",
    "ebit_margin_path": f"有预期年取预期；其后逐年向近 {MARGIN_LOOKBACK} 年均值线性回落",
    "da_to_revenue_ltm": "LTM 折旧摊销 ÷ LTM 营业总收入",
    "capex_to_revenue_ltm": "LTM 购建固定资产等支付现金 ÷ LTM 营业总收入",
    "net_debt": "有息负债（含租赁负债） − (货币资金 + 交易性金融资产)",
    "roic_ltm": "NOPAT ÷ 投入资本 = LTM EBIT × (1 − 有效税率) ÷ (净资产 + 有息负债 − 现金)",
    "risk_free": "按 --risk-free-basis 在「即期」与「近 5 年中枢」间择一；另一行只作对照，不进级联",
    "cost_of_debt_pretax": f"利息费用 LTM ÷ 平均有息负债；越出护栏或合理区间时退回惯例 {PRETAX_COST_OF_DEBT:.1%}",
    "equity_weight": "总市值 ÷ (总市值 + 有息负债)",
    "cost_of_equity": "CoE = rf + β × ERP（ERP 取级联那一行，实测窗口与口径写在该行凭证里）",
    "wacc": "WACC = w_e × CoE + (1 − w_e) × Kd × (1 − t)",
    "terminal_growth": (
        f"按 --terminal-growth-basis 择一：保守＝近 {GROWTH_WORST_WINDOW} 年最差增速之半 ／ "
        f"中枢＝近 5 年营收加权 ／ 乐观＝历史最好年度增速 ／ "
        f"再投资＝近 {SUSTAINABLE_WINDOW} 个财年 (1−PO)·ROE 之中位；"
        f"测不出来退回惯例 {TERMINAL_GROWTH:.1%}；进级联那一档再取 max(0, 实测)"
    ),
    "exit_ev_ebitda": "(总市值 + 净债务 + 少数股东权益) ÷ (LTM 营收 × (LTM EBIT 率 + 折旧摊销率))",
    "pe_ttm": "滚动市盈率 = 总市值 ÷ 归母净利润 LTM（行情通道给的市盈率不进这一行，两条通道口径不同）",
    "horizon_years": (
        f"显性年数 = 「显性预测期」压进 [max({HORIZON_MIN_YEARS}, 一致预期覆盖年数 + 1), {HORIZON_MAX_YEARS}]，"
        f"默认 {HORIZON_TARGET_YEARS} 年"
    ),
}

# 代入式：把符号式里的每个符号换成「这一行当前的数」，让放大页能说出「这一步到底代了多少」。
# 占位符 `{行 key}` 由前端用唯一的 fmt() 填，所以数字与该行的当前值同源——后端不在此重算任何算术，
# 也只在下发「哪一格是哪一个符号」这件事。
#
# 只给「每个符号都在这行的上游里」的行写模板。分子是序列端点或不落在 derives 里的量（LTM EBIT、
# 净资产、末年营收、利息费用）就不硬凑：宁可这一行退回「符号式 + 上游当前值」，也不写出一个
# 代不进去的式子——那是最难发现的假精确。
SUBSTITUTIONS: dict[str, str] = {
    "net_debt": "净债务 = {interest_bearing_debt} − {cash_and_financial_assets}",
    "equity_weight": "w_e = {market_cap} ÷ ({market_cap} + {interest_bearing_debt})",
    "cost_of_equity": "CoE = {risk_free} + {beta} × {erp}",
    "wacc": "WACC = {equity_weight} × {cost_of_equity} + (1 − {equity_weight}) × {cost_of_debt_pretax} × (1 − {tax_rate_ltm})",
    "exit_ev_ebitda": "EV/EBITDA = ({market_cap} + {net_debt} + {minority_interest}) ÷ ({revenue_ltm} × ({ebit_margin_ltm} + {da_to_revenue_ltm}))",
    "pe_ttm": "PE = {market_cap} ÷ {net_profit_ltm}",
}

PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def substitution(key: str, upstream: list[str]) -> dict:
    """这一行的代入式模板，以及它引用到的上游行（去重，一个符号出现两次算一行）；
    任一占位符不在这行的上游里，整条模板就不出。"""
    template = SUBSTITUTIONS.get(key) or ""
    refs = list(dict.fromkeys(match.group(1) for match in PLACEHOLDER.finditer(template)))
    if not template or any(ref not in upstream for ref in refs):
        return {"template": "", "refs": []}
    return {"template": template, "refs": refs}


def _parents(rows: dict[str, dict]) -> dict[str, list[str]]:
    """上游只认这张图里真有的行：老批次／无租赁口径下没有那一行，边就不画，而不是画向虚无。"""
    return {key: [p for p in row["derives"] if p in rows and p != key] for key, row in rows.items()}


def _layers(parents: dict[str, list[str]]) -> dict[str, int]:
    """最长上游链＝离原始取数有几步。零上游（原始取数或惯例值）落在第 0 列，也就是图的最左侧。"""
    layers: dict[str, int] = {}
    active: set[str] = set()

    def depth(key: str) -> int:
        if key in layers:
            return layers[key]
        if key in active:
            raise ValueError(f"推导关系成环，无法分层：{' → '.join([*active, key])}")
        active.add(key)
        upstream = parents[key]
        value = 0 if not upstream else 1 + max(depth(node) for node in upstream)
        active.discard(key)
        layers[key] = value
        return value

    for key in parents:
        depth(key)
    return layers


def graph(ladder: dict) -> dict:
    """节点带机器基线与「能不能改」，边带方向（from 上游 → to 本行）；实时值走归因载荷。"""
    rows = flat_rows(ladder)
    by_key = {row["key"]: row for row in rows}
    if len(by_key) != len(rows):
        raise ValueError("假设链里有重名行，推导图无法按 key 接线")
    parents = _parents(by_key)
    layers = _layers(parents)
    # 实线＝改上游这一格当场会重算；虚线＝只在建链那一刻算过一次。按批次判定，不按代码判定。
    live = live_rows(inputs_from_ladder(ladder)) | DERIVED_INPUTS
    children: dict[str, list[str]] = {key: [] for key in by_key}
    for key, upstream in parents.items():
        for node in upstream:
            children[node].append(key)

    nodes = [
        {
            "key": key,
            "label": row["label"],
            "unit": row["unit"],
            "group": row["group"],
            "group_title": row["group_title"],
            "kind": row.get("kind"),
            "source": row.get("source"),
            "note": row.get("note"),
            "evidence": row.get("evidence") or "",
            "formula": FORMULAS.get(key, ""),
            "substitute": substitution(key, parents[key]),
            "confidence": row.get("confidence"),
            "editable": row["editable"],
            "derived": row["derived"],
            "value_type": row["value_type"],
            "edit": row["edit"],
            "baseline": row["baseline"],
            "layer": layers[key],
            "order": index,
            "upstream": parents[key],
            "downstream": children[key],
            # 只有明确 False 才画虚线：估值段的节点由引擎每次现算，不带这个字段，默认就是实线。
            "live": key in live,
        }
        for index, (key, row) in enumerate(by_key.items())
    ]
    # 同一列里按建链顺序堆叠，同组的行因此挨在一起；列与列的先后才由层号决定。
    nodes.sort(key=lambda node: (node["layer"], node["order"]))
    edges = [
        {"from": node, "to": key, "layer": layers[node], "live": key in live}
        for key, upstream in parents.items() for node in upstream
    ]
    return {
        "nodes": nodes,
        "edges": sorted(edges, key=lambda edge: (edge["layer"], edge["to"])),
        "columns": (max(layers.values()) + 1) if layers else 0,
        "roots": sorted(key for key, upstream in parents.items() if not upstream),
    }
