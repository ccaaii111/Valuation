"""Builds the machine baseline for the assumption ladder.

Each row carries a baseline value plus provenance so the panel can show what a human
changed and how far the final value moved because of it.

Two text fields, split on purpose: `note` says what the row is and what it does in the cascade
(no numbers — 2026-10-13 定的口径：说明只讲意义与作用), `evidence` says how this batch's number was
produced (window, sample, guardrail verdict, the fetch that backed it). The panel shows the note and
hangs the evidence on hover; the derivation graph shows both stacked in its sidebar.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.model import beta as beta_model
from backend.model import normalize as normalize_model
from backend.model import periods as P

# Revenue anchors on 营业总收入 because broker consensus is built on that line, not on 营业收入.
REVENUE_KEYS = ("TOTAL_OPERATE_INCOME",)
# EBIT 的基线取营业利润，再做两项科目调整。
# 加回 FE_INTEREST_EXPENSE：营业利润已经把它扣掉了，而 WACC 里另外按债务成本计价，不加回来等于利息扣两遍。
# 剔除 FAIRVALUE_CHANGE_INCOME：这笔收益对应的交易性金融资产，在资本结构那一档已经按现金从企业价值里扣过
# 一次（CASH_KEYS 含 TRADE_FINASSET），留在 EBIT 里就是同一笔资产计两遍。
# 两项字段缺失或为 0 时调整量为 0，即退回「EBIT＝营业利润」。
# 投资收益与对联营合营企业的投资收益不剔除：它们对应的长期股权投资没有从企业价值里单独扣过，
# 剔掉收益会把这块价值整个丢掉。
EBIT_BASE_KEY = "OPERATE_PROFIT"
EBIT_ADJUSTMENTS = (("FE_INTEREST_EXPENSE", 1.0), ("FAIRVALUE_CHANGE_INCOME", -1.0))
EBIT_CAPTIONS = {
    "FE_INTEREST_EXPENSE": "财务费用中的利息费用",
    "FAIRVALUE_CHANGE_INCOME": "公允价值变动收益",
    EBIT_BASE_KEY: "营业利润",
}
DA_KEYS = ("FA_IR_DEPR", "IA_AMORTIZE", "LPE_AMORTIZE", "USERIGHT_ASSET_AMORTIZE")
# ROEJQ 是东财自己在「主要财务指标」里给的加权净资产收益率（%）。用它而不是拿净利润÷净资产重算：
# 加权口径已经处理过年内增发与回购的时点，重算只会引入一个跟报表不同的第二套净资产定义。
ROE_KEY = "ROEJQ"
SHORT_DEBT_KEYS = ("SHORT_LOAN", "NONCURRENT_LIAB_1YEAR", "SHORT_BOND_PAYABLE")
LONG_DEBT_KEYS = ("LONG_LOAN", "BOND_PAYABLE", "PERPETUAL_BOND_PAYBALE")
# LEASE_LIAB is the non-current portion only; the current portion already sits in 一年内到期的非流动负债.
LEASE_DEBT_KEYS = ("LEASE_LIAB",)
CASH_KEYS = ("MONETARYFUNDS", "TRADE_FINASSET")
# GBALANCE carries both the combined caption (应收票据及应收账款) and its subset (应收账款);
# summing across a group double-counts the subset, so within a group the first present key wins.
RECEIVABLE_GROUPS = (("NOTE_ACCOUNTS_RECE", "ACCOUNTS_RECE"), ("INVENTORY",))
PAYABLE_GROUPS = (("NOTE_ACCOUNTS_PAYABLE", "ACCOUNTS_PAYABLE"), ("TAX_PAYABLE",))

CAPEX_KEY = "CONSTRUCT_LONG_ASSET"
BORROW_IN_KEY = "RECEIVE_LOAN_CASH"
REPAY_OUT_KEY = "PAY_DEBT_CASH"
# 财务费用下的利息支出。FINANCE_EXPENSE 是财务费用净额（茅台为负，因为存款利息抵过了），不能当分子。
INTEREST_EXPENSE_KEYS = ("FE_INTEREST_EXPENSE",)
# 滚动市盈率的分子。用归母而不是净利润：市值买的是归属普通股股东那一份，少数股东那份利润不在里面。
PARENT_NET_PROFIT_KEY = "PARENT_NETPROFIT"

# 显性预测期是口径选择，不是测算结果：进级联的年数由人定（默认 10 年，评估实务那句「一般 8–12 年」），
# 每年回落几个点是它的输出。稳态不测——它由「第几年进入稳态」这个选择定义，所以回落段必须走完在显性期内，
# 否则末年与永续 g 之间剩下一截没人负责。
HORIZON_TARGET_YEARS = 10
HORIZON_MIN_YEARS = 5  # 准则下限：少于 5 年谈不上「详细预测」
HORIZON_MAX_YEARS = 12  # 上限：更远的稳态看不见，多出来的年数只是把永续假设摊长
# 年数是输入，「每年掉几个点」是它的输出；输出离谱就说明这个输入对这只票不合适。
# 掉得太急是硬着陆（要么加长年数要么换 g 档），几乎不掉则是它早就贴着 g、拉长只是在摊开永续假设。
# 两条都只报数不改数——与债务成本、ERP、rf 那几处护栏同一套做法。
GLIDE_MAX_PER_YEAR = 0.03
GLIDE_MIN_PER_YEAR = 0.001
TERMINAL_GROWTH = 0.025
ERP = 0.060
PRETAX_COST_OF_DEBT = 0.031
MARGIN_LOOKBACK = 5

# 营收增速序列的三情形窗口（进级联的第四档不看这条序列，见下面 SUSTAINABLE_WINDOW）。
# 中枢用「近五年营收增量合计 ÷ 基数合计」（即以上一年营收加权的年均增速），
# 而非各年增速的算术平均：一个小年撬不动大体量年份。
# 保守取近十年最差年度的「一半」：一年的休克（茅台 2025 −1.2%、中石化 2020 −28.9%）不该原样永续下去，
# 但也不该被均值抹平。少于三个年度增速就无所谓"最差"，此时退回惯例值并说明原因。
GROWTH_CENTRAL_WINDOW = 5
GROWTH_WORST_WINDOW = 10
GROWTH_MIN_OBSERVATIONS = 3
# 第四档：Higgins 的再投资增长率 g = (1−PO)·ROE。取最近 SUSTAINABLE_WINDOW 个已实施现金分配的财年，
# 逐年配对后取中位数；能配对的年份少于 SUSTAINABLE_MIN_YEARS 个就不叫「稳定分红」，这一档作废而不是硬算。
SUSTAINABLE_WINDOW = 5
SUSTAINABLE_MIN_YEARS = 4
TERMINAL_GROWTH_BASIS = ("conservative", "central", "optimistic", "sustainable", "convention")

# 债务成本实测的护栏。分母一小，比值就变成噪声：茅台平均有息负债 2.6 亿（几乎全是租赁负债），
# 0.45 亿利息费用能算出 17.7% 的"债务成本"。越界一律退回惯例值，并把被拒的算法写进 note。
DEBT_COST_MIN_ABSOLUTE = 1.0e8
DEBT_COST_MIN_OF_REVENUE = 0.005
DEBT_COST_RANGE = (0.005, 0.12)

# β 只有一个下限是算术性的：回归窗口一短，测到的是风格背离而不是系统性风险（2025~2026 中证500 +12%
# 而茅台/中石化各自下跌，两家 52 周 β 都成了显著的负值）。β≤0 会把股权成本压到无风险利率以下，
# Gordon 的 1/(r−g) 直接翻号，所以这种情形不出数（见 measure_beta）。除此之外测到多少用多少：
# 实务样本周数、R²、|β| 大小只写进 note 当证据，不再替换数值。
BETA_PRACTICE_MIN_WEEKS = 104

# 股权风险溢价（ERP）的实测窗口。级联固定取近 3 年：默认基准 932000 由中证在 2023-08-11 正式发布，
# 近 3 年那一档整段落在发布日之后，量到的是真实行情；近 5 年与近 10 年的头几年是指数公司的回算段，
# 官网把回算历史与真实历史混在同一条序列里交出，取数层分辨不出（见 docs/caliber.md §12）。其余三档并列做参考行，
# 因为 realized ERP 对窗口极敏感（同一批 2026-10-14 的数：今年 −10.6%、近 3 年 +6.9%、
# 近 5 年 +3.5%、近 10 年 −2.1%），只给一个数等于把窗口选择这件事藏起来。
ERP_CASCADE_WINDOW = "3y"
ERP_WINDOW_LABELS = {"ytd": "今年以来", "3y": "近 3 年", "5y": "近 5 年", "10y": "近 10 年"}
# 四档从短到长固定排；级联那一档进定价，其余三档是它的参考行。级联换档时两张表自动跟着换，
# 不再有写死的「今年／近 3／近 10」三元组。
ERP_WINDOW_ORDER = ("ytd", "3y", "5y", "10y")
ERP_REFERENCE_WINDOWS = tuple(window for window in ERP_WINDOW_ORDER if window != ERP_CASCADE_WINDOW)
# realized ERP 的可用区间。下界 0%：窗口整体下跌时超额收益为负，CAPM 会折出低于无风险利率的股权成本，
# 那已经不是风险定价；上界 15%：一轮牛市尾巴年化出来的数不该被当成永续的风险补偿。
ERP_RANGE = (0.0, 0.15)

# 面板的编辑口径：金额行说「比现在多几个点」，所以填相比基线的 ±%；比率行本身已是百分数，
# 再套一层百分比会和「改动%」列撞车，参数行（β/ERP/rf/g/倍数/价格）更是直接填数值。
MONEY_UNITS = ("亿元", "亿股")

# 每一行的上游是谁——建链那一刻的事实，不是引擎运行时的重算关系。
#
# 两者必须分清：`wacc` 会随 rf/β/税率 当场重算（`dcf.DERIVED_INPUTS` 管这一层），而 `revenue_path`
# 只在 build_ladder 里由锚点与增速路径算出一次，之后改上游不会回头改它——它记下的是「这个数当时怎么来的」。
# 推导图（M5）靠这张表画线，所以宁可少写也别写错：只列**本身也是假设行**的上游，
# 来自报表原始科目的都留空（它们的凭证在 source 与 manifest 里，不在行与行之间）。
# key 不在表里＝该行没有上游（原始取数或惯例值），漏填会被 tests/test_derivation.py 的哨兵抓住。
DERIVES: dict[str, tuple[str, ...]] = {
    # 收入：锚点 → 增速 → 绝对额
    "revenue_cagr_5y": ("revenue_base",),
    "anchor_source": ("consensus_revenue",),
    "revenue_growth_path": ("consensus_revenue", "revenue_base", "revenue_cagr_5y", "terminal_growth", "horizon_target"),
    "revenue_path": ("revenue_growth_path", "consensus_revenue", "revenue_base"),
    # 盈利：首年取一致预期、缺锚退 LTM，其后向近五年均值回落。年份跟着增速路径走，所以那条边也在此声明。
    # 两档 EBIT 率的分子都减过非经常性损益，所以那条边从「剔除额」行出发——关掉它两行都退回报表原值。
    "ebit_margin_ltm": ("revenue_ltm", "normalization_strip"),
    "ebit_margin_hist": ("normalization_strip",),
    "ebit_margin_start": ("ebit_margin_consensus", "ebit_margin_ltm", "consensus_revenue"),
    "ebit_margin_path": ("revenue_growth_path", "ebit_margin_start", "ebit_margin_consensus", "ebit_margin_hist"),
    # 再投资：LTM 比率的分母是 LTM 营收
    "da_to_revenue_ltm": ("revenue_ltm",),
    "capex_to_revenue_ltm": ("revenue_ltm",),
    # 桥与回报
    "net_debt": ("interest_bearing_debt", "cash_and_financial_assets"),
    "roic_ltm": ("tax_rate_ltm", "tax_rate_hist", "interest_bearing_debt", "cash_and_financial_assets"),
    # 折现率：rf 在即期与中枢之间择一，级联两行由引擎当场重算
    "risk_free": ("risk_free_spot", "risk_free_norm"),
    "cost_of_debt_pretax": ("interest_bearing_debt",),
    "equity_weight": ("market_cap", "interest_bearing_debt"),
    "cost_of_equity": ("risk_free", "beta", "erp"),
    "wacc": ("equity_weight", "cost_of_equity", "cost_of_debt_pretax", "tax_rate_ltm", "tax_rate_hist"),
    # 终值：g 在四种情形之间择一（三档营收增速 + 一档再投资增速），退出倍数用现价反推
    "terminal_growth": (
        "terminal_growth_central",
        "terminal_growth_optimistic",
        "terminal_growth_sustainable",
    ),
    "exit_ev_ebitda": ("market_cap", "revenue_ltm", "ebit_margin_ltm", "da_to_revenue_ltm", "net_debt", "minority_interest"),
    # 退出倍数用的滚动市盈率是自算的，所以它有上游：改总市值或改归母净利 LTM，这一行跟着重算。
    "pe_ttm": ("market_cap", "net_profit_ltm"),
    "horizon_years": ("revenue_growth_path",),
}


@dataclass
class Row:
    key: str
    label: str
    unit: str
    baseline: Any
    source: str
    kind: str = "data"  # data | estimate | convention | derived
    confidence: float | None = None
    note: str = ""  # 这一行是什么、在级联或引擎里起什么作用——不写数字，数字归 evidence
    evidence: str = ""  # 这次取值怎么来的：窗口、样本、算式各段、护栏判定、取数函数与时刻
    edit: str = ""  # delta（相比基线 ±%）| value（直接填数值），留空按单位推断
    derives: tuple[str, ...] = ()  # 上游行的 key，留空则查 DERIVES 表
    # 建链那一刻用掉的原始标量（金额一律与行同单位：亿元）。存它只为让「这一行怎么来的」那条边在
    # 运行时也能重算：分子分母只活在报表里，改上游就没有第二次机会。缺 raw 的老批次照旧——
    # 那条边在图上就是虚线（见 docs/caliber.md §4.6「哪条边当场算」）。
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.edit = self.edit or ("delta" if self.unit in MONEY_UNITS else "value")
        self.derives = tuple(self.derives) or DERIVES.get(self.key, ())

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "baseline": self.baseline,
            "source": self.source,
            "kind": self.kind,
            "confidence": self.confidence,
            "note": self.note,
            "evidence": self.evidence,
            "edit": self.edit,
            "derives": list(self.derives),
            **({"raw": self.raw} if self.raw else {}),
        }


@dataclass
class Ladder:
    ticker: dict
    as_of: str
    groups: list[dict] = field(default_factory=list)
    # 建链那一刻「这一格不是实测」的顶层告示。行 note 里也写着同样的话，但 note 要逐行点开才看得到，
    # 而这类降级会一路改到折现率与每股价值，必须进级联结果旁边。
    warnings: list[str] = field(default_factory=list)

    def rows(self) -> list[Row]:
        return [row for group in self.groups for row in group["rows"]]

    def to_json(self) -> dict:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of,
            "groups": [
                {"id": g["id"], "title": g["title"], "rows": [r.to_json() for r in g["rows"]]} for g in self.groups
            ],
            "warnings": list(self.warnings),
        }


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def _sum(row: dict | None, keys: Iterable[str]) -> float | None:
    if not row:
        return None
    values = [P.num(row, k) for k in keys]
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _sum_groups(row: dict | None, groups: Iterable[Iterable[str]]) -> float | None:
    """Sum one value per group; within a group the first key present wins.

    GBALANCE reports both a combined caption and its subset (应收票据及应收账款 / 应收账款),
    so a flat sum charges the subset twice and can flip the sign of working capital.
    """
    if not row:
        return None
    total, seen = 0.0, False
    for keys in groups:
        value = P.num(row, *keys)
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def _ebit(row: dict | None) -> float | None:
    """EBIT for one income-statement row: 营业利润 plus the signed caption adjustments."""
    if not row:
        return None
    base = P.num(row, EBIT_BASE_KEY)
    if base is None:
        return None
    return base + sum(weight * (P.num(row, key) or 0.0) for key, weight in EBIT_ADJUSTMENTS)


def _ebit_ltm(rows: Iterable[dict]) -> float | None:
    """Same, on a trailing-twelve-month basis: each caption rolls up on its own, missing = 0."""
    ordered = list(rows)
    base = P.ltm(ordered, EBIT_BASE_KEY)
    if base is None:
        return None
    return base + sum(weight * (P.ltm(ordered, key) or 0.0) for key, weight in EBIT_ADJUSTMENTS)


def _ebit_bridge_ltm(rows: Iterable[dict]) -> list[tuple[str, float]]:
    """The captions that actually moved EBIT this batch, with their signed LTM amounts."""
    ordered = list(rows)
    out = []
    for key, weight in EBIT_ADJUSTMENTS:
        value = P.ltm(ordered, key)
        if value:
            out.append((key, weight * value))
    return out


def _ebit_evidence(bridge: list[tuple[str, float]]) -> str:
    base = f"基线 {EBIT_CAPTIONS[EBIT_BASE_KEY]}（{EBIT_BASE_KEY}）"
    if not bridge:
        return f"{base}；两项调整科目本期为 0 或缺失，未作调整"
    terms = "；".join(
        f"{EBIT_CAPTIONS[key]}（{key}）{'+' if amount > 0 else '−'}{_fmt_yi(abs(amount))}"
        for key, amount in bridge
    )
    return (
        f"{base}；LTM 调整项：{terms}。"
        "投资收益与对联营合营企业的投资收益不剔除——它们对应的长期股权投资没有从企业价值里单独扣过"
    )


def _margin_basis_gap(consensus: dict[int, float], ltm: float | None, basis: str = "调整后") -> str:
    """券商那档是营业利润率、历史那档是机器 EBIT 率：两个数并列摆出，不做换算。"""
    if not consensus:
        return "本批次无一致预期锚点"
    mean_consensus = sum(consensus.values()) / len(consensus)
    years = "、".join(str(year) for year in sorted(consensus))
    measured = f"本批次无{basis} EBIT 率可比" if ltm is None else f"{basis}口径 LTM {ltm:.2%}"
    return (
        f"锚定年份 {years}，券商营业利润率均值 {mean_consensus:.2%}；{measured}。"
        "两档分子定义不同：券商档只预测营业利润，科目调整与非经常性损益剔除都不做；"
        "一个是未来、一个是历史，这两个数不能互为校验"
    )


def _series_ratio(numerators: dict[int, float | None], denominators: dict[int, float | None]) -> dict[int, float]:
    out = {}
    for year, numerator in numerators.items():
        ratio = _ratio(numerator, denominators.get(year))
        if ratio is not None:
            out[year] = ratio
    return out


def _tail(values: dict[int, float], count: int) -> list[float]:
    return [v for _, v in sorted(values.items())[-count:]]


def confidence_from_coverage(org_count: int | None) -> float | None:
    """Coverage-based heuristic: 30+ institutions is treated as fully anchored."""
    if not org_count:
        return None
    return round(min(1.0, org_count / 30.0), 3)


def parse_consensus(payload: dict | None) -> dict:
    payload = payload or {}
    actual: dict[int, dict] = {}
    estimate: dict[int, dict] = {}
    for row in payload.get("yctj_list") or []:
        year = row.get("YEAR")
        if not year:
            continue
        target = actual if row.get("YEAR_MARK") == "A" else estimate
        target[int(year)] = {
            "revenue": row.get("TOTAL_OPERATE_INCOME"),
            "net_profit": row.get("PARENT_NETPROFIT"),
            "operate_profit": row.get("OPERATE_PROFIT"),
            "eps": row.get("EPS"),
            "roe": row.get("ROE"),
            "eps_org_count": row.get("EPS_COUNT"),
            "revenue_org_count": row.get("TOTAL_OPERATE_INCOME_COUNT"),
        }
    for row in payload.get("yctj_chart") or []:
        year = row.get("YEAR")
        if year and row.get("YEAR_MARK") == "E" and int(year) in estimate:
            estimate[int(year)]["pe"] = row.get("PE")
    return {
        "actual": dict(sorted(actual.items())),
        "estimate": dict(sorted(estimate.items())),
        # Only forecast years carry real disagreement; disclosed years have a degenerate spread.
        "dispersion": {
            year: spread for year, spread in _institution_dispersion(payload.get("jgyc") or []).items()
            if year in estimate
        },
    }


def _institution_dispersion(jgyc: list[dict]) -> dict[int, dict]:
    """Scatters per-institution EPS by forecast year; pseudo rows aggregate others and would double count."""
    buckets: dict[int, list[float]] = {}
    for report in jgyc or []:
        if str(report.get("ORG_CODE")) == "00000000":
            continue
        name = str(report.get("ORG_NAME_ABBR") or "")
        if "平均" in name or "一致" in name:
            continue
        for slot in range(1, 5):
            year, eps = report.get(f"YEAR{slot}"), report.get(f"EPS{slot}")
            if year and isinstance(eps, (int, float)) and eps > 0:
                buckets.setdefault(int(year), []).append(float(eps))
    out: dict[int, dict] = {}
    for year, values in buckets.items():
        avg, sd = P.mean(values), P.stdev(values)
        out[year] = {
            "n": len(values),
            "mean": avg,
            "stdev": sd,
            "cv": _ratio(sd, avg),
            "min": min(values),
            "max": max(values),
        }
    return out


def _nwc(balance_row: dict | None) -> float | None:
    receivable = _sum_groups(balance_row, RECEIVABLE_GROUPS)
    payable = _sum_groups(balance_row, PAYABLE_GROUPS)
    if receivable is None or payable is None:
        return None
    return receivable - payable


def resolve_horizon(anchor_count: int, requested: float | None) -> tuple[int, str]:
    """显性期年数：人的选择，压在下限与上限之间；被压时说明是谁压的。

    下限取「锚点数 + 1」与 `HORIZON_MIN_YEARS` 的较大者，且**优先于上限**：券商给到 12 年时宁可行长
    一年也不把已有绝对额锚切掉——切掉的不是插值精度，是那年真实的预期营收。回落段至少要有一年，
    否则末年不等于永续 g 这件事没有年数去消化。
    """
    floor = max(HORIZON_MIN_YEARS, anchor_count + 1)
    wanted = HORIZON_TARGET_YEARS if requested is None or isinstance(requested, bool) else int(round(requested))
    if wanted < floor:
        return floor, f"填 {wanted} 年低于下限 {floor} 年（{HORIZON_MIN_YEARS} 年与锚点数 + 1 取大），按 {floor} 年走"
    if wanted > HORIZON_MAX_YEARS:
        capped = max(HORIZON_MAX_YEARS, floor)
        return capped, (
            f"填 {wanted} 年超过上限 {HORIZON_MAX_YEARS} 年，按 {capped} 年走"
            + (f"（锚点已占 {anchor_count} 年，上限让给锚点）" if capped > HORIZON_MAX_YEARS else "")
        )
    return wanted, ""


def glide_diagnostics(
    growth: dict[int, float], anchors: dict[int, float], target: float
) -> tuple[float | None, float | None]:
    """(每年回落幅度, 末年增速)——从已生成的增速行读，不重走一遍回落公式。

    线性回落的相邻差恰好等于「末年再走一步就到 g」，所以这两个量读最后一格就够了：显性期年数是输入，
    这一对是输出，只进凭证与警告，不是可编辑的假设。
    """
    tail_years = [year for year in sorted(growth) if year not in anchors]
    if not tail_years:
        return None, None
    last = growth[tail_years[-1]]
    return target - last, last


def horizon_warnings(horizon: int, clamp_note: str, fade: float | None, last_year: float | None, target: float) -> list[str]:
    """显性期这个选择在这只票身上是否说得出话：被压过、掉得太急、几乎不掉，三样各报一句。"""
    warnings = [clamp_note] if clamp_note else []
    if fade is None or last_year is None:
        return warnings
    direction = "回落" if fade < 0 else "抬升"
    if abs(fade) > GLIDE_MAX_PER_YEAR:
        warnings.append(
            f"显性期 {horizon} 年意味着增速每年{direction} {abs(fade):.2%}，超过 {GLIDE_MAX_PER_YEAR:.1%} 的常理上限："
            f"末锚要走到永续 {target:.2%} 只能硬着陆，加长显性期或换 g 档"
        )
    elif abs(fade) < GLIDE_MIN_PER_YEAR:
        warnings.append(
            f"显性期 {horizon} 年意味着增速每年只变 {abs(fade):.2%}，末年 {last_year:.2%} 与永续 {target:.2%} 已经贴着："
            "这家公司其实早就接近稳态，这些年只是在把永续假设摊开"
        )
    return warnings


def glide_growth(
    anchors: dict[int, float],
    base_revenue: float | None,
    fallback_growth: float | None,
    target: float,
    years: Iterable[int],
) -> dict[int, float]:
    """锚点年取相邻营收之比，其余年份从最后一个锚点增速线性落到 `target`。

    增速是比值，分子分母同单位即可（建链时喂元，重算时喂行里存的亿元）。`years` 决定这一行覆盖哪几年：
    建链与重算都按「显性预测期」那一行现算（见 `recompute._horizon_years`），改 g 只换回落的终点，
    改年数才换年份数。
    """
    growth: dict[int, float] = {}
    prior = base_revenue
    for year in sorted(anchors):
        if prior:
            growth[year] = anchors[year] / prior - 1.0
        prior = anchors[year]

    anchor_end = max(anchors) if anchors else None
    last_growth = growth.get(anchor_end) if anchor_end is not None else None
    if last_growth is None:
        last_growth = fallback_growth if fallback_growth is not None else target
    tail_years = [year for year in sorted(years) if year not in anchors]
    tail = len(tail_years)
    for offset, year in enumerate(tail_years, start=1):
        growth[year] = last_growth + (target - last_growth) * offset / (tail + 1)
    return dict(sorted(growth.items()))


def build_growth_path(
    consensus: dict,
    base_revenue: float | None,
    base_year: int | None,
    fallback_growth: float | None,
    terminal_growth: float | None = None,
    horizon_target: float | None = None,
) -> tuple[dict[int, float], dict[int, float], int]:
    """Consensus-anchored growth where covered, otherwise history-anchored, then a glide onto terminal growth.

    The glide lands on the same g the terminal value is priced with, so the explicit path and the
    perpetuity never disagree about where growth was headed. How many years that takes is the human's
    call (`horizon_target`), not a by-product of how far the brokers wrote: one year short of g is
    where the perpetuity picks up.
    """
    target = TERMINAL_GROWTH if terminal_growth is None else terminal_growth
    estimate = consensus["estimate"]
    anchors = {year: estimate[year]["revenue"] for year in sorted(estimate) if estimate[year].get("revenue")}

    horizon, _ = resolve_horizon(len(anchors), horizon_target)
    anchor_end = max(anchors) if anchors else base_year
    if anchor_end is None:
        return {}, anchors, horizon

    tail = horizon - len(anchors)
    years = sorted(anchors) + [anchor_end + offset for offset in range(1, tail + 1)]
    return glide_growth(anchors, base_revenue, fallback_growth, target, years), anchors, horizon


def build_revenue_path(growth: dict[int, float], anchors: dict[int, float], base_revenue: float | None) -> dict[int, float]:
    running = base_revenue
    out: dict[int, float] = {}
    for year in sorted(growth):
        if anchors.get(year):
            running = anchors[year]
        elif running:
            running *= 1.0 + growth[year]
        if running:
            out[year] = running
    return out


def debt_total(row: dict | None, include_lease_debt: bool = True) -> float | None:
    """Interest-bearing liabilities on one balance-sheet date, in the same caliber as the ladder."""
    if not row:
        return None
    groups = [SHORT_DEBT_KEYS, LONG_DEBT_KEYS] + ([LEASE_DEBT_KEYS] if include_lease_debt else [])
    present = [v for v in (_sum(row, group) for group in groups) if v is not None]
    return sum(present) if present else None


def measure_cost_of_debt(
    interest_ltm: float | None, avg_debt: float | None, revenue_ltm: float | None
) -> tuple[float | None, str]:
    """Pretax cost of debt = 利息费用 LTM ÷ 平均有息负债, with guards.

    Returns (value, evidence) — on rejection the value is None and the evidence says which guard fired,
    so the caller can keep the convention value AND show why it is still a guess. Known bias: 资本化利息
    不进财务费用，所以实测值对高在建工程的公司偏低。
    """
    if interest_ltm is None:
        return None, "利润表没有利息支出科目（FE_INTEREST_EXPENSE 缺失）"
    if interest_ltm <= 0:
        return None, f"利息费用 LTM {_fmt_yi(interest_ltm)} 非正，无法作分子"
    if not avg_debt:
        return None, "没有有息负债可作分母"
    floor = max(DEBT_COST_MIN_ABSOLUTE, (revenue_ltm or 0.0) * DEBT_COST_MIN_OF_REVENUE)
    if avg_debt < floor:
        return None, (
            f"平均有息负债 {_fmt_yi(avg_debt)} 低于护栏 {_fmt_yi(floor)}"
            f"（max(1 亿, 营收×{DEBT_COST_MIN_OF_REVENUE:.1%})），比值是噪声"
        )
    value = interest_ltm / avg_debt
    low, high = DEBT_COST_RANGE
    if not low <= value <= high:
        return None, f"实测 {value:.2%} 越出 {low:.1%}~{high:.0%} 合理区间，利息或负债口径异常"
    return value, f"利息费用 LTM {_fmt_yi(interest_ltm)} ÷ 平均有息负债 {_fmt_yi(avg_debt)}；未扣除资本化利息"


def _fmt_yi(amount: float | None) -> str:
    return "—" if amount is None else f"{amount / 1e8:,.2f} 亿元"


def trailing_pe(
    market_cap: float | None, net_profit_ltm: float | None, quote: dict
) -> tuple[float | None, str]:
    """滚动市盈率 = 总市值 ÷ 归母净利润 LTM，通道原值只留在凭证里当对照。

    不采纳行情给的市盈率：push2 那个字段是动态市盈率（最新中期归母按 12/月数 年化），腾讯那个才是滚动口径，
    降级与否会让同一只票跳数。分子分母都在这张链上、各自有凭证，所以自算的那一个才是可复算的。
    分母非正时不出数也不回退到通道值：一行上不能同时躺两套口径。
    """
    channel = quote.get("pe_channel")
    basis = quote.get("pe_channel_basis") or quote.get("source") or "无"
    tail = f"；行情通道原值 {channel}（{basis}）" if channel is not None else "；行情通道未给市盈率"
    if not market_cap:
        return None, f"不可得：本批没有总市值作分子{tail}"
    if net_profit_ltm is None:
        return None, f"不可得：报表滚不出归母净利润 LTM（最新报告期与上年同期不齐）{tail}"
    if net_profit_ltm <= 0:
        return None, f"不可得：归母净利润 LTM {_fmt_yi(net_profit_ltm)} 非正，市盈率没有意义{tail}"
    value = market_cap / net_profit_ltm
    return round(value, 2), (
        f"总市值 {_fmt_yi(market_cap)} ÷ 归母净利润 LTM {_fmt_yi(net_profit_ltm)} = {value:.2f}{tail}"
    )


def benchmark_label(prices: dict | None) -> str:
    bench = (prices or {}).get("benchmark") or {}
    code = bench.get("code") or "?"
    return f"{bench.get('name') or '基准指数'}({code})"


class BetaNotApplicable(ValueError):
    """β 在算术上不成立：CAPM 折不出可用的折现率，整条假设链不该继续。"""


def measure_beta(prices: dict | None) -> tuple[float, str]:
    """β from the run's price snapshot — 测到多少用多少，只有算术不成立才不出数。

    抛 BetaNotApplicable 的只有「算术上不成立」：重叠周数少到拟合不出斜率、β 非正。β≤0 会把股权成本压到
    无风险利率以下，越负越接近翻号，Gordon 的 1/(r−g) 随之失真。反过来，β 只有 0.05、样本短于实务下限、
    R² 很低这些「数值不好看」的情况一律照用，证据留在 note 里由人判断——用一个编出来的 1.00 去替换实测值，
    制造的错误比它避免的多。回归本身在 model.beta 里。

    异常分类：没有价格快照是**调用方漏传数据**，抛 ValueError（用法错误），不属于 CAPM 不适用。两条入口
    （CLI 与面板）在更早的取数层就把它报成 prices.PricesUnavailable 并附上真实原因，见 builder.missing_prices_error。
    """
    if not prices:
        raise ValueError(
            "没有价格快照，β 无从回归：直接调用 build_ladder 请带上 prices，或用 --beta / beta_manual 显式给定一个正数"
        )

    stock = (prices.get("stock") or {}).get("closes") or {}
    bench = prices.get("benchmark") or {}
    index = bench.get("closes") or {}
    label = benchmark_label(prices)
    stats = beta_model.regress(stock, index)
    if stats.beta is None:
        raise BetaNotApplicable(
            f"{label} 周收益重叠仅 {stats.observations} 周，少于 3 周拟合不出斜率 → β 测不出来，CAPM 不适用："
            "请用 --beta 显式给定，或改用别的估值口径"
        )

    # 残差为零时标准误是 0，t 发散：写成 ∞ 而不是让它把整条假设链的构建带崩。
    t_text = "∞" if stats.t_stat is None else f"{stats.t_stat:+.2f}"
    quality = f"{stats.observations} 周重叠 {stats.first_day}~{stats.last_day}，R²={stats.r_squared:.3f}，t={t_text}"
    if stats.beta <= 0:
        raise BetaNotApplicable(
            f"β 为负（实测 {stats.beta:.3f}，{quality}，基准 {label}）→ CAPM 不适用该模型："
            "负 β 把股权成本压到无风险利率以下，Gordon 终值 1/(r−g) 会翻号。"
            "仍要用本模型就加 --beta 显式给定，否则这只票该换估值口径"
        )
    evidence = f"{label} 周收益 OLS：{quality}，β={stats.beta:.3f}"
    if stats.observations < BETA_PRACTICE_MIN_WEEKS:
        evidence += f"；样本短于实务下限 {BETA_PRACTICE_MIN_WEEKS} 周，可能测到的是风格背离而非系统性风险"
    evidence += "；前复权价随除权重排，β 以本批次价格快照为准"
    return stats.beta, evidence


def _rounded_pct(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def row_years(value: Any) -> dict[int, float]:
    """行值里的「年 → 数」，键一律收成 int。

    基线经 JSON 往返后年份变成字符串，进程内直接建的链则是整数；重导算术两边都要吃，所以在此归一。
    """
    if not isinstance(value, dict):
        return {}
    out: dict[int, float] = {}
    for key, item in value.items():
        try:
            year = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            out[year] = item
    return out


def rekey_years(values: dict[int, float], template: Any) -> dict:
    """按模板行原本的键型把年份还回去：存的批次是字符串年，进程内的链是整数年。

    重导出来的行要和基线行逐年份对得上，面板的「相比基线 ±%」才归得进同一年；键型换了会被读成
    每一年都凭空多出来又凭空少掉，所以这里跟着原行的写法走。
    """
    keys = list(template or [])
    as_text = bool(keys) and isinstance(keys[0], str)
    return {(str(year) if as_text else year): value for year, value in sorted(values.items())}


def annual_revenue_growth(revenue: dict[int, float]) -> dict[int, float]:
    """Year-over-year revenue growth, only across contiguous fiscal years.

    A gap in the report series (停牌、借壳重述、上市首年) would otherwise turn a two-year change into
    an annual rate and hand the 保守 scenario a fabricated trough.
    """
    clean = {year: value for year, value in revenue.items() if value and value > 0}
    return {
        year: clean[year] / clean[year - 1] - 1.0
        for year in sorted(clean)
        if year - 1 in clean
    }


def terminal_growth_scenarios(revenue: dict[int, float]) -> dict:
    """One company's three perpetual-growth scenarios, measured off its own reported revenue history.

    Nothing is capped here: whether a g survives contact with the discount rate is the engine's call,
    and a magic ceiling would hide exactly the sensitivity the panel is meant to show.
    """
    growth = annual_revenue_growth(revenue)
    years = sorted(growth)
    stats: dict[str, Any] = {
        "observations": len(years),
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
        "central": None,
        "central_years": 0,
        "worst": None,
        "worst_year": None,
        "worst_span": None,
        "conservative": None,
        "optimistic": None,
        "best_year": None,
        "unavailable": "",
    }
    if len(years) < GROWTH_MIN_OBSERVATIONS:
        stats["unavailable"] = (
            f"只有 {len(years)} 个连续年度的营收增速观测（需 ≥{GROWTH_MIN_OBSERVATIONS} 个），"
            f"序列起点 {stats['first_year']}"
        )
        return stats

    central_window = years[-GROWTH_CENTRAL_WINDOW:]
    # 以上一年营收为权重，加权平均就变成「五年营收增量合计 ÷ 基数合计」：口径是公司整体的增速，
    # 而不是各年增速的算术平均——一个小年撬不动大体量年份。
    weights = {year: revenue[year - 1] for year in central_window}
    total_weight = sum(weights.values())
    if total_weight:
        stats["central"] = sum(growth[year] * weights[year] for year in central_window) / total_weight
        stats["central_years"] = len(central_window)
    worst_window = years[-GROWTH_WORST_WINDOW:]
    stats["worst_span"] = f"{worst_window[0]}~{worst_window[-1]}"
    stats["worst_year"] = min(worst_window, key=lambda year: growth[year])
    stats["worst"] = growth[stats["worst_year"]]
    stats["conservative"] = stats["worst"] / 2.0
    stats["best_year"] = max(years, key=lambda year: growth[year])
    stats["optimistic"] = growth[stats["best_year"]]
    return stats


def roe_by_year(main_rows: Iterable[dict]) -> dict[int, float]:
    """年报行的加权净资产收益率（百分数 → 小数）。

    只用 12-31 那一行：中报与三季报的 ROE 是年初至今累计，把它当成当年的回报会把一年的盈利能力折半。
    """
    out: dict[int, float] = {}
    for year, row in P.annual_rows(main_rows).items():
        value = P.num(row, ROE_KEY)
        if value:
            out[year] = value / 100.0
    return out


def sustainable_growth_stats(
    payouts: dict | None, roe: dict[int, float], *, window: int = SUSTAINABLE_WINDOW
) -> dict:
    """第四档：Higgins 的再投资增长率 (1−PO)·ROE，取近 `window` 个已实施分配财年的中位数。

    它量的是「利润留下来多少、能撑起多快增长」，不是「永续期能长多快」：只要 ROE 高于折现率，这个式子
    就隐含超额收益永续存在，而 Gordon 与 UEM 都要 g<r。所以默认不进级联，只当对照行（2026-10-22 现取批次实测：
    茅台 2025 年 6.78%、招行 8.69%、格力 8.59%，都高过同一批各自的股权成本；善水科技 2025 年 2.13% 反而低于它的
    CoE 6.79%——ROE 塌方时这一档不乐观，所以它是对照值而不是待替换的默认档）。

    逐年配对而不是一次汇总：ROE 与分红率都按财年对齐，某一年派了特别分红就单独剔掉并记下理由，
    汇总口径会把那一年的高派发摊到整段窗口上。取中位数（与 rf 窗口同一套纪律）：五年里有一年特别分红
    或一年 ROE 塌方，都不该牵着这一档走。
    """
    stats: dict[str, Any] = {
        "window": window,
        "observations": 0,
        "first_year": None,
        "last_year": None,
        "value": None,
        "years": [],
        "excluded": [],
        "payout_min": None,
        "payout_max": None,
        "available_years": 0,
        "unavailable": "",
    }
    reported = (payouts or {}).get("years") or {}
    stats["available_years"] = len(reported)
    if not (payouts or {}).get("available"):
        stats["unavailable"] = (payouts or {}).get("unavailable") or "这批没有分红凭证"
        return stats
    paired = sorted(reported, reverse=True)
    if not paired:
        stats["unavailable"] = "分红序列里没有已实施的现金分配年度"
        return stats
    for year in paired[:window]:
        if year not in roe:
            stats["excluded"].append({"year": year, "reason": "缺年报加权 ROE"})
            continue
        payout = reported[year]["payout"]
        if payout > 1.0:
            stats["excluded"].append(
                {"year": year, "reason": f"分红率 {payout:.1%} 派过了当年全部每股收益（特别分红）"}
            )
            continue
        retained = (1.0 - payout) * roe[year]
        stats["years"].append(
            {"year": year, "payout": payout, "roe": roe[year], "growth": retained}
        )
    stats["years"].sort(key=lambda item: item["year"])
    used = [item["growth"] for item in stats["years"]]
    if len(used) < SUSTAINABLE_MIN_YEARS:
        stats["unavailable"] = (
            f"最近 {min(window, len(paired))} 个已实施分配的财年里只有 {len(used)} 个配得上年报加权 ROE"
            f"（需 ≥{SUSTAINABLE_MIN_YEARS} 个；有分红的年度共 {len(paired)} 个）"
        )
        return stats
    stats["observations"] = len(used)
    stats["first_year"] = stats["years"][0]["year"]
    stats["last_year"] = stats["years"][-1]["year"]
    stats["value"] = statistics.median(used)
    rates = [item["payout"] for item in stats["years"]]
    stats["payout_min"] = min(rates)
    stats["payout_max"] = max(rates)
    return stats


def _sus_pairs_text(sus: dict) -> str:
    """「2021 留存 48.1%×ROE 30.26%→14.51% ｜ …」——第四行的 evidence 用，级联行也引它。"""
    return " ｜ ".join(
        f"{item['year']} 留存 {1.0 - item['payout']:.1%}×ROE {item['roe']:.2%}→{item['growth']:.2%}"
        for item in sus.get("years") or []
    )


def pick_terminal_growth(
    stats: dict, manual: float | None, basis: str, sustainable: dict | None = None
) -> tuple[float | None, str, bool]:
    """(级联用的永续增长率, 基准名, 是否实测)。人工值优先；测不出来就退回惯例值并带着原因上路。

    实测出来的那一档兜底在 0：默认不给「永续负增长」定价——一家公司年年萎缩是一回事，把萎缩外推到
    无穷远是另一回事，后者会把终值压成负数并让 g 越过折现率的下界。原值留在基准名里，凭证看得见抬之前是多少。
    人工值（`--terminal-growth` / 面板改这一行）不抬也不压：改了就是当常量用，页面不再替你猜。
    """
    if basis not in TERMINAL_GROWTH_BASIS:
        raise ValueError(f"未知永续增速基准 {basis!r}，可选: {TERMINAL_GROWTH_BASIS}")
    if manual is not None:
        return manual, "人工输入 (--terminal-growth)", True
    if basis == "convention":
        return TERMINAL_GROWTH, f"惯例 {TERMINAL_GROWTH:.1%}", False
    if basis == "sustainable":
        # 第四档看的是分红与 ROE 两串数，成不成立与营收三情形那一套各判各的，所以它不看 `stats`。
        sus = sustainable or {}
        if sus.get("value") is None:
            reason = sus.get("unavailable") or "没有可配的年度"
            return TERMINAL_GROWTH, f"惯例 {TERMINAL_GROWTH:.1%}（再增长率不可用：{reason}）", False
        value = sus["value"]
        floored = max(0.0, value)
        label = f"再投资（近{sus['observations']}年 (1−PO)·ROE 中位）"
        if floored != value:
            label = f"{label}，实测 {value:.2%} 为负 → 兜底 0"
        return floored, label, True
    if stats.get("unavailable"):
        return TERMINAL_GROWTH, f"惯例 {TERMINAL_GROWTH:.1%}（实测不可用）", False
    value = {"conservative": stats.get("conservative"), "central": stats.get("central"),
             "optimistic": stats.get("optimistic")}[basis]
    if value is None:
        return TERMINAL_GROWTH, f"惯例 {TERMINAL_GROWTH:.1%}（该情形缺观测）", False
    labels = {
        "conservative": f"保守（近{GROWTH_WORST_WINDOW}年最差 {stats['worst']:.2%} 之半）",
        "central": f"中枢（近{stats['central_years']}年营收加权）",
        "optimistic": f"乐观（历史最好 {stats['best_year']} {stats['optimistic']:.2%}）",
    }
    floored = max(0.0, value)
    label = labels[basis] if floored == value else f"{labels[basis]}，实测 {value:.2%} 为负 → 兜底 0"
    return floored, label, True


def _pct_text(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2%}"


def _short_basis(basis: str) -> str:
    """把「保守（近10年最差 -1.20% 之半）」这类基准名收成不带数的短名。

    note 要说「本行采用哪一档」，但不能把那一档的算式和数字也搬进来——两半本来就是同一个字符串里的，
    拆开取前段就不会和 evidence 分叉。
    """
    head = str(basis or "").split("（")[0].split("(")[0].strip()
    head = re.sub(r"[-+]?\d+(\.\d+)?%$", "", head).strip()
    if head.startswith("惯例"):
        return "惯例值"
    if head.startswith("人工输入"):
        return "人工输入"
    return head or "惯例值"


def terminal_growth_rows(
    stats: dict, sus: dict, terminal_growth: float | None, tg_basis: str, measured: bool
) -> list[Row]:
    """一行进级联，三行做对照：与无风险利率的即期/中枢同一套摆法。

    参考行不在 ENGINE_INPUTS 里，改它们不动结果（面板会登记为未定价改动）；要用中枢、乐观或再投资增速定价，
    改级联那一行或用 --terminal-growth-basis 换档即可，面板的归因列会给出这一改动值多少钱。
    """
    observed = not stats.get("unavailable")
    sus_observed = sus.get("value") is not None
    detail = (
        f"样本 {stats['first_year']}~{stats['last_year']} 共 {stats['observations']} 个连续年度增速"
        if observed
        else f"不可用：{stats['unavailable']}"
    )
    scenarios = (
        f"中枢（近{stats['central_years']}年营收加权）{_pct_text(stats.get('central'))} ｜ "
        f"保守（{stats['worst_span']} 最差 {stats['worst_year']} {_pct_text(stats.get('worst'))} 之半）"
        f"{_pct_text(stats.get('conservative'))} ｜ "
        f"乐观（历史最好 {stats['best_year']}）{_pct_text(stats.get('optimistic'))}。"
        if observed
        else f"三情形测算未通过：{stats['unavailable']}。"
    )
    # 第四档单列一句：它的证据是分红率与加权 ROE 两串数，塞进上面那句营收情形里会把两套口径混成一串。
    sustainable_line = (
        f"再投资（近{sus['observations']}个财年 (1−PO)·ROE 中位）{_pct_text(sus['value'])}。"
        if sus_observed
        else f"再投资档不可用：{sus.get('unavailable') or '本批无分红凭证'}。"
    )
    excluded = "；".join(f"{item['year']} {item['reason']}" for item in sus.get("excluded") or [])
    cascaded = _short_basis(tg_basis)
    return [
        Row(
            "terminal_growth",
            "永续增长率（级联采用）",
            "pct",
            _rounded_pct(terminal_growth),
            (
                f"东财分红方案 × 年报加权 ROE：{sus['first_year']}~{sus['last_year']} 逐年配对"
                if cascaded == "再投资"
                else f"公司年报营收增速序列：{detail}"
            ),
            kind="data" if measured else "convention",
            note=(
                "显性期之后每一年的营收增速，终值按它无限外推。它必须低于折现率，否则 Gordon 与 UEM 都收不住。"
                f"本行采用「{cascaded}」。"
            ),
            evidence=(
                f"{scenarios}{sustainable_line}本行采用「{tg_basis or '惯例'}」。"
                "换情形用 --terminal-growth-basis，或直接改本行。"
            ),
        ),
        Row(
            "terminal_growth_central",
            f"营收近{stats.get('central_years') or GROWTH_CENTRAL_WINDOW}年加权增速（中枢）",
            "pct",
            _rounded_pct(stats.get("central")),
            f"GINCOME 年报序列，近{stats.get('central_years')}年增量合计/基数合计（即按上年营收加权）：{detail}",
            note="参考值，不进级联：三情形里的中枢档，用来看级联采用的那一档偏在哪一侧。",
            evidence=f"当前级联采用「{tg_basis or '惯例'}」。",
        ),
        Row(
            "terminal_growth_optimistic",
            "历史最好年度营收增速（乐观）",
            "pct",
            _rounded_pct(stats.get("optimistic")),
            f"GINCOME 年报序列年度增速最大值：{detail}",
            note="参考值，不进级联：样本里最好的那一年。把它当永续增速多半会不低于折现率，终值直接无解。",
            evidence=(
                (f"出现在 {stats.get('best_year')}；" if observed else "")
                + f"当前级联采用「{tg_basis or '惯例'}」。"
            ),
        ),
        Row(
            "terminal_growth_sustainable",
            f"(1−PO)·ROE 再投资增长率（近{sus.get('window') or SUSTAINABLE_WINDOW}个财年之中位）",
            "pct",
            _rounded_pct(sus.get("value")),
            "东财个股分红方案（akshare stock_fhps_detail_em）× GINCOME 加权净资产收益率（ROEJQ）年报序列",
            note=(
                "参考值，不进级联：留存比例×净资产收益率，量的是「利润留在公司能撑起多快增长」，"
                "不是永续期该用多快。只要 ROE 高过折现率，这个式子就隐含超额收益永续存在，"
                "而 Gordon 与 UEM 都要 g 低于折现率，所以它天生偏乐观。"
            ),
            evidence=(
                (
                    f"{_sus_pairs_text(sus)}。分红率区间 {sus['payout_min']:.1%}~{sus['payout_max']:.1%}，"
                    f"中位 {sus['value']:+.2%}。"
                    if sus_observed
                    else f"不可用：{sus.get('unavailable') or '本批无分红凭证'}。"
                )
                + (f"窗口内剔除：{excluded}。" if excluded else "")
                + f"有已实施分配的年度共 {sus.get('available_years') or 0} 个。"
                + f"当前级联采用「{tg_basis or '惯例'}」。"
            ),
        ),
    ]


def risk_free_rows(
    risk_free: float | None, risk_free_source: str, rf_window: dict | None, rf_basis: str
) -> list[Row]:
    """一行进级联，两行做对照：即期与中枢同时出现在面板上。

    即期是市场今天报的数，中枢是一段窗口的均值，估值只能取一个进 CAPM，但只看到其中一个的人无从判断
    它偏在哪一侧。参考行不在 ENGINE_INPUTS 里，改它们不动结果（面板会登记为未定价改动）。
    """
    window = rf_window or {}
    years = window.get("window_years")
    span = f"近{years}年" if years else "多年"
    series = window.get("series") or "10Y 国债收益率"
    spot = window.get("spot")
    mean = window.get("mean")
    text = lambda value: "—" if value is None else f"{value:.4%}"  # noqa: E731
    window_detail = (
        f"中位 {window['median']:.4%}，区间 {window['min']:.2%}~{window['max']:.2%}，"
        f"{window['observations']} 个观测 {window['first_day']}~{window['last_day']}"
        if mean is not None
        else f"不可用：{window.get('unavailable') or '没有国债收益率序列'}"
    )
    return [
        Row(
            "risk_free",
            "无风险利率 10Y 国债（级联采用）",
            "pct",
            _rounded_pct(risk_free),
            risk_free_source,
            kind="data" if risk_free is not None else "convention",
            note=(
                "整条折现率的底座：股权成本从它起算，永续增速也得低于它，终值才算得住。"
                f"本行采用「{_short_basis(rf_basis or '即期')}」，即期跟着利率周期走，中枢把它摊平。"
            )
            if risk_free is not None
            else "取数失败，可人工填该行，或用 --rate 指定折现率",
            evidence=(
                f"即期 {text(spot)} ｜ {span}中枢 {text(mean)}（{window_detail}）。"
                f"当前采用「{rf_basis or '即期'}」，换基准用 --risk-free-basis，或直接改本行。"
            ),
        ),
        Row(
            "risk_free_spot",
            f"{series} 即期",
            "pct",
            _rounded_pct(spot),
            window.get("spot_source") or "未取到即期观测",
            note=f"参考值，不进级联：市场今天报的数，用来看级联那一档离今天多远。当前级联采用「{_short_basis(rf_basis or '即期')}」。",
            evidence="要用即期定价请改上一行。",
        ),
        Row(
            "risk_free_norm",
            f"{series} {span}中枢",
            "pct",
            _rounded_pct(mean),
            f"{series} {span}日频均值",
            note="参考值，不进级联：一段窗口摊平后的利率水平，不随今天的债市上下。",
            evidence=f"{window_detail}；当前级联采用「{rf_basis or '即期'}」。",
        ),
    ]


def pick_erp(stats: dict | None) -> tuple[float, str, bool, str]:
    """(级联用的股权风险溢价, 基准名, 是否实测, 退回原因)。测不出来或越出可用区间都退回惯例值。

    与债务成本、永续增速同一套纪律：兜底值留着，但被拒的那次实测是多少、为什么没用，必须一起上路。
    第四个返回值是给顶层警告用的——ERP 不是实测值时整条折现率的可信度都变了，只写在行 note 里看不见。
    """
    window = ((stats or {}).get("windows") or {}).get(ERP_CASCADE_WINDOW) or {}
    label = ERP_WINDOW_LABELS[ERP_CASCADE_WINDOW]
    unavailable = str(window.get("unavailable") or "")
    if unavailable.startswith("unavailable: "):
        unavailable = unavailable[len("unavailable: "):]
    if unavailable:
        reason = f"{label}实测不可用：{unavailable}"
    else:
        value = window.get("erp")
        if value is None:
            reason = f"{label}没有测算"
        elif not ERP_RANGE[0] <= value <= ERP_RANGE[1]:
            reason = f"{label}实测 {value:+.2%} 越出可用区间 {ERP_RANGE[0]:.0%}~{ERP_RANGE[1]:.0%}"
        else:
            return value, f"{label} realized 超额收益", True, ""
    return ERP, f"惯例 {ERP:.1%}（{reason}）", False, reason


def erp_warnings(stats: dict | None, measured: bool, reason: str) -> list[str]:
    """级联里 ERP 这一格「不是实测」这件事，必须出现在页面顶部，而不是藏在某行的 note 里。

    警告留数字（被拒的那次实测是多少正是这条告示的全部信息量），删掉的是教你怎么办的长篇——
    窗口与市场基准如今都是页面上的下拉，出路看得见，不需要每批都上一课。
    """
    out: list[str] = []
    if not measured:
        out.append(
            f"{reason}，股权风险溢价退回惯例 {ERP:.1%}，级联折现率因此与这段窗口的市场表现脱钩。"
            "换窗口或换市场基准都在页面上的下拉里。"
        )
    block = (stats or {}).get("total_return") or {}
    if block.get("twin") and block.get("unavailable"):
        out.append(
            f"市场腿没能用上全收益指数 {block['twin']}（{block['unavailable']}）："
            "本次 ERP 按「价格指数 + 沪深两市股息率」测算，分红腿退回全市场口径。"
        )
    return out


def _erp_text(window: dict) -> str:
    """一条腿说完：区间与样本、市场涨幅、无风险利率、超额。市场腿有全收益与价格+股息率两种讲法。"""
    if not window:
        return "没有测算：缺少市场收益率快照"
    if window.get("unavailable"):
        return f"不可用：{window['unavailable']}"
    risk_free = f"10Y 国债{window.get('risk_free_basis') or '窗口均值'} {window['risk_free_mean']:.2%}"
    head = (
        f"{window['base_day']}~{window['last_day']}（{window['span_years']:.2f} 年、"
        f"{window['observations']} 个收盘）："
    )
    if window.get("market_leg") == "total_return":
        cumulative = window["total_return_last"] / window["total_return_base"] - 1.0
        return (
            f"{head}全收益指数累计 {_pct_text(cumulative)}、年化总收益 "
            f"{_pct_text(window['total_return_annualized'])}，{risk_free} → 超额 {_pct_text(window['erp'])}"
        )
    return (
        f"{head}指数累计 {_pct_text(window['price_change'])}、年化 {_pct_text(window['price_annualized'])}，"
        f"股息率均值 {window['dividend_yield_mean']:.2%}，{risk_free} → 年化总收益 "
        f"{_pct_text(window['total_return_annualized'])}、超额 {_pct_text(window['erp'])}"
    )


def erp_rows(
    stats: dict | None, erp: float | None, erp_basis: str, measured: bool, market_source: str = ""
) -> list[Row]:
    """级联一行进定价，其余窗口各一行做对照：与无风险利率的即期/中枢、永续增速的几档参考行同一套摆法。

    参考行不在 ENGINE_INPUTS 里，改它们不动结果（面板会登记为未定价改动）；要按别的窗口定价，
    选中那条窗口就是把它的实测数写进级联行——所以这里没有 --erp 开关，窗口选择走人工修正。
    """
    windows = (stats or {}).get("windows") or {}
    bench = ((stats or {}).get("benchmark") or {}).get("name") or "市场组合"
    twin = ((stats or {}).get("total_return") or {}).get("twin") or ""
    cascade = windows.get(ERP_CASCADE_WINDOW) or {}
    label = ERP_WINDOW_LABELS[ERP_CASCADE_WINDOW]
    leg = cascade.get("market_leg") == "total_return" and not cascade.get("unavailable")
    measured_here = bool(cascade) and not cascade.get("unavailable")
    caliber = (
        f"市场腿是 {bench} 的全收益指数 {twin}：分红在除权日就再投资进点位，不用外部股息率补口，"
        "但它是税前口径（不扣红利税）。"
        if leg
        else f"分红腿是全市场口径（沪深两市 A 股股息率的日均值），不是 {bench} 成分的分红；"
    )
    price_only = (
        f"只按价格涨幅算的超额是 {_pct_text(cascade['erp_price_only'])}。"
        if cascade.get("erp_price_only") is not None
        else ""
    )
    rows = [
        Row(
            "erp",
            "股权风险溢价（级联采用）",
            "pct",
            _rounded_pct(erp),
            (
                f"{bench} 全收益指数 − 同窗口 10Y 国债{cascade.get('risk_free_basis') or '中枢'}（{label}年化超额）"
                if leg and measured
                else f"{bench} 收盘涨幅 + 沪深两市股息率 − 同窗口 10Y 国债{cascade.get('risk_free_basis') or '均值'}（{label}年化超额）"
                if measured
                else "A股常用区间中值"
            ),
            kind="data" if measured else "convention",
            note=(
                "承担股票市场风险相比无风险利率多要的那份补偿，乘上 β 加到无风险利率上就是股权成本。"
                f"realized 口径对窗口极敏感，窗口与市场基准都做成了页面上的下拉。本行采用「{_short_basis(erp_basis)}」。"
            ),
            evidence=(
                f"{_erp_text(cascade)}。本行采用「{erp_basis}」。"
                + (caliber if measured_here else "")
                + price_only
                + f"另有 {len(ERP_REFERENCE_WINDOWS)} 行是同式另取的窗口"
                f"（{'／'.join(ERP_WINDOW_LABELS[window] for window in ERP_REFERENCE_WINDOWS)}）；"
                "要按别的值定价就换窗口或直接改本行（面板可改，无命令行开关）。"
                + (f"取数：{market_source}" if market_source else "")
            ),
        )
    ]
    for window in ERP_REFERENCE_WINDOWS:
        reference = windows.get(window) or {}
        name = ERP_WINDOW_LABELS[window]
        rows.append(
            Row(
                f"erp_{window}",
                f"{name}{bench}超额收益（参考）",
                "pct",
                _rounded_pct(reference.get("erp")),
                f"与级联行同一算法，窗口换成{name}",
                note=f"参考值，不进级联：同式换到「{name}」窗口的实测超额，供级联行的窗口下拉取用。",
                evidence=f"{_erp_text(reference)}。当前级联采用「{erp_basis}」。",
            )
        )
    return rows


# 顶层警告里认出「ERP 退回惯例值」那句话的记号：窗口选中后要把这句话改口，文案与记号必须同源。
ERP_FALLBACK_MARK = "股权风险溢价退回惯例"


def erp_window_options(rows: list[dict]) -> list[dict]:
    """把级联行与几条参考行的实测收成下拉选项，越出可用区间（含负超额）的那一档不给选。

    级联行退回惯例值时，级联那一档自己也不算可用：惯例 6% 不是任何窗口的实测数，把它标成
    「近 3 年 +6.00%」等于凭空造一个市场事实。那个被拒的实测值仍摆在行上，看得见但选不中。
    """
    by_key = {row.get("key"): row for row in rows}
    options: list[dict] = []
    for window in ERP_WINDOW_ORDER:
        key = "erp" if window == ERP_CASCADE_WINDOW else f"erp_{window}"
        row = by_key.get(key)
        if row is None:
            continue
        value = row.get("baseline")
        usable = (
            value is not None
            and ERP_RANGE[0] <= value <= ERP_RANGE[1]
            and (key != "erp" or row.get("kind") == "data")
        )
        options.append(
            {
                "window": window,
                "label": ERP_WINDOW_LABELS[window],
                "row": key,
                "value": value,
                "usable": usable,
                "cascade": window == ERP_CASCADE_WINDOW,
            }
        )
    return options


def erp_window_choice(options: list[dict], value: Any) -> dict | None:
    """把 overrides.erp 那个数反回它是哪一档窗口——manual.json 存的是数而不是窗口名，靠数对上就算选中。"""
    if value is None:
        return None
    try:
        target = float(value)
    except (TypeError, ValueError):
        return None
    for option in options:
        if option["usable"] and abs(float(option["value"]) - target) <= 1e-9:
            return option
    return None


def erp_window_applied(option: dict) -> str:
    return (
        f"股权风险溢价改用「{option['label']}」实测 {float(option['value']):+.2%}："
        "这是一次人工修正，级联折现率按这一档窗口重算。"
    )


def erp_warnings_for_window(warnings: list[str], option: dict | None) -> list[str]:
    """窗口被选中后，那条「退回惯例值、与这段窗口脱钩」的警告说的就是上一回事了。

    这条告示讲的是这次重算用的谁，别的警告（比如全收益孪生没取到）照原样留着。
    """
    if option is None:
        return list(warnings)
    kept = [warning for warning in warnings if ERP_FALLBACK_MARK not in warning]
    return [erp_window_applied(option), *kept]


def derive_rates(
    risk_free: float | None,
    beta: float | None,
    erp: float | None,
    equity_weight: float | None,
    cost_of_debt_pretax: float | None,
    tax_rate: float | None,
) -> tuple[float | None, float | None]:
    """(cost of equity, WACC) from primitives — the single source of the discount-rate cascade.

    A missing primitive gives back None instead of a guess, so the caller keeps whatever the ladder
    already carried; a failed risk-free fetch must never silently become an invented discount rate.
    """
    cost_of_equity = None
    if risk_free is not None and beta is not None and erp is not None:
        cost_of_equity = risk_free + beta * erp
    wacc = None
    if cost_of_equity is not None and equity_weight is not None:
        pretax_debt = cost_of_debt_pretax or 0.0
        wacc = equity_weight * cost_of_equity + (1 - equity_weight) * pretax_debt * (1 - (tax_rate or 0.0))
    return cost_of_equity, wacc


def implied_ev_ebitda(
    market_cap: float | None,
    revenue: float | None,
    ebit_margin: float | None,
    da_ratio: float | None,
    net_debt: float | None,
    minority: float | None,
) -> float | None:
    """EV/EBITDA the current market price already implies — the reverse-DCF anchor used when no broker multiple exists.

    Monetary arguments share one unit (亿元); margins and ratios are decimals.
    """
    if not (market_cap and revenue):
        return None
    ebitda = revenue * ((ebit_margin or 0.0) + (da_ratio or 0.0))
    if ebitda <= 0:
        return None
    return (market_cap + (net_debt or 0.0) + (minority or 0.0)) / ebitda


def build_margin_path(
    years: list[int], consensus: dict[int, float], reversion_target: float | None, start: float | None
) -> dict[int, float]:
    """Consensus margin where covered, otherwise a glide toward the historical mean."""
    out = {year: consensus[year] for year in years if consensus.get(year) is not None}
    outside = [y for y in years if y not in out]
    if not outside:
        return out
    previous = out[max(out)] if out else start
    if previous is None:
        return out
    if reversion_target is None:
        out.update({year: previous for year in outside})
        return out
    for index, year in enumerate(outside, start=1):
        out[year] = previous + (reversion_target - previous) * index / (len(outside) + 1)
    return dict(sorted(out.items()))


def build_ladder(
    snapshot: dict,
    consensus: dict,
    quote: dict,
    risk_free: float | None,
    risk_free_source: str = "akshare bond_zh_us_rate",
    include_lease_debt: bool = True,
    prices: dict | None = None,
    prices_source: str = "",
    rf_window: dict | None = None,
    rf_basis: str = "",
    terminal_basis: str = "conservative",
    terminal_growth_manual: float | None = None,
    beta_manual: float | None = None,
    market_stats: dict | None = None,
    market_source: str = "",
    horizon_target: float | None = None,
    normalize: bool = True,
) -> Ladder:
    income_annual = P.annual_rows(snapshot["income"])
    cashflow_annual = P.annual_rows(snapshot["cashflow"])
    balance_annual = P.annual_rows(snapshot["balance"])

    revenue_annual = {y: P.num(r, *REVENUE_KEYS) for y, r in income_annual.items()}
    revenue_annual = {y: v for y, v in revenue_annual.items() if v}
    revenue_ltm = P.ltm(snapshot["income"], *REVENUE_KEYS)
    net_profit_ltm = P.ltm(snapshot["income"], PARENT_NET_PROFIT_KEY)

    estimate = consensus["estimate"]
    estimate_years = sorted(estimate)
    first_estimate = estimate_years[0] if estimate_years else None
    org_count = estimate.get(first_estimate, {}).get("revenue_org_count") if first_estimate else None
    anchor_confidence = confidence_from_coverage(org_count)

    last_actual_year = max(revenue_annual) if revenue_annual else None
    last_actual_revenue = revenue_annual.get(last_actual_year) if last_actual_year else None
    revenue_cagr = P.cagr(revenue_annual, 5)
    # CAGR 用掉的那两个端点单独留着：改「最后年报营收」时这一行才有权跟着重算（见 recompute）。
    cagr_span = sorted(revenue_annual.items())
    cagr_first = cagr_span[-6][0] if revenue_cagr is not None and len(cagr_span) >= 6 else None
    # g 先于折现率定下来：增速路径的回落终点是它，Gordon 终值也直接用它。
    tg_stats = terminal_growth_scenarios(revenue_annual)
    # 第四档要两串数：分红率来自本批的 dividend 凭证（老批次没有就当缺，不拦路），ROE 来自 main。
    sus_stats = sustainable_growth_stats(snapshot.get("dividends"), roe_by_year(snapshot.get("main") or []))
    terminal_growth, tg_basis, tg_measured = pick_terminal_growth(
        tg_stats, terminal_growth_manual, terminal_basis, sus_stats
    )
    growth, anchors, horizon = build_growth_path(
        consensus, last_actual_revenue, last_actual_year, revenue_cagr, terminal_growth, horizon_target
    )
    # 显性期年数是输入、每年回落幅度是它的输出：这两个数只进凭证与顶层警告，不当假设行。
    requested_horizon, horizon_clamp = resolve_horizon(len(anchors), horizon_target)
    glide_fade, glide_last = glide_diagnostics(growth, anchors, TERMINAL_GROWTH if terminal_growth is None else terminal_growth)
    glide_tail = max(0, horizon - len(anchors))
    revenue_path = build_revenue_path(growth, anchors, last_actual_revenue)

    # 正常化：把官方扣非差额搬到 EBIT 层的分子上。逐年各剔各的（近五年均值），LTM 用最近整年那一笔。
    # 窗口里任一年没过护栏就整窗不调整——两套口径混在同一个五年序列里，回落终点会落在无法复现的混合数上。
    normalizations = normalize_model.annual_normalizations(snapshot["income"], _ebit, MARGIN_LOOKBACK)
    normal_window = normalize_model.accepted_window(normalizations) if normalize else []
    normalized_ebit = {item.year: item.ebit_normalized for item in normal_window}
    margin_annual = _series_ratio(
        {**{y: _ebit(r) for y, r in income_annual.items()}, **normalized_ebit}, revenue_annual
    )
    ebit_ltm = _ebit_ltm(snapshot["income"])
    ebit_bridge = _ebit_bridge_ltm(snapshot["income"])
    # LTM 分子用最近整年的剔除额，不做季度差分：单季扣非差额年内会反号（紫金 2025 四季 2.85/13.82/20.69/−26.83 亿）。
    # 没过护栏＝照用原数，不是当零：那笔剔除额压根没算出来，分子必须退回报表值。
    strip_ltm = normal_window[-1].strip if normal_window else None
    ebit_ltm_recurring = None if ebit_ltm is None else ebit_ltm - (strip_ltm or 0.0)
    margin_ltm = _ratio(ebit_ltm_recurring, revenue_ltm)
    margin_consensus = {
        y: _ratio(r.get("operate_profit"), r.get("revenue")) for y, r in estimate.items() if r.get("operate_profit")
    }
    margin_hist = P.mean(_tail(margin_annual, MARGIN_LOOKBACK))
    margin_start = margin_consensus.get(first_estimate, margin_ltm)
    margin_path = build_margin_path(sorted(growth), margin_consensus, margin_hist, margin_start)

    tax_annual = _series_ratio(
        {y: P.num(r, "INCOME_TAX") for y, r in income_annual.items()},
        {y: P.num(r, "TOTAL_PROFIT") for y, r in income_annual.items()},
    )
    tax_ltm = _ratio(P.ltm(snapshot["income"], "INCOME_TAX"), P.ltm(snapshot["income"], "TOTAL_PROFIT"))
    tax_hist = P.mean(_tail(tax_annual, MARGIN_LOOKBACK))

    da_annual = _series_ratio({y: _sum(r, DA_KEYS) for y, r in cashflow_annual.items()}, revenue_annual)
    capex_annual = _series_ratio({y: P.num(r, CAPEX_KEY) for y, r in cashflow_annual.items()}, revenue_annual)
    # 比率的分子单独留一份：分母（LTM 营收）被人改了，比率才有原料跟着重算，否则这条边只能虚着。
    da_ltm_abs = P.ltm_sum(snapshot["cashflow"], DA_KEYS)
    # 折旧摊销四项编在现金流量表的补充资料里，一季报与三季报不编那一段（准则只要求半年报和年报披露）。
    # 最新一行恰好是季报时，滚动窗口会断在没有披露的那一格，整条序列就取不出数——把窗口尾巴退到最近
    # 真报了数的那一期，分母跟着换成同一个十二个月的营收，两边必须是同一段，不然比率不是任何一年的比率。
    da_window = ""
    da_window_revenue = revenue_ltm
    if da_ltm_abs is None:
        da_window = P.last_reported_day(snapshot["cashflow"], *DA_KEYS) or ""
        if da_window:
            da_ltm_abs = P.ltm_sum(snapshot["cashflow"], DA_KEYS, anchor=da_window)
            da_window_revenue = P.ltm(snapshot["income"], *REVENUE_KEYS, anchor=da_window)
        else:
            # 一张补充资料都没编（银行、保险这类没有现金流量表的公司）：退无可退，照旧留空。
            da_window_revenue = None
    capex_ltm_abs = P.ltm(snapshot["cashflow"], CAPEX_KEY)
    da_ltm = _ratio(da_ltm_abs, da_window_revenue)
    capex_ltm = _ratio(capex_ltm_abs, revenue_ltm)

    nwc_annual = {y: _nwc(r) for y, r in balance_annual.items()}
    nwc_annual = {y: v for y, v in nwc_annual.items() if v is not None}
    nwc_pull = []
    for year, value in sorted(nwc_annual.items()):
        revenue_delta = (revenue_annual.get(year) or 0) - (revenue_annual.get(year - 1) or 0)
        previous = nwc_annual.get(year - 1)
        if previous is not None and revenue_delta:
            nwc_pull.append((value - previous) / revenue_delta)
    nwc_ratio = P.mean(nwc_pull[-MARGIN_LOOKBACK:])

    head_balance = P.latest(snapshot["balance"])
    cash = _sum(head_balance, CASH_KEYS) or 0.0
    lease_debt = _sum(head_balance, LEASE_DEBT_KEYS) if include_lease_debt else None
    debt = debt_total(head_balance, include_lease_debt) or 0.0
    lease_financing = (
        (P.num(head_balance, *LEASE_DEBT_KEYS) or 0.0) - (P.twelve_months_ago(snapshot["balance"], *LEASE_DEBT_KEYS) or 0.0)
        if include_lease_debt and P.num(head_balance, *LEASE_DEBT_KEYS) is not None
        else None
    )
    minority = P.num(head_balance, "MINORITY_EQUITY") or 0.0
    total_equity = P.num(head_balance, "TOTAL_EQUITY")
    net_debt = debt - cash
    borrow_in = P.ltm(snapshot["cashflow"], BORROW_IN_KEY)
    repay_out = P.ltm(snapshot["cashflow"], REPAY_OUT_KEY)
    net_borrowing = (borrow_in or 0.0) - (repay_out or 0.0)

    # ROIC 的分子与 EBIT 率 LTM 用同一个数：进定价的分子只有一档，正常化了就两边都正常化。
    nopat = (ebit_ltm_recurring or 0.0) * (1.0 - (tax_ltm if tax_ltm is not None else (tax_hist or 0.0)))
    invested = (total_equity or 0.0) + debt - cash
    roic = _ratio(nopat, invested)

    market_cap = quote.get("total_market_cap")
    equity_weight = _ratio(market_cap, (market_cap or 0) + debt) if market_cap else None
    pe_ttm, pe_evidence = trailing_pe(market_cap, net_profit_ltm, quote)
    interest_ltm = P.ltm(snapshot["income"], *INTEREST_EXPENSE_KEYS)
    prior_debt = debt_total(P.same_quarter_last_year(snapshot["balance"]), include_lease_debt)
    # 上年同期缺行时（上市不满两年）退化为期末值，宁可用单点分母也不放弃实测——note 会说清是哪一种。
    avg_debt = None if not debt else ((debt + prior_debt) / 2 if prior_debt else debt)
    measured_debt_cost, debt_cost_evidence = measure_cost_of_debt(interest_ltm, avg_debt, revenue_ltm)
    if prior_debt is None and avg_debt:
        debt_cost_evidence += "；缺上年同期，分母取期末值"
    cost_of_debt_pretax = PRETAX_COST_OF_DEBT if measured_debt_cost is None else measured_debt_cost
    measured_beta: float | None = None
    if beta_manual is not None:
        # 显式给定就不走回归——这既是 --no-beta 的配套出路，也是 β 不成立时唯一的出路。
        if beta_manual <= 0:
            raise BetaNotApplicable(f"--beta 给的是 {beta_manual}：β≤0 照样折不出可用的股权成本，人工值也得为正")
        beta, beta_evidence = beta_manual, "人工输入 (--beta / 面板 β 框)，未走回归"
    else:
        beta, beta_evidence = measure_beta(prices)
        measured_beta = beta
    erp, erp_basis, erp_measured, erp_reason = pick_erp(market_stats)
    cost_of_equity, wacc = derive_rates(risk_free, beta, erp, equity_weight, cost_of_debt_pretax, tax_ltm)

    pct = lambda value: None if value is None else round(value, 6)  # noqa: E731
    yi = lambda value: None if value is None else round(value / 1e8, 4)  # noqa: E731
    # 建链那一刻的原料，缺一个就少写一个：整包为空＝这一行没有可在运行时重导的边（图上画虚线）。
    raw = lambda **entries: {k: v for k, v in entries.items() if v is not None}  # noqa: E731
    ev_ebitda = implied_ev_ebitda(
        market_cap=yi(market_cap),
        revenue=yi(revenue_ltm),
        ebit_margin=margin_ltm,
        da_ratio=da_ltm,
        net_debt=yi(net_debt),
        minority=yi(minority),
    )

    if da_window:
        window_year = int(da_window[:4])
        window_md = da_window[5:]
        if window_md == "12-31":
            window_math = f"{window_year} 年报四项合计"
        else:
            window_math = (
                f"{window_year} {P.PERIOD_BY_MD[window_md]} + {window_year - 1} 年报 − {window_year - 1} {P.PERIOD_BY_MD[window_md]}"
            )
        da_evidence = (
            f"最新一期 {snapshot['as_of']} 是一季报或三季报，没有编现金流量表补充资料，四项无披露。"
            f"窗口退到最近真报了数的一期 {da_window}：分子＝{window_math}＝{_fmt_yi(da_ltm_abs)}，"
            f"分母用同一段十二个月的营收 {_fmt_yi(da_window_revenue)}。"
        )
    elif da_ltm_abs is None:
        da_evidence = "报表里没有现金流量表补充资料，四项从未披露，本行留空。"
    else:
        da_evidence = ""

    # 两行 EBIT 率的凭证都要说一句分子动没动；逐年明细与残差只写在「非经常性损益剔除额」那一行。
    # 来源列跟着动：没正常化时它仍写「调整后」，写「正常化后」就成了假凭证。
    margin_basis = "正常化后 EBIT" if normal_window else "调整后 EBIT"
    margin_basis_short = "正常化后" if normal_window else "调整后"
    if not normalize:
        normal_sentence = "--no-normalize：两行 EBIT 率的分子用报表原值，未剔非经常性损益"
    elif normal_window:
        normal_sentence = (
            f"分子已剔非经常性损益：LTM 用 {normal_window[-1].year} 年报的 {_fmt_yi(strip_ltm)}，"
            f"近{MARGIN_LOOKBACK}年均值逐年各剔各的，窗口与残差见「非经常性损益剔除额」行"
        )
    else:
        normal_sentence = (
            f"未正常化（{normalize_model.window_reason(normalizations)}），两行 EBIT 率的分子用报表原值"
        )
    # 这一行进没进分子是批次状态，不是这行的定义，所以 note 跟着窗口走：整窗被拒时还写「数已经用在分子里了」
    # 就是一句假凭证，而面板与图上只有 note 会被读成「这行是干什么的」。
    strip_note = (
        "上面两行 EBIT 率的分子里被剔掉的金额，LTM 用最近整年那一笔，近五年均值逐年各剔各的。只读参考行，数已经用在分子里了。"
        if normal_window else
        "非经常性损益按同一口径折算出来的剔除额，本批次没有从上面两行 EBIT 率的分子里剔掉，只作对照。只读参考行。"
    )

    groups: list[dict] = []

    def add(gid: str, title: str, rows: list[Row]) -> None:
        groups.append({"id": gid, "title": title, "rows": rows})

    anchor_note = (
        f"覆盖机构 {org_count} 家" if org_count else "无研报覆盖，改用历史 CAGR 回退锚"
    )
    add(
        "revenue",
        "收入预测",
        [
            Row(
                "revenue_ltm",
                "营业总收入 LTM",
                "亿元",
                yi(revenue_ltm),
                "GINCOME 累计值滚动十二个月",
                note="最近四个季度的营收合计，LTM 口径的规模基准；各比率的分子分母多与它同频。",
                evidence=f"报表截至 {snapshot['as_of']}。",
            ),
            Row(
                "revenue_base",
                "最后年报营收",
                "亿元",
                yi(last_actual_revenue),
                "GINCOME 年报序列",
                note="最后一份完整年报的营收，增速路径从这一年起算。",
                evidence=f"={last_actual_year}。",
            ),
            Row("revenue_cagr_5y", "近5年营收 CAGR", "pct", pct(revenue_cagr), "GINCOME 年报序列", note="无一致预期时的回退锚，决定显性期首年的增速",
                raw=raw(first_year=cagr_first, first_revenue=yi(revenue_annual.get(cagr_first)), last_year=last_actual_year)),
            Row(
                "anchor_source",
                "收入锚点来源",
                "文本",
                "一致预期" if anchors else "历史CAGR回退",
                "yctj_list / GINCOME",
                note="说明这几年的营收锚取自卖方一致预期还是历史增速回退，后面的置信度都跟着它。",
                evidence=anchor_note,
            ),
            Row(
                "consensus_revenue",
                "营收一致预期",
                "亿元",
                {y: yi(v) for y, v in anchors.items()},
                "yctj_list TOTAL_OPERATE_INCOME(E)",
                kind="estimate",
                confidence=anchor_confidence,
                note="卖方一致预期的分年营收，显性期前几年的锚点；改它等于改整段收入路径的起点。",
                evidence=anchor_note,
            ),
            Row("consensus_dispersion", "机构 EPS 分歧度 CV", "pct", {y: pct(d.get("cv")) for y, d in consensus["dispersion"].items()}, "jgyc 逐家预测", kind="estimate", note="越大越分歧，锚点越不可信"),
            Row("revenue_growth_path", "营收增速路径", "pct", {y: pct(v) for y, v in growth.items()},
                "一致预期锚定 + 向级联永续增速线性回落", kind="estimate"),
            Row("revenue_path", "营收绝对额路径", "亿元", {y: yi(v) for y, v in revenue_path.items()}, "锚点与增速推导", kind="estimate"),
        ],
    )

    add(
        "profitability",
        "盈利与税率",
        [
            Row("ebit_margin_ltm", "EBIT 率 LTM", "pct", pct(margin_ltm),
                f"{margin_basis} / TOTAL_OPERATE_INCOME",
                note="没有任何券商锚点时，整条 EBIT 率路径由这一行起头；有锚的年份路径用券商口径，这一行只作对照。",
                evidence=_ebit_evidence(ebit_bridge) + "。" + normal_sentence,
                raw=raw(ebit_ltm=yi(ebit_ltm_recurring))),
            Row("ebit_margin_hist", f"EBIT 率近{MARGIN_LOOKBACK}年均值", "pct", pct(margin_hist),
                f"{margin_basis} / TOTAL_OPERATE_INCOME 年报序列",
                note="EBIT 率路径的回落终点，与 EBIT 率 LTM 行同一口径。",
                evidence=normal_sentence),
            Row(
                "normalization_strip", "非经常性损益剔除额", "亿元", yi(strip_ltm),
                "(归母净利润 − 扣非归母净利润) ÷ [(1 − 实际税率)(1 − 少数股东比例)] × w，"
                "w＝偶发科目落在营业利润内的份额；位置由两条加总恒等式判定",
                kind="data",
                note=strip_note,
                evidence=(
                    normalize_model.evidence(normalizations)
                    + ("。LTM 用的是最近整年的那一笔，不是季度差分——单季扣非差额年内会反号"
                       if normal_window else "。本批次未进定价")
                ),
                raw=raw(ebit_ltm_reported=yi(ebit_ltm)),
            ),
            Row("ebit_margin_consensus", "EBIT 率一致预期", "pct", {y: pct(v) for y, v in margin_consensus.items()},
                "yctj_list 营业利润 / 营业总收入", kind="estimate", confidence=anchor_confidence,
                note="卖方预测的营业利润率，不做科目调整。",
                evidence=_margin_basis_gap(margin_consensus, margin_ltm, margin_basis_short)),
            Row("ebit_margin_start", "显性期首年 EBIT 率", "pct", pct(margin_start),
                f"一致预期首年，缺锚时用{margin_basis_short} EBIT LTM", kind="estimate",
                note="有锚取券商口径、无锚取机器口径，两种口径的分子定义不同，取到哪一档看这一行的来源列。"),
            Row(
                "ebit_margin_path",
                "EBIT 率分年路径",
                "pct",
                {y: pct(v) for y, v in margin_path.items()},
                "一致预期覆盖年 + 向近5年均值回落",
                kind="estimate",
                note="锚定年与回落终点分属两档分子：前者是券商的营业利润，后者是上面两行 EBIT 率的分子。",
            ),
            Row(
                "net_profit_ltm",
                "归母净利润 LTM",
                "亿元",
                yi(net_profit_ltm),
                "GINCOME PARENT_NETPROFIT 累计值滚动十二个月",
                note="最近四个季度归属母公司股东的净利润合计，当前滚动市盈率那一行拿它作分母。",
                evidence=f"报表截至 {snapshot['as_of']}。",
            ),
            Row("tax_rate_ltm", "有效税率 LTM", "pct", pct(tax_ltm), "INCOME_TAX / TOTAL_PROFIT"),
            Row("tax_rate_hist", f"有效税率近{MARGIN_LOOKBACK}年均值", "pct", pct(tax_hist), "GINCOME 年报序列"),
        ],
    )

    add(
        "reinvestment",
        "再投资与营运资本",
        [
            Row("da_to_revenue_ltm", "折旧摊销/营收 LTM", "pct", pct(da_ltm), "GCASHFLOW 折旧摊销四项",
                note="含使用权资产摊销；租赁已计为债务，FCFF 不再扣租赁本金流出",
                evidence=da_evidence,
                raw=raw(da_ltm=yi(da_ltm_abs), da_window=da_window or None,
                        da_window_revenue=yi(da_window_revenue) if da_window else None,
                        revenue_ltm=yi(revenue_ltm) if da_window else None)),
            Row("da_to_revenue_hist", "折旧摊销/营收 近5年均值", "pct", pct(P.mean(_tail(da_annual, MARGIN_LOOKBACK))), "GCASHFLOW 年报序列"),
            Row("capex_to_revenue_ltm", "资本支出/营收 LTM", "pct", pct(capex_ltm), CAPEX_KEY,
                raw=raw(capex_ltm=yi(capex_ltm_abs))),
            Row("capex_to_revenue_hist", "资本支出/营收 近5年均值", "pct", pct(P.mean(_tail(capex_annual, MARGIN_LOOKBACK))), "GCASHFLOW 年报序列"),
            Row("nwc_to_revenue_delta", "营运资本变动/营收变动", "pct", pct(nwc_ratio), "应收票据及应收账款+存货−应付票据及应付账款−应交税费 差分"),
        ],
    )

    bridge_rows = [
        Row("cash_and_financial_assets", "货币资金+交易性金融资产", "亿元", yi(cash), "GBALANCE"),
        Row(
            "interest_bearing_debt",
            "有息负债",
            "亿元",
            yi(debt),
            "GBALANCE 短借+一年内到期+长借+债券" + ("+租赁负债(非流动)" if include_lease_debt else ""),
            note="算作债务的有息负债合计，既进净债务，也定 WACC 里债务那条腿的权重。",
            evidence="" if lease_debt is None else f"其中租赁负债(非流动) {yi(lease_debt)} 亿元。",
        ),
        Row("net_debt", "净债务", "亿元", yi(net_debt), "有息负债-现金", note="负值即净现金"),
        Row("minority_interest", "少数股东权益", "亿元", yi(minority), "GBALANCE"),
        Row("net_borrowing", "净借款流入", "亿元", yi(net_borrowing), "GCASHFLOW 借款收现-偿债付现", note="仅 FCFE 口径使用"),
    ]
    if include_lease_debt:
        bridge_rows.append(
            Row(
                "lease_financing",
                "租赁净融资",
                "亿元",
                yi(lease_financing),
                "GBALANCE 租赁负债(非流动) 较上年同日",
                note="仅 FCFE 口径使用，与净借款流入相加；不含一年内到期部分",
            )
        )
    bridge_rows.append(
        Row(
            "roic_ltm",
            "LTM ROIC",
            "pct",
            pct(roic),
            "NOPAT/(净资产+有息负债-现金)",
            note="低于 WACC 时增长毁灭价值",
            raw=raw(ebit_ltm=yi(ebit_ltm_recurring), total_equity=yi(total_equity)),
        )
    )
    add("capital_structure", "资本结构与桥", bridge_rows)

    add(
        "discount_rate",
        "折现率",
        [
            *risk_free_rows(risk_free, risk_free_source, rf_window, rf_basis),
            Row(
                "beta",
                "贝塔",
                "倍数",
                round(beta, 4),
                f"周收益对 {benchmark_label(prices)} OLS 回归" if measured_beta is not None else "人工输入 (--beta)",
                kind="data" if measured_beta is not None else "convention",
                note=(
                    "个股相对市场组合的联动强度，股权成本里的风险补偿按它放大。"
                    f"本行取{'回归实测' if measured_beta is not None else '人工输入'}"
                    "（β≤0 时 CAPM 不成立，建链会拒绝而不是填个占位数）。"
                ),
                evidence=beta_evidence + (f"；取数：{prices_source}" if prices_source else ""),
            ),
            *erp_rows(market_stats, erp, erp_basis, erp_measured, market_source),
            Row(
                "cost_of_debt_pretax",
                "税前债务成本",
                "pct",
                pct(cost_of_debt_pretax),
                "利息费用 LTM / 平均有息负债" if measured_debt_cost is not None else "LPR 附近",
                kind="data" if measured_debt_cost is not None else "convention",
                note=(
                    "WACC 里债务利息那条腿，按利息费用对平均有息负债的比值取税前口径，"
                    f"权益权重之外的那部分资本就用它计价。本行取{'实测值' if measured_debt_cost is not None else '惯例值'}。"
                ),
                evidence=debt_cost_evidence
                if measured_debt_cost is not None
                else f"实测被拒：{debt_cost_evidence}；沿用惯例值 {PRETAX_COST_OF_DEBT:.2%}",
                raw=raw(interest_ltm=yi(interest_ltm), prior_debt=yi(prior_debt)),
            ),
            Row("equity_weight", "权益权重", "pct", pct(equity_weight), "总市值/(总市值+有息负债)"),
            Row(
                "cost_of_equity",
                "股权成本 CAPM",
                "pct",
                pct(cost_of_equity),
                "级联：rf + β·ERP",
                kind="derived",
                note="改上游任一值即重算，不可直接编辑",
            ),
            Row(
                "wacc",
                "WACC",
                "pct",
                pct(wacc),
                "级联：权益权重×股权成本 + 债务权重×税前债务成本×(1−税率)",
                kind="derived",
                note="改上游任一值即重算，不可直接编辑",
            ),
        ],
    )

    add(
        "terminal",
        "终值与口径",
        [
            *terminal_growth_rows(tg_stats, sus_stats, terminal_growth, tg_basis, tg_measured),
            Row("exit_pe", "一致预期市盈率", "倍数", {y: (v.get("pe") and round(v["pe"], 2)) for y, v in estimate.items()}, "yctj_chart PE(E)", kind="estimate"),
            Row("pe_ttm", "当前滚动市盈率", "倍数", pe_ttm, "总市值 ÷ 归母净利润 LTM", evidence=pe_evidence, note="FCFE 退出倍数在预测末年之后沿用"),
            Row("exit_ev_ebitda", "隐含 EV/EBITDA", "倍数", None if ev_ebitda is None else round(ev_ebitda, 2), "现价反推", kind="estimate", note="FCFF 退出倍数默认值"),
            Row(
                "horizon_target",
                "显性预测期",
                "年",
                requested_horizon,
                f"惯例 {HORIZON_TARGET_YEARS} 年，可改 (--horizon / 面板)",
                kind="convention",
                note="详细预测期一共走几年，也就是增速从末锚回落到永续 g 需要几年；改它会整段换掉增速路径的年份数。",
                evidence=(
                    f"本批实走 {horizon} 年（锚点 {len(anchors)} 年 + 回落 {glide_tail} 年，"
                    f"下限 max({HORIZON_MIN_YEARS}, 锚点 + 1)、上限 {HORIZON_MAX_YEARS}）。"
                    + (f"{horizon_clamp}。" if horizon_clamp else "")
                    + (
                        f"回落段每年变动 {glide_fade:+.2%}，末年 {_pct_text(glide_last)} → 永续 "
                        f"{_pct_text(TERMINAL_GROWTH if terminal_growth is None else terminal_growth)}。"
                        if glide_fade is not None
                        else "没有回落段（锚点之外的一年都没有）。"
                    )
                ),
            ),
            Row(
                "horizon_years",
                "显性预测年数",
                "年",
                horizon,
                f"一致预期 {len(anchors)} 年 + 回落 {glide_tail} 年，取自「显性预测期」那一行",
                note="显性期一共折到哪一年，决定终值站在哪一期现金流上；它不进级联——定价用的是增速行覆盖了几年，这一行只把那个数说给人看。",
                evidence=(
                    f"引擎折 {horizon} 年（{sorted(growth)[0] if growth else '—'}–{sorted(growth)[-1] if growth else '—'}），"
                    f"每年变动 {glide_fade:+.2%}。" if growth else f"引擎折 {horizon} 年，增速路径为空。"
                ),
            ),
            Row("mid_year_discount", "年中折现", "bool", True, "默认开启", kind="convention"),
        ],
    )

    add(
        "market",
        "市场与股本",
        [
            Row("price", "最新价", "元", quote.get("price"), f"quote:{quote.get('source')}", note="" if quote.get("available") else "行情源均不可用，需人工填入"),
            Row("total_shares", "总股本", "亿股", yi(quote.get("total_shares")), f"quote:{quote.get('source')}", note="未含潜在稀释"),
            Row("market_cap", "总市值", "亿元", yi(market_cap), f"quote:{quote.get('source')}"),
        ],
    )

    return Ladder(
        ticker={
            "code": snapshot["code"],
            "secucode": snapshot["secucode"],
            "name": quote.get("name") or snapshot["code"],
            "report_as_of": snapshot["as_of"],
        },
        as_of=snapshot["as_of"],
        groups=groups,
        warnings=[
            *erp_warnings(market_stats, erp_measured, erp_reason),
            *horizon_warnings(
                horizon,
                horizon_clamp,
                glide_fade,
                glide_last,
                TERMINAL_GROWTH if terminal_growth is None else terminal_growth,
            ),
        ],
    )
