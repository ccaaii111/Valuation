"""Tests for the measured equity risk premium: 近 3 年 realized 超额进级联，今年/近 5/近 10 年并列.

No network. The legs are hand-built series with exact properties, so the annualization and the window
anchoring can be checked against arithmetic rather than against another run of the same code: the index
level is `base × (1+annual)^(天数/365.25)`, which makes every window's annualized return exactly `annual`;
dividend yield and the 10Y reading are constants.

Two market legs are in scope, and which one runs is a fact of the payload, not a switch: a total-return
twin in `total_return.closes` replaces the price+dividend leg (`market_leg`), and its absence — or a twin
fetch that failed — falls back to 乐咕. The fallback path is what the fixture without a `total_return`
key builds, so the arithmetic tests below keep exercising it while the twin tests add the other one.

    python tests/test_erp.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.data import macro, market, prices  # noqa: E402
from backend.model.assumptions import (  # noqa: E402
    ERP,
    ERP_CASCADE_WINDOW,
    ERP_FALLBACK_MARK,
    ERP_RANGE,
    ERP_REFERENCE_WINDOWS,
    ERP_WINDOW_LABELS,
    ERP_WINDOW_ORDER,
    build_ladder,
    erp_rows,
    erp_window_choice,
    erp_window_options,
    erp_warnings,
    erp_warnings_for_window,
    pick_erp,
)
from backend.model.attribution import attribute  # noqa: E402
from backend.model.dcf import ENGINE_INPUTS, inputs_from_ladder  # noqa: E402
from backend.model.derivation import FORMULAS  # noqa: E402
from fixtures import Frame, Spy, synthetic_snapshot  # noqa: E402

FAILURES = 0
END = date(2026, 6, 30)  # 周二；所有窗口的终点都锚在这一天
ORIGIN = date(2015, 1, 1)
RF = 2.0  # 百分数口径的 10Y 国债读数
DY = 3.0  # 百分数口径的股息率


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def close(a: float | None, b: float, tol: float = 1e-9) -> bool:
    return a is not None and abs(a - b) < tol


def weekdays(start: date, end: date, every: int = 1) -> list[date]:
    days, index = [], 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            if index % every == 0:
                days.append(day)
            index += 1
        day += timedelta(days=1)
    return days


def level(segments: list[tuple[date, float]], day: date) -> float:
    """分段连续复利到该日的指数点位：`segments` 是 (起始日, 年化率)，首日必须是 ORIGIN。"""
    factor, bounds = 1.0, [start for start, _ in segments] + [day]
    for index, (start, rate) in enumerate(segments):
        end = min(bounds[index + 1], day)
        if end > start:
            factor *= (1.0 + rate) ** ((end - start).days / 365.25)
    return 1000.0 * factor


def payload(annual: float = 0.10, switches: list[str] | None = None,
            dividend: float = DY, rf_value: float = RF, rf_from: date | None = None,
            every: int = 1, dividend_markets: dict | None = None,
            index_until: date | None = None, twin: str = "H00905",
            tr_annual: float | None = None, tr_unavailable: str = "") -> tuple[dict, dict]:
    """(market_returns 快照, risk_free 快照)——指数按日连续复利上涨，分红与国债恒定为百分数。

    `switches` 里的每个日期就是「从这天起指数不再上涨」的切换点，用来让不同窗口测出不同的数。
    `tr_annual` 才挂上全收益腿（另一条连续复利序列，起点不同、年化独立）；给 `tr_unavailable` 则是
    「这个基准有孪生，但那次取数没成功」——市场腿退回价格 + 股息率，原因留在凭证里。两者都不给时
    载荷里根本没有 `total_return` 键，走的就是无孪生基准那条降级路。
    """
    segments = [(ORIGIN, annual)] + [(date.fromisoformat(day), 0.0) for day in sorted(switches or [])]
    days = weekdays(ORIGIN, index_until or END, every)
    closes = {day.isoformat(): level(segments, day) for day in days}
    markets = dividend_markets if dividend_markets is not None else {
        "上证A股": {day.isoformat(): dividend for day in weekdays(ORIGIN, END)},
        "深证A股": {day.isoformat(): dividend for day in weekdays(ORIGIN, END)},
    }
    rf_rows_payload = [
        {"日期": day.isoformat(), "中国国债收益率10年": rf_value}
        for day in weekdays(rf_from or ORIGIN, END)
    ]
    document = {
        "source": "test",
        "benchmark": {"code": "000905", "symbol": "sh000905", "name": "中证500", "closes": closes},
        "dividend": {"markets": markets},
    }
    if tr_annual is not None:
        document["total_return"] = {
            "source": f"akshare {prices.CSINDEX_FUNC}",
            "twin": twin,
            "closes": {day.isoformat(): 2.0 * level([(ORIGIN, tr_annual)], day) for day in days},
        }
    elif tr_unavailable:
        document["total_return"] = {"twin": twin, "unavailable": tr_unavailable}
    return document, {"rows": rf_rows_payload}


def stats(**kwargs) -> dict:
    document, rf = payload(**kwargs)
    return market.window_stats(document, rf)


def rows_of(document: dict) -> list[dict]:
    """归因报告把行摊平在顶层，假设链放在组里：两处都是同一批行，读法不同。"""
    return document.get("rows") or [row for group in document["groups"] for row in group["rows"]]


def row_of(document: dict, key: str) -> dict:
    return next(row for row in rows_of(document) if row["key"] == key)


def ladder(**kwargs) -> dict:
    snapshot = synthetic_snapshot()
    kwargs.setdefault("beta_manual", 1.00)
    return json.loads(json.dumps(build_ladder(
        snapshot, snapshot["consensus"], snapshot["quote"], 0.02, **kwargs
    ).to_json(), ensure_ascii=False))


# ---------------------------------------------------------------- 取数层的窗口算术

def test_a_steady_market_gives_the_same_erp_in_every_window() -> None:
    result = stats()
    check("四个窗口都测算成功", all(not w["unavailable"] for w in result["windows"].values()),
          str({k: v["unavailable"] for k, v in result["windows"].items() if v["unavailable"]}))
    for label, window in result["windows"].items():
        check(f"{label} 年化涨幅就是 10%", close(window["price_annualized"], 0.10, 1e-6),
              str(window["price_annualized"]))
        # 0.10 价格 + 0.03 分红 − 0.02 国债 = 0.11
        check(f"{label} 超额＝总收益减国债", close(window["erp"], 0.11, 1e-6), str(window["erp"]))
        check(f"{label} 记下分红腿与国债腿", close(window["dividend_yield_mean"], 0.03, 1e-9)
              and close(window["risk_free_mean"], 0.02, 1e-9), str(window))
        check(f"{label} 不含股息的超额更低", close(window["erp_price_only"], 0.08, 1e-6),
              str(window["erp_price_only"]))


def test_the_base_is_the_last_close_before_the_window() -> None:
    result = stats()["windows"]["ytd"]
    check("今年的基数是去年最后一个收盘", result["base_day"] == "2025-12-31", str(result["base_day"]))
    check("终点就是序列末日", result["last_day"] == END.isoformat(), str(result["last_day"]))
    ten = stats()["windows"]["10y"]
    check("十年窗口的跨度接近十年", 9.8 <= ten["span_years"] <= 10.1, str(ten["span_years"]))
    check("跨度越长的窗口收盘数越多", ten["observations"] > stats()["windows"]["3y"]["observations"])


def test_windows_separate_when_the_market_stopped_rising() -> None:
    result = stats(switches=["2023-01-02"])["windows"]
    check("近 3 年只看到横盘", close(result["3y"]["erp"], 0.01, 1e-6), str(result["3y"]["erp"]))
    check("近 10 年仍带着早年的涨幅", result["10y"]["erp"] > result["3y"]["erp"] + 0.02,
          f"{result['10y']['erp']} vs {result['3y']['erp']}")


def test_the_anchor_is_the_last_common_day_not_today() -> None:
    document, rf = payload(index_until=date(2026, 7, 10))
    result = market.window_stats(document, rf)["windows"]["ytd"]
    check("分红序列落后时窗口终点跟着退", result["last_day"] == END.isoformat(), str(result["last_day"]))


def test_thin_or_impossible_legs_refuse_to_produce_a_number() -> None:
    short_rf = stats(rf_from=date(2026, 4, 1))["windows"]
    check("国债腿太薄时五年窗口不出数", "国债收益率" in short_rf["5y"]["unavailable"],
          short_rf["5y"]["unavailable"])
    sparse = stats(every=30)["windows"]
    check("收盘太稀时窗口不出数", "低于每年" in sparse["5y"]["unavailable"], sparse["5y"]["unavailable"])
    broken, rf = payload(dividend_markets={"上证A股": {"error": "legulegu 502"}, "深证A股": {"error": "legulegu 502"}})
    result = market.window_stats(broken, rf)
    check("两市分红全无答复时整份测算作废", "股息率" in result["windows"]["5y"]["unavailable"],
          result["unavailable"] or result["windows"]["5y"]["unavailable"])
    fat = stats(dividend=15.0)["windows"]
    check("股息率单位跳变被拒", "单位疑似已改变" in fat["5y"]["unavailable"], fat["5y"]["unavailable"])
    jump = stats(rf_value=300.0)["windows"]
    check("国债读数跳变被拒", "单位疑似已改变" in jump["5y"]["unavailable"], jump["5y"]["unavailable"])
    empty = market.window_stats(None, None)
    check("没有快照时每个窗口都带同一句原因",
          all(w["unavailable"] for w in empty["windows"].values()) and not empty["windows"]["5y"]["erp"])
    no_rf = market.window_stats(payload()[0], None)
    check("缺 risk_free 凭证时不猜无风险腿", "risk_free" in no_rf["unavailable"], no_rf["unavailable"])


def test_the_rf_snapshot_must_reach_the_longest_window() -> None:
    """两条腿来自两次取数，但国债快照必须比 ERP 最长窗口长——否则十年那档只是拿半截序列求均值。"""
    check("国债窗口覆盖十一年", macro.WINDOW_DAYS >= round(11 * 365.25), str(macro.WINDOW_DAYS))
    check("市场窗口与国债窗口同长", market.HISTORY_DAYS == macro.WINDOW_DAYS,
          f"{market.HISTORY_DAYS} vs {macro.WINDOW_DAYS}")
    check("十年窗口的国债读数够密", stats()["windows"]["10y"]["risk_free_observations"]
          >= market.MIN_RISK_FREE_OBSERVATIONS * 9.8, str(stats()["windows"]["10y"]["risk_free_observations"]))


# ---------------------------------------------------------------- 级联择一与护栏

def test_pick_erp_takes_the_three_year_window_and_nothing_else() -> None:
    result = stats()
    value, basis, measured, reason = pick_erp(result)
    check("级联取近 3 年", close(value, result["windows"][ERP_CASCADE_WINDOW]["erp"], 1e-12), str(value))
    check("基准名说清窗口与口径", measured and ERP_WINDOW_LABELS[ERP_CASCADE_WINDOW] in basis and "realized" in basis, basis)
    check("测得出来就没有退回原因", reason == "", reason)


def test_pick_erp_falls_back_and_names_the_rejected_measure() -> None:
    value, basis, measured, reason = pick_erp(None)
    check("没有测算时退回惯例值", value == ERP and not measured and "惯例" in basis, f"{value} {basis}")
    check("退回落空时也给出原因", reason == "近 3 年没有测算", reason)
    for forged, needle in ((-0.02, "越出"), (0.20, "越出")):
        stats_dict = {"windows": {ERP_CASCADE_WINDOW: {"erp": forged}}}
        value, basis, measured, reason = pick_erp(stats_dict)
        check(f"实测 {forged:+.0%} 被护栏拒掉", value == ERP and not measured and needle in basis, basis)
        check(f"越界的原因就是那句 {forged:+.0%}", needle in reason and f"{forged:+.2%}" in reason, reason)
    unavailable = {"windows": {ERP_CASCADE_WINDOW: {"erp": 0.05, "unavailable": "序列太短"}}}
    value, basis, measured, reason = pick_erp(unavailable)
    check("窗口自带不可用原因时不用它的数", value == ERP and not measured and "实测不可用" in basis, basis)
    check("不可用的原文进了原因", "序列太短" in reason, reason)
    check("护栏下界就是 0%（负超额不是风险定价）", ERP_RANGE == (0.0, 0.15), str(ERP_RANGE))


# ---------------------------------------------------------------- 全收益腿：有孪生时股息率整条腿消失

def test_a_total_return_twin_replaces_the_dividend_leg() -> None:
    result = stats(tr_annual=0.08)
    check("有孪生就记在总答复上", result["total_return"]["twin"] == "H00905", str(result["total_return"]))
    window = result["windows"][ERP_CASCADE_WINDOW]
    check("市场腿改口为全收益", window["market_leg"] == "total_return", window["market_leg"])
    # 孪生年化 8% − 国债 2%；与价格指数那 10% 无关
    check("超额按全收益算", close(window["erp"], 0.06, 1e-6), str(window["erp"]))
    check("总收益就是孪生自己的年化", close(window["total_return_annualized"], 0.08, 1e-6),
          str(window["total_return_annualized"]))
    check("股息率这一腿整个消失", window["dividend_yield_mean"] is None and window["dividend_observations"] == 0,
          str(window["dividend_markets_missing"]))
    check("价格指数留着只做对照", close(window["price_annualized"], 0.10, 1e-6), str(window["price_annualized"]))
    check("不含股息的对照仍按价格指数", close(window["erp_price_only"], 0.08, 1e-6), str(window["erp_price_only"]))
    check("全收益的样本数与窗口同数", window["total_return_observations"] == window["observations"],
          f"{window['total_return_observations']} vs {window['observations']}")
    base_day = date.fromisoformat(window["base_day"])
    check("基数取的是孪生序列", close(window["total_return_base"], 2.0 * level([(ORIGIN, 0.08)], base_day), 1e-9),
          str(window["total_return_base"]))
    check("两套点位各归各的序列", close(window["close_base"], level([(ORIGIN, 0.10)], base_day), 1e-9),
          str(window["close_base"]))
    check("没有孪生时走价格 + 股息率", stats()["windows"][ERP_CASCADE_WINDOW]["market_leg"] == "price_plus_dividend")


def test_a_failed_twin_falls_back_to_the_dividend_leg_in_the_numbers() -> None:
    result = stats(tr_unavailable="stock_zh_index_hist_csindex(H00905) 在 4018 天窗口内没有有效收盘")
    window = result["windows"][ERP_CASCADE_WINDOW]
    check("孪生取空时市场腿退回价格 + 股息率", window["market_leg"] == "price_plus_dividend", window["market_leg"])
    check("退回后的超额就是价格 + 分红 − 国债", close(window["erp"], 0.11, 1e-6), str(window["erp"]))
    check("孪生代码与失败原因都留在总答复里",
          result["total_return"]["twin"] == "H00905" and "没有有效收盘" in result["total_return"]["unavailable"],
          str(result["total_return"]))
    value, basis, measured, reason = pick_erp(result)
    check("降级路测出来的数照进级联", measured and close(value, 0.11, 1e-9), f"{value} {basis} {reason}")
    warnings = erp_warnings(result, measured, reason)
    check("顶层只警告一次孪生", len(warnings) == 1, str(warnings))
    check("警告点出没用上的孪生", "H00905" in warnings[0], warnings[0])
    check("警告交代分红腿退回全市场口径",
          "价格指数 + 沪深两市股息率" in warnings[0] and "全市场口径" in warnings[0], warnings[0])


# ---------------------------------------------------------------- 取数层：一趟预取全部基准

def spy_on(**overrides) -> tuple[Spy, list[date]]:
    """三条腿的替身答复：价格/全收益共用同一批交易日，股息率恒定 3%。"""
    days = weekdays(date.today() - timedelta(days=70), date.today())
    answers = {
        prices.INDEX_FUNC: Frame(["date", "close"], [(d.isoformat(), 1000.0 + i) for i, d in enumerate(days)]),
        prices.CSINDEX_FUNC: Frame(["日期", "收盘"], [(d.isoformat(), 2000.0 + i) for i, d in enumerate(days)]),
        market.DIVIDEND_FUNC: Frame(["日期", "股息率"], [(d.isoformat(), 3.0) for d in days]),
    }
    answers.update(overrides)
    return Spy(answers), days


def fetched_with(**overrides) -> tuple[dict, list[tuple[str, str]]]:
    """用替身跑一趟预取，交回整份市场腿凭证与调用记录。真函数在 finally 里归位。"""
    real = prices.fetch_frame
    try:
        spy, _ = spy_on(**overrides)
        prices.fetch_frame = spy
        document = market.fetch_all(prices.IndexSeriesMap().load()).payload
    finally:
        prices.fetch_frame = real
    return document, spy.calls


def test_one_build_prefetches_every_benchmark() -> None:
    """换基准不再回线上，前提是所有基准的序列都在同一份凭证里——这里钉住「都到了」。"""
    document, calls = fetched_with()
    symbols = {symbol for func, symbol in calls}
    check("七个基准都进了预取集合", market.prefetched(document) == sorted(prices.BENCHMARK_NAMES),
          str(market.prefetched(document)))
    check("新浪那六家各取一次", {f"sh{code}" for code in prices.BENCHMARK_NAMES if code not in prices.CSINDEX_CODES and not code.startswith("399")}
          <= symbols, str(sorted(symbols)))
    check("深市指数走 sz 前缀", "sz399001" in symbols and "sz399006" in symbols, str(sorted(symbols)))
    check("四个全收益孪生都走中证官网", {prices.TR_SYMBOLS[code] for code in prices.TR_SYMBOLS} <= symbols,
          str(sorted(symbols)))
    check("乐咕两市一趟只取一次", [symbol for func, symbol in calls if func == market.DIVIDEND_FUNC] == list(market.DIVIDEND_MARKETS),
          str(calls))
    check("出处串把三条腿都点名", market.DIVIDEND_FUNC in document["source"] and prices.CSINDEX_FUNC in document["source"],
          document["source"])


def test_the_view_handed_downstream_still_says_which_leg_it_used() -> None:
    """下游只认单基准那一份形状：切出来的视图必须把「这一档用的是全收益还是价格+股息率」照原样说清。"""
    document, _ = fetched_with()
    twin_leg = market.for_benchmark(document, "000905")
    check("视图只带一个 benchmark", twin_leg["benchmark"]["code"] == "000905" and "benchmarks" not in twin_leg,
          str(sorted(twin_leg)))
    check("有孪生那档：凭证写明未取股息率", "未取乐咕股息率" in (twin_leg["dividend"].get("skipped") or ""),
          str(twin_leg["dividend"]))
    check("有孪生那档：全收益序列在视图里", len(twin_leg["total_return"]["closes"]) > 40,
          str(twin_leg["total_return"])[:80])
    downgraded = market.for_benchmark(document, "399006")
    check("无孪生那档：孪生缺席写清原因", downgraded["total_return"]["twin"] is None
          and "没有全收益孪生" in downgraded["total_return"]["unavailable"], str(downgraded["total_return"]))
    check("无孪生那档：两市股息率都在", set(downgraded["dividend"]["markets"]) == set(market.DIVIDEND_MARKETS),
          str(list(downgraded["dividend"]["markets"])))
    check("没预取到的基准切不出视图", market.for_benchmark(document, "000999") is None)
    check("这份视图能直接喂 window_stats", market.window_stats(downgraded, {"rows": []})["windows"], "")
    old = {"benchmark": {"code": "000905", "symbol": "sh000905", "name": "中证500", "closes": {"2026-01-02": 1.0}},
           "total_return": {"twin": "H00905", "closes": {"2026-01-02": 2.0}},
           "dividend": {"skipped": "市场腿用 H00905 全收益指数，分红已含在点位里，未取乐咕股息率"}}
    check("老批次的单基准凭证原样交回", market.for_benchmark(old, "000905") is old
          and market.for_benchmark(old, "000906") is None, str(market.prefetched(old)))
    check("老批次的预取集合就是那一个", market.prefetched(old) == ["000905"], str(market.prefetched(old)))


def test_a_failed_twin_fetch_still_reaches_the_dividend_leg() -> None:
    """中证官网整条通道回空表：孪生全落空、932000 不进预取集合，其余六个基准照常可用。"""
    document, calls = fetched_with(**{prices.CSINDEX_FUNC: Frame(["日期", "收盘"], [])})
    view = market.for_benchmark(document, "000906")
    check("孪生代码留着，警告才点得出名", view["total_return"]["twin"] == "H00906", str(view["total_return"]))
    check("取数失败原文进凭证且不重念前缀",
          "没有有效收盘" in view["total_return"]["unavailable"]
          and view["total_return"]["unavailable"].count(prices.CSINDEX_FUNC) == 1,
          view["total_return"]["unavailable"])
    check("降级后不再谎称未取股息率", "skipped" not in view["dividend"], str(view["dividend"])[:80])
    check("孪生落空时乐咕照取补上那条腿", [symbol for func, symbol in calls if func == market.DIVIDEND_FUNC]
          == list(market.DIVIDEND_MARKETS), str(calls))
    check("价格指数那一腿没被牵连", len(view["benchmark"]["closes"]) > 40, str(view["benchmark"])[:60])
    check("走官网的 932000 整个不进预取集合", "932000" not in market.prefetched(document),
          str(market.prefetched(document)))
    check("它落在 unavailable 里，原因照写", "932000" in document["unavailable"], str(document["unavailable"]))


# ---------------------------------------------------------------- 假设链上的四行

def test_the_ladder_shares_one_cascade_row_and_three_reference_rows() -> None:
    rows = row_of(ladder(market_stats=stats(), market_source="akshare @ 2026-10-13"), "erp")
    check("级联行由实测驱动", rows["kind"] == "data" and close(rows["baseline"], 0.11, 1e-6), str(rows["baseline"]))
    check("标签写明级联采用", "级联采用" in rows["label"], rows["label"])
    check("凭证交代分红腿的口径杂质", "沪深两市" in rows["evidence"] and "成分的分红" in rows["evidence"], rows["evidence"])
    check("凭证给出不含股息的那个数", "只按价格涨幅算的超额" in rows["evidence"], rows["evidence"][-160:])
    check("凭证记下取数出处", "akshare @ 2026-10-13" in rows["evidence"], rows["evidence"][-60:])
    check("说明只讲溢价的作用，不复述算式", "%" not in rows["note"] and "股权成本" in rows["note"], rows["note"])
    check("级联档就是近 3 年，参考档是另外三档",
          ERP_CASCADE_WINDOW == "3y" and ERP_REFERENCE_WINDOWS == ("ytd", "5y", "10y"),
          str((ERP_CASCADE_WINDOW, ERP_REFERENCE_WINDOWS)))
    for window in ERP_REFERENCE_WINDOWS:
        key = f"erp_{window}"
        row = row_of(ladder(market_stats=stats(), market_source=""), key)
        check(f"{key} 不进引擎输入", key not in ENGINE_INPUTS and "不进级联" in row["note"], row["note"][-80:])
        check(f"{key} 就是 {window} 窗口的测算", close(row["baseline"], stats()["windows"][window]["erp"], 1e-6),
              str(row["baseline"]))
        check(f"{key} 没有上游", row["derives"] == [], str(row["derives"]))
    check("级联行仍是引擎输入",
          "erp" in ENGINE_INPUTS and not any(f"erp_{window}" in ENGINE_INPUTS for window in ERP_REFERENCE_WINDOWS))


def test_the_ladder_reads_the_total_return_leg() -> None:
    row = row_of(ladder(market_stats=stats(tr_annual=0.08), market_source="csindex @ 2026-10-13"), "erp")
    check("级联行由全收益驱动", row["kind"] == "data" and close(row["baseline"], 0.06, 1e-6), str(row["baseline"]))
    check("来源写明「全收益指数 − 同窗口 10Y 国债」", "全收益指数 − 同窗口 10Y 国债" in row["source"], row["source"])
    check("来源点名的基准就是中证500", "中证500" in row["source"], row["source"])
    check("凭证报出孪生代码", "中证500 的全收益指数 H00905" in row["evidence"], row["evidence"][:220])
    check("凭证交代税前口径", "税前" in row["evidence"] and "红利税" in row["evidence"], row["evidence"])
    check("全收益时不再道歉说分红腿是全市场", "沪深两市" not in row["evidence"], row["evidence"])
    check("凭证里的算术按全收益讲", "全收益指数累计" in row["evidence"] and "年化总收益 +8.00%" in row["evidence"],
          row["evidence"][:200])
    check("不含股息的对照数照给", "只按价格涨幅算的超额是 +8.00%" in row["evidence"], row["evidence"][-260:])
    check("取数出处写在凭证末尾", "csindex @ 2026-10-13" in row["evidence"], row["evidence"][-60:])
    check("说明里不出现任何百分数", "%" not in row["note"], row["note"])
    check("测得出来时顶层不留警告", ladder(market_stats=stats(tr_annual=0.08))["warnings"] == [],
          str(ladder(market_stats=stats(tr_annual=0.08))["warnings"]))


def test_the_ladder_apologizes_when_the_leg_is_the_fallback() -> None:
    document = ladder(market_stats=stats(tr_unavailable="stock_zh_index_hist_csindex(H00905) 空表"))
    row = row_of(document, "erp")
    check("降级行的来源回到价格 + 股息率", "收盘涨幅 + 沪深两市股息率" in row["source"], row["source"])
    check("降级行的凭证重新交代全市场口径", "不是 中证500 成分的分红" in row["evidence"], row["evidence"][:260])
    check("孪生失败这一条上了顶层", len(document["warnings"]) == 1 and "H00905" in document["warnings"][0],
          str(document["warnings"]))
    check("孪生取到时无孪生基准不报警", ladder(market_stats=stats())["warnings"] == [],
          str(ladder(market_stats=stats())["warnings"]))


def test_a_negative_premium_falls_back_to_the_convention_and_warns() -> None:
    document = ladder(market_stats=stats(annual=0.10, tr_annual=-0.05))
    row = row_of(document, "erp")
    check("负超额不进级联", row["baseline"] == ERP and row["kind"] == "convention", str(row["baseline"]))
    check("顶层只有一条警告（孪生取到了就不重复报）", len(document["warnings"]) == 1, str(document["warnings"]))
    text = document["warnings"][0]
    check("警告报出被拒的实测值", "-7.00%" in text, text)
    check("警告说清折现率与市场脱钩", "脱钩" in text, text)
    check("警告把出路指到下拉", "换窗口" in text and "换市场基准" in text and "下拉" in text, text)
    check("行说明记下退到了哪一档", "本行采用「惯例值」" in row["note"], row["note"])
    check("被拒的那个数留在凭证里", "惯例 6.0%" in row["evidence"] and "-7.00%" in row["evidence"], row["evidence"][:220])
    check("被拒的实测仍摆在参考行里", close(row_of(document, "erp_ytd")["baseline"], -0.07, 1e-6),
          str(row_of(document, "erp_ytd")["baseline"]))


def test_the_ladder_without_a_market_snapshot_keeps_the_convention_value() -> None:
    document = ladder()
    rows = row_of(document, "erp")
    ytd = row_of(document, "erp_ytd")
    check("无快照时级联行是惯例值", rows["kind"] == "convention" and rows["baseline"] == ERP, str(rows["baseline"]))
    check("惯例值也照摆三行参考", ytd["baseline"] is None and "缺少市场收益率快照" in ytd["evidence"], ytd["evidence"])
    check("惯例行仍写着常用区间中值", "A股常用区间中值" in rows["source"], rows["source"])
    check("连快照都没有时顶层必须报警", len(document["warnings"]) == 1 and "近 3 年没有测算" in document["warnings"][0],
          str(document["warnings"]))
    check("这种场合不谎报孪生", not any("H00905" in w for w in document["warnings"]), str(document["warnings"]))


def test_the_measured_premium_reaches_the_discount_rate() -> None:
    document = ladder(market_stats=stats())
    inputs = inputs_from_ladder(document)
    check("CAPM 用的是实测 ERP", close(inputs["cost_of_equity"], 0.02 + 1.00 * 0.11, 1e-6),
          str(inputs["cost_of_equity"]))
    fallback = inputs_from_ladder(ladder())
    check("没测到时 CAPM 用惯例值", close(fallback["cost_of_equity"], 0.02 + 1.00 * ERP, 1e-6),
          str(fallback["cost_of_equity"]))


def test_attribution_prices_the_cascade_row_only() -> None:
    document = ladder(market_stats=stats())
    report = attribute(document, {"erp": 0.06})
    cascade = row_of(report, "erp")
    reference = row_of(report, "erp_ytd")
    check("改级联行被定价", cascade["editable"] and cascade["changed"] and cascade["contribution_pct"] is not None,
          str(cascade["contribution_pct"]))
    check("改参考行不定价", not reference["editable"] and not reference["changed"], str(reference["current"]))
    check("退回基线仍可归因", cascade["value_if_reverted"] is not None and cascade["contribution_pct"] is not None,
          f"{cascade['value_if_reverted']} {cascade['contribution_pct']}")
    check("基线的高 ERP 折出更低的每股价值", cascade["value_if_reverted"] < report["per_share"],
          f"{cascade['value_if_reverted']} < {report['per_share']}")


# ---------------------------------------------------------------- 「ERP 窗口」下拉：选项就是那四行实测

# 稳态市场量不出「不同窗口给出不同数」，所以这里让指数在 2026 年停涨：四档就此分开。
# 非同数不可测——四档同数时，靠数反回当前选中项只会落到第一个匹配，页面分不清点的到底是哪一档。
SPREAD = {"switches": ["2026-01-01"]}


def test_the_dropdown_offers_every_window_row_in_order() -> None:
    document = ladder(market_stats=stats(**SPREAD))
    options = erp_window_options(rows_of(document))
    check("四档齐全，从短到长", [o["window"] for o in options] == list(ERP_WINDOW_ORDER), str(options))
    check("标签取自级联同一张表", [o["label"] for o in options] == [ERP_WINDOW_LABELS[w] for w in ERP_WINDOW_ORDER],
          str([o["label"] for o in options]))
    check("只有级联那一档标着 cascade", [o["cascade"] for o in options] == [False, True, False, False],
          str([o["cascade"] for o in options]))
    check("每档认领自己那一行", [o["row"] for o in options] == ["erp_ytd", "erp", "erp_5y", "erp_10y"],
          str([o["row"] for o in options]))
    check("选项的值就是那一行的机器基线",
          all(o["value"] == row_of(document, o["row"])["baseline"] for o in options),
          str([(o["row"], o["value"], row_of(document, o["row"])["baseline"]) for o in options]))
    check("四个窗口给出四个不同的数", len({o["value"] for o in options}) == 4, str([o["value"] for o in options]))
    check("都落在可用区间里，所以都可选", all(o["usable"] for o in options), str([(o["window"], o["value"]) for o in options]))


def test_the_choice_is_read_back_from_the_number() -> None:
    options = erp_window_options(rows_of(ladder(market_stats=stats(**SPREAD))))
    for option in options:
        back = erp_window_choice(options, option["value"])
        check(f"{option['label']} 的实测数反回它自己", back is not None and back["window"] == option["window"],
              str(back))
    check("没有修正就是没有选中", erp_window_choice(options, None) is None)
    check("手改的数对不上任何一档", erp_window_choice(options, 0.0555) is None)
    check("从 manual.json 读回的数字串也认",
          erp_window_choice(options, str(next(o for o in options if o["cascade"])["value"]))["window"]
          == ERP_CASCADE_WINDOW)
    check("空串不是 0", erp_window_choice(options, "") is None)


def test_a_negative_window_is_never_offered() -> None:
    # 全收益腿年化 −5%、国债 2% ⇒ 四个窗口都是 −7.00%，级联退回惯例 6%
    document = ladder(market_stats=stats(annual=0.10, tr_annual=-0.05))
    options = erp_window_options(rows_of(document))
    check("负超额一档都不给选", not any(o["usable"] for o in options), str([(o["window"], o["value"]) for o in options]))
    check("被拒的实测仍然摆着（看得见，选不中）",
          all(o["value"] == row_of(document, o["row"])["baseline"] for o in options)
          and close(row_of(document, "erp_ytd")["baseline"], -0.07, 1e-6), str(options))
    cascade_option = next(o for o in options if o["cascade"])
    check("级联行是惯例值，所以近 3 年这一档也不可选",
          cascade_option["value"] == ERP and row_of(document, "erp")["kind"] == "convention"
          and not cascade_option["usable"], str(cascade_option))
    check("惯例 6% 反不出任何窗口", erp_window_choice(options, ERP) is None and erp_window_choice(options, -0.07) is None)


def test_a_premium_above_the_ceiling_is_refused_too() -> None:
    document = ladder(market_stats=stats(annual=0.20))  # 20% 价格 + 3% 分红 − 2% 国债 = 21%，高于上界 15%
    options = erp_window_options(rows_of(document))
    check("越过上界的窗口同样不给选", not any(o["usable"] for o in options), str(options))
    check("三档参考行都是 +21.00%", all(close(o["value"], 0.21, 1e-6) for o in options if not o["cascade"]),
          str([o["value"] for o in options]))
    check("级联档带着惯例值，被拒的那次实测留在参考行里",
          next(o for o in options if o["cascade"])["value"] == ERP
          and close(row_of(document, "erp_10y")["baseline"], 0.21, 1e-6),
          str(row_of(document, "erp")["baseline"]))
    check("级联行因此也是惯例值", row_of(document, "erp")["kind"] == "convention", str(row_of(document, "erp")["baseline"]))
    check("链上没有 ERP 行时不给选项", erp_window_options([{"key": "beta", "baseline": 1.0}]) == [])


def test_one_positive_window_is_the_only_choice() -> None:
    """级联档自己越界、只有一档参考行在区间内——页面会遇到的一种形状。

    三行参考数取自 2026-10-14 那批的 932000（今年 −10.58%、近 5 年 +3.51%、近 10 年 −2.07%），
    级联那一档换成「实测被护栏拒掉、行上是惯例值」。七家基准当下的近 3 年全在区间内，所以这个形状
    暂时不会由默认口径产生；这里量的是机器——下拉只给那一档、顶层警告跟着那一档改口。
    """
    rows = [
        {"key": "erp", "baseline": ERP, "kind": "convention"},
        {"key": "erp_ytd", "baseline": -0.1058, "kind": "data"},
        {"key": "erp_5y", "baseline": 0.0351, "kind": "data"},
        {"key": "erp_10y", "baseline": -0.0207, "kind": "data"},
    ]
    options = erp_window_options(rows)
    check("四档都在，只有一档可选", [o["window"] for o in options if o["usable"]] == ["5y"], str(options))
    check("那一个数就是当前选中项", erp_window_choice(options, 0.0351) is not None
          and erp_window_choice(options, 0.0351)["label"] == "近 5 年")
    fallback = erp_warnings(
        {"total_return": {"twin": "932000CNY010", "unavailable": "csindex(932000CNY010) 返回空表"}},
        False, "近 3 年实测 -0.26% 越出可用区间")
    check("兜底那句带着改口要认的记号", any(ERP_FALLBACK_MARK in w for w in fallback), str(fallback))
    out = erp_warnings_for_window(fallback, erp_window_choice(options, 0.0351))
    check("选中一档后那条改口成这一档的实测", len(out) == 2 and "近 5 年" in out[0] and "+3.51%" in out[0], str(out))
    check("改口后不再说惯例，也不再谎称脱钩",
          not any(ERP_FALLBACK_MARK in w for w in out) and "脱钩" not in out[0], str(out))
    check("这是一次人工修正，措辞不许变成重算市场", "人工修正" in out[0], out[0])
    check("孪生那条与窗口无关，原样留着", out[1] == fallback[1], str(out[1]))
    check("没选窗口时一个字都不动", erp_warnings_for_window(fallback, None) == fallback)


# ---------------------------------------------------------------- 显示层不许写死常数

def test_the_derivation_graph_stops_hardcoding_the_convention() -> None:
    check("CoE 公式不再印着 6%", "6.0%" not in FORMULAS["cost_of_equity"], FORMULAS["cost_of_equity"])
    check("ERP 各行都无上游因而无公式",
          not ({"erp", *(f"erp_{window}" for window in ERP_REFERENCE_WINDOWS)} & set(FORMULAS)),
          str(sorted({"erp", *(f"erp_{window}" for window in ERP_REFERENCE_WINDOWS)} & set(FORMULAS))))
    check("窗口标签与取数层同序", set(ERP_WINDOW_LABELS) == {label for label, _ in market.ERP_WINDOWS},
          str(sorted(ERP_WINDOW_LABELS)))
    rows = erp_rows(stats(), 0.11, "近 3 年 realized 超额收益", True)
    check("级联行没有上游可写，就不许编公式", all(row.derives == () for row in rows))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
