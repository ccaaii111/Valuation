"""取数前的准入：名录说这只票不在市，就一个来源都不碰（#62，2026-10-15 定的口径）.

不联网。名录写在临时目录里，批次层与取数层换成「一碰就炸」的桩——这套件钉的就是「没碰」本身：
被拦时不建目录、不排队。另外钉住两条边界：复现不查名录（盘上的历史证据今天否决不了），以及名录答不了时
放行（没有索引、或索引旧过七天——把「不知道」写成「查无此人」比白跑一遍更糟）。

    python tests/test_admission.py
"""
from __future__ import annotations

import atexit
import importlib.util
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import builder  # noqa: E402
from backend.data import prices, universe as U  # noqa: E402
from backend.data.em_company import Ticker  # noqa: E402
from backend.model.assumptions import BetaNotApplicable  # noqa: E402

FAILURES = 0
# 名录的 `generated_at` 挂在真实时钟前一天：本套件里 `builder.build()` 与 CLI 都按当下判断，
# 写死一个日期会让这套件在第七天之后自己变卦（2026-10-15 那份桩就是这么在 10-22 中午红的）。
# 需要钉边界的两条（第七天、第八天）把 `now` 显式传进去，锚在同一时刻。
GENERATED = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
NOW = GENERATED + timedelta(days=7)
ROWS = [("600519", "贵州茅台"), ("000001", "平安银行"), ("830799", "艾融软件")]
STAMP = GENERATED.strftime("%Y-%m-%dT%H:%M:%SZ")
DOCUMENT = dict(U.build(rows=ROWS), generated_at=STAMP)
# 桩名录全写进这一个目录，进程退出时删掉：每次跑这套件都在 %TEMP% 留一个目录，跑几十次就没人认得出哪个还有用。
INDEX_DIR = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, INDEX_DIR, True)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


class Boom(Exception):
    """执行走到这里就说明链子已经碰了取数层——准入本该拦在它前面。"""


class StubStore:
    """只记录「有没有开批次」，然后炸。"""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.forked: list[tuple[str, str]] = []

    def create(self, code: str):
        self.created.append(code)
        raise Boom("已经开了批次目录")

    def fork(self, code: str, source_run: str):
        self.forked.append((code, source_run))
        raise Boom("复现也要开目录")


class StubFundamentals:
    """取数层只要被叫一次就算失败：准入拦的就是「白跑一遍」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, *args, **kwargs):
        self.calls.append("fetch")
        raise Boom("已经联网取数")

    def replay(self, *args, **kwargs):
        self.calls.append("replay")
        raise Boom("复现读的是盘上证据")


INDEX_FILES = [0]


def index_path(document: dict | None) -> Path:
    """一份名录写进临时目录里的一个独立文件——退出时整目录连带删掉，跑这套件不该在 %TEMP% 留东西。"""
    INDEX_FILES[0] += 1
    path = INDEX_DIR / f"universe-{INDEX_FILES[0]}.json"
    if document is not None:
        U.write(document, path)
    return path


class redirect:
    """临时换掉 universe.UNIVERSE_PATH，退出时还原——本套件从不读真名录，否决与放行才是确定的。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.saved = None

    def __enter__(self):
        self.saved = U.UNIVERSE_PATH
        U.UNIVERSE_PATH = self.path
        return self

    def __exit__(self, *exc):
        U.UNIVERSE_PATH = self.saved
        return False


def stubbed():
    """把批次层与取数层换成桩，返回 (store, fundamentals, 还原函数)。"""
    store, fundamentals = StubStore(), StubFundamentals()
    saved = builder.RunStore, builder.fundamentals
    builder.RunStore, builder.fundamentals = store, fundamentals  # type: ignore[attr-defined]
    return store, fundamentals, saved


def restore(saved) -> None:
    builder.RunStore, builder.fundamentals = saved  # type: ignore[attr-defined]


def test_admission_three_answers() -> None:
    with redirect(index_path(DOCUMENT)):
        allowed, absent, denied = builder.admission("600519")
        check("名录里有→放行", allowed and absent == "" and "贵州茅台" in denied, f"{absent} {denied}")
        allowed, absent, denied = builder.admission("600002", now=NOW)
        check("名录里没有→否决", not allowed and absent.startswith("3 只名录（") and absent.endswith("）里没有它"), absent)
        check("否决的长句先报代码", not allowed and "600002 不在当前 A 股名录里" in denied, denied[:40])
        check("否决的长句给出出路", "update_universe.py --force" in denied and "多半已退市" in denied, denied[:60])

    # 名录答不了的两种情形都放行：判据是「没有证据就不下结论」，与批次缺行情凭证时不声称知道基准同源。
    with redirect(index_path(None)):
        allowed, absent, denied = builder.admission("600002")
        check("盘上没有索引时放行", allowed and absent == "" and "没有可用的股票索引" in denied, f"{allowed} {denied}")
    eight_days = dict(DOCUMENT, generated_at=(NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with redirect(index_path(eight_days)):
        allowed, absent, denied = builder.admission("600002", now=NOW)
        check("名录旧过七天时放行", allowed and absent == "" and "8 天未刷新" in denied, f"{allowed} {denied}")
    exactly_seven = dict(DOCUMENT, generated_at=(NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with redirect(index_path(exactly_seven)):
        allowed, absent, _ = builder.admission("600002", now=NOW)
        check("第七天当天名录仍算得出答复", not allowed and absent != "", f"{allowed} {absent}")


def test_build_never_touches_the_network_when_blocked() -> None:
    store, fundamentals, saved = stubbed()
    try:
        with redirect(index_path(DOCUMENT)):
            try:
                builder.build(builder.BuildOptions(code="600002"))
                check("名录否决时整条链停下", False, "没有抛 TickerUnavailable")
            except builder.TickerUnavailable as exc:
                check("名录否决时整条链停下", "600002 不在当前 A 股名录里" in str(exc), str(exc)[:40])
            except Boom as exc:
                check("名录否决时整条链停下", False, f"链子走过头：{exc}")
            check("被拦时不创建批次目录", store.created == [], str(store.created))
            check("被拦时一次取数都没发起", fundamentals.calls == [], str(fundamentals.calls))

            # 同一只票换成名录里的代码：链子照走，桩在下一步炸——证明拦它的只有名录这一道。
            try:
                builder.build(builder.BuildOptions(code="600519"))
            except Boom as exc:
                check("名录里的代码照旧走链", store.created == ["600519"], f"{store.created} {exc}")

            # 复现不查名录：退市票的旧批次仍然放得开，读的是盘上证据。
            try:
                builder.build(builder.BuildOptions(code="600002", from_run="20260924-002605"))
            except Boom as exc:
                check("复现不查名录", store.forked == [("600002", "20260924-002605")], f"{store.forked} {exc}")
            check("复现只 fork 不 create", store.created == ["600519"] and len(store.forked) == 1, f"{store.created} {store.forked}")
    finally:
        restore(saved)


def test_cli_maps_each_failure_to_its_own_exit_code() -> None:
    """三类失败三个出口：查无此票 3（一次网都没上），行情没取到与 β 不成立 2，成功 0。"""
    spec = importlib.util.spec_from_file_location("build_baseline", ROOT / "scripts" / "build_baseline.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    store, fundamentals, saved = stubbed()
    try:
        with redirect(index_path(DOCUMENT)):
            check("查无此票以 3 退出", module.main(["600002"]) == 3, str(store.created))
            check("CLI 拒绝时也没开目录", store.created == [] and fundamentals.calls == [], str(fundamentals.calls))
            saved_build = module.build
            for raise_, expect, label in (
                (prices.PricesUnavailable("取不到周频行情"), 2, "行情没取到仍退 2"),
                (BetaNotApplicable("β≤0，CAPM 折不出发"), 2, "β 不成立仍退 2"),
            ):
                module.build = lambda options, progress=None, error=raise_: (_ for _ in ()).throw(error)
                code = module.main(["600519"])
                check(label, code == expect, str(code))
            module.build = saved_build
    finally:
        restore(saved)


def test_boards_that_are_not_wired_up_fail_at_construction() -> None:
    """板块路由：未接通的号段要在构造时就报「未接通」，不能拼出一个能发请求的 secucode 白跑一遍。

    踩过的坑：920xxx 与 900xxx 首位是 9，曾被当成沪市；200xxx 首位是 2，被当成深市。
    """
    for code, expect in (("920008", "北交所"), ("830799", "北交所"), ("430047", "北交所"),
                         ("900901", "B 股"), ("200011", "B 股")):
        try:
            Ticker(code)
            check(f"{code} 仍被当成沪深票", False, "构造成功了")
        except NotImplementedError as exc:
            check(f"{code} 说清未接通", expect in str(exc), str(exc)[:60])
    for code, suffix in (("600519", "SH"), ("000001", "SZ"), ("300750", "SZ"), ("688981", "SH")):
        ticker = Ticker(code)
        check(f"{code} 路由到 {suffix}", ticker.suffix == suffix and ticker.secucode.endswith(f".{suffix}"), ticker.secucode)
    for code in ("110000", "510300"):
        try:
            Ticker(code)
            check(f"{code} 走不通时说得出口", False, "构造成功了")
        except (ValueError, NotImplementedError) as exc:
            check(f"{code} 走不通时说得出口", "沪深" in str(exc) or "无法识别" in str(exc), str(exc)[:50])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
