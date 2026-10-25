"""Run the DCF engine against a stored baseline, with optional manual overrides.

    python scripts/run_valuation.py 600519
    python scripts/run_valuation.py 600519 --attribution --overrides '{"beta": 0.6}'
    python scripts/run_valuation.py 600519 --overrides my.json --attribution
    python scripts/run_valuation.py 000858 --overrides runs/000858/20260923-213848/manual.json
    python scripts/run_valuation.py 600028 --method fcfe --terminal uem --rate 0.09

--overrides maps a ladder row key to its new value, given inline (JSON starting with a brace) or as a
path to a .json file. Year-map rows merge per year, so only the years you name move and the rest of
the path is preserved; unknown keys or years are rejected.
    {"revenue_growth_path": {"2029": 0.08}, "terminal_growth": 0.03}
The panel's 「保存修正」 document (manual.json) is accepted too: only its overrides rows are reused,
while the caliber it was saved under is reported rather than applied, so the command line stays
authoritative. Money rows arrive as absolute values — the ±% boxes live in the panel alone.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.model.attribution import attribute  # noqa: E402
from backend.model.dcf import (  # noqa: E402
    ENGINE_INPUTS,
    COMBINATIONS,
    apply_overrides,
    cross_check,
    evaluate,
    inputs_from_ladder,
)


def latest_baseline(code: str) -> Path:
    candidates = sorted((ROOT / "runs" / code).glob("*/baseline.json"))
    if not candidates:
        raise SystemExit(f"runs/{code} 下没有 baseline.json，请先跑 build_baseline.py")
    return candidates[-1]


def _fmt(value, unit: str = "") -> str:
    """Unit-aware: values below 1 are magnitudes, not percentages, unless the row says so."""
    if isinstance(value, dict):
        return " ".join(f"{k}:{_fmt(v, unit)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        if unit == "pct":
            return f"{value:+.2%}"
        return f"{value:,.2f}"
    return str(value)


def _clip(text: str, width: int = 16) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def load_overrides(text: str | None) -> tuple[dict, dict]:
    """Accept either an inline JSON object or a path to one, and fail with a usable message.

    Returns (overrides, document). `document` is non-empty only for the wrapper the panel writes
    (runs/<code>/<run>/manual.json), whose calibration fields the CLI deliberately ignores — only the
    corrected rows are reused; the caliber still comes from the command line.
    """
    if not text:
        return {}, {}
    if not text.lstrip().startswith("{"):
        candidate = Path(text)
        if not candidate.is_file():
            raise SystemExit(f"修正内容既不是 JSON 对象（以 {{ 开头）也不是存在的文件: {text}")
        text = candidate.read_text(encoding="utf-8")
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"修正 JSON 解析失败（{exc}），示例: --overrides '{{\"terminal_growth\": 0.02}}'") from None
    if not isinstance(loaded, dict):
        raise SystemExit("修正 JSON 必须是对象：{\"terminal_growth\": 0.02}")
    if isinstance(loaded.get("overrides"), dict):
        wrapper = {key: value for key, value in loaded.items() if key != "overrides"}
        return loaded["overrides"], wrapper
    return loaded, {}


def print_schedule(result) -> None:
    header = f"{'年份':<6}{'折现期':>7}{'营收':>10}{'EBIT':>10}{'税后经营利润':>13}{'FCFF':>10}{'FCFE':>10}{'FCFF现值':>10}"
    print(header)
    for line in result.lines:
        year = f"{line.year}*" if line.stub < 1.0 else str(line.year)
        print(
            f"{year:<6}{line.t:>7.2f}{line.revenue:>10,.0f}{line.ebit:>10,.0f}{line.nopat:>13,.1f}"
            f"{line.fcff:>10,.1f}{line.fcfe:>10,.1f}{line.pv_fcff:>10,.1f}"
        )
    stub = (result.params or {}).get("stub") or 1.0
    if stub < 1.0:
        print(
            f"＊{result.lines[0].year} 距报表截至 {(result.params or {}).get('report_as_of')} 只剩 {stub:.2f} 年，"
            "该行各列金额与折现期都只算这一段"
        )
    total = result.pv_explicit + result.pv_terminal
    print(
        f"显性期现值 {result.pv_explicit:,.1f} | 终值 {_fmt(result.terminal_value)} 现值 {result.pv_terminal:,.1f}"
        + (f" | 占比 {result.pv_terminal / total:.1%}" if total else " | 占比 —（现值为 0）")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DCF 估值与人工修正归因")
    parser.add_argument("code")
    parser.add_argument("--run", help="指定取数批次目录名，默认最新")
    parser.add_argument("--method", default="fcff", choices=["fcff", "fcfe"])
    parser.add_argument("--terminal", default="gordon", choices=["gordon", "exit_multiple", "uem"])
    parser.add_argument("--rate", type=float, help="覆盖折现率（小数）")
    parser.add_argument("--mid-year", dest="mid_year", action="store_true", default=None)
    parser.add_argument("--no-mid-year", dest="mid_year", action="store_false")
    parser.add_argument("--overrides", help="人工修正：内联 JSON、.json 路径，或面板「保存修正」写出的 manual.json（只取其 overrides 行）")
    parser.add_argument("--attribution", action="store_true", help="输出逐行变动百分比与归因")
    parser.add_argument("--json", type=Path, help="把完整结果写入 JSON")
    args = parser.parse_args(argv)

    path = ROOT / "runs" / args.code / args.run / "baseline.json" if args.run else latest_baseline(args.code)
    ladder = json.loads(path.read_text(encoding="utf-8"))
    overrides, document = load_overrides(args.overrides)

    try:
        inputs = apply_overrides(inputs_from_ladder(ladder), overrides)
    except ValueError as exc:
        raise SystemExit(f"修正文件不可用: {exc}")
    ticker = ladder.get("ticker", {})
    print(f"{ticker.get('name')} ({ticker.get('secucode')})  报表截至 {ticker.get('report_as_of')}  基线 {path.parent.name}")
    if document:
        if document.get("baseline") and document["baseline"] != path.parent.name:
            print(f"  ! 修正存自批次 {document['baseline']}，当前基线 {path.parent.name}——行键一样但基线数值可能已变")
        saved = "/".join(str(document[k]) for k in ("method", "terminal", "rate") if document.get(k) is not None)
        now = "/".join(str(v) for v in (args.method, args.terminal, args.rate) if v is not None)
        if saved and saved != now:
            print(f"  修正文档口径 {saved}，本次按命令行 {now}（文档只取修正行）")
    missing = sorted(ENGINE_INPUTS - set(inputs))
    if missing:
        # 老批次本来就没有后来才加的那几行（如「显性预测期」），这种缺是正常存档；
        # 真正要担心的是新建的链与引擎改了名却不同步——两种都会走到这一句，所以按批次日期自己分辨。
        print(f"  ! 基线缺少引擎输入键 {missing}（老批次没这一行属正常，新链缺它就是命名漂移）")

    print("\n[口径 × 终值 矩阵，每股价值 元]")
    for method, terminal in COMBINATIONS:
        result = evaluate(inputs, method, terminal, rate=args.rate, mid_year=args.mid_year)
        note = "" if result.per_share is not None else f"  → {result.warnings[0] if result.warnings else '无法计算'}"
        print(f"  {method.upper():<5} / {terminal:<14} = {_fmt(result.per_share):>12}{note}")

    result = evaluate(inputs, args.method, args.terminal, rate=args.rate, mid_year=args.mid_year)
    print(f"\n[{args.method.upper()} / {args.terminal} 明细，折现率 {_fmt(result.rate, 'pct')}，年中折现 {'是' if result.mid_year else '否'}]")
    if result.lines:
        print_schedule(result)
    print(f"企业价值 {_fmt(result.enterprise_value)} | 股权价值 {_fmt(result.equity_value)} | 每股 {_fmt(result.per_share)} 元")
    if result.upside is not None:
        print(f"现价 {_fmt(inputs.get('price'))} → 上行空间 {result.upside:+.2%}")
    for note in cross_check(inputs, rate=args.rate, mid_year=args.mid_year):
        print(f"自洽性: {note}")
    for warning in result.warnings:
        print(f"  ! {warning}")

    report: dict = {"baseline": str(path), "inputs": {k: v for k, v in inputs.items()}, "result": result.to_json()}
    if args.attribution:
        report["attribution"] = attribute(ladder, overrides, args.method, args.terminal, mid_year=args.mid_year, rate=args.rate)
        att = report["attribution"]
        print(f"\n[逐行归因] 基线每股 {_fmt(att['baseline_per_share'])} → 当前 {_fmt(att['per_share'])} "
              f"({_fmt(att['total_change_pct'], 'pct')})，改动 {att['edited_row_count']} 行")
        print(f"  {'假设':<26}{'基线':>16}{'当前':>16}{'改动%':>10}{'归因%':>10}  （归因相对基线每股值，单行改动时等于总变动）")
        coe_base, coe_now = att["rates"]["cost_of_equity"]
        wacc_base, wacc_now = att["rates"]["wacc"]
        if (coe_base, wacc_base) != (coe_now, wacc_now):
            pinned = (
                "（已 --rate 强制指定，级联结果未参与折现）"
                if args.rate is not None and abs(args.rate - (wacc_now if args.method == "fcff" else coe_now)) > 1e-12 else ""
            )
            print(
                f"  折现率级联: 股权成本 {_fmt(coe_base, 'pct')} → {_fmt(coe_now, 'pct')}，"
                f"WACC {_fmt(wacc_base, 'pct')} → {_fmt(wacc_now, 'pct')}{pinned}"
            )
        if not overrides:
            print("  （本次未提供 --overrides，逐行归因表为空）")
        for row in att["rows"]:
            if not row["changed"]:
                continue
            base_s = _fmt(row["baseline"], row["unit"])
            now_s = _fmt(row["current"], row["unit"])
            change = row["change_pct"]
            chg_s = "分年 ↓" if isinstance(change, dict) else (
                "基线为0" if change is None else _fmt(change, "pct")
            )
            print(
                f"  {row['label']:<26}{_clip(base_s):>16}{_clip(now_s):>16}"
                f"{_clip(chg_s, 12):>13}{_clip(_fmt(row['contribution_pct'], 'pct'), 12):>13}"
            )
            if isinstance(row["baseline"], dict) or isinstance(row["current"], dict):
                print(f"      基线 {base_s}")
                print(f"      当前 {now_s}")
                moved = {k: v for k, v in (change or {}).items() if v} if isinstance(change, dict) else None
                if moved:
                    print(f"      改动 {_fmt(moved, 'pct')}")
            if row["rate_if_reverted"] is not None and att["rate"] is not None and row["rate_if_reverted"] != att["rate"]:
                print(f"      折现率: 仅回退此行 → {_fmt(row['rate_if_reverted'], 'pct')}，当前场景 {_fmt(att['rate'], 'pct')}")
            if not row["editable"]:
                print("      ! 该行当前不参与估值计算（改动未被定价）")

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写出: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
