"""HTTP transport and per-run snapshot store.

Snapshots are audit evidence of what was fetched from where, not a local data source.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "runs"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def now_iso() -> str:
    return _now()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Fetched:
    name: str
    url: str
    fetched_at: str
    sha256: str
    payload: Any


class Transport:
    def __init__(self, timeout: float = 30.0, retries: int = 3):
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
        )
        self._retries = retries

    def get_json(
        self, name: str, url: str, params: dict | None = None, headers: dict | None = None, attempts: int | None = None
    ) -> Fetched:
        last: Exception | None = None
        for attempt in range(attempts or self._retries):
            try:
                response = self._client.get(url, params=params, headers=headers or {})
                response.raise_for_status()
                text = response.text
                if not text.lstrip("\ufeff").startswith(("{", "[")):
                    raise ValueError(f"non-JSON body (likely blocked or wrong params): {text[:80]!r}")
                return Fetched(
                    name=name,
                    url=str(response.request.url),
                    fetched_at=_now(),
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    payload=json.loads(text),
                )
            except Exception as exc:  # noqa: BLE001 - retry on any transport/parse failure
                last = exc
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"{name}: failed after {attempts or self._retries} attempts: {last}")

    def get_text(
        self,
        name: str,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        encoding: str = "utf-8",
        attempts: int | None = None,
    ) -> Fetched:
        last: Exception | None = None
        for attempt in range(attempts or self._retries):
            try:
                response = self._client.get(url, params=params, headers=headers or {})
                response.raise_for_status()
                text = response.content.decode(encoding, errors="replace")
                if not text.strip():
                    raise ValueError("empty body")
                return Fetched(
                    name=name,
                    url=str(response.request.url),
                    fetched_at=_now(),
                    sha256=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                    payload={"text": text},
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"{name}: failed after {attempts or self._retries} attempts: {last}")

    def close(self) -> None:
        self._client.close()


class RunStore:
    """Writes raw payloads plus a manifest; a build replayed from an old batch forks into its own directory."""

    def __init__(self, run_id: str, code: str):
        self.run_id = run_id
        self.code = code
        self.dir = RUNS_DIR / code / run_id
        self.raw_dir = self.dir / "raw"
        self.created_at = _now()
        self.replayed_from: dict | None = None
        # 这一批建链实际用的市场基准。凭证是多个基准共用的（一次预取全存下），换基准复现也不会改写它，
        # 所以「这批用的是谁」只能记在每批各写一份的 manifest 里，不能去 raw 里猜。
        self.benchmark = ""
        # 快照出自哪一批：复现链上的行留痕要指回最初联网取数的那一批，别停在中间某层副本上。
        self.snapshot_ref = run_id
        self._manifest: list[dict] = []

    @classmethod
    def create(cls, code: str) -> "RunStore":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        store = cls(ts, code)
        store.raw_dir.mkdir(parents=True, exist_ok=True)
        return store

    @classmethod
    def fork(cls, code: str, source_run_id: str) -> "RunStore":
        """Carry an existing batch's 取数证据 into a **new** directory; the source batch becomes read-only.

        `created_at` 跟着证据走而不是跟着目录走：这次建链没联网，凭证的取数时刻仍是源批次那一刻。
        否则一个月前的原始答复换个口径 flag 重跑一遍就自称「刚刚取数」，而 api.valuation_status 显示的正是它。
        目录名用的是 fork 自己的时刻，所以同一份证据可以并存多个口径的批次。
        """
        source = cls(source_run_id, code)
        if not source.dir.is_dir():
            raise FileNotFoundError(f"没有这个取数批次: {source.dir}")
        if not source.raw_dir.is_dir():
            raise FileNotFoundError(f"取数批次 {source_run_id} 没有原始数据可复现：请重新联网取数（不带 --from-run）")
        entries, evidence_at, origin = source._evidence()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        while (RUNS_DIR / code / stamp).exists():
            # 批次名精确到秒：同一秒里已经有一次建链就等下一秒，绝不互相覆盖（复现同一份证据跑两个口径会撞上）
            time.sleep(1.0)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        store = cls(stamp, code)
        store.raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source.raw_dir, store.raw_dir, dirs_exist_ok=True)
        store._manifest = entries
        store.created_at = evidence_at
        # replayed_from 记「直接父批」，snapshot_ref 记「当初真正联网的那一批」：连复现两层也不会指错。
        store.snapshot_ref = (origin or {}).get("run_id", source_run_id)
        store.replayed_from = {"run_id": source_run_id, "created_at": evidence_at}
        return store

    def _evidence(self) -> tuple[list[dict], str, dict | None]:
        """Read back this batch's 凭证 (sources, created_at, replayed_from). 老批次没有 manifest.json 时按
        raw/ 里真有的文件补条目，时间戳留空——「不知道何时取的」必须表现为缺凭证（新鲜度判它会重取），
        而不是 manifest 里一句「什么都没取到」。
        """
        manifest = self.dir / "manifest.json"
        if manifest.exists():
            document = json.loads(manifest.read_text(encoding="utf-8"))
            sources = list(document.get("sources") or [])
            return sources, document.get("created_at") or self.created_at, document.get("replayed_from")
        entries = [
            {"name": path.stem, "path": str(path.relative_to(self.dir))}
            for path in sorted(self.raw_dir.glob("*.json"))
        ]
        return entries, self.created_at, None

    def record(self, item: Fetched) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"{item.name}.json"
        path.write_text(json.dumps(item.payload, ensure_ascii=False, indent=1), encoding="utf-8")
        self._manifest.append(
            {k: v for k, v in asdict(item).items() if k != "payload"} | {"path": str(path.relative_to(self.dir))}
        )

    @property
    def source_count(self) -> int:
        return len(self._manifest)

    def load(self, name: str) -> Any:
        entry = next((m for m in self._manifest if m["name"] == name), None)
        if entry:
            return json.loads((self.dir / entry["path"]).read_text(encoding="utf-8"))
        path = self.raw_dir / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_result(self, name: str, obj: Any) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def finish(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "manifest.json"
        document = {
            "run_id": self.run_id,
            "code": self.code,
            "created_at": self.created_at,
            "sources": self._manifest,
        }
        if self.benchmark:
            document["benchmark"] = self.benchmark
        if self.replayed_from:
            # 复现链的出处：这批的原始证据来自哪一批、那批是几时取的。
            document["replayed_from"] = self.replayed_from
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)
