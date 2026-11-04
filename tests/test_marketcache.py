"""市场级本机缓存：取到最新一次就存下来，同一根收盘轴内不再重取，证据照旧逐批落全。

不联网——缓存目录一律指到临时目录，akshare 那一层用 `fixtures.Frame` 冒充。
钉住四件事：判新旧的到底是哪一根轴、命中时到底有没有碰线上、缓存把故障固化成一天默认答复没有、
以及每批 `raw/` 与 manifest 的取数时刻是不是那次真正联网。

    python tests/test_marketcache.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.builder import BuildOptions, build  # noqa: E402
from backend.data import macro, market, marketcache, prices  # noqa: E402
from backend.data.marketcache import Cache, fresh  # noqa: E402
from backend.data.transport import Fetched, now_iso  # noqa: E402
from fixtures import Frame  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    shown = detail if len(detail) <= 200 else detail[:200] + "…（截断）"
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {shown}" if shown else ""))
    if not condition:
        FAILURES += 1


class Boom(RuntimeError):
    """把建链停在已经看过的那一步。"""


class Temp:
    """临时缓存目录；退出时整目录删掉，跑这套件不在 %TEMP% 留东西。"""

    def __init__(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="marketcache-"))

    def __enter__(self) -> Cache:
        return Cache(self.directory)

    def __exit__(self, *exc):
        for path in self.directory.glob("*"):
            path.unlink()
        self.directory.rmdir()
        return False


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 北京时间 2026-09-28（周一）16:00 与 2026-09-29（周二）10:00／16:00：三刻跨两根收盘轴。
MONDAY_AFTER_CLOSE = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
TUESDAY_BEFORE_CLOSE = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
TUESDAY_AFTER_CLOSE = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)


def test_fresh_rule_follows_the_close_axis() -> None:
    taken = stamp(MONDAY_AFTER_CLOSE)
    check("收盘当天之后取的，次日上午还算同一根轴", fresh(taken, now=TUESDAY_BEFORE_CLOSE))
    check("跨到新收盘就判旧", not fresh(taken, now=TUESDAY_AFTER_CLOSE))
    check("时间戳读不出来按不新", not fresh("昨天下午", now=TUESDAY_BEFORE_CLOSE))
    check("空时间戳按不新", not fresh("", now=TUESDAY_BEFORE_CLOSE))


def test_read_rejects_every_unusable_document() -> None:
    with Temp() as cache:
        check("盘上没有这份", cache.read(marketcache.RISK_FREE) is None)

        broken = cache.path(marketcache.RISK_FREE)
        broken.write_text('{"version": 1, "payload": {"rows": [', encoding="utf-8")  # 半截 JSON
        check("半截 JSON 按没缓存处理，不抛", cache.read(marketcache.RISK_FREE, now=datetime.now(timezone.utc)) is None)

        old = cache.path(marketcache.INDEX_SERIES)
        old.write_text(json.dumps({"version": 99, "fetched_at": now_iso(), "last_day": "2026-10-28", "payload": {}}), encoding="utf-8")
        check("版本不对按没缓存处理", cache.read(marketcache.INDEX_SERIES) is None)

        empty = cache.path(marketcache.MARKET_DIVIDEND)
        empty.write_text(json.dumps({"version": 1, "fetched_at": now_iso(), "last_day": "2026-10-28", "payload": {}}), encoding="utf-8")
        check("载荷是空字典按没缓存处理", cache.read(marketcache.MARKET_DIVIDEND) is None)


def test_save_never_stores_a_document_without_an_observation_day() -> None:
    with Temp() as cache:
        check("last_day 为空时不落盘", cache.save(marketcache.RISK_FREE, {"rows": []}, fetched_at=now_iso(), last_day="") is None)
        check("落盘了就能读回", cache.save(marketcache.RISK_FREE, {"rows": [1]}, fetched_at=now_iso(), last_day="2026-10-28") is not None)
        check("临时文件名不会留下半截", not list(cache.directory.glob("*.tmp")))


# ---------------------------------------------------------------- 国债那条腿

def rf_payload(days: int = 1600, spot: float = 1.7) -> dict:
    end = date.today() - timedelta(days=1)  # 昨天的收盘观测：今天这根轴上源还没发
    # 1600 天让窗口跨过中枢那两条护栏（≥1000 个观测、≥4 年跨度），中枢才真算得出数来；
    # 除即期那一天外都填 2.1，中枢与即期因此不相等，比对才不是「None 等于 None」。
    rows = [
        {"日期": (end - timedelta(days=step)).isoformat(), macro.SERIES: spot if step == 0 else 2.1}
        for step in range(days)
    ]
    return {"source": "akshare bond_zh_us_rate", "url": macro.SOURCE_URL, "column": macro.SERIES, "rows": rows}


class RecordingStore:
    """够 `risk_free_stats` 用的最小批次层：能问「这批里有没有这份快照」，能收下写进来的凭证。"""

    benchmark = ""
    snapshot_ref = "20261029-150821"

    def __init__(self, recorded: dict | None = None):
        self._recorded = recorded
        self.recorded: list[Fetched] = []

    def load(self, name: str):
        return self._recorded if name == macro.RAW_NAME else None

    def record(self, item: Fetched) -> None:
        self.recorded.append(item)


def test_risk_free_fetches_once_then_reuses_the_cache() -> None:
    calls: list[int] = []

    def stub_fetch() -> Fetched:
        calls.append(1)
        return macro._seal(rf_payload(), now_iso())

    saved = macro.fetch
    try:
        macro.fetch = stub_fetch
        with Temp() as cache:
            first = RecordingStore()
            stats, source = macro.risk_free_stats(first, cache=cache)
            check("第一趟联网一次", len(calls) == 1, str(len(calls)))
            check("第一趟的凭证照写这一批", len(first.recorded) == 1, str(first.recorded))
            check("第一趟的留痕不提缓存", "缓存" not in source, source)
            wire_time = first.recorded[0].fetched_at

            second = RecordingStore()
            replayed, source = macro.risk_free_stats(second, cache=cache)
            check("第二趟一次网都不联", len(calls) == 1, str(len(calls)))
            check("第二趟的凭证照写这一批", len(second.recorded) == 1, str(second.recorded))
            check("凭证的取数时刻是那次真正联网", second.recorded[0].fetched_at == wire_time, second.recorded[0].fetched_at)
            check("摘要与联网那一版同一个算法", second.recorded[0].sha256 == first.recorded[0].sha256)
            check("留痕写明复用本机缓存", "本机市场缓存" in source and wire_time in source, source)
            check("复用的中枢与联网那一版相等", replayed["mean"] == stats["mean"], f"{replayed['mean']} vs {stats['mean']}")

            given = RecordingStore(rf_payload())
            macro.risk_free_stats(given, live=False, cache=cache)
            check("批次里已有快照时先读快照，不碰缓存", given.recorded == [], str(given.recorded))
            check("不联网那条路仍然不联网", len(calls) == 1, str(len(calls)))
    finally:
        macro.fetch = saved


def test_risk_free_does_not_cache_a_refused_reading() -> None:
    calls: list[int] = []

    def stub_fetch() -> Fetched:
        calls.append(1)
        return macro._seal(rf_payload(spot=900.0), now_iso())  # 单位跳变：护栏要拒

    saved = macro.fetch
    try:
        macro.fetch = stub_fetch
        with Temp() as cache:
            stats, _ = macro.risk_free_stats(RecordingStore(), cache=cache)
            check("荒谬读数被护栏拒掉", stats["spot"] is None, str(stats["spot"]))
            check("被拒的答复不落缓存", cache.path(marketcache.RISK_FREE).is_file() is False)
            macro.risk_free_stats(RecordingStore(), cache=cache)
            check("下一趟仍然联网重试", len(calls) == 2, str(len(calls)))
    finally:
        macro.fetch = saved


# ---------------------------------------------------------------- 指数预取那本账

INDEX_DAYS = [(date.today() - timedelta(days=step)).isoformat() for step in range(1, 40)]


def index_frame() -> Frame:
    return Frame(["date", "close", "日期", "收盘"], [(day, 1000.0 + step, day, 1000.0 + step) for step, day in enumerate(INDEX_DAYS)])


class WarmSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def book(codes: tuple[str, ...], cache: Cache | None, spy: WarmSpy) -> prices.IndexSeriesMap:
    ledger = prices.IndexSeriesMap(codes=codes, history_days=prices.LONG_HISTORY_DAYS, cache=cache)
    saved = (prices._warm_v8, prices.fetch_frame, prices._akshare)
    prices._warm_v8 = spy
    prices._akshare = lambda: None
    prices.fetch_frame = lambda func, **kwargs: index_frame()
    try:
        ledger.load()
    finally:
        prices._warm_v8, prices.fetch_frame, prices._akshare = saved
    return ledger


def test_index_book_is_prefetched_once_and_reused_the_same_day() -> None:
    codes = ("000905", "932000")
    with Temp() as cache:
        first_spy = WarmSpy()
        first = book(codes, cache, first_spy)
        check("联网那一趟预热过 JS 引擎", first_spy.calls == 1, str(first_spy.calls))
        check("联网那一趟不算缓存", first.from_cache is False)
        check("整本取到了才落缓存", cache.path(marketcache.INDEX_SERIES).is_file())

        second_spy = WarmSpy()
        second = book(codes, cache, second_spy)
        check("命中缓存时不预热引擎", second_spy.calls == 0, str(second_spy.calls))
        check("命中缓存时认得出是缓存", second.from_cache is True)
        check("两条序列都从缓存端回来", sorted(second.available) == sorted(codes), str(second.available))
        check("缓存里切出来的窗口与联网那一版同一段", second.closes("000905") == first.closes("000905"))
        check("账本的取数时刻用的是那次真正联网", second.fetched_at == first.fetched_at, second.fetched_at)


def test_index_book_misses_when_the_cached_copy_cannot_cover_the_request() -> None:
    with Temp() as cache:
        cache.save(
            marketcache.INDEX_SERIES,
            {"window_days": 1000, "closes": {"000905": {day: 1.0 for day in INDEX_DAYS[:5]}}},
            fetched_at=now_iso(), last_day=INDEX_DAYS[0],
        )
        spy = WarmSpy()
        short_window = book(("000905",), cache, spy)
        check("窗口比请求的短就不命中", short_window.from_cache is False and spy.calls == 1, str(spy.calls))

        cache.save(
            marketcache.INDEX_SERIES,
            {"window_days": prices.LONG_HISTORY_DAYS, "closes": {"000906": {day: 1.0 for day in INDEX_DAYS[:5]}}},
            fetched_at=now_iso(), last_day=INDEX_DAYS[0],
        )
        missing = book(("000905", "000906"), cache, WarmSpy())
        check("缺要的那条代码就不命中", missing.from_cache is False, str(missing.available))


def test_index_book_with_a_failed_leg_is_never_cached() -> None:
    saved = (prices._warm_v8, prices._akshare, prices.fetch_frame)
    prices._warm_v8 = lambda: None
    prices._akshare = lambda: None

    def half(func, **kwargs):
        if str(kwargs.get("symbol", "")).endswith("932000"):
            raise prices.PricesUnavailable("stock_zh_index_hist_csindex(932000) 空表")
        return index_frame()

    prices.fetch_frame = half
    try:
        with Temp() as cache:
            ledger = prices.IndexSeriesMap(codes=("000905", "932000"), cache=cache).load()
            check("缺一条腿时那条腿进 unavailable", "932000" in ledger.unavailable, str(ledger.unavailable))
            check("缺腿的那一趟不落缓存", cache.path(marketcache.INDEX_SERIES).is_file() is False)
    finally:
        prices._warm_v8, prices._akshare, prices.fetch_frame = saved


# ---------------------------------------------------------------- 全市场股息率那条腿

def dividend_series(days: int = 30) -> dict[str, float]:
    end = date.today() - timedelta(days=1)
    return {(end - timedelta(days=step)).isoformat(): 2.0 for step in range(days)}


def leg(cache: Cache | None, calls: list[str], fetch=None) -> tuple[dict, str, bool]:
    """走一遍股息率那条腿：默认两条市场都读到数，`fetch` 可以换成回故障的那版。"""
    saved = market._series

    def stub(func, day_column, value_column, floor, **kwargs):
        calls.append(str(kwargs.get("symbol")))
        return dividend_series()

    market._series = fetch or stub
    try:
        return market._dividend_leg(cache)
    finally:
        market._series = saved


def failing_leg(func, day_column, value_column, floor, **kwargs):
    raise market.MarketUnavailable("stock_a_gxl_lg 取数失败")


def test_dividend_leg_fetches_once_and_records_which_leg_was_reused() -> None:
    calls: list[str] = []
    with Temp() as cache:
        document, taken, reused = leg(cache, calls)
        check("两条市场各取一次", calls == ["上证A股", "深证A股"], str(calls))
        check("联网那一趟不算复用", reused is False)
        check("读到数就落缓存", cache.path(marketcache.MARKET_DIVIDEND).is_file())

        _, second_taken, second_reused = leg(cache, calls)
        check("第二趟一次都不取", calls == ["上证A股", "深证A股"], str(calls))
        check("第二趟认得出是复用", second_reused is True)
        check("时刻用的是那次真正联网", second_taken == taken, second_taken)

        ledger = prices.IndexSeriesMap(codes=("000905",), loader=lambda code, window: {day: 1.0 for day in INDEX_DAYS})
        item = market.fetch_all(ledger.load(), "000905", cache)
        check("凭证记下哪条腿复用了", list(item.payload.get("reused") or {}) == ["全市场股息率"], str(item.payload.get("reused")))
        check("凭证的时刻取两条腿里最早那次", item.fetched_at == min(ledger.fetched_at, second_taken), item.fetched_at)
        check("没复用的那条腿不写进凭证", "指数收盘" not in (item.payload.get("reused") or {}))


def test_dividend_leg_does_not_cache_a_total_failure() -> None:
    with Temp() as cache:
        document, _, reused = leg(cache, [], failing_leg)
        check("两侧都失败时载荷里留着原因", "error" in document["markets"]["上证A股"], list(document["markets"]))
        check("一次故障不落缓存", cache.path(marketcache.MARKET_DIVIDEND).is_file() is False)
        check("下一趟仍按未命中走", reused is False)


# ---------------------------------------------------------------- 建链那道开关

class StubStore:
    benchmark = ""
    snapshot_ref = "20261029-150821"
    created: list[str] = []

    @classmethod
    def create(cls, code: str):
        cls.created.append(code)
        return cls()

    def load(self, name: str):
        return None

    def record(self, item) -> None:
        pass

    def write_result(self, name: str, obj):
        return Path(f"_{name}.json")

    def finish(self) -> Path:
        return Path("_manifest.json")


def probe_build(**flags):
    """把建链停在指数账本构造那一步，看 builder 到底把缓存对象交下去没有。

    桩只换属性，不换真函数：`build()` 里三处都是 `模块.名字` 的调用形态，属性换了才拦得住。
    """
    import backend.builder as B

    seen: dict[str, object] = {"caches": [], "rf_cache": "国债那一步没被调用", "ledger_cache": "账本没被构造"}
    saved = (B.RunStore, B.fundamentals, B.universe.listed, marketcache.Cache, macro.risk_free_stats,
             prices.IndexSeriesMap)
    real_cache, real_map = marketcache.Cache, prices.IndexSeriesMap

    class ProbeCache(real_cache):  # type: ignore[misc]
        def __init__(self, directory=None):
            super().__init__(directory)
            seen["caches"].append(self)

    class ProbeMap(real_map):
        def __init__(self, *args, **kwargs):
            seen["ledger_cache"] = kwargs.get("cache", "没给这个参数")

        def load(self):
            raise Boom("账本的构造已经看过")

    def spy_risk_free(store, live=True, cache=None):
        seen["rf_cache"] = cache
        return None, "unavailable: 桩"

    with Temp() as cache:
        marketcache.Cache = ProbeCache  # type: ignore[misc]
        macro.risk_free_stats = spy_risk_free  # type: ignore[assignment]
        prices.IndexSeriesMap = ProbeMap  # type: ignore[assignment]
        B.RunStore = StubStore  # type: ignore[attr-defined]
        B.fundamentals = type("StubFundamentals", (), {"fetch": staticmethod(lambda *a, **k: {})})  # type: ignore[attr-defined]
        B.universe.listed = lambda code, now=None: (True, "")
        try:
            build(BuildOptions(code="600519", **flags))
            check("建链停在桩上", False, "没有抛 Boom")
        except Boom:
            pass
        finally:
            (B.RunStore, B.fundamentals, B.universe.listed, marketcache.Cache, macro.risk_free_stats,
             prices.IndexSeriesMap) = saved
        check("桩建链不往临时缓存目录写东西", list(cache.directory.iterdir()) == [], str(list(cache.directory.iterdir())))
        return seen


def test_builder_passes_the_cache_and_the_switch_honours_it() -> None:
    default = probe_build()
    check("默认把缓存交到国债那一步", isinstance(default["rf_cache"], Cache), str(default["rf_cache"]))
    check("默认把同一个缓存交到指数账本", default["ledger_cache"] is default["rf_cache"], str(default["ledger_cache"]))

    forced = probe_build(no_market_cache=True)
    check("--no-market-cache 时国债那一步拿到的是「没有缓存」", forced["rf_cache"] is None, str(forced["rf_cache"]))
    check("--no-market-cache 时账本也拿不到缓存", forced["ledger_cache"] is None, str(forced["ledger_cache"]))
    check("绕过缓存时一个缓存对象都不建", forced["caches"] == [], str(forced["caches"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
