"""Tests for the normalised risk-free rate: 即期值与多年中枢并列，其中一个进级联.

No network — window_stats is fed synthetic payloads and the ladder gets hand-built stats dicts.

    python tests/test_rf_normalization.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.data.macro import SERIES, risk_free_stats, window_stats  # noqa: E402
from backend.model.assumptions import ERP, build_ladder  # noqa: E402
from backend.model.attribution import attribute  # noqa: E402
from backend.model.dcf import discount_rates, inputs_from_ladder  # noqa: E402
from fixtures import synthetic_snapshot  # noqa: E402
from backend.builder import pick_risk_free  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


# ---------------------------------------------------------------- synthetic 国债收益率序列

def rf_payload(pairs: list[tuple[date, float | None]]) -> dict:
    return {
        "source": "akshare bond_zh_us_rate",
        "column": SERIES,
        "rows": [{"日期": day.isoformat(), SERIES: value} for day, value in pairs],
    }


def five_year_series(start: date = date(2021, 1, 1)) -> list[tuple[date, float]]:
    """1900 个日频点。最后 1827 点为窗口内：1825×2.5 加 1.5 与 3.5，均值恰为 2.5。

    窗口外的 73 点取 9.9——只要裁剪出错（按今天而非最后一个观测取窗口），均值就会被拉到 2.78。
    """
    return (
        [(start + timedelta(days=i), 9.9) for i in range(73)]
        + [(start + timedelta(days=i), 2.5) for i in range(73, 1898)]
        + [(start + timedelta(days=1898), 1.5), (start + timedelta(days=1899), 3.5)]
    )


def window(spot=0.0168, mean=0.0231, years=5, obs=1250, median=None, lo=None, hi=None, unavailable="") -> dict:
    """A hand-built risk_free_stats-shaped dict, for the ladder-side tests."""
    return {
        "series": "中国国债收益率10年",
        "window_years": years,
        "spot": spot,
        "spot_source": f"akshare 中国国债收益率10年 @ 2026-09-22 = {spot * 100:.4f}%",
        "observations": obs,
        "first_day": "2021-09-22",
        "last_day": "2026-09-22",
        "mean": mean,
        "median": mean if median is None else median,
        "min": mean if lo is None else lo,
        "max": mean if hi is None else hi,
        "unavailable": unavailable,
    }


def ladder(rf_value: float | None, rf_window: dict | None, basis: str) -> dict:
    snapshot = synthetic_snapshot()
    built = build_ladder(
        snapshot, snapshot["consensus"], snapshot["quote"], rf_value,
        rf_basis=basis, rf_window=rf_window, beta_manual=1.00,  # 本套件只测 rf，β 得显式给
    )
    return built.to_json()


def row_of(lad: dict, key: str) -> dict:
    return next(r for r in attribute(lad)["rows"] if r["key"] == key)


# ---------------------------------------------------------------- window_stats

def test_window_stats_measures_the_trimmed_window() -> None:
    stats = window_stats(rf_payload(five_year_series()))
    check("窗口只取最后一个观测之前的 5 年", stats["observations"] == 1827, str(stats["observations"]))
    check("窗口起点即裁剪后的第一天", stats["first_day"] == (date(2021, 1, 1) + timedelta(days=73)).isoformat(), stats["first_day"])
    check("窗口终点即最新观测", stats["last_day"] == (date(2021, 1, 1) + timedelta(days=1899)).isoformat(), stats["last_day"])
    check("中枢 = 窗口内日频均值（百分点已换算）", abs(stats["mean"] - 0.025) < 1e-12, f"{stats['mean']}")
    check("中位数", stats["median"] == 0.025, str(stats["median"]))
    check("区间", (stats["min"], stats["max"]) == (0.015, 0.035), f"{stats['min']}~{stats['max']}")
    check("即期是最后一个观测", stats["spot"] == 0.035, str(stats["spot"]))
    check("护栏未触发时 unavailable 为空", stats["unavailable"] == "", stats["unavailable"])


def test_window_stats_anchors_on_the_snapshot_not_today() -> None:
    """--from-run 复现旧批次时不能因为日期推移而少算几年。"""
    recent = window_stats(rf_payload(five_year_series(date(2021, 1, 1))))
    old = window_stats(rf_payload(five_year_series(date(2011, 1, 1))))
    check("同一形状前移十年，中枢不变", recent["mean"] == old["mean"], f"{recent['mean']} vs {old['mean']}")
    check("同一形状前移十年，观测数不变", recent["observations"] == old["observations"], str(old["observations"]))


def test_short_snapshot_cannot_produce_a_centre() -> None:
    """旧批次只记录了 45 天窗口的快照：宁缺毋滥，不能把 30 天均值叫作五年中枢。"""
    payload = rf_payload([(date(2026, 9, 1) + timedelta(days=i), 2.0) for i in range(30)])
    stats = window_stats(payload)
    check("短窗口拒绝给出中枢", stats["mean"] is None, str(stats["mean"]))
    check("拒绝理由含样本数与下限", "30" in stats["unavailable"] and "下限" in stats["unavailable"], stats["unavailable"])
    check("短窗口的即期仍然可用", stats["spot"] == 0.02, str(stats["spot"]))


def test_sparse_snapshot_fails_the_span_guard() -> None:
    """够不上时间跨度的样本（历史中断/月度序列）也不配叫中枢。"""
    payload = rf_payload([(date(2021, 1, 1) + timedelta(days=i), 2.0) for i in range(1100)])
    stats = window_stats(payload)
    check("跨度不足时拒绝", stats["mean"] is None, str(stats["mean"]))
    check("理由写明跨度", "跨度" in stats["unavailable"], stats["unavailable"])


def test_unit_shift_inside_the_window_is_refused() -> None:
    pairs = five_year_series()
    pairs[-500] = (pairs[-500][0], 900.0)  # 一个荒谬读数混在窗口中段
    stats = window_stats(rf_payload(pairs))
    check("窗口内异常读数拒绝中枢", stats["mean"] is None, str(stats["mean"]))
    check("理由指向单位", "单位" in stats["unavailable"], stats["unavailable"])


def test_empty_payload() -> None:
    stats = window_stats({"rows": []})
    check("空序列既无即期也无中枢", stats["spot"] is None and stats["mean"] is None)
    check("空序列给出 unavailable", stats["unavailable"].startswith("unavailable"), stats["unavailable"])


class StubStore:
    """A run store with (or without) a recorded risk_free snapshot — the replay paths, offline."""

    run_id = "20260921-101500"
    snapshot_ref = run_id  # 自己联网取的，留痕就指自己

    def __init__(self, recorded: dict | None = None):
        self._recorded = recorded

    def load(self, name: str):
        return self._recorded if name == "risk_free" else None

    def record(self, item) -> None:  # pragma: no cover - must never be reached
        raise AssertionError("replay 不应写入新快照")


def test_replay_paths_stay_offline() -> None:
    stats, source = risk_free_stats(StubStore(), live=False)
    check("缺快照且禁网时不联网也不崩", stats is None, str(stats))
    check("来源说明批次里没有快照", "没有" in source and "risk_free" in source, source)
    recorded = rf_payload(five_year_series())
    replayed, source = risk_free_stats(StubStore(recorded), live=False)
    check("有快照时按快照复现", replayed["mean"] == window_stats(recorded)["mean"], str(replayed["mean"]))
    check("复现的来源串标出批次", "快照复现" in source and StubStore.run_id in source, source)
    short, _ = risk_free_stats(StubStore(rf_payload([(date(2026, 9, 1) + timedelta(days=i), 2.0) for i in range(30)])), live=False)
    check("旧批次的 45 天快照仍能出即期，但没有中枢", short["mean"] is None and short["spot"] == 0.02, str(short["mean"]))


# ---------------------------------------------------------------- basis choice

def test_basis_choice() -> None:
    full = window()
    check("默认用中枢进级联", pick_risk_free(full, None, "norm") == (0.0231, "近5年中枢"))
    check("可切回即期", pick_risk_free(full, None, "spot") == (0.0168, "即期"))
    check("人工值覆盖两者", pick_risk_free(full, 0.02, "norm")[0] == 0.02)
    check("人工值把基准名也让出去", "--risk-free" in pick_risk_free(full, 0.02, "norm")[1])
    empty = window(mean=None, unavailable="近 5 年窗口只有 30 个日频观测")
    value, basis = pick_risk_free(empty, None, "norm")
    check("中枢不可用时退回即期", value == 0.0168, str(value))
    check("退回写进基准名，面板上看得到", "退回" in basis, basis)
    check("整条序列都取不到时留空", pick_risk_free(None, None, "norm") == (None, ""))


# ---------------------------------------------------------------- ladder rows

def test_ladder_shows_both_numbers() -> None:
    lad = ladder(0.0231, window(), "近5年中枢")
    driver, spot, norm = row_of(lad, "risk_free"), row_of(lad, "risk_free_spot"), row_of(lad, "risk_free_norm")
    check("级联行取中枢", driver["baseline"] == 0.0231, str(driver["baseline"]))
    check("即期参考行同时在场", spot["baseline"] == 0.0168, str(spot["baseline"]))
    check("中枢参考行同时在场", norm["baseline"] == 0.0231, str(norm["baseline"]))
    check("级联行的凭证两个数都写出", "1.6800%" in driver["evidence"] and "2.3100%" in driver["evidence"], driver["evidence"])
    check("note 说明本行用了哪个基准", "近5年中枢" in driver["note"], driver["note"])
    check("中枢的凭证带样本量与区间", "1250 个观测" in norm["evidence"], norm["evidence"])
    check("参考行不进级联，面板上只读", spot["editable"] is False and norm["editable"] is False)
    check("未改动时参考行不算未定价改动", lad and attribute(lad)["unpriced_edits"] == [], str(attribute(lad)["unpriced_edits"]))


def test_spot_basis_keeps_the_same_two_reference_rows() -> None:
    lad = ladder(0.0168, window(), "即期")
    check("即期基准下级联行等于即期", row_of(lad, "risk_free")["baseline"] == 0.0168)
    check("中枢仍作为参考留在链上", row_of(lad, "risk_free_norm")["baseline"] == 0.0231)
    check("note 标明当前采用即期", "采用「即期」" in row_of(lad, "risk_free")["note"], row_of(lad, "risk_free")["note"])


def test_missing_window_degrades_the_rows() -> None:
    lad = ladder(0.0168, None, "即期（中枢不可用而退回）")
    check("无窗口统计时中枢行为空", row_of(lad, "risk_free_norm")["baseline"] is None)
    check("降级说明出现在凭证", "不可用" in row_of(lad, "risk_free")["evidence"], row_of(lad, "risk_free")["evidence"])
    check("级联行照旧取到即期", row_of(lad, "risk_free")["baseline"] == 0.0168)


def test_rate_failure_leaves_the_row_blank() -> None:
    lad = ladder(None, None, "")
    driver = row_of(lad, "risk_free")
    check("取数失败时级联行为空", driver["baseline"] is None)
    check("取数失败时标为惯例并提示人工填", driver["kind"] == "convention" and "--rate" in driver["note"], driver["note"])


# ---------------------------------------------------------------- engine

def test_engine_prices_off_the_adopted_rate() -> None:
    norm_inputs = inputs_from_ladder(ladder(0.0231, window(), "近5年中枢"))
    spot_inputs = inputs_from_ladder(ladder(0.0168, window(), "即期"))
    norm_equity, _ = discount_rates(norm_inputs)
    spot_equity, _ = discount_rates(spot_inputs)
    check("中枢进入 CAPM", abs(norm_equity - (0.0231 + 1.0 * ERP)) < 1e-9, f"{norm_equity}")
    check("即期与中枢差出 6.3bp 股权成本", abs(norm_equity - spot_equity - 0.0063) < 1e-9, f"{norm_equity - spot_equity}")
    check("摊平周期后 r−g 变宽", norm_equity - 0.025 > spot_equity - 0.025)


def test_human_override_of_the_driver_still_wins() -> None:
    report = attribute(ladder(0.0231, window(), "近5年中枢"), {"risk_free": 0.03})
    base_equity, now_equity = report["rates"]["cost_of_equity"]
    check("改级联行会重算股权成本", abs(now_equity - (0.03 + 1.0 * ERP)) < 1e-9, f"{now_equity}")
    check("机器基线仍是中枢值", abs(base_equity - (0.0231 + 1.0 * ERP)) < 1e-9, f"{base_equity}")
    check("改动登记为已定价", report["unpriced_edits"] == [], str(report["unpriced_edits"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
