"""非经常性损益的正常化：把官方扣非差额搬到 EBIT 层。

《监管规则适用指引——解释性公告第 1 号》（2023 修订）定义的是归母、税后的一个净额：
S = 归母净利润 − 扣非归母净利润。它从不说明那笔偶发落在营业利润的上面还是下面，而级联吃的是
营业利润，所以搬运它需要两件事：把税后净额还原成税前总额（gross-up），以及一个位置份额 w。
金额一律取自官方 S，科目表在这里只降级成分类器——用来定位置，不用来定金额。

为什么必须有 w：茅台 2018 的 S 是 −3.82 亿，主体是 5.27 亿对外捐赠（营业外支出，本来就不在
513.43 亿的营业利润里）。不做位置判断、直接把反推额从 EBIT 里扣掉，会让 EBIT 凭空抬高 1.07%；
按 w≈0.02 调整后是 +0.02%，误差缩小约 50 倍。

只用年报：A 股偶发项年末一次算清，单季 S 年内会反号（紫金 2025 四季是 2.85／13.82／20.69／−26.83 亿），
拿它差分进 LTM 分子等于塞进一个随 Q4 滚入而跳变的阶梯函数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from backend.model import periods as P

# 两条加总恒等式的容差，按 |EBIT| 计。超过它就不认这套分堆——残差本身就是「位置未知」的规模。
IDENTITY_TOLERANCE = 0.005

# A 堆：营业利润之内的偶发候选。每项是 (主字段, 主符号, 缺失时的替代字段, 替代符号)。
# 符号按报表填列方向定（收益为正）；孪生科目只会出现一支，替代支取反——
# 判据是实测：招行 2024 按此把 E2 残差从 42.42% 收到 2.24%。
# INVEST_JOINT_INCOME 是 INVEST_INCOME 的「其中：」子项，含它会把同一笔收益计两遍——
# 实测含它时万科残差 4.948%、中石化 7.032%，不含时两者归 0.000%，所以不列。
IN_EBIT_STACK = (
    ("FAIRVALUE_CHANGE_INCOME", 1.0, None, None),
    ("INVEST_INCOME", 1.0, None, None),
    ("ASSET_DISPOSAL_INCOME", 1.0, None, None),
    ("ASSET_IMPAIRMENT_INCOME", 1.0, "ASSET_IMPAIRMENT_LOSS", -1.0),
    ("CREDIT_IMPAIRMENT_INCOME", 1.0, "CREDIT_IMPAIRMENT_LOSS", -1.0),
    ("OTHER_INCOME", 1.0, None, None),
)
# B 堆：营业利润之外、利润总额之内的偶发（营业外收支与非流动资产处置损益）。
BELOW_EBIT_STACK = (
    ("NONBUSINESS_INCOME", 1.0, None, None),
    ("NONBUSINESS_EXPENSE", -1.0, None, None),
    ("NONCURRENT_DISPOSAL_INCOME", 1.0, None, None),
    ("NONCURRENT_DISPOSAL_LOSS", -1.0, None, None),
)

CAPTIONS = {
    "FAIRVALUE_CHANGE_INCOME": "公允价值变动收益",
    "INVEST_INCOME": "投资收益",
    "ASSET_DISPOSAL_INCOME": "资产处置收益",
    "ASSET_IMPAIRMENT_INCOME": "资产减值收益",
    "ASSET_IMPAIRMENT_LOSS": "资产减值损失",
    "CREDIT_IMPAIRMENT_INCOME": "信用减值收益",
    "CREDIT_IMPAIRMENT_LOSS": "信用减值损失",
    "OTHER_INCOME": "其他收益",
    "NONBUSINESS_INCOME": "营业外收入",
    "NONBUSINESS_EXPENSE": "营业外支出",
    "NONCURRENT_DISPOSAL_INCOME": "非流动资产处置收益",
    "NONCURRENT_DISPOSAL_LOSS": "非流动资产处置损失",
}


def _fmt_yi(amount: float | None) -> str:
    return "—" if amount is None else f"{amount / 1e8:,.2f} 亿元"


@dataclass(frozen=True)
class Stack:
    """One caption pile: signed total (for the identity), absolute total (for w), the terms."""

    signed: float = 0.0
    magnitude: float = 0.0
    terms: tuple[tuple[str, float], ...] = ()


def pile(row: dict | None, stack: Iterable[tuple]) -> Stack:
    """Sum a caption pile. Within a twin pair only the first reported caption is counted."""
    if not row:
        return Stack()
    signed, magnitude, terms = 0.0, 0.0, []
    for primary, primary_sign, fallback, fallback_sign in stack:
        key, sign, value = primary, primary_sign, P.num(row, primary)
        if value is None and fallback:
            key, sign, value = fallback, fallback_sign, P.num(row, fallback)
        if value is None:
            continue
        signed += sign * value
        magnitude += abs(value)
        terms.append((key, sign * value))
    return Stack(signed, magnitude, tuple(terms))


@dataclass(frozen=True)
class YearNormalization:
    """One fiscal year's verdict: how much non-operating profit sits inside EBIT, if we can tell."""

    year: int
    ebit: float
    accepted: bool
    reason: str
    non_recurring: float | None = None  # S：归母、税后
    tax_rate: float | None = None
    minority_share: float | None = None
    position: float | None = None  # w：A 堆在总偶发额绝对值里的份额
    gross: float | None = None  # 反推成税前总额的那个数
    strip: float | None = None  # 从 EBIT 里剔掉的金额（元）
    ebit_normalized: float | None = None
    e1_residual: float | None = None
    e2_residual: float | None = None
    in_ebit: Stack = Stack()
    below_ebit: Stack = Stack()


def normalize_year(year: int, row: dict | None, ebit: float | None) -> YearNormalization:
    """One annual row in, one verdict out. Never raises: a rejected year carries the reason."""
    if not ebit:
        return YearNormalization(year, ebit or 0.0, False, "该年没有营业利润")

    inside = pile(row, IN_EBIT_STACK)
    below = pile(row, BELOW_EBIT_STACK)
    operate = P.num(row, "OPERATE_PROFIT")
    total_profit = P.num(row, "TOTAL_PROFIT")
    revenue = P.num(row, "TOTAL_OPERATE_INCOME")
    cost = P.num(row, "TOTAL_OPERATE_COST")

    def rejected(reason: str, **extra) -> YearNormalization:
        return YearNormalization(
            year, ebit, False, reason, e1_residual=_e1(), e2_residual=_e2(),
            in_ebit=inside, below_ebit=below, **extra,
        )

    def _e1() -> float | None:
        if None in (total_profit, operate):
            return None
        return abs(total_profit - operate - below.signed) / abs(ebit)

    def _e2() -> float | None:
        if None in (operate, revenue, cost):
            return None
        return abs(operate - (revenue - cost) - inside.signed) / abs(ebit)

    e1, e2 = _e1(), _e2()
    if e1 is None or e2 is None:
        return rejected("利润总额或营业总成本缺失，两条恒等式无法验算")
    if max(e1, e2) > IDENTITY_TOLERANCE:
        # 不闭合＝这套分堆在该类报表上不自证（金融股的营业总成本结构本就不同），位置未知就不搬金额。
        return rejected(
            f"两条加总恒等式不闭合（E1 {e1:.2%}、E2 {e2:.2%}，容差 {IDENTITY_TOLERANCE:.2%}）"
        )

    parent = P.num(row, "PARENT_NETPROFIT")
    deduct = P.num(row, "DEDUCT_PARENT_NETPROFIT")
    if parent is None or deduct is None:
        return rejected("东财未披露该年扣非归母净利，金额来源缺失")
    s = parent - deduct

    net = P.num(row, "NETPROFIT")
    # 亏损年照算：利润总额与净利润同时为负时，t 是税盾、m 是少数股东的亏损份额，分母仍在正常区间
    # （盘上 132 个恒等式闭合的年报行实测 (1−t)(1−m) 落在 0.408~1.198）。只有 0 是致命的。
    if not total_profit or not net:
        return rejected("利润总额或净利润为 0，税盾与少数比例无从反推", non_recurring=s)

    # 无少数股东的公司 MINORITY_INTEREST 整列为 None，按 0 处理：实测那不是覆盖率缺口。
    minority = P.num(row, "MINORITY_INTEREST") or 0.0
    minority_share = minority / net
    tax = P.num(row, "INCOME_TAX")
    if tax is None:
        return rejected(
            "所得税费用缺失，无法把税后净额还原成税前", non_recurring=s, minority_share=minority_share
        )
    tax_rate = tax / total_profit
    denominator = (1 - tax_rate) * (1 - minority_share)
    if denominator <= 0:
        return rejected(
            "税盾与少数股东比例之积不为正，反推无意义",
            non_recurring=s, tax_rate=tax_rate, minority_share=minority_share,
        )
    if inside.magnitude + below.magnitude == 0:
        # 两堆都空：偶发额没在任何科目里露过面，多半是 S 本身为 0 之外的口径缺口，不许用小分母抖出满额调整。
        return rejected(
            "科目两堆均为 0，这笔偶发无处安放",
            non_recurring=s, tax_rate=tax_rate, minority_share=minority_share,
        )

    gross = s / denominator
    # w 只取绝对值占比：符号交给 gross-up 那一层，位置份额不能把调整额改成反号。
    position = inside.magnitude / (inside.magnitude + below.magnitude)
    strip = gross * position
    if (ebit - strip) * ebit <= 0:
        # 唯一的规模护栏：正常化可以把盈利做薄、把亏损做窄，但不许把营业利润推过零、改成反号。
        # 盘上 132 个恒等式闭合的年报行实测触发 0 次；被它拦下的都是「剔除额比整年营业利润还大」这种
        # 无法解释的情形——位置与反推的两项误差在这个量级上已经盖住了被测对象本身。
        return rejected(
            "剔除额不小于当年营业利润，正常化会把营业利润推成反号或零",
            non_recurring=s, tax_rate=tax_rate, minority_share=minority_share, position=position, gross=gross,
        )

    return YearNormalization(
        year, ebit, True, "", non_recurring=s, tax_rate=tax_rate, minority_share=minority_share,
        position=position, gross=gross, strip=strip, ebit_normalized=ebit - strip,
        e1_residual=e1, e2_residual=e2, in_ebit=inside, below_ebit=below,
    )


def annual_normalizations(
    rows: Iterable[dict], ebit_of: Callable[[dict], float | None], years: int
) -> list[YearNormalization]:
    """The most recent `years` annual rows, normalized. Years failing a guardrail stay in the list."""
    annual = P.annual_rows(rows)
    return [normalize_year(year, annual[year], ebit_of(annual[year])) for year in sorted(annual)[-years:]]


def accepted_window(items: Iterable[YearNormalization]) -> list[YearNormalization]:
    """整窗判定：窗口里有一年没过护栏，整个窗口就不正常化。

    不允许「前三年用官方扣非、后两年自算」——两套口径在茅台上都差 4.7 倍，混进同一个 5 年序列
    会让回落终点落在一个无法复现的混合数上。
    """
    kept = list(items)
    return kept if kept and all(item.accepted for item in kept) else []


def window_reason(items: Iterable[YearNormalization]) -> str:
    """一句话说清这个窗口为什么没正常化；窗口可用时回空串。"""
    blocked = [item for item in items if not item.accepted]
    if not items:
        return "没有可用的年报窗口"
    if not blocked:
        return ""
    years = "、".join(str(item.year) for item in blocked)
    return f"{years}：{blocked[0].reason}"


def residual_text(e1: float | None, e2: float | None) -> str:
    """两条恒等式各说各的：一条缺料只说那一条验不了，别把另一条的数一起抹掉。"""
    if e1 is None and e2 is None:
        return "残差无法验算"

    def one(label: str, value: float | None) -> str:
        return f"{label} 无法验算" if value is None else f"{label} {value:.4%}"

    return "／".join((one("E1", e1), one("E2", e2)))


def evidence(items: Iterable[YearNormalization]) -> str:
    """逐年凭证：剔了多少、位置份额多大、两条恒等式的残差是多少。

    位置是堆级近似——逐笔明细在年报「非经常性损益明细表」正文里，免登录通道拿不到，
    所以凭证如实写「按科目楼层分堆」。
    """
    kept = list(items)
    if not kept:
        return "没有年报窗口可用"
    lines = []
    for item in kept:
        residuals = residual_text(item.e1_residual, item.e2_residual)
        if item.accepted:
            lines.append(
                f"{item.year} 剔除 {_fmt_yi(item.strip)}（S {_fmt_yi(item.non_recurring)}，"
                f"税盾 {item.tax_rate:.2%}、少数 {item.minority_share:.2%}、位置 w {item.position:.3f}，"
                f"{residuals}）"
            )
        else:
            # 恒等式不闭合那条理由里已经带着 E1/E2 的数，再追一个括注就是把同一对残差写两遍。
            # 其余护栏的 reason 没有这两个数（缺料那一支尤其要说「哪一条验不了」），括注要留。
            note = "" if item.reason.startswith("两条加总恒等式不闭合") else f"（{residuals}）"
            lines.append(f"{item.year} 未正常化：{item.reason}{note}")
    tail = ""
    if any(item.accepted for item in kept):
        tail = "。金额取自官方披露的归母与扣非归母之差，位置按科目楼层分堆判定，不是逐笔明细"
    return "；".join(lines) + tail
