"""The single build chain: fetch evidence, measure the assumptions, write the baseline.

Both entry points run this — scripts/build_baseline.py prints the ladder, the web panel serves it — so a
code typed into the browser produces byte-identical artifacts to a CLI run. `progress` exists only so the
panel can say which source is still loading; the CLI ignores it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend.data import fundamentals, macro, market, prices, universe
from backend.data.em_company import Ticker
from backend.data.transport import RunStore, Transport
from backend.model.assumptions import BetaNotApplicable, build_ladder

Progress = Callable[[str], None]

RISK_FREE_BASIS = ("norm", "spot")


class TickerUnavailable(RuntimeError):
    """名录判定这只票当前不在市（已退市／从没上市／代码写错）：一个来源都不碰就停。

    与 `prices.PricesUnavailable` 分家：那一类是「联网了却没取到」，可能是源抖动，该重取；这一类连网都没上，
    重取一百次也是同一个答案。文案绝不建议 `--beta`——给一只退市的票填贝塔，是把「没有这家公司」读成
    「有这家公司，只是行情缺一段」。
    """


# 步骤名会直接显示在面板上，所以取数层用英文 key、这里翻成人话。
STEP_LABELS = {
    "main_indicators": "主要财务指标",
    "balance_sheet": "资产负债表",
    "income_statement": "利润表",
    "cash_flow": "现金流量表",
    "consensus": "一致预期",
    "quote": "实时行情",
    "dividends": "分红方案（现金分红明细）",
    "risk_free": "10Y 国债收益率序列",
    "price_history": "周频行情与全基准指数（β 回归）",
    "market_returns": "市场腿与股息率（全基准，ERP）",
    "ladder": "构建假设链",
    "replay": "复现既有批次",
}


def describe(step: str) -> str:
    return STEP_LABELS.get(step, step)


def admission(code: str, *, now: datetime | None = None) -> tuple[bool, str, str]:
    """取数前的准入判断：(放行吗, 短句, 全文)。

    名录是唯一证据，它答不了就放行——把「不知道」写成「查无此人」比白跑一次更糟。两种答不了：盘上没有索引、
    索引旧过七天（昨天刚上市的票会被名录漏掉）。

    两句各有用途，别合成一句：短句是「名录里没有它」这一个事实，给面板新鲜度那一行（它只放得下一行）；
    全文还要说清出路（重建名录那条命令），给拒绝和报错。

    `now` 一路传到「名录旧过几天」那一步：这条判断的两侧（索引自己的时间戳与今天）都得能被测试钉住，
    否则写死日期的套件会在跨过午夜的那一刻自己变卦（2026-10-16 就撞到过）。
    """
    verdict, basis = universe.listed(code, now=now)
    if verdict is not False:
        return True, "", basis
    return False, basis, (
        f"{code} 不在当前 A 股名录里（{basis}）：多半已退市，也可能从没上市或代码写错。"
        "取数链路不覆盖退市公司——退市票的行情源根本没有可用序列，联网也只是白跑一遍。"
        "若这只票其实刚上市（名录七天刷新一次，新上市的会晚到），先跑 "
        "python scripts/update_universe.py --force 重建名录，再回来取数。"
    )


def missing_prices_error(source: str, code: str) -> prices.PricesUnavailable:
    """Turn the provenance string that `price_history` returns into an actionable failure.

    The reason is written where it happens (akshare's own exception text, the skipped flag, a batch that
    was fetched before beta existed), so the message can name the fix instead of blaming CAPM.
    """
    if source.startswith("unavailable:"):
        detail = source[len("unavailable:"):].strip()
        if detail.startswith("基准缺序列"):
            # 个股的行情是好的，缺的是指数那一条腿——把「核对 6 位代码」那种建议摆在这儿只会误导。
            return prices.PricesUnavailable(
                f"{code} 的这个基准没有可用的指数收盘序列：{detail}"
                "个股行情本身没问题，缺的是指数那一条腿：换回这批预取过的基准（不联网即可复现），或重新联网取一批。"
            )
        return prices.PricesUnavailable(
            f"{code} 取不到周频行情：{detail}。"
            "代码可能不存在或已退市，也可能是行情源临时故障——先核对 6 位代码，或稍后重新取数。"
        )
    if source.startswith("已跳过"):
        return prices.PricesUnavailable(
            f"{code} 没有 β：{source}，又没有用 --beta 给定。去掉这个开关让机器回归，或显式给一个正数。"
        )
    return prices.PricesUnavailable(
        f"{code} 没有可用的行情快照（{source or '原因未记录'}）。重新联网取数，或用 --beta 显式给定一个正数。"
    )


@dataclass
class BuildOptions:
    code: str
    from_run: str = ""
    no_risk_free: bool = False
    risk_free: float | None = None
    risk_free_basis: str = "norm"
    no_beta: bool = False
    beta: float | None = None
    benchmark: str = prices.DEFAULT_BENCHMARK
    exclude_lease_debt: bool = False
    normalize: bool = True
    terminal_basis: str = "conservative"
    terminal_growth: float | None = None
    horizon: int | None = None


@dataclass
class BuildResult:
    ladder: Any
    store: RunStore
    baseline_path: Path
    manifest_path: Path

    @property
    def run_id(self) -> str:
        return self.store.run_id


def pick_risk_free(window: dict | None, manual: float | None, basis: str) -> tuple[float | None, str]:
    """(级联用的无风险利率, 基准名)。人工值优先；中枢取不出来就退回即期，并把退回原因写进基准名。"""
    if manual is not None:
        return manual, "人工输入 (--risk-free)"
    if not window:
        return None, ""
    spot, mean = window.get("spot"), window.get("mean")
    if basis == "spot":
        return spot, "即期"
    if mean is None:
        return spot, "即期（中枢不可用而退回）"
    return mean, f"近{window.get('window_years')}年中枢"


def build(options: BuildOptions, progress: Progress | None = None) -> BuildResult:
    say: Progress = (lambda step: progress(describe(step))) if progress else (lambda step: None)
    ticker = Ticker(options.code)

    if options.from_run:
        say("replay")
        # 复现只读源批次：证据复制进一个新目录，那一批的 baseline/manifest 不会被这一轮的口径改动碰着。
        store = RunStore.fork(ticker.code, options.from_run)
        snapshot = fundamentals.replay(ticker, store)
        replaying = True
    else:
        # 复现不查名录：盘上那一批是历史证据，今天的名录否决不了昨天真实取到的数（退市票的旧批次照旧可放）。
        allowed, _, denied = admission(ticker.code)
        if not allowed:
            raise TickerUnavailable(denied)
        store = RunStore.create(ticker.code)
        transport = Transport()
        try:
            snapshot = fundamentals.fetch(ticker, store, transport, progress=say)
        finally:
            transport.close()
        replaying = False

    # 无论这批的基准是从联网预取里切的、还是复现时换的，manifest 记的都是「这一批实际按谁算的」。
    store.benchmark = prices.benchmark_code(options.benchmark)

    if options.no_risk_free:
        rf_window, rf_source = None, "unavailable: 已跳过 (--no-risk-free)"
    else:
        say("risk_free")
        rf_window, rf_source = macro.risk_free_stats(store, live=not replaying)
    rf_value, rf_basis = pick_risk_free(rf_window, options.risk_free, options.risk_free_basis)
    if options.risk_free is not None:
        rf_source = "人工输入 (--risk-free)"

    # 市场序列一趟只并发取一次：所有基准的指数收盘加它们的全收益孪生，β 与 ERP 两份凭证各按自己的窗口切。
    # 复现不建这个账本——快照里已经有序列了，联网的活儿一次都不该做。
    series = None if replaying else prices.IndexSeriesMap().load()

    if options.beta is not None:
        # 人工 β 不必再花一次行情取数；raw/ 里没有价格快照本身就是「这步没跑」的痕迹。
        price_payload, price_source = None, ""
    elif options.no_beta:
        price_payload, price_source = None, "已跳过 (--no-beta)"
    else:
        say("price_history")
        price_payload, price_source = prices.price_history(
            store, ticker.secucode, options.benchmark, live=not replaying, series=series
        )

    if price_payload is None and options.beta is None:
        # 「这只票取不到行情」和「β 在算术上不成立」是两回事：前者该核对代码或重取，建议 --beta 反而是误导。
        error = missing_prices_error(price_source, ticker.code)
        store.write_result("build_failed", {"error": str(error), "reason": "price_history_unavailable"})
        store.finish()
        raise error

    say("market_returns")
    # ERP 的无风险腿复用同一份 risk_free 凭证，不另起序列；取不到就当没测，绝不中断建链——
    # 与 β 不同，这一行有 documented 的惯例兜底值（见 assumptions.pick_erp）。
    market_payload, market_source = market.market_history(store, options.benchmark, live=not replaying, series=series)
    erp_stats = (
        market.window_stats(market_payload, store.load(macro.RAW_NAME))
        if market_payload is not None
        else market.empty_stats(market_source)
    )

    say("ladder")
    try:
        ladder = build_ladder(
            snapshot, snapshot["consensus"], snapshot["quote"], rf_value, rf_source,
            include_lease_debt=not options.exclude_lease_debt,
            normalize=options.normalize,
            prices=price_payload,
            prices_source=price_source,
            rf_window=rf_window,
            rf_basis=rf_basis,
            terminal_basis=options.terminal_basis,
            terminal_growth_manual=options.terminal_growth,
            beta_manual=options.beta,
            market_stats=erp_stats,
            market_source=market_source,
            horizon_target=options.horizon,
        )
    except BetaNotApplicable as exc:
        # 折不出发就不写 baseline.json，但取数证据照落盘：留痕要能回答「为什么这只票没有基线」。
        store.write_result("build_failed", {"error": str(exc), "reason": "beta_not_applicable"})
        store.finish()
        raise
    baseline_path = store.write_result("baseline", ladder.to_json())
    return BuildResult(ladder, store, baseline_path, store.finish())
