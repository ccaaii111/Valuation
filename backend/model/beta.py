"""OLS beta against a benchmark index, computed from weekly close series.

Pure functions over {date: close} maps: the fetch layer owns akshare, this module owns the math, so a
beta stays reproducible from whatever price snapshot a run recorded — no pandas, no network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

WINDOW_WEEKS = 156  # 3 年周频，实务默认口径
MAX_GAP_DAYS = 10  # 相邻两周的周末收盘相隔约 7 天；超过 10 天说明中间停牌了一周，不把这个跳步当周期收益


@dataclass
class BetaStats:
    beta: float | None
    r_squared: float | None
    t_stat: float | None
    observations: int
    first_day: str
    last_day: str

    @property
    def available(self) -> bool:
        return self.beta is not None


def _iso_week(day: str) -> str:
    iso_year, iso_week, _ = date.fromisoformat(day).isocalendar()
    return f"{iso_year}-{iso_week:02d}"


def weekly_closes(closes: dict[str, float]) -> dict[str, tuple[str, float]]:
    """The last traded close inside each ISO week, keyed by that week."""
    out: dict[str, tuple[str, float]] = {}
    for day in sorted(closes):
        value = closes[day]
        if isinstance(value, (int, float)) and value > 0:
            out[_iso_week(day)] = (day, float(value))
    return out


def _gap_days(first: str, second: str) -> int:
    return (date.fromisoformat(second) - date.fromisoformat(first)).days


def period_returns(weeks: dict[str, tuple[str, float]]) -> dict[str, float]:
    """Week-over-week returns on the trading calendar, dropping 停牌 gaps."""
    keys = sorted(weeks)
    out: dict[str, float] = {}
    for previous, current in zip(keys, keys[1:]):
        previous_day, previous_close = weeks[previous]
        current_day, current_close = weeks[current]
        if not previous_close or _gap_days(previous_day, current_day) > MAX_GAP_DAYS:
            continue
        out[current] = current_close / previous_close - 1.0
    return out


def regress(
    stock: dict[str, float], index: dict[str, float], window_weeks: int = WINDOW_WEEKS
) -> BetaStats:
    """Slope of 个股周收益 on 指数周收益, over the last `window_weeks` shared weeks."""
    stock_returns = period_returns(weekly_closes(stock))
    index_returns = period_returns(weekly_closes(index))
    shared = sorted(set(stock_returns) & set(index_returns))[-window_weeks:]
    if len(shared) < 3:
        return BetaStats(None, None, None, len(shared), "", "")

    index_days = weekly_closes(index)
    x = [index_returns[key] for key in shared]
    y = [stock_returns[key] for key in shared]
    count = len(shared)
    mean_x = sum(x) / count
    mean_y = sum(y) / count
    sxx = sum((value - mean_x) ** 2 for value in x)
    syy = sum((value - mean_y) ** 2 for value in y)
    sxy = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    if not sxx or not syy:
        return BetaStats(None, None, None, count, index_days[shared[0]][0], index_days[shared[-1]][0])

    slope = sxy / sxx
    r_squared = sxy**2 / (sxx * syy)
    # 斜率的标准误来自残差方差，t 值只作为 note 里的证据强度展示，不当护栏用。
    standard_error = ((syy - slope * sxy) / (count - 2) / sxx) ** 0.5
    t_stat = slope / standard_error if standard_error else None
    return BetaStats(
        beta=slope,
        r_squared=r_squared,
        t_stat=t_stat,
        observations=count,
        first_day=index_days[shared[0]][0],
        last_day=index_days[shared[-1]][0],
    )
