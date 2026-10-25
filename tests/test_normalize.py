"""非经常性损益正常化：金额取自官方扣非差额，位置由两条加总恒等式判定，减法落在 EBIT 层。

    python tests/test_normalize.py

钉三层。`normalize_year` 那一层：gross-up 与位置份额各算一遍，六道护栏逐条命中一次，亏损年照样正常化
（只做窄、不许跨过零）。级联那一层：这笔金额真的进了定价的分子，`--no-normalize` 退回报表原值，面板改
营收之后正常化还活着，窗口里有一年没过护栏就整窗不剔。行那一层：「非经常性损益剔除额」是只读参考行、
note 里不出现数字、凭证如实写「按科目楼层分堆，不是逐笔明细」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model import normalize as NZ  # noqa: E402
from backend.model import periods as P  # noqa: E402
from backend.model.assumptions import MARGIN_LOOKBACK, _ebit, build_ladder  # noqa: E402
from backend.model.attribution import flat_rows  # noqa: E402
from backend.model.dcf import ENGINE_INPUTS, apply_overrides, evaluate, inputs_from_ladder  # noqa: E402
from backend.model.derivation import FORMULAS, graph  # noqa: E402
from fixtures import synthetic_ladder, synthetic_snapshot  # noqa: E402

FAILURES = 0
YI = 1e8

# 夹具的 2025 年报行：营收 1000 亿、营业利润 300 亿、利润总额 320 亿、所得税 80 亿（t=25%）、
# 净利润 240 亿、少数股东 2.4 亿（m=10%）、归母 21.6 亿、扣非归母 21.3 亿（S=0.3 亿），
# A 堆只有投资收益 10 亿、B 堆只有营业外收入 20 亿（w=1/3），两条恒等式精确归零。
SNAP = synthetic_snapshot(normalization=True)
BASE = P.annual_rows(SNAP["income"])[2025]

STRIP_YI = 3.0 / 0.675 / 3  # 剔除额（亿元）：S 3 亿 ÷ [(1−0.25)(1−0.10)] × 位置份额 1/3
EBIT_ON_YI = 300.0 - STRIP_YI
MARGIN_ON = round(EBIT_ON_YI / 1000.0, 6)
LADDER_ON = synthetic_ladder(normalize=True)
LADDER_OFF = synthetic_ladder()


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def year_row(year: int = 2025, drop: tuple[str, ...] = (), **yi_fields) -> dict:
    """在夹具的年报行上改几笔金额（亿元），或抽掉几列——每道护栏都要有恰好缺料的那一行。"""
    row = {"REPORT_DATE": f"{year}-12-31"}
    row.update({key: value for key, value in BASE.items() if key != "REPORT_DATE"})
    for key in drop:
        row.pop(key, None)
    for key, value in yi_fields.items():
        row[key] = value * YI
    return row


def verdict(**changes) -> NZ.YearNormalization:
    row = year_row(**changes)
    return NZ.normalize_year(2025, row, _ebit(row))


def close(value, expected: float, tol: float = 1e-6) -> bool:
    return value is not None and abs(value - expected) < tol


def row_of(ladder: dict, key: str) -> dict:
    return next(r for group in ladder["groups"] for r in group["rows"] if r["key"] == key)


# ---------------------------------------------------------------- normalize_year


def test_gross_up_and_position_share() -> None:
    item = NZ.normalize_year(2025, BASE, _ebit(BASE))
    check("夹具行过护栏", item.accepted, item.reason)
    check("S 是归母与扣非归母之差", close(item.non_recurring, 3.0 * YI), str(item.non_recurring))
    check("实际税率取所得税÷利润总额", close(item.tax_rate, 0.25), str(item.tax_rate))
    check("少数股东比例取少数÷净利润", close(item.minority_share, 0.10), str(item.minority_share))
    check("反推额把税后净额还原成税前", close(item.gross, 3.0 / 0.675 * YI), str(item.gross))
    check("位置份额按两堆绝对值算", close(item.position, 1 / 3), str(item.position))
    check("剔除额＝反推额×位置份额", close(item.strip, STRIP_YI * YI), str(item.strip))
    check("剔的是 EBIT 层", close(item.ebit_normalized, EBIT_ON_YI * YI, 1e-3), str(item.ebit_normalized))
    # 这就是 w 存在的理由：不做位置判断会多剔两倍，茅台 2018 那笔捐赠正落在 B 堆。
    check("不做位置判断会多剔两倍", close(item.gross - item.strip, 2 * STRIP_YI * YI, 1e-3), str(item.gross))
    check("两条恒等式精确闭合", close(item.e1_residual, 0.0, 1e-9) and close(item.e2_residual, 0.0, 1e-9),
          f"{item.e1_residual} / {item.e2_residual}")


def test_donation_sits_below_ebit() -> None:
    """按茅台 2018 的比例造一行：偶发主体是 52.7 亿对外捐赠（营业外支出，本来就不在营业利润里）。

    不做位置判断会把 5.66 亿的反推额整个从 EBIT 里扣掉（凭空抬高 1.89%），按 w≈0.019 调整后只动
    0.035%——`normalize.py` 的模块说明与 docs/caliber.md §5.2 都按这一条讲 w 的必要性，所以它得在盘上可跑。
    """
    row = year_row(
        TOTAL_OPERATE_COST=701.0, INVEST_INCOME=1.0, NONBUSINESS_INCOME=0.0, NONBUSINESS_EXPENSE=52.7,
        TOTAL_PROFIT=247.3, INCOME_TAX=61.825, NETPROFIT=185.475, MINORITY_INTEREST=18.5475,
        PARENT_NETPROFIT=166.9275, DEDUCT_PARENT_NETPROFIT=170.7475,
    )
    item = NZ.normalize_year(2025, row, _ebit(row))
    check("捐赠行的两条恒等式闭合", item.accepted, item.reason)
    check("S 就是那笔税后捐赠的净额", close(item.non_recurring, -3.82 * YI), str(item.non_recurring))
    check("位置份额小到一个零头", item.position is not None and item.position < 0.02, str(item.position))
    naive = abs(item.gross) / 300.0 / YI
    adjusted = abs(item.strip) / 300.0 / YI
    check("不做位置判断会凭空抬高 1.9%", close(naive, 0.0189, 5e-4), f"{naive:.4%}")
    check("按 w 调整后只动 0.035%", close(adjusted, 0.00035, 5e-5), f"{adjusted:.4%}")
    check("误差缩小约 50 倍", close(naive / adjusted, 53.6, 2.0), f"{naive / adjusted:.1f}×")


def test_pile_classification() -> None:
    both = NZ.pile({"ASSET_IMPAIRMENT_INCOME": 3 * YI, "ASSET_IMPAIRMENT_LOSS": 5 * YI}, NZ.IN_EBIT_STACK)
    check("孪生只认先出现的一支", both.signed == 3 * YI, str(both))
    only_loss = NZ.pile({"ASSET_IMPAIRMENT_LOSS": 5 * YI}, NZ.IN_EBIT_STACK)
    check("损失支取反号、绝对值照计", close(only_loss.signed, -5 * YI) and close(only_loss.magnitude, 5 * YI),
          str(only_loss))
    joint = NZ.pile({"INVEST_JOINT_INCOME": 4 * YI}, NZ.IN_EBIT_STACK)
    check("「其中：」子项不入堆", joint.magnitude == 0.0, str(joint))
    disposal = NZ.pile({"NONCURRENT_DISPOSAL_LOSS": 2 * YI}, NZ.BELOW_EBIT_STACK)
    check("营业外那堆也带符号", close(disposal.signed, -2 * YI), str(disposal))


def test_identity_must_close() -> None:
    broken = verdict(NONBUSINESS_INCOME=50.0)  # 利润总额没跟着动，E1 差出 30 亿
    check("恒等式不闭合先拒", not broken.accepted and "恒等式不闭合" in broken.reason, broken.reason)
    check("拒的时候把残差写下来", close(broken.e1_residual, 30.0 / 300.0, 1e-9), str(broken.e1_residual))
    near = verdict(NONBUSINESS_INCOME=21.4)  # 残差 1.4÷300＝0.4667%，仍在容差内
    check("容差之内仍认这套分堆", near.accepted, near.reason)
    check("容差就是那条常量", NZ.IDENTITY_TOLERANCE == 0.005, str(NZ.IDENTITY_TOLERANCE))
    missing = year_row(drop=("TOTAL_OPERATE_COST",))
    item = NZ.normalize_year(2025, missing, _ebit(missing))
    check("缺营业总成本时验不了就不剔", not item.accepted and "无法验算" in item.reason, item.reason)


def test_amount_source_must_be_official() -> None:
    for key in ("PARENT_NETPROFIT", "DEDUCT_PARENT_NETPROFIT"):
        item = verdict(drop=(key,))
        check(f"缺 {key} 就拒", not item.accepted and "扣非" in item.reason, item.reason)
    item = verdict(drop=("INCOME_TAX",))
    check("缺所得税反推不了税前", not item.accepted and "所得税费用缺失" in item.reason, item.reason)
    check("这条仍留下 S 供人核对", close(item.non_recurring, 3.0 * YI), str(item.non_recurring))


def test_gross_up_denominator() -> None:
    # 利润总额为 0 又要 E1 闭合，只能让营业外那堆正好抵掉营业利润：300 亿的营业外支出。
    zero_profit = year_row(TOTAL_PROFIT=0.0, NONBUSINESS_INCOME=-300.0)
    item = NZ.normalize_year(2025, zero_profit, _ebit(zero_profit))
    check("利润总额为 0 时不反推", "利润总额或净利润为 0" in item.reason, item.reason)
    check("净利润为零走同一条", "利润总额或净利润为 0" in verdict(NETPROFIT=0.0).reason, verdict(NETPROFIT=0.0).reason)
    check("税盾为负时反推无意义", "不为正" in verdict(INCOME_TAX=400.0).reason, verdict(INCOME_TAX=400.0).reason)
    empty = verdict(NONBUSINESS_INCOME=0.0, INVEST_INCOME=0.0, TOTAL_PROFIT=300.0, TOTAL_OPERATE_COST=700.0)
    check("两堆均为 0 无处安放", "两堆均为 0" in empty.reason, empty.reason)


def test_sign_flip_is_the_only_size_cap() -> None:
    thin = verdict(PARENT_NETPROFIT=816.0, DEDUCT_PARENT_NETPROFIT=213.0)  # S=603 亿 → 剔 297.78 亿
    check("可以把盈利做薄", thin.accepted and 0 < thin.ebit_normalized < 5 * YI, f"{thin.ebit_normalized} {thin.reason}")
    over = verdict(PARENT_NETPROFIT=913.0, DEDUCT_PARENT_NETPROFIT=213.0)  # S=700 亿 → 剔 345.68 亿，反号
    check("不许把营业利润推过零", not over.accepted and "反号或零" in over.reason, over.reason)
    zero_ebit = NZ.normalize_year(2025, year_row(OPERATE_PROFIT=0.0, TOTAL_PROFIT=0.0), 0.0)
    check("营业利润为 0 的年份不剔", not zero_ebit.accepted and "没有营业利润" in zero_ebit.reason, zero_ebit.reason)


def test_loss_year_gets_narrowed() -> None:
    """亏损年照常正常化：t 是税盾、m 是少数股东的亏损份额，分母仍在正常区间，只有跨过零才拦。"""
    row = year_row(
        TOTAL_OPERATE_INCOME=100.0, TOTAL_OPERATE_COST=121.0, OPERATE_PROFIT=-20.0, TOTAL_PROFIT=-22.0,
        INCOME_TAX=-5.5, NETPROFIT=-16.5, MINORITY_INTEREST=-1.65, PARENT_NETPROFIT=-14.85,
        DEDUCT_PARENT_NETPROFIT=-14.55, INVEST_INCOME=1.0, NONBUSINESS_INCOME=-2.0,
    )
    item = NZ.normalize_year(2025, row, _ebit(row))
    check("亏损年过护栏", item.accepted, item.reason)
    check("亏损年的剔除额是负的", item.strip is not None and item.strip < 0, str(item.strip))
    check("亏损被做窄", item.ebit_normalized > item.ebit, f"{item.ebit_normalized} vs {item.ebit}")
    check("窄了但没跨过零", item.ebit_normalized < 0, str(item.ebit_normalized))
    wide = dict(row, PARENT_NETPROFIT=-114.85 * YI)  # S=−100.3 亿 → 剔 −49.5 亿，把亏损做成盈利
    blocked = NZ.normalize_year(2025, wide, _ebit(wide))
    check("窄过头才拦", not blocked.accepted and "反号或零" in blocked.reason, blocked.reason)


def test_window_is_all_or_nothing() -> None:
    good = NZ.normalize_year(2025, BASE, _ebit(BASE))
    bad = NZ.normalize_year(2024, year_row(2024, NONBUSINESS_INCOME=50.0), 300.0 * YI)
    check("一年没过护栏整窗作废", NZ.accepted_window([good, bad]) == [])
    check("全过就整窗留下", len(NZ.accepted_window([good, good])) == 2)
    reason = NZ.window_reason([good, bad])
    check("理由点名是哪一年", reason.startswith("2024："), reason)
    check("可用窗口不给理由", NZ.window_reason([good]) == "", NZ.window_reason([good]))
    check("空窗口也说话", "没有可用的年报窗口" in NZ.window_reason([]), NZ.window_reason([]))


def test_annual_window_is_the_recent_years() -> None:
    rows = [year_row(year) for year in range(2019, 2026)]
    items = NZ.annual_normalizations(rows, _ebit, MARGIN_LOOKBACK)
    check("只取最近五个年报年", [item.year for item in items] == [2021, 2022, 2023, 2024, 2025],
          str([item.year for item in items]))
    check("窗口里每年各剔各的", all(item.accepted for item in items), NZ.window_reason(items))
    check("剔除额逐年各算一遍", close(items[0].strip, STRIP_YI * YI) and close(items[-1].strip, STRIP_YI * YI))


# ----------------------------------------------------------------------- 级联层


def test_the_subtraction_lands_in_pricing() -> None:
    ltm = row_of(LADDER_ON, "ebit_margin_ltm")
    hist = row_of(LADDER_ON, "ebit_margin_hist")
    check("LTM 分子剔过非经常性损益", close(ltm["baseline"], MARGIN_ON), str(ltm["baseline"]))
    check("均值行同口径", close(hist["baseline"], MARGIN_ON), str(hist["baseline"]))
    check("raw 存正常化之后的分子", close(ltm["raw"]["ebit_ltm"], EBIT_ON_YI, 5e-4), str(ltm["raw"]))
    check("ROIC 读同一个正常化分子", close(row_of(LADDER_ON, "roic_ltm")["raw"]["ebit_ltm"], EBIT_ON_YI, 5e-4),
          str(row_of(LADDER_ON, "roic_ltm")["raw"]))
    check("来源列说正常化后 EBIT", ltm["source"].startswith("正常化后 EBIT"), ltm["source"])
    check("均值行来源列同步", hist["source"].startswith("正常化后 EBIT"), hist["source"])
    check("首年那行的退回档跟着改口径", "缺锚时用正常化后 EBIT LTM" in row_of(LADDER_ON, "ebit_margin_start")["source"],
          row_of(LADDER_ON, "ebit_margin_start")["source"])
    on = evaluate(inputs_from_ladder(LADDER_ON)).per_share
    off = evaluate(inputs_from_ladder(LADDER_OFF)).per_share
    check("进定价之后每股值更低", on < off, f"{on} vs {off}")


def test_strip_row_identity() -> None:
    rows = [row for row in flat_rows(LADDER_ON) if row["key"] == "normalization_strip"]
    check("这一行只出现一次", len(rows) == 1, str(len(rows)))
    row = row_of(LADDER_ON, "normalization_strip")
    check("金额就是最近整年那一笔", close(row["baseline"], STRIP_YI, 5e-4), str(row["baseline"]))
    check("单位是亿元", row["unit"] == "亿元", row["unit"])
    check("只读参考行：不在引擎输入里", "normalization_strip" not in ENGINE_INPUTS)
    check("只读参考行：面板上没有输入框", rows[0]["editable"] is False)
    check("note 里不出现数字", not any(char.isdigit() for char in row["note"]), row["note"])
    check("note 说明数已经用在分子里了", "只读参考行" in row["note"], row["note"])
    check("算式写在来源列", row["source"].startswith("(归母净利润 − 扣非归母净利润)"), row["source"])
    check("raw 存未剔的原值", close(row["raw"]["ebit_ltm_reported"], 300.0, 5e-4), str(row["raw"]))


def test_strip_row_evidence() -> None:
    evidence = row_of(LADDER_ON, "normalization_strip")["evidence"]
    check("凭证逐年列剔除额", "2025 剔除 1.48 亿元" in evidence, evidence[:90])
    check("凭证写位置份额", "位置 w 0.333" in evidence, evidence[90:200])
    check("凭证写两条恒等式的残差", "E1 0.0000%" in evidence and "E2 0.0000%" in evidence, evidence[:200])
    check("凭证承认是堆级近似", "不是逐笔明细" in evidence, evidence[-90:])
    check("凭证说明 LTM 不做季度差分", "不是季度差分" in evidence, evidence[-160:])
    ltm = row_of(LADDER_ON, "ebit_margin_ltm")["evidence"]
    check("LTM 行指出用的是哪一年", "LTM 用 2025 年报的 1.48 亿元" in ltm, ltm[-140:])
    check("两行 EBIT 率同指一行", "窗口与残差见「非经常性损益剔除额」行" in ltm)


def test_off_switch_returns_reported_values() -> None:
    ltm = row_of(LADDER_OFF, "ebit_margin_ltm")
    strip = row_of(LADDER_OFF, "normalization_strip")
    check("关掉就没有剔除额", strip["baseline"] is None, str(strip["baseline"]))
    check("关掉时来源列退回调整后", ltm["source"].startswith("调整后 EBIT"), ltm["source"])
    check("关掉时两档比率退回报表原值",
          close(ltm["baseline"], 0.30) and close(row_of(LADDER_OFF, "ebit_margin_hist")["baseline"], 0.30),
          str(ltm["baseline"]))
    check("关掉时 raw 也是原值", close(ltm["raw"]["ebit_ltm"], 300.0, 5e-4), str(ltm["raw"]))
    check("凭证写清是开关关掉的", "--no-normalize" in ltm["evidence"], ltm["evidence"][-90:])
    check("那一行说自己没进定价", "本批次未进定价" in strip["evidence"], strip["evidence"][-60:])
    check("note 也不冒充进了分子", ("没有从" in strip["note"] and "只作对照" in strip["note"]), strip["note"])


def test_one_rejected_year_voids_the_window() -> None:
    """2024 那条恒等式不闭合 → 整窗不剔：两套口径混进同一个五年序列，回落终点就成了无法复现的混合数。"""
    snapshot = synthetic_snapshot(normalization=True)
    annual = P.annual_rows(snapshot["income"])
    broken = dict(annual[2024], NONBUSINESS_INCOME=50.0 * YI)
    snapshot["income"] = [broken if r is annual[2024] else r for r in snapshot["income"]]
    ladder = json.loads(
        json.dumps(build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, beta_manual=1.00).to_json(),
                   ensure_ascii=False)
    )
    ltm = row_of(ladder, "ebit_margin_ltm")
    strip = row_of(ladder, "normalization_strip")
    check("整窗作废后比率是原值", close(ltm["baseline"], 0.30), str(ltm["baseline"]))
    check("作废的理由写进凭证", "未正常化" in ltm["evidence"] and "2024" in ltm["evidence"], ltm["evidence"][-120:])
    check("没剔时来源列不冒充正常化后", ltm["source"].startswith("调整后 EBIT"), ltm["source"])
    check("剔除额那一行也没数", strip["baseline"] is None, str(strip["baseline"]))
    check("那一行的凭证逐年报到", "2024 未正常化" in strip["evidence"], strip["evidence"][:120])


def test_normalization_survives_a_revenue_edit() -> None:
    """面板改营收：分子是存下来的正常化 EBIT，不是报表原值——这条是「正常化进定价」最硬的证据。"""
    out = apply_overrides(inputs_from_ladder(LADDER_ON), {"revenue_ltm": 800.0})
    check("改营收后仍按正常化分子重导", close(out["ebit_margin_ltm"], EBIT_ON_YI / 800.0), str(out["ebit_margin_ltm"]))
    check("没有退回原值", abs(out["ebit_margin_ltm"] - 0.375) > 1e-6, str(out["ebit_margin_ltm"]))


# --------------------------------------------------------------------- 推导图


def test_the_strip_node_is_a_root() -> None:
    built = graph(LADDER_ON)
    node = next(n for n in built["nodes"] if n["key"] == "normalization_strip")
    check("剔除额节点没有上游", node["upstream"] == [] and "normalization_strip" in built["roots"], str(node["upstream"]))
    check("根节点不带式子", node["formula"] == "", node["formula"])
    edges = {edge["to"]: edge for edge in built["edges"] if edge["from"] == "normalization_strip"}
    check("有一条边进 EBIT 率 LTM", "ebit_margin_ltm" in edges, str(sorted(edges)))
    check("有一条边进 EBIT 率均值", "ebit_margin_hist" in edges, str(sorted(edges)))
    check("进 LTM 那条是实线", edges["ebit_margin_ltm"]["live"] is True)
    check("进均值那条是虚线（没有运行时重导函数）", edges["ebit_margin_hist"]["live"] is False)
    check("算式住在来源列而不是式子表", "normalization_strip" not in FORMULAS, str(sorted(FORMULAS)))


if __name__ == "__main__":
    for name in sorted(globals()):
        if name.startswith("test_"):
            print(f"[{name}]")
            globals()[name]()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
