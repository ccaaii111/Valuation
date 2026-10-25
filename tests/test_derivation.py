"""哨兵：假设链的 derives 边必须与真实推导一致 —— 不新算任何数字，只查关系。 python tests/test_derivation.py

推导图（M5）直接把这张表当边集画，所以一条写错的边就是一条画错的线。这里盯的是四类会悄悄发生的漂移：
表里的 key 被改名、上游指向不存在的行、图上出现环、级联行依赖了引擎根本不读的行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.assumptions import DERIVES, Row, build_ladder  # noqa: E402
from backend.model.attribution import attribute, flat_rows  # noqa: E402
from backend.model.dcf import DERIVED_INPUTS, ENGINE_INPUTS  # noqa: E402
from fixtures import synthetic_ladder, synthetic_snapshot  # noqa: E402

FAILURES = 0
LADDER = synthetic_ladder()  # 走一遍真实 build_ladder，键名与生产同源
KEYS = [row["key"] for row in flat_rows(LADDER)]
BY_KEY = {row["key"]: row for row in flat_rows(LADDER)}


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


def downstream(key: str) -> set[str]:
    """key 的全部祖先（传递闭包）。图是无环的，环由 cycle() 单独报。"""
    out: set[str] = set()
    queue = list(BY_KEY[key]["derives"])
    while queue:
        node = queue.pop()
        if node in out:
            continue
        out.add(node)
        queue.extend(BY_KEY.get(node, {}).get("derives", []))
    return out


def cycle(key: str) -> list[str] | None:
    """沿上游深度优先走，回到自己就是环。"""
    stack = [(key, iter(BY_KEY[key]["derives"]))]
    path = [key]
    while stack:
        children = stack[-1][1]
        stepped = False
        for child in children:
            if child == key:
                return [*path, child]
            if child in path or child not in BY_KEY:
                continue
            path.append(child)
            stack.append((child, iter(BY_KEY[child]["derives"])))
            stepped = True
            break
        if not stepped:
            stack.pop()
            path.pop()
    return None


def test_declared_edges_point_at_real_rows() -> None:
    unknown = sorted(key for key in DERIVES if key not in BY_KEY)
    check("表里没有悬空的行名", not unknown, str(unknown))
    ghost = sorted({parent for key, parents in DERIVES.items() for parent in parents if parent not in BY_KEY})
    check("表里没有指向不存在行的边", not ghost, str(ghost))
    self_edge = [key for key, parents in DERIVES.items() if key in parents]
    check("没有自环", not self_edge, str(self_edge))
    duplicated = [key for key, parents in DERIVES.items() if len(set(parents)) != len(parents)]
    check("上游不重复列", not duplicated, str(duplicated))


def test_the_graph_is_acyclic() -> None:
    loops = [loop for key in BY_KEY for loop in [cycle(key)] if loop]
    check("整张图无环", not loops, str(loops[:1]))
    # g 先于折现率、折现率先于路径：分组顺序与推导顺序本就不同，靠拓扑而不是列表次序画图。
    check("跨组的反向边确实存在（终值喂收入路径）", "terminal_growth" in BY_KEY["revenue_growth_path"]["derives"])
    check("wacc 的祖先含 rf/β/ERP", {"risk_free", "beta", "erp"} <= downstream("wacc"), str(sorted(downstream("wacc")))[:120])
    check("收入路径的祖先含一致预期与期末营收", {"consensus_revenue", "revenue_base"} <= downstream("revenue_path"))


def test_cascade_rows_only_read_what_the_engine_reads() -> None:
    """引擎当场重算的那两行（cost_of_equity/wacc）只能依赖引擎真会喂给它的键，否则图上那条线是假的。"""
    for key in sorted(DERIVED_INPUTS):
        listed = set(DERIVES.get(key, ()))
        check(f"{key} 的上游都在 ENGINE_INPUTS 里", listed <= (ENGINE_INPUTS | DERIVED_INPUTS), str(sorted(listed - ENGINE_INPUTS - DERIVED_INPUTS)))
        check(f"{key} 声明了上游", bool(listed))
    # 「级联」作为source开头＝这行是引擎当场折出来的；句中提到的（增速路径向级联永续增速回落）只是散文，
    # 别把两者混成一个判据，否则图上会给收入路径也挂上「重算」的牌子。
    claiming = [row["key"] for row in BY_KEY.values() if (row.get("source") or "").startswith("级联")]
    check("自称级联结果的行就是那两行", set(claiming) == set(DERIVED_INPUTS), str(claiming))
    check("级联结果不在可编辑集合里", not (DERIVED_INPUTS & ENGINE_INPUTS), str(sorted(DERIVED_INPUTS & ENGINE_INPUTS)))


def test_data_rows_do_not_claim_upstream_they_cannot_show() -> None:
    """只列同为假设行的上游：源表原始科目的行必须留空，否则图上会凭空多出一条没有凭证的线。"""
    mislabeled = [key for key, row in BY_KEY.items() if row["kind"] == "derived" and key not in DERIVED_INPUTS]
    check("kind=derived 只用在级联结果上", not mislabeled, str(mislabeled))
    for key in ("revenue_base", "revenue_ltm", "consensus_revenue", "beta", "erp", "price", "total_shares"):
        check(f"{key} 是取数或惯例值，无上游", BY_KEY[key]["derives"] == [], str(BY_KEY[key]["derives"]))
    # 滚动市盈率自算之后就有了上游；它仍然不是 kind=derived（引擎按行值定价，人工可覆盖）。
    check("pe_ttm 的上游是市值与归母净利 LTM", BY_KEY["pe_ttm"]["derives"] == ["market_cap", "net_profit_ltm"], str(BY_KEY["pe_ttm"]["derives"]))


def test_edges_survive_the_json_round_trip() -> None:
    stored = json.loads(json.dumps(LADDER, ensure_ascii=False))
    check("baseline.json 里带 derives", stored["groups"][0]["rows"][0].get("derives") is not None)
    again = {row["key"]: row["derives"] for row in flat_rows(stored)}
    check("读回来一模一样", all(again[key] == BY_KEY[key]["derives"] for key in BY_KEY))
    stripped = json.loads(json.dumps({"groups": [
        {"id": g["id"], "title": g["title"], "rows": [{k: v for k, v in r.items() if k != "derives"} for r in g["rows"]]}
        for g in LADDER["groups"]]}))
    recovered = {row["key"]: row["derives"] for row in flat_rows(stripped)}
    check("老批次回填同一张表，不必重跑取数", all(recovered[key] == BY_KEY[key]["derives"] for key in BY_KEY))


def test_the_panel_payload_carries_the_edges() -> None:
    report = attribute(LADDER, {"consensus_revenue": {"2026": 1200.0}}, "fcff", "gordon")
    rows = {row["key"]: row for row in report["rows"]}
    check("归因载荷带 derives", rows["revenue_path"]["derives"] == list(DERIVES["revenue_path"]), str(rows["revenue_path"]["derives"]))
    check("未改动的行也带上游", rows["wacc"]["derives"] and rows["tax_rate_hist"]["derives"] == [])
    explicit = Row("wacc", "WACC", "pct", 0.08, "级联")
    check("Row 自己填的上游优先于表", explicit.derives == DERIVES["wacc"] and Row("x", "x", "pct", 1, "s", derives=("y",)).derives == ("y",))


def test_the_bridge_and_rate_edges_match_the_formulas() -> None:
    """几条最要紧的边按公式逐个点名，改式子的人必须同时改表。"""
    check("净债务 = 有息负债 − 现金", set(BY_KEY["net_debt"]["derives"]) == {"interest_bearing_debt", "cash_and_financial_assets"})
    check("权益权重看总市值与有息负债", set(BY_KEY["equity_weight"]["derives"]) == {"market_cap", "interest_bearing_debt"})
    check("WACC 吃税率（ltm 缺位时用 hist）", {"tax_rate_ltm", "tax_rate_hist"} <= set(BY_KEY["wacc"]["derives"]))
    check("隐含 EV/EBITDA 由现价反推", {"market_cap", "ebit_margin_ltm", "da_to_revenue_ltm"} <= set(BY_KEY["exit_ev_ebitda"]["derives"]))
    check("增速路径向永续增速回落", "terminal_growth" in BY_KEY["revenue_growth_path"]["derives"])
    check("rf 行记下即期与中枢之争", set(BY_KEY["risk_free"]["derives"]) == {"risk_free_spot", "risk_free_norm"})


def test_ladder_built_without_lease_still_holds() -> None:
    """关掉租赁口径时少一行 lease_financing，其余边不受影响（表按 key 查，不按位置）。"""
    snapshot = synthetic_snapshot()
    ladder = build_ladder(snapshot, snapshot["consensus"], snapshot["quote"], 0.02, include_lease_debt=False, beta_manual=1.0).to_json()
    rows = {row["key"]: row for row in flat_rows(ladder)}
    check("无租赁口径时确实没有那一行", "lease_financing" not in rows)
    check("其余边照旧", all(rows[key]["derives"] == BY_KEY[key]["derives"] for key in rows if key in BY_KEY))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
