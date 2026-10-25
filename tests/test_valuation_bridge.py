"""估值段（图上最后几跳）的账：它必须只是把引擎已经算过的东西摊开给人看。 python tests/test_valuation_bridge.py

这一段的危险不是算错，而是算第二遍：画图代码一旦自己重跑一次 DCF，面板、CLI、图三处就会各说一套，
而且分叉得极慢（只在改了某个不上图的输入时才现形）。所以这里盯三件事：
1) 每个节点的数都等于引擎那次 run 的对应量，每股价值那一跳直接等于归因里的 per_share；
2) 边只接在图里真有的行上，换口径／换终值时步骤集合变了也不留悬空边；
3) 终值算不出时链条就地收口，不补一个假的 0 或者假节点。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.attribution import attribute, flat_rows  # noqa: E402
from backend.model.dcf import evaluate, valuation_bridge  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402

FAILURES = 0
LADDER = synthetic_ladder()
LADDER_KEYS = {row["key"] for row in flat_rows(LADDER)}
COMBOS = [(method, terminal) for method in ("fcff", "fcfe") for terminal in ("gordon", "exit_multiple", "uem")]
TOL = 1e-3


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


def close(a: float | None, b: float, tol: float = TOL) -> bool:
    return a is not None and b is not None and abs(a - b) < tol


def by_key(bridge: dict) -> dict[str, dict]:
    return {node["key"]: node for node in bridge["nodes"]}


def test_the_bridge_only_unrolls_one_run() -> None:
    """六个 口径×终值 全走一遍：形状随口径变，账目必须每次都对得上同一次 run。"""
    for method, terminal in COMBOS:
        report = attribute(LADDER, {}, method, terminal)
        bridge = report["valuation"]
        nodes = by_key(bridge)
        label = f"{method}/{terminal}"
        check(f"{label} 有节点可画", bool(nodes), str(len(nodes)))
        check(f"{label} 全是引擎步骤", all(node["kind"] == "engine" and node["derived"] and not node["editable"] for node in nodes.values()))
        check(f"{label} 每跳都写清了算法", all(node["formula"] for node in nodes.values()),
              str([key for key, node in nodes.items() if not node["formula"]]))
        keys = set(nodes)
        # 段内的边两头都在段里；跨段的边从假设链的行接进来，那些行由 /api/derivation 的骨架提供。
        check(f"{label} 边不指向图外的步骤",
              all(edge["to"] in keys and (edge["from"] in keys or edge["from"] in LADDER_KEYS) for edge in bridge["edges"]),
              str([e for e in bridge["edges"] if e["to"] not in keys or (e["from"] not in keys and e["from"] not in LADDER_KEYS)]))
        check(f"{label} 上游只接图里真有的行",
              all(set(node["upstream"]) <= (keys | LADDER_KEYS) for node in nodes.values()))
        # 拓扑序：段内上游一定先出场，浏览器按这个顺序算列深，不必再做一次拓扑排序。
        order = [node["key"] for node in bridge["nodes"]]
        check(f"{label} 节点表本身就是拓扑序",
              all(order.index(source) < index for index, key in enumerate(order) for source in nodes[key]["upstream"] if source in nodes))
        check(f"{label} 每股价值＝归因里的每股价值", close(nodes["step_per_share"]["baseline"], report["per_share"]),
              f"{nodes['step_per_share']['baseline']} vs {report['per_share']}")
        # 逐年表与面板下方那张折现明细表必须是同一批数：两处不同源就迟早分叉。
        explicit = nodes["step_pv_explicit"]
        schedule = {str(line["year"]): line[f"pv_{method}"] for line in report["schedule"]}
        check(f"{label} 逐年现值与折现明细表同源",
              {year: round(value, 4) for year, value in schedule.items()} == explicit["series"],
              f"{sorted(explicit['series'])} vs {sorted(schedule)}")
        check(f"{label} 逐年现值加总＝显性期合计",
              close(sum(explicit["series"].values()), explicit["baseline"]))


def test_the_arithmetic_reconciles() -> None:
    """把最后几跳的加减法逐条对回去：桥上的算式和引擎的账不能差出一个少数点。"""
    inputs = {
        "revenue_base": 1000.0,
        "revenue_growth_path": {year: 0.0 for year in (2026, 2027, 2028, 2029, 2030)},
        "consensus_revenue": {},
        "ebit_margin_path": {year: 0.30 for year in (2026, 2027, 2028, 2029, 2030)},
        "ebit_margin_ltm": 0.30,
        "tax_rate_ltm": 0.25,
        "da_to_revenue_ltm": 0.05,
        "capex_to_revenue_ltm": 0.05,
        "nwc_to_revenue_delta": 0.0,
        "interest_bearing_debt": 0.0,
        "cost_of_debt_pretax": 0.05,
        "net_borrowing": 0.0,
        "net_debt": 200.0,
        "minority_interest": 50.0,
        "wacc": 0.075,
        "cost_of_equity": 0.075,
        "terminal_growth": 0.025,
        "mid_year_discount": False,
        "total_shares": 100.0,
        "price": 40.0,
        "roic_ltm": 0.20,
    }
    # 平台型算例，中间量全能手算：营收 5×1000、EBIT 1500、NOPAT 1125、D&A 250、CapEx 250、ΔNWC 0
    nodes = by_key(valuation_bridge(evaluate(inputs, "fcff", "gordon")))
    check("营收合计 5000", close(nodes["step_revenue"]["baseline"], 5000.0))
    check("EBIT 合计 1500", close(nodes["step_ebit"]["baseline"], 1500.0))
    check("NOPAT 合计 1125", close(nodes["step_nopat"]["baseline"], 1125.0))
    check("FCFF = NOPAT + D&A − CapEx − ΔNWC", close(nodes["step_fcff"]["baseline"], 1125.0 + 250.0 - 250.0 - 0.0))
    check("显性期现值 910.32", close(nodes["step_pv_explicit"]["baseline"], 910.3242))
    check("终值 4612.50", close(nodes["step_terminal_value"]["baseline"], 4612.50))
    check("终值现值 3212.88", close(nodes["step_pv_terminal"]["baseline"], 3212.8768))
    check("EV = 两段现值相加", close(nodes["step_enterprise_value"]["baseline"], 910.3242 + 3212.8768))
    check("股权 = EV − 净债务 − 少数股东", close(nodes["step_equity_value"]["baseline"], 910.3242 + 3212.8768 - 200.0 - 50.0))
    check("每股 = 股权 ÷ 股本", close(nodes["step_per_share"]["baseline"], (4123.2010 - 250.0) / 100.0))
    check("相对现价跟着每股价值走", close(nodes["step_upside"]["baseline"], 38.73201 / 40.0 - 1, 1e-4))
    check("逐年现值加总＝显性期合计",
          close(sum(nodes["step_pv_explicit"]["series"].values()), nodes["step_pv_explicit"]["baseline"]))
    check("公式里写的是引擎用的那个折现率", "7.50%" in nodes["step_pv_explicit"]["formula"], nodes["step_pv_explicit"]["formula"])
    check("FCFE 没有企业价值那一跳", "step_enterprise_value" not in by_key(valuation_bridge(evaluate(inputs, "fcfe", "gordon"))))


def test_the_bridge_stays_silent_when_the_engine_refuses_to_price() -> None:
    """缺 ROIC 的 UEM、缺倍数行的退出倍数：引擎连合计都没算，桥上就不该有 0 冒充显性期现值。"""
    thin = {
        "revenue_base": 1000.0, "revenue_growth_path": {year: 0.0 for year in (2026, 2027)},
        "consensus_revenue": {}, "ebit_margin_path": {year: 0.30 for year in (2026, 2027)},
        "ebit_margin_ltm": 0.30, "tax_rate_ltm": 0.25, "da_to_revenue_ltm": 0.05,
        "capex_to_revenue_ltm": 0.05, "nwc_to_revenue_delta": 0.0, "interest_bearing_debt": 0.0,
        "cost_of_debt_pretax": 0.05, "net_borrowing": 0.0, "net_debt": 0.0, "minority_interest": 0.0,
        "wacc": 0.075, "cost_of_equity": 0.075, "terminal_growth": 0.025, "mid_year_discount": False,
        "total_shares": 100.0, "price": 40.0,
    }
    for method, terminal, reason in (
        ("fcff", "uem", "ROIC"), ("fcfe", "uem", "ROIC"),
        ("fcff", "exit_multiple", "EV/EBITDA"), ("fcfe", "exit_multiple", "市盈率"),
    ):
        result = evaluate(thin, method, terminal)
        bridge = valuation_bridge(result)
        check(f"{method}/{terminal} 引擎拒定价时整段留空", bridge["nodes"] == [] and bridge["edges"] == [])
        check(f"{method}/{terminal} 拒定价的理由有句话", any(reason in warning for warning in result.warnings),
              str(result.warnings[:2]))


def test_missing_share_count_stops_the_chain_before_per_share() -> None:
    """股本缺失只影响最后两跳：前面的账都是算出来的，该照画；到每股那一步才停，不编一个除数。"""
    nodes = by_key(attribute(LADDER, {"total_shares": 0}, "fcff", "gordon")["valuation"])
    check("缺股本就没有每股价值", not {"step_per_share", "step_upside"} & set(nodes), str(sorted(nodes)))
    check("但前面几步照画",
          {"step_revenue", "step_fcff", "step_pv_explicit", "step_enterprise_value", "step_equity_value"} <= set(nodes))
    check("企业价值＝两段现值相加",
          close(nodes["step_enterprise_value"]["baseline"],
                nodes["step_pv_explicit"]["baseline"] + nodes["step_pv_terminal"]["baseline"]))


def test_an_edit_moves_the_bridge_and_its_formula() -> None:
    """改一个假设：段上的数字与式子里代的那批标量要一起动，否则图会说「税率 25%」而按 15% 算。"""
    before = by_key(attribute(LADDER, {}, "fcff", "gordon")["valuation"])
    after = by_key(attribute(LADDER, {"tax_rate_ltm": 0.15}, "fcff", "gordon")["valuation"])
    check("改税率会改 NOPAT", not close(after["step_nopat"]["baseline"], before["step_nopat"]["baseline"]))
    check("公式里代的是新税率", "15.00%" in after["step_nopat"]["formula"], after["step_nopat"]["formula"])
    check("旧式子的税率不会留着", "15.00%" not in before["step_nopat"]["formula"])
    check("每股价值跟着走", not close(after["step_per_share"]["baseline"], before["step_per_share"]["baseline"]))


def test_a_ladder_row_absent_from_the_old_batch_drops_its_edge() -> None:
    """known 传的是这批基线实有的行：老批次没有租赁行，就不该有那条边——与骨架同一纪律。

    段内的边不受 known 影响：那几步是引擎自己长出来的，跟这批数据新旧无关。
    """
    inputs = {
        "revenue_base": 1000.0, "revenue_growth_path": {2026: 0.0, 2027: 0.0}, "consensus_revenue": {},
        "ebit_margin_path": {2026: 0.30, 2027: 0.30}, "ebit_margin_ltm": 0.30, "tax_rate_ltm": 0.25,
        "da_to_revenue_ltm": 0.05, "capex_to_revenue_ltm": 0.05, "nwc_to_revenue_delta": 0.0,
        "interest_bearing_debt": 100.0, "cost_of_debt_pretax": 0.05, "net_borrowing": 0.0,
        "net_debt": 100.0, "minority_interest": 0.0, "wacc": 0.075, "cost_of_equity": 0.075,
        "terminal_growth": 0.025, "mid_year_discount": False, "total_shares": 100.0, "price": 40.0,
    }
    result = evaluate(inputs, "fcfe", "gordon")
    known = {"revenue_base", "wacc", "cost_of_equity", "terminal_growth", "total_shares", "price", "net_debt"}
    full = by_key(valuation_bridge(result))
    trimmed = by_key(valuation_bridge(result, known=known))
    check("步数与 known 无关", set(trimmed) == set(full), f"{len(trimmed)} vs {len(full)}")
    check("跨段上游按 known 收干净", all(set(node["upstream"]) <= (set(trimmed) | known) for node in trimmed.values()),
          str({key: node["upstream"] for key, node in trimmed.items() if set(node["upstream"]) - (set(trimmed) | known)}))
    check("确实少了几条边", sum(len(node["upstream"]) for node in trimmed.values())
          < sum(len(node["upstream"]) for node in full.values()))


if __name__ == "__main__":
    for name in sorted(globals()):
        if name.startswith("test_"):
            print(f"[{name}]")
            globals()[name]()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
