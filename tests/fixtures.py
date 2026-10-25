"""A hand-built toy company shared by the model tests: no network, no stored run needed.

1000亿 revenue, 30% EBIT margin, 25% effective tax, D&A 4% / capex 5% of revenue, 200亿 borrowed debt
plus 25亿 of non-current lease liability against 120亿 cash, 100亿股 at 30元.

`Frame`/`Spy` stand in for akshare's tables so the channel layer can be tested without pandas or network.
"""
from __future__ import annotations

import json

from backend.data import prices
from backend.model.assumptions import build_ladder, parse_consensus

YI = 1e8
REVENUE = {2021: 700, 2022: 780, 2023: 860, 2024: 940, 2025: 1000}
LEASE_LIAB = {2021: 12, 2022: 14, 2023: 16, 2024: 20, 2025: 25}


class Frame:
    """够 `prices.parse_closes` 用的最小「表」：列名 + 行元组。

    parse_closes 只读 `.columns`，再 `frame[[a, b]].itertuples(index=False, name=None)`，所以这里只实现
    这两件——测试用它冒充 akshare 的答复，免得把 pandas 拖进一套本可以纯 python 跑的哨兵里。
    """

    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.columns = list(columns)
        self._rows = list(rows)

    def __getitem__(self, names: list[str]) -> "Frame":
        wanted = [self.columns.index(name) for name in names]
        return Frame(names, [tuple(row[i] for i in wanted) for row in self._rows])

    def itertuples(self, index: bool = False, name: str | None = None):
        return iter(self._rows)


class Spy:
    """替 `prices.fetch_frame` 记账：哪个函数被调过、带什么参数、给了什么答复。"""

    def __init__(self, answers: dict[str, Frame]) -> None:
        self.answers = answers
        self.kwargs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, func: str, **kwargs) -> Frame:
        self.calls.append((func, str(kwargs.get("symbol", ""))))
        self.kwargs.setdefault(func, kwargs)
        if func not in self.answers:
            raise prices.PricesUnavailable(f"测试替身没有为 {func} 准备答复")
        return self.answers[func]


def _fy(year: int, **fields) -> dict:
    return {"REPORT_DATE": f"{year}-12-31", **fields}


def synthetic_snapshot(
    lease_caption: bool = True,
    interest_expense: float | None = None,
    fairvalue_change: float | None = None,
    normalization: bool = False,
    quarterly_head: bool = False,
) -> dict:
    """`interest_expense` / `fairvalue_change` 是「财务费用下的利息支出」与「公允价值变动收益」，单位亿元。

    两者都只落在 2025 年报行——与 `FE_INTEREST_EXPENSE` 在东财答复里的常见形态一致（只在最新一期披露）。

    `normalization` 给每年的报表行补上正常化要读的那几列，并且补得让两条加总恒等式**精确归零**：
    投资收益 1% 营收（A 堆）、营业外收入 2%（B 堆）、扣非差额 0.3%（S）。默认关——盘上其余套件的
    期望值都建立在「这一组没接入」的那批数上，打开会让 20 多个哨兵同时漂。

    `quarterly_head` 在年报之上再加四行中期（2025-06-30、2025-09-30、2026-06-30、2026-09-30，报表截至日
    跟着变成 2026-09-30），并按真实披露形态只在中报行编现金流量表补充资料——季报行有营收、有资本开支，
    唯独折旧摊销那四项是空的。它是「最新一期是季报、滚动窗口断在没有披露的那一格」这条路径的最小样本。
    默认关，其余套件的期望值不受影响。
    """
    def extras(y: int) -> dict:
        if y != 2025:
            return {}
        out = {}
        if interest_expense is not None:
            out["FE_INTEREST_EXPENSE"] = interest_expense * YI
        if fairvalue_change is not None:
            out["FAIRVALUE_CHANGE_INCOME"] = fairvalue_change * YI
        return out

    def recurring(v: float) -> dict:
        """正常化要读的那几列，按营收等比造：E1 = 0.32 − 0.30 − 0.02 = 0，E2 = 0.30 − (1 − 0.71) − 0.01 = 0。"""
        if not normalization:
            return {}
        return {
            "INVEST_INCOME": v * YI * 0.01,
            "NONBUSINESS_INCOME": v * YI * 0.02,
            "TOTAL_OPERATE_COST": v * YI * 0.71,
            "NETPROFIT": v * YI * 0.24,
            "MINORITY_INTEREST": v * YI * 0.024,
            "PARENT_NETPROFIT": v * YI * 0.216,
            "DEDUCT_PARENT_NETPROFIT": v * YI * 0.213,
        }

    income = [
        _fy(
            y,
            TOTAL_OPERATE_INCOME=v * YI,
            OPERATE_PROFIT=v * YI * 0.30,
            TOTAL_PROFIT=v * YI * 0.32,
            INCOME_TAX=v * YI * 0.08,
            **recurring(v),
            # 归母净利：正常化那一组由 recurring 给（0.216，配合 0.003 的扣非差额），其余按 24% 净利率造。
            **({} if normalization else {"PARENT_NETPROFIT": v * YI * 0.24}),
            **extras(y),
        )
        for y, v in REVENUE.items()
    ]
    balance = [
        _fy(
            y,
            MONETARYFUNDS=120 * YI,
            SHORT_LOAN=30 * YI,
            LONG_LOAN=170 * YI,
            **({"LEASE_LIAB": LEASE_LIAB[y] * YI} if lease_caption else {}),
            MINORITY_EQUITY=40 * YI,
            TOTAL_EQUITY=900 * YI,
            NOTE_ACCOUNTS_RECE=v * YI * 0.10,
            NOTE_ACCOUNTS_PAYABLE=v * YI * 0.08,
        )
        for y, v in REVENUE.items()
    ]
    cashflow = [
        _fy(
            y,
            FA_IR_DEPR=v * YI * 0.04,
            CONSTRUCT_LONG_ASSET=v * YI * 0.05,
            RECEIVE_LOAN_CASH=20 * YI,
            PAY_DEBT_CASH=15 * YI,
        )
        for y, v in REVENUE.items()
    ]
    if quarterly_head:
        # 中期行的金额按全年节奏造：营收是年初至当期末的累计值，补充资料只在 06-30 那两行编，季报行留空。
        for date, revenue, with_supplement in (("2025-06-30", 480, True), ("2025-09-30", 750, False),
                                               ("2026-06-30", 560, True), ("2026-09-30", 850, False)):
            income.append({
                "REPORT_DATE": date,
                "TOTAL_OPERATE_INCOME": revenue * YI,
                "OPERATE_PROFIT": revenue * YI * 0.30,
                "TOTAL_PROFIT": revenue * YI * 0.32,
                "INCOME_TAX": revenue * YI * 0.08,
                "PARENT_NETPROFIT": revenue * YI * 0.24,
            })
            cashflow.append({
                "REPORT_DATE": date,
                "CONSTRUCT_LONG_ASSET": revenue * YI * 0.05,
                **({"FA_IR_DEPR": revenue * YI * 0.04} if with_supplement else {}),
            })
            balance.append({
                "REPORT_DATE": date,
                "MONETARYFUNDS": 120 * YI,
                "SHORT_LOAN": 30 * YI,
                "LONG_LOAN": 170 * YI,
                **({"LEASE_LIAB": 25 * YI} if lease_caption else {}),
                "MINORITY_EQUITY": 40 * YI,
                "TOTAL_EQUITY": 900 * YI,
                "NOTE_ACCOUNTS_RECE": revenue * YI * 0.10,
                "NOTE_ACCOUNTS_PAYABLE": revenue * YI * 0.08,
            })

    consensus = parse_consensus(
        {
            "yctj_list": [
                {"YEAR": 2026, "YEAR_MARK": "E", "TOTAL_OPERATE_INCOME": 1100 * YI, "OPERATE_PROFIT": 330 * YI,
                 "TOTAL_OPERATE_INCOME_COUNT": 10},
                {"YEAR": 2027, "YEAR_MARK": "E", "TOTAL_OPERATE_INCOME": 1180 * YI, "OPERATE_PROFIT": 355 * YI,
                 "TOTAL_OPERATE_INCOME_COUNT": 9},
            ],
            "yctj_chart": [{"YEAR": 2026, "YEAR_MARK": "E", "PE": 15.2}, {"YEAR": 2027, "YEAR_MARK": "E", "PE": 14.0}],
            "jgyc": [{"ORG_CODE": "00000011", "ORG_NAME_ABBR": "甲证券", "YEAR1": 2026, "EPS1": 3.0}],
        }
    )
    quote = {
        "available": True,
        "source": "test",
        "price": 30.0,
        "total_shares": 100 * YI,
        "total_market_cap": 3000 * YI,
        # 通道原值故意与自算的 12.5 不等：哨兵要能看出这一行进的是自算那个数，不是这个。
        "pe_channel": 13.5,
        "pe_channel_basis": "test 通道字段",
        "name": "测试股份",
    }
    return {
        "code": "000001",
        "secucode": "000001.SZ",
        "as_of": "2026-09-30" if quarterly_head else "2025-12-31",
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "main": [],
        "consensus": consensus,
        "quote": quote,
    }


def synthetic_ladder(
    risk_free: float = 0.02,
    include_lease_debt: bool = True,
    interest_expense: float | None = None,
    beta: float = 1.00,
    normalize: bool = False,
    quarterly_head: bool = False,
) -> dict:
    """Round-trip through JSON so year keys are strings, exactly like a stored baseline.json.

    No price snapshot here, so β has to be handed over explicitly — 引擎不再有占位值，回归出的 β 由
    test_beta.py 专测。默认 1.00 与旧惯例值同数，各套件的历史期望值因此不受语义改动影响。

    `normalize` 同时管两件事：报表行里有没有正常化要读的那几列、级联开不开这道减法。默认关，
    于是「关掉正常化的链」就是盘上其余套件一直在用的那批数——不需要第二个基准。
    `quarterly_head` 透传给 `synthetic_snapshot`，见那里的说明（折旧摊销窗口退锚那条路径的最小样本）。
    """
    snapshot = synthetic_snapshot(interest_expense=interest_expense, normalization=normalize,
                                  quarterly_head=quarterly_head)
    ladder = build_ladder(
        snapshot, snapshot["consensus"], snapshot["quote"], risk_free, include_lease_debt=include_lease_debt,
        normalize=normalize, beta_manual=beta,
    ).to_json()
    return json.loads(json.dumps(ladder, ensure_ascii=False))
