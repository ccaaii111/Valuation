"""M1 重算机器：改一个节点，整条链该跟着动。

图上「改永续增速 → 营收增速路径」明明有线，改了却什么都不发生，是因为这些边只活在取数那一刻。
这张套件钉住三件事：没有改动时一个数都不漂、人工改过的行当常量、被波及的下游按拓扑序当场重导。
最后一节把 runtime/build-time 归属表钉死，将来新加一条派生边却忘了配 derive() 就在此变红。

    python tests/test_recompute.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import DERIVES, HORIZON_MAX_YEARS, HORIZON_MIN_YEARS  # noqa: E402
from backend.model.attribution import attribute  # noqa: E402
from backend.model.dcf import (  # noqa: E402
    DERIVED_INPUTS,
    ENGINE_INPUTS,
    RUNTIME_EDGES,
    apply_overrides,
    evaluate,
    inputs_from_ladder,
)
from fixtures import synthetic_ladder  # noqa: E402
from backend.model.recompute import (  # noqa: E402
    NEEDS_RAW,
    DERIVE,
    _ORDER,
    _depth,
    downstream_of,
    live_rows,
    recompute_derived,
)

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


LADDER = synthetic_ladder()
INPUTS = inputs_from_ladder(LADDER)
GROWTH = "revenue_growth_path"


def row_of(report: dict, key: str) -> dict:
    return next(r for r in report["rows"] if r["key"] == key)


def test_zero_edits_is_identity() -> None:
    """没有人工改动时一个数都不许漂：重算只吃被波及的分支，不吃整张图。"""
    check("空 override 原样返回", apply_overrides(INPUTS, {}) == INPUTS)
    check("recompute_derived 空改动不产新值", recompute_derived(INPUTS, set()) == INPUTS)
    report = attribute(LADDER)
    check("面板上没有任何行被挂上级联牌", not any(r["recomputed"] for r in report["rows"]))
    check("面板上没有任何行算作改动", report["edited_row_count"] == 0)
    check("基线每股值等于引擎直接跑", report["per_share"] == round(evaluate(INPUTS).per_share, 4))


def test_terminal_growth_cascades_into_the_revenue_path() -> None:
    """用户报的那条 bug：改 g 而增速路径不动。"""
    out = apply_overrides(INPUTS, {"terminal_growth": 0.04})
    base_path = INPUTS[GROWTH]
    now_path = out[GROWTH]
    years = sorted(now_path)
    check("年份集合一年不多一年不少", sorted(base_path) == years, f"{years} vs {sorted(base_path)}")
    check("锚点年仍由一致预期说了算", all(now_path[y] == base_path[y] for y in ("2026", "2027")), str(now_path))
    tail = [y for y in years if y not in ("2026", "2027")]
    check("尾段每一年都朝新的 g 挪", all(now_path[y] != base_path[y] for y in tail), str([now_path[y] for y in tail]))
    check("末年确实朝新的 g 同向移动", (now_path[years[-1]] - base_path[years[-1]]) * (0.04 - INPUTS["terminal_growth"]) > 0,
          f"{now_path[years[-1]]} vs 基线 {base_path[years[-1]]}，目标 0.04")
    # 线性回落最后一步只走 tail/(tail+1)，不是直接落到 g，所以拿步长而不是「离目标更近」来断言。
    # 两端都按 6 位小数存过（assumptions 的 pct 与 recompute 的 round），差值就带 1e-6 级的舍入尾巴；
    # 显性期从 5 年拉到 10 年后尾巴年数变多，这条容差必须按存储粒度给，不能按旧的五步给。
    step = len(tail) / (len(tail) + 1.0)
    check("末年的移动量等于 g 的改动乘以回落步长",
          abs((now_path[years[-1]] - base_path[years[-1]]) - (0.04 - INPUTS["terminal_growth"]) * step) < 2e-6,
          f"步长 {step}")
    check("营收绝对额跟着增速走", out["revenue_path"][years[-1]] != INPUTS["revenue_path"][years[-1]],
          f"{out['revenue_path'][years[-1]]}")
    check("EBIT 率路径的年份跟着增速路径", sorted(out["ebit_margin_path"]) == years)
    check("每股值确实被这条链改到了", evaluate(out).per_share != evaluate(INPUTS).per_share,
          f"{round(evaluate(out).per_share, 4)} vs {round(evaluate(INPUTS).per_share, 4)}")


def test_manual_rows_are_constants() -> None:
    """人工点过的行不再被级联覆盖：改了就是当输入用。"""
    out = apply_overrides(INPUTS, {"terminal_growth": 0.04, GROWTH: {"2030": 0.0}})
    check("人工那一年保持人工值", out[GROWTH]["2030"] == 0.0, str(out[GROWTH]))
    check("人工行整行是常量，未点名的年份仍是基线",
          out[GROWTH]["2028"] == INPUTS[GROWTH]["2028"], str(out[GROWTH]))
    check("它的下游按人工值重算", abs(out["revenue_path"]["2030"] - out["revenue_path"]["2029"]) < 5e-4,
          f"{out['revenue_path']['2030']} vs {out['revenue_path']['2029']}")
    report = attribute(LADDER, {"terminal_growth": 0.04, GROWTH: {"2030": 0.0}})
    check("只有人工点的两行算作改动", report["edited_row_count"] == 2, str(report["edited_row_count"]))
    check("增速路径行自己算人工改动", row_of(report, GROWTH)["changed"] and not row_of(report, GROWTH)["recomputed"])
    check("g 行也算人工改动", row_of(report, "terminal_growth")["changed"])
    check("营收绝对额行是被动的那一个", row_of(report, "revenue_path")["recomputed"] is True
          and row_of(report, "revenue_path")["changed"] is False)


def test_capital_rows_move_without_touching_the_revenue_branch() -> None:
    out = apply_overrides(INPUTS, {"market_cap": 6000.0})
    check("权益权重随市值重算", out["equity_weight"] != INPUTS["equity_weight"],
          f"{out['equity_weight']} vs {INPUTS['equity_weight']}")
    check("隐含 EV/EBITDA 随市值重算", out["exit_ev_ebitda"] != INPUTS["exit_ev_ebitda"], str(out["exit_ev_ebitda"]))
    check("净债务不在这条链上，原样不动", out["net_debt"] == INPUTS["net_debt"])
    check("收入分支零漂移", out[GROWTH] == INPUTS[GROWTH] and out["revenue_path"] == INPUTS["revenue_path"])
    check("下游闭包按 DERIVES 走", downstream_of({"market_cap"}) >= {"equity_weight", "exit_ev_ebitda"},
          str(sorted(downstream_of({"market_cap"}))))


def test_two_hop_chain_finishes_in_one_pass() -> None:
    """债务 → 净债务 → 隐含 EV/EBITDA 隔了两跳，一轮拓扑序就要算到底，不能留半截旧值。"""
    out = apply_overrides(INPUTS, {"interest_bearing_debt": 300.0})
    check("净债务先落", out["net_debt"] == round(300.0 - INPUTS["cash_and_financial_assets"], 4), str(out["net_debt"]))
    check("用新净债务算出的 EV/EBITDA 才是面板看到的",
          abs(out["exit_ev_ebitda"] - DERIVE["exit_ev_ebitda"](out)) < 5e-3,
          f"{out['exit_ev_ebitda']} vs {DERIVE['exit_ev_ebitda'](out)}")
    check("权益权重也吃这条债务", out["equity_weight"] == round(3000.0 / (3000.0 + 300.0), 6), str(out["equity_weight"]))
    report = attribute(LADDER, {"interest_bearing_debt": 300.0})
    check("债务改动经级联进折现率", report["rates"]["wacc"][1] != report["rates"]["wacc"][0], str(report["rates"]))
    check("面板把两跳都挂上级联牌",
          {r["key"] for r in report["rows"] if r["recomputed"]} >= {"net_debt", "exit_ev_ebitda"},
          str([r["key"] for r in report["rows"] if r["recomputed"]]))


def test_year_key_shape_follows_the_row() -> None:
    """批次里年份是字符串键，进程内直建的链是整数键；重导不能把键型换掉。"""
    check("JSON 往返的行重导后仍是字符串年份", all(isinstance(k, str) for k in apply_overrides(INPUTS, {"terminal_growth": 0.04})[GROWTH]))
    inline = {**INPUTS, GROWTH: {int(k): v for k, v in INPUTS[GROWTH].items()}}
    out = apply_overrides(inline, {"terminal_growth": 0.04})
    check("整数年份的行重导后仍是整数年份", all(isinstance(k, int) for k in out[GROWTH]), str(sorted(out[GROWTH])[:2]))
    check("两条键型算出的数一致", {int(k): round(v, 6) for k, v in out[GROWTH].items()}
          == {int(k): round(v, 6) for k, v in apply_overrides(INPUTS, {"terminal_growth": 0.04})[GROWTH].items()})


def test_no_anchor_company_still_recomputes() -> None:
    """没有一致预期锚点的公司：增速路径整条从级联 g 落出来，年份仍跟着原行走。"""
    no_anchor = {**INPUTS, "consensus_revenue": {}}
    out = apply_overrides(no_anchor, {"terminal_growth": 0.05})
    check("空锚点也能重导", out[GROWTH] is not None and set(out[GROWTH]) == set(INPUTS[GROWTH]), str(out[GROWTH]))
    check("没有锚点时逐年落到新的 g", all(abs(v - 0.05) < 5e-7 for v in out[GROWTH].values()), str(out[GROWTH]))
    check("营收绝对额按这条平地路径复利", abs(out["revenue_path"]["2027"] - 1000 * 1.05 * 1.05) < 5e-3,
          str(out["revenue_path"]))


def test_the_horizon_row_recuts_the_year_set() -> None:
    """E2：显性期是输入。改它，增速行整段换年数，营收、率路径与那个年数行一起跟上。"""
    base_years = sorted(INPUTS[GROWTH])
    out = apply_overrides(INPUTS, {"horizon_target": 7})
    years = sorted(out[GROWTH])
    check("默认走满 10 年、改 7 年就只剩 7 年", len(base_years) == 10 and len(years) == 7, f"{base_years} → {years}")
    check("锚点年一个不切", {"2026", "2027"} <= set(years), str(years))
    check("营收绝对额跟着换年", sorted(out["revenue_path"]) == years, str(sorted(out["revenue_path"])))
    check("EBIT 率路径跟着换年", sorted(out["ebit_margin_path"]) == years)
    check("显性年数那一行当场跟上，不留旧计数", out["horizon_years"] == 7, str(out["horizon_years"]))
    check("每股值确实被年数改到了", evaluate(out).per_share != evaluate(INPUTS).per_share,
          f"{round(evaluate(out).per_share, 4)} vs {round(evaluate(INPUTS).per_share, 4)}")
    check("填得比默认窄到下限以下时按下限走", len(apply_overrides(INPUTS, {"horizon_target": 2})[GROWTH]) == HORIZON_MIN_YEARS)
    check("填得比上限还长时按上限走", len(apply_overrides(INPUTS, {"horizon_target": 20})[GROWTH]) == HORIZON_MAX_YEARS)
    kept = apply_overrides(INPUTS, {"horizon_target": INPUTS["horizon_target"]})
    check("年数没动时这条链一个数都不漂", sorted(kept[GROWTH]) == base_years
          and all(abs(kept[GROWTH][y] - INPUTS[GROWTH][y]) < 1e-6 for y in base_years),
          str({y: round(kept[GROWTH][y] - INPUTS[GROWTH][y], 9) for y in base_years}))
    report = attribute(LADDER, {"horizon_target": 7})
    check("面板把增速行挂上级联牌", row_of(report, GROWTH)["recomputed"] and not row_of(report, GROWTH)["changed"])
    check("面板上改的只有显性期那一行", report["edited_row_count"] == 1, str(report["edited_row_count"]))


def test_a_new_wider_horizon_lets_the_human_pin_a_year_that_did_not_exist() -> None:
    """同一次改动里既拉年数又点某一年：那一年得有个落点，不能被判成「基线里没有这个年度」。"""
    out = apply_overrides(INPUTS, {"horizon_target": 12, GROWTH: {"2037": 0.0}})
    check("新长出来的年份收下了人工值", out[GROWTH]["2037"] == 0.0, str(sorted(out[GROWTH])))
    check("其余年份是 12 年那条回落曲线，不是 10 年的曲线拼一截", len(out[GROWTH]) == 12 and out[GROWTH]["2028"] != INPUTS[GROWTH]["2028"],
          str(out[GROWTH]["2028"]))
    check("它的下游按 12 年重导", sorted(out["revenue_path"]) == sorted(out[GROWTH]))
    check("年数没变时基线外的年份照样被拒", _raises(lambda: apply_overrides(INPUTS, {GROWTH: {"2036": 0.0}})))


def _raises(call) -> bool:
    try:
        call()
    except ValueError:
        return True
    return False


def test_a_legacy_batch_without_the_horizon_row_keeps_its_years() -> None:
    """老批次的基线没有「显性预测期」那一行：退回旧规则，年数一字不变，历史批次不许被现在重排。"""
    stored = {key: value for key, value in INPUTS.items() if key != "horizon_target"}
    out = apply_overrides(stored, {"terminal_growth": 0.04})
    check("缺那一行时按已存年集重导", sorted(out[GROWTH]) == sorted(stored[GROWTH]),
          f"{sorted(out[GROWTH])} vs {sorted(stored[GROWTH])}")
    check("那一行不在链上时改它被拒（不是被忽略）", _raises(lambda: apply_overrides(stored, {"horizon_target": 7})))
    check("老批次的年数行仍跟着增速行走", out["horizon_years"] == len(stored[GROWTH]), str(out["horizon_years"]))
    # 实测盘上最老的两个批次（600519 20260921-1448/1450）就长这样：链上没有「最后年报营收」那一行。
    no_base = {**stored, "revenue_growth_path": {"2026": 0.032, "2027": 0.05, "2028": 0.049, "2029": 0.041, "2030": 0.033},
               "consensus_revenue": {"2026": 1775.44, "2027": 1864.7, "2028": 1955.45}, "revenue_base": None}
    kept = apply_overrides(no_base, {"terminal_growth": 0.03})
    check("缺首年基数时这条边不通：一年都不许多，也不许少", kept[GROWTH] == no_base[GROWTH], str(sorted(kept[GROWTH])))
    check("年数行也跟着停在原处", kept["horizon_years"] == 5, str(kept["horizon_years"]))


def test_registry_declares_every_edge_it_can_honour() -> None:
    """runtime/build-time 归属表：能当场算的边进 DERIVE，领不到原料的不许声明是活的。"""
    check("每张可重导的行都在 derives 里声明过上游", set(DERIVE) <= set(DERIVES), str(sorted(set(DERIVE) - set(DERIVES))))
    check("级联折现率仍由引擎持有，不进这张表", not (set(DERIVE) & DERIVED_INPUTS), str(sorted(set(DERIVE) & DERIVED_INPUTS)))
    check("归属表 = 重导行 + 引擎两行", RUNTIME_EDGES == frozenset(DERIVE) | DERIVED_INPUTS, str(sorted(RUNTIME_EDGES)))
    for key, parents in DERIVES.items():
        if key not in DERIVE:
            continue
        missing = [p for p in parents if p not in INPUTS]
        check(f"{key} 的每个上游都是链上的行", not missing, str(missing))
    display_only = {"revenue_path", "revenue_cagr_5y", "ebit_margin_start", "horizon_years"}
    check("重导行要么进引擎，要么是只喂链上别的行的中间行", set(DERIVE) <= ENGINE_INPUTS | display_only,
          str(sorted(set(DERIVE) - ENGINE_INPUTS - display_only)))
    check("revenue_path 只是给人看的显示行，不进引擎输入", "revenue_path" not in ENGINE_INPUTS)
    check("显性年数也只是计数：不进引擎输入，改了它不动结果", "horizon_years" not in ENGINE_INPUTS)
    for key in ("revenue_cagr_5y", "ebit_margin_start"):
        fed = [child for child, parents in DERIVES.items() if key in parents]
        check(f"{key} 不进引擎输入，但确实喂给增速/率路径", bool(fed), str(fed))
    check("每张行的深度不为负", all(_depth(k) >= 0 for k in DERIVE))
    index = {key: i for i, key in enumerate(_ORDER)}
    check("增速路径排在营收绝对额之前", index[GROWTH] < index["revenue_path"], str(_ORDER))
    check("增速路径排在 EBIT 率路径之前", index[GROWTH] < index["ebit_margin_path"], str(_ORDER))
    check("净债务排在隐含 EV/EBITDA 之前", index["net_debt"] < index["exit_ev_ebitda"], str(_ORDER))
    check("LTM EBIT 率排在首年率之前", index["ebit_margin_ltm"] < index["ebit_margin_start"], str(_ORDER))
    check("首年率排在率路径之前", index["ebit_margin_start"] < index["ebit_margin_path"], str(_ORDER))
    check("CAGR 排在增速路径之前", index["revenue_cagr_5y"] < index[GROWTH], str(_ORDER))
    cyclic = [k for k in DERIVE if _depth_ok(k)]
    check("整张重导子图无环", len(cyclic) == len(DERIVE), str(sorted(set(DERIVE) - set(cyclic))))


def _depth_ok(key: str) -> bool:
    try:
        _depth(key)
    except ValueError:
        return False
    return True


def test_build_time_edges_stay_out_of_the_runtime_machine() -> None:
    """这些边的原料连 raw 都存不下（一组窗口、三种情形、一条差分），只许虚着。

    `horizon_years` 从前也在这张名单里（年数是锚点覆盖广度的副产品，行里读不出来）；显性期改成输入之后
    它就是增速行覆盖的年份数，原料全在链上，画成虚线反而是骗人的线——所以它搬进了 DERIVE。
    """
    build_only = ["risk_free", "revenue_ltm", "terminal_growth", "terminal_growth_central", "terminal_growth_optimistic",
                  "nwc_to_revenue_delta", "anchor_source", "consensus_dispersion"]
    for key in build_only:
        check(f"{key} 不在可重导集合里", key not in DERIVE)
    check("显性年数跟着增速行走", "horizon_years" in DERIVE)
    check("改无风险利率不重导中枢/即期两行", apply_overrides(INPUTS, {"risk_free": 0.03})["risk_free_norm"] == INPUTS["risk_free_norm"])
    check("改 g 不重导中枢/乐观两行",
          apply_overrides(INPUTS, {"terminal_growth": 0.04})["terminal_growth_central"] == INPUTS["terminal_growth_central"])


def test_ltm_ratios_follow_the_revenue_edit() -> None:
    """M2：改 LTM 营收，三个 LTM 比率与 EBIT 率都该按存下的分子重算——分子活在报表，raw 是它们唯一的出路。"""
    new_revenue = 800.0
    out = apply_overrides(INPUTS, {"revenue_ltm": new_revenue})
    for row, numerator in (("ebit_margin_ltm", "ebit_ltm"), ("da_to_revenue_ltm", "da_ltm"), ("capex_to_revenue_ltm", "capex_ltm")):
        top = INPUTS["_raw"][row][numerator]
        check(f"{row} 按原分子÷新分母重导", out[row] == round(top / new_revenue, 6), f"{out[row]} vs 原 {INPUTS[row]}")
        check(f"{row} 挂上级联牌", row_of(attribute(LADDER, {"revenue_ltm": new_revenue}), row)["recomputed"])
    check("折旧/资本开支的历史均值行不动（那是另一条差分，没有原料）", out["da_to_revenue_hist"] == INPUTS["da_to_revenue_hist"])
    check("增速路径不被营收 LTM 牵动（它吃的是年报口径）", out[GROWTH] == INPUTS[GROWTH])


def test_margin_start_falls_back_to_the_revenue_ltm_edit() -> None:
    """没锚点的公司：首年率就是 LTM 率。LTM 率被营收改动拖着走了，首年率不许还留在旧数上。"""
    no_margin_anchor = {**INPUTS, "ebit_margin_consensus": {}, "consensus_revenue": {}}
    check("无锚时首年率等于 LTM 率", DERIVE["ebit_margin_start"](no_margin_anchor) == no_margin_anchor["ebit_margin_ltm"])
    out = apply_overrides(no_margin_anchor, {"revenue_ltm": 800.0})
    check("改营收后首年率跟着新的 LTM 率走", out["ebit_margin_start"] == out["ebit_margin_ltm"],
          f"{out['ebit_margin_start']} vs {out['ebit_margin_ltm']}")
    check("率路径首年也吃到这个新数", out["ebit_margin_path"]["2026"] > INPUTS["ebit_margin_path"]["2026"],
          f"{out['ebit_margin_path']['2026']} vs 基线 {INPUTS['ebit_margin_path']['2026']}")
    with_consensus = {**INPUTS, "ebit_margin_consensus": {"2027": 0.28}}
    check("首年缺预期时退回 LTM 而不是次年", DERIVE["ebit_margin_start"](with_consensus) == with_consensus["ebit_margin_ltm"],
          str(DERIVE["ebit_margin_start"](with_consensus)))


def test_debt_edit_reprices_the_cost_of_debt() -> None:
    """利息费用 LTM 存进 raw 后，改有息负债就能真的换掉 WACC 里那条债务腿。"""
    ladder = synthetic_ladder(interest_expense=7.0)
    rich = inputs_from_ladder(ladder)
    check("基线用掉的就是实测值", rich["cost_of_debt_pretax"] != 0.031, str(rich["cost_of_debt_pretax"]))
    check("原料齐的行才算活边", live_rows(rich) == frozenset(DERIVE) - {"revenue_cagr_5y"},
          str(sorted(frozenset(DERIVE) - live_rows(rich))))  # 样本只到 5 个年报，CAGR 端点凑不齐
    top = rich["_raw"]["cost_of_debt_pretax"]["interest_ltm"]
    prior = rich["_raw"]["cost_of_debt_pretax"]["prior_debt"]
    debt = rich["interest_bearing_debt"]
    out = apply_overrides(rich, {"interest_bearing_debt": debt + 100})
    expected = round(top / ((debt + 100 + prior) / 2), 6)
    check("债务成本按新分母重导", out["cost_of_debt_pretax"] == expected, f"{out['cost_of_debt_pretax']} vs {expected}")
    check("债务改动经原料进到 WACC", row_of(attribute(ladder, {"interest_bearing_debt": debt + 100}), "wacc")["recomputed"])
    check("债务改动也重导了成本这一行", row_of(attribute(ladder, {"interest_bearing_debt": debt + 100}), "cost_of_debt_pretax")["recomputed"])
    zeroed = apply_overrides(rich, {"interest_bearing_debt": 0.0})
    check("债务归零时护栏把这一行退回惯例值", zeroed["cost_of_debt_pretax"] == 0.031, str(zeroed["cost_of_debt_pretax"]))


def test_tax_and_cash_edit_move_roic() -> None:
    out = apply_overrides(INPUTS, {"tax_rate_ltm": 0.15})
    raw = INPUTS["_raw"]["roic_ltm"]
    invested = raw["total_equity"] + INPUTS["interest_bearing_debt"] - INPUTS["cash_and_financial_assets"]
    check("ROIC 用新税率重算", out["roic_ltm"] == round(raw["ebit_ltm"] * 0.85 / invested, 6), f"{out['roic_ltm']} vs 基线 {INPUTS['roic_ltm']}")
    check("ROIC 挂上级联牌", row_of(attribute(LADDER, {"tax_rate_ltm": 0.15}), "roic_ltm")["recomputed"])


def test_cagr_endpoint_edit_repins_the_fallback_anchor() -> None:
    """改「最后年报营收」把 CAGR 一起拖走：终点是那一行，起点是 raw 里存的首年。"""
    inputs = {**INPUTS, "_raw": {**INPUTS["_raw"], "revenue_cagr_5y": {"first_year": 2020, "first_revenue": 800.0, "last_year": 2025}}}
    out = apply_overrides(inputs, {"revenue_base": 1000.0})
    check("端点齐了才声明这条边活着", out["revenue_cagr_5y"] == round((1000.0 / 800.0) ** 0.2 - 1.0, 6), str(out["revenue_cagr_5y"]))
    no_history = {**INPUTS, "consensus_revenue": {}, GROWTH: {y: 0.06 for y in INPUTS[GROWTH]}}
    check("样本不足六年的批次不重导 CAGR", DERIVE["revenue_cagr_5y"](no_history) is None)


def test_batch_without_raw_keeps_every_number_put_where_it_was() -> None:
    """M2 之前存的老批次：缺原料就一个数都不动，且 live_rows 少掉那六行——图上它们只能是虚线。"""
    legacy = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in legacy["groups"]:
        for row in group["rows"]:
            row.pop("raw", None)
    inputs = inputs_from_ladder(legacy)
    check("老批次读出来没有原料", inputs["_raw"] == {})
    check("老批次里活边只剩不吃原料的那些", live_rows(inputs) == frozenset(DERIVE) - frozenset(NEEDS_RAW), str(sorted(live_rows(inputs))))
    for key, value in apply_overrides(inputs, {"revenue_ltm": 800.0, "interest_bearing_debt": 300.0, "tax_rate_ltm": 0.15}).items():
        if key in NEEDS_RAW:
            check(f"老批次 {key} 停在基线", value == inputs[key], f"{value} vs {inputs[key]}")
    check("老批次仍然重导增速路径（那条边从来不吃原料）",
          apply_overrides(inputs, {"terminal_growth": 0.04})[GROWTH] != inputs[GROWTH])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
