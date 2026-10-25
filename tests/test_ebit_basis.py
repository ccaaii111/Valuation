"""EBIT 的科目口径：营业利润 + 利息费用 − 公允价值变动收益，可跑 plain python。

    python tests/test_ebit_basis.py

这套件钉的是四件事：调整算进比率、算进 `raw.ebit_ltm`（NOPAT/ROIC/隐含 EV/EBITDA 都读它）、
不该动的科目确实没动、凭证把「这一批动了哪两笔」写清楚而 note 里不出现数字。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import _ebit, _ebit_ltm, build_ladder, parse_consensus  # noqa: E402
from backend.model.dcf import evaluate, inputs_from_ladder  # noqa: E402
from fixtures import REVENUE, YI, synthetic_snapshot  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


def ladder(interest: float | None = None, fairvalue: float | None = None, *, income=None,
           anchorless: bool = False) -> dict:
    snapshot = synthetic_snapshot(interest_expense=interest, fairvalue_change=fairvalue)
    if income is not None:
        snapshot["income"] = income(list(snapshot["income"]))
    if anchorless:
        snapshot["consensus"] = parse_consensus({"yctj_list": []})
    built = build_ladder(
        snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.00
    )
    return json.loads(json.dumps(built.to_json(), ensure_ascii=False))


def row(lad: dict, key: str) -> dict:
    return next(r for g in lad["groups"] for r in g["rows"] if r["key"] == key)


def close(value, expected: float, tol: float = 5e-7) -> bool:
    return value is not None and abs(value - expected) < tol


# fixture 的营业利润恒为营收的 30%，2025 年 300 亿、营收 1000 亿。下面全部按这个基准推。
def test_no_caption_falls_back_to_operate_profit() -> None:
    r = row(ladder(), "ebit_margin_ltm")
    check("两项科目都缺失时等于营业利润", close(r["baseline"], 0.30), str(r["baseline"]))
    check("凭证写明未作调整", "未作调整" in r["evidence"], r["evidence"])
    check("来源列是调整后口径", r["source"].startswith("调整后 EBIT"), r["source"])


def test_interest_adds_back() -> None:
    r = row(ladder(interest=8.9), "ebit_margin_ltm")
    check("利息费用加进 EBIT", close(r["baseline"], 308.9 / 1000), str(r["baseline"]))
    check("raw 里的 EBIT 同步", close(r["raw"]["ebit_ltm"], 308.9, 5e-4), str(r["raw"]))
    check("凭证列出加回额", "FE_INTEREST_EXPENSE" in r["evidence"] and "+8.90 亿元" in r["evidence"], r["evidence"])


def test_fairvalue_is_stripped() -> None:
    gain = row(ladder(fairvalue=12.0), "ebit_margin_ltm")
    loss = row(ladder(fairvalue=-12.0), "ebit_margin_ltm")
    check("公允价值变动收益从 EBIT 剔掉", close(gain["baseline"], 288.0 / 1000), str(gain["baseline"]))
    check("公允价值变动损失反向加回", close(loss["baseline"], 312.0 / 1000), str(loss["baseline"]))
    check("凭证按符号写金额", "−12.00 亿元" in gain["evidence"] and "+12.00 亿元" in loss["evidence"],
          f"{gain['evidence']} / {loss['evidence']}")


def test_both_captions_net_out() -> None:
    r = row(ladder(interest=8.9, fairvalue=12.0), "ebit_margin_ltm")
    check("两项同时生效取净额", close(r["baseline"], 296.9 / 1000), str(r["baseline"]))
    check("两条调整都进凭证", "FE_INTEREST_EXPENSE" in r["evidence"] and "FAIRVALUE_CHANGE_INCOME" in r["evidence"],
          r["evidence"])
    check("凭证仍写明不剔投资收益", "投资收益" in r["evidence"] and "不剔除" in r["evidence"], r["evidence"])


def test_captions_we_do_not_touch() -> None:
    def patched(rows):
        return [dict(r, INVEST_INCOME=50 * YI, INVEST_JOINT_INCOME=30 * YI,
                     ASSET_IMPAIRMENT_INCOME=-20 * YI, OTHER_INCOME=15 * YI,
                     FINANCE_EXPENSE=-40 * YI) for r in rows]

    r = row(ladder(income=patched), "ebit_margin_ltm")
    check("投资/减值/其他收益都不动 EBIT", close(r["baseline"], 0.30), str(r["baseline"]))
    check("财务费用净额不加回（只用利息费用）", close(r["baseline"], 0.30), str(r["baseline"]))
    check("没动科目时凭证仍写未作调整", "未作调整" in r["evidence"], r["evidence"])


def test_annual_series_uses_the_same_basis() -> None:
    def patched(rows):
        return [dict(r, FAIRVALUE_CHANGE_INCOME=10 * YI) for r in rows]

    expected = sum((0.30 * v - 10) / v for v in REVENUE.values()) / len(REVENUE)
    r = row(ladder(income=patched), "ebit_margin_hist")
    check("近 5 年序列逐年吃同一调整", close(r["baseline"], expected, 1e-6), f"{r['baseline']} vs {expected}")
    check("均值行与 LTM 行同口径", "调整后 EBIT" in r["source"], r["source"])


def test_note_carries_no_numbers() -> None:
    lad = ladder(interest=8.9, fairvalue=12.0)
    for key in ("ebit_margin_ltm", "ebit_margin_hist", "ebit_margin_consensus", "ebit_margin_start",
                "ebit_margin_path"):
        note = row(lad, key)["note"]
        check(f"{key} 的说明行不含数字", not any(c.isdigit() for c in note), note)


def test_consensus_stays_on_operate_profit() -> None:
    lad = ladder(interest=8.9)
    r = row(lad, "ebit_margin_consensus")
    # fixture 的 2026 预期：营业利润 330 亿 ÷ 营收 1100 亿 = 30%，2027 是 355/1180
    check("一致预期档仍是券商营业利润率", close(r["baseline"]["2026"], 0.30) and close(r["baseline"]["2027"], 355 / 1180),
          str(r["baseline"]))
    check("凭证并列两档并声明不可互为校验", "券商营业利润率均值" in r["evidence"] and "不能互为校验" in r["evidence"],
          r["evidence"])
    start = row(lad, "ebit_margin_start")
    check("首年有锚时站券商口径", close(start["baseline"], 0.30) and "一致预期首年" in start["source"], start["source"])


def test_adjustment_reaches_the_engine() -> None:
    # 有锚时盈利路径站在券商口径上，这一批里 EBIT 率改动只影响 ROIC 与现价反推的退出倍数，
    # 所以按这两个单通道下游验，不比每股——每股那一路还混着债务成本实测。
    addback = ladder(fairvalue=-12.0)
    plain = ladder()
    check("调整后比率流入引擎", close(inputs_from_ladder(addback)["ebit_margin_ltm"], 312.0 / 1000),
          str(inputs_from_ladder(addback)["ebit_margin_ltm"]))
    check("EBIT 抬高则隐含 EV/EBITDA 降低",
          row(addback, "exit_ev_ebitda")["baseline"] < row(plain, "exit_ev_ebitda")["baseline"],
          f"{row(addback, 'exit_ev_ebitda')['baseline']} vs {row(plain, 'exit_ev_ebitda')['baseline']}")
    check("ROIC 读同一个调整后 EBIT", close(row(addback, "roic_ltm")["raw"]["ebit_ltm"], 312.0, 5e-4),
          str(row(addback, "roic_ltm")["raw"]))


def test_anchorless_ladder_prices_the_adjusted_ebit() -> None:
    # 没有任何券商锚点的批次里，调整后 EBIT 率是整条盈利轨道的起点，这时它才该动每股。
    plain = inputs_from_ladder(ladder(anchorless=True))
    addback = inputs_from_ladder(ladder(fairvalue=-12.0, anchorless=True))
    check("无锚时首年率跟着调整后 EBIT 走", close(addback["ebit_margin_start"], 312.0 / 1000),
          str(addback["ebit_margin_start"]))
    fcff = evaluate(addback, "fcff", "gordon")
    base = evaluate(plain, "fcff", "gordon")
    check("无锚批次里 EBIT 率抬高则每股抬高", fcff.per_share > base.per_share, f"{fcff.per_share} vs {base.per_share}")


def test_missing_base_caption() -> None:
    check("单行无营业利润返回 None", _ebit({"TOTAL_OPERATE_INCOME": 100 * YI}) is None)
    check("空行返回 None", _ebit(None) is None)
    check("整表无营业利润返回 None", _ebit_ltm([{
        "REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 100 * YI, "FE_INTEREST_EXPENSE": 5 * YI,
    }]) is None)
    only_interest = _ebit({"TOTAL_OPERATE_INCOME": 100 * YI, "FE_INTEREST_EXPENSE": 5 * YI})
    check("无营业利润时不用利息凭空造 EBIT", only_interest is None, str(only_interest))


def test_consensus_without_forecast() -> None:
    built = ladder(anchorless=True)
    r = row(built, "ebit_margin_consensus")
    check("无锚时凭证直说", r["evidence"] == "本批次无一致预期锚点", r["evidence"])
    check("无锚时首年退回调整后 LTM", "缺锚时用调整后 EBIT LTM" in row(built, "ebit_margin_start")["source"],
          row(built, "ebit_margin_start")["source"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
