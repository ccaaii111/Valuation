"""不联网地问一句：预取那条线程池偶发的原生崩溃，到底是不是「两条线程争进程里第一次初始化 JS 引擎」。

docs/caliber.md §12 第 16 条记的现场死法是 `[FATAL:partition_address_space.cc(243)] Check failed:
!IsConfigurablePoolInitialized()`，进程当场没、**没有 Python traceback**，所以光看端到端重跑只能数出
「没再犯」，问不出「为什么」。这个探针把唯一相关的那一步单独拿出来重复——`py_mini_racer.MiniRacer()`
的构造（akshare 的 `stock_zh_index_daily` 每调一次就造一个），完全不碰网络，几十秒一轮。

三种做法各代表一个假设：

    python scripts/probe_prefetch_warmup.py --parent --warm 0 --procs 20   # 什么都不做
    python scripts/probe_prefetch_warmup.py --parent --warm 1 --procs 20   # 主线程先单独建一个
    python scripts/probe_prefetch_warmup.py --parent --warm 2 --procs 20   # 后台线程先单独建一个

`--warm 2` 那条是这道题的关键，也是当初把我引偏的地方：如果崩的原因是「非主线程」，`prices._warm_v8()`
放在 `IndexSeriesMap.load()` 里就修不了面板那条路（`builder.build()` 跑在作业线程里，不在主线程）。
实测 2026-10-23：不预热 46 个进程死 13 个，主线程先建 26 个死 0 个，后台线程先建 20 个死 0 个——
争的是「第一次」，不是「主线程」，所以那条预热放在哪条线程上都算数。

每个进程都是一个新解释器：同一个进程里第二次以后的初始化早已落地，循环复现不出来。
退出码：全活 0，有任何一个进程没回来 1。这只是机制探针，不是验收——端到端的计数归
`repro_prefetch_abort.py`。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FASTFAIL = 0xC0000409  # Windows 上 fastfail 的退出码，V8 的 CHECK 失败走的就是这一条
BREAKPOINT = 0x80000003  # 现场那条 CHECK 实际报的码（2147483651），两种都算同一条坑
_NOTES = {FASTFAIL: "fastfail", BREAKPOINT: "breakpoint"}
_LABELS = {0: "不预热", 1: "主线程先建", 2: "后台线程先建"}


def child(warm: int, rounds: int, threads: int) -> int:
    from py_mini_racer import MiniRacer

    def build() -> None:
        MiniRacer().eval("1+1")

    if warm:
        if warm == 1:
            build()
        else:
            # 单独一条线程建完再 join：模拟面板那种「建链跑在作业线程里」的形态。
            first = threading.Thread(target=build, name="prewarm-off-main")
            first.start()
            first.join()

    built = 0
    tally = threading.Lock()
    for _ in range(rounds):
        gate = threading.Barrier(threads)  # 放行即同时构造：把「争第一次」压到最大概率

        def make(slot: int) -> None:
            nonlocal built
            gate.wait()
            MiniRacer().eval(f"1+{slot}")
            with tally:
                built += 1

        pool = [threading.Thread(target=make, args=(i,)) for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()
    print(f"child ok: {built} contexts")
    return 0


def parent(warm: int, procs: int, rounds: int, threads: int) -> int:
    dead: list[str] = []
    for index in range(1, procs + 1):
        code = subprocess.call([sys.executable, str(Path(__file__).resolve()), "--child",
                               "--warm", str(warm), "--rounds", str(rounds), "--threads", str(threads)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if code:
            dead.append(f"第 {index} 个进程退出码 {_NOTES.get(code & 0xFFFFFFFF, hex(code))}")
            print("  ✗", dead[-1], flush=True)
    print(f"{_LABELS[warm]}（{threads} 线程 × {rounds} 轮）：{procs} 个进程里 {len(dead)} 个没活着回来", flush=True)
    return 1 if dead else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="并发构造 MiniRacer，统计原生崩溃的进程数")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent", action="store_true", help="开 N 个新进程各跑一遍")
    parser.add_argument("--warm", type=int, choices=(0, 1, 2), default=0,
                        help="0 不预热／1 主线程先建一次／2 后台线程先建一次")
    parser.add_argument("--procs", type=int, default=20, help="跑几个独立进程（默认 20）")
    parser.add_argument("--rounds", type=int, default=40, help="每个进程跑几轮（默认 40）")
    parser.add_argument("--threads", type=int, default=8, help="每轮几条线程同时构造（默认 8）")
    options = parser.parse_args(argv)
    if options.child:
        return child(options.warm, options.rounds, options.threads)
    return parent(options.warm, options.procs, options.rounds, options.threads)


if __name__ == "__main__":
    sys.exit(main())
