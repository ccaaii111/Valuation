"""Risk-free rate: 10Y 中国国债到期收益率, 即期值与多年中枢并列.

Sourced from akshare's 东方财富 中美国债收益率 series (no account, no key). The raw rows are
recorded into the run's snapshot store so the discount rate is auditable like every other input.

Two numbers come out of one snapshot: the latest observation (即期) and the mean over a multi-year
window (中枢). 即期随利率周期走——2026 年 9 月的 1.68% 处在十年最低位，直接进 CAPM 会把折现率压到
只比永续增速高一点，Gordon 的 1/(r−g) 就成了估值本身。中枢把这一段周期摊平，但它是统计量而非观测，
所以两者都留在假设链上，只有一个进级联。
"""
from __future__ import annotations

import hashlib
import json
import statistics
from contextlib import redirect_stderr
from datetime import date, timedelta
from io import StringIO
from typing import Any

from backend.data.transport import Fetched, RunStore, now_iso

RAW_NAME = "risk_free"
SOURCE_URL = "https://data.eastmoney.com/cjsj/zmgzsyl.html"
AKSHARE_FUNC = "bond_zh_us_rate"
SERIES = "中国国债收益率10年"
# akshare pages history per start_date; ~11 calendar years is ~2700 rows and still one call.
# 为什么取这么长：这份快照是整条链唯一的国债收益率凭证，除 5 年中枢外还要当 ERP 的无风险腿
# （见 market.HISTORY_DAYS，最长参考窗口是近 10 年）。窗口一短，10 年那档就只能拿半截序列求均值，
# 测出来的超额收益属于另一个窗口。多出来的行只是留档，risk_free_rows 仍然只报近 5 年。
WINDOW_DAYS = round(11 * 365.25)
MAX_PLAUSIBLE_PCT = 25.0  # above this the column's unit must have changed; refuse to guess
NORMAL_WINDOW_YEARS = 5
# 中枢的下限。样本一短，"中枢"就只是换了个名字的即期值——2021~2026 这一段 10Y 单边下行，
# 用一年内均值当五年中枢，算出来的数与直接用即期相差不到 10bp，却会让人以为已经做过正常化。
NORMAL_MIN_OBSERVATIONS = 1000  # ≈4 个交易日年
NORMAL_MIN_SPAN_DAYS = 4 * 365


class MacroUnavailable(RuntimeError):
    pass


def fetch(days: int = WINDOW_DAYS) -> Fetched:
    """Hit akshare, then keep only the column we price off — the payload is evidence, not an archive."""
    try:
        import akshare  # slow import; only paid when actually fetching
    except ImportError as exc:
        raise MacroUnavailable(f"akshare 未安装: {exc}") from exc

    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        # akshare renders tqdm bars on stderr; keep the CLI/report output clean.
        with redirect_stderr(StringIO()):
            frame = getattr(akshare, AKSHARE_FUNC)(start_date=start)
    except Exception as exc:  # noqa: BLE001 - akshare raises requests/parse errors from many endpoints
        raise MacroUnavailable(f"{AKSHARE_FUNC} 取数失败: {type(exc).__name__}: {exc}") from exc
    if SERIES not in getattr(frame, "columns", []):
        raise MacroUnavailable(f"{AKSHARE_FUNC} 未返回列 {SERIES}，实际列: {list(frame.columns)[:8]}")

    rows = [{"日期": str(item[0])[:10], SERIES: _numeric(item[1])} for item in frame[["日期", SERIES]].itertuples(index=False, name=None)]
    payload = {"source": f"akshare {AKSHARE_FUNC}", "url": SOURCE_URL, "column": SERIES, "rows": rows}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return Fetched(
        name=RAW_NAME,
        url=SOURCE_URL,
        fetched_at=now_iso(),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        payload=payload,
    )


def _numeric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN guard without importing math


def parse(payload: dict) -> tuple[float | None, str]:
    """(decimal rate, provenance) for the most recent published observation.

    The eastmoney column is percent-scaled (1.6829 means 1.6829%), so the conversion is unconditional.
    """
    rows = (payload or {}).get("rows") or []
    dated = sorted(((str(row.get("日期") or ""), _numeric(row.get(SERIES))) for row in rows), key=lambda item: item[0])
    for day, value in reversed(dated):
        if value is None:
            continue
        if value > MAX_PLAUSIBLE_PCT:
            return None, f"unavailable: {SERIES} 观测值 {value} 超出 {MAX_PLAUSIBLE_PCT}%，列单位疑似已改变"
        return round(value / 100.0, 6), f"akshare {SERIES} @ {day} = {value:.4f}%"
    return None, f"unavailable: {payload.get('source', AKSHARE_FUNC)} 在 {len(rows)} 行中没有可用观测值"


def _observations(payload: dict | None) -> list[tuple[date, float]]:
    """(day, percent reading) ascending, dropping blanks and unparsable dates."""
    rows = (payload or {}).get("rows") or []
    out: list[tuple[date, float]] = []
    for row in rows:
        value = _numeric(row.get(SERIES))
        if value is None:
            continue
        try:
            day = date.fromisoformat(str(row.get("日期") or "")[:10])
        except ValueError:
            continue
        out.append((day, value))
    return sorted(out, key=lambda item: item[0])


def window_stats(payload: dict | None, years: int = NORMAL_WINDOW_YEARS) -> dict:
    """即期值 + 近 N 年中枢，同源于一份快照。

    The window is anchored on the last observation, not on today, so replaying an old batch offline
    reproduces the same 中枢. `mean` is None when a guard fires and `unavailable` says which one —
    the same contract the debt-cost and β rows use: keep the fallback AND explain the rejected measure.
    """
    obs = _observations(payload)
    spot, spot_source = parse(payload)
    stats: dict[str, Any] = {
        "series": SERIES,
        "window_years": years,
        "spot": spot,
        "spot_source": spot_source,
        "observations": 0,
        "first_day": None,
        "last_day": None,
        "mean": None,
        "median": None,
        "min": None,
        "max": None,
        "unavailable": "" if spot is not None else spot_source,
    }
    if not obs:
        return stats

    last_day = obs[-1][0]
    cutoff = last_day - timedelta(days=round(years * 365.25))
    window = [(day, value) for day, value in obs if day >= cutoff]
    stats["last_day"] = last_day.isoformat()
    stats["first_day"] = window[0][0].isoformat()
    stats["observations"] = len(window)

    if stats["unavailable"]:
        return stats  # 即期已经不靠谱（单位跳变/空载荷），中枢不必再算
    if len(window) < NORMAL_MIN_OBSERVATIONS:
        stats["unavailable"] = (
            f"近 {years} 年窗口只有 {len(window)} 个日频观测（{stats['first_day']}~{stats['last_day']}），"
            f"低于 {NORMAL_MIN_OBSERVATIONS} 个的下限"
        )
        return stats
    span_days = (last_day - window[0][0]).days
    if span_days < NORMAL_MIN_SPAN_DAYS:
        stats["unavailable"] = (
            f"窗口跨度 {span_days} 天，不足 {years} 年：样本稀疏或历史中断，均值定不住中枢"
        )
        return stats
    peak = max(value for _, value in window)
    if peak > MAX_PLAUSIBLE_PCT:
        stats["unavailable"] = f"窗口内出现 {peak} 的读数，超出 {MAX_PLAUSIBLE_PCT}%，列单位疑似已改变"
        return stats

    values = [value for _, value in window]
    stats["mean"] = round(statistics.mean(values) / 100.0, 6)
    stats["median"] = round(statistics.median(values) / 100.0, 6)
    stats["min"] = round(min(values) / 100.0, 6)
    stats["max"] = round(max(values) / 100.0, 6)
    return stats


def _provenance(stats: dict, tail: str) -> str:
    """序列级来源：即期与中枢同出这一条序列，具体读数在各自的假设行里。"""
    if not stats.get("last_day"):
        return f"{stats.get('unavailable') or 'unavailable: 无国债收益率观测'}{tail}"
    return (
        f"akshare {AKSHARE_FUNC} {SERIES} 日频序列 {stats['first_day']}~{stats['last_day']}"
        f"（{stats['observations']} 个观测）{tail}"
    )


def risk_free_stats(store: RunStore | None = None, live: bool = True) -> tuple[dict | None, str]:
    """(stats, provenance) for the ladder — one snapshot, both numbers, recorded for the audit trail.

    A run that already carries the snapshot reads from it, so --from-run stays offline; a fresh run
    fetches and records. `live=False` forbids the fallback fetch.
    """
    if store is not None:
        recorded = store.load(RAW_NAME)
        if recorded is not None:
            stats = window_stats(recorded)
            return stats, _provenance(stats, f"，快照复现 {store.snapshot_ref}")
        if not live:
            return None, f"unavailable: 批次 {store.snapshot_ref} 没有 {RAW_NAME} 快照，--from-run 不重新联网"

    try:
        item = fetch()
    except MacroUnavailable as exc:
        return None, f"unavailable: {exc}"

    if store is not None:
        store.record(item)
    stats = window_stats(item.payload)
    return stats, _provenance(stats, f"，取数于 {item.fetched_at}")
