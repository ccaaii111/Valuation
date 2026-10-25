"""Tests for 面板取数: a typed code starts a background build that writes a baseline the panel can read.

No network — jobs.run_build stands in for backend.builder.build, and both RUNS_DIR references point at a
temp dir (backend.web.store imported its own copy, so patching one alone would have the panel read real
runs/).

    python tests/test_panel_fetch.py
"""
from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend.data.transport as transport  # noqa: E402
from backend.builder import BuildOptions, describe  # noqa: E402
from backend.data import prices  # noqa: E402
from backend.model.assumptions import BetaNotApplicable  # noqa: E402
from backend.web import jobs, store  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402

SCRATCH = Path(tempfile.mkdtemp())
# 每次跑这套件都会新开一个临时目录，退出时删掉：不清理的话 %TEMP% 里会攒下几十份同名 runs/。
atexit.register(shutil.rmtree, SCRATCH, True)
TMP = SCRATCH / "runs"
transport.RUNS_DIR = store.RUNS_DIR = TMP

from backend.web.app import create_app  # noqa: E402

CLIENT = create_app().test_client()
FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def reset() -> None:
    jobs._jobs.clear()
    jobs._inflight.clear()
    jobs._code_locks.clear()


class Fake:
    """A build that writes a synthetic baseline, reports the usual steps, and optionally blocks or explodes."""

    def __init__(self, run_id: str = "20260923-120000", boom: str = ""):
        self.run_id = run_id
        self.boom = boom
        self.codes: list[str] = []
        self.betas: list = []
        self.benchmarks: list[str] = []
        self.steps: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, options, progress=None):
        self.codes.append(options.code)
        self.betas.append(options.beta)
        self.benchmarks.append(options.benchmark)
        # 真 builder 交回的是英文 key，翻名发生在 builder 里——这里照做，才测得到面板上看到的东西。
        for name in ("balance_sheet", "consensus"):
            self.steps.append(name)
            if progress:
                progress(describe(name))
        if self.boom == "block":
            self.entered.set()
            self.release.wait(5)
        elif self.boom:
            # 既接受字符串（模拟取数报错），也接受异常本身（模拟建链拒绝）。
            raise self.boom if isinstance(self.boom, Exception) else RuntimeError(self.boom)
        directory = TMP / options.code / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "baseline.json").write_text(json.dumps(synthetic_ladder(), ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(run_id=self.run_id)


def wait_for(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"作业未结束: {jobs.get(job_id)}")


def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def fetch(code, **extra) -> dict:
    response = CLIENT.post("/api/fetch", json={"code": code, **extra})
    return {"status": response.status_code, **response.get_json()}


# ------------------------------------------------------------------ 代码归一化与拒绝


def test_code_is_normalised_or_rejected() -> None:
    for typed, expected in (("600519.SH", "600519"), ("sh600519", "600519"), (" 000001 ", "000001")):
        reset()
        jobs.run_build = Fake()
        out = fetch(typed)
        check(f"{typed!r} 归一化为 {expected}", out["status"] == 200 and out["code"] == expected, str(out))
        wait_for(out["job_id"])
    reset()
    jobs.run_build = Fake()
    for bad in ("", "abc", "60051", "110000"):
        out = fetch(bad)
        rejected = out["status"] == 400 and ("代码" in out["error"] or "北交所" in out["error"])
        check(f"拒绝 {bad!r}", rejected, str(out))
    check("拒绝之后不会留下作业", jobs._jobs == {}, str(jobs._jobs))


# ------------------------------------------------------------------ 成功：作业落到 runs/，面板读得到


def test_successful_job_writes_a_baseline_the_panel_reads() -> None:
    reset()
    jobs.run_build = Fake(run_id="20260923-130000")
    out = fetch("600028")
    job = wait_for(out["job_id"])
    check("作业完成并回报批次号", job["state"] == "done" and job["run_id"] == "20260923-130000", str(job))
    check("基线文件写在代码目录下", (TMP / "600028" / "20260923-130000" / "baseline.json").is_file())
    check("耗时计到毫秒", isinstance(job["elapsed_ms"], float) and job["elapsed_ms"] >= 0, str(job["elapsed_ms"]))
    panel = CLIENT.get("/api/panel?code=600028").get_json()
    check("新批次不指定 run 也能读出估值", panel.get("run_id") == "20260923-130000", str(panel)[:120])
    check("读出的就是玩具公司的每股 64.19", abs(panel["attribution"]["per_share"] - 64.1941) < 0.01,
          str(panel["attribution"]["per_share"]))
    entry = next((c for c in CLIENT.get("/api/options").get_json()["codes"] if c["code"] == "600028"), None)
    check("代码出现在选项里", entry is not None and entry["runs"][0]["run_id"] == "20260923-130000", str(entry))


def test_progress_reports_the_step_still_running() -> None:
    reset()
    fake = Fake(boom="block")
    jobs.run_build = fake
    out = fetch("600519")
    fake.entered.wait(5)
    check("阻塞期间最后一份来源名留在作业上", jobs.get(out["job_id"])["step"] == "一致预期",
          str(jobs.get(out["job_id"])))
    fake.release.set()
    check("跑完后步骤改口", wait_for(out["job_id"])["step"] == "基线已写出")


# ------------------------------------------------------------------ 失败：说清卡在哪一步


def test_failed_build_reports_step_and_error() -> None:
    reset()
    jobs.run_build = Fake(boom="东财 502")
    out = fetch("600519")
    job = wait_for(out["job_id"])
    check("作业转 failed 而不是吊死", job["state"] == "failed", str(job))
    check("错误带异常类型与原文", "RuntimeError" in job["error"] and "东财 502" in job["error"], job["error"])
    check("卡住的步骤一并报出", job["step"] == "一致预期", job["step"])
    check("失败作业不占住这个代码", jobs._inflight == {}, str(jobs._inflight))
    check("同一代码可以立刻重试", fetch("600519")["job_id"] != out["job_id"])


def test_beta_escape_hatch_travels_with_the_request() -> None:
    reset()
    fake = Fake()
    jobs.run_build = fake
    wait_for(fetch("600519", beta="0.9")["job_id"])
    check("贝塔随取数下发", fake.betas == [0.9], str(fake.betas))
    wait_for(fetch("600028", beta="")["job_id"])
    check("贝塔留空即走回归", fake.betas == [0.9, None], str(fake.betas))
    before = len(jobs._jobs)
    for bad, reason in (("-1", "正数"), ("abc", "数字")):
        out = fetch("000001", beta=bad)
        check(f"拒绝贝塔 {bad!r}", out["status"] == 400 and reason in out["error"], str(out))
    check("被拒的请求不会留下作业", len(jobs._jobs) == before, str(list(jobs._jobs)))


def test_negative_beta_fails_the_build_with_a_readable_reason() -> None:
    reset()
    jobs.run_build = Fake(boom=BetaNotApplicable("β 为负（实测 -0.31，156 周对中证500）→ CAPM 不适用该模型：加 --beta"))
    out = fetch("600030")
    job = wait_for(out["job_id"])
    check("建链拒绝转成 failed", job["state"] == "failed", str(job))
    check("异常类型与原文照抄给面板", "BetaNotApplicable" in job["error"] and "CAPM 不适用" in job["error"], job["error"])
    check("拒绝之后没有基线可读", not (TMP / "600030" / "20260923-120000" / "baseline.json").exists())


def test_the_benchmark_travels_with_the_request() -> None:
    """市场组合是建链之前就定下的事，所以它和 --beta 一样随请求下发，而不是事后改行。"""
    reset()
    fake = Fake()
    jobs.run_build = fake
    out = fetch("600519", benchmark="000906")
    check("答复回显归一后的基准", out["benchmark"] == "000906", str(out))
    wait_for(out["job_id"])
    check("基准随取数下发给 builder", fake.benchmarks == ["000906"], str(fake.benchmarks))
    wait_for(fetch("600028")["job_id"])
    check("基准留空即默认那家", fake.benchmarks == ["000906", prices.DEFAULT_BENCHMARK], str(fake.benchmarks))
    wait_for(fetch("000001", benchmark="sh600028")["job_id"])
    check("带前缀的指数代码归一后下发", fake.benchmarks[-1] == "600028", str(fake.benchmarks))
    before = len(jobs._jobs)
    out = fetch("000001", benchmark="600519.SH")
    check("交易所后缀不是指数代码的写法，400 挡下", out["status"] == 400 and "基准" in out["error"], str(out))
    check("被拒的基准不会留下作业", len(jobs._jobs) == before, str(list(jobs._jobs)))


def test_unknown_job_is_a_message_not_a_crash() -> None:
    response = CLIENT.get("/api/job?job=deadbeef")
    check("未知作业号 400", response.status_code == 400, str(response.status_code))
    check("说明服务可能重启过", "重启" in response.get_json()["error"], str(response.get_json()))


# ------------------------------------------------------------------ 并发


def test_the_same_request_joins_the_running_job() -> None:
    reset()
    fake = Fake(boom="block")
    jobs.run_build = fake
    first = fetch("600519")
    fake.entered.wait(5)
    second = fetch("600519.SH")
    check("同码同参数的第二次复用作业", second["job_id"] == first["job_id"], f"{first['job_id']} vs {second['job_id']}")
    fake.release.set()
    wait_for(first["job_id"])
    fake = Fake(boom="", run_id="20260923-140000")
    jobs.run_build = fake
    check("跑完之后同码再起新作业", fetch("600519")["job_id"] != first["job_id"])


def test_a_second_request_with_other_options_is_refused() -> None:
    """并作业的判据是「这次要的是不是同一次建链」，不是「是不是同一只票」。"""
    reset()
    fake = Fake(boom="block")
    jobs.run_build = fake
    first = fetch("600519", benchmark="000906")
    fake.entered.wait(5)
    same = fetch("600519.SH", benchmark="sh000906")
    check("归一化之后参数相同仍复用", same.get("job_id") == first["job_id"], str(same))
    out = fetch("600519", benchmark="000300")
    check("换基准的第二次被拒而不是并进去",
          out["status"] == 400 and "000906" in out["error"] and "000300" in out["error"], str(out)[:140])
    check("被拒的请求不留下第二个作业", len(jobs._jobs) == 1, str(list(jobs._jobs)))
    check("原来那个作业不受影响还在跑", jobs.get(first["job_id"])["state"] == "running", str(jobs.get(first["job_id"])))
    fake.release.set()
    wait_for(first["job_id"])
    third = fetch("600519", benchmark="000300")
    check("前一个跑完之后新基准的请求起新作业", third["job_id"] != first["job_id"], str(third))
    wait_for(third["job_id"])
    check("两个基准各自下发到建链", fake.benchmarks == ["000906", "000300"], str(fake.benchmarks))


def test_a_replay_job_does_not_answer_a_fetch_request() -> None:
    """原来最坏的那条谎：正在复现老批次时按下强制重取，答复里那一批一次网都没联。"""
    reset()
    fake = Fake(boom="block")
    jobs.run_build = fake
    fork = jobs.start("600519", BuildOptions(code="600519", from_run="20260923-120000"))
    fake.entered.wait(5)
    try:
        jobs.start("600519", BuildOptions(code="600519"))
        check("复现作业不回答取数请求", False, "第二次没抛错")
    except ValueError as exc:
        check("复现作业不回答取数请求", "复现 20260923-120000" in str(exc) and "取数" in str(exc), str(exc)[:90])
    check("抛错之后仍然只有一个作业", list(jobs._jobs) == [fork], str(list(jobs._jobs)))
    check("同一次复现的第二次仍复用", jobs.start("600519", BuildOptions(code="600519", from_run="20260923-120000")) == fork)
    fake.release.set()
    wait_for(fork)


def test_different_codes_run_side_by_side() -> None:
    reset()
    fake = Fake(boom="block")
    jobs.run_build = fake
    a, b = fetch("600519"), fetch("000001")
    fake.entered.wait(5)
    check("两个代码两个作业", a["job_id"] != b["job_id"], f"{a} {b}")
    check("两条链路同时在跑", fake.codes == ["600519", "000001"] and all(
        jobs.get(job)["state"] == "running" for job in (a["job_id"], b["job_id"])), str(fake.codes))
    fake.release.set()
    for job in (a["job_id"], b["job_id"]):
        wait_for(job)
    check("各自的基线写进各自目录", all(
        (TMP / c / "20260923-120000" / "baseline.json").is_file() for c in ("600519", "000001")))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
