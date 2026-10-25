"""分红取数与读法：第 10 个来源自己的测试——不联网，akshare 从不被 import。

为什么要有这个文件：级联侧的 `test_terminal_growth.py` 用的是它自己造的 `payout_dict()` 桩，
测的是「级联怎么读分红」，不是「分红怎么被读出来」。桩与 `payouts()` 之间本来没有任何约束——
把每 10 股忘了除以 10、把分母换成中报每股收益，23 套件照样全绿，而凭证里的数已经全是错的。
这里钉的就是解析器本身。

    python tests/test_dividends.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 借 test_terminal_growth 的桩做同形检查

from backend.data import dividends as D  # noqa: E402
from backend.data import freshness as F  # noqa: E402
from backend.data import fundamentals, prices  # noqa: E402

FAILURES = 0
BEIJING = timezone(timedelta(hours=8))


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def row(period: str, per_ten: float | None, eps: float | None = None,
        shares: float = 1_000_000_000.0, progress: str = "实施分配") -> dict:
    """一行「已清洗」的分红记录，字段名照东财个股分红页的中文列。"""
    return {
        "报告期": period,
        "现金分红-现金分红比例": per_ten,
        "每股收益": eps,
        "总股本": shares,
        "方案进度": progress,
    }


def payload(*rows: dict, unavailable: str = "") -> dict:
    return {"source": f"akshare {D.FUNC}", "url": D.SOURCE_URL, "rows": list(rows), "unavailable": unavailable}


class StubFrame:
    """只长 `to_dict("records")` 的表——真 pandas 帧在这里多余，装个鸭子就够。"""

    def __init__(self, records: list[dict]) -> None:
        self.records = records

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self.records


def with_fetch_frame(replacement):
    """把 `prices.fetch_frame` 换成桩，跑完还回去（dividends 模块按属性查找，不缓存引用）。"""
    original = prices.fetch_frame
    prices.fetch_frame = replacement
    return original


# --- 快照键与通道 -------------------------------------------------------------------


def test_the_source_is_the_tenth_and_it_is_named_right() -> None:
    """改名会让 manifest 与复现对不上；走 akshare 而不是东财 REST 通道也是口径的一部分。"""
    check("快照键为 dividends", D.RAW_NAME == "dividends", D.RAW_NAME)
    check("键在 fundamentals 的取数清单里", D.RAW_NAME in fundamentals.RAW_NAMES, str(fundamentals.RAW_NAMES))
    check("清单是七份原始响应", len(fundamentals.RAW_NAMES) == 7, str(len(fundamentals.RAW_NAMES)))
    check("走 akshare 的个股分红函数", D.FUNC == "stock_fhps_detail_em", D.FUNC)


# --- _clean：NaN 不进 JSON，日期收成 ISO，文本凭证原样留着 ----------------------------------


def test_clean_normalizes_gaps_dates_and_numbers() -> None:
    cleaned = D._clean(
        {
            "报告期": "2024-12-31 00:00:00",
            "现金分红-现金分红比例": "276.73",
            "每股收益": 68.64,
            "预案公告日": "NaT",
            "股权登记日": None,
            "除权除息日": "nan",
            "送转股份-送股比例": "None",
            "现金分红-现金分红比例描述": "10派276.73元(含税,扣税后249.06元)",
            "是否特批": True,
        }
    )
    check("带时间的日期收成裸日期", cleaned["报告期"] == "2024-12-31", str(cleaned["报告期"]))
    check("数字字符串收成 float", cleaned["现金分红-现金分红比例"] == 276.73, str(cleaned["现金分红-现金分红比例"]))
    check("float 原样", cleaned["每股收益"] == 68.64)
    for column in ("预案公告日", "股权登记日", "除权除息日", "送转股份-送股比例"):
        check(f"{column} 的空值归 None", cleaned[column] is None, repr(cleaned[column]))
    check("方案描述原样留着", cleaned["现金分红-现金分红比例描述"].startswith("10派276.73元"),
          cleaned["现金分红-现金分红比例描述"])
    # 东财不给布尔列，但这条守卫是有意的：布尔值走 float() 会变成 1.0，被当成一笔分红计进比率。
    check("布尔不当数用", cleaned["是否特批"] == "True", repr(cleaned["是否特批"]))
    check("清洗后的载荷能进 JSON", "NaN" not in str(D._clean({"报告期": "2024-12-31", "每股收益": float("nan")})))


# --- payouts：三条读法各拦一类错数 --------------------------------------------------


def test_two_plans_in_one_fiscal_year_are_summed() -> None:
    """茅台 2024 的真数：三季报那笔 238.82 加年报 276.73，每 10 股要先除以 10。

    2024 年起中期分红常态化，一年两笔是常态。只认年报那一行会把半个财年的钱整段漏掉。
    """
    result = D.payouts(
        payload(
            row("2024-09-30", 238.82, 48.42, 1_256_197_800.0),
            row("2024-12-31", 276.73, 68.64, 1_256_197_800.0),
        )
    )
    year = result["years"][2024]
    check("同年两笔相加并折成每股", abs(year["dps"] - 51.555) < 1e-9, str(year["dps"]))
    check("分红率按全年算", abs(year["payout"] - 51.555 / 68.64) < 1e-12, str(year["payout"]))
    check("两笔都记进计划数", year["plans"] == 2, str(year["plans"]))
    check("没漏掉中期那一笔", year["dps"] > 276.73 / 10, str(year["dps"]))


def test_the_denominator_only_comes_from_the_annual_row() -> None:
    """中报／三季报的每股收益是年初至今累计口径，配同年一整年的派息会出 200% 量级。

    这一条最狠的地方在于它会「自己变合理」：拿 2024 三季报的 48.42 当分母，51.555÷48.42=106.5%，
    看着像特别分红而不像错数，所以断言里连 `special` 一起钉住。
    """
    result = D.payouts(
        payload(
            row("2024-09-30", 238.82, 48.42),
            row("2024-12-31", 276.73, 68.64),
        )
    )
    year = result["years"][2024]
    check("分母是年报每股收益", year["eps"] == 68.64, str(year["eps"]))
    check("分红率低于 100%", year["payout"] < 1.0, str(year["payout"]))
    check("没被误标成特别分红", year["special"] is False, str(year["special"]))
    # 分母换成中期那行（48.42）会报出 106.5%，看着像特别分红而不像错数：这条把两种读法隔开。
    interim_denominator = year["dps"] / 48.42
    check("认错分母就会跨过 100%", interim_denominator > 1.0 > year["payout"], f"{interim_denominator:.4f}")


def test_plans_that_were_never_paid_are_skipped() -> None:
    """「预披露」「股东大会通过」都还只是许诺，钱没离开公司就不算分红率。"""
    result = D.payouts(payload(row("2025-12-31", 100.0, 10.0, progress="股东大会通过")))
    check("未实施的年度不进 years", 2025 not in result["years"], str(sorted(result["years"])))
    check("未实施要说明为什么", result["skipped"] == [{"year": 2025, "reason": "有分红方案但未实施"}],
          str(result["skipped"]))
    check("整年都没派过就报不可用", result["available"] is False and "已实施" in result["unavailable"],
          result["unavailable"])


def test_a_year_with_only_an_interim_plan_is_unreadable_not_zero() -> None:
    """年报还没披露、只派过中期那一笔：那是「读不出来」，不是「这一年零分红」。"""
    result = D.payouts(payload(row("2025-09-30", 239.57, 51.53)))
    check("当年不进 years", 2025 not in result["years"], str(sorted(result["years"])))
    check("理由点名分母", result["skipped"][0]["reason"] == "缺年报每股收益，分母对不上", str(result["skipped"]))
    check("零分红和读不出不是一回事", "每股收益" in result["skipped"][0]["reason"], str(result["skipped"]))


def test_special_dividend_is_flagged_not_capped() -> None:
    """派发超过当年全部每股收益（格力的 2020 年派到 107.8%）整年剔除并记下，不当 100% 截断。

    这一档量的是「利润留存下来能撑起多快增长」，把超派一年当成留存率 0% 是一种伪造。
    夹具是合成数（150÷10÷12 = 125%）；盘上真有这个数：格力电器 000651 批次
    `runs/000651/20261022-174243/raw/dividends.json` 的 2020 年每股派 4.0 元、年报每股收益 3.71 元 → 107.8%。
    那一年在该批窗口（2021～2025）之外，所以它只证明「读得出来」，不参与那一档的中位数。
    """
    result = D.payouts(payload(row("2020-12-31", 150.0, 12.0)))
    year = result["years"][2020]
    check("超过 100% 不截断", abs(year["payout"] - 150.0 / 10 / 12.0) < 1e-12, str(year["payout"]))
    check("超派标成特别分红", year["special"] is True, str(year["special"]))


def test_the_amount_field_is_the_cash_that_left_the_company() -> None:
    """`amount` 目前没有读方，但它进凭证就得算得对：Σ(每笔/10 × 该笔总股本)，单位元。"""
    result = D.payouts(payload(row("2024-09-30", 238.82, 48.42, 1_256_197_800.0),
                               row("2024-12-31", 276.73, 68.64, 1_250_000_000.0)))
    expected = 238.82 / 10 * 1_256_197_800.0 + 276.73 / 10 * 1_250_000_000.0
    check("派息金额按各笔股本", abs(result["years"][2024]["amount"] - expected) < 1e-3,
          str(result["years"][2024]["amount"]))


# --- 缺凭证：空表与老批次都不该抛错 ---------------------------------------------------


def test_the_cascade_stub_still_matches_the_parser() -> None:
    """级联侧的 `payout_dict()` 桩与本模块之间唯一的接缝约束。

    `test_terminal_growth.py` 不吃 `payouts()` 的真实输出，它自己造形状——所以解析器哪天改字段名或
    少给一个键，那边不会红。这一条把两边对齐：顶层四键、年度条目每键，桩都必须照着改。
    """
    from test_terminal_growth import payout_dict

    real = D.payouts(payload(row("2024-12-31", 276.73, 68.64)))
    stub = payout_dict({2024: 0.5})
    check("顶层键一致", sorted(real) == sorted(stub), f"{sorted(real)} vs {sorted(stub)}")
    check("年度条目键一致", sorted(real["years"][2024]) == sorted(stub["years"][2024]),
          f"{sorted(real['years'][2024])} vs {sorted(stub['years'][2024])}")
    check("skipped 的条目形状一致", real["skipped"] == [] and stub["skipped"] == [], "两边都空，改形状要一起改")
    # 桩把 payout>1 标成 special 靠的是 `> 1.0`，与解析器同一条判据：换判据要两边一起换。
    parser_special = D.payouts(payload(row("2020-12-31", 150.0, 12.0)))["years"][2020]["special"]
    stub_special = payout_dict({2020: 1.5})["years"][2020]["special"]
    check("超派的判据同源", parser_special and stub_special, f"{parser_special} / {stub_special}")


def test_a_company_that_never_paid_is_a_plain_no() -> None:
    """有这张表但一行都没有＝查不到分红数据，与「压根没取过这张表」是两句不同的话。"""
    for label, body in (("只有空行", {"rows": []}),
                        ("源答没有记录", payload(unavailable="旧批次里那句取数失败: TypeError: 'NoneType' object"))):
        result = D.payouts(body)
        check(f"{label}：不可用", result["available"] is False)
        check(f"{label}：理由统一成那一句", result["unavailable"] == D.NO_RECORD, result["unavailable"])
        check(f"{label}：原始报错串不上面板", "TypeError" not in result["unavailable"], result["unavailable"])
        check(f"{label}：没有年度也没有跳过", result["years"] == {} and result["skipped"] == [], str(result))


def test_an_old_batch_without_the_source_reads_as_no_evidence() -> None:
    """2026-10-16 之前的批次连 dividends.json 都没有——`payloads.get` 给的是 None，不是空 dict。"""
    for label, body in (("None", None), ("空 dict", {})):
        result = D.payouts(body)
        check(f"{label} → 本批没有分红凭证", result["available"] is False and "没有分红凭证" in result["unavailable"],
              result["unavailable"])


# --- fetch：它失败只失败在自己身上 ----------------------------------------------------


def test_fetch_never_raises_when_the_wire_fails() -> None:
    """其余九个来源在这里会一路把建链打死；dividends 只把「查不到」写进载荷，报错串另存一个不读的键。"""
    def boom(func, **kwargs):
        raise prices.PricesUnavailable(f"{func} 取数失败: ConnectionError: 连接被重置")

    original = with_fetch_frame(boom)
    try:
        fetched = D.fetch("600519")
    finally:
        prices.fetch_frame = original
    check("取数失败不抛错，返回的还是一个来源", fetched.name == D.RAW_NAME, fetched.name)
    check("载荷上的理由是那一句现成的话", fetched.payload["unavailable"] == D.NO_RECORD,
          fetched.payload["unavailable"])
    check("失败时 rows 为空", fetched.payload["rows"] == [], str(fetched.payload["rows"]))
    check("原始报错仍然留在盘上（只给修数据的人看）", "ConnectionError" in fetched.payload["fetch_error"],
          fetched.payload["fetch_error"])
    check("载荷仍然有 sha256 与 url", len(fetched.sha256) == 64 and fetched.url.startswith("https://"), fetched.url)
    check("读法把它认成缺分红数据，而不是缺凭证", D.payouts(fetched.payload)["unavailable"] == D.NO_RECORD,
          D.payouts(fetched.payload)["unavailable"])


def test_fetch_stores_the_whole_table_not_a_precomputed_ratio() -> None:
    """窗口从 5 年换 7 年、读法改一条，都不该重新联网——凭证要回答「取到了什么」。"""
    records = [{"报告期": "2024-12-31 00:00:00", "现金分红-现金分红比例": 276.73, "每股收益": 68.64,
                "总股本": 1_256_197_800.0, "方案进度": "实施分配", "现金分红-股息率": 0.0221,
                "现金分红-现金分红比例描述": "10派276.73元(含税)"}]
    original = with_fetch_frame(lambda func, **kwargs: StubFrame(records))
    try:
        fetched = D.fetch("600519")
    finally:
        prices.fetch_frame = original
    stored = fetched.payload["rows"][0]
    check("整张表原样落盘（含描述列）", "现金分红-现金分红比例描述" in stored, str(sorted(stored)))
    check("不落算好的比率", "payout" not in fetched.payload, str(sorted(fetched.payload)))
    check("日期在落盘前就收成了裸日期", stored["报告期"] == "2024-12-31", stored["报告期"])
    check("取数来源与页面都记着", fetched.payload["source"].startswith("akshare") and fetched.payload["url"],
          str(fetched.payload))


# --- 不进新鲜度清单：这条口径决定老批次会不会一夜全部判旧 -------------------------------


ALL_FRESH = {
    "main_indicators": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "balance_sheet": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "income_statement": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "cash_flow": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "consensus": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "risk_free": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "market_returns": datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
    "quote": datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc),
    "price_history": datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc),
}
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)


def stamps(sources: dict) -> dict[str, str]:
    return {name: value.strftime("%Y-%m-%dT%H:%M:%SZ") for name, value in sources.items()}


def test_a_missing_dividend_record_does_not_make_a_batch_stale() -> None:
    """用户 2026-10-16 定的口径：dividends 不进 `evaluate` 的 expected 清单。

    把它算进凭证清单，盘上每一批老批次都会因为「少了一行不参与定价的参考数」被判重取。
    """
    without = F.decision(stamps(ALL_FRESH), "2026-06-30", NOW)
    with_stamp = F.decision(stamps(dict(ALL_FRESH, dividends=datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc))),
                            "2026-06-30", NOW)
    stale_names = {name for row_ in F.evaluate(stamps(ALL_FRESH), "2026-06-30", NOW) for name in row_.sources}
    check("缺 dividends 也判复现", without["action"] == "replay", str(without))
    check("有 dividends 时间戳同样判复现", with_stamp["action"] == "replay", str(with_stamp))
    check("dividends 不在任何 stale 组里", D.RAW_NAME not in stale_names, str(stale_names))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
