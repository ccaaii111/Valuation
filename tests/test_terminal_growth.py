"""Tests for the measured perpetual growth rate: 保守情形进级联，中枢/乐观/再投资并列.

No network — the scenarios come off hand-built revenue dicts and the toy company's fixture history.

    python tests/test_terminal_growth.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import (  # noqa: E402
    GROWTH_MIN_OBSERVATIONS,
    SUSTAINABLE_MIN_YEARS,
    TERMINAL_GROWTH,
    annual_revenue_growth,
    build_growth_path,
    build_ladder,
    pick_terminal_growth,
    roe_by_year,
    sustainable_growth_stats,
    terminal_growth_rows,
    terminal_growth_scenarios,
)
from backend.model.attribution import attribute  # noqa: E402
from backend.model.dcf import ENGINE_INPUTS, evaluate, inputs_from_ladder  # noqa: E402
from fixtures import synthetic_snapshot  # noqa: E402
from test_dcf import base_inputs  # noqa: E402

FAILURES = 0
YI = 1e8


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def close(a: float | None, b: float, tol: float = 1e-9) -> bool:
    return a is not None and abs(a - b) < tol


def rev(amounts: dict[int | str, float]) -> dict[int, float]:
    """Year -> 营收（亿元），换算成引擎吃的元。"""
    return {int(year): value * YI for year, value in amounts.items()}


def payout_dict(payouts: dict[int, float]) -> dict:
    """按 `dividends.payouts` 的形状造一份分红读数：级联只读 payout，其余字段是给文案备着的。"""
    return {
        "available": True,
        "unavailable": "",
        "years": {
            year: {"payout": value, "dps": value, "eps": 1.0, "amount": None, "plans": 1,
                   "special": value > 1.0}
            for year, value in payouts.items()
        },
        "skipped": [],
    }


def ladder(**kwargs) -> dict:
    """Round-trip through JSON so year keys are strings, exactly like a stored baseline.json."""
    snapshot = synthetic_snapshot()
    # 本套件只测 g；没有价格快照时 β 必须显式给（引擎不再有占位值）。
    kwargs.setdefault("beta_manual", 1.00)
    document = build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, **kwargs).to_json()
    return json.loads(json.dumps(document, ensure_ascii=False))


def row_of(document: dict, key: str) -> dict:
    rows = document.get("rows") or [r for group in document["groups"] for r in group["rows"]]
    return next(row for row in rows if row["key"] == key)


# ---------------------------------------------------------------- 增速序列与三情形

def test_growth_series_only_spans_contiguous_years() -> None:
    gaps = annual_revenue_growth({2020: 100.0, 2021: 120.0, 2023: 180.0})
    check("缺年报的年份不进序列", list(gaps) == [2021], str(gaps))
    check("相邻两年才算增速", close(gaps[2021], 0.2))
    check("零或负营收不作分母", annual_revenue_growth({2019: 0.0, 2020: 100.0}) == {})


def test_central_is_the_pooled_growth_not_an_average_of_ratios() -> None:
    # 2021 年断崖：算术平均把它摊成 −11.5%，加权（增量合计/基数合计）留住了 −28%。
    stats = terminal_growth_scenarios(rev({2019: 1000, 2020: 1100, 2021: 200, 2022: 210, 2023: 220, 2024: 230}))
    check("中枢 = 近五年增量合计 ÷ 基数合计", close(stats["central"], -770.0 / 2730.0), f"{stats['central']:.6f}")
    check("中枢不等于算术平均", abs(stats["central"] + 0.11501) > 1e-3, f"{stats['central']:.4%}")
    check("中枢年数按观测数", stats["central_years"] == 5)
    check("保守取最差年之一半", close(stats["conservative"], (200 / 1100 - 1.0) / 2.0), f"{stats['conservative']:.4%}")
    check("保守指向最差那一年", stats["worst_year"] == 2021)
    check("乐观取全史最好", close(stats["optimistic"], 0.10) and stats["best_year"] == 2020)
    check("最差窗口跨度可展示", stats["worst_span"] == "2020~2024", stats["worst_span"])


def test_worst_window_looks_at_the_ten_most_recent_growth_years() -> None:
    # 2011 年的 −60% 落在十年窗口之外：保守情形只该看到窗口内的 2024 年 −2%。
    amounts = {2010: 1000.0, 2011: 400.0}
    for year in range(2012, 2024):
        amounts[year] = 400.0 + (year - 2011) * 40.0
    amounts[2024] = amounts[2023] * 0.98
    stats = terminal_growth_scenarios(rev(amounts))
    check("观测数等于连续年份个数", stats["observations"] == 14, str(stats["observations"]))
    check("窗口外的暴跌不算保守情形", stats["worst_year"] == 2024, str(stats["worst_year"]))
    check("保守 = 窗口内最差之半", close(stats["conservative"], -0.01), f"{stats['conservative']:.4%}")
    check("十年窗口从 2015 起算", stats["worst_span"] == "2015~2024", stats["worst_span"])
    check("乐观仍可看全史", stats["best_year"] == 2012 and close(stats["optimistic"], 0.10), f"{stats['best_year']}")


def test_short_history_falls_back_with_a_reason() -> None:
    thin = terminal_growth_scenarios(rev({2023: 100, 2025: 120}))
    check("观测不足即标记不可用", GROWTH_MIN_OBSERVATIONS == 3 and thin["unavailable"], str(thin["unavailable"]))
    value, basis, measured = pick_terminal_growth(thin, None, "conservative")
    check("测不出来就退回惯例值", value == TERMINAL_GROWTH and not measured, f"{value} {basis}")
    check("退回原因挂在基准名上", "惯例" in basis and "不可用" in basis, basis)
    empty = terminal_growth_scenarios({})
    check("零观测同样不抛异常", empty["unavailable"] and empty["central"] is None, str(empty))
    document = ladder()
    driver = row_of(document, "terminal_growth")
    check("玩具公司四年观测足够，不必退回", driver["kind"] == "data" and driver["baseline"] > 0, driver["kind"])
    sparse = json.loads(json.dumps(terminal_growth_scenarios(rev({2023: 100, 2025: 120})), ensure_ascii=False))
    check("不可用时三行为空但仍挂在链上", all(
        row_of(document, key)["baseline"] is not None for key in ("terminal_growth_central", "terminal_growth_optimistic")
    ) and sparse["unavailable"], str(sparse))


# ---------------------------------------------------------------- 基准选择

def scenario() -> dict:
    """2021 年腰斩再腰斩的样本：保守与中枢两档都测成负的，正好用来验兜底。"""
    return terminal_growth_scenarios(rev({2019: 1000, 2020: 1100, 2021: 200, 2022: 210, 2023: 220, 2024: 230}))


def healthy() -> dict:
    """增速全为正、最差年也有 4% 的样本：兜底不该在这里露头。"""
    return terminal_growth_scenarios(rev({2019: 1000, 2020: 1100, 2021: 1150, 2022: 1210, 2023: 1270, 2024: 1340}))


def test_basis_choice_and_manual_override() -> None:
    stats = healthy()
    conservative, label_c, measured = pick_terminal_growth(stats, None, "conservative")
    central, label_m, _ = pick_terminal_growth(stats, None, "central")
    optimistic, label_o, _ = pick_terminal_growth(stats, None, "optimistic")
    convention, label_v, convention_measured = pick_terminal_growth(stats, None, "convention")
    check("保守进级联", close(conservative, stats["conservative"]) and "保守" in label_c and measured, label_c)
    check("实测为正时不抬", conservative > 0 and "兜底" not in label_c, label_c)
    check("中枢可换", close(central, stats["central"]) and "中枢" in label_m, label_m)
    check("乐观可换", close(optimistic, stats["optimistic"]) and "乐观" in label_o, label_o)
    check("惯例可换", convention == TERMINAL_GROWTH and not convention_measured and "惯例" in label_v, label_v)
    manual, label_x, manual_measured = pick_terminal_growth(stats, 0.03, "conservative")
    check("人工值压过一切情形", manual == 0.03 and "--terminal-growth" in label_x and manual_measured, label_x)
    try:
        pick_terminal_growth(stats, None, "magic")
        check("未知基准要报错", False, "未抛 ValueError")
    except ValueError as exc:
        check("未知基准要报错", "未知永续增速基准" in str(exc), str(exc))


def test_negative_measured_growth_floors_at_zero() -> None:
    """默认档不许给「永续负增长」定价：实测为负就抬到 0，但原值得留在凭证里。"""
    stats = scenario()
    for basis in ("conservative", "central"):
        value, label, measured = pick_terminal_growth(stats, None, basis)
        check(f"{basis} 实测为负即兜底 0", value == 0.0 and measured, f"{value} {label}")
        check(f"{basis} 基准名写明抬之前是多少",
              "兜底 0" in label and f"{stats[basis]:.2%}" in label, label)
    check("样本本身确实测出负值", stats["conservative"] < 0 and stats["central"] < 0,
          f"{stats['conservative']} {stats['central']}")
    optimistic, label_o, _ = pick_terminal_growth(stats, None, "optimistic")
    check("最好的那一年为正就用正的", close(optimistic, stats["optimistic"]) and "兜底" not in label_o, label_o)
    manual, label_x, _ = pick_terminal_growth(stats, -0.02, "conservative")
    check("人工负值不抬：改了就是当常量用", manual == -0.02 and "兜底" not in label_x, f"{manual} {label_x}")
    convention, _, _ = pick_terminal_growth(stats, None, "convention")
    check("惯例值不受兜底影响", convention == TERMINAL_GROWTH, str(convention))
    thin = terminal_growth_scenarios(rev({2023: 100, 2025: 120}))
    value, label, measured = pick_terminal_growth(thin, None, "conservative")
    check("实测不可用时仍走惯例回退", value == TERMINAL_GROWTH and "惯例" in label and not measured, label)


def test_reference_rows_keep_the_unfloored_measurement() -> None:
    """进级联的那一行兜底，三个参考行不许跟着兜——它们的用处正是显示偏在哪一侧。"""
    stats = scenario()
    value, basis, measured = pick_terminal_growth(stats, None, "conservative")
    rows = {row.key: row for row in terminal_growth_rows(stats, sustainable_growth_stats(None, {}), value, basis, measured)}
    check("级联行取兜底后的 0", rows["terminal_growth"].baseline == 0, str(rows["terminal_growth"].baseline))
    check("中枢行仍是实测负值", close(rows["terminal_growth_central"].baseline, stats["central"], 5e-7),
          str(rows["terminal_growth_central"].baseline))
    check("乐观行仍是实测正值", close(rows["terminal_growth_optimistic"].baseline, stats["optimistic"], 5e-7),
          str(rows["terminal_growth_optimistic"].baseline))
    check("凭证看得见兜底这一步", "兜底 0" in rows["terminal_growth"].evidence, rows["terminal_growth"].evidence)
    check("说明行不复述兜底", "兜底" not in rows["terminal_growth"].note and "%" not in rows["terminal_growth"].note,
          rows["terminal_growth"].note)


# ---------------------------------------------------------------- 第四档：再投资增长率

def test_roe_by_year_only_reads_annual_weighted_rows() -> None:
    rows = [
        {"REPORT_DATE": "2025-12-31", "ROEJQ": 32.53},
        {"REPORT_DATE": "2025-06-30", "ROEJQ": 16.75},  # 中报是累计口径，进来了就把当年折半
        {"REPORT_DATE": "2024-12-31", "ROEJQ": 36.02},
        {"REPORT_DATE": "2023-12-31", "ROEJQ": None},
    ]
    out = roe_by_year(rows)
    check("只取 12-31 那一行", sorted(out) == [2024, 2025], str(out))
    check("百分数换成小数", close(out[2025], 0.3253), str(out.get(2025)))
    check("缺 ROE 的年份不进序列", 2023 not in out, str(out))
    check("没有 main 行也不炸", roe_by_year([]) == {})


def test_sustainable_median_pairs_retention_with_roe() -> None:
    # 五年都留一半利润，ROE 依次 10%~30%：逐年 (1−PO)·ROE = 5%/10%/15%/20%/25%，中位 15%。
    payouts = payout_dict({year: 0.5 for year in range(2021, 2026)})
    roe = {year: 0.10 * (index + 1) for index, year in enumerate(range(2021, 2026))}
    sus = sustainable_growth_stats(payouts, roe)
    check("取的是中位数不是均值", close(sus["value"], 0.15), str(sus["value"]))
    check("逐年按财年对齐", [item["year"] for item in sus["years"]] == [2021, 2022, 2023, 2024, 2025],
          str(sus["years"]))
    check("配对用满窗口", sus["observations"] == 5 and sus["window"] == 5, str(sus["observations"]))
    check("分红率极差留着做证据", close(sus["payout_min"], 0.5) and close(sus["payout_max"], 0.5),
          f"{sus['payout_min']}~{sus['payout_max']}")


def test_sustainable_drops_special_dividend_years() -> None:
    # 格力 2020 年那种：派发 107.8% 每股收益，钱是补往年的，不该摊进这一档。
    payouts = payout_dict({2020: 0.5, 2021: 0.5, 2022: 0.5, 2023: 0.5, 2024: 0.5, 2025: 1.078})
    sus = sustainable_growth_stats(payouts, {year: 0.20 for year in range(2020, 2026)})
    check("超额派发那一年不进中位", all(item["year"] != 2025 for item in sus["years"]), str(sus["years"]))
    check("剔除的理由说得出数", any("特别分红" in item["reason"] and "107.8%" in item["reason"]
                                for item in sus["excluded"]), str(sus["excluded"]))
    check("少一年照样够门槛", sus["observations"] == SUSTAINABLE_MIN_YEARS, str(sus["observations"]))
    check("中位不被特别分红牵着走", close(sus["value"], 0.10), str(sus["value"]))


def test_sustainable_needs_a_stable_series() -> None:
    stats = scenario()
    thin = sustainable_growth_stats(payout_dict({2022: 0.5, 2023: 0.5, 2024: 0.5}), {2022: 0.2, 2023: 0.2, 2024: 0.2})
    check("配得上的年份不足就作废", thin["value"] is None and bool(thin["unavailable"]), str(thin["unavailable"]))
    check("作废的理由报出数量", f"需 ≥{SUSTAINABLE_MIN_YEARS} 个" in thin["unavailable"], thin["unavailable"])
    value, label, measured = pick_terminal_growth(stats, None, "sustainable", thin)
    check("作废时级联退回惯例", value == TERMINAL_GROWTH and not measured and "再增长率不可用" in label, label)

    blank = sustainable_growth_stats(None, {2025: 0.3})
    check("没有分红凭证只作废这一档", blank["value"] is None and "没有分红凭证" in blank["unavailable"],
          blank["unavailable"])
    fallback_value, fallback_label, _ = pick_terminal_growth(stats, None, "sustainable", blank)
    check("回退照样把原因带上路", fallback_value == TERMINAL_GROWTH and "这批没有分红凭证" in fallback_label,
          fallback_label)


def test_sustainable_basis_prices_off_the_median() -> None:
    stats = scenario()
    payouts = payout_dict({year: 0.5 for year in range(2021, 2026)})
    roe = {year: 0.10 * (index + 1) for index, year in enumerate(range(2021, 2026))}
    sus = sustainable_growth_stats(payouts, roe)
    value, label, measured = pick_terminal_growth(stats, None, "sustainable", sus)
    check("换档后级联取的是中位数", close(value, 0.15) and measured, f"{value} {label}")
    check("基准名以「再投资」开头", label.startswith("再投资"), label)
    loss = sustainable_growth_stats(payouts, {year: -0.10 for year in range(2021, 2026)})
    floored, floor_label, floor_measured = pick_terminal_growth(stats, None, "sustainable", loss)
    check("再投资档也兜底在 0", floored == 0.0 and floor_measured and "−5.00%" in floor_label.replace("-", "−"),
          f"{floored} {floor_label}")
    manual, manual_label, _ = pick_terminal_growth(stats, -0.02, "sustainable", loss)
    check("人工值不归这一档管", manual == -0.02 and "人工输入" in manual_label, manual_label)


# ---------------------------------------------------------------- 假设链上的四行

def test_ladder_shares_one_driver_and_three_reference_rows() -> None:
    document = ladder()
    driver = row_of(document, "terminal_growth")
    central = row_of(document, "terminal_growth_central")
    optimistic = row_of(document, "terminal_growth_optimistic")
    # 玩具公司 2021~2025 营收 700/780/860/940/1000：最差是 2025 的 +6.38%，之半 +3.19%；
    # 中枢 = 300/3280 = +9.15%；乐观 = 2022 的 +11.43%。
    check("级联行取保守情形", close(driver["baseline"], 0.031915, 1e-6), str(driver["baseline"]))
    check("级联行由实测驱动", driver["kind"] == "data" and "年报营收增速" in driver["source"], driver["source"])
    check("标签写明级联采用", "级联采用" in driver["label"], driver["label"])
    check("中枢行取加权增速", close(central["baseline"], 0.091463, 1e-6), str(central["baseline"]))
    check("乐观行取历史最好", close(optimistic["baseline"], 0.114286, 1e-6), str(optimistic["baseline"]))
    for row, name in ((central, "中枢"), (optimistic, "乐观"),
                      (row_of(document, "terminal_growth_sustainable"), "再投资")):
        check(f"{name}行不进引擎输入", row["key"] not in ENGINE_INPUTS and "不进级联" in row["note"], row["note"])
    evidence = driver["evidence"]
    check("凭证同时给出三情形", all(token in evidence for token in ("中枢", "保守", "乐观")), evidence)
    check("凭证点名最差那一年", "2025" in evidence and "+6.38%" in evidence, evidence)
    check("凭证给出换基准的出口", "--terminal-growth-basis" in evidence, evidence)
    check("说明只讲作用不复述算式", "%" not in driver["note"] and "折现率" in driver["note"], driver["note"])
    check("乐观行警告会顶穿折现率", "折现率" in optimistic["note"], optimistic["note"])


def test_ladder_keeps_the_fourth_row_visible_when_it_cannot_measure_it() -> None:
    """玩具 fixture 既没有 dividend 凭证也没有 main 的加权 ROE：第四行要空白得说明白，不许塌成 0。

    这就是所有老批次在面板上的样子——用户 2026-10-16 定的口径：缺凭证只作废这一档。
    """
    document = ladder()
    fourth = row_of(document, "terminal_growth_sustainable")
    driver = row_of(document, "terminal_growth")
    check("第四行照样在终值组里", fourth["key"] not in ENGINE_INPUTS and "不进级联" in fourth["note"], fourth["note"])
    check("缺凭证时是空白不是零", fourth["baseline"] is None, str(fourth["baseline"]))
    check("空白的原因写在凭证里", "没有分红凭证" in fourth["evidence"], fourth["evidence"])
    check("级联行的凭证点名第四档", "再投资档不可用" in driver["evidence"], driver["evidence"])
    check("第四行的说明不复述数字", "%" not in fourth["note"] and "ROE" in fourth["note"], fourth["note"])
    check("默认档不受它影响", close(driver["baseline"], 0.031915, 1e-6), str(driver["baseline"]))


def test_ladder_switches_the_cascade_to_the_sustainable_basis() -> None:
    snapshot = synthetic_snapshot()
    snapshot["main"] = [{"REPORT_DATE": f"{year}-12-31", "ROEJQ": 20.0} for year in range(2021, 2026)]
    snapshot["dividends"] = payout_dict({year: 0.5 for year in range(2021, 2026)})
    document = json.loads(
        json.dumps(
            build_ladder(
                snapshot, snapshot["consensus"], snapshot["quote"], 0.02,
                beta_manual=1.00, terminal_basis="sustainable",
            ).to_json(),
            ensure_ascii=False,
        )
    )
    driver = row_of(document, "terminal_growth")
    fourth = row_of(document, "terminal_growth_sustainable")
    # 留存一半 × 加权 ROE 20% = 10%，五年都一样，中位还是 10%。
    check("换档后进级联的是再投资增速", close(driver["baseline"], 0.10, 1e-6), str(driver["baseline"]))
    check("级联行的出处跟着换", "分红方案" in driver["source"] and "加权 ROE" in driver["source"], driver["source"])
    check("级联行说采用再投资档", "再投资" in driver["note"] and "%" not in driver["note"], driver["note"])
    check("第四行的凭证摆出逐年配对", "2021 留存 50.0%×ROE 20.00%→10.00%" in fourth["evidence"], fourth["evidence"])
    check("营收三情形照样并列", close(row_of(document, "terminal_growth_central")["baseline"], 0.091463, 1e-6),
          str(row_of(document, "terminal_growth_central")["baseline"]))


def test_switching_basis_moves_only_the_driver_row() -> None:
    central_run = row_of(ladder(terminal_basis="central"), "terminal_growth")
    convention_run = row_of(ladder(terminal_basis="convention"), "terminal_growth")
    manual_run = row_of(ladder(terminal_growth_manual=0.01), "terminal_growth")
    optimistic_row = row_of(ladder(terminal_basis="optimistic"), "terminal_growth")
    check("换中枢只换级联值", close(central_run["baseline"], 0.091463, 1e-6), str(central_run["baseline"]))
    check("换惯例把 kind 打回 convention",
          convention_run["baseline"] == TERMINAL_GROWTH and convention_run["kind"] == "convention",
          f"{convention_run['baseline']} {convention_run['kind']}")
    check("人工值标进基准名", manual_run["baseline"] == 0.01 and "人工输入" in manual_run["note"], manual_run["note"])
    check("乐观基准同样是实测", optimistic_row["kind"] == "data" and close(optimistic_row["baseline"], 0.114286, 1e-6),
          str(optimistic_row["baseline"]))
    check("参考行不受基准切换影响",
          row_of(ladder(terminal_basis="optimistic"), "terminal_growth_central")["baseline"]
          == row_of(ladder(), "terminal_growth_central")["baseline"])


def test_growth_path_glides_onto_the_driver_growth() -> None:
    no_anchor = {"estimate": {}, "actual": {}, "dispersion": {}}
    default = build_growth_path(no_anchor, 100.0, 2025, 0.10)[0]
    shrinking = build_growth_path(no_anchor, 100.0, 2025, 0.10, -0.14)[0]
    check("回落终点跟 g 走", shrinking[2030] < 0.0 < default[2030] < 0.10, f"{default[2030]:.4f} vs {shrinking[2030]:.4f}")
    path = row_of(ladder(terminal_basis="convention"), "revenue_growth_path")["baseline"]
    conservative_path = row_of(ladder(), "revenue_growth_path")["baseline"]
    # 玩具公司的保守 g（+3.19%）高于惯例 2.5%，所以路径末年也该更高。
    check("面板上的增速路径同步", conservative_path["2030"] > path["2030"],
          f"{conservative_path['2030']} vs {path['2030']}")
    check("路径来源写明回落终点", "永续增速" in row_of(ladder(), "revenue_growth_path")["source"],
          row_of(ladder(), "revenue_growth_path")["source"])


# ---------------------------------------------------------------- 引擎侧后果

def test_engine_treats_a_shrinking_perpetuity_sensibly() -> None:
    gordon = evaluate(base_inputs(terminal_growth=-0.1444), "fcff", "gordon", rate=0.075)
    check("负 g 下 Gordon 照常出数", close(gordon.terminal_value, 225.0 * 0.8556 / 0.2194, 0.01),
          f"{gordon.terminal_value:.2f}")
    uem = evaluate(base_inputs(terminal_growth=-0.1444, roic_ltm=0.04), "fcff", "uem", rate=0.075)
    check("收缩中的公司再投资率为 0", any("再投资率 0.0%" in w for w in uem.warnings), str(uem.warnings))
    check("UEM 终值 = NOPAT(1+g)/(r−g)", close(uem.terminal_value, 225.0 * 0.8556 / 0.2194, 0.01),
          f"{uem.terminal_value:.2f}")
    collapsed = evaluate(base_inputs(terminal_growth=-1.5), "fcff", "gordon")
    check("g ≤ −100% 直接拒绝", collapsed.per_share is None and "−100%" in collapsed.warnings[0], str(collapsed.warnings))


def test_exit_multiple_survives_a_growth_above_the_rate() -> None:
    blocked = evaluate(base_inputs(terminal_growth=0.08, exit_ev_ebitda=5.0), "fcff", "gordon")
    priced = evaluate(base_inputs(terminal_growth=0.08, exit_ev_ebitda=5.0), "fcff", "exit_multiple")
    check("g ≥ r 时 Gordon 无解", blocked.per_share is None and "无意义" in blocked.warnings[0], str(blocked.warnings))
    check("g ≥ r 时退出倍数照常", priced.per_share is not None and close(priced.terminal_value, 1750.0),
          f"{priced.per_share} {priced.terminal_value}")
    check("退出倍数仍警告 g 高于折现率", any("不可比价" in w for w in priced.warnings), str(priced.warnings))


def test_attribution_prices_the_driver_row_only() -> None:
    document = ladder()
    report = attribute(document, {"terminal_growth": 0.025})
    driver = row_of(report, "terminal_growth")
    central = row_of(report, "terminal_growth_central")
    check("基线 g 就是保守情形", close(inputs_from_ladder(document)["terminal_growth"], 0.031915, 1e-6),
          str(inputs_from_ladder(document)["terminal_growth"]))
    check("改 g 被定价", driver["editable"] and driver["changed"] and close(driver["change_pct"], -0.21668, 1e-4),
          f"{driver['change_pct']} {driver['contribution_pct']}")
    check("退回基线可归因", driver["value_if_reverted"] is not None and driver["contribution_pct"] is not None,
          str(driver["contribution_pct"]))
    check("参考行改动不定价", not central["editable"] and not central["changed"], str(central["current"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
