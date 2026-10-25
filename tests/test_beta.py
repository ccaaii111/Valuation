"""Regression tests for the measured beta, runnable with plain python.

    python tests/test_beta.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.builder import missing_prices_error  # noqa: E402
from backend.data import prices  # noqa: E402
from backend.model import beta as B  # noqa: E402
from backend.model.assumptions import (  # noqa: E402
    BETA_PRACTICE_MIN_WEEKS,
    BetaNotApplicable,
    build_ladder,
    measure_beta,
)
from backend.model.dcf import evaluate, inputs_from_ladder  # noqa: E402
from fixtures import Frame, Spy, synthetic_snapshot  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0

FRIDAY_0 = date(2023, 1, 6)  # an ISO week-1 Friday, so each step lands in its own week
INDEX_RETURNS = [((i * 7919) % 13 - 6) * 0.004 for i in range(160)]


def close(value, expected: float, tol: float = 1e-9) -> bool:
    return value is not None and abs(value - expected) < tol


def closes(returns, base: float = 100.0, step_days: int = 7, start: date = FRIDAY_0) -> dict[str, float]:
    out = {start.isoformat(): base}
    value = base
    for index, ret in enumerate(returns):
        value *= 1.0 + ret
        out[(start + timedelta(days=step_days * (index + 1))).isoformat()] = round(value, 8)
    return out


def price_payload(stock_slope: float = 1.5, weeks: int = 160, benchmark: str = "000905", jitter: float = 0.0) -> dict:
    index = INDEX_RETURNS[:weeks]
    stock = [r * stock_slope + jitter * ((i % 3) - 1) * 0.01 for i, r in enumerate(index)]
    return {
        "source": "test",
        "stock": {"symbol": "sh600028", "closes": closes(stock)},
        "benchmark": {"code": benchmark, "symbol": f"sh{benchmark}", "name": "中证500", "closes": closes(index)},
    }


def test_weekly_bucketing_and_gaps() -> None:
    same_week = {"2023-01-04": 10.0, "2023-01-06": 11.0, "2023-01-13": 12.0}
    weeks = B.weekly_closes(same_week)
    check("同一 ISO 周只留最后一个收盘", len(weeks) == 2 and weeks[sorted(weeks)[0]][1] == 11.0, str(weeks))
    returns = B.period_returns(weeks)
    check("周收益按周末收盘连乘", abs(returns[sorted(weeks)[1]] - (12.0 / 11.0 - 1.0)) < 1e-12, str(returns))
    suspended = B.weekly_closes({"2023-01-06": 10.0, "2023-01-13": 11.0, "2023-02-03": 12.0})
    gap_returns = B.period_returns(suspended)
    check(
        "停牌跨周不记作一个周期收益",
        list(gap_returns) == ["2023-02"] and abs(gap_returns["2023-02"] - 0.1) < 1e-12,
        str(gap_returns),
    )


def test_regress_recovers_the_slope() -> None:
    payload = price_payload(1.5)
    stats = B.regress(payload["stock"]["closes"], payload["benchmark"]["closes"])
    check("斜率复原为 1.5", abs(stats.beta - 1.5) < 1e-9, str(stats.beta))
    check("完全线性时 R²=1", abs(stats.r_squared - 1.0) < 1e-9, str(stats.r_squared))
    check("完全线性时无残差，t 不发散成数字", stats.t_stat is None, str(stats.t_stat))
    check("窗口截到 156 周", stats.observations == B.WINDOW_WEEKS, str(stats.observations))
    inverted = B.regress(closes([r * -1 for r in INDEX_RETURNS]), payload["benchmark"]["closes"])
    check("反向联动的 β 为负", abs(inverted.beta + 1.0) < 1e-9, str(inverted.beta))
    thin = B.regress(closes(INDEX_RETURNS[:2]), closes(INDEX_RETURNS[:2]))
    check("重叠不足 3 周时不编造 β", thin.beta is None and thin.observations == 2, str(thin))
    empty = B.regress({}, {})
    check("空序列返回空统计", empty.beta is None and empty.observations == 0, str(empty))


def test_window_argument_controls_the_sample() -> None:
    payload = price_payload(2.0)
    stats = B.regress(payload["stock"]["closes"], payload["benchmark"]["closes"], window_weeks=52)
    check("窗口参数生效", stats.observations == 52, str(stats.observations))


def test_only_arithmetic_failures_reject_the_measurement() -> None:
    """只有「算术上不成立」抛 BetaNotApplicable；漏传数据是 ValueError，数值不好看则照用。"""
    cases = [
        ("重叠太短", price_payload(1.5, weeks=2), "拟合不出斜率"),
        ("负 β", price_payload(-0.5), "β 为负"),
    ]
    for label, payload, reason in cases:
        try:
            measure_beta(payload)
            check(f"算术不成立要拒绝：{label}", False, "未抛 BetaNotApplicable")
        except BetaNotApplicable as exc:
            # 每条拒绝都得给出路，否则面板上就只剩一个走不通的死胡同。
            check(f"算术不成立要拒绝：{label}", reason in str(exc) and "--beta" in str(exc), str(exc)[:56])

    try:
        measure_beta(None)
        check("漏传数据不算 CAPM 不适用", False, "未抛异常")
    except BetaNotApplicable as exc:
        check("漏传数据不算 CAPM 不适用", False, f"被判成 BetaNotApplicable: {exc}")
    except ValueError as exc:
        check("漏传数据算用法错误（ValueError）", "没有价格快照" in str(exc), str(exc)[:56])

    adopted, evidence = measure_beta(price_payload(1.5))
    check("达标 β 被采纳", close(adopted, 1.5), str(adopted))
    check("证据含 R²/t/窗口", "R²=" in evidence and "t=" in evidence and "156 周" in evidence, evidence)
    check("证据点名基准指数", "中证500(000905)" in evidence, evidence)
    check("证据说明前复权快照", "前复权" in evidence, evidence)
    noisy_value, noisy_evidence = measure_beta(price_payload(1.5, jitter=1.0))
    check("有残差时 t 是有限数值", close(noisy_value, 1.5) and "t=+" in noisy_evidence, noisy_evidence)

    tiny, _ = measure_beta(price_payload(0.05))
    check("β 只有 0.05 也照用", close(tiny, 0.05), str(tiny))
    wild, wild_note = measure_beta(price_payload(4.0))
    check("β=4 越出旧区间仍照用", close(wild, 4.0), f"{wild} {wild_note[:40]}")
    flat, _ = measure_beta(price_payload(0.25))
    check("股权成本贴地不再是拒绝理由", close(flat, 0.25), str(flat))
    short, short_note = measure_beta(price_payload(1.5, weeks=40))
    check("短于实务下限照用但说明", close(short, 1.5, 1e-6) and "实务下限" in short_note, short_note)


def test_ladder_row_flips_from_convention_to_data() -> None:
    def ladder(**kwargs):
        snapshot = synthetic_snapshot()
        return build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, **kwargs)

    measured = next(r for r in ladder(prices=price_payload(1.5), prices_source="akshare sina 行情 @ test").rows() if r.key == "beta")
    check("β 行是数据行", measured.kind == "data", measured.kind)
    check("基线是实测值", close(measured.baseline, 1.5), str(measured.baseline))
    check("来源写明基准", "中证500" in measured.source, measured.source)
    check("取数凭证进证据行", "akshare sina" in measured.evidence, measured.evidence)
    check("说明只讲 β 的作用", "联动强度" in measured.note and "akshare" not in measured.note, measured.note)

    short = next(r for r in ladder(prices=price_payload(1.5, weeks=40)).rows() if r.key == "beta")
    check("短样本的警告写在行上而非改数", close(short.baseline, 1.5) and "实务下限" in short.evidence, short.evidence)

    manual = next(r for r in ladder(beta_manual=0.9).rows() if r.key == "beta")
    check("人工 β 是惯例行", manual.kind == "convention" and close(manual.baseline, 0.9), manual.kind)
    check("人工 β 说明没走回归", "人工输入 (--beta" in manual.evidence and "未走回归" in manual.evidence, manual.evidence)
    check("人工 β 的说明标出取的是人工值", "本行取人工输入" in manual.note, manual.note)

    try:
        ladder()
        check("既无行情又无 --beta 时不出基线", False, "未抛异常")
    except BetaNotApplicable as exc:
        check("既无行情又无 --beta 时不出基线", False, f"被误判成 CAPM 不适用: {exc}")
    except ValueError as exc:
        check("既无行情又无 --beta 时不出基线", "没有价格快照" in str(exc), str(exc)[:56])
    try:
        ladder(beta_manual=-0.5)
        check("人工 β 也必须为正", False, "未抛 BetaNotApplicable")
    except BetaNotApplicable as exc:
        check("人工 β 也必须为正", "也得为正" in str(exc), str(exc)[:56])

    cascade = {r.key: r.baseline for r in ladder(prices=price_payload(1.5)).rows()}
    check("股权成本吃实测 β", abs(cascade["cost_of_equity"] - (0.02 + 1.5 * 0.06)) < 1e-9, str(cascade["cost_of_equity"]))
    pinned = {r.key: r.baseline for r in ladder(beta_manual=0.5).rows()}
    check("实测 β 抬高折现率", cascade["wacc"] > pinned["wacc"], f"{cascade['wacc']} vs {pinned['wacc']}")
    check("实务下限不超过回归窗口", BETA_PRACTICE_MIN_WEEKS <= B.WINDOW_WEEKS, f"{BETA_PRACTICE_MIN_WEEKS} vs {B.WINDOW_WEEKS}")


def test_engine_consumes_measured_beta() -> None:
    snapshot = synthetic_snapshot()

    def inputs(**kwargs):
        ladder = build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, **kwargs)
        return inputs_from_ladder(ladder.to_json())

    high_beta, low_beta = inputs(prices=price_payload(1.5)), inputs(beta_manual=0.5)
    check("β 流入 DCF 输入", abs(high_beta["beta"] - 1.5) < 1e-9, str(high_beta["beta"]))
    rich = evaluate(high_beta, "fcff", "gordon")
    cheap = evaluate(low_beta, "fcff", "gordon")
    check("高 β 压低每股价值", rich.per_share < cheap.per_share, f"{rich.per_share} vs {cheap.per_share}")


def test_missing_price_feed_is_blamed_on_the_feed_not_on_capm() -> None:
    """分类：取不到数 / 被人跳过 / 老批次无快照 → PricesUnavailable；只有 β 本身不成立才是 BetaNotApplicable。"""
    unavailable = missing_prices_error(
        "unavailable: akshare stock_zh_a_daily(sh600001) 取数失败: PricesUnavailable: 在 1250 天窗口内没有有效收盘", "600001"
    )
    check("取数失败属行情不可得", isinstance(unavailable, prices.PricesUnavailable), type(unavailable).__name__)
    check("取数失败带出真实原因", "stock_zh_a_daily" in str(unavailable), str(unavailable)[:60])
    check("取数失败不该叫 CAPM 不适用", "CAPM" not in str(unavailable) and "核对" in str(unavailable), str(unavailable)[-58:])
    check("取数失败不该建议 --beta", "--beta" not in str(unavailable), str(unavailable)[-58:])

    switch = missing_prices_error(
        "unavailable: 基准缺序列：批次 20261014-010101 的凭证里没有 000999（已有 000300, 000905），"
        "换这个基准要联网重取一批",
        "600519",
    )
    check("基准缺序列不怪到个股头上", isinstance(switch, prices.PricesUnavailable) and "退市" not in str(switch), str(switch)[:70])
    check("基准缺序列点名是哪一条腿", "指数" in str(switch) and "000999" in str(switch), str(switch)[-80:])
    check("基准缺序列给两条出路", "换回" in str(switch) and "重新联网" in str(switch), str(switch)[-46:])

    skipped = missing_prices_error("已跳过 (--no-beta)", "600519")
    check("开关跳过的说清是谁跳的", isinstance(skipped, prices.PricesUnavailable) and "--no-beta" in str(skipped), str(skipped)[:56])
    check("跳过时给两条出路", "去掉" in str(skipped) and "--beta" in str(skipped), str(skipped)[-56:])

    replay = missing_prices_error("批次 20260921-101500 未记录行情快照，--from-run 不重新联网", "600519")
    check("无快照的老批次要重新联网", isinstance(replay, prices.PricesUnavailable) and "重新联网" in str(replay), str(replay)[:60])
    check("取数失败三类都不叫 CAPM 不适用", all("CAPM" not in str(err) for err in (unavailable, skipped, replay)), "文案")


def test_sina_symbol_mapping() -> None:
    check("沪市个股加 sh", prices.stock_symbol("600028.SH") == "sh600028", prices.stock_symbol("600028.SH"))
    check("深市个股加 sz", prices.stock_symbol("000001.SZ") == "sz000001", prices.stock_symbol("000001.SZ"))
    check("缺后缀时按首位判所", prices.stock_symbol("600519") == "sh600519", prices.stock_symbol("600519"))
    check("399 指数归深市", prices.index_symbol("399006") == "sz399006", prices.index_symbol("399006"))
    check("中证500 挂沪市", prices.index_symbol("000905") == "sh000905", prices.index_symbol("000905"))
    check("已带前缀不重复加", prices.index_symbol("sh000905") == "sh000905", prices.index_symbol("sh000905"))
    for bad in ("", "ABC", "00090"):
        try:
            prices.index_symbol(bad)
        except ValueError:
            continue
        check(f"非法指数代码要报错: {bad!r}", False, "未抛 ValueError")


def test_the_channel_is_chosen_by_the_code() -> None:
    """932000 在 sina 是一张空表，只能走中证官网；官网又不管深市指数，各走各的门。"""
    check("中证2000 走中证官网", prices.index_channel("932000") == (prices.CSINDEX_FUNC, prices.CSINDEX_URL),
          str(prices.index_channel("932000")))
    check("中证500 走新浪", prices.index_channel("000905") == (prices.INDEX_FUNC, prices.SOURCE_URL),
          str(prices.index_channel("000905")))
    check("创业板指也走新浪", prices.index_channel("399006") == (prices.INDEX_FUNC, prices.SOURCE_URL))
    check("通道标签两条都点名", set(prices.CHANNEL_LABELS) == {prices.INDEX_FUNC, prices.CSINDEX_FUNC},
          str(prices.CHANNEL_LABELS))
    for code in prices.BENCHMARK_NAMES:
        check(f"名单里的基准 {code} 有中文名", bool(prices.BENCHMARK_NAMES.get(code)), str(prices.BENCHMARK_NAMES))


def test_index_closes_routes_and_keeps_the_explicit_window() -> None:
    """中证官网不给 start/end_date 就会把序列凭空停在 2024-06-04，这条必须显式传。"""
    days = [date(2026, 1, 5) + timedelta(days=7 * i) for i in range(10)]
    sina = Frame(["date", "close"], [(d.isoformat(), 100.0 + i) for i, d in enumerate(days)])
    csindex = Frame(["日期", "收盘"], [(d.isoformat(), 200.0 + i) for i, d in enumerate(days)])
    real = prices.fetch_frame
    try:
        spy = Spy({prices.INDEX_FUNC: sina, prices.CSINDEX_FUNC: csindex})
        prices.fetch_frame = spy
        sina_closes = prices.index_closes("000905")
        csindex_closes = prices.index_closes("932000")
    finally:
        prices.fetch_frame = real
    check("两条代码各走自己的门",
          spy.calls == [(prices.INDEX_FUNC, "sh000905"), (prices.CSINDEX_FUNC, "932000")], str(spy.calls))
    check("新浪只按 symbol 取", spy.kwargs[prices.INDEX_FUNC] == {"symbol": "sh000905"}, str(spy.kwargs[prices.INDEX_FUNC]))
    check("官网显式给起止日", {"symbol", "start_date", "end_date"} == set(spy.kwargs[prices.CSINDEX_FUNC]),
          str(spy.kwargs[prices.CSINDEX_FUNC]))
    check("官网按中文列名解析", len(csindex_closes) == 10 and 200.0 in csindex_closes.values(), str(csindex_closes)[:60])
    check("新浪按英文列名解析", len(sina_closes) == 10 and 100.0 in sina_closes.values(), str(sina_closes)[:60])


def test_parse_closes_drops_the_echoed_start_date_row() -> None:
    """中证官网把请求的 startDate 原样回显成首行，值与下一个真实交易日相同——它不能进样本。"""
    echoed = Frame(["日期", "收盘"], [("2026-01-01", 100.0), ("2026-01-02", 100.0), ("2026-01-05", 101.0)])
    popped = prices.parse_closes(echoed, "日期", "收盘", "2026-01-01", echo_day="2026-01-01")
    check("回显的假日被丢掉", list(popped) == ["2026-01-02", "2026-01-05"], str(list(popped)))
    flat = Frame(["日期", "收盘"], [("2026-01-02", 100.0), ("2026-01-05", 100.0), ("2026-01-06", 101.0)])
    check("真实交易日的同值连号不误删",
          len(prices.parse_closes(flat, "日期", "收盘", "2026-01-01", echo_day="2026-01-01")) == 3)
    moving = Frame(["日期", "收盘"], [("2026-01-01", 99.0), ("2026-01-02", 100.0)])
    check("回显日与次日不同值就当真收盘留下",
          len(prices.parse_closes(moving, "日期", "收盘", "2026-01-01", echo_day="2026-01-01")) == 2)
    check("不传 echo_day 就一行不动", len(prices.parse_closes(echoed, "日期", "收盘", "2026-01-01")) == 3)
    check("floor 之前的收盘不进序列",
          list(prices.parse_closes(echoed, "日期", "收盘", "2026-01-05")) == ["2026-01-05"],
          str(prices.parse_closes(echoed, "日期", "收盘", "2026-01-05")))
    try:
        prices.parse_closes(Frame(["日期", "开盘"], [("2026-01-01", 1.0)]), "日期", "收盘", "2026-01-01")
        check("缺列要报取数失败", False, "未抛 PricesUnavailable")
    except prices.PricesUnavailable as exc:
        check("缺列要报取数失败并列出现有列", "缺列" in str(exc) and "开盘" in str(exc), str(exc)[:60])


def test_the_prefetched_book_slices_one_download_per_requesting_window() -> None:
    """预取只下一次长窗口，β 与 ERP 各按自己的窗口在本地切——切不出来的基准要问得出原因。"""
    every = {(date.today() - timedelta(days=step)).isoformat(): 100.0 + step
             for step in range(0, 1500, 3)}

    def load(code: str, window: int) -> dict[str, float]:
        if code == "932000":
            raise prices.PricesUnavailable("stock_zh_index_hist_csindex(932000) 空表")
        return dict(every)

    ledger = prices.IndexSeriesMap(codes=("000905", "932000"), history_days=prices.LONG_HISTORY_DAYS, loader=load)
    ledger.load()
    check("长窗口整份可用", len(ledger.closes("000905")) == len(every), str(len(ledger.closes("000905"))))
    short = ledger.closes("000905", 1250)
    check("β 的窗口在本地切短", 0 < len(short) < len(every), f"{len(short)} vs {len(every)}")
    floor = (date.today() - timedelta(days=1250)).isoformat()
    check("切出来的都落在窗口内", all(day >= floor for day in short), min(short))
    check("落空的那个进 unavailable", "932000" in ledger.unavailable and "932000" not in ledger.available,
          str(ledger.unavailable))
    for code, expect in (("932000", "空表"), ("000906", "没有预取")):
        try:
            ledger.closes(code)
            check(f"{code} 取不到序列即抛", False, "未抛 PricesUnavailable")
        except prices.PricesUnavailable as exc:
            check(f"{code} 抛出的就是原因", expect in str(exc), str(exc)[:60])


def test_the_v8_engine_is_warmed_before_the_pool_opens() -> None:
    """预取的线程池开工前，第一个 JS 引擎上下文得在当前线程建掉——否则进程会偶发当场死在 V8 的 CHECK。

    docs/caliber.md §12 第 16 条：akshare 每次调 `stock_zh_index_daily` 都新造一个 `MiniRacer()`，两个 worker 同时
    争「第一次初始化」就炸（`partition_address_space.cc(243)`，没有 Python traceback，批次只剩 `raw/`）。
    实测（不联网，`py_mini_racer` 单独构造）：不预热 40 个进程死 10 个，先建一次 40 个全活。
    """
    days = [(date.today() - timedelta(days=step)).isoformat() for step in range(1, 400)]
    frame = Frame(["日期", "收盘"], [(day, 1000.0 + index) for index, day in enumerate(days)])
    order: list[str] = []

    def warm_spy() -> None:
        order.append("warm")

    real_warm, real_fetch = prices._warm_v8, prices.fetch_frame
    try:
        prices._warm_v8 = warm_spy
        prices.fetch_frame = lambda func, **kwargs: (order.append(f"fetch:{kwargs.get('symbol')}"), frame)[1]
        prices.IndexSeriesMap(codes=("000905", "932000"), loader=prices.series_loader).load()
    finally:
        prices._warm_v8, prices.fetch_frame = real_warm, real_fetch

    check("引擎只预热一次", order.count("warm") == 1, str(order))
    check("预热排在线程池的所有取数之前", order[0] == "warm" and order[1].startswith("fetch:"), str(order[:3]))


def test_replayed_book_does_not_touch_the_js_engine() -> None:
    """离线复现（注入 loader）不该付 py_mini_racer 的账：那趟根本没有 akshare 调用可保护。"""
    order: list[str] = []
    real_warm = prices._warm_v8
    try:
        prices._warm_v8 = lambda: order.append("warm")
        prices.IndexSeriesMap(codes=("000905",), loader=lambda code, window: {
            date.today().isoformat(): 1000.0
        }).load()
    finally:
        prices._warm_v8 = real_warm
    check("假 loader 不预热引擎", order == [], str(order))


def test_the_price_credential_holds_every_benchmark_and_views_stay_single() -> None:
    """行情凭证按 M4 存全表，交给建链的视图仍是老那一形状；老批次的单基准凭证照读。"""
    days = [(date.today() - timedelta(days=7 * i)).isoformat() for i in range(6)]
    ledger = prices.IndexSeriesMap(codes=("000905", "000906"), loader=lambda code, window: {
        day: 1000.0 + index for index, day in enumerate(days)
    })
    real = prices.fetch_frame
    try:
        prices.fetch_frame = lambda func, **kwargs: Frame(
            ["date", "close"], [(day, 10.0 + index) for index, day in enumerate(days)]
        )
        document = prices.fetch_all("600519.SH", ledger.load(), "000905").payload
    finally:
        prices.fetch_frame = real
    check("凭证记下列表里两家基准", prices.prefetched(document) == ["000905", "000906"], str(prices.prefetched(document)))
    check("used_benchmark 记这次建链用的那个", document["used_benchmark"] == "000905", str(document["used_benchmark"]))
    view = prices.for_benchmark(document, "000906")
    check("视图回到老形状", set(view) >= {"stock", "benchmark"} and "benchmarks" not in view, str(sorted(view)))
    check("视图里的基准是切出来的那个", view["benchmark"]["code"] == "000906", str(view["benchmark"])[:60])
    check("个股序列不随基准换", view["stock"]["symbol"] == "sh600519", str(view["stock"])[:40])
    check("集合外的基准切不出视图", prices.for_benchmark(document, "399006") is None)

    old = {"stock": {"symbol": "sh600519", "closes": {days[0]: 10.0}},
           "benchmark": {"code": "000905", "symbol": "sh000905", "name": "中证500", "closes": {days[0]: 1000.0}}}
    check("老批次原样交回", prices.for_benchmark(old, "000905") is old)
    check("老批次切不出别的基准", prices.for_benchmark(old, "000906") is None)
    check("老批次的预取集合就是那一个", prices.prefetched(old) == ["000905"], str(prices.prefetched(old)))


def test_only_twinless_benchmarks_have_no_total_return_sibling() -> None:
    """ERP 的市场腿靠这张表决定走哪条：默认基准必须有孪生，三家深市/上证指数必须没有。"""
    check("默认基准有全收益孪生", prices.TR_SYMBOLS.get(prices.DEFAULT_BENCHMARK) == "932000CNY010",
          str(prices.TR_SYMBOLS))
    check("默认基准的指数腿走中证官网", prices.index_channel(prices.DEFAULT_BENCHMARK) == (prices.CSINDEX_FUNC, prices.CSINDEX_URL),
          str(prices.index_channel(prices.DEFAULT_BENCHMARK)))
    check("孪生名单里每个代码都在中文名表里", set(prices.TR_SYMBOLS) <= set(prices.BENCHMARK_NAMES),
          str(sorted(set(prices.TR_SYMBOLS) - set(prices.BENCHMARK_NAMES))))
    for code in ("000001", "399001", "399006"):
        check(f"{code} 没有孪生，走降级腿", code not in prices.TR_SYMBOLS, str(prices.TR_SYMBOLS))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
