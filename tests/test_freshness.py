"""Per-source freshness thresholds — 纯函数、固定 now、不联网: python tests/test_freshness.py"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import freshness as F  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


BEIJING = timezone(timedelta(hours=8))


def at(text: str) -> datetime:
    """北京时间写测试直观，manifest 记的是 UTC，这里换一次。"""
    naive = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    return naive.replace(tzinfo=BEIJING).astimezone(timezone.utc)


# 2026-09-25 是周五，2026-09-28 是周一
FRIDAY_CLOSE = at("2026-09-25T16:00:00")
MONDAY_MORNING = at("2026-09-28T10:00:00")
MONDAY_AFTER_CLOSE = at("2026-09-28T16:00:00")
TUESDAY_MORNING = at("2026-09-29T09:00:00")
ALL_FRESH = {
    "main_indicators": at("2026-09-20T09:00:00"),
    "balance_sheet": at("2026-09-20T09:00:00"),
    "income_statement": at("2026-09-20T09:00:00"),
    "cash_flow": at("2026-09-20T09:00:00"),
    "consensus": at("2026-09-20T09:00:00"),
    "risk_free": at("2026-09-20T09:00:00"),
    "market_returns": at("2026-09-20T09:00:00"),
    "quote": MONDAY_AFTER_CLOSE,
    "price_history": MONDAY_AFTER_CLOSE,
}
AS_OF_Q2 = "2026-06-30"


def stamps(sources: dict) -> dict[str, str]:
    """时间戳按 manifest 的样子写成字符串；None 留在字典里当「有这一项但没记上时间」。"""
    return {name: value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else "" for name, value in sources.items()}


def rows(sources: dict, now: datetime, as_of: str | None = AS_OF_Q2, **kwargs) -> list[dict]:
    return F.decision(stamps(sources), as_of, now, **kwargs)["stale"]


def groups(sources: dict, now: datetime, as_of: str | None = AS_OF_Q2, **kwargs) -> list[str]:
    return [row["group"] for row in rows(sources, now, as_of, **kwargs)]


def reason(sources: dict, now: datetime, group: str, as_of: str | None = AS_OF_Q2, **kwargs) -> str:
    return next((row["reason"] for row in rows(sources, now, as_of, **kwargs) if row["group"] == group), "")


def test_market_clock_uses_beijing_time_and_the_close() -> None:
    check("UTC 换北京时间", F.market_day(at("2026-09-28T09:00:00")) == date(2026, 9, 28))
    check("UTC 夜里仍是前一天", F.market_day(at("2026-09-28T01:00:00")) == date(2026, 9, 28) and F.market_day(at("2026-09-27T15:00:00")) == date(2026, 9, 27))
    check("naive 输入按 UTC 处理", F.market_day(datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)) == F.market_day(datetime(2026, 9, 27, 23, 0)))
    check("收盘前退回上一交易日", F.latest_close(MONDAY_MORNING) == date(2026, 9, 25), str(F.latest_close(MONDAY_MORNING)))
    check("收盘后算当天", F.latest_close(MONDAY_AFTER_CLOSE) == date(2026, 9, 28), str(F.latest_close(MONDAY_AFTER_CLOSE)))
    check("周六不开盘，仍归周五", F.latest_close(at("2026-09-26T20:00:00")) == date(2026, 9, 25))
    check("周一盘前不算新收盘价", F.latest_close(MONDAY_MORNING) == F.latest_close(FRIDAY_CLOSE))


def test_quote_needs_a_new_close_not_a_new_day() -> None:
    at_friday = dict(ALL_FRESH, quote=FRIDAY_CLOSE)
    check("周五收盘后取过、周一盘前不重取报价", "报价" not in groups(at_friday, MONDAY_MORNING), str(groups(at_friday, MONDAY_MORNING)))
    check("周一收盘后报价过期", "报价" in groups(at_friday, MONDAY_AFTER_CLOSE), str(groups(at_friday, MONDAY_AFTER_CLOSE)))
    check("报价理由说得出哪天到哪天", "2026-09-25" in reason(at_friday, MONDAY_AFTER_CLOSE, "报价") and "2026-09-28" in reason(at_friday, MONDAY_AFTER_CLOSE, "报价"))


def test_weekly_prices_rolling_into_a_new_week() -> None:
    # 9/23 那次取数看到的是 9/22 的收盘，属于 9/21 那一周；本周一盘前最新的周线还是同一根。
    at_last_week = dict(ALL_FRESH, price_history=at("2026-09-23T09:00:00"))
    check("同一交易周内行情不过期", "行情(β)" not in groups(at_last_week, MONDAY_MORNING), str(groups(at_last_week, MONDAY_MORNING)))
    check("跨过周五收盘就过期", "行情(β)" in groups(at_last_week, MONDAY_AFTER_CLOSE), str(groups(at_last_week, MONDAY_AFTER_CLOSE)))
    check("人工给定 β 时不查行情", "行情(β)" not in groups(at_last_week, MONDAY_AFTER_CLOSE, beta_given=True), str(groups(at_last_week, MONDAY_AFTER_CLOSE, beta_given=True)))
    check("β 给定时缺行情凭证也不算缺", "缺取数凭证" not in groups(dict(ALL_FRESH, price_history=None), MONDAY_AFTER_CLOSE, beta_given=True), str(groups(dict(ALL_FRESH, price_history=None), MONDAY_AFTER_CLOSE, beta_given=True)))


def test_statements_wait_for_the_disclosure_deadline() -> None:
    """报告期到了不等于数据公开了：三季报的截止是 10/31，10/20 去重取也是白取。"""
    at_all = dict(ALL_FRESH)
    check("6/30 报告期、10/30 仍不过期", "三表" not in groups(at_all, at("2026-10-30T10:00:00")), str(groups(at_all, at("2026-10-30T10:00:00"))))
    check("10/31 起三季报必须已披露", "三表" in groups(at_all, at("2026-11-02T10:00:00")), str(groups(at_all, at("2026-11-02T10:00:00"))))
    check("年报截至 12/31 → 次年 4/30", "三表" in groups(at_all, at("2026-05-06T10:00:00"), as_of="2025-12-31"))
    check("4/30 之前年报与一季报都还没齐", "三表" not in groups(at_all, at("2026-04-20T10:00:00"), as_of="2025-12-31"))
    check("三表一次点名四个来源", [r for r in rows(at_all, at("2026-11-02T10:00:00")) if r["group"] == "三表"][0]["sources"] == list(F.STATEMENTS))
    check("非常规报告期说得出原因", "标准季末" in reason(at_all, MONDAY_AFTER_CLOSE, "三表", as_of="2026-02-28"))
    check("没有报告期就不编造三表结论", "三表" not in groups(at_all, at("2026-11-02T10:00:00"), as_of=None))


def test_monthly_sources_and_missing_evidence() -> None:
    check("一致预期 31 天后过期", "一致预期" in groups(dict(ALL_FRESH, consensus=at("2026-08-28T09:00:00")), MONDAY_AFTER_CLOSE))
    check("rf 31 天后过期", "无风险利率" in groups(dict(ALL_FRESH, risk_free=at("2026-08-28T09:00:00")), MONDAY_AFTER_CLOSE))
    check("市场收益率 31 天后过期", "市场收益率(ERP)" in groups(
        dict(ALL_FRESH, market_returns=at("2026-08-28T09:00:00")), MONDAY_AFTER_CLOSE))
    check("30 天内不动", "无风险利率" not in groups(dict(ALL_FRESH, risk_free=at("2026-08-29T23:00:00")), MONDAY_AFTER_CLOSE))
    check("缺凭证统一归一组", groups(dict(ALL_FRESH, consensus=None, risk_free=None), MONDAY_AFTER_CLOSE) == ["缺取数凭证"], str(groups(dict(ALL_FRESH, consensus=None, risk_free=None), MONDAY_AFTER_CLOSE)))
    check("这一组点得出缺了哪两个来源", sorted(rows(dict(ALL_FRESH, consensus=None, risk_free=None), MONDAY_AFTER_CLOSE)[0]["sources"]) == ["consensus", "risk_free"])
    no_quote = {k: v for k, v in ALL_FRESH.items() if k != "quote"}
    check("上一批没记报价就无从判断", "缺取数凭证" in groups(no_quote, MONDAY_AFTER_CLOSE), str(groups(no_quote, MONDAY_AFTER_CLOSE)))
    check("缺凭证时点得出是哪个来源", "quote" in rows(no_quote, MONDAY_AFTER_CLOSE)[0]["sources"])
    check("时间戳读不出来也算缺", "缺取数凭证" in groups(dict(ALL_FRESH, quote=None), MONDAY_AFTER_CLOSE))


def test_decision_shape_for_the_button() -> None:
    fresh = F.decision(stamps(ALL_FRESH), AS_OF_Q2, TUESDAY_MORNING)
    check("全部未过期 → 复现快照", fresh["action"] == "replay" and not fresh["stale"], str(fresh["stale"]))
    check("答复里带报告期", fresh["as_of"] == AS_OF_Q2, str(fresh["as_of"]))
    check("复现与取数互斥", "stale_groups" in fresh and fresh["stale_groups"] == [])
    stale = F.decision(stamps(dict(ALL_FRESH, quote=FRIDAY_CLOSE, price_history=FRIDAY_CLOSE)), AS_OF_Q2, at("2026-09-29T16:00:00"))
    check("有任一过期 → 重新取数", stale["action"] == "fetch", stale["action"])
    check("过期理由逐组可读", all(row["reason"] and row["sources"] for row in stale["stale"]), str(stale["stale"])[:80])
    check("stale_groups 与 stale 对齐", stale["stale_groups"] == [row["group"] for row in stale["stale"]])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
