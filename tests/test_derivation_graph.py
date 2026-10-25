"""推导图的数据层：列＝离原始取数几步，边＝建链时的出处。 python tests/test_derivation_graph.py

M5 的页面只负责画，线怎么摆在这里定。所以这里盯的是「画出来会不会误导」那一类：
一条从右往左的线会让人以为改本行能回头改上游，一个丢掉节点的布局会让人以为面板上有些行不存在，
一份在画图代码里重算出来的数字会和面板/CLI 对不上。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.attribution import attribute, flat_rows  # noqa: E402
from backend.model.dcf import RUNTIME_EDGES, inputs_from_ladder  # noqa: E402
from backend.model.derivation import FORMULAS, PLACEHOLDER, SUBSTITUTIONS, graph  # noqa: E402
from backend.model.recompute import NEEDS_RAW, DERIVE, live_rows  # noqa: E402
from backend.web import api, store  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402

FAILURES = 0
LADDER = synthetic_ladder()
GRAPH = graph(LADDER)
NODES = {node["key"]: node for node in GRAPH["nodes"]}
EDGES = {(edge["from"], edge["to"]) for edge in GRAPH["edges"]}


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


def test_every_row_gets_a_place() -> None:
    rows = flat_rows(LADDER)
    check("图上的行数＝面板行数", len(GRAPH["nodes"]) == len(rows) == len(NODES), f"{len(GRAPH['nodes'])} vs {len(rows)}")
    check("每行都有列号", all(isinstance(node["layer"], int) and node["layer"] >= 0 for node in GRAPH["nodes"]))
    check("列数与最深一行相符", GRAPH["columns"] == max(node["layer"] for node in GRAPH["nodes"]) + 1, str(GRAPH["columns"]))
    check("无上游的行全在最左列", set(GRAPH["roots"]) == {node["key"] for node in GRAPH["nodes"] if node["layer"] == 0})
    check("最左列就是用户要的那一列", len(GRAPH["roots"]) > len(NODES) // 3, f"{len(GRAPH['roots'])} 行原始取数/惯例值")
    check("参考行也在图上（不被子图裁掉）",
          {"risk_free_spot", "terminal_growth_optimistic", "terminal_growth_sustainable", "consensus_dispersion"} <= set(NODES))


def test_edges_always_point_left_to_right() -> None:
    backwards = [edge for edge in EDGES if NODES[edge[0]]["layer"] >= NODES[edge[1]]["layer"]]
    check("没有一条线从右往左画", not backwards, str(backwards))
    missing = [edge for edge in EDGES if edge[1] not in NODES or edge[0] not in NODES]
    check("边两头都有节点", not missing, str(missing))
    for key, node in NODES.items():
        check(f"{key} 的上游与边集一致", set(node["upstream"]) == {frm for frm, to in EDGES if to == key})
    # 反向跨组边（终值喂显性期）正是必须靠层号而不能靠面板行序的理由。
    check("终值组喂显性期这条边存在", ("terminal_growth", "revenue_growth_path") in EDGES)
    check("所以终值行排在显性期左边", NODES["terminal_growth"]["layer"] < NODES["revenue_growth_path"]["layer"])
    # 三档参考行都连向同一个级联行：换 --terminal-growth-basis 只是换一个箭头在用，四行始终齐备，
    # 缺分红凭证时第四行也只是空白着连一条线，不会从图上消失。
    for src in ("terminal_growth_central", "terminal_growth_optimistic", "terminal_growth_sustainable"):
        check(f"{src} 画向级联行", (src, "terminal_growth") in EDGES)
        check(f"{src} 在最左列且不可定价", NODES[src]["layer"] == 0 and not NODES[src]["editable"],
              f"{NODES[src]['layer']} {NODES[src]['editable']}")


def test_the_depth_matches_the_formula() -> None:
    expected = {
        "revenue_base": 0, "beta": 0, "risk_free_spot": 0, "risk_free_norm": 0,
        "risk_free": 1, "cost_of_debt_pretax": 1, "net_debt": 1, "terminal_growth": 1,
        "cost_of_equity": 2, "revenue_growth_path": 2, "ebit_margin_start": 2,
        "wacc": 3, "revenue_path": 3, "horizon_years": 3,
    }
    for key, layer in expected.items():
        check(f"{key} 落在第 {layer} 列", NODES[key]["layer"] == layer, str(NODES[key]["layer"]))
    check("级联结果在它所有上游的右边", all(NODES[p]["layer"] < NODES["wacc"]["layer"] for p in NODES["wacc"]["upstream"]))
    check("最深的一列只有终点行", max(node["layer"] for node in GRAPH["nodes"]) == 3)


def test_the_graph_is_data_not_a_second_engine() -> None:
    """图只搬已有事实。这里算出任何估值结果，就说明有人把第二套算法搬进了画图代码。"""
    text = json.dumps(GRAPH, ensure_ascii=False)
    for needle in ("per_share", "equity_value", "enterprise_value", "fcff", "pv_"):
        check(f"图里不含估值结果 {needle}", needle not in text)
    for row in flat_rows(LADDER):
        check(f"{row['key']} 的基线与链上一致", NODES[row["key"]]["baseline"] == row["baseline"])


def test_the_panel_and_the_graph_agree_on_what_is_editable() -> None:
    """两处都能改数：判据分叉就会出现「图上能填、面板里是锁的」这种假 bug。"""
    report = attribute(LADDER, {"beta": 1.1}, "fcff", "gordon")
    for row in report["rows"]:
        node = NODES[row["key"]]
        check(f"{row['key']} 可编辑性一致", node["editable"] == row["editable"], f"{node['editable']} vs {row['editable']}")
        check(f"{row['key']} 级联性一致", node["derived"] == row["derived"])
        check(f"{row['key']} 控件类型一致", node["value_type"] == row["value_type"])
        check(f"{row['key']} 金额行语义一致", node["edit"] == row["edit"])
        # 说明与凭证分两栏下发：面板悬浮与导图侧栏都拼这三段，字段各说一半就不能在两处显示不同的话。
        check(f"{row['key']} 来源一致", node["source"] == row["source"])
        check(f"{row['key']} 说明一致", node["note"] == row["note"], f'{node["note"]} vs {row["note"]}')
        check(f"{row['key']} 凭证一致", node["evidence"] == row["evidence"], f'{node["evidence"]} vs {row["evidence"]}')


def test_a_legacy_batch_without_evidence_shows_the_untouched_note() -> None:
    """分家之前落盘的批次没有 evidence 字段：两处都读成空串，不去猜哪半是凭证。"""
    stripped = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in stripped["groups"]:
        for row in group["rows"]:
            row.pop("evidence", None)
    nodes = {node["key"]: node for node in graph(stripped)["nodes"]}
    for row in attribute(stripped)["rows"]:
        check(f"{row['key']} 老批次凭证两边都是空", nodes[row["key"]]["evidence"] == "" and row["evidence"] == "")
        check(f"{row['key']} 老批次说明原样透出", nodes[row["key"]]["note"] == row["note"], str(row["note"])[:60])


def test_a_missing_row_drops_its_edge_instead_of_crashing() -> None:
    """老批次／无租赁口径下少几行是常态：边收干净，不能画向虚无。"""
    ladder = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in ladder["groups"]:
        group["rows"] = [row for row in group["rows"] if row["key"] not in ("risk_free_spot", "risk_free_norm")]
    partial = graph(ladder)
    keys = {node["key"] for node in partial["nodes"]}
    check("少两行就少两个节点", len(keys) == len(NODES) - 2, str(len(keys)))
    check("不画指向不存在行的边", all(edge["from"] in keys and edge["to"] in keys for edge in partial["edges"]))
    check("父行都不在图上时，本行就成了最左列",
          next(node for node in partial["nodes"] if node["key"] == "risk_free")["layer"] == 0)
    check("它因此被认作图上的根", "risk_free" in partial["roots"])
    check("少两行不会多一列", partial["columns"] <= GRAPH["columns"], str(partial["columns"]))


def test_a_cycle_is_reported_instead_of_hanging() -> None:
    ladder = {"groups": [{"id": "g", "title": "组", "rows": [
        {"key": "a", "label": "A", "unit": "pct", "baseline": 0.1, "derives": ["b"]},
        {"key": "b", "label": "B", "unit": "pct", "baseline": 0.1, "derives": ["a"]},
    ]}]}
    try:
        graph(ladder)
    except ValueError as exc:
        check("成环时报的是人话", "成环" in str(exc), str(exc))
    else:
        check("成环时报的是人话", False, "没抛异常")


def test_legacy_batch_without_derives_lays_out_the_same() -> None:
    stripped = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in stripped["groups"]:
        for row in group["rows"]:
            row.pop("derives", None)
    legacy = graph(stripped)
    check("老批次回填后边数相同", len(legacy["edges"]) == len(GRAPH["edges"]), f"{len(legacy['edges'])} vs {len(GRAPH['edges'])}")
    check("老批次回填后分层相同",
          {node["key"]: node["layer"] for node in legacy["nodes"]} == {node["key"]: node["layer"] for node in GRAPH["nodes"]})


def test_live_marks_say_which_edge_still_carries_a_recompute() -> None:
    """实线/虚线的判据：`live` 只说「改上游这一格当场会不会重算」，它必须与代码里的归属表同出一处。

    这条哨兵防的是最难发现的一类假线：往 `DERIVES` 加了一条边、没配 `derive()`，图上看似的线改了什么都不发生。
    """
    live = {node["key"]: node["live"] for node in GRAPH["nodes"]}
    with_upstream = {node["key"] for node in GRAPH["nodes"] if node["upstream"]}
    check("实线的行都在当场重算的归属表里", {k for k, v in live.items() if v} <= RUNTIME_EDGES,
          str(sorted({k for k, v in live.items() if v} - RUNTIME_EDGES)))
    starved = set(DERIVE) - set(live_rows(inputs_from_ladder(LADDER)))
    check("归属表里的行没被画成虚线", (RUNTIME_EDGES & with_upstream) - starved <= {k for k, v in live.items() if v},
          str(sorted((RUNTIME_EDGES & with_upstream) - starved - {k for k, v in live.items() if v})))
    check("这条批次领不到原料的行才是虚线", {k for k, v in live.items() if not v and k in with_upstream}
          == (with_upstream - RUNTIME_EDGES) | (starved & with_upstream),
          str(sorted(starved)))
    check("每条边的实虚都与终点行一致", all(edge["live"] == live[edge["to"]] for edge in GRAPH["edges"]))


def test_a_batch_without_material_draws_those_edges_dashed() -> None:
    """老批次（M2 之前存的）没原料：那六条边的线仍在，但只能是虚的——图不能承诺代码兑不了的重算。"""
    legacy = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in legacy["groups"]:
        for row in group["rows"]:
            row.pop("raw", None)
    partial = graph(legacy)
    live = {node["key"]: node["live"] for node in partial["nodes"]}
    for key in NEEDS_RAW:
        check(f"老批次 {key} 画虚线", live[key] is False)
    check("新批次里原料齐的行是实线", live_rows(inputs_from_ladder(LADDER)) >= {"ebit_margin_ltm", "roic_ltm"}
          and {node["key"]: node["live"] for node in GRAPH["nodes"]}["ebit_margin_ltm"] is True)
    check("不吃原料的边不受老批次影响", live["revenue_growth_path"] is True and live["wacc"] is True)
    check("虚线不改变图的形状", {node["key"] for node in partial["nodes"]} == set(NODES)
          and {(e["from"], e["to"]) for e in partial["edges"]} == EDGES)


def test_every_line_has_a_formula_behind_it() -> None:
    """「这一格的数从哪来」是这张图存在的理由：凡有上游就必须写清怎么算，漏一行等于让用户自己猜。

    式子只作显示（不进 baseline.json、不动任何数字），所以它的缺席不会让别处变红——只能靠这条哨兵。
    反方向也钉住：无上游的行（原始取数或惯例值）没有「怎么算」，硬凑一句就是假凭证。
    """
    with_upstream = {node["key"] for node in GRAPH["nodes"] if node["upstream"]}
    with_formula = {node["key"] for node in GRAPH["nodes"] if node["formula"]}
    check("凡有上游的行都有公式，且只有它们有", with_upstream == with_formula,
          f"缺 {sorted(with_upstream - with_formula)}／多 {sorted(with_formula - with_upstream)}")
    check("FORMULAS 里的键全落在图上", set(FORMULAS) <= set(NODES), str(sorted(set(FORMULAS) - set(NODES))))


def test_a_substituted_formula_only_reads_rows_on_its_own_chain() -> None:
    """代入式是放大页唯一算得出口的「算式」：它引用的每一格都必须是这一行真正的上游。

    代进一个非上游的数，看上去是一条完整算式，实际把不相干的行连成了依赖——比不写更糟，
    而且只在放大页出现，图上与面板里都不会报错。所以这条只能在模型层钉住。
    """
    templated = {node["key"] for node in GRAPH["nodes"] if node["substitute"]["template"]}
    check("写了模板的行全都领得到（没被静默丢掉）", templated == set(SUBSTITUTIONS),
          f"缺 {sorted(set(SUBSTITUTIONS) - templated)}／多 {sorted(templated - set(SUBSTITUTIONS))}")
    for key in sorted(templated):
        node = NODES[key]
        refs = node["substitute"]["refs"]
        check(f"{key} 代入式只引自己的上游", set(refs) <= set(node["upstream"]), str(set(refs) - set(node["upstream"])))
        check(f"{key} 的引用已去重", refs == list(dict.fromkeys(refs)), str(refs))
        found = [m.group(1) for m in PLACEHOLDER.finditer(node["substitute"]["template"])]
        check(f"{key} 的引用与模板占位符一致", set(found) == set(refs), f"{found} vs {refs}")
        # 逐年序列与文本行代不进去（页面会退回符号式），所以模板里不该有它们。
        check(f"{key} 代进去的每格都是标量", all(NODES[r]["value_type"] == "number" for r in refs),
              str([r for r in refs if NODES[r]["value_type"] != "number"]))
    check("没模板的行交出空模板而不是半条式子",
          all(node["substitute"] == {"template": "", "refs": []} for node in GRAPH["nodes"] if node["key"] not in templated))


def test_a_missing_row_kills_the_whole_substitution() -> None:
    """少一行就整条不出：宁可放大页只给符号式，也不给一条代了个「—」进去的算式。"""
    ladder = json.loads(json.dumps(LADDER, ensure_ascii=False))
    for group in ladder["groups"]:
        group["rows"] = [row for row in group["rows"] if row["key"] != "minority_interest"]
    nodes = {node["key"]: node for node in graph(ladder)["nodes"]}
    check("引用行不在图上时代入式整条不出", nodes["exit_ev_ebitda"]["substitute"] == {"template": "", "refs": []})
    check("符号式仍然在", bool(nodes["exit_ev_ebitda"]["formula"]))
    check("不受影响的行照旧给代入式", nodes["net_debt"]["substitute"]["template"] == SUBSTITUTIONS["net_debt"])


def test_the_zoomed_chain_is_upstream_only() -> None:
    """放大段摊的是「这一行的来路」，并且只按跳数取两跳。那个窗口走的就是这里下发的 upstream 数组，
    所以「链朝哪个方向走、哪些行算链上」先在模型层定下来，页面只负责数到第二跳就停。

    跳数不等于列号：market_cap 距 WACC 两跳却落在最左列。按列号切会把它切掉，那一跳的代入式就缺一格。
    """
    def closure(key: str) -> set[str]:
        seen, stack = set(), [key]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(NODES[node]["upstream"])
        return seen

    def hops(key: str, depth: int) -> set[str]:
        seen, frontier = {key}, [key]
        for _ in range(depth):
            nxt = [up for node in frontier for up in NODES[node]["upstream"] if up not in seen]
            seen |= set(nxt)
            frontier = nxt
        return seen

    check("链尾就是那一行自己", "wacc" in closure("wacc"))
    check("WACC 的链含三级上游与它们的原始取数",
          {"risk_free_spot", "risk_free_norm", "beta", "erp", "cost_of_equity", "equity_weight", "market_cap",
           "cost_of_debt_pretax", "tax_rate_ltm"} <= closure("wacc"), str(sorted(closure("wacc"))))
    check("链不含下游行", not ({"revenue_path", "exit_ev_ebitda", "horizon_years"} & closure("wacc")))
    check("原始取数行只有一跳", closure("beta") == {"beta"})
    check("闭包不随层号丢失（终值反向跨组那条边也在链上）",
          "terminal_growth" in closure("revenue_growth_path") and "revenue_cagr_5y" in closure("revenue_growth_path"))
    check("两跳含 market_cap，尽管它落在第 0 列",
          "market_cap" in hops("wacc", 2) and NODES["market_cap"]["layer"] == 0, str(NODES["market_cap"]["layer"]))
    check("两跳够不到原始取数那一层，三跳才够",
          "risk_free_spot" not in hops("wacc", 2) and "risk_free_spot" in hops("wacc", 3), str(sorted(hops("wacc", 2))))
    check("两跳窗口同样不含下游行", not ({"revenue_path", "exit_ev_ebitda", "horizon_years"} & hops("wacc", 2)))


def test_the_api_hands_the_page_something_drawable() -> None:
    """GET /api/derivation 的契约：给骨架，不给估值结果，也不新开写入路径。"""
    original = store.RUNS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        store.RUNS_DIR = Path(tmp)
        try:
            directory = Path(tmp) / "600519" / "20260924-120000"
            directory.mkdir(parents=True)
            (directory / "baseline.json").write_text(json.dumps(LADDER, ensure_ascii=False), encoding="utf-8")
            payload = api.derivation_payload("600519", "")
            check("批次为空时取最新一批", payload["run_id"] == "20260924-120000", payload["run_id"])
            check("载荷就是同一张图", {node["key"] for node in payload["nodes"]} == set(NODES))
            check("列数与边数一致", (payload["columns"], len(payload["edges"])) == (GRAPH["columns"], len(GRAPH["edges"])))
            check("报基线在哪", payload["baseline"].endswith("baseline.json"), payload["baseline"][-30:])
            check("报告期与代码带出去", payload["as_of"] == LADDER["as_of"] and payload["code"] == "600519")
            check("没有 manual.json 时 saved 是空的", payload["saved"] is None)
            check("不跑引擎所以没有估值结果", "per_share" not in json.dumps(payload, ensure_ascii=False))
            try:
                api.derivation_payload("600519", "20260101-000000")
            except FileNotFoundError as exc:
                check("点名批次缺 baseline", "baseline.json" in str(exc), str(exc)[:60])
            else:
                check("点名批次缺 baseline", False, "没抛异常")
            try:
                api.derivation_payload("../etc", "")
            except ValueError as exc:
                check("非法代码进不来磁盘", "非法证券代码" in str(exc), str(exc)[:60])
            else:
                check("非法代码进不来磁盘", False, "没抛异常")
        finally:
            store.RUNS_DIR = original


if __name__ == "__main__":
    for name in sorted(globals()):
        if name.startswith("test_"):
            print(f"[{name}]")
            globals()[name]()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
