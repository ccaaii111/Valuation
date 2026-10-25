"""The only module that knows the on-disk layout of runs/.

Baselines are audit evidence produced by build_baseline.py; the panel reads them and never fetches.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from backend.data.transport import RUNS_DIR

CODE_RE = re.compile(r"[0-9A-Za-z]{1,10}")
RUN_RE = re.compile(r"\d{8}-\d{6}")
MANUAL_NAME = "manual.json"


def resolve_run(code: str, run_id: str) -> str:
    """A blank batch means "the newest one", the same default the CLI uses."""
    if run_id:
        return run_id
    runs = list_runs(code) if CODE_RE.fullmatch(code or "") else []
    if not runs:
        raise FileNotFoundError(f"runs/{code} 下还没有基线：输入代码点「取数估值」，或跑 build_baseline.py")
    return runs[0]["run_id"]


def _checked(code: str, run_id: str) -> Path:
    # Both parts come from the query string, so a stray ../ must never reach the filesystem.
    if not CODE_RE.fullmatch(code or ""):
        raise ValueError(f"非法证券代码: {code!r}")
    run_id = resolve_run(code, run_id)
    if not RUN_RE.fullmatch(run_id):
        raise ValueError(f"非法取数批次（应为 YYYYMMDD-HHMMSS）: {run_id!r}")
    directory = (RUNS_DIR / code / run_id).resolve()
    if not directory.is_relative_to(RUNS_DIR.resolve()):
        raise ValueError(f"批次目录越界: {run_id}")
    return directory


def path_for(code: str, run_id: str, name: str) -> Path:
    return _checked(code, run_id) / name


def load_ladder(code: str, run_id: str) -> tuple[dict, Path]:
    path = path_for(code, run_id, "baseline.json")
    if not path.is_file():
        raise FileNotFoundError(f"该批次没有 baseline.json: {path}")
    return json.loads(path.read_text(encoding="utf-8")), path


def load_manual(code: str, run_id: str) -> dict | None:
    path = path_for(code, run_id, MANUAL_NAME)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def write_manual(code: str, run_id: str, document: dict) -> Path:
    path = path_for(code, run_id, MANUAL_NAME)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_runs(code: str) -> list[dict]:
    directory = RUNS_DIR / code
    if not directory.is_dir():
        return []
    runs = []
    for entry in sorted(directory.iterdir()):
        if not RUN_RE.fullmatch(entry.name):
            continue
        baseline = entry / "baseline.json"
        if not baseline.is_file():
            continue
        ladder = json.loads(baseline.read_text(encoding="utf-8"))
        runs.append(
            {
                "run_id": entry.name,
                "as_of": ladder.get("as_of"),
                "name": (ladder.get("ticker") or {}).get("name"),
                "edited": (entry / MANUAL_NAME).is_file(),
            }
        )
    return sorted(runs, key=lambda r: r["run_id"], reverse=True)


def list_codes() -> list[dict[str, Any]]:
    if not RUNS_DIR.is_dir():
        return []
    out = []
    for directory in sorted(RUNS_DIR.iterdir()):
        if not directory.is_dir() or not CODE_RE.fullmatch(directory.name):
            continue
        runs = list_runs(directory.name)
        if runs:
            out.append({"code": directory.name, "name": runs[0]["name"], "runs": runs})
    return out


def load_manifest(code: str, run_id: str) -> dict | None:
    """The 取数凭证: which source answered at which UTC minute. 新鲜度判断全靠它。"""
    path = path_for(code, run_id, "manifest.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_raw(code: str, run_id: str, name: str) -> Any:
    """这一批的原始答复 raw/<name>.json；没落盘就回 None，由调用方决定「无从判断」怎么讲。"""
    path = _checked(code, run_id) / "raw" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_build_failure(code: str, run_id: str) -> dict | None:
    path = path_for(code, run_id, "build_failed.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def latest_run(code: str) -> dict | None:
    """The newest batch directory, **including** ones that ended in build_failed.json.

    list_runs() filters those out on purpose (no baseline = nothing to show). 新鲜度判断反过来要它们：
    「上次取数失败」本身就是要显示给用户的信息，不能因为没写出基线就当这次取数没发生过。
    """
    directory = RUNS_DIR / code
    if not CODE_RE.fullmatch(code or "") or not directory.is_dir():
        return None
    names = sorted((e.name for e in directory.iterdir() if e.is_dir() and RUN_RE.fullmatch(e.name)), reverse=True)
    if not names:
        return None
    run_id = names[0]
    baseline = directory / run_id / "baseline.json"
    ladder = json.loads(baseline.read_text(encoding="utf-8")) if baseline.is_file() else None
    failure = load_build_failure(code, run_id)
    return {"run_id": run_id, "as_of": (ladder or {}).get("as_of"), "failed": failure}
