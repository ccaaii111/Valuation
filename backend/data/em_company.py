"""Eastmoney public endpoints for A-share company fundamentals."""
from __future__ import annotations

import re
from typing import Any, Callable

from backend.data import universe
from backend.data.transport import Fetched, Transport, now_iso

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CONSENSUS = "https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax"
PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q"

MAIN = "RPT_F10_FINANCE_MAINFINADATA"
BALANCE = "RPT_F10_FINANCE_GBALANCE"
INCOME = "RPT_F10_FINANCE_GINCOME"
CASHFLOW = "RPT_F10_FINANCE_GCASHFLOW"

F10_REFERER = {"Referer": "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html"}

QUOTE_FIELDS = "f43,f44,f45,f57,f58,f84,f85,f92,f116,f117,f162,f167,f173"

# fltt=2 asks push2 for decimal values; the default payload returns prices scaled by 100.
QUOTE_PARAMS = {"fields": QUOTE_FIELDS, "fltt": 2, "invt": 2}


class Ticker:
    def __init__(self, code: str):
        raw = code.strip().upper().replace("SH", "").replace("SZ", "").replace(".", "")
        raw = re.sub(r"^(SH|SZ|BJ)", "", raw)
        if not re.fullmatch(r"\d{6}", raw):
            raise ValueError(f"无法识别的证券代码: {code}")
        self.code = raw
        # 板块表只借 universe 的前缀判据（那是纯字符串规则，不读索引文件），免得两张号段表各写一份、
        # 日后一处改了另一处不知道。它在这里只回答「这条路走不走得通」，不回答任何一个进模型的数。
        board, blocked = universe.board_of(raw)
        if blocked:
            raise NotImplementedError(f"{raw}（{board}）取数链路未接通：{blocked}")
        if raw[0] in ("6", "9"):
            self.suffix, self.secucode, self.em_code, self.secid = "SH", f"{raw}.SH", f"SH{raw}", f"1.{raw}"
        elif raw[0] in ("0", "2", "3"):
            self.suffix, self.secucode, self.em_code, self.secid = "SZ", f"{raw}.SZ", f"SZ{raw}", f"0.{raw}"
        else:
            raise NotImplementedError(
                f"{raw} 的首位不属于沪深两所已接通的号段（沪 6/9 开头、深 0/2/3 开头），"
                "这只票多半还没上市或代码写错了"
            )


def _report(transport: Transport, name: str, report_name: str, secucode: str, page_size: int) -> Fetched:
    return transport.get_json(
        name,
        DATACENTER,
        params={
            "reportName": report_name,
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": 1,
            "pageSize": page_size,
            "sortTypes": -1,
            "sortColumns": "REPORT_DATE",
        },
        headers=F10_REFERER,
    )


def rows_of(payload: Any) -> list[dict]:
    result = (payload or {}).get("result") or {}
    rows = result.get("data") or []
    deduped: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("REPORT_DATE"))[:10]
        deduped.setdefault(key, row)
    return sorted(deduped.values(), key=lambda r: str(r.get("REPORT_DATE")), reverse=True)


def fetch_quote(transport: Transport, ticker: Ticker) -> Fetched:
    """push2 carries shares and market cap; it rate-limits hard, so degrade to a text quote source."""
    try:
        return transport.get_json(
            "quote",
            PUSH2,
            params={"secid": ticker.secid, **QUOTE_PARAMS},
            headers={"Referer": "https://quote.eastmoney.com/"},
            attempts=1,
        )
    except Exception:  # noqa: BLE001 - any push2 failure routes to the fallback
        pass
    try:
        item = transport.get_text(
            "quote",
            TENCENT_QUOTE,
            params={"q": f"{ticker.suffix.lower()}{ticker.code}"},
            encoding="gbk",
            attempts=2,
        )
        return Fetched(
            name="quote",
            url=item.url,
            fetched_at=item.fetched_at,
            sha256=item.sha256,
            payload={"fallback": "tencent", "text": item.payload["text"]},  # type: ignore[index]
        )
    except Exception as exc:  # noqa: BLE001 - price is optional for the ladder, it only blocks WACC weights
        return Fetched(
            name="quote",
            url="",
            fetched_at=now_iso(),
            sha256="",
            payload={"unavailable": str(exc)[:200]},
        )


def fetch_all(
    transport: Transport, ticker: Ticker, history_pages: int = 200, progress: Callable[[str], None] | None = None
) -> list[Fetched]:
    """Six fixed-order calls. `progress(name)` fires before each one so a caller can show what is loading."""
    say: Callable[[str], None] = progress or (lambda name: None)
    calls = [
        ("main_indicators", lambda: _report(transport, "main_indicators", MAIN, ticker.secucode, history_pages)),
        ("balance_sheet", lambda: _report(transport, "balance_sheet", BALANCE, ticker.secucode, history_pages)),
        ("income_statement", lambda: _report(transport, "income_statement", INCOME, ticker.secucode, history_pages)),
        ("cash_flow", lambda: _report(transport, "cash_flow", CASHFLOW, ticker.secucode, history_pages)),
        (
            "consensus",
            lambda: transport.get_json(
                "consensus", CONSENSUS, params={"code": ticker.em_code}, headers=F10_REFERER
            ),
        ),
        ("quote", lambda: fetch_quote(transport, ticker)),
    ]
    items = []
    for name, call in calls:
        say(name)
        items.append(call())
    return items
