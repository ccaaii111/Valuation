"""市场级数据的本机缓存：国债收益率、指数收盘、全市场股息率——取到最新一次就存下来，当天不再重取。

这三段与「估哪只票」无关，却从前每一批都重下一遍：2026-10-29 相隔两分半取数的两只票，两份
`raw/risk_free.json` 逐字节相同（各 234 KB），一趟指数预取还要再拉 11 条序列约 28 秒。放在
`data/market_cache/`（与 `data/universe.json` 同一种做法：原子写、读不出就按没缓存处理）之后，
第二只票这一步一次网也不联。

「已经是最新」按收盘轴判，不按文件年龄：缓存那次真正取数与本次取数落在同一根已收盘日之内（都归
`freshness.latest_close` 的同一个日子）就算新。15:00 之后取到的数当天有效，跨到新收盘必重取；
节假日与周末不会放大重取，也不会让上周的序列冒充本周的。

证据纪律不动一处：
- 每批 `raw/` 里的文件照旧写全，内容就是从缓存里拿的那份原始答复，一个字节都不减。
- manifest 的 `fetched_at` 记缓存里那次**真正联网**的时刻，不记本次复制时刻——与 `RunStore.fork`
  同一条口径（取数时刻跟着证据走，不跟着目录走）。所以新鲜度判断看到的仍是那一刻，缓存把三天前的
  答复伪装成刚取的这件事在结构上发生不了。
- 复现（`--from-run`）根本走不到这里：批次里已经有 `raw/`，读快照那条路先返回。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.data import freshness
from backend.data.transport import ROOT

CACHE_DIR = ROOT / "data" / "market_cache"
VERSION = 1

RISK_FREE = "risk_free"
INDEX_SERIES = "index_series"
MARKET_DIVIDEND = "market_dividend"


@dataclass
class Cached:
    """一份缓存：原始答复加它真正联网的那一刻，以及序列里最后一个观测日（只用于留痕与显示）。"""

    name: str
    fetched_at: str
    last_day: str
    payload: Any
    source: str = ""
    url: str = ""


@dataclass
class Cache:
    """`directory=None` 时用模块级 `CACHE_DIR`；测试把整只缓存指到临时目录，不碰真实盘。"""

    directory: Path | None = field(default=None)

    def path(self, name: str) -> Path:
        return (self.directory or CACHE_DIR) / f"{name}.json"

    def read(self, name: str, *, now: datetime | None = None) -> Cached | None:
        """盘上这份能用就用；读不出、版本不对、跨了新一根收盘，一律按没缓存处理（调用方照常联网）。"""
        path = self.path(name)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict) or document.get("version") != VERSION:
            return None
        if not fresh(document.get("fetched_at"), now=now):
            return None
        payload = document.get("payload")
        if payload in (None, {}, []):
            return None
        return Cached(
            name=name,
            fetched_at=str(document.get("fetched_at") or ""),
            last_day=str(document.get("last_day") or ""),
            payload=payload,
            source=str(document.get("source") or ""),
            url=str(document.get("url") or ""),
        )

    def save(
        self, name: str, payload: Any, *, fetched_at: str, last_day: str = "", source: str = "", url: str = ""
    ) -> Path | None:
        """落缓存；拿不到任何观测日（整条腿都是失败的）就不落，免得把一次故障钉成一天的默认答复。"""
        if not last_day:
            return None
        document = {
            "version": VERSION,
            "name": name,
            "fetched_at": fetched_at,
            "last_day": last_day,
            "source": source,
            "url": url,
            "payload": payload,
        }
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：进程在写的中途没掉，不会留下半截 JSON 让下一次启动读不出。
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)
        return path


def fresh(stamp: str | None, now: datetime | None = None) -> bool:
    """缓存那次取数还在本次的同一根收盘轴里吗。时间戳读不出来就按不新。"""
    at = freshness.parse(stamp)
    if at is None:
        return False
    return freshness.latest_close(at) == freshness.latest_close(now or datetime.now(timezone.utc))


def last_day_of(series_days: list[str]) -> str:
    """几条序列里最靠后的那个观测日；空序列给空串，`save` 据此拒绝落盘。"""
    days = [day for day in series_days if day]
    return max(days) if days else ""
