"""Fiscal period handling.

Eastmoney statement rows carry cumulative year-to-date figures (Q1/H1/Q9) plus full-year
annual rows, so any growth or margin work must go through here first.
"""
from __future__ import annotations

from typing import Iterable

PERIOD_BY_MD = {"03-31": "Q1", "06-30": "H1", "09-30": "Q9", "12-31": "FY"}


def report_date(row: dict) -> str:
    return str(row.get("REPORT_DATE") or "")[:10]


def period_of(row: dict) -> str | None:
    return PERIOD_BY_MD.get(report_date(row)[5:])


def fiscal_year(row: dict) -> int | None:
    date = report_date(row)
    return int(date[:4]) if len(date) >= 10 else None


def sort_desc(rows: Iterable[dict]) -> list[dict]:
    return sorted(rows, key=report_date, reverse=True)


def annual_rows(rows: Iterable[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in rows:
        if period_of(row) == "FY":
            year = fiscal_year(row)
            if year:
                out.setdefault(year, row)
    return out


def interim_rows(rows: Iterable[dict]) -> dict[int, dict]:
    """One non-annual row per fiscal year, keeping the most advanced period reported."""
    out: dict[int, dict] = {}
    for row in sort_desc(rows):
        period, year = period_of(row), fiscal_year(row)
        if period and period != "FY" and year and year not in out:
            out[year] = row
    return out


def latest(rows: Iterable[dict]) -> dict | None:
    ordered = sort_desc(list(rows))
    return ordered[0] if ordered else None


def num(row: dict | None, *keys: str) -> float | None:
    if not row:
        return None
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def last_reported_day(rows: Iterable[dict], *keys: str) -> str | None:
    """最靠后的一个「这几个科目报了数字」的报告期日期，用来把十二个月的窗口尾巴锚过去。"""
    for row in sort_desc(list(rows)):
        if num(row, *keys) is not None:
            return report_date(row)
    return None


def ltm(rows: Iterable[dict], *keys: str, anchor: str = "") -> float | None:
    """Latest twelve-month value: last annual figure, or interim rolled onto prior annual.

    `anchor` 把窗口尾巴从最新那一行挪到指定报告期——现金流量表的补充资料（折旧摊销四项）只在
    中报与年报里编，最新一行是季报时得往前找一期，否则整条滚动序列会断在没有披露的那一格。
    """
    ordered = sort_desc(list(rows))
    if anchor:
        ordered = [row for row in ordered if report_date(row) <= anchor]
    if not ordered:
        return None
    head = ordered[0]
    period, year = period_of(head), fiscal_year(head)
    if period == "FY":
        return num(head, *keys)
    if period is None or year is None:
        return None
    prior_annual = next((r for r in ordered if fiscal_year(r) == year - 1 and period_of(r) == "FY"), None)
    prior_same = next((r for r in ordered if report_date(r) == f"{year - 1}-{head['REPORT_DATE'][5:10]}"), None)
    current_ytd = num(head, *keys)
    annual = num(prior_annual, *keys)
    comparable_ytd = num(prior_same, *keys)
    if current_ytd is None or annual is None or comparable_ytd is None:
        return None
    return current_ytd + annual - comparable_ytd


def same_quarter_last_year(rows: Iterable[dict]) -> dict | None:
    """The balance-sheet row for the same quarter-end one year earlier, or None if never reported."""
    ordered = sort_desc(list(rows))
    if not ordered:
        return None
    head, tail = ordered[0], report_date(ordered[0])[5:10]
    year = fiscal_year(head)
    if year is None or not tail:
        return None
    return next((row for row in ordered if report_date(row) == f"{year - 1}-{tail}"), None)


def twelve_months_ago(rows: Iterable[dict], *keys: str) -> float | None:
    """Balance-sheet item at the same quarter-end one year earlier, for a point-in-time LTM delta."""
    prior = same_quarter_last_year(rows)
    return num(prior, *keys) if prior else None


def ltm_sum(rows: Iterable[dict], keys: Iterable[str], anchor: str = "") -> float | None:
    """Sum of several line items on a trailing-twelve-month basis."""
    parts = [ltm(rows, key, anchor=anchor) for key in keys]
    present = [p for p in parts if p is not None]
    return sum(present) if present else None


def annual_series(rows: Iterable[dict], *keys: str, years: int = 10) -> dict[int, float]:
    series = {year: num(row, *keys) for year, row in annual_rows(rows).items()}
    filled = {year: value for year, value in series.items() if value is not None}
    return dict(sorted(filled.items(), reverse=True)[:years])


def growth(series: dict[int, float]) -> dict[int, float]:
    ordered = sorted(series.items(), reverse=True)
    out: dict[int, float] = {}
    for (year, value), (_, prior) in zip(ordered, ordered[1:]):
        if prior not in (None, 0):
            out[year] = value / prior - 1.0
    return out


def cagr(series: dict[int, float], periods: int) -> float | None:
    ordered = sorted(series.items())
    if len(ordered) < periods + 1:
        return None
    (first_year, first), (_, last) = ordered[-(periods + 1)], ordered[-1]
    span = ordered[-1][0] - first_year
    if first <= 0 or last <= 0 or span <= 0:
        return None
    return (last / first) ** (1.0 / span) - 1.0


def mean(values: Iterable[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def stdev(values: Iterable[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    avg = sum(clean) / len(clean)
    return (sum((v - avg) ** 2 for v in clean) / (len(clean) - 1)) ** 0.5
