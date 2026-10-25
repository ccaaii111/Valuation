"""Build the machine baseline assumption ladder for one A-share ticker.

The fetching and measuring lives in backend/builder.py, which the web panel runs too — this file only
parses flags and prints.

    python scripts/build_baseline.py 600519
    python scripts/build_baseline.py 600519 --from-run 20260921-101500   # 复现该批次的原始答复，另起一个新批次
    python scripts/build_baseline.py 600519 --no-risk-free               # leave the rate blank
    python scripts/build_baseline.py 600519 --risk-free 0.0168           # manual override
    python scripts/build_baseline.py 600519 --risk-free-basis spot       # price off the observation, not the mean
    python scripts/build_baseline.py 600519 --terminal-growth-basis central  # 乐观/sustainable/convention 同理可换
    python scripts/build_baseline.py 600519 --benchmark 000300           # beta against 沪深300
    python scripts/build_baseline.py 600519 --beta 0.90                  # 给定 β，跳过行情回归
    python scripts/build_baseline.py 600519 --no-beta                    # 跳过回归；没有 --beta 时折现率出不来
    python scripts/build_baseline.py 600519 --horizon 12                 # 显性预测期拉到 12 年再回落
    python scripts/build_baseline.py 600519 --no-normalize               # EBIT 率分子退回报表原值

`--no-normalize` 关掉的是定价里的一个减法，不是关掉一行显示：默认档会把「归母净利润 − 扣非归母净利润」
按位置份额反推成税前额，从两档 EBIT 率的分子里剔掉（见 docs/caliber.md §5.2 的非经常性损益那一节）。年报窗口里有一年
过不了护栏（两条加总恒等式不闭合、缺扣非、反推分母不为正、剔完会翻号），整个窗口都不剔，
链上「非经常性损益剔除额」那一行会写明是哪一年、因为什么。金融股落在第一条上就出局：
它们的营业总成本结构让 E2 天生不闭合。

`--horizon` 改的是「详细预测几年」这一个数：年数是输入，每年回落多少个百分点是它算出来的输出，
所以这里没有 `--fade` 之类的旋钮。年数在 5 与 12 之间钳，下限还要容得下券商已给的锚点年，钳过就在那一行
与顶层警告各留一句。复现（`--from-run`）时也吃这个参数：同一份原始答复，换个显性期就是另一个批次。

`--no-beta` 不再意味着「β 用占位 1.00」：β 只能来自回归或来自你手上那个 `--beta`，两者都没有就以 2 退出
（取数证据仍落在 runs/ 里）。失败分三类文案：`TickerUnavailable` 是 A 股名录里就没有这只票（已退市／从没
上市／代码写错），一次网都不上，以 3 退出，出路只有核对代码或刷新名录；`PricesUnavailable` 是联网了却没取到
行情（源抖动，或名录还新的票刚退市还没被剔掉），退 2；`BetaNotApplicable` 才是 CAPM 在这个数上折不出发
（β≤0、重叠不足 3 周），也退 2。
`--from-run` 只读源批次的 raw/：证据复制成一个新批次，源批次的 baseline.json 与 manifest.json 原样不动，
所以同一份原始答复可以并存多个口径的基线，谁也不覆盖谁。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.builder import RISK_FREE_BASIS, BuildOptions, TickerUnavailable, build  # noqa: E402
from backend.data import prices  # noqa: E402
from backend.model.assumptions import (  # noqa: E402
    HORIZON_MAX_YEARS,
    HORIZON_MIN_YEARS,
    HORIZON_TARGET_YEARS,
    TERMINAL_GROWTH_BASIS,
    BetaNotApplicable,
)


def _fmt(baseline) -> str:
    if isinstance(baseline, dict):
        return ", ".join(f"{k}:{_fmt(v)}" for k, v in sorted(baseline.items(), key=lambda kv: str(kv[0])))
    if baseline is None:
        return "—"
    if isinstance(baseline, bool):
        return "是" if baseline else "否"
    if isinstance(baseline, float):
        return f"{baseline:.4f}" if abs(baseline) < 100 else f"{baseline:,.2f}"
    return str(baseline)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成假设链基线")
    parser.add_argument("code")
    parser.add_argument("--from-run", help="复现既有取数批次的原始答复，不重新联网；结果写进一个新批次，源批次不动")
    parser.add_argument("--no-risk-free", action="store_true", help="跳过无风险利率取数，留空待人工填")
    parser.add_argument("--risk-free", type=float, dest="risk_free", help="手工指定无风险利率（小数），覆盖取数结果")
    parser.add_argument(
        "--risk-free-basis",
        choices=RISK_FREE_BASIS,
        default="norm",
        help="进级联的无风险利率：多年中枢（默认）或即期观测值；两个数都会留在假设链上",
    )
    parser.add_argument("--no-beta", action="store_true", help="跳过行情回归；不给 --beta 就没有 β，建链以「没取到行情」退出")
    parser.add_argument("--beta", type=float, help="手工指定贝塔，跳过周频行情回归；必须是正数，是给 β 为负那只票留的出路")
    parser.add_argument(
        "--benchmark",
        default=prices.DEFAULT_BENCHMARK,
        help=f"市场组合的指数代码（默认 {prices.DEFAULT_BENCHMARK} {prices.BENCHMARK_NAMES[prices.DEFAULT_BENCHMARK]}）：β 对它回归、ERP 取它的超额收益，"
        f"两处「市场」必须是同一个。有全收益孪生者（{('、'.join(prices.TR_SYMBOLS))}）的市场腿直接取中证全收益指数，"
        "没有的退回「价格指数 + 乐咕两市股息率」",
    )
    parser.add_argument(
        "--terminal-growth-basis",
        choices=TERMINAL_GROWTH_BASIS,
        default="conservative",
        help="进级联的永续增速：保守=近十年最差增速之半（默认）、中枢=近五年营收加权、"
        "乐观=历史最好增速、sustainable=再投资增速（近五年逐年 (1−分红率)×加权 ROE 取中位）、"
        "convention=惯例 2.5%%；四档都会留在假设链上，再投资档要本批同时有分红方案与年报加权 ROE，"
        "缺凭证只作废这一档并退回惯例值。实测档为负时进级联的那一档兜底在 0"
        "（默认不给永续负增长定价），参考行仍记实测原值",
    )
    parser.add_argument(
        "--terminal-growth",
        type=float,
        dest="terminal_growth",
        help="手工指定永续增长率（小数），覆盖四档测算；人工值不抬也不压，填负数就按负数折",
    )
    parser.add_argument(
        "--exclude-lease-debt",
        action="store_true",
        help="不把租赁负债（非流动部分）计为有息负债，同时去掉 FCFE 的租赁净融资行",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="EBIT 率的分子退回报表原值，不剔非经常性损益；默认剔除（金额取官方归母与扣非归母之差，"
        "两条加总恒等式都闭合才认这个位置，窗口里任一年没过护栏就整窗不剔）",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        dest="horizon",
        help=f"显性预测期年数（整数年，默认 {HORIZON_TARGET_YEARS}）：下限是 max({HORIZON_MIN_YEARS}, 券商锚点数 + 1)，"
        f"上限 {HORIZON_MAX_YEARS} 年（准则那句「详细预测期一般在 8-12 年」的上沿）。"
        "每年回落多少个百分点是它算出来的商，不在这里填；超界不报错，钳到边界并在假设链上留一条警告",
    )
    args = parser.parse_args(argv)

    options = BuildOptions(
        code=args.code,
        from_run=args.from_run or "",
        no_risk_free=args.no_risk_free,
        risk_free=args.risk_free,
        risk_free_basis=args.risk_free_basis,
        no_beta=args.no_beta,
        beta=args.beta,
        benchmark=args.benchmark,
        exclude_lease_debt=args.exclude_lease_debt,
        normalize=not args.no_normalize,
        terminal_basis=args.terminal_growth_basis,
        terminal_growth=args.terminal_growth,
        horizon=args.horizon,
    )
    try:
        result = build(options)
    except TickerUnavailable as exc:  # 名录里就没有这只票：一次网都没上，重跑也是同一个答案
        print(f"查无此票：{exc}", file=sys.stderr)
        return 3
    except prices.PricesUnavailable as exc:  # 行情没取到：这只票（或这次网络）不行，不是模型不适用
        print(f"取数失败：{exc}", file=sys.stderr)
        return 2
    except BetaNotApplicable as exc:
        print(f"不适用：{exc}", file=sys.stderr)
        return 2
    ladder, store = result.ladder, result.store

    print(f"{ladder.ticker['name']} ({ladder.ticker['secucode']})  报表截至 {ladder.as_of}")
    print(f"run: {store.dir.relative_to(ROOT)}")
    if store.replayed_from:
        source = store.replayed_from["run_id"]
        print(f"复现自: runs/{store.code}/{source}（原始答复取于 {store.replayed_from['created_at']}，那一批没有被动过）")
    for group in ladder.groups:
        print(f"\n[{group['title']}]")
        for row in group["rows"]:
            flag = {"data": "", "estimate": " 预估", "convention": " 惯例", "derived": " 级联"}.get(row.kind, "")
            conf = f" conf={row.confidence}" if row.confidence is not None else ""
            print(f"  {row.label:<28}{flag}{conf:<12} = {_fmt(row.baseline):<46} [{row.unit}] <- {row.source}")
    print(f"\n写出: {result.baseline_path}")
    print(f"取数凭证: {result.manifest_path} ({store.source_count} 个来源)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
