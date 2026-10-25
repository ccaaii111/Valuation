"""折旧摊销的滚动窗口：最新一期没编现金流量表补充资料时，窗口尾巴退到最近真报了数的一期。

    python tests/test_da_window.py

钉三层。期间层：`anchor` 只挪窗口尾巴，不传锚点的老算法逐字不变。级联层：退锚发生在「季报行有营收有
资本开支、唯独四项是空的」这一种形态上，分子分母取同一段十二个月，锚日与算式写进凭证；整张报表都没编
那一段的公司（银行、保险）退无可退，照旧留空。重算层：改营收 LTM 缩放的是那段窗口营收，不是拿全窗营收
去除退锚的分子；老批次没有窗口键就退回原来的读法。

夹具的 quarterly_head 批（tests/fixtures.py）：营收 LTM 1100 亿，折旧摊销窗口 43.2 亿 ÷ 同窗营收 1080 亿
= 4.0%。跨窗口相除（43.2 ÷ 1100）会得 3.9273%——两个数不相等正是本套件要的判别。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model import periods as P  # noqa: E402
from backend.model.assumptions import DA_KEYS, build_ladder  # noqa: E402
from backend.model.dcf import apply_overrides, evaluate, inputs_from_ladder  # noqa: E402
from backend.model.recompute import DERIVE  # noqa: E402
from fixtures import synthetic_ladder, synthetic_snapshot  # noqa: E402

FAILURES = 0
YI = 1e8


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


def row(lad: dict, key: str) -> dict:
    return next(r for g in lad["groups"] for r in g["rows"] if r["key"] == key)


def close(value, expected: float, tolerance: float = 5e-7) -> bool:
    return value is not None and abs(value - expected) < tolerance


def ladder(quarterly_head: bool = False, **kwargs) -> dict:
    return synthetic_ladder(quarterly_head=quarterly_head, **kwargs)


# ---------------------------------------------------------------- 期间层

def test_anchor_only_moves_the_window_tail() -> None:
    rows = synthetic_snapshot(quarterly_head=True)["cashflow"]
    head = P.report_date(P.sort_desc(rows)[0])
    check("最新一期是三季报", head == "2026-09-30", head)
    check("季报行没有补充资料，四项取不出 LTM", P.ltm_sum(rows, DA_KEYS) is None)
    check("锚点是最近真报了数的一期", P.last_reported_day(rows, *DA_KEYS) == "2026-06-30",
          str(P.last_reported_day(rows, *DA_KEYS)))
    anchored = P.ltm_sum(rows, DA_KEYS, anchor="2026-06-30")
    check("退锚之后窗口算得出来（2026H1 22.4 + 2025 年报 40 − 2025H1 19.2）", close(anchored, 43.2 * YI), str(anchored / YI))
    check("空锚点等于不传", P.ltm_sum(rows, DA_KEYS, anchor="") is None)
    later = P.ltm(rows, "CONSTRUCT_LONG_ASSET", anchor="2026-09-30")
    check("锚在最新一期时与不锚同数", close(later, P.ltm(rows, "CONSTRUCT_LONG_ASSET")), str(later))
    earlier = P.ltm_sum(rows, DA_KEYS, anchor="2025-12-31")
    check("锚到更早的年报就取那一年", close(earlier, 40 * YI), str(earlier))
    check("锚日在全部披露行之前就没有窗口", P.ltm_sum(rows, DA_KEYS, anchor="2020-12-31") is None)


def test_last_reported_day_needs_any_one_of_the_four() -> None:
    rows = [{"REPORT_DATE": "2026-06-30", "IA_AMORTIZE": 1.0}, {"REPORT_DATE": "2026-09-30", "FA_IR_DEPR": 2.0}]
    check("报了其中一项就算披露过", P.last_reported_day(rows, *DA_KEYS) == "2026-09-30", str(P.last_reported_day(rows, *DA_KEYS)))
    check("一项都没报就没有锚", P.last_reported_day([{"REPORT_DATE": "2026-09-30"}], *DA_KEYS) is None)
    check("空表不编造日期", P.last_reported_day([], *DA_KEYS) is None)


# ---------------------------------------------------------------- 级联层

def test_retreated_row_uses_the_same_window_denominator() -> None:
    lad = ladder(quarterly_head=True)
    r = row(lad, "da_to_revenue_ltm")
    check("这一行不再留空", r["baseline"] is not None, str(r["baseline"]))
    check("分子分母同窗：43.2 ÷ 1080 = 4.0%", close(r["baseline"], 0.04), str(r["baseline"]))
    check("不是跨窗口相除出来的 3.9273%", not close(r["baseline"], 0.039273, 1e-5), str(r["baseline"]))
    check("资本支出行不受影响（它在季报正文里就报了）", close(row(lad, "capex_to_revenue_ltm")["baseline"], 0.055 / 1.1),
          str(row(lad, "capex_to_revenue_ltm")["baseline"]))
    check("营收 LTM 行仍是全窗那一段 1100 亿", close(row(lad, "revenue_ltm")["baseline"], 1100.0), str(row(lad, "revenue_ltm")["baseline"]))
    check("锚日写进行凭证", "2026-06-30" in r["evidence"] and "补充资料" in r["evidence"], r["evidence"])
    check("凭证点明分母是同一段十二个月", "同一段十二个月" in r["evidence"], r["evidence"])
    check("note 里不出现数字", not any(ch.isdigit() for ch in r["note"]), r["note"])
    check("raw 存了锚日", r["raw"]["da_window"] == "2026-06-30", str(r["raw"]))
    check("raw 存了同窗营收", close(r["raw"]["da_window_revenue"], 1080.0), str(r["raw"]))
    check("raw 存了全窗营收作缩放基准", close(r["raw"]["revenue_ltm"], 1100.0), str(r["raw"]))


def test_head_already_disclosing_keeps_the_plain_roll() -> None:
    """报表截至日就是年报（或最新一期自己编了补充资料）：一个字都不该变，锚点根本不该出现。"""
    plain = ladder()
    r = row(plain, "da_to_revenue_ltm")
    check("年报批仍是 4%", close(r["baseline"], 0.04), str(r["baseline"]))
    check("年报批不写锚日", "da_window" not in r["raw"], str(r["raw"]))
    check("年报批的凭证不提退锚", "补充资料" not in r["evidence"], r["evidence"])
    rows = synthetic_snapshot(quarterly_head=True)["cashflow"]
    without_q3 = [x for x in rows if P.report_date(x) != "2026-09-30"]
    snapshot = synthetic_snapshot(quarterly_head=True)
    snapshot["cashflow"] = without_q3
    snapshot["income"] = [x for x in snapshot["income"] if P.report_date(x) != "2026-09-30"]
    snapshot["balance"] = [x for x in snapshot["balance"] if P.report_date(x) != "2026-09-30"]
    snapshot["as_of"] = "2026-06-30"
    built = build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.00)
    head_is_interim = next(r2 for r2 in built.rows() if r2.key == "da_to_revenue_ltm")
    check("最新一行是中报时窗口本来就算得出来，不必退锚", close(head_is_interim.baseline, 0.04), str(head_is_interim.baseline))
    check("这种批次 raw 里没有锚日", "da_window" not in head_is_interim.raw, str(head_is_interim.raw))
    check("锚日与 P.ltm_sum 直接一致", close(P.ltm_sum(without_q3, DA_KEYS) / YI, 43.2), str(P.ltm_sum(without_q3, DA_KEYS)))


def test_no_supplement_anywhere_stays_blank() -> None:
    """整张现金流量表都没编那一段（银行、保险这类）：退无可退，留空并把原因写进凭证，不拿零冒充。"""
    snapshot = synthetic_snapshot(quarterly_head=True)
    snapshot["cashflow"] = [{k: v for k, v in r.items() if k not in DA_KEYS} for r in snapshot["cashflow"]]
    built = build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.00)
    r = next(x for x in built.rows() if x.key == "da_to_revenue_ltm")
    check("该行仍留空", r.baseline is None, str(r.baseline))
    check("凭证说清是「从未披露」而不是「这次没取到」", "从未披露" in r.evidence, r.evidence)
    check("留空时不写锚日，也不写分母", r.raw == {}, str(r.raw))


# ---------------------------------------------------------------- 重算层

def test_revenue_edit_scales_the_window_not_the_cross_window_division() -> None:
    inputs = inputs_from_ladder(ladder(quarterly_head=True))
    check("基线就是同窗那个 4%", close(inputs["da_to_revenue_ltm"], 0.04), str(inputs["da_to_revenue_ltm"]))
    zero_edits = DERIVE["da_to_revenue_ltm"](inputs)
    check("零改动重导仍是基线（raw 只存到亿元四位，容 1e-5）", close(zero_edits, 0.04, 1e-5), str(zero_edits))
    out = apply_overrides(inputs, {"revenue_ltm": 1210.0})
    check("营收 +10% 缩放的是那段窗口营收：43.2 ÷ 1188", close(out["da_to_revenue_ltm"], round(43.2 / (1080 * 1.1), 6)),
          str(out["da_to_revenue_ltm"]))
    check("不是拿全窗新营收去除退锚分子", not close(out["da_to_revenue_ltm"], round(43.2 / 1210.0, 6), 1e-6),
          str(out["da_to_revenue_ltm"]))
    check("比率被营收改动拖着走（这条边是活的）", out["da_to_revenue_ltm"] < inputs["da_to_revenue_ltm"],
          f"{out['da_to_revenue_ltm']} vs {inputs['da_to_revenue_ltm']}")
    check("资本支出行照旧按全窗营收走", close(out["capex_to_revenue_ltm"], round(55.0 / 1210.0, 6)), str(out["capex_to_revenue_ltm"]))


def test_batch_without_the_window_keys_keeps_the_old_reading() -> None:
    """老批次（raw 只有分子）：读法逐字退回「原分子 ÷ 当前营收 LTM」，不必重跑取数。"""
    inputs = inputs_from_ladder(ladder())
    stripped = {**inputs, "_raw": {**inputs["_raw"], "da_to_revenue_ltm": {"da_ltm": 40.0}}}
    check("无窗口键就按全窗营收相除", close(DERIVE["da_to_revenue_ltm"](stripped), 40.0 / inputs["revenue_ltm"]),
          str(DERIVE["da_to_revenue_ltm"](stripped)))
    missing = {**inputs, "_raw": {**inputs["_raw"], "da_to_revenue_ltm": {}}}
    check("连分子都没有就不重导（留原数）", DERIVE["da_to_revenue_ltm"](missing) is None)


# ---------------------------------------------------------------- 引擎层

def test_the_retreated_number_reaches_the_price() -> None:
    """退锚的意义在于它进定价：同一批证据，比率有与没有，每股必须不同。"""
    inputs = inputs_from_ladder(ladder(quarterly_head=True))
    with_da = evaluate(inputs, "fcff", "gordon")
    without = dict(inputs)
    without["da_to_revenue_ltm"] = None
    baselineless = evaluate(without, "fcff", "gordon")
    check("引擎真的用上了这笔折旧摊销", with_da.per_share != baselineless.per_share,
          f"{with_da.per_share:.2f} vs {baselineless.per_share:.2f}")
    check("补回折旧摊销抬高每股价值（它是 FCFF 的加项）", with_da.per_share > baselineless.per_share,
          f"{with_da.per_share:.2f} vs {baselineless.per_share:.2f}")
    multiple = DERIVE["exit_ev_ebitda"](inputs)
    cleared = DERIVE["exit_ev_ebitda"](without)
    check("隐含 EV/EBITDA 的分母因为这块折旧而变大，倍数因此更低", multiple is not None and cleared is not None
          and multiple < cleared, f"{multiple} vs {cleared}")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
