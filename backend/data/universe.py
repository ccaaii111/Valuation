"""A 全市场 code→name 索引：搜索页用它，取数前也用它问一句「这只票还在不在市」.

This file is **not** a data source for valuations. Nothing under ``backend/`` may import it to obtain a
number that ends up in a model: 估值用的每一个数都必须在 ``runs/<code>/<批次>/raw/`` 里留下当次联网的原始
响应，而本索引会被复用七天、会被人工刷新，复现不了任何一次取数。它回答的是「这只票叫什么、代码是多少」，
不回答「这只票值多少钱」。唯一的例外是 ``listed()``：它只回答准入（在不在市），产出的是一句拒绝与一次
不联网，不是任何一个进模型的数。

Why an index at all: 搜索要在几百毫秒内对 5000+ 只票做「代码前缀 / 名称包含 / 简拼」三类匹配，
每次按键都回服务端取数不现实，所以启动时一次拉全、落成 JSON、让浏览器整份拿在手里即时过滤。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_PATH = ROOT / "data" / "universe.json"
DEFAULT_MAX_AGE_DAYS = 7
CODE_RE = re.compile(r"\d{6}")


class UniverseUnavailable(RuntimeError):
    """The index could not be built — 搜索页没有可用清单时给出可操作的解释，而不是空列表。"""


# 板块按代码首位划分；blocked 的那几类搜索引擎会照列但标灰，因为取数链路目前走不通，
# 让用户搜到「查无此票」比搜到「知道有、但这里算不了」更难解释。
BOARDS: tuple[tuple[str, str, str], ...] = (
    ("688", "科创板", ""),
    ("605", "沪市主板", ""),
    ("60", "沪市主板", ""),
    ("900", "沪市B股", "B 股计价结算口径不同，未接通"),
    ("001", "深市主板", ""),
    ("002", "深市主板", ""),
    ("003", "深市主板", ""),
    ("00", "深市主板", ""),
    ("301", "创业板", ""),
    ("30", "创业板", ""),
    ("200", "深市B股", "B 股计价结算口径不同，未接通"),
    ("920", "北交所", "北交所暂未接通：行情与三表端点走的是沪深通道"),
    ("8", "北交所", "北交所暂未接通：行情与三表端点走的是沪深通道"),
    ("4", "北交所", "北交所暂未接通：行情与三表端点走的是沪深通道"),
)


@dataclass
class Entry:
    code: str
    name: str
    py: str  # 简拼：贵州茅台 → gzmt
    full: str  # 全拼：贵州茅台 → guizhoumaotai
    board: str
    blocked: str = ""

    def as_row(self) -> dict[str, str]:
        return asdict(self)


def board_of(code: str) -> tuple[str, str]:
    """(板块, 未接通原因)。未知首位仍照列，只是说不出板块。"""
    for prefix, board, blocked in BOARDS:
        if code.startswith(prefix):
            return board, blocked
    return "未识别", ""


def pinyin(name: str) -> tuple[str, str]:
    """(简拼, 全拼) for a Chinese stock name; 非汉字字符原样保留在小写全文里。

    pypinyin 是软依赖：没有它搜索只是失去拼音这条通路，代码和名称照样能搜，所以降级而不是报错。
    """
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return "", ""
    syllables = lazy_pinyin(name, style=Style.FIRST_LETTER, errors="default")
    full = lazy_pinyin(name, style=Style.NORMAL, errors="default")
    joined = "".join(syllables).lower()
    return re.sub(r"[^a-z0-9]", "", joined), re.sub(r"[^a-z0-9]", "", "".join(full).lower())


def normalize(rows: list[tuple[str, str]]) -> list[Entry]:
    """Turn raw (code, name) pairs into searchable entries — sorted by code, so the file diffs cleanly."""
    out = []
    seen: set[str] = set()
    for code, name in rows:
        code = re.sub(r"\D", "", str(code))
        # 名录把名称按定宽补了空格（"五 粮 液"、"万 科Ａ"，实测 36 只），留着的话按名称搜就搜不到它们。
        name = re.sub(r"\s+", "", str(name))
        if not CODE_RE.fullmatch(code) or not name or code in seen:
            continue
        seen.add(code)
        board, blocked = board_of(code)
        short, full = pinyin(name)
        out.append(Entry(code=code, name=name, py=short, full=full, board=board, blocked=blocked))
    return sorted(out, key=lambda e: e.code)


def fetch_rows() -> list[tuple[str, str]]:
    """The whole A-share roster from akshare. 一次调用 18 个分页请求，约 30~60 秒。"""
    try:
        import akshare
    except ImportError as exc:
        raise UniverseUnavailable(f"akshare 未安装: {exc}") from None
    from contextlib import redirect_stderr
    from io import StringIO

    try:
        # akshare 往 stderr 吐进度条，启动脚本里会盖掉「索引已更新」那行。
        with redirect_stderr(StringIO()):
            frame = akshare.stock_info_a_code_name()
    except Exception as exc:  # noqa: BLE001 - akshare surfaces requests/parse errors from many endpoints
        raise UniverseUnavailable(f"{type(exc).__name__}: {exc}") from None

    columns = list(getattr(frame, "columns", []))
    if "code" not in columns or "name" not in columns:
        raise UniverseUnavailable(f"stock_info_a_code_name() 未返回 code/name 列，实际列: {columns[:8]}")
    return [(str(c), str(n)) for c, n in frame[["code", "name"]].itertuples(index=False, name=None)]


def build(*, rows: list[tuple[str, str]] | None = None, source: str = "akshare stock_info_a_code_name") -> dict:
    entries = normalize(rows if rows is not None else fetch_rows())
    if not entries:
        raise UniverseUnavailable("名录为空：一个可用的代码／名称对都没拿到，不写索引文件")
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "url": "https://www.sse.com.cn / www.szse.cn（经 akshare 汇总，免登录）",
        "note": "搜索索引；取数前只借它问一句「这只票还在不在市」，进模型的每一个数仍照旧记在 runs/<代码>/<批次>/raw/ 里。",
        "total": len(entries),
        "entries": [e.as_row() for e in entries],
    }


def write(document: dict, path: Path | None = None) -> Path:
    # 默认路径晚绑定：测试靠替换 UNIVERSE_PATH 把索引写到临时目录，写死在函数签名里就换不掉。
    path = path or UNIVERSE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再改名：进程被 Ctrl+C 打断不会留下半截 JSON，下次启动也就不会被「文件存在但读不出」卡住。
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)
    return path


def read(path: Path | None = None) -> dict | None:
    path = path or UNIVERSE_PATH
    if not path.is_file():
        return None
    # 这里不缓存：5574 行整份解析实测 5.6 毫秒，问一次「这只票在不在市」的代价，比留一份可能过期的副本便宜。
    # 副本会说话：刚重拉完的名录要是还按昨天那一份答复，准入判断就是拿一份旧证据在拒绝一只票。
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 索引坏了不等于没索引：让调用方按「没有索引」处理并提示重建，比整个面板起不来好。
        print(f"股票索引读不出（{exc}）：请跑 python scripts/update_universe.py 重建 {path}")
        return None
    if not isinstance(document, dict) or not document.get("entries"):
        return None
    return document


def age_days(document: dict, now: datetime | None = None) -> float:
    """How stale the index is, in days — the number behind 「索引 X 天未更新」."""
    try:
        generated = datetime.strptime(str(document["generated_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, ValueError):
        return 1e9
    return (((now or datetime.now(timezone.utc)) - generated).total_seconds() / 86400.0)


def listed(
    code: str,
    *,
    path: Path | None = None,
    now: datetime | None = None,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
) -> tuple[bool | None, str]:
    """取数前的准入判断：(在不在市, 依据)。索引是唯一证据，所以它答不了时返回 ``None`` 而不是「查无此人」。

    None 有两条：盘上没有索引、或索引旧过 `max_age_days`（昨天刚上市的票会被误杀）。两种都按「不知道」
    交给真正的取数去回答——与「批次缺行情凭证时不声称知道它用过哪个基准」同一条纪律：没有证据就不下结论。
    """
    document = read(path)
    if document is None:
        return None, "盘上没有可用的股票索引"
    age = age_days(document, now)
    if age > max_age_days:
        return None, f"名录已 {age:.0f} 天未刷新，超过 {max_age_days:g} 天"
    entries = document.get("entries") or []
    for row in entries:
        if str(row.get("code")) == code:
            return True, f"{len(entries)} 只名录里的 {row.get('name')}"
    return False, f"{len(entries)} 只名录（{str(document.get('generated_at'))[:10]}）里没有它"


def refresh(
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    *,
    force: bool = False,
    path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict, str]:
    """Return (index document, status) where status is fresh|updated|stale|rebuilt.

    path/now 可注入，为的是这四条分支能在离线测试里走一遍——真去拉一次全名录要一分钟，等不起。

    fresh   索引还新，直接用
    updated 过期了，重新拉到了
    stale   想重拉但失败了，只能用旧的——调用方要把天数摆到页面上
    rebuilt 磁盘上根本没有索引，且这次拉成功（没有退路的冷启动）
    """
    path = path or UNIVERSE_PATH
    existing = read(path)
    if existing and not force and age_days(existing, now) <= max_age_days:
        return existing, "fresh"
    try:
        document = build()
    except UniverseUnavailable:
        if existing:
            return existing, "stale"
        raise
    write(document, path)
    return document, ("updated" if existing else "rebuilt")
