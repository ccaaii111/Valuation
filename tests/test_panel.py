"""Panel route tests against a synthetic baseline in a temp runs dir: no network, no real runs touched.

    python tests/test_panel.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.model.attribution import attribute  # noqa: E402
from backend.web import store  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402
import test_erp as TE  # noqa: E402  复用它的市场腿夹具：面板载荷要测「带着实测 ERP 的批次」

TMP = tempfile.TemporaryDirectory()
RUN_ID = "20260922-101112"
OLDER_RUN = "20260921-090000"
store.RUNS_DIR = Path(TMP.name) / "runs"
for _run in (RUN_ID, OLDER_RUN):
    (store.RUNS_DIR / "000001" / _run).mkdir(parents=True)
    (store.RUNS_DIR / "000001" / _run / "baseline.json").write_text(
        json.dumps(synthetic_ladder(), ensure_ascii=False), encoding="utf-8"
    )

from backend.web.app import create_app  # noqa: E402

CLIENT = create_app().test_client()


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0


def row_of(payload: dict, key: str) -> dict:
    return next(r for r in payload["attribution"]["rows"] if r["key"] == key)


def recompute(**body):
    body.setdefault("code", "000001")
    body.setdefault("run", RUN_ID)
    return CLIENT.post("/api/recompute", json=body)


def test_options_lists_baselines() -> None:
    payload = CLIENT.get("/api/options").get_json()
    check("列出发过基线的代码", [c["code"] for c in payload["codes"]] == ["000001"], str(payload["codes"]))
    check("批次带报表截至日", payload["codes"][0]["runs"][0]["as_of"] == "2025-12-31")
    check("批次从新到旧排列", [r["run_id"] for r in payload["codes"][0]["runs"]] == [RUN_ID, OLDER_RUN])
    check("归因口径写进契约", "不可相加" in payload["attribution_note"])


def test_blank_run_falls_back_to_the_newest_baseline() -> None:
    payload = CLIENT.get("/api/panel?code=000001").get_json()
    check("缺 run 参数时不报错", "error" not in payload, str(payload)[:120])
    check("回退到最新批次并回报真实批次号", payload["run_id"] == RUN_ID, str(payload.get("run_id")))
    check("未基线的代码回退时报 404", CLIENT.get("/api/panel?code=600000").status_code == 404)


def test_panel_payload_describes_controls() -> None:
    payload = CLIENT.get(f"/api/panel?code=000001&run={RUN_ID}").get_json()
    report = payload["attribution"]
    check("首屏每股等于矩阵 fcff:gordon", report["per_share"] == payload["matrix"]["fcff:gordon"]["per_share"], str(report["per_share"]))
    # g 从公司自己的增速序列取保守情形：玩具公司近5年最差是 2025 的 +6.38%，之半即 +3.19%，高于旧惯例 2.5%。
    check("玩具公司的机器基线 64.19 元", abs(report["per_share"] - 64.1941) < 0.01, str(report["per_share"]))
    check("未修正时总变动为 0", report["total_change_pct"] == 0.0, str(report["total_change_pct"]))
    check("每行给出控件类型", {r["value_type"] for r in payload["attribution"]["rows"]} >= {"number", "year_map"})
    check("派生行只读", row_of(payload, "wacc")["derived"] is True and row_of(payload, "wacc")["editable"] is False)
    check("惯例行可编辑", row_of(payload, "beta")["editable"] is True and row_of(payload, "beta")["kind"] == "convention")
    check("矩阵六个组合齐全", len(payload["matrix"]) == 6, str(sorted(payload["matrix"])))
    check("未修正时改动数为 0", payload["attribution"]["edited_row_count"] == 0)
    check("耗时与 evaluate 次数可见", payload["timings"]["evaluate_count"] == 2, str(payload["timings"]))


def test_beta_edit_cascades_into_displayed_wacc() -> None:
    payload = recompute(overrides={"beta": 0.6}).get_json()
    report = payload["attribution"]
    base, now = report["rates"]["wacc"]
    check("β 改动被定价", abs(row_of(payload, "beta")["contribution_pct"]) > 0.01, str(row_of(payload, "beta")["contribution_pct"]))
    check("面板显示的 WACC 就是级联结果", row_of(payload, "wacc")["current"] == now, f"{row_of(payload, 'wacc')['current']} vs {now}")
    check("显示的 WACC 确实动了", now is not None and base is not None and now < base, f"{base} → {now}")
    check("改动行数只数人工编辑", report["edited_row_count"] == 1, str(report["edited_row_count"]))
    check("级联行不算未定价改动", report["unpriced_edits"] == [], str(report["unpriced_edits"]))
    check("级联行标记为自动重算", row_of(payload, "wacc")["recomputed"] is True and row_of(payload, "wacc")["changed"] is False)
    check("改动行计入 evaluate 次数", payload["timings"]["evaluate_count"] == 3, str(payload["timings"]))


def test_bad_requests_are_rejected_with_a_message() -> None:
    for overrides, needle in (({"wacc": 0.09}, "级联结果"), ({"nope": 1}, "未知假设行"),
                              ({"revenue_growth_path": {"2099": 0.1}}, "不存在这些年度")):
        response = recompute(overrides=overrides)
        check(f"拒收 {next(iter(overrides))}", response.status_code == 400 and needle in response.get_json()["error"],
              str(response.get_json()))
    check("非法批次名不碰文件系统", recompute(run="../../secret").status_code == 400)
    check("代码越界被拒", CLIENT.get("/api/panel?code=..%2f..%2fetc&run=20260922-101112").status_code == 400)
    check("未知终值方法被拒", recompute(terminal="magic").status_code == 400)
    check("折现率越界被拒", recompute(rate="9").status_code == 400)
    check("不存在的批次报 404", recompute(run="20000101-000000").status_code == 404)


def test_year_map_edit_keeps_other_years() -> None:
    payload = recompute(overrides={"revenue_growth_path": {"2029": 0.1}}).get_json()
    row = row_of(payload, "revenue_growth_path")
    check("只改一年时其余年保留", row["current"]["2026"] == row["baseline"]["2026"], str(row["current"]))
    check("改动列只有一年非零", [k for k, v in row["change_pct"].items() if v] == ["2029"], str(row["change_pct"]))
    check("未点名的年份改动为 0", all(v == 0 for k, v in row["change_pct"].items() if k != "2029"))


def test_save_and_restore() -> None:
    overrides = {"terminal_growth": 0.02, "beta": 0.9}
    saved = CLIENT.post("/api/save", json={"code": "000001", "run": RUN_ID, "overrides": overrides, "method": "fcff"}).get_json()
    check("保存落到批次目录", Path(saved["saved"]).name == "manual.json", saved["saved"])
    payload = CLIENT.get(f"/api/panel?code=000001&run={RUN_ID}").get_json()
    check("载入时回读上次修正", payload["saved"]["overrides"] == overrides, str(payload["saved"]))
    check("批次列表标出已修正", payload["code"] and CLIENT.get("/api/options").get_json()["codes"][0]["runs"][0]["edited"] is True)
    check("保存前照样校验键", CLIENT.post("/api/save", json={"code": "000001", "run": RUN_ID, "overrides": {"wacc": 0.1}}).status_code == 400)
    (store.RUNS_DIR / "000001" / RUN_ID / "manual.json").unlink()


def test_money_rows_are_edited_as_delta() -> None:
    payload = CLIENT.get(f"/api/panel?code=000001&run={RUN_ID}").get_json()
    check("金额行按相比基线 ±% 编辑", row_of(payload, "revenue_ltm")["edit"] == "delta")
    check("一致预期营收逐格 ±%", row_of(payload, "consensus_revenue")["edit"] == "delta")
    check("市值股本也是 ±%", row_of(payload, "market_cap")["edit"] == "delta" and row_of(payload, "total_shares")["edit"] == "delta")
    check("比率行仍填数值", row_of(payload, "ebit_margin_ltm")["edit"] == "value")
    check("参数行仍填数值", all(row_of(payload, k)["edit"] == "value" for k in ("beta", "erp", "risk_free", "terminal_growth")))
    check("倍数与价格行填数值", all(row_of(payload, k)["edit"] == "value" for k in ("exit_pe", "pe_ttm", "price")))
    # 换算只发生在面板里：引擎收到的仍是绝对值，所以「填 +8%」与「改动% 列显示 +8.00%」同源。
    base = row_of(payload, "revenue_ltm")["baseline"]
    moved = recompute(overrides={"revenue_ltm": round(base * 1.08, 6)}).get_json()
    row = row_of(moved, "revenue_ltm")
    check("±8% 换算成绝对值后改动列读作 +8.00%", abs(row["change_pct"] - 0.08) < 1e-6, str(row["change_pct"]))
    check("这一行确实被定价", row["editable"] and moved["attribution"]["edited_row_count"] == 1, str(moved["attribution"]["edited_row_count"]))


def test_old_batch_recovers_the_edit_mode_from_the_unit() -> None:
    ladder = json.loads(json.dumps(synthetic_ladder()))
    for group in ladder["groups"]:
        for row in group["rows"]:
            row.pop("edit", None)
    report = attribute(ladder, {}, "fcff", "gordon")
    rows = {r["key"]: r for r in report["rows"]}
    check("缺 edit 的老批次按单位推断", rows["revenue_ltm"]["edit"] == "delta" and rows["beta"]["edit"] == "value")
    fresh = {r["key"]: r["edit"] for r in attribute(synthetic_ladder(), {}, "fcff", "gordon")["rows"]}
    check("推断结果与新批次逐行一致", all(r["edit"] == fresh[r["key"]] for r in report["rows"]))


# ---------------------------------------------------------------- 「ERP 窗口」下拉的载荷

def test_a_convention_only_batch_offers_no_window() -> None:
    """玩具批次没有市场腿：四档都在，一档都不可选——惯例 6% 不会被标成某个窗口的实测。"""
    payload = CLIENT.get(f"/api/panel?code=000001&run={RUN_ID}").get_json()
    windows = payload["erp_windows"]
    check("四档窗口随载荷下发", [w["window"] for w in windows] == ["ytd", "3y", "5y", "10y"], str(windows))
    check("没有实测时一档都点不亮", not any(w["usable"] for w in windows), str([(w["window"], w["value"]) for w in windows]))
    check("没改时不带选中项", payload["erp_window"] is None, str(payload["erp_window"]))
    check("把惯例值填回去也不算选了哪一档", recompute(overrides={"erp": 0.06}).get_json()["erp_window"] is None)


def test_the_window_choice_rides_the_existing_override_path() -> None:
    """下拉不新增写入通路：选中一档＝把它的实测数交给 overrides.erp，manual.json 里存的还是那个数。

    这里临时把 runs/ 换成一份带市场腿的批次——面板与推导图共用同一套载荷，所以两处都得给同一份选项。
    """
    with tempfile.TemporaryDirectory() as tmp:
        held = store.RUNS_DIR
        try:
            store.RUNS_DIR = Path(tmp)
            directory = store.RUNS_DIR / "000001" / RUN_ID
            directory.mkdir(parents=True)
            (directory / "baseline.json").write_text(
                json.dumps(TE.ladder(market_stats=TE.stats(**TE.SPREAD)), ensure_ascii=False), encoding="utf-8"
            )
            base = CLIENT.get(f"/api/panel?code=000001&run={RUN_ID}").get_json()
            picked = next(w for w in base["erp_windows"] if w["window"] == "5y")
            check("四个窗口给出四个可用的数", all(w["usable"] for w in base["erp_windows"])
                  and len({w["value"] for w in base["erp_windows"]}) == 4, str(base["erp_windows"]))
            after = recompute(overrides={"erp": picked["value"]}).get_json()
            row = row_of(after, "erp")
            check("选中一档就是改级联那一行", row["changed"] and row["current"] == picked["value"], str(row["current"]))
            check("载荷认出当前选中的是近 5 年", after["erp_window"]["window"] == "5y", str(after["erp_window"]))
            check("选项不跟着当前值漂移", [w["value"] for w in after["erp_windows"]] == [w["value"] for w in base["erp_windows"]])
            check("顶部告示说清这次用了哪一档", any("近 5 年" in w for w in after["warnings"]), str(after["warnings"]))
            graph = CLIENT.get(f"/api/derivation?code=000001&run={RUN_ID}").get_json()
            check("推导图拿到同一份选项", [w["value"] for w in graph["erp_windows"]]
                  == [w["value"] for w in base["erp_windows"]], str(graph["erp_windows"]))
            check("骨架照旧只给行与边", {"nodes", "roots", "edges"} <= set(graph), str(sorted(graph)))
        finally:
            store.RUNS_DIR = held


def test_page_is_served() -> None:
    page = CLIENT.get("/panel").get_data(as_text=True)
    check("面板页可取", "假设修正面板" in page and "collectOverrides" in page, f"{len(page)} 字节")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
