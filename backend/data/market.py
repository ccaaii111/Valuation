"""Realized market returns for the equity risk premium: 同指数全收益（或价格+股息率）+ 10Y 国债.

ERP 是 CAPM 里唯一一处「整个市场值多少风险」的输入。惯例值 6.0% 写在 assumptions 里，跟今年的市场
没有半点关系；这里把它改成实测：一段窗口内市场组合的年化总收益减去同口径的 10Y 国债收益率，就是那段
窗口的 realized ERP。窗口取多长、越界要不要用，判断在 `assumptions.pick_erp`，本模块只负责把每个窗口的
数摊开、并说清算不出来是因为哪一条腿。

市场组合用哪个指数＝β 回归用哪个基准（`--benchmark`）：两处「市场」必须是同一个东西。否则 β 说的是
中证500 的相对波动、ERP 说的是沪深300 的超额收益，同一个 CAPM 式子里混进两个市场对风险的定价。

市场腿有两条，取哪条由「这个基准有没有全收益孪生」决定（名单只在 `prices.TR_SYMBOLS` 一处，这里不再抄一遍）：
- 有孪生：直接取中证官网的全收益指数。分红在除权日就再投资进点位里，不需要外部股息率，也就不再有
  「分红腿是全市场口径」这层杂质。它仍是税前口径（红利税不计）。
- 没有孪生（上证/深成/创业板这三家不由中证发布，外加任何自己填进来的代码）：退回「价格指数年化涨幅 +
  乐咕沪深两市股息率均值」。免登录源里能拿到长历史的全市场股息率只有乐咕这一家（中证指数的股息率只回最近
  20 个交易日，国证接口根本没有股息率列），而它统计的是两市全部 A 股，与指数成分不是一套股票——这条杂质
  照写进行 note。
所以乐咕只在无孪生基准（或孪生取数失败）那一档被用到；有孪生的那档凭证里明确记着「未取股息率」及其原因。
预取全部基准时它一趟只取一次——那三条无孪生的路共用同一份两市股息率，它与选哪个基准无关。

无风险腿复用建链同一份 risk_free 快照，不另起序列：一次取数一份凭证。进 CAPM 的 rf 仍按
`--risk-free-basis` 单独择一，两者不必相等（「窗口超额 + 中枢 rf」这个混搭记在已知限制里）。
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import date, timedelta
from typing import Any, Callable

from backend.data import prices
from backend.data.macro import MAX_PLAUSIBLE_PCT
from backend.data.transport import Fetched, RunStore, now_iso

RAW_NAME = "market_returns"
SERIES_COLUMN = "中国国债收益率10年"
DIVIDEND_FUNC = "stock_a_gxl_lg"
DIVIDEND_URL = "https://legulegu.com/stockdata/guxilv"
# 分红腿取两市口径的日均值：单取上证会把深市的高分红股票整个漏掉。
DIVIDEND_MARKETS = ("上证A股", "深证A股")
# 最长的参考窗口是近 10 年，起点要落在序列之内，再加一年冗余应对停牌与长假。这条也就是预取的下载窗口
# （`prices.LONG_HISTORY_DAYS`）——β 那 1250 天从它切出来，两个窗口因此不可能各自漂。
HISTORY_DAYS = prices.LONG_HISTORY_DAYS
MAX_PLAUSIBLE_YIELD_PCT = 10.0  # 股息率列若跳到这个量级，单位一定已经变了
MAX_PLAUSIBLE_YIELD = MAX_PLAUSIBLE_YIELD_PCT / 100.0  # 本模块比的是小数口径
MAX_PLAUSIBLE_RATE = MAX_PLAUSIBLE_PCT / 100.0  # 国债收益率读数同样已经是小数

# 窗口清单：级联固定取 `assumptions.ERP_CASCADE_WINDOW` 那一档，其余三档只作对照行。
# years=None 表示「今年以来」：基数是上一个收盘，终点是最近一个共同观测日。
ERP_WINDOWS: tuple[tuple[str, float | None], ...] = (("ytd", None), ("3y", 3.0), ("5y", 5.0), ("10y", 10.0))
# 一段窗口能不能算年化收益：日频行情一年约 243 个收盘，100 个/年是「序列没断」的下限；
# 跨度不足请求长度的 75% 说明历史中途断过，年化出来的数不属于这个窗口。
MIN_OBSERVATIONS_PER_YEAR = 100
MIN_RISK_FREE_OBSERVATIONS = 100  # 国债收益率序列同样是日频；一薄就说明快照窗口比 ERP 窗口短
MIN_SPAN_RATIO = 0.75
MIN_SPAN_DAYS = 60  # 今年 1 月的「今年以来」只有几天，年化等于把噪声放大成一位小数


class MarketUnavailable(RuntimeError):
    pass


def _keep(action: Callable[[], Any], what: str) -> Any:
    """prices 层的取数失败在这里降级成 MarketUnavailable。

    β 缺行情会炸链，ERP 缺一条腿不会：级联有 documented fallback（`market_history`），所以异常必须停
    在本模块的类型上，让上层把它写进 note 而不是写进 build_failed.json。

    prices 层的几条「没有有效收盘」原文自己就带着 `函数(代码)` 前缀，与这里的 `what` 同串；这句警告会一路
    显示到面板顶部，所以同前缀不再重复一遍。
    """
    try:
        return action()
    except prices.PricesUnavailable as exc:
        text = str(exc)
        raise MarketUnavailable(text if text.startswith(what) else f"{what}: {text}") from exc


def _series(func: str, day_column: str, value_column: str, floor: str, **kwargs: Any) -> dict[str, float]:
    """一条日频序列（股息率读数是百分数，此处只原样收着，转小数在窗口里做）。"""
    return _keep(
        lambda: prices.parse_closes(prices.fetch_frame(func, **kwargs), day_column, value_column, floor),
        f"{func}({kwargs.get('symbol', '')})",
    )


def _total_return_leg(code: str, series: prices.IndexSeriesMap) -> dict:
    """一个基准的市场腿：有孪生就取全收益指数；没有孪生、或孪生取不到，都写清原因并留给降级腿。

    孪生取不到不等于这一档的测算作废——还有一条「价格 + 股息率」的路，原始答复留在凭证里。
    """
    twin = prices.TR_SYMBOLS.get(code)
    if twin is None:
        return {"twin": None,
                "unavailable": f"{code} 在中证官网没有全收益孪生指数，市场腿退回价格指数 + 股息率"}
    try:
        closes = series.closes(twin)
    except prices.PricesUnavailable as exc:
        return {"twin": twin, "unavailable": str(exc)}
    return {"source": f"akshare {prices.CSINDEX_FUNC}", "url": prices.CSINDEX_URL, "twin": twin, "closes": closes}


def _dividend_leg() -> dict:
    """乐咕两市股息率：与基准无关（统计的是两市全部 A 股），一趟建链只取一次，无孪生的那几家共用。

    缺一侧也照落盘：ERP 会退回惯例值，但原始答复是什么必须留在凭证里。
    """
    floor = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    markets: dict[str, Any] = {}
    for name in DIVIDEND_MARKETS:
        try:
            markets[name] = _series(DIVIDEND_FUNC, "日期", "股息率", floor, symbol=name)
        except MarketUnavailable as exc:
            markets[name] = {"error": str(exc)}
    return {"source": f"akshare {DIVIDEND_FUNC}", "url": DIVIDEND_URL, "markets": markets}


def fetch_all(series: prices.IndexSeriesMap, benchmark: str = prices.DEFAULT_BENCHMARK) -> Fetched:
    """一份市场腿凭证装着所有预取到的基准：价格指数、各自的孪生、共用的股息率。

    股息率与基准无关（乐咕统计的是两市全部 A 股），所以一趟建链只取一次，三家无孪生的基准共用同一份。
    """
    book: dict[str, Any] = {}
    for code in [row for row in series.available if row in prices.BENCHMARK_NAMES]:
        leg = _total_return_leg(code, series)
        book[code] = {
            "symbol": prices.index_symbol(code),
            "name": prices.BENCHMARK_NAMES.get(code),
            "closes": series.closes(code, HISTORY_DAYS),
            "total_return": leg,
        }
    channels = sorted({prices.index_channel(code) for code in book})
    funcs = [func for func, _ in channels] + [prices.CSINDEX_FUNC, DIVIDEND_FUNC]
    urls = [url for _, url in channels] + [prices.CSINDEX_URL, DIVIDEND_URL]
    payload = {
        "source": "akshare " + "+".join(dict.fromkeys(funcs)),
        "url": " ; ".join(dict.fromkeys(urls)),
        "window_days": HISTORY_DAYS,
        "used_benchmark": prices.benchmark_code(benchmark),
        "benchmarks": book,
        "dividend": _dividend_leg(),
        "unavailable": {code: why for code, why in series.unavailable.items() if code in prices.BENCHMARK_NAMES},
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return Fetched(
        name=RAW_NAME,
        url=payload["url"],
        fetched_at=now_iso(),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        payload=payload,
    )


def prefetched(payload: dict | None) -> list[str]:
    """这批快照里真拿着市场腿的基准。老批次只有一个基准，那一个就是它的全部预取集合。"""
    if not payload:
        return []
    book = payload.get("benchmarks")
    if book is not None:
        return sorted(book)
    recorded = str((payload.get("benchmark") or {}).get("code") or "")
    return [recorded] if recorded else []


def _dividend_view(total_return: dict, dividend: dict | None) -> dict:
    """有孪生那一档，凭证要写明「未取股息率」——这句话从前由取数那一刻写下，现在由切出来的视图说。"""
    if total_return.get("closes"):
        return {"skipped": f"市场腿用 {total_return.get('twin')} 全收益指数，分红已含在点位里，未取乐咕股息率"}
    return dividend or {}


def for_benchmark(payload: dict | None, benchmark: str) -> dict | None:
    """把多基准凭证切成 `window_stats` 从前见过的那一份——下游只认这一个形状。"""
    if not payload:
        return None
    code = prices.benchmark_code(benchmark)
    book = payload.get("benchmarks")
    if book is None:
        recorded = str((payload.get("benchmark") or {}).get("code") or "")
        return payload if recorded == code else None
    entry = book.get(code)
    if entry is None:
        return None
    single = {key: value for key, value in payload.items() if key not in ("benchmarks", "dividend", "total_return")}
    return {
        **single,
        "benchmark": {"code": code, "symbol": entry["symbol"], "name": entry["name"], "closes": entry["closes"]},
        "total_return": entry["total_return"],
        "dividend": _dividend_view(entry["total_return"], payload.get("dividend")),
    }


def market_history(
    store: RunStore | None,
    benchmark: str = prices.DEFAULT_BENCHMARK,
    live: bool = True,
    series: prices.IndexSeriesMap | None = None,
) -> tuple[dict | None, str]:
    """(payload, provenance-or-reason) — a None payload means ERP stays the convention value.

    Unlike β, a missing market series must never abort the chain: the ladder has a documented fallback,
    so the reason goes into the row note instead of into build_failed.json.
    """
    code = prices.benchmark_code(benchmark)
    if store is not None:
        recorded = store.load(RAW_NAME)
        if recorded:
            view = for_benchmark(recorded, code)
            if view is None:
                return None, (
                    f"unavailable: 基准未预取：批次 {store.snapshot_ref} 的凭证里没有 {code}"
                    f"（已有 {', '.join(prefetched(recorded)) or '无'}），换这个基准要联网重取一批"
                )
            return view, f"快照复现 {store.snapshot_ref}"
        if not live:
            return None, f"unavailable: 批次 {store.snapshot_ref} 未记录 {RAW_NAME} 快照，--from-run 不重新联网"

    try:
        item = fetch_all((series or prices.IndexSeriesMap()).load(), benchmark)
    except (MarketUnavailable, prices.PricesUnavailable, ValueError) as exc:
        return None, f"unavailable: {exc}"

    if store is not None:
        store.record(item)
    view = for_benchmark(item.payload, code)
    if view is None:
        why = item.payload["unavailable"].get(code) or f"这次只预取到 {', '.join(prefetched(item.payload)) or '无'}"
        return None, f"unavailable: {code} 的市场腿没有原始序列——{why}"
    return view, f"{item.payload['source']} @ {item.fetched_at}"


def _decimals(payload: dict | None, column: str) -> dict[str, float]:
    """day→decimal from a percent-scaled series (1.6829 means 1.6829%)."""
    out: dict[str, float] = {}
    for row in (payload or {}).get("rows") or []:
        day = str(row.get("日期") or "")[:10]
        try:
            number = float(row.get(column))
        except (TypeError, ValueError):
            continue
        if day and number == number:
            out[day] = number / 100.0
    return dict(sorted(out.items()))


def _mean_between(series: dict[str, float], start: str, end: str) -> tuple[float | None, int]:
    values = [value for day, value in series.items() if start <= day <= end]
    return (None if not values else statistics.mean(values)), len(values)


def _dividend_between(markets: dict[str, Any], start: str, end: str) -> tuple[float | None, int, list[str]]:
    """两市股息率的日均值（转小数）+ 读数天数 + 缺腿说明。"""
    legs: dict[str, dict[str, float]] = {}
    missing: list[str] = []
    for name, series in markets.items():
        if not isinstance(series, dict):
            missing.append(str(name))
            continue
        if series.get("error"):
            missing.append(f"{name} 取数失败（{series['error']}）")
            continue
        readings = {day: value / 100.0 for day, value in series.items() if start <= day <= end and day}
        if not readings:
            missing.append(f"{name} 在窗口内没有读数")
            continue
        legs[name] = readings
    if not legs:
        return None, 0, missing
    days = sorted(set().union(*(set(series) for series in legs.values())))
    daily = [statistics.mean([series[day] for series in legs.values() if day in series]) for day in days]
    return statistics.mean(daily), len(days), missing


def _blank_window(label: str, years: float | None, reason: str = "") -> dict[str, Any]:
    return {
        "window": label,
        "years_requested": years,
        "market_leg": "",
        "observations": 0,
        "base_day": None,
        "last_day": None,
        "span_days": 0,
        "span_years": None,
        "close_base": None,
        "close_last": None,
        "price_change": None,
        "price_annualized": None,
        "total_return_base": None,
        "total_return_last": None,
        "total_return_observations": 0,
        "total_return_annualized": None,
        "dividend_yield_mean": None,
        "dividend_observations": 0,
        "dividend_markets_missing": [],
        "risk_free_mean": None,
        "risk_free_observations": 0,
        "risk_free_basis": "",
        "risk_free_first_day": None,
        "risk_free_last_day": None,
        "erp": None,
        "erp_price_only": None,
        "unavailable": reason,
    }


def _is_day(text: str) -> bool:
    """只认真正的日期键：分红腿失败时落的是 {"error": …}，那一条不能当「有一天读数」。"""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))


def _annualized(series: dict[str, float], base_day: str, end_day: str, span_years: float) -> float | None:
    base, last = series.get(base_day), series.get(end_day)
    if not base or not last or span_years <= 0:
        return None
    return (last / base) ** (1.0 / span_years) - 1.0


def _risk_free_leg(rf: dict[str, float], years: float | None, start_day: str, end_day: str) -> dict[str, Any]:
    """无风险腿与级联的 rf 中枢用同一把尺子：窗口锚在国债序列最后一个观测日，往前推整 N 年。

    否则「近 5 年超额」减的是行情末日往前推的国债均值，而级联那一行 rf 用的是 `macro.window_stats` 的
    中枢，两个 5 年根本不是同一段——差额会在 CAPM 里以 r−g 的口径错位重现。round 到 6 位与 macro 同规矩，
    5 年那一档的减数因此与 rf 中枢行完全相同。今年以来没有「中枢」可言，照本窗口求均值，并在 basis 点名。
    """
    last_day = max(rf)
    if years is None:
        first_day, last = start_day, end_day
        basis = "今年以来均值"
        limit_years = (date.fromisoformat(last) - date.fromisoformat(first_day)).days / 365.25
    else:
        first_day = (date.fromisoformat(last_day) - timedelta(days=round(years * 365.25))).isoformat()
        last, basis, limit_years = last_day, f"近 {years:g} 年中枢", years
    mean, count = _mean_between(rf, first_day, last)
    out: dict[str, Any] = {"mean": None, "observations": count, "basis": basis,
                           "first_day": first_day, "last_day": last, "reasons": []}
    if mean is None:
        out["reasons"] = [f"risk_free 快照在 {first_day}~{last} 没有 {SERIES_COLUMN} 读数"]
        return out
    if count < MIN_RISK_FREE_OBSERVATIONS * limit_years:
        out["reasons"] = [
            f"国债收益率在 {first_day}~{last} 只有 {count} 个读数 ÷ {limit_years:.2f} 年，低于每年 "
            f"{MIN_RISK_FREE_OBSERVATIONS} 个的下限——快照窗口比这段 ERP 窗口短，均值定不住无风险腿"
        ]
    elif max((value for day, value in rf.items() if first_day <= day <= last), default=0.0) > MAX_PLAUSIBLE_RATE:
        out["reasons"] = [f"窗口内出现超出 {MAX_PLAUSIBLE_PCT:.0f}% 的国债收益率读数，单位疑似已改变"]
    else:
        out["mean"] = round(mean, 6)
    return out


def _window(closes: dict[str, float], tr: dict[str, float], markets: dict[str, Any], rf: dict[str, float],
            label: str, years: float | None, end_day: str) -> dict[str, Any]:
    stats = _blank_window(label, years)
    primary = tr or closes
    stats["market_leg"] = "total_return" if tr else "price_plus_dividend"
    start_day = f"{int(end_day[:4])}-01-01" if years is None else (
        date.fromisoformat(end_day) - timedelta(days=round(years * 365.25))
    ).isoformat()
    earlier = [day for day in primary if day < start_day]
    if not earlier:
        stats["unavailable"] = (
            f"{start_day} 之前没有更早的收盘作涨幅基数（{'全收益' if tr else '价格'}指数序列起点 {min(primary)}）"
        )
        return stats
    base_day = earlier[-1]
    window = [day for day in primary if base_day <= day <= end_day]
    span_days = (date.fromisoformat(end_day) - date.fromisoformat(base_day)).days
    span_years = span_days / 365.25

    stats["base_day"] = base_day
    stats["last_day"] = end_day
    stats["observations"] = len(window)
    stats["span_days"] = span_days
    stats["span_years"] = round(span_years, 4)

    reasons: list[str] = []
    if span_days < MIN_SPAN_DAYS:
        reasons.append(f"窗口跨度只有 {span_days} 天，不足 {MIN_SPAN_DAYS} 天")
    if years is not None and span_days < years * 365.25 * MIN_SPAN_RATIO:
        reasons.append(f"窗口跨度 {span_days} 天，不足请求长度 {years:g} 年的 {MIN_SPAN_RATIO:.0%}（序列太短或历史中断）")
    if len(window) < MIN_OBSERVATIONS_PER_YEAR * span_years:
        reasons.append(f"{len(window)} 个收盘 ÷ {span_years:.2f} 年，低于每年 {MIN_OBSERVATIONS_PER_YEAR} 个的下限")

    market_annualized = _annualized(primary, base_day, end_day, span_years)
    if market_annualized is None:
        reasons.append("首末收盘读数不合法，年化涨幅无从起算")

    # 价格指数那一腿在两条路径下都留着：无孪生时它就是市场腿，有孪生时它只用来给「不含分红」的对照数。
    stats["price_annualized"] = _annualized(closes, base_day, end_day, span_years)
    if stats["price_annualized"] is not None:
        stats["close_base"] = closes[base_day]
        stats["close_last"] = closes[end_day]
        stats["price_change"] = closes[end_day] / closes[base_day] - 1.0

    if tr:
        stats["total_return_base"] = tr.get(base_day)
        stats["total_return_last"] = tr.get(end_day)
        stats["total_return_observations"] = len(window)
    else:
        dividend, dividend_days, missing = _dividend_between(markets, base_day, end_day)
        stats["dividend_yield_mean"] = dividend
        stats["dividend_observations"] = dividend_days
        stats["dividend_markets_missing"] = missing
        if dividend is None:
            reasons.append("股息率腿不可用：" + ("；".join(missing) if missing else "窗口内没有读数"))
        elif dividend > MAX_PLAUSIBLE_YIELD:
            reasons.append(f"窗口内股息率均值 {dividend:.2%} 超出 {MAX_PLAUSIBLE_YIELD_PCT:.0f}%，单位疑似已改变")

    rf_leg = _risk_free_leg(rf, years, start_day, end_day)
    risk_free = rf_leg["mean"]
    stats["risk_free_mean"] = risk_free
    stats["risk_free_basis"] = rf_leg["basis"]
    stats["risk_free_first_day"] = rf_leg["first_day"]
    stats["risk_free_last_day"] = rf_leg["last_day"]
    stats["risk_free_observations"] = rf_leg["observations"]
    reasons.extend(rf_leg["reasons"])

    if reasons:
        stats["unavailable"] = "；".join(reasons)
        return stats

    if tr:
        stats["total_return_annualized"] = market_annualized
        stats["erp"] = market_annualized - risk_free
        stats["erp_price_only"] = None if stats["price_annualized"] is None else stats["price_annualized"] - risk_free
        return stats

    # 分红腿按「年化价格涨幅 + 窗口股息率均值」近似：股息率是当期收益率，既不含红利再投资的复利，
    # 也不是指数成分的分红——所以这一条腿只是把系统性低估补回来，不等于总收益指数。
    stats["total_return_annualized"] = market_annualized + stats["dividend_yield_mean"]
    stats["erp"] = stats["total_return_annualized"] - risk_free
    stats["erp_price_only"] = market_annualized - risk_free
    return stats


def empty_stats(reason: str) -> dict:
    """No usable snapshot — every window carries the same reason and the ladder falls back to convention."""
    return {"benchmark": None, "total_return": {"twin": None, "unavailable": reason}, "unavailable": reason,
            "windows": {label: _blank_window(label, years, reason) for label, years in ERP_WINDOWS}}


def window_stats(payload: dict | None, rf_payload: dict | None) -> dict:
    """每个窗口一份 realized 超额收益测算；算不出来的窗口只说原因，不猜数。

    窗口锚在这几条序列的最后一个共同观测日上，而不是「今天」：复现老批次时同一份快照必须算出同一个数。
    市场腿走哪条路径由快照里实际有什么决定（有全收益就用全收益，否则用价格+股息率），不在载荷里另存一个
    「腿」字段——那样真话就存了两份，老批次复现时还会被新字段骗到。
    """
    if not payload:
        return empty_stats("unavailable: 没有 market_returns 快照")
    benchmark = payload.get("benchmark") or {}
    closes = {str(day)[:10]: float(value) for day, value in (benchmark.get("closes") or {}).items()}
    if not closes:
        return empty_stats("unavailable: 快照里没有指数收盘序列")
    block = payload.get("total_return") or {}
    tr = {str(day)[:10]: float(value) for day, value in (block.get("closes") or {}).items()}
    markets = (payload.get("dividend") or {}).get("markets") or {}
    dividend_days = {day for series in markets.values() if isinstance(series, dict) for day in series if _is_day(day)}
    if not tr and not dividend_days:
        why = block.get("unavailable") or (payload.get("dividend") or {}).get("skipped") or ""
        return empty_stats("unavailable: 快照里既没有全收益指数也没有股息率序列"
                           + (f"（{why}）" if why else ""))
    rf = _decimals(rf_payload, SERIES_COLUMN)
    if not rf:
        return empty_stats("unavailable: 没有 risk_free 快照，ERP 的无风险利率腿无从计算")

    # 窗口终点＝三条腿都覆盖到的最后一天，谁慢就用谁，免得用一段没有市场腿或没有 rf 的尾巴。
    end_day = min(max(closes), max(tr or dividend_days), max(rf))
    return {
        "benchmark": {"code": benchmark.get("code"), "symbol": benchmark.get("symbol"), "name": benchmark.get("name")},
        "total_return": {"twin": block.get("twin"),
                         "unavailable": block.get("unavailable")
                         or ("" if tr else "这批快照取数时还没有全收益腿，按价格指数 + 股息率复现")},
        "unavailable": "",
        "windows": {label: _window(closes, tr, markets, rf, label, years, end_day)
                    for label, years in ERP_WINDOWS},
    }
