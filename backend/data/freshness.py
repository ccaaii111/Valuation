"""「上次取数还是最新的吗」——按来源分档的新鲜度规则，纯函数、不联网、可离线测.

一个「取数估值」按钮背后的判断（用户 2026-09-24 定的口径）：后台自己决定这次是复现快照还是重新联网，
依据是上一批各来源的取数时刻和当期报告期。阈值不能一刀切，因为各来源的变动节奏差三个数量级：

    报价      每个交易日收盘后变
    行情(β)   周频，一周多一个收盘点
    三表      一年四次，且要看法定披露截止日而不是报告期本身——报告期到了不代表数据已经公开
    一致预期  滚动修正，按月度容忍
    rf        10Y 序列每天进一位，但 5 年窗口中枢一个月才挪不到 1bp
    市场收益率  指数收盘与股息率每天进一位，但五年窗口的年化超额一个月才挪几个 bp，与 rf 同档

节假日不在这张表里：交易日历要另取一个数据源，而这里的规则本来就允许「一天内少重取一次」，
用工作日近似带来的误差是长假后第一次点按钮会多取一次数，方向上是安全的。

分红方案（第 10 个来源）不在表里，也不进 `evaluate` 的 expected 清单：它只喂永续增速的对照档，缺了估值照跑。
把它算进凭证清单会让每一批老批次一夜之间全部判旧，而判旧的理由是「少了一行不参与定价的参考数」。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

MARKET_UTC_OFFSET = 8  # A 股用北京时间记账，manifest 里是 UTC
CLOSE_HOUR = 15  # 15:00 之后当日收盘才成为「已发生的收盘价」
MONTHLY_DAYS = 30  # 一致预期、rf 与市场收益率的容忍窗口
# 报告期 → 法定披露截止（年报与一季报共用 4/30，所以 12-31 那一档要跨年）
DISCLOSURE_DEADLINE = {3: (4, 30), 6: (8, 31), 9: (10, 31), 12: (4, 30)}
STATEMENTS = ("main_indicators", "balance_sheet", "income_statement", "cash_flow")


@dataclass
class Stale:
    """One reason to go back to the wire, phrased so it can go straight into the banner."""

    group: str  # 报价 / 行情 / 三表 / 一致预期 / 无风险利率 / 缺凭证
    sources: tuple[str, ...]
    reason: str

    def as_row(self) -> dict:
        return {"group": self.group, "sources": list(self.sources), "reason": self.reason}


def parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_day(text: str | None) -> date | None:
    """baseline 里的 as_of 是裸日期（报告期终点），和时间戳不是一种东西。"""
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def market_day(at: datetime) -> date:
    """UTC 取数时刻换回北京时间的日历日。naive 输入按 UTC 处理（manifest 里不会有 naive）。"""
    reference = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return (reference + timedelta(hours=MARKET_UTC_OFFSET)).date()


def last_session(on: date) -> date:
    """On or before ``on``：最近一个工作日（只跳周末，不查节假日）。"""
    day = on
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def latest_close(now: datetime) -> date:
    """已经发生的最新收盘价属于哪一天：当天 15:00 之后算当天，之前退回上一交易日。"""
    today = market_day(now)
    if today.weekday() < 5 and now_hour(now) >= CLOSE_HOUR:
        return today
    return last_session(today - timedelta(days=1))


def now_hour(now: datetime) -> int:
    reference = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (reference + timedelta(hours=MARKET_UTC_OFFSET)).hour


def week_key(day: date) -> tuple[int, int]:
    year, week, _ = day.isocalendar()
    return year, week


def next_period(as_of: date) -> date | None:
    """as_of 之后的下一个报告期终点；不是标准季末（并购重组的非常规截止）时返回 None。"""
    steps = {date(as_of.year, 3, 31): date(as_of.year, 6, 30), date(as_of.year, 6, 30): date(as_of.year, 9, 30),
             date(as_of.year, 9, 30): date(as_of.year, 12, 31), date(as_of.year, 12, 31): date(as_of.year + 1, 3, 31)}
    return steps.get(as_of)


def disclosure_deadline(period: date) -> date:
    """报告期 → 法定披露截止日。"""
    month, day = DISCLOSURE_DEADLINE[period.month]
    year = period.year if period.month != 12 else period.year + 1
    return date(year, month, day)


def source_stamps(manifest: dict | None) -> dict[str, str | None]:
    """manifest 的 sources 是列表（每项带 url 与 sha256），这里只要 name → fetched_at。

    重复的 name 以最后一次为准：同一来源在一次建链里重试过，说明前一次的答复没被采用。
    """
    stamps: dict[str, str | None] = {}
    for entry in (manifest or {}).get("sources") or []:
        name = entry.get("name")
        if name:
            stamps[name] = entry.get("fetched_at")
    return stamps


def evaluate(fetched: dict[str, str | None], as_of: str | None, now: datetime, *, beta_given: bool = False) -> list[Stale]:
    """Which source groups are stale for a 取数 that happens at ``now``.

    ``fetched`` 是 manifest 里的 name → fetched_at；缺一个键或时间戳读不出来，等于那一批没留下这个来源的凭证
    （比如当时用 --beta 跳过了行情），无从判断新旧，只能重取。
    ``beta_given`` 时行情不参与判断：人工给定 β 的这次建链不会用到周频序列。
    """
    stale: list[Stale] = []
    expected = ["quote", *STATEMENTS, "consensus", "risk_free", "market_returns"]
    if not beta_given:
        expected.append("price_history")

    def when(name: str) -> datetime | None:
        return parse(fetched.get(name))

    missing = [name for name in expected if not when(name)]
    if missing:
        # 缺哪几个就点名哪几个：面板把这句挂在状态条的 title 上，看不到它就只剩一句「判不了」。
        stale.append(Stale("缺取数凭证", tuple(sorted(missing)), "上一批的 manifest 里缺 " + "、".join(missing) + " 的时间戳"))

    quote_stamp = when("quote")
    if quote_stamp:
        since = latest_close(quote_stamp)
        until = latest_close(now)
        if until > since:
            stale.append(Stale("报价", ("quote",), f"收盘从 {since} 走到 {until}"))

    if not beta_given:
        stamp = when("price_history")
        if stamp and week_key(latest_close(now)) > week_key(latest_close(stamp)):
            stale.append(Stale("行情(β)", ("price_history",), f"进入新的交易周（{stamp.date()} 之后）"))

    period = parse_day(as_of)
    if period:
        following = next_period(period)
        if following:
            deadline = disclosure_deadline(following)
            if market_day(now) >= deadline:
                stale.append(Stale("三表", STATEMENTS, f"{following} 报告期已过披露截止 {deadline}"))
        else:
            stale.append(Stale("三表", STATEMENTS, f"报告期截至 {period} 不是标准季末，无法推算披露截止"))

    for name, label in (
        ("consensus", "一致预期"),
        ("risk_free", "无风险利率"),
        ("market_returns", "市场收益率(ERP)"),
    ):
        stamp = when(name)
        if stamp and (now - stamp) > timedelta(days=MONTHLY_DAYS):
            stale.append(Stale(label, (name,), f"距上次取数 {(now - stamp).days} 天，超过 {MONTHLY_DAYS} 天"))
    return stale


def decision(fetched: dict[str, str | None], as_of: str | None, now: datetime, *, beta_given: bool = False) -> dict:
    """The one answer the button needs: fetch again, or replay the stored snapshots."""
    stale = evaluate(fetched, as_of, now, beta_given=beta_given)
    return {
        "action": "fetch" if stale else "replay",
        "as_of": as_of,
        "stale": [row.as_row() for row in stale],
        "stale_groups": [row.group for row in stale],
    }
