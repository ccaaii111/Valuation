"""--from-run 的落盘语义：复现只读源批次，证据另起一个新批次。 python tests/test_replay_run.py

原来的缺陷是 `RunStore.replay` 把 store 指回被复现批次自己的目录，于是建链结尾的
`write_result("baseline")` + `finish()` 当场复写了那份批次的原基线与取数凭证——换个口径 flag 重跑一次，
机器基线就没了，而且 manifest 的 created_at 被改成当下，一个月前的证据自称「刚刚取数」。
这里钉四件事：源批次字节不变、新批次带全套证据、created_at 跟着证据走、失败只落在新批次里。
最后一条测的是 `--horizon`：复现分支同样是建链，命令行填的年数要能在盘上那一行读到。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import builder  # noqa: E402
from backend.data import freshness, transport  # noqa: E402
from backend.data.transport import RunStore  # noqa: E402
from backend.model.assumptions import (  # noqa: E402
    HORIZON_MAX_YEARS,
    HORIZON_MIN_YEARS,
    HORIZON_TARGET_YEARS,
    BetaNotApplicable,
)
from backend.web import store as web_store  # noqa: E402
from fixtures import synthetic_snapshot  # noqa: E402

# 复现分支只把 fundamentals.replay 当作「读磁盘上那份原始答复」，测试里让它回一份合成快照，绝不联网。
builder.fundamentals.replay = lambda ticker, store: synthetic_snapshot()

FAILURES = 0
CODE = "000001"
SOURCE = "20260920-093000"
EVIDENCE_AT = "2026-09-20T01:30:00Z"
NAMES = ("main_indicators", "balance_sheet", "income_statement", "cash_flow", "consensus", "quote")
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


def write_source(with_manifest: bool = True) -> Path:
    """一份「已经取过数」的批次：raw/ 有原始答复，manifest 有每个来源的取数时刻，baseline 是上一轮的原基线。"""
    directory = transport.RUNS_DIR / CODE / SOURCE
    (directory / "raw").mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        payload = {"source": name, "answer": 1}
        (directory / "raw" / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if with_manifest:
        sources = [
            {
                "name": name,
                "url": f"https://example.test/{name}",
                "fetched_at": EVIDENCE_AT,
                "sha256": f"{index:064x}",
                "path": f"raw/{name}.json",
            }
            for index, name in enumerate(NAMES)
        ]
        document = {"run_id": SOURCE, "code": CODE, "created_at": EVIDENCE_AT, "sources": sources}
        (directory / "manifest.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    baseline = {"as_of": "2025-12-31", "ticker": {"name": "源批次的原基线"}, "note": "复现不该碰它"}
    (directory / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    return directory


def fingerprint(directory: Path) -> dict:
    """目录里每个文件的名字与字节数——复现前后各查一次，一样才算「没动过」。"""
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    return {path.relative_to(directory).as_posix(): path.stat().st_size for path in files}


def fork_and_build(*, from_run: str = SOURCE, **flags) -> builder.BuildResult:
    """走完整条链（复现取数 → 测假设 → 写盘），只是不联网：rf 留空、β 人工给。"""
    return builder.build(builder.BuildOptions(code=CODE, from_run=from_run, no_risk_free=True, **flags))


@contextlib.contextmanager
def runs_dir():
    """把两处 RUNS_DIR 一起挪进临时目录：builder 走 transport，面板走 web.store。"""
    temp = Path(tempfile.mkdtemp())
    original = transport.RUNS_DIR, web_store.RUNS_DIR
    transport.RUNS_DIR = web_store.RUNS_DIR = temp
    try:
        yield temp
    finally:
        transport.RUNS_DIR, web_store.RUNS_DIR = original
        shutil.rmtree(temp, ignore_errors=True)


def test_the_source_batch_is_not_touched() -> None:
    with runs_dir() as temp:
        source = write_source()
        before, source_bytes = fingerprint(source), (source / "baseline.json").read_bytes()
        result = fork_and_build(beta=1.0)
        check("复现写到新批次，不写回源批次", result.run_id != SOURCE, f"{result.run_id} vs {SOURCE}")
        check("源批次一个文件也没多", fingerprint(source) == before, str(set(fingerprint(source)) ^ set(before)))
        check("源批次的原基线一字未改", (source / "baseline.json").read_bytes() == source_bytes)
        check("新批次写得出基线", result.baseline_path.is_file())
        check("基线落在新批次目录下", result.baseline_path.parent == temp / CODE / result.run_id, str(result.baseline_path))


def test_the_fork_carries_the_evidence() -> None:
    with runs_dir():
        source = write_source()
        result = fork_and_build(beta=1.0)
        store = result.store
        manifest = json.loads((store.dir / "manifest.json").read_text(encoding="utf-8"))
        copied = sorted(path.name for path in store.raw_dir.glob("*.json"))
        check("原始答复整套复制过来", copied == sorted(f"{name}.json" for name in NAMES), str(copied))
        same = all((store.raw_dir / f"{n}.json").read_bytes() == (source / "raw" / f"{n}.json").read_bytes() for n in NAMES)
        check("复制过来的字节相同", same)
        check("按名字还读得到", store.load("quote") == {"source": "quote", "answer": 1}, str(store.load("quote")))
        stamps = [entry.get("fetched_at") for entry in manifest["sources"]]
        check("每个来源的取数时刻照抄", stamps == [EVIDENCE_AT] * len(NAMES), str(stamps))
        check("created_at 跟着证据走", manifest["created_at"] == EVIDENCE_AT, str(manifest["created_at"]))
        check(
            "出处记在凭证里",
            manifest.get("replayed_from") == {"run_id": SOURCE, "created_at": EVIDENCE_AT},
            str(manifest.get("replayed_from")),
        )
        check("批次名是这次建链自己的时刻", store.run_id == datetime.now().strftime("%Y%m%d-%H%M%S"), store.run_id)

        again = fork_and_build(from_run=store.run_id, beta=1.0)  # 复现的复现：证据链要能一路指回最初联网那一批
        nested = json.loads((again.store.dir / "manifest.json").read_text(encoding="utf-8"))
        check("留痕认最初那一批", again.store.snapshot_ref == SOURCE, again.store.snapshot_ref)
        check("父批是直接来源", nested["replayed_from"]["run_id"] == store.run_id, str(nested["replayed_from"]))
        check("证据时刻仍是最初取数那一刻", nested["created_at"] == EVIDENCE_AT, str(nested["created_at"]))
        check(
            "复制来的凭证条目没被改",
            json.loads((store.dir / "manifest.json").read_text(encoding="utf-8"))["sources"]
            == json.loads((again.store.dir / "manifest.json").read_text(encoding="utf-8"))["sources"],
        )


def test_two_calibers_coexist_off_one_batch() -> None:
    """缺陷 #1 的用户可见症状：同一份证据跑两个口径，后一个不该吃掉前一个。"""
    with runs_dir() as temp:
        write_source()
        first = fork_and_build(beta=1.0)
        second = fork_and_build(beta=0.5)
        runs = {row["run_id"] for row in web_store.list_runs(CODE)}
        check("两个口径的基线并存", {first.run_id, second.run_id, SOURCE} <= runs, str(sorted(runs)))
        check("两份基线各有各的凭证", first.run_id != second.run_id, f"{first.run_id} vs {second.run_id}")
        origins = {
            json.loads((temp / CODE / run / "manifest.json").read_text(encoding="utf-8"))["replayed_from"]["run_id"]
            for run in (first.run_id, second.run_id)
        }
        check("两份都认源批次", origins == {SOURCE}, str(origins))


def test_a_legacy_batch_without_manifest_still_reads() -> None:
    """老批次只有 raw/：复现出的凭证要说「不知道几时取的」，而不是「什么都没取到」。"""
    with runs_dir():
        write_source(with_manifest=False)
        result = fork_and_build(beta=1.0)
        manifest = json.loads((result.store.dir / "manifest.json").read_text(encoding="utf-8"))
        names = sorted(entry["name"] for entry in manifest["sources"])
        check("凭证按 raw/ 里的文件补上", names == sorted(NAMES), str(names))
        check("补的条目不带取数时刻", all("fetched_at" not in entry for entry in manifest["sources"]))
        check("路径指向本批次的副本", all((result.store.dir / entry["path"]).is_file() for entry in manifest["sources"]))
        verdict = freshness.decision(freshness.source_stamps(manifest), "2025-12-31", NOW)
        check("新鲜度因此判它要重取", verdict["action"] == "fetch", str(verdict["action"]))
        check("理由是缺凭证而非没取过", "缺取数凭证" in verdict["stale_groups"], str(verdict["stale_groups"]))


def test_a_failed_build_only_marks_the_fork() -> None:
    """缺陷 #4：失败标记和上一轮基线并存同一目录，「这批有没有基线」就含糊了。"""
    with runs_dir() as temp:
        source = write_source()
        try:
            fork_and_build(beta=0.0)  # β≤0 是 CAPM 折不出发，走 BetaNotApplicable 那条
            check("复现建链失败会抛出", False, "没抛异常")
        except BetaNotApplicable as exc:
            check("复现建链失败会抛出", "β" in str(exc), str(exc)[:60])
        forks = sorted(entry for entry in (temp / CODE).iterdir() if entry.name != SOURCE)
        check("失败也落在一个新批次", len(forks) == 1 and (forks[0] / "build_failed.json").is_file(), str([p.name for p in forks]))
        check("失败的新批次里没有基线", not (forks[0] / "baseline.json").exists())
        check("源批次的基线还在", (source / "baseline.json").is_file())
        check("源批次没被打上失败标记", not (source / "build_failed.json").exists())
        check("面板还读得到源批次", web_store.load_ladder(CODE, SOURCE)[0]["note"] == "复现不该碰它")
        runs = {row["run_id"] for row in web_store.list_runs(CODE)}
        check("没基线的复现批次不进列表", runs == {SOURCE}, str(sorted(runs)))


def test_a_missing_batch_is_named_instead_of_half_built() -> None:
    with runs_dir() as temp:
        try:
            RunStore.fork(CODE, "20260101-000000")
            check("批次不存在直接报错", False, "没抛异常")
        except FileNotFoundError as exc:
            check("批次不存在直接报错", "没有这个取数批次" in str(exc), str(exc)[:60])
        check("什么也没留下", not any(temp.rglob("*")), str(list(temp.rglob("*"))[:3]))


def written_rows(path: Path) -> tuple[dict, list]:
    """读盘上那份 baseline.json：键 → 行，外加顶层 warnings。复现的产物最终要靠它说话。"""
    document = json.loads(path.read_text(encoding="utf-8"))
    return {row["key"]: row for group in document["groups"] for row in group["rows"]}, document["warnings"]


def test_the_horizon_flag_reaches_the_written_chain() -> None:
    """`--horizon` 从 BuildOptions 一路走到盘上那一行——把 builder.build() 里那句透传删掉，这条要红。

    E1/E2 把年数的语义钉在模型层与重算层，唯独「人填的年怎么进链」这一段没人管：删掉透传不会有任何套件变
    红，面板与 CLI 只会显示一个谁都没填过的默认 10 年，而且增速行照它折——错得最难发现的那种。
    夹具的合意预期有 2026/2027 两个锚点，所以下限是 max(5, 2 + 1) = 5 年。
    """
    with runs_dir() as temp:
        write_source()
        rows, warnings = written_rows(fork_and_build(beta=1.0, horizon=7).baseline_path)
        check("填 7 年链上就是 7 年", rows["horizon_target"]["baseline"] == 7, str(rows["horizon_target"]["baseline"]))
        check("增速行真的只走 7 年", len(rows["revenue_growth_path"]["baseline"]) == 7, str(len(rows["revenue_growth_path"]["baseline"])))
        check("年数那一行跟着落盘", rows["horizon_years"]["baseline"] == 7, str(rows["horizon_years"]["baseline"]))
        check("在界内不喊钳位", not any("超过上限" in text or "低于下限" in text for text in warnings), str(warnings))

        rows, _ = written_rows(fork_and_build(beta=1.0).baseline_path)
        check("不给就按默认年数走", rows["horizon_target"]["baseline"] == HORIZON_TARGET_YEARS, str(rows["horizon_target"]["baseline"]))

        rows, warnings = written_rows(fork_and_build(beta=1.0, horizon=20).baseline_path)
        check("超上限时链上存实走的年数", rows["horizon_target"]["baseline"] == HORIZON_MAX_YEARS, str(rows["horizon_target"]["baseline"]))
        check("凭证说清了是谁压的", f"填 20 年超过上限 {HORIZON_MAX_YEARS} 年" in rows["horizon_target"]["evidence"], rows["horizon_target"]["evidence"][:80])
        check("顶层警告跟着指路", any("超过上限" in text for text in warnings), str(warnings))

        rows, warnings = written_rows(fork_and_build(beta=1.0, horizon=3).baseline_path)
        check("低于下限按下限走", rows["horizon_target"]["baseline"] == HORIZON_MIN_YEARS, str(rows["horizon_target"]["baseline"]))
        check("下限那句点明取大规则", f"低于下限 {HORIZON_MIN_YEARS} 年" in rows["horizon_target"]["evidence"], rows["horizon_target"]["evidence"][:80])
        check("被下限压过也出声", any("低于下限" in text for text in warnings), str(warnings))

        # CLI 那一头：`--horizon` 的 argparse 接线。参数名一改（dest 打错、忘了落进 BuildOptions）
        # 上面几条照旧全绿，因为没人从命令行走进来。这里真跑一次 main()，走复现分支所以不联网。
        spec = importlib.util.spec_from_file_location("build_baseline", ROOT / "scripts" / "build_baseline.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        module.ROOT = temp  # 收尾那句 run: <相对路径> 按 ROOT 求相对，批次在临时目录就得跟着一道挪
        with contextlib.redirect_stdout(io.StringIO()) as noise:
            exit_code = module.main([CODE, "--from-run", SOURCE, "--no-risk-free", "--beta", "1.0", "--horizon", "9"])
        produced = sorted(path for path in (temp / CODE).iterdir() if (path / "baseline.json").is_file())
        check("CLI 退出码 0", exit_code == 0, f"{exit_code} {noise.getvalue()[:80]}")
        rows, _ = written_rows(produced[-1] / "baseline.json")
        check("--horizon 9 走到盘上的那一行", rows["horizon_target"]["baseline"] == 9, str(rows["horizon_target"]["baseline"]))


if __name__ == "__main__":
    for name in sorted(globals()):
        if name.startswith("test_"):
            print(f"[{name}]")
            globals()[name]()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
