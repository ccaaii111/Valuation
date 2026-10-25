"""Regression tests for the baseline ladder, runnable with plain python (no pytest required).

    python tests/test_assumptions.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import (  # noqa: E402
    TERMINAL_GROWTH,
    HORIZON_MAX_YEARS,
    HORIZON_MIN_YEARS,
    HORIZON_TARGET_YEARS,
    build_growth_path,
    build_margin_path,
    build_revenue_path,
    confidence_from_coverage,
    glide_diagnostics,
    parse_consensus,
    resolve_horizon,
)
from fixtures import synthetic_ladder  # noqa: E402

EMPTY_CONSENSUS = parse_consensus({})


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0


def test_no_coverage_falls_back_to_history() -> None:
    growth, anchors, horizon = build_growth_path(EMPTY_CONSENSUS, 100.0, 2025, fallback_growth=0.10)
    check("无覆盖时仍生成年份路径", len(growth) > 0, str(sorted(growth)))
    check("无覆盖时不产生锚点", anchors == {})
    check("无覆盖时也按默认显性期走", horizon == HORIZON_TARGET_YEARS, f"horizon={horizon}")
    values = [growth[y] for y in sorted(growth)]
    check("回退路径首年沿用历史CAGR附近", abs(values[0] - 0.10) < 0.03, f"first={values[0]:.4f}")
    check("回退路径单调回落", all(a >= b for a, b in zip(values, values[1:])), str([round(v, 4) for v in values]))
    check("末年收敛于永续增长率", abs(values[-1] - TERMINAL_GROWTH) < 0.02, f"last={values[-1]:.4f}")
    path = build_revenue_path(growth, anchors, 100.0)
    check("回退时营收绝对额仍在增长", len(path) == len(growth) and all(v > 100 for v in path.values()))


def test_no_coverage_without_history() -> None:
    growth, anchors, horizon = build_growth_path(EMPTY_CONSENSUS, None, None, fallback_growth=None)
    check("既无覆盖又无历史时返回空路径而非报错", growth == {} and anchors == {})


def test_consensus_anchor_path() -> None:
    payload = {
        "yctj_list": [
            {"YEAR": 2025, "YEAR_MARK": "A", "TOTAL_OPERATE_INCOME": 1000, "OPERATE_PROFIT": 300},
            {"YEAR": 2026, "YEAR_MARK": "E", "TOTAL_OPERATE_INCOME": 1100, "OPERATE_PROFIT": 320, "EPS": 3.3, "EPS_COUNT": 12, "TOTAL_OPERATE_INCOME_COUNT": 12},
            {"YEAR": 2027, "YEAR_MARK": "E", "TOTAL_OPERATE_INCOME": 1180, "OPERATE_PROFIT": 335, "EPS": 3.5, "EPS_COUNT": 11, "TOTAL_OPERATE_INCOME_COUNT": 11},
        ],
        "yctj_chart": [{"YEAR": 2026, "YEAR_MARK": "E", "PE": 15.2}],
        "jgyc": [
            {"ORG_CODE": "00000000", "ORG_NAME_ABBR": "近六月平均", "YEAR1": 2026, "EPS1": 3.3},
            {"ORG_CODE": "00000011", "ORG_NAME_ABBR": "甲证券", "YEAR1": 2026, "EPS1": 3.0},
            {"ORG_CODE": "00000012", "ORG_NAME_ABBR": "乙证券", "YEAR1": 2026, "EPS1": 3.6},
        ],
    }
    consensus = parse_consensus(payload)
    check("已披露与预测年分离", sorted(consensus["actual"]) == [2025] and sorted(consensus["estimate"]) == [2026, 2027])
    check("伪聚合机构行被剔除", consensus["dispersion"][2026]["n"] == 2, str(consensus["dispersion"].get(2026)))
    check("分歧度只保留预测年", set(consensus["dispersion"]) == {2026}, str(sorted(consensus["dispersion"])))
    check("PE 落到预测年", consensus["estimate"][2026]["pe"] == 15.2)

    growth, anchors, horizon = build_growth_path(consensus, 1000.0, 2025, fallback_growth=0.05)
    check("首个预测年增速按锚点计算", abs(growth[2026] - 0.10) < 1e-9, f"g2026={growth[2026]:.4f}")
    check("锚点年被覆盖", anchors == {2026: 1100, 2027: 1180})
    tail_years = sorted(y for y in growth if y > 2027)
    check("未覆盖年补齐到显性期末", tail_years == list(range(2028, 2028 + horizon - len(anchors))), f"tail={tail_years}")
    check("总预测年数等于显性期", len(growth) == horizon, f"years={len(growth)} horizon={horizon}")
    check("补齐段从末锚点增速起步", growth[tail_years[0]] < growth[2027] or growth[tail_years[0]] == growth[2027])
    check("补齐段收敛于永续", abs(growth[max(growth)] - TERMINAL_GROWTH) < 0.05)

    margins = build_margin_path([2026, 2027, 2028, 2029], {2026: 0.29, 2027: 0.284}, 0.25, 0.29)
    check("利润率路径覆盖年取一致预期", abs(margins[2026] - 0.29) < 1e-9)
    check("未覆盖年向历史均值回归", margins[2029] < margins[2028], f"{ {k: round(v,4) for k,v in margins.items()} }")
    check("回归不超过目标位", all(v >= 0.25 for v in margins.values()), str({k: round(v, 4) for k, v in margins.items()}))


def test_horizon_is_an_input_not_a_by_product_of_coverage() -> None:
    """显性期年数是人的选择；旧口径里它是 max(5, 锚点数 + 2)，券商写到第几年就走第几年、回落段恒为魔法数 2 年。

    三条边界各钉一句：默认 10 年、压在下限与 12 年之间、锚点优先于上限（切掉锚点丢的是那年真实的预期营收，
    不是插值精度）。
    """
    check("不给就用默认 10 年", resolve_horizon(3, None) == (HORIZON_TARGET_YEARS, ""), str(resolve_horizon(3, None)))
    check("10 年在界内就照走", resolve_horizon(0, 10)[0] == 10)
    check("低于准则下限压回 5 年", resolve_horizon(3, 2)[0] == HORIZON_MIN_YEARS, str(resolve_horizon(3, 2)))
    check("低于下限的理由点名是谁压的", "低于下限" in resolve_horizon(3, 2)[1], resolve_horizon(3, 2)[1])
    check("超过上限压到 12 年", resolve_horizon(3, 20)[0] == HORIZON_MAX_YEARS, str(resolve_horizon(3, 20)))
    check("被上限压住时说得出被压了多少", "超过上限" in resolve_horizon(3, 20)[1], resolve_horizon(3, 20)[1])
    # 锚点 11 年 ⇒ 下限 12；填 20 会被上限拦一下，但拦完仍要给锚点留出回落的那一年。
    check("锚点长过上限时锚点赢", resolve_horizon(12, 20)[0] == 13, str(resolve_horizon(12, 20)))
    check("锚点优先时话说在前面", "上限让给锚点" in resolve_horizon(12, 20)[1], resolve_horizon(12, 20)[1])
    check("小数年数收成整数", resolve_horizon(3, 8.6)[0] == 9, str(resolve_horizon(3, 8.6)))

    growth, anchors, horizon = build_growth_path(EMPTY_CONSENSUS, 100.0, 2025, fallback_growth=0.10, horizon_target=7)
    check("显性期跟着输入走", horizon == 7 and len(growth) == 7, f"horizon={horizon} years={len(growth)}")

    fade, last_year = glide_diagnostics(growth, {}, TERMINAL_GROWTH)
    check("每年回落幅度是输出的读法", abs(fade - (TERMINAL_GROWTH - last_year)) < 1e-12, f"{fade} vs {last_year}")
    check("末年离永续还差一步", abs((last_year + fade) - TERMINAL_GROWTH) < 1e-12, f"last={last_year} fade={fade}")
    check("没有回落段就不硬给步长", glide_diagnostics({2026: 0.1}, {2026: 1000.0}, TERMINAL_GROWTH) == (None, None))


def test_confidence_scaling() -> None:
    check("零覆盖不给出置信度", confidence_from_coverage(0) is None)
    check("30家及以上视为满锚", confidence_from_coverage(46) == 1.0)
    check("少量覆盖按比例折减", abs(confidence_from_coverage(6) - 0.2) < 1e-9, str(confidence_from_coverage(6)))


def test_nwc_never_double_counts_combined_caption() -> None:
    """GBALANCE 的"应收票据及应收账款"已包含"应收账款"，相加会把子科目计两次并可能翻转 NWC 正负号。

    数值取自中石化 2026 中报（亿元）：合并科目与子科目重复计入时 NWC 为 −879.89，正确为 +148.82。
    """
    from backend.model.assumptions import _nwc

    row = {
        "NOTE_ACCOUNTS_RECE": 738.09e8,
        "ACCOUNTS_RECE": 738.09e8,
        "INVENTORY": 2233.19e8,
        "NOTE_ACCOUNTS_PAYABLE": 2417.90e8,
        "ACCOUNTS_PAYABLE": 1766.80e8,
        "TAX_PAYABLE": 404.56e8,
    }
    nwc = _nwc(row)
    check("合并科目优先，不重复计子科目", abs(nwc - 148.82e8) < 1e6, f"{nwc / 1e8:,.2f} 亿元")
    check("营运资本符号未被重复计入翻转", nwc > 0, f"{nwc / 1e8:,.2f} 亿元")
    merged = dict(row)
    merged.pop("NOTE_ACCOUNTS_RECE")
    merged.pop("NOTE_ACCOUNTS_PAYABLE")
    check("缺合并科目时回退子科目", _nwc(merged) is not None, str(_nwc(merged)))
    check("空行返回 None", _nwc(None) is None and _nwc({}) is None)


def test_ladder_covers_every_engine_input() -> None:
    """Guards against 假设链 renaming a row while the engine still asks for the old key."""
    from backend.model.dcf import ENGINE_INPUTS, inputs_from_ladder

    ladder = synthetic_ladder()
    flat = inputs_from_ladder(ladder)
    rows = [r for g in ladder["groups"] for r in g["rows"]]
    missing = sorted(ENGINE_INPUTS - set(flat))
    check("引擎输入键全部由假设链提供", not missing, f"缺失 {missing}")
    check("面板可编辑行恰为引擎输入集合", {r["key"] for r in rows} >= set(ENGINE_INPUTS))
    check("增速路径起点取最后年报营收", flat["revenue_base"] == 1000.0, str(flat["revenue_base"]))
    check("每股口径单位为亿股", flat["total_shares"] == 100.0, str(flat["total_shares"]))
    check("退出倍数行可编辑且有值", flat["exit_ev_ebitda"] and flat["pe_ttm"] == 12.5, str(flat["exit_ev_ebitda"]))
    check(
        "滚动市盈率自算：市值 ÷ 归母净利 LTM，不取通道原值 13.5",
        flat["net_profit_ltm"] == 240.0 and flat["market_cap"] == 3000.0 and flat["pe_ttm"] == 12.5,
        f"{flat['market_cap']} / {flat['net_profit_ltm']} = {flat['pe_ttm']}",
    )
    check("无风险利率来源可审计", any(r["source"].startswith("akshare") for r in rows if r["key"] == "risk_free"))
    check("手工惯例值被标注", sum(r["kind"] == "convention" for r in rows) >= 4, str([r["key"] for r in rows if r["kind"] == "convention"]))
    check("每行都有来源", all(r["source"] for r in rows))


ALLOWED_DIGITS = re.compile(r"近 ?\d+ ?年|β≤0")  # 窗口名与 β 的下界是语义的一部分，不算凭证
EVIDENCE_TOKENS = (
    ("%", "百分号"),
    ("取数：", "取数凭证"),
    ("个收盘", "样本量"),
    ("个观测", "观测数"),
    ("R²", "拟合优度"),
    ("t=", "t 值"),
    ("→", "算式箭头"),
    ("年化", "年化字样"),
    ("覆盖机构", "覆盖家数"),
    ("报表截至", "报表日期"),
    ("实测被拒", "护栏判定"),
)


def _rows_of(document: dict) -> list[dict]:
    return document.get("rows") or [r for g in document["groups"] for r in g["rows"]]


def test_notes_state_meaning_while_numbers_live_in_evidence() -> None:
    """2026-10-13 定的全站口径：说明行只写这一行是什么、在级联或引擎里起什么作用。

    这一批的数怎么算出来的——窗口跨度、样本量、算式各段、护栏判定、取数函数与时刻——另立 evidence
    字段，面板把它挂在标签悬浮里，导图收进「凭证」折叠。只要有人把数字回填进 note，本哨兵就变红。
    覆盖数据行/惯例行/降级行三条路径，因为这三条各写各的文案。
    """
    import test_beta as TB
    import test_erp as TE
    import test_rf_normalization as TR

    ladders = [
        ("纯惯例（无快照）", synthetic_ladder()),
        ("ERP 价格+股息腿", TE.ladder(market_stats=TE.stats())),
        ("ERP 全收益腿", TE.ladder(market_stats=TE.stats(tr_annual=0.08))),
        ("ERP 孪生取不到", TE.ladder(market_stats=TE.stats(tr_unavailable="csindex(H00905) 空表"))),
        ("ERP 为负退惯例", TE.ladder(market_stats=TE.stats(annual=0.10, tr_annual=-0.05))),
        ("β 回归实测", TE.ladder(beta_manual=None, prices=TB.price_payload(1.5),
                                 prices_source="akshare sina 行情 @ test")),
        ("rf 多年中枢", TR.ladder(0.0231, TR.window(), "近5年中枢")),
        ("rf 取数失败", TR.ladder(None, None, "")),
    ]

    hits: dict[str, list[str]] = {}
    swept = 0
    for tag, ladder in ladders:
        rows = _rows_of(ladder)
        swept += len(rows)
        for row in rows:
            note = row["note"]
            for token, name in EVIDENCE_TOKENS:
                if token in note:
                    hits.setdefault(name, []).append(f"{tag}／{row['key']}")
            if re.search(r"\d", ALLOWED_DIGITS.sub("", note)):
                hits.setdefault("数字", []).append(f"{tag}／{row['key']}")

    for name in [label for _, label in EVIDENCE_TOKENS] + ["数字"]:
        check(f"{swept} 行的说明里都没有{name}", not hits.get(name), f"出现在 {sorted(set(hits.get(name, [])))}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
