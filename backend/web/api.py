"""Compose the model layer into one payload the browser can render, and start builds on request.

Panel reads never fetch: they replay a stored baseline, so a recompute is offline and reproducible.
Writing a *new* baseline is the exception — POST /api/fetch first asks 新鲜度 whether the stored batch is
still current, and only when a source has moved on does it hand the work to backend.web.jobs, which runs
the same build chain the CLI does and writes into runs/. Kept free of Flask imports so the tests drive it
directly.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from backend import builder
from backend.data import freshness, market, prices, universe
from backend.data.em_company import Ticker
from backend.model.assumptions import (
    erp_window_choice,
    erp_window_options,
    erp_warnings_for_window,
)
from backend.model.attribution import attribute, flat_rows
from backend.model.dcf import apply_overrides, cross_check, inputs_from_ladder, matrix
from backend.model.derivation import graph as derivation_graph
from backend.web import jobs, store

METHODS = ("fcff", "fcfe")
TERMINALS = ("gordon", "exit_multiple", "uem")
RATE_LIMIT = 0.5


def utc_now() -> datetime:
    """新鲜度判断的时钟。测试冻住它才能同时测「未过期」与「跨了一次收盘」，面板不管这一层。"""
    return datetime.now(timezone.utc)


def options() -> dict:
    return {
        "codes": store.list_codes(),
        "methods": list(METHODS),
        "terminals": list(TERMINALS),
        "benchmarks": [_benchmark_option(code) for code in prices.BENCHMARK_NAMES],
        "benchmark_default": _benchmark_option(prices.DEFAULT_BENCHMARK),
        "attribution_note": "归因列是「只把这一行退回机器基线」的单行敏感性，多行改动时不可相加。",
        "edit_note": "金额行（亿元/亿股）的框是相比机器基线的 ±%，比率与参数行直接填数值；基线为 0 或负的行退回绝对值。",
    }


def universe_payload() -> dict:
    """The whole search index in one response — 5000+ 行、每次按键都回服务端不现实，浏览器整份拿住自己过滤。

    It answers 「这只票叫什么」, never 「这只票值多少钱」：估值取数一律走 runs/ 里有原始响应凭证的那条路。
    """
    document = universe.read()
    if not document:
        raise FileNotFoundError(
            "还没有可用的股票索引：跑 python scripts/update_universe.py 拉一次，或重启面板让启动脚本自动拉"
        )
    age = universe.age_days(document)
    return {
        "generated_at": document.get("generated_at"),
        "age_days": round(age, 1),
        "stale": age > universe.DEFAULT_MAX_AGE_DAYS,
        "source": document.get("source"),
        "note": document.get("note"),
        "total": document.get("total"),
        "max_age_days": universe.DEFAULT_MAX_AGE_DAYS,
        "entries": document["entries"],
    }


def _validated(method: str, terminal: str, rate: Any) -> tuple[str, str, float | None]:
    if method not in METHODS:
        raise ValueError(f"未知口径 {method!r}，可用 {list(METHODS)}")
    if terminal not in TERMINALS:
        raise ValueError(f"未知终值方法 {terminal!r}，可用 {list(TERMINALS)}")
    if rate is not None and rate != "":
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            raise ValueError(f"折现率必须是数字或留空，收到 {rate!r}") from None
        if not 0.0 < rate < RATE_LIMIT:
            raise ValueError(f"折现率需落在 0~{RATE_LIMIT:.0%} 之间，收到 {rate:.2%}")
    else:
        rate = None
    return method, terminal, rate


def _benchmark(value: Any) -> str:
    """基准指数代码：只认 6 位数字（可带 sh/sz 前缀），留空＝默认。

    刻意不收白名单：`--benchmark` 指数的选择是「市场组合是谁」这个研究判断，面板无权替用户圈定。
    白名单外的代码照样建链，只是它在中证官网多半没有全收益孪生，于是市场腿走「价格指数 + 股息率」，
    取不到股息率时退回惯例 6% 并产出警告——那条降级路本来就为这种情况存在。
    """
    text = str(value or "").strip()
    if not text:
        return prices.DEFAULT_BENCHMARK
    try:
        return re.sub(r"^(sh|sz)", "", prices.index_symbol(text))
    except ValueError:
        raise ValueError(f"无法识别的基准指数代码 {value!r}：要 6 位数字，可带 sh/sz 前缀") from None


def _benchmark_name(code: str) -> str:
    """白名单外的代码就用代码自称，面板照样显示得出这一项。"""
    return prices.BENCHMARK_NAMES.get(code) or code


def _benchmark_option(code: str) -> dict:
    return {
        "code": code,
        "name": _benchmark_name(code),
        "total_return": code in prices.TR_SYMBOLS,  # False＝市场腿退回「价格指数 + 股息率」
    }


def snapshot_benchmark(code: str, run_id: str) -> dict:
    """这一批快照实际用的市场基准，从磁盘上的留痕读，不猜默认值。

    manifest 的 `benchmark` 是每批各写一份的，换基准复现出来的新批次也记自己那一个；老批次的 manifest
    还没有这个字段，才退回两份凭证里的 `used_benchmark`（新批次）或 `benchmark.code`（单基准的老批次）。
    三处都没有的批次（--beta 建链没取行情）回空串，调用方据此放弃「换基准」的判断——没有证据就不能声称知道它用过谁。
    """
    used = (store.load_manifest(code, run_id) or {}).get("benchmark") or ""
    if not used:
        for name in (prices.RAW_NAME, market.RAW_NAME):
            payload = store.load_raw(code, run_id, name) or {}
            used = payload.get("used_benchmark") or str((payload.get("benchmark") or {}).get("code") or "")
            if used:
                break
    return _benchmark_option(str(used))


def prefetched_benchmarks(code: str, run_id: str) -> list[str]:
    """这一批的凭证里已经备着收盘序列的基准——切到它们只读盘，切到集合外的才要联网。

    新批次的两份凭证记的是同一张预取表（同一个账本切的），老批次可能只落盘了其中一份，所以取并集：
    一份凭证说有，就意味着那一段的数确实在盘上。集合里只有下拉那几家，白名单外的代码永远不在其中。
    """
    have = set(prices.prefetched(store.load_raw(code, run_id, prices.RAW_NAME)))
    have |= set(market.prefetched(store.load_raw(code, run_id, market.RAW_NAME)))
    return sorted(have)


def panel_payload(
    code: str,
    run_id: str,
    overrides: dict | None = None,
    method: str = "fcff",
    terminal: str = "gordon",
    rate: Any = None,
    mid_year: bool | None = None,
    check_consistency: bool = True,
) -> dict:
    """Everything one panel render needs, plus the cost of producing it."""
    started = time.perf_counter()
    method, terminal, rate = _validated(method, terminal, rate)
    ladder, path = store.load_ladder(code, run_id)
    run_id = path.parent.name  # a blank run in the query string resolves to the newest baseline
    report = attribute(ladder, overrides or {}, method, terminal, mid_year=mid_year, rate=rate)

    current = apply_overrides(inputs_from_ladder(ladder), overrides or {})
    # 「ERP 窗口」下拉的选项由这里下发：四行实测里能用的那几档。归因后的行只多了「改到多少」，
    # baseline 与 kind 与机器基线一致，所以选项与当前改动无关——切到一档不会把别的档位藏起来。
    # 选中一档只是把它的实测数写进 overrides.erp，走的就是面板改数那条路，所以 manual.json 里没有窗口名。
    windows = erp_window_options(report["rows"])
    chosen = erp_window_choice(windows, (overrides or {}).get("erp"))
    payload: dict[str, Any] = {
        "code": code,
        "run_id": run_id,
        "baseline": str(path),
        "ticker": ladder.get("ticker") or {},
        "as_of": ladder.get("as_of"),
        # 建链那一刻的顶层降级告示（老批次的 baseline 里还没有这个字段，读成空）；
        # 窗口被选中时那句「退回惯例值、与这段窗口脱钩」说的已是上一回事，改口。
        "warnings": erp_warnings_for_window(ladder.get("warnings") or [], chosen),
        "erp_windows": windows,
        "erp_window": chosen,
        # 这批数据的市场腿是谁：面板下拉的初始值，读的是凭证而不是本机默认值
        "benchmark": snapshot_benchmark(code, run_id),
        "attribution": report,
        "matrix": matrix(current, mid_year=report["mid_year"], rate=rate),
        # A previous 保存修正, so the panel can offer to restore it rather than lose it on reload.
        "saved": store.load_manual(code, run_id),
    }
    consistency_started = time.perf_counter()
    payload["cross_check"] = (
        cross_check(current, rate=rate, mid_year=report["mid_year"]) if check_consistency else []
    )
    payload["timings"] = {
        "total_ms": round((time.perf_counter() - started) * 1000, 1),
        "cross_check_ms": round((time.perf_counter() - consistency_started) * 1000, 1),
        "evaluate_count": 2 + sum(1 for row in report["rows"] if row["changed"] and row["editable"]),
    }
    return payload


def derivation_payload(code: str, run_id: str) -> dict:
    """推导图的骨架：行与行的出处关系加机器基线。只读磁盘，不跑引擎、更不联网。

    图上改数走的是面板同一套 `POST /api/recompute` 与 `POST /api/save`——这里刻意不给第二条写入路径，
    否则「图上改了但 CLI 复现不出来」就有了藏身之处。`saved` 只为把「有未载入的人工修正」显示出来。
    """
    ladder, path = store.load_ladder(code, run_id)
    run_id = path.parent.name  # 空白批次落到最新那一份，基准与预取集合都要按它来读
    return {
        "code": code,
        "run_id": run_id,
        "baseline": str(path),
        "ticker": ladder.get("ticker") or {},
        "as_of": ladder.get("as_of"),
        "warnings": ladder.get("warnings") or [],
        # 图上的「ERP 窗口」下拉与面板同源：选项还是那四行实测，选中之后走的是 /api/recompute 那条路，
        # 所以这里只给选项，当前选中谁由页面上的 overrides 说了算。
        "erp_windows": erp_window_options(flat_rows(ladder)),
        # 顶栏的「市场基准」下拉与新鲜度判断在两页共用同一套机器，所以这里也要给这批实际用的基准
        "benchmark": snapshot_benchmark(code, run_id),
        "saved": store.load_manual(code, run_id),
        **derivation_graph(ladder),
    }


def _beta(value: Any) -> float | None:
    """留空＝走回归。人工 β 是给「实测为负／没有行情」那只票留的出路，所以它自己也得是正数。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        beta = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"贝塔必须是数字或留空，收到 {value!r}") from None
    if beta <= 0:
        raise ValueError(f"贝塔必须是正数（β≤0 时 CAPM 折不出可用折现率），收到 {beta}")
    return beta


def _needed(code: str, run_id: str, reason: str, *, blocked: str = "", benchmark: dict, **extra: Any) -> dict:
    """No batch to judge, or a batch that never produced a baseline — 只能联网，无需逐源比对。

    `blocked` 是名录给的准入否决理由（非空即否决，这只票不在市）：动作仍然是 fetch，因为按钮该照常可点、
    点了要在 0.2 秒内把同一句话说回来，而不是让面板多长出一个前端要特判的动作值。

    `benchmark` 照样带上：这几条路里盘上没有可比的对象，所以 snapshot 空、switch 假，但下一次联网用哪个
    基准是已经定下来的事实。少这一个键，前端就得替每条早退分支各记一份「这里有没有 benchmark」。
    """
    return {
        "code": code,
        "run_id": run_id,
        "as_of": None,
        "fetched_at": None,
        "age_hours": None,
        "action": "fetch",
        "stale": [],
        "stale_groups": [],
        "blocked": blocked,
        "benchmark": benchmark,
        "reason": reason,
        **extra,
    }


def valuation_status(
    code: str, run_id: str = "", *, beta_given: bool = False, benchmark: str = "", now: datetime | None = None
) -> dict:
    """「取数估值」只有一个按钮，新旧由这里判断（2026-09-24 定的分源阈值）.

    判断依据全在磁盘上：manifest 记每个来源的取数时刻，baseline 记报告期，名录记这只票还在不在市，三者都不需要联网。
    所以这里只回答「这次要不要回线上」和「回线上有没有意义」，绝不代替用户去取数——需要重取时仍然走 jobs，
    凭证照样落盘。

    名录那条只是否决（`blocked` 非空），不是新动作：动作仍是 fetch，按钮照常可点，点了由 `start_fetch`
    原句回错。它答不了（没有索引或索引旧过七天）就当没有意见。

    请求的基准不在这批预取的集合里时也算「要重取」，而且这条压得过有效期：β 是相对某个指数的回归斜率，
    ERP 是相对某个指数的超额收益，换了市场组合以后旧快照里的数回答的是另一个问题。集合内的基准另一回事——
    一次预取已把下拉那几家的序列都存进凭证，换过去只是切另一条序列，不联网（`switch` 为真，仍另开批次）。
    """
    now = now or utc_now()
    code = str(code).strip()
    # 基准在这里就算出来：它属于「下一次要联网的话联什么」，跟盘上有没有批次无关，所以四条早退分支也各带一份。
    requested = _benchmark_option(_benchmark(benchmark))
    nothing_to_switch = {"requested": requested, "snapshot": {}, "prefetched": [], "switch": False}
    # 名录只回答准入（这只票还在不在市），一个进模型的数都不从它取；它答不了时返回放行，与 universe.listed
    # 的 fail-open 同一条纪律。这里只把它的答案摆在这一行让用户按下按钮之前就看见——按钮不锁。
    allowed, absent, denied = builder.admission(code)
    latest = store.latest_run(code)
    if latest is None:
        return _needed(
            code,
            "",
            denied if not allowed else f"runs/{code} 下还没有取数批次，这一次要联网",
            blocked=absent,
            benchmark=nothing_to_switch,
        )
    run_id = run_id or latest["run_id"]
    failure = latest["failed"] if run_id == latest["run_id"] else None
    if failure:
        return _needed(
            code,
            run_id,
            denied
            if not allowed
            else f"上次取数（{run_id}）没写出基线：{failure.get('reason') or '原因未记录'}，这一次要联网",
            blocked=absent,
            benchmark=nothing_to_switch,
            failed=failure,
        )

    manifest = store.load_manifest(code, run_id)
    if manifest is None:
        return _needed(
            code, run_id, f"{run_id} 缺 manifest.json，无从判断新旧，这一次要联网", blocked=absent, benchmark=nothing_to_switch
        )
    try:
        ladder, _ = store.load_ladder(code, run_id)
    except FileNotFoundError:
        return _needed(
            code, run_id, f"{run_id} 缺 baseline.json，这一次要联网", blocked=absent, benchmark=nothing_to_switch
        )

    as_of = ladder.get("as_of")
    check = freshness.decision(freshness.source_stamps(manifest), as_of, now, beta_given=beta_given)
    snapshot = snapshot_benchmark(code, run_id)
    have = prefetched_benchmarks(code, run_id)
    # 换基准要不要联网，看的是这批预取过谁，不是它当初用了谁：一次预取把下拉那几家的收盘序列全存进同一份凭证，
    # 换一个基准只是从盘上切另一条序列，旧的 β 与 ERP 才会作废。集合外的基准没有序列可读，才要重新联网。
    if have and requested["code"] not in have:
        got = "、".join(f"{_benchmark_name(item)}（{item}）" for item in have)
        why = (
            f"这次请求 {requested['name']}（{requested['code']}），而这批快照的凭证里只有 {got} 的收盘序列"
            "——β 是相对指数的回归斜率、ERP 是相对指数的超额收益，旧快照不复用"
        )
        if not requested["total_return"]:
            why += "；该指数在中证官网没有全收益孪生，市场腿改取「价格指数 + 股息率」"
        check["action"] = "fetch"
        check["stale"].append({"group": "基准指数", "sources": [prices.RAW_NAME, market.RAW_NAME], "reason": why})
        check["stale_groups"].append("基准指数")
    # 换了基准但序列就在盘上：不联网，复现成另一个批次（M4-4 由 /api/fetch 起这个作业）。
    switch = bool(snapshot["code"]) and requested["code"] != snapshot["code"] and requested["code"] in have
    started = freshness.parse(manifest.get("created_at"))
    age_hours = round((now - started).total_seconds() / 3600, 1) if started else None
    if check["action"] == "replay":
        reason = (
            f"这批已预取 {requested['name']}（{requested['code']}）的收盘序列，换成它不联网；"
            f"会另开一个批次复现，{run_id} 原样留着"
            if switch
            else f"{run_id} 仍在有效期内（报告期 {as_of}，{age_hours} 小时前取数），复现快照不联网"
        )
    else:
        reason = "需要重取：" + "；".join(f"{row['group']} — {row['reason']}" for row in check["stale"])
        # 退市／查无此票时不必再数哪几组过期：这一次根本不会联网，名录那一句话就是全部答复。
        if not allowed:
            reason = denied
    return {
        "code": code,
        "run_id": run_id,
        "as_of": as_of,
        "fetched_at": manifest.get("created_at"),
        "age_hours": age_hours,
        "latest": run_id == latest["run_id"],
        "benchmark": {"requested": requested, "snapshot": snapshot, "prefetched": have, "switch": switch},
        # 复现不查名录（盘上的历史证据今天否决不了），所以否决只在真要联网的那条路上生效。
        "blocked": "" if (allowed or check["action"] == "replay" or switch) else absent,
        **check,
        "reason": reason,
    }


def start_fetch(body: dict) -> dict:
    """Queue a live build for a typed-in code — or say why the stored one is still current.

    Only the defaults travel with a panel fetch — rf 中枢、g 保守情形、租赁计债，加上这一批的市场组合
    （`benchmark`，默认中证2000）. Every one of them is a row on the ladder afterwards, so switching 口径
    in the browser means editing the row rather than adding a second set of query parameters that could
    disagree with the CLI's flags. Two are the exception, because both are decided *before* there is a
    ladder to edit: `--beta` is the escape hatch for a stock whose β comes out negative or has no quotes
    at all, and `--benchmark` is a choice about which market the stock is measured against — you cannot
    read it off a ladder built against a different index.

    `force` skips the freshness check: 用户想要一份新凭证（换了网络环境、怀疑上一批的答复）时，
    阈值不该把他关在旧快照里。它不跳过名录准入——2026-10-15 定的口径是「名录就是最终裁判，不给后门」，
    强制重取管的是新旧，不管这只票还在不在市。
    """
    code = str(body.get("code", "")).strip()
    if not code:
        raise ValueError("请先填写 6 位股票代码")
    try:
        ticker = Ticker(code)
    except NotImplementedError as exc:  # 北交所：交易所路由尚未接通，说人话而不是抛 500
        raise ValueError(str(exc)) from None
    beta = _beta(body.get("beta"))
    benchmark = _benchmark(body.get("benchmark"))
    status = valuation_status(
        ticker.code, str(body.get("run", "")), beta_given=beta is not None, benchmark=benchmark
    )
    if status["action"] == "replay" and not body.get("force"):
        if not status["benchmark"]["switch"]:
            return {"replayed": True, **status}
        # 换的是预取集合内的另一个基准：序列已在盘上，复现成一批新的就够，一次网也不联。
        # 复现必须真的开新目录——β 与 ERP 两行都要按新基准重算，原地改写会让上一批的留痕消失。
        return {
            "job_id": jobs.start(
                ticker.code,
                builder.BuildOptions(code=ticker.code, from_run=status["run_id"], beta=beta, benchmark=benchmark),
            ),
            "code": ticker.code,
            "benchmark": benchmark,
            "action": "replay",
            "replayed_from": status["run_id"],
            "reason": status["reason"],
        }
    # 名录说这只票不在市：一个来源都不碰，也不排队——排队了才会发现 jobs 里那条链要 0.2 秒才炸，
    # 而炸之前面板已经显示「已排队」，那是假进度。这里直接回错，文案与 CLI 同一句。
    if status["blocked"]:
        raise ValueError(status["reason"])
    return {
        "job_id": jobs.start(
            ticker.code, builder.BuildOptions(code=ticker.code, beta=beta, benchmark=benchmark)
        ),
        "code": ticker.code,
        "benchmark": benchmark,
        "action": "fetch",
        "stale_groups": status["stale_groups"],
        "reason": status["reason"],
    }


def job_status(job_id: str) -> dict:
    return jobs.get(job_id)


def save_manual(code: str, run_id: str, document: dict) -> dict:
    """Persist the human's corrections next to the evidence they were derived from."""
    method, terminal, rate = _validated(
        document.get("method", "fcff"), document.get("terminal", "gordon"), document.get("rate")
    )
    overrides = document.get("overrides") or {}
    ladder, path = store.load_ladder(code, run_id)
    apply_overrides(inputs_from_ladder(ladder), overrides)  # reject unknown keys / derived rows before writing
    saved = store.write_manual(
        code,
        run_id,
        {
            "baseline": path.parent.name,
            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "overrides": overrides,
            "method": method,
            "terminal": terminal,
            "rate": rate,
            "mid_year": document.get("mid_year"),
            "per_share": document.get("per_share"),
        },
    )
    return {"saved": str(saved), "overrides": overrides}
