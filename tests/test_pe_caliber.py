"""滚动市盈率的口径钉子：这一行的数自己算，行情通道原值只进凭证。

盘上实测（2026-10-23，67 份存了 `pe_ttm` 与 `market_cap` 的批次，逐批用同批报表滚出 `PARENT_NETPROFIT` LTM 复算）：
腾讯通道 45 份里可算的 41 份有 37 份逐位等于「总市值 ÷ 归母净利润 LTM」、4 份只差末位 0.01；
push2 通道 22 份里可算的 17 份**一份都不等**（差 0.06～18.78，最大 +69.7%）——那个字段是动态市盈率（最新中期归母按 12/月数 年化）。
两条通道各折一个数，同一只票降级就跳数，所以这个缺陷要钉在三个层面上：解析、级联、引擎。

    python tests/test_pe_caliber.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.data import fundamentals as FD  # noqa: E402
from backend.model.assumptions import build_ladder, trailing_pe  # noqa: E402
from backend.model.dcf import apply_overrides, evaluate, inputs_from_ladder  # noqa: E402
from fixtures import YI, synthetic_snapshot  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


# 市值 3000 亿、归母净利 LTM 240 亿（fixtures 那家玩具公司）→ 自算 12.5；两条通道都故意给 13.5 以外的数，
# 好让「采纳原值」与「自算」在断言里分得开。
PUSH2_PAYLOAD = {
    "data": {"f43": 30.0, "f57": "000001", "f58": "测试股份", "f84": 100 * YI, "f116": 3000 * YI,
             "f162": 13.5, "f167": 1.2}
}
TENCENT_TEXT = ["x"] * 50
TENCENT_TEXT[1] = "测试股份"
TENCENT_TEXT[3] = "30.00"
TENCENT_TEXT[39] = "14.20"
TENCENT_TEXT[44] = "2600.00"
TENCENT_TEXT[45] = "3000.00"
TENCENT_TEXT[46] = "1.20"
TENCENT_PAYLOAD = {"fallback": "tencent", "text": "~".join(TENCENT_TEXT)}


def ladder_for(payload: dict) -> dict:
    """同一套报表，只换行情通道——两条通道必须折出同一个市盈率。"""
    snapshot = synthetic_snapshot()
    snapshot["quote"] = FD.parse_quote(payload)
    built = build_ladder(
        snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.00
    )
    return json.loads(json.dumps(built.to_json(), ensure_ascii=False))


def row(ladder: dict, key: str) -> dict:
    return {r["key"]: r for g in ladder["groups"] for r in g["rows"]}[key]


def flat(ladder: dict) -> dict:
    return inputs_from_ladder(ladder)


def test_parse_keeps_the_channel_value_out_of_pe_ttm() -> None:
    for name, parsed, expected, basis in (
        ("push2", FD.parse_quote(PUSH2_PAYLOAD), 13.5, "f162"),
        ("tencent", FD.parse_quote(TENCENT_PAYLOAD), 14.2, "第 39 位"),
    ):
        check(f"{name} 通道只交原值，不产 pe_ttm", "pe_ttm" not in parsed and parsed["pe_channel"] == expected, str(parsed))
        check(f"{name} 通道的口径写在原值旁边", basis in (parsed.get("pe_channel_basis") or ""), str(parsed.get("pe_channel_basis")))
        check(f"{name} 通道带着总市值作分子", parsed["total_market_cap"] == 3000 * YI, str(parsed.get("total_market_cap")))


def test_both_channels_yield_one_number() -> None:
    push2, tencent = ladder_for(PUSH2_PAYLOAD), ladder_for(TENCENT_PAYLOAD)
    check("两条通道同一个滚动市盈率", flat(push2)["pe_ttm"] == flat(tencent)["pe_ttm"] == 12.5,
          f"{flat(push2)['pe_ttm']} vs {flat(tencent)['pe_ttm']}")
    check("分母单独成行，面板能复算", row(push2, "net_profit_ltm")["baseline"] == 240.0
          and row(push2, "market_cap")["baseline"] == 3000.0, str(row(push2, "net_profit_ltm")))
    for name, ladder in (("push2", push2), ("tencent", tencent)):
        evidence = row(ladder, "pe_ttm")["evidence"]
        channel = "13.5" if name == "push2" else "14.2"
        check(f"{name} 的凭证把分子分母与通道原值都写全",
              "3,000.00 亿元" in evidence and "240.00 亿元" in evidence and f"原值 {channel}" in evidence, evidence)


def test_no_number_when_the_denominator_is_not_positive() -> None:
    snapshot = synthetic_snapshot()
    for income in snapshot["income"]:
        income["PARENT_NETPROFIT"] = -50 * YI
    built = json.loads(json.dumps(
        build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.00).to_json(),
        ensure_ascii=False))
    check("亏损年不出数", row(built, "pe_ttm")["baseline"] is None, str(row(built, "pe_ttm")))
    check("原因写在凭证里，且没有回退到通道原值",
          "非正" in row(built, "pe_ttm")["evidence"] and row(built, "net_profit_ltm")["baseline"] == -50.0,
          row(built, "pe_ttm")["evidence"])

    missing_cap = synthetic_snapshot()
    missing_cap["quote"]["total_market_cap"] = None
    built = json.loads(json.dumps(
        build_ladder(missing_cap, missing_cap["consensus"], missing_cap["quote"], 0.02, beta_manual=1.00).to_json(),
        ensure_ascii=False))
    check("缺总市值也不出数", row(built, "pe_ttm")["baseline"] is None
          and "总市值" in row(built, "pe_ttm")["evidence"], row(built, "pe_ttm")["evidence"])


def test_helper_wording_covers_every_guard() -> None:
    quote = {"pe_channel": 13.5, "pe_channel_basis": "test 通道字段", "source": "test"}
    check("分母缺行时说得清缺什么", trailing_pe(3000 * YI, None, quote)[0] is None
          and "滚不出" in trailing_pe(3000 * YI, None, quote)[1], trailing_pe(3000 * YI, None, quote)[1])
    check("通道没给市盈率时凭证不硬凑原值", "未给市盈率" in trailing_pe(3000 * YI, 240 * YI, {"source": "test"})[1],
          trailing_pe(3000 * YI, 240 * YI, {"source": "test"})[1])
    value, evidence = trailing_pe(3000 * YI, 240 * YI, quote)
    check("出数时算式完整", value == 12.5 and "÷" in evidence, evidence)


def test_the_engine_refuses_a_negative_multiple() -> None:
    """旧护栏只拦 None 与 0：负倍数乘负净利会折出一个正终值（盘上 000002、600408、688561 都采到过负值）。"""
    inputs = dict(flat(ladder_for(PUSH2_PAYLOAD)))
    for pe in (-22.87, 0, None):
        inputs["pe_ttm"] = pe
        result = evaluate(inputs, "fcfe", "exit_multiple")
        check(f"退出市盈率 {pe} 不折出终值", result.per_share is None and "非正" in result.warnings[0],
              f"{result.per_share} / {result.warnings[:1]}")
    inputs["pe_ttm"] = 12.5
    check("正倍数照常出数", evaluate(inputs, "fcfe", "exit_multiple").per_share is not None)


def test_editing_the_denominator_reprices_the_ratio() -> None:
    """这一行有上游之后，面板改归母净利或改市值都得跟着动——否则图上那条线是假的。"""
    inputs = flat(ladder_for(PUSH2_PAYLOAD))
    doubled = apply_overrides(inputs, {"net_profit_ltm": 480.0})
    check("归母净利翻倍，市盈率减半", doubled["pe_ttm"] == 6.25, str(doubled["pe_ttm"]))
    halved = apply_overrides(inputs, {"market_cap": 1500.0})
    check("市值减半，市盈率跟着减半", halved["pe_ttm"] == 6.25, str(halved["pe_ttm"]))


SUITES = (
    test_parse_keeps_the_channel_value_out_of_pe_ttm,
    test_both_channels_yield_one_number,
    test_no_number_when_the_denominator_is_not_positive,
    test_helper_wording_covers_every_guard,
    test_the_engine_refuses_a_negative_multiple,
    test_editing_the_denominator_reprices_the_ratio,
)

if __name__ == "__main__":
    for suite in SUITES:
        print(f"\n[{suite.__name__}]")
        suite()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
