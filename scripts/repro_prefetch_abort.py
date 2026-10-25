"""多基准预取的偶发原生崩溃（docs/caliber.md §12 第 16 条）——反复重跑它，才能验证某个修法真的把这条坑填了。

崩溃时长这样：worker 线程里第一次初始化 V8，进程当场死掉，打印
`[FATAL:partition_address_space.cc(243)] Check failed: !IsConfigurablePoolInitialized()`，
**没有任何 Python traceback**，批次只落到 `raw/`（没有 manifest、没有 baseline）。同一份代码重试即通过
（2026-10-16 连测：炸、炸、通过），所以单轮跑通了什么都说明不了，只有轮数能。

现在的修法是 `prices._warm_v8()`：线程池开工前先在当前线程建掉第一个 JS 引擎上下文。它挡的是「两个
worker 同时争进程级第一次初始化」，不是「必须挂在主线程上」——不联网实测（`py_mini_racer` 单独构造，
46 个独立进程 × 8 线程 × 40 轮，见 `probe_prefetch_warmup.py`）：不预热死 13 个，先建一次死 0 个
（主线程先建 26 个、后台线程先建 20 个，各 0 死）。所以面板那种「建链跑在后台线程里」的形态同样被覆盖。

    python scripts/repro_prefetch_abort.py                  # 只跑预取那一趟（11 条序列并发），5 轮
    python scripts/repro_prefetch_abort.py --rounds 20
    python scripts/repro_prefetch_abort.py --build 600519   # 换成整条建链，看它停在哪一步

每轮都是一个新进程：原生崩溃只与进程里第一次初始化 V8 有关，同一进程内循环复现不出来。
每轮还会自查护栏是否真生效——线程池的 worker 不许抢在第一次预热之前取数。护栏没生效单独记一类失败，
不与崩溃混数：跑通了但护栏没跑到，等于没验。
退出码：全过 0，有任何一轮崩溃或护栏没生效 1。
"""
from __future__ import annotations

import argparse
import faulthandler
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FASTFAIL = 0xC0000409  # Windows 上 fastfail 的退出码，V8 的 CHECK 失败走的就是这一条
BREAKPOINT = 0x80000003  # 实测这条 CHECK 在现场报的是这个码（2147483651），两种都算同一条坑
# 护栏没生效时的退出码：与「进程被 V8 打死」分开记，否则汇总里会把一次配置错误数成一次崩溃。
GUARD_FAILED = 97
_ABORT_NOTES = {
    FASTFAIL: "（fastfail，就是那条 V8 CHECK）",
    BREAKPOINT: "（breakpoint，就是那条 V8 CHECK）",
}


def trace_fetch_frame_calls() -> None:
    """给 `prices.fetch_frame` 包一层，打出哪个线程在调哪个函数：进程死了才知道停在谁身上。"""
    from backend.data import prices

    original = prices.fetch_frame

    def traced(func: str, **kwargs):
        print(f"  [{threading.current_thread().name}] -> {func} {kwargs}", flush=True)
        out = original(func, **kwargs)
        print(f"  [{threading.current_thread().name}] <- ok {func}", flush=True)
        return out

    prices.fetch_frame = traced


def trace_warm_up() -> list[str]:
    """记下「谁在预热之前取过数」：跑通了但护栏没生效，等于没验。

    判据只盯那条坑本身——**线程池的 worker 不许在第一次预热之前取数**。整条建链里主线程确实可能先调
    一次 akshare（`macro` 的股息率腿在预取之前就发请求），那种串行的一次构造正是安全形态，不算违规；
    真违规的是 worker 抢在预热前面同时去建上下文。
    """
    from backend.data import prices

    seen: list[str] = []
    original_warm, original_fetch = prices._warm_v8, prices.fetch_frame

    def warm() -> None:
        seen.append(f"warm@{threading.current_thread().name}")
        original_warm()

    def fetch(func: str, **kwargs):
        name = threading.current_thread().name
        if not any(line.startswith("warm@") for line in seen) and name.startswith("ThreadPoolExecutor"):
            seen.append(f"racing-fetch@{name}")
        return original_fetch(func, **kwargs)

    prices._warm_v8 = warm
    prices.fetch_frame = fetch
    return seen


def run_once(build_code: str) -> int:
    faulthandler.enable(all_threads=True)
    trace_fetch_frame_calls()
    seen = trace_warm_up()
    if build_code:
        from backend import builder

        result = builder.build(builder.BuildOptions(code=build_code))
        print(f"  建链完成：{getattr(result, 'run_id', result)}", flush=True)
    else:
        from backend.data.prices import IndexSeriesMap

        ledger = IndexSeriesMap().load()
        missing = ledger.unavailable
        print(f"  预取完成：{len(ledger.available)}/{len(ledger.codes)} 条序列到手"
              + (f"，缺 {sorted(missing)}" if missing else ""), flush=True)
    return _warm_verdict(seen)


def _warm_verdict(seen: list[str]) -> int:
    """预热没抢在线程池前面，就算这轮没炸也不算过——坑还在，只是没踩到。"""
    racing = [line for line in seen if line.startswith("racing-fetch@")]
    warms = [line for line in seen if line.startswith("warm@")]
    if racing:
        print(f"  ✗ 护栏没生效：worker 抢在预热之前取了数 {racing[:2]}", flush=True)
        return GUARD_FAILED
    if not warms:
        print("  ✗ 护栏没生效：这一轮根本没预热（_warm_v8 没被调用）", flush=True)
        return GUARD_FAILED
    print(f"  护栏生效：{warms[0]} 先于线程池的所有取数", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重跑多基准预取，统计原生崩溃的轮数")
    parser.add_argument("--rounds", type=int, default=5, help="跑几轮（默认 5）")
    parser.add_argument("--build", metavar="代码", default="", help="每轮跑整条建链，而不是只跑预取")
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)

    if options.once:
        return run_once(options.build)

    crashes: list[str] = []
    guard_failures: list[str] = []
    for round_number in range(1, options.rounds + 1):
        print(f"第 {round_number}/{options.rounds} 轮", flush=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--once"]
        if options.build:
            command += ["--build", options.build]
        code = subprocess.call(command)
        if code == GUARD_FAILED:
            guard_failures.append(f"第 {round_number} 轮护栏没生效")
            print("  ✗ 护栏没生效（这不是崩溃，是修法没跑到）", flush=True)
        elif code != 0:
            note = _ABORT_NOTES.get(code & 0xFFFFFFFF, "")
            crashes.append(f"第 {round_number} 轮退出码 {code}{note}")
            print(f"  ✗ {crashes[-1]}", flush=True)
    print(f"\n{options.rounds} 轮里 {len(crashes)} 轮被崩溃打死、{len(guard_failures)} 轮护栏没生效"
          + ("：" if (crashes or guard_failures) else "，全部通过。"), flush=True)
    for line in crashes + guard_failures:
        print("  " + line, flush=True)
    return 1 if (crashes or guard_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
