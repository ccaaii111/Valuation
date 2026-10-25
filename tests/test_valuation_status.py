"""「取数估值」只有一个按钮：回不回线上由 backend/data/freshness.py 的分源阈值决定.

不联网 —— RUNS_DIR 指到临时目录，批次由这里手写成 baseline.json + manifest.json（`benchmark=` 再补一份
raw/ 里的基准凭证），job 由 Fake 顶掉真 builder。时钟冻在 2026-09-28（周一）16:00 北京时间，所以「未过期」
和「跨了一次收盘」两种答复都能测；换基准压过有效期那一条也只在冻住的时钟下才测得出来。

    python tests/test_valuation_status.py
"""
from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend.data.transport as transport  # noqa: E402
from backend import builder  # noqa: E402
from backend.data import prices  # noqa: E402
from backend.web import api, jobs, store  # noqa: E402
from fixtures import synthetic_ladder  # noqa: E402

SCRATCH = Path(tempfile.mkdtemp())
# 每次跑这套件都会新开一个临时目录，退出时删掉：不清理的话 %TEMP% 里会攒下几十份同名 runs/。
atexit.register(shutil.rmtree, SCRATCH, True)
TMP = SCRATCH / "runs"
transport.RUNS_DIR = store.RUNS_DIR = TMP

FAILURES = 0
BEIJING = timezone(timedelta(hours=8))
ALL_AT = {
    "main_indicators": "2026-09-20T09:00:00",
    "balance_sheet": "2026-09-20T09:00:00",
    "income_statement": "2026-09-20T09:00:00",
    "cash_flow": "2026-09-20T09:00:00",
    "consensus": "2026-09-20T09:00:00",
    "risk_free": "2026-09-20T09:00:00",
    "market_returns": "2026-09-20T09:00:00",
    "quote": "2026-09-28T16:00:00",
    "price_history": "2026-09-28T16:00:00",
}
NOW = "2026-09-28T16:00:00"  # 周一收盘后；2026-09-25 是周五
FRIDAY = "2026-09-25T16:00:00"
CREATED = "2026-09-28T00:00:00Z"  # 北京时间 08:00，距 NOW 整 8 小时


def at(text: str) -> datetime:
    """北京时间写测试直观，manifest 记的是 UTC。"""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=BEIJING).astimezone(timezone.utc)


api.utc_now = lambda: at(NOW)

from backend.web.app import create_app  # noqa: E402

CLIENT = create_app().test_client()


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES += 1


def reset() -> Fake:
    jobs._jobs.clear()
    jobs._inflight.clear()
    jobs._code_locks.clear()
    fake = Fake()
    jobs.run_build = fake
    return fake


class Fake:
    """一个「什么也不写」的建链：本套件只关心按钮起不起作业、把什么参数交给了 builder。"""

    def __init__(self):
        self.codes: list[str] = []
        self.benchmarks: list[str] = []
        self.from_runs: list[str] = []

    def __call__(self, options, progress=None):
        self.codes.append(options.code)
        self.benchmarks.append(options.benchmark)
        self.from_runs.append(options.from_run)
        return SimpleNamespace(run_id="20260928-160000")


def batch(
    code: str, run_id: str, *, stamps: dict | None = None, as_of: str = "2026-06-30",
    failed: dict | None = None, benchmark: str = "", prefetched: list[str] | None = None,
) -> Path:
    """一份能读能判的批次：baseline 给报告期，manifest 给每个来源的取数时刻。

    `benchmark` 另写两份 raw/ 凭证（行情与市场腿），因为面板读的就是这里记录的基准而不是本机默认值；
    不给时什么都不写，模拟那个基准凭证还不存在的年代。
    `prefetched` 换成 M4 之后的凭证形状——一份凭证装着多家基准的序列，另在 manifest 记这批用的是谁。
    """
    directory = TMP / code / run_id
    directory.mkdir(parents=True, exist_ok=True)
    if benchmark:
        raw = directory / "raw"
        raw.mkdir(exist_ok=True)
        if prefetched is None:
            recorded = json.dumps(
                {"benchmark": {"code": benchmark, "name": prices.BENCHMARK_NAMES.get(benchmark)}}, ensure_ascii=False
            )
        else:
            recorded = json.dumps(
                {
                    "used_benchmark": benchmark,
                    "benchmarks": {code_: {"symbol": code_} for code_ in prefetched},
                    "unavailable": {},
                },
                ensure_ascii=False,
            )
        for name in (prices.RAW_NAME, "market_returns"):
            (raw / f"{name}.json").write_text(recorded, encoding="utf-8")
    sources = dict(ALL_AT if stamps is None else stamps)
    if failed:
        (directory / "build_failed.json").write_text(json.dumps(failed, ensure_ascii=False), encoding="utf-8")
    else:
        ladder = dict(synthetic_ladder(), as_of=as_of)
        (directory / "baseline.json").write_text(json.dumps(ladder, ensure_ascii=False), encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "code": code,
                "created_at": CREATED,
                "benchmark": benchmark,
                "sources": [{"name": name, "fetched_at": value} for name, value in sources.items()],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


def fetch(code: str, **extra) -> dict:
    response = CLIENT.post("/api/fetch", json={"code": code, **extra})
    return {"status": response.status_code, **response.get_json()}


def status(code: str, **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return CLIENT.get(f"/api/valuation_status?code={code}&{query}").get_json()


def wait_for(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"作业未结束: {jobs.get(job_id)}")


# ------------------------------------------------------------------ 复现快照那一支


def test_fresh_batch_replays_without_touching_the_network() -> None:
    reset()
    batch("600028", "20260928-120000")
    out = fetch("600028")
    check("未过期时不排队", out.get("replayed") is True and "job_id" not in out, str(out))
    check("答复指回原批次", out["run_id"] == "20260928-120000", str(out["run_id"]))
    check("过期分组为空", out["stale_groups"] == [] and out["action"] == "replay", str(out))
    check("确实没有作业起来", jobs._jobs == {}, str(jobs._jobs))
    check("报告期与取数时刻一起带回", out["as_of"] == "2026-06-30" and out["age_hours"] == 8.0, str(out["age_hours"]))
    check("答复说得出为什么不联网", "复现快照" in out["reason"], out["reason"])
    panel = CLIENT.get("/api/panel?code=600028").get_json()
    check("复现的正是面板读到的那一批", panel["run_id"] == "20260928-120000", str(panel["run_id"]))


def test_route_reports_the_same_verdict_the_button_reads() -> None:
    batch("600887", "20260928-121000")
    out = status("600887")
    check("GET 与 POST 同一套判断", out["action"] == "replay", str(out)[:120])
    check("答复字段齐全", {"run_id", "as_of", "fetched_at", "stale", "reason"} <= set(out), str(sorted(out)))
    check("状态条说人话", "仍在有效期内" in out["reason"], out["reason"])
    missing = status("300001")
    check("没取过数的代码直接说要联网", missing["action"] == "fetch" and "还没有取数批次" in missing["reason"], str(missing))
    check("没取过数也没有批次号", missing["run_id"] == "" and missing["stale"] == [], str(missing))


def test_every_branch_answers_the_benchmark_too() -> None:
    """早退的四条分支也要带 `benchmark`：前端读的是 `out.benchmark.switch`，一个键缺了就整行新鲜度抛异常。

    2026-10-15 真实浏览器里撞到的：000651 从没取过数，走「还没有取数批次」这条早退，答复里没有 benchmark，
    common.js 在 `out.benchmark.switch` 上抛 TypeError，那一行留白——而旁边的提示正指着它说「联不联网看上面那行」。
    没有批次时确实「无可切」（snapshot 空、switch 假），但「这一次要联哪个基准」从请求进来那一刻就是定数。
    """
    out = status("300001")
    check("没批次也报得出要联的基准", out["benchmark"]["requested"]["code"] == prices.DEFAULT_BENCHMARK
          and out["benchmark"]["switch"] is False, str(out["benchmark"]))
    check("没批次时没有可比的历史快照", out["benchmark"]["snapshot"] == {} and out["benchmark"]["prefetched"] == [],
          str(out["benchmark"]))
    typed = status("300001", benchmark="000906")
    check("早退分支认得用户点的基准", typed["benchmark"]["requested"]["code"] == "000906", str(typed["benchmark"]))
    bad = CLIENT.get("/api/valuation_status?code=300001&benchmark=abc")
    check("认不出的基准代码照样回 400", bad.status_code == 400 and "基准指数" in bad.get_json()["error"],
          str(bad.get_json()))


def test_the_index_can_refuse_without_changing_the_action() -> None:
    """名录判「不在市」时只改写说法，不改动作：动作仍是 fetch，按钮仍可点，但点下去不排队（2026-10-15 口径）。

    准入与新鲜度是两件事，各判各的：force 跳过的是阈值，跳过不了名录；而复现那条路根本不查名录——
    盘上的历史证据今天否决不了（`build()` 里的同一规则，那边另有套件钉）。
    """
    denied = (False, "5574 只名录里没有它", "查无此票：先跑 python scripts/update_universe.py --force 重建名录")
    real = builder.admission
    builder.admission = lambda code: denied
    fake = reset()
    try:
        out = status("600001")
        check("动作照旧是 fetch", out["action"] == "fetch", str(out)[:80])
        check("否决短句单独带回", out["blocked"] == "5574 只名录里没有它", str(out.get("blocked")))
        check("长句摆进 reason 供 title 显示", out["reason"] == denied[2], out["reason"][:40])

        out = fetch("600001")
        check("按下不排队，直接 400", out["status"] == 400 and "update_universe" in out["error"], str(out)[:120])
        check("被拦之后没有作业", jobs._jobs == {}, str(jobs._jobs))
        out = fetch("600001", force="true")
        check("强制重取也不给后门", out["status"] == 400 and jobs._jobs == {}, str(out)[:80])

        batch("600002", "20260928-170000")
        out = status("600002")
        check("未过期批次照旧复现，名录不插手", out["action"] == "replay" and out["blocked"] == "", str(out)[:100])
        out = fetch("600002")
        check("复现答复照旧不联网", out.get("replayed") is True and "job_id" not in out, str(out)[:100])
        check("这里一次建链都没起", fake.codes == [], str(fake.codes))
    finally:
        builder.admission = real


# ------------------------------------------------------------------ 过期那一支


def test_a_new_close_turns_the_button_back_into_a_build() -> None:
    fake = reset()
    batch("600519", "20260925-160000", stamps=dict(ALL_AT, quote=FRIDAY, price_history=FRIDAY))
    out = fetch("600519")
    check("报价过期就起作业", "job_id" in out, str(out))
    check("作业起来时带上过期分组", out["stale_groups"] == ["报价", "行情(β)"], str(out["stale_groups"]))
    check("理由写清哪天到哪天", "2026-09-25" in out["reason"] and "2026-09-28" in out["reason"], out["reason"])
    wait_for(out["job_id"])
    check("真把代码交给了 builder", fake.codes == ["600519"], str(fake.codes))


def test_force_skips_the_verdict() -> None:
    fake = reset()
    batch("000858", "20260928-122000")
    check("同一批次默认仍是复现", fetch("000858").get("replayed") is True)
    out = fetch("000858", force=True)
    check("force=True 时照样联网", "job_id" in out and not out.get("replayed"), str(out))
    wait_for(out["job_id"])
    check("强制重取时理由仍是复现的话", "复现快照" in out["reason"], out["reason"])
    check("只有被要求时才起作业", fake.codes == ["000858"], str(fake.codes))


def test_disclosure_deadline_is_what_ages_the_statements() -> None:
    """6/30 报告期要等三季报，法定截止 10/31；11 月起才逼着重取。"""
    batch("600036", "20260928-123000")
    before = api.valuation_status("600036", "20260928-123000", now=at("2026-10-30T10:00:00"))
    check("10/30 还没有三表的事", "三表" not in before["stale_groups"], str(before["stale_groups"]))
    after = api.valuation_status("600036", "20260928-123000", now=at("2026-11-02T10:00:00"))
    check("过了 10/31 三表必须重取", "三表" in after["stale_groups"], str(after["stale_groups"]))
    check("三表过期说得出是哪个报告期", "2026-09-30" in after["reason"], after["reason"])


# ------------------------------------------------------------------ 缺凭证 / 上次失败 / 人工 β


def test_a_failed_batch_is_not_something_to_replay() -> None:
    fake = reset()
    batch("600030", "20260928-130000", failed={"reason": "beta_not_applicable"})
    out = fetch("600030")
    check("上次没写出基线 → 这次联网", "job_id" in out, str(out))
    check("理由引用得上次失败", "没写出基线" in out["reason"] and "beta_not_applicable" in out["reason"], out["reason"])
    wait_for(out["job_id"])
    check("起了唯一一个作业", fake.codes == ["600030"], str(fake.codes))


def test_missing_evidence_is_named_not_guessed() -> None:
    fake = reset()
    batch("601318", "20260928-140000", stamps={k: v for k, v in ALL_AT.items() if k != "risk_free"})
    out = fetch("601318")
    check("缺 rf 凭证就判不了新旧", out["stale_groups"] == ["缺取数凭证"], str(out["stale_groups"]))
    check("点名缺的是哪个来源", "risk_free" in out["reason"], out["reason"])
    wait_for(out["job_id"])
    check("判不了就不复现", fake.codes == ["601318"], str(fake.codes))


def test_manual_beta_drops_the_weekly_requirement() -> None:
    reset()
    batch("601899", "20260928-150000", stamps={k: v for k, v in ALL_AT.items() if k != "price_history"})
    check("不带 β 时缺行情凭证要重取", "job_id" in fetch("601899"), "复现了不该复现的批次")
    out = status("601899", beta="0.8")
    check("人工 β 只测用得到的来源", out["action"] == "replay", str(out["stale_groups"]))
    posted = fetch("601899", beta="0.8")
    check("POST 也跟着改口", posted.get("replayed") is True, str(posted))


def test_a_pinned_batch_is_judged_on_its_own_evidence() -> None:
    fake = reset()
    batch("000001", "20260928-153000")
    batch("000001", "20260901-090000", stamps=dict(ALL_AT, quote="2026-09-01T09:00:00", price_history="2026-09-01T09:00:00"))
    check("默认判最新一批", status("000001")["run_id"] == "20260928-153000" and status("000001")["action"] == "replay")
    check("指定旧批次按它自己的凭证判", status("000001", run="20260901-090000")["action"] == "fetch")
    pinned = fetch("000001", run="20260901-090000")
    check("复现某一批次时可以只改 run", "job_id" in pinned, str(pinned))
    wait_for(pinned["job_id"])
    check("过期分组落在报价与行情", pinned["stale_groups"] == ["报价", "行情(β)"], str(pinned["stale_groups"]))
    check("builder 收到的是面板选中的代码", fake.codes == ["000001"], str(fake.codes))


# ------------------------------------------------------------------ 市场基准：换了指数的快照回答的是另一个问题


def test_the_dropdown_offers_the_named_benchmarks_with_their_legs() -> None:
    options = CLIENT.get("/api/options").get_json()
    by_code = {item["code"]: item for item in options["benchmarks"]}
    check("中证的四家宽基都在下拉里", {"000905", "000906", "932000", "000300"} <= set(by_code), str(sorted(by_code)))
    check("深市与上证的三家也在", {"000001", "399001", "399006"} <= set(by_code), str(sorted(by_code)))
    check("默认项就是中证2000", options["benchmark_default"]["code"] == prices.DEFAULT_BENCHMARK,
          str(options["benchmark_default"]))
    check("有全收益孪生的标了出来", all(by_code[c]["total_return"] for c in prices.TR_SYMBOLS), str(by_code["000906"]))
    check("没孪生的标为降级腿", not any(by_code[c]["total_return"] for c in ("000001", "399001", "399006")),
          str(by_code["399006"]))
    check("每一项都带中文名", all(item["name"] for item in by_code.values()), str(options["benchmarks"][:2]))


def test_a_fresh_batch_still_replays_when_the_benchmark_matches() -> None:
    reset()
    default = prices.DEFAULT_BENCHMARK  # 这一段量的是「留空＝默认基准」，所以批次本身就摆在默认那家
    batch("600585", "20260928-161000", benchmark=default)
    check("同基准时仍在有效期内复现", status("600585", benchmark=default)["action"] == "replay")
    blank = status("600585")
    check("留空＝默认基准，与快照同一个", blank["action"] == "replay" and blank["benchmark"]["requested"]["code"] == default,
          str(blank["benchmark"]))
    check("同基准时不提基准指数", "基准指数" not in blank["stale_groups"], str(blank["stale_groups"]))
    panel = CLIENT.get("/api/panel?code=600585").get_json()
    check("面板显示的是这批实际用的基准",
          panel["benchmark"] == {"code": default, "name": prices.BENCHMARK_NAMES[default], "total_return": True},
          str(panel["benchmark"]))


def test_switching_the_benchmark_forces_a_refetch_inside_the_window() -> None:
    fake = reset()
    batch("600585", "20260928-162000", benchmark="000905")
    out = status("600585", benchmark="000906")
    check("换基准压过有效期", out["action"] == "fetch", str(out["reason"])[:80])
    check("过期分组只点名基准指数", out["stale_groups"] == ["基准指数"], str(out["stale_groups"]))
    check("理由把两个基准都念出来", "中证500" in out["reason"] and "中证800" in out["reason"], out["reason"])
    check("理由说清为什么不复用旧快照", "回归斜率" in out["reason"] and "超额收益" in out["reason"], out["reason"])
    check("答复带回请求与快照两个基准",
          (out["benchmark"]["requested"]["code"], out["benchmark"]["snapshot"]["code"]) == ("000906", "000905"),
          str(out["benchmark"]))
    check("降级腿的提醒只在没有孪生时才出现", "没有全收益孪生" not in out["reason"], out["reason"])
    posted = fetch("600585", benchmark="000906")
    check("POST 起的是作业而不是复现", "job_id" in posted and not posted.get("replayed"), str(posted))
    wait_for(posted["job_id"])
    check("builder 收到换过的基准", fake.benchmarks == ["000906"], str(fake.benchmarks))


def test_switching_inside_the_prefetched_set_is_an_offline_replay() -> None:
    """M4 的正题：一次预取把下拉那几家的序列全存进同一份凭证，换基准于是只是从盘上切另一条。"""
    fake = reset()
    batch("600586", "20260928-165500", benchmark="000905", prefetched=["000905", "000906", "000300"])
    out = status("600586", benchmark="000906")
    check("切到已预取的基准不联网", out["action"] == "replay", str(out["reason"])[:80])
    check("基准指数不算过期分组", "基准指数" not in out["stale_groups"], str(out["stale_groups"]))
    check("答复认下这是换基准", out["benchmark"]["switch"] is True, str(out["benchmark"]))
    check("预取集合照实下发", out["benchmark"]["prefetched"] == ["000300", "000905", "000906"],
          str(out["benchmark"]["prefetched"]))
    check("这批用的基准从 manifest 读", out["benchmark"]["snapshot"]["code"] == "000905", str(out["benchmark"]))
    check("理由说清另开一个批次", "另开一个批次" in out["reason"] and "000906" in out["reason"], out["reason"])

    posted = fetch("600586", benchmark="000906")
    check("POST 起的是复现作业而不是回读旧批",
          "job_id" in posted and posted["action"] == "replay" and not posted.get("replayed"), str(posted))
    check("作业记下来自哪一批", posted.get("replayed_from") == "20260928-165500", str(posted))
    wait_for(posted["job_id"])
    check("builder 带 from_run 复现", fake.from_runs == ["20260928-165500"], str(fake.from_runs))
    check("复现也带着换过的基准", fake.benchmarks == ["000906"], str(fake.benchmarks))

    force = fetch("600586", benchmark="000906", force=True)
    wait_for(force["job_id"])
    check("force 才真要重新联网取数", force["action"] == "fetch" and fake.from_runs[-1] == "", str(force))

    outside = status("600586", benchmark="399006")
    check("预取集合外的基准仍判重取", outside["action"] == "fetch" and "基准指数" in outside["stale_groups"],
          str(outside["stale_groups"]))
    check("没换基准就不起复现", status("600586", benchmark="000905")["benchmark"]["switch"] is False)

    # 顶栏的取数台两页共用，所以 /api/derivation 也得带这批的基准——缺了它，导图的下拉只能显示本机默认值，
    # 老批次就会被人看成新口径。
    graph = CLIENT.get("/api/derivation?code=600586").get_json()
    check("推导图也带这批实际用的基准", graph["benchmark"]["code"] == "000905", str(graph["benchmark"]))
    check("两页读到的基准同源",
          graph["benchmark"] == CLIENT.get("/api/panel?code=600586").get_json()["benchmark"],
          str(graph["benchmark"]))


def test_a_prefixed_or_unlisted_benchmark_is_still_a_benchmark() -> None:
    batch("600276", "20260928-163000", benchmark="000905")
    check("带前缀的 sh000905 不触发重取", status("600276", benchmark="sh000905")["action"] == "replay")
    deep = status("600276", benchmark="sz399006")
    check("深市代码归一后再比对", deep["benchmark"]["requested"]["code"] == "399006", str(deep["benchmark"]))
    check("没有孪生时理由多一句", "没有全收益孪生" in deep["reason"] and "价格指数 + 股息率" in deep["reason"],
          deep["reason"])
    outside = status("600276", benchmark="000903")
    check("白名单外的代码照样收", outside["benchmark"]["requested"]["code"] == "000903", str(outside["benchmark"]))
    check("没有中文名时就用代码自称", outside["benchmark"]["requested"]["name"] == "000903",
          str(outside["benchmark"]["requested"]))
    check("认不出的基准仍判重取", outside["action"] == "fetch", outside["reason"])
    response = CLIENT.post("/api/fetch", json={"code": "600276", "benchmark": "ABC"})
    check("不是指数代码就 400", response.status_code == 400, str(response.get_json()))
    check("400 说清要什么", "6 位数字" in response.get_json()["error"], str(response.get_json()))


def test_a_batch_without_benchmark_evidence_never_claims_a_switch() -> None:
    """真实批次有 manifest 就有 raw；这里专门造一个「有取数时刻、无原始答复」的畸形批次。"""
    reset()
    batch("600000", "20260928-164000")
    out = status("600000", benchmark="000906")
    check("没有证据就不声称知道它用过谁", out["benchmark"]["snapshot"]["code"] == "", str(out["benchmark"]))
    check("换基准的判断就此退场", "基准指数" not in out["stale_groups"] and out["action"] == "replay", str(out["stale_groups"]))
    check("面板也就显示不出基准", CLIENT.get("/api/panel?code=600000").get_json()["benchmark"]["code"] == "")
    batch("600000", "20260928-164500")
    raw = TMP / "600000" / "20260928-164500" / "raw"
    raw.mkdir()
    (raw / "market_returns.json").write_text(json.dumps({"benchmark": {"code": "000906"}}), encoding="utf-8")
    matched = status("600000", run="20260928-164500", benchmark="000906")
    check("两份凭证任一份记着基准就算数", matched["benchmark"]["snapshot"]["code"] == "000906", str(matched["benchmark"]))
    check("只有一份时另一份缺席也不误报换基准",
          matched["action"] == "replay" and "基准指数" not in matched["stale_groups"], str(matched["stale_groups"]))


def test_an_old_baseline_without_the_warnings_key_reads_as_none() -> None:
    directory = batch("601988", "20260928-165000")
    path = directory / "baseline.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("warnings")
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    panel = CLIENT.get("/api/panel?code=601988").get_json()
    # 65.4504 = 老整年轴 64.1941 换到残期轴：这批的截止日是 2026-06-30 而首年正是 2026，
    # 首年只剩 0.5 年——金额减半、折现期 0.5→0.25，2–10 年与终值各提前半年。
    check("老批次读得出估值", abs(panel["attribution"]["per_share"] - 65.4504) < 0.01, str(panel["attribution"]["per_share"]))
    check("顶层没有警告字段时读成空", panel["warnings"] == [], str(panel["warnings"]))
    fresh = batch("601988", "20260928-165500")
    stored = json.loads((fresh / "baseline.json").read_text(encoding="utf-8"))
    check("新写出的基线自带警告字段", "warnings" in stored, str(sorted(stored)))
    check("无市场快照的基线就带着那条降级警告", len(stored["warnings"]) == 1, str(stored["warnings"])[:90])
    passthrough = CLIENT.get("/api/panel?code=601988&run=20260928-165500").get_json()
    check("面板把警告原样透传", passthrough["warnings"] == stored["warnings"], str(passthrough["warnings"])[:90])
    graph = CLIENT.get("/api/derivation?code=601988").get_json()
    check("推导图也读同一个顶层字段", graph["warnings"] == stored["warnings"], str(graph["warnings"])[:90])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
