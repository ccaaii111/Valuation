"""分红方案：第 10 个来源，也是唯一一个「缺了不拦路」的来源。

东财「个股分红」页（akshare `stock_fhps_detail_em`）免登录、按报告期给出每 10 股派息、每股收益、总股本与
方案进度，够把某一年实际派下的现金红利配到那一年的年报每股收益上——级联第四档（Higgins 的
g = (1−PO)·ROE）里那个 PO 只能从这里量出来，一致预期与三表都没有它。

它失败时不抛错，也不进 `freshness` 的 expected 清单（用户 2026-10-16 定：缺凭证只作废这一档）。理由是这一档
只是对照值：老批次的证据不该因为多了一行参考数就集体判旧，分红缺了估值照跑。
用户 2026-10-22 又定了后半句：**查不到分红数据就当作这只票没有分红**，不区分「源答没有记录」与「这一趟没取到」，
所以失败分支也只落 `NO_RECORD` 那一句，原始报错串不进面板（它会把人引去点「强制重取」，而这一行重取多少次都一样）。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from backend.data import prices
from backend.data.transport import Fetched, now_iso

RAW_NAME = "dividends"
FUNC = "stock_fhps_detail_em"
SOURCE_URL = "https://data.eastmoney.com/yjfp/"
# 方案进度里出现这两个字才算钱真派下去了：「预披露」「股东大会通过」「决案」都还只是许诺。
IMPLEMENTED = "实施"
ANNUAL_MD = "12-31"
# 空表与取数失败共用这一句：面板那一行只说「查不到」，不替后来的人猜是哪种。
NO_RECORD = "东财个股分红页查不到这只票的现金分红记录：这一档按无分红处理"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # NaN 既不等于自身也不能进 JSON 凭证，一律当缺数。
        return number if number == number else None
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if text in ("NaT", "nan", "None") else text[:10]


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    """一行原始记录 → 可进 JSON 的形状：日期收成 ISO 文本，NaN/NaT 收成 None，数字收成 float。

    非数字的文本列（「10转1.00派6.00元(含税…)」这类方案描述）原样留着——它是凭证的一部分，替后来的人
    说明那一年到底派了什么。
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        text = str(value)
        if value is None or text in ("NaT", "nan", "None", ""):
            out[key] = None
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
            out[key] = text[:10]
        else:
            out[key] = text if _number(value) is None else _number(value)
    return out


def _fetched(payload: dict[str, Any]) -> Fetched:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return Fetched(
        name=RAW_NAME,
        url=payload["url"],
        fetched_at=now_iso(),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        payload=payload,
    )


def fetch(code: str) -> Fetched:
    """取这只票的分红送配明细全表；取不到就按「这家公司没有分红数据」落盘，绝不让建链因此失败。

    存的是整张表而不是算好的比率：凭证要回答「从哪儿取到了什么」，读法改了（比如窗口从 5 年换 7 年）
    不必重新联网。
    """
    try:
        frame = prices.fetch_frame(FUNC, symbol=str(code).strip())
    except prices.PricesUnavailable as exc:
        frame, error = None, str(exc)[:200]
    else:
        frame, error = frame, ""
    rows = [_clean(record) for record in frame.to_dict("records")] if frame is not None else []
    return _fetched(
        {
            "source": f"akshare {FUNC}",
            "url": SOURCE_URL,
            "rows": rows,
            "unavailable": "" if rows else NO_RECORD,
            # 原始报错只留在盘上给修数据的人看：读法不碰这个键，所以面板永远只看到上面那一句。
            "fetch_error": error,
        }
    )


def _year(date_text: Any) -> int | None:
    head = _text(date_text)
    return int(head[:4]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head) else None


def _is_annual(date_text: Any) -> bool:
    return _text(date_text)[5:] == ANNUAL_MD


def payouts(payload: dict | None) -> dict:
    """财年 → 这一年度实际派下的现金红利 ÷ 年报每股收益。

    三条读法各拦一类错数，缺一条就得到看起来很离谱的分红率：

    - 分子按财年求和，且每 10 股派 X 元要先除以 10：2024 年之后中期分红常态化，一年两笔是常态
      （茅台 2024 是三季报那笔 238.82 加年报 276.73），只认年报那一行会把半个财年的钱整段漏掉。
    - 分母只取年报行的每股收益：中报、三季报的每股收益是年初至今累计口径，把它跟同年那笔中期分红配对，
      算出来的是「半年赚的钱派了一整年」这种 200% 上下的数。
    - 只认「实施」：预披露与刚过股东大会的方案钱还没离开公司，算进分红率等于替公司提前许诺。

    年报还没披露、只派过中期那一笔的年份会因缺分母落到 `skipped`，而不是被当成一年零分红——那是
    「读不出来」，不是「没分红」。
    """
    rows = (payload or {}).get("rows") or []
    if not rows:
        # 有这张表但一行都没有＝查不到分红数据；压根没有这张表＝这批没取过分红。
        return {
            "available": False,
            "unavailable": NO_RECORD if payload else "这批没有分红凭证",
            "years": {},
            "skipped": [],
        }
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        year = _year(row.get("报告期"))
        if year:
            grouped.setdefault(year, []).append(row)
    years: dict[int, dict] = {}
    skipped: list[dict] = []
    for year in sorted(grouped, reverse=True):
        group = grouped[year]
        paid = [
            row
            for row in group
            if IMPLEMENTED in str(row.get("方案进度") or "")
            and _number(row.get("现金分红-现金分红比例")) is not None
            and _number(row.get("现金分红-现金分红比例")) > 0
        ]
        if not paid:
            if any(_number(row.get("现金分红-现金分红比例")) for row in group):
                skipped.append({"year": year, "reason": "有分红方案但未实施"})
            continue
        annual = next((row for row in group if _is_annual(row.get("报告期"))), None)
        eps = _number((annual or {}).get("每股收益"))
        if not eps or eps <= 0:
            skipped.append({"year": year, "reason": "缺年报每股收益，分母对不上"})
            continue
        per_ten = [_number(row.get("现金分红-现金分红比例")) or 0.0 for row in paid]
        dps = sum(per_ten) / 10.0
        shares = [_number(row.get("总股本")) or 0.0 for row in paid]
        payout = dps / eps
        years[year] = {
            "payout": payout,
            "dps": dps,
            "eps": eps,
            "amount": sum(rate / 10.0 * share for rate, share in zip(per_ten, shares)) or None,
            "plans": len(paid),
            # 派发超过当年全部每股收益：特别分红或分红挂在上年利润上，格力的 2020 年就是 107.8%。
            "special": payout > 1.0,
        }
    return {
        "available": bool(years),
        "unavailable": "" if years else "没有已实施的现金分红方案",
        "years": years,
        "skipped": skipped,
    }
