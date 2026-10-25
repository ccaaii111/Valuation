"""Index and stock close series: 两条免登录通道, akshare 包装.

两条通道都免登录：sina（`stock_zh_index_daily` / `stock_zh_a_daily(adjust="qfq")`）与中证官网
（`stock_zh_index_hist_csindex`）。东财 `index_zh_a_hist` 在这张网络上会断连，所以不采用。指数腿按代码挑门：
sina 对 932000 中证2000 返回空表（akshare 抛 KeyError: 'date'），这类代码改走官网——它必须显式给
start/end_date，否则序列会凭空停在 2024-06-04。默认基准就是 932000，所以默认口径下 β 的指数腿与
ERP 的市场腿都出自官网那条通道。
The payload is regression evidence — dates and closes only — and is recorded per run so
--from-run reproduces a beta offline: 前复权序列会随每次除权重新基准化, 隔天再取同一个 β 就会变。

一次取数把 `BENCHMARK_NAMES` 里所有基准的指数序列都预取齐（`IndexSeriesMap`，并发 3），换基准因此不必
回线上——见 docs/caliber.md §4.6「哪条边当场算」旁边那节。取不到的基准不进预取集合，切到它时新鲜度照旧要求联网重取。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import date, timedelta
from io import StringIO
from typing import Any, Callable

from backend.data.transport import Fetched, RunStore, now_iso

RAW_NAME = "price_history"
SOURCE_URL = "https://finance.sina.com.cn"
CSINDEX_URL = "https://www.csindex.com.cn"
INDEX_FUNC = "stock_zh_index_daily"
CSINDEX_FUNC = "stock_zh_index_hist_csindex"
STOCK_FUNC = "stock_zh_a_daily"
HISTORY_DAYS = 1250  # 156 周回归需要 ~157 个周末收盘，留出停牌与节假日冗余
# 预取按这条长窗口下载：ERP 要 10 年，β 的 1250 天是它的子集。两条通道都是本地按 floor 过滤
# （sina 根本不收日期参数，官网收了也只是超集），所以一次长下载切两个窗口与从前两次下载同一批数。
LONG_HISTORY_DAYS = round(11 * 365.25)
WORKERS = 3  # 免登录源的并发上限：再高就是在替别人做压力测试
ATTEMPTS = 4
# 默认市场组合＝中证2000。理由一半是研究判断（A 股绝大多数个股的市值落在这家指数的区间里，小盘宽基才是
# 那把尺子），一半是取数边界：它在 `TR_SYMBOLS` 名单里，市场腿因此是全收益口径；它由中证在 2023-08-11
# 正式发布，而级联窗口（近 3 年）整段落在发布日之后，量到的是真实行情而非回算段（docs/caliber.md §12）。
# β 侧也不吃亏（个股腿仍是同一只票的前复权收盘）。`--benchmark` 随时可换回别家。
DEFAULT_BENCHMARK = "932000"
BENCHMARK_NAMES = {
    "000905": "中证500",
    "000906": "中证800",
    "932000": "中证2000",
    "000300": "沪深300",
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}
# sina 没有日线历史的代码（实测 sh932000 回空表，akshare 抛 KeyError: 'date'）。
CSINDEX_CODES = frozenset({"932000"})
# 每个宽基在全收益指数家族里的孪生代码（同为中证官网口径，实测 2026-10-13 四者都有覆盖 11 年窗口的日频历史）。
# 有孪生时 ERP 的市场腿直接取它：分红在除权日就再投资进指数了，不需要外部股息率。000001／399001／399006
# 没有孪生（实测 H00001 回空表；深证成指与创业板指不归中证发布），才降级用「价格指数 + 乐咕两市股息率」。
TR_SYMBOLS = {"000905": "H00905", "000906": "H00906", "932000": "932000CNY010", "000300": "H00300"}
CHANNEL_LABELS = {INDEX_FUNC: "新浪", CSINDEX_FUNC: "中证官网"}
# 一趟建链要预取的全部序列：七个宽基价格指数 + 它们的全收益孪生（孪生代码都不是六位数字，见 `series_loader`）。
PREFETCH_CODES = tuple(BENCHMARK_NAMES) + tuple(sym for sym in TR_SYMBOLS.values() if sym not in BENCHMARK_NAMES)


class PricesUnavailable(RuntimeError):
    pass


def index_symbol(code: str) -> str:
    """Sina's index symbol: 399xxx 归深市，其余主流宽基（含 000905 中证500）挂 sh。"""
    digits = re.sub(r"^(sh|sz)", "", str(code).strip().lower())
    if not re.fullmatch(r"\d{6}", digits):
        raise ValueError(f"无法识别的指数代码: {code}")
    return ("sz" if digits.startswith("399") else "sh") + digits


def stock_symbol(secucode: str) -> str:
    code, _, suffix = str(secucode).partition(".")
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"无法识别的证券代码: {secucode}")
    return (suffix.lower() or ("sh" if code[0] in "69" else "sz")) + code


def _akshare() -> Any:
    """akshare 只在真要取数时才 import（很慢），而这次 import 由 `load()` 安排在池子开工之前、
    在发起取数的那条线程上完成。

    这里只解决「慢 import 在每个 worker 里重复一遍」。它管不到 V8 的崩溃——那条坑争的是进程里第一次
    `MiniRacer()` 构造，不是第一次 import，挡它的是 `_warm_v8()`。
    """
    try:
        import akshare  # slow import; only paid when actually fetching
    except ImportError as exc:
        raise PricesUnavailable(f"akshare 未安装: {exc}") from exc
    return akshare


_V8_WARMED = False
_V8_WARM_LOCK = threading.Lock()


def _warm_v8() -> None:
    """在开线程池之前，于当前线程建掉第一个 JS 引擎上下文（只建一次）。

    akshare 的 `stock_zh_index_daily` 每次调用都新造一个 `MiniRacer()`，而 V8 的进程级「第一次初始化」
    经不起两条线程同时去争：撞上了进程直接死在
    `[FATAL:partition_address_space.cc(243)] Check failed: !IsConfigurablePoolInitialized()`，
    py_mini_racer 自己的锁挡不住（那把锁管的是上下文复用，不是这次初始化），Python 层连 traceback
    都拿不到，批次只落到 `raw/`。先单独建一个再丢掉，初始化就只剩那一次，后面的构造走已就绪的路径——
    不联网实测（46 个独立进程 × 8 线程 × 40 轮，`scripts/probe_prefetch_warmup.py`）：不预热死 13 个，
    先建一次死 0 个；而且落在主线程还是后台线程都同样安全（主线程先建 26 个、后台线程先建 20 个，各 0 死），
    争的是「第一次」不是「主线程」（docs/caliber.md §12 第 16 条，端到端重跑见 `scripts/repro_prefetch_abort.py`）。

    只是 import akshare 并不够：import 不构造上下文，争的仍是线程池里的第一次。取不到引擎（没装
    py_mini_racer 之类）不当成错误——真取数时自有另一条路报「取数失败」。
    """
    global _V8_WARMED
    with _V8_WARM_LOCK:  # 两个调用方同时预热就等于没预热：这里守的正是「同一时刻只有一条线程在做第一次」
        if _V8_WARMED:
            return
        try:
            from py_mini_racer import MiniRacer
        except Exception:  # noqa: BLE001 - 预热只是护栏，装不上就不预热，别在这里改判取数结果
            return
        MiniRacer().eval("1+1")
        _V8_WARMED = True


def fetch_frame(func: str, **kwargs: Any) -> Any:
    """一次 akshare 调用，失败重试到 ATTEMPTS 次。返回的表由调用方解析。"""
    akshare = _akshare()
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            # akshare renders tqdm bars on stderr; keep the CLI output clean.
            with redirect_stderr(StringIO()):
                return getattr(akshare, func)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - akshare raises requests/parse errors from many endpoints
            last = exc
            time.sleep(1.5)
    raise PricesUnavailable(f"{func} 取数失败: {type(last).__name__}: {last}")


def parse_closes(frame: Any, day_column: str, value_column: str, floor: str, echo_day: str = "") -> dict[str, float]:
    """day→读数，只留 floor 之后的正数；空表与缺列都算取数失败。

    echo_day：中证官网会把请求的 startDate 原样回显成首行，值与下一个真实交易日相同（请求 2010-01-01
    就凭空多出一条假日收盘）。留着它，样本计数与跨度护栏就都被一条不存在的读数带偏，所以「与次日同值」
    就丢掉。真实交易日的同值连号不会命中——它的首行日期不等于请求的起始日时根本不看。
    """
    columns = list(getattr(frame, "columns", []))
    for needed in (day_column, value_column):
        if needed not in columns:
            raise PricesUnavailable(f"{value_column} 序列缺列 {needed}，实际列: {columns[:8]}")
    out: dict[str, float] = {}
    for day, value in frame[[day_column, value_column]].itertuples(index=False, name=None):
        text = str(day)[:10]
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if text >= floor and number == number and number > 0:  # NaN guard without importing math
            out[text] = number
    ordered = dict(sorted(out.items()))
    days = list(ordered)
    if echo_day and days[:1] == [echo_day] and len(days) > 1 and ordered[days[1]] == ordered[echo_day]:
        ordered.pop(echo_day)
    return ordered


def index_channel(code: str) -> tuple[str, str]:
    """(akshare 函数, 出处 URL)：这个基准的价格指数走哪条通道。"""
    return (CSINDEX_FUNC, CSINDEX_URL) if code in CSINDEX_CODES else (INDEX_FUNC, SOURCE_URL)


def csindex_closes(symbol: str, history_days: int = HISTORY_DAYS) -> dict[str, float]:
    """中证官网的日频收盘（价格指数与全收益指数同一接口，中文列名，必须显式给日期区间）."""
    start = date.today() - timedelta(days=history_days)
    floor = start.isoformat()
    frame = fetch_frame(CSINDEX_FUNC, symbol=symbol, start_date=start.strftime("%Y%m%d"),
                        end_date=date.today().strftime("%Y%m%d"))
    closes = parse_closes(frame, "日期", "收盘", floor, echo_day=floor)
    if not closes:
        raise PricesUnavailable(f"{CSINDEX_FUNC}({symbol}) 在 {history_days} 天窗口内没有有效收盘")
    return closes


def index_closes(code: str, history_days: int = HISTORY_DAYS) -> dict[str, float]:
    """基准指数的日频收盘（价格口径），按代码挑通道。"""
    func, _ = index_channel(code)
    if func == CSINDEX_FUNC:
        return csindex_closes(code, history_days)
    symbol = index_symbol(code)
    closes = parse_closes(fetch_frame(func, symbol=symbol), "date", "close",
                          (date.today() - timedelta(days=history_days)).isoformat())
    if not closes:
        raise PricesUnavailable(f"{func}({symbol}) 在 {history_days} 天窗口内没有有效收盘")
    return closes


def series_loader(code: str, history_days: int) -> dict[str, float]:
    """预取账本用的取数路由：六位宽基按代码挑通道，全收益孪生（H00905／932000CNY010）只走中证官网。"""
    return index_closes(code, history_days) if re.fullmatch(r"\d{6}", code) else csindex_closes(code, history_days)


class IndexSeriesMap:
    """一趟建链里所有市场序列的账本：并发取一次（宽基指数与它们的全收益孪生），两份凭证各按自己的窗口就地切。

    单条序列取不到只让它自己不进预取集合（`closes` 对那个代码照抛），整批取数不因此作废——切到没预取
    到的那个基准时，新鲜度会像从前一样要求联网重取，而不是拿一条缺腿的序列假装算过。
    """

    def __init__(
        self,
        codes: tuple[str, ...] | list[str] = PREFETCH_CODES,
        history_days: int = LONG_HISTORY_DAYS,
        workers: int = WORKERS,
        loader: Callable[[str, int], dict[str, float]] = series_loader,
    ) -> None:
        self.codes = [str(code).strip() for code in codes]
        self.history_days = history_days
        self.workers = max(1, workers)
        self._loader = loader
        self._lock = threading.Lock()
        self._series: dict[str, dict[str, float]] = {}
        self._failed: dict[str, str] = {}
        self._loaded = False

    def load(self) -> "IndexSeriesMap":
        if self._loader is series_loader:
            _akshare()  # 池子开工前把慢 import 落在发起线程上，见 _akshare 的说明
            _warm_v8()  # import 还不够：进程里第一次 MiniRacer 构造必须先单独发生一次，见 _warm_v8 的说明
        with self._lock:  # 锁里跑网络没有代价：并发在池子里，别人不排队等这份账本。
            if self._loaded:
                return self
            with ThreadPoolExecutor(max_workers=min(self.workers, len(self.codes) or 1)) as pool:
                for code, outcome in zip(self.codes, pool.map(self._one, self.codes)):
                    if "error" in outcome:
                        self._failed[code] = outcome["error"]
                    else:
                        self._series[code] = outcome["closes"]
            self._loaded = True
            return self

    def _one(self, code: str) -> dict:
        try:
            return {"closes": self._loader(code, self.history_days)}
        except PricesUnavailable as exc:
            # 这条原文自己就带着 `函数(代码)` 与窗口，会一路显示到面板顶部，别再冠一层类型名。
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 一个指数取不到不该拖垮其余六个
            return {"error": f"{type(exc).__name__}: {exc}"}

    @property
    def available(self) -> list[str]:
        self.load()
        return sorted(self._series)

    @property
    def unavailable(self) -> dict[str, str]:
        self.load()
        return dict(self._failed)

    def closes(self, code: str, history_days: int | None = None) -> dict[str, float]:
        self.load()
        series = self._series.get(code)
        if series is None:
            raise PricesUnavailable(self._failed.get(code) or f"没有预取 {code} 的指数序列")
        days = self.history_days if history_days is None else history_days
        if days >= self.history_days:
            return dict(series)
        floor = (date.today() - timedelta(days=days)).isoformat()
        return {day: value for day, value in series.items() if day >= floor}


def _stock_closes(symbol: str) -> dict[str, float]:
    """个股前复权收盘。不显式给起点就依赖 akshare 的默认窗口，长度不可控。"""
    start = date.today() - timedelta(days=HISTORY_DAYS)
    frame = fetch_frame(STOCK_FUNC, symbol=symbol, adjust="qfq", start_date=start.strftime("%Y%m%d"),
                        end_date=date.today().strftime("%Y%m%d"))
    closes = parse_closes(frame, "date", "close", start.isoformat())
    if not closes:
        raise PricesUnavailable(f"{STOCK_FUNC}({symbol}) 在 {HISTORY_DAYS} 天窗口内没有有效收盘")
    return closes


def benchmark_code(benchmark: str) -> str:
    """把 `000905`／`sh000905`／`SZ399001` 都归成裸 6 位码——预取集合的键、下拉的值必须是同一个写法。"""
    return re.sub(r"^(sh|sz)", "", index_symbol(benchmark))


def fetch_all(secucode: str, series: IndexSeriesMap, benchmark: str = DEFAULT_BENCHMARK) -> Fetched:
    """一份行情凭证装着所有预取到的基准指数序列；`used_benchmark` 记这次建链用的是哪一个。"""
    symbol = stock_symbol(secucode)
    used = benchmark_code(benchmark)
    book = {
        code: {"symbol": index_symbol(code), "name": BENCHMARK_NAMES.get(code), "closes": series.closes(code, HISTORY_DAYS)}
        for code in series.available
        if code in BENCHMARK_NAMES
    }
    missing = {code: why for code, why in series.unavailable.items() if code in BENCHMARK_NAMES}
    channels = sorted({index_channel(code) for code in [*book, *missing]})
    payload = {
        "source": f"akshare {STOCK_FUNC}+" + "+".join(func for func, _ in channels),
        "url": " ; ".join([SOURCE_URL] + [url for _, url in channels if url != SOURCE_URL]),
        "adjust": "qfq",
        "window_days": HISTORY_DAYS,
        "stock": {"symbol": symbol, "closes": _stock_closes(symbol)},
        "used_benchmark": used,
        "benchmarks": book,
        "unavailable": missing,
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
    """这批快照里真拿着序列的基准。老批次只有一个基准，那一个就是它的全部预取集合。"""
    if not payload:
        return []
    book = payload.get("benchmarks")
    if book is not None:
        return sorted(book)
    recorded = str((payload.get("benchmark") or {}).get("code") or "")
    return [recorded] if recorded else []


def for_benchmark(payload: dict | None, benchmark: str) -> dict | None:
    """把多基准凭证切成建链从前见过的那一份（`stock` + 单个 `benchmark`）——下游只认这一个形状。

    老批次的凭证本来就是单基准：请求的正是它记着的那个就原样交回，请求别的基准才回 None。
    """
    if not payload:
        return None
    code = benchmark_code(benchmark)
    book = payload.get("benchmarks")
    if book is None:
        recorded = str((payload.get("benchmark") or {}).get("code") or "")
        return payload if recorded == code else None
    entry = book.get(code)
    if entry is None:
        return None
    single = {key: value for key, value in payload.items() if key not in ("benchmarks", "used_benchmark")}
    return {**single, "benchmark": {"code": code, **entry}}


def price_history(
    store: RunStore | None,
    secucode: str,
    benchmark: str = DEFAULT_BENCHMARK,
    live: bool = True,
    series: IndexSeriesMap | None = None,
) -> tuple[dict | None, str]:
    """(payload, provenance-or-reason) — a None payload means beta stays a placeholder, and the reason goes in the row note."""
    code = benchmark_code(benchmark)
    if store is not None:
        recorded = store.load(RAW_NAME)
        if recorded:
            view = for_benchmark(recorded, code)
            if view is None:
                return None, (
                    f"unavailable: 基准缺序列：批次 {store.snapshot_ref} 的凭证里没有 {code}"
                    f"（已有 {', '.join(prefetched(recorded)) or '无'}），换这个基准要联网重取一批"
                )
            return view, f"快照复现 {store.snapshot_ref}"
        if not live:
            return None, f"批次 {store.snapshot_ref} 未记录行情快照，--from-run 不重新联网"

    try:
        item = fetch_all(secucode, (series or IndexSeriesMap()).load(), benchmark)
    except (PricesUnavailable, ValueError) as exc:
        return None, f"unavailable: {exc}"

    if store is not None:
        store.record(item)
    view = for_benchmark(item.payload, code)
    if view is None:
        return None, (
            f"unavailable: 基准缺序列：这次预取到了 {', '.join(prefetched(item.payload)) or '一个基准都没有'}，"
            f"唯独没有 {code}——{item.payload['unavailable'].get(code) or '原因未记录'}"
        )
    func, _ = index_channel(code)
    return view, f"akshare 个股新浪 + 指数{CHANNEL_LABELS[func]} @ {item.fetched_at}"
