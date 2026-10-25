"""Assembles a fundamentals snapshot for a ticker, either live or replayed from a stored run."""
from __future__ import annotations

from typing import Any, Callable

from backend.data import dividends, em_company
from backend.data.em_company import Ticker
from backend.data.transport import RunStore, Transport
from backend.model import periods as P
from backend.model.assumptions import parse_consensus

RAW_NAMES = ("main_indicators", "balance_sheet", "income_statement", "cash_flow", "consensus", "quote", "dividends")


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_tencent(text: str) -> dict:
    # qt.gtimg.cn is positional: 1 name, 3 price, 39 PE(TTM), 44 流通市值(亿), 45 总市值(亿), 46 PB.
    parts = text.split("~")
    if len(parts) < 47:
        return {"available": False, "source": "tencent 字段不足"}
    price = _as_float(parts[3])
    total_cap = (_as_float(parts[45]) or 0.0) * 1e8
    return {
        "available": bool(price),
        "source": "tencent",
        "name": parts[1],
        "price": price,
        "total_market_cap": total_cap or None,
        "float_market_cap": (_as_float(parts[44]) or 0.0) * 1e8 or None,
        "total_shares": (total_cap / price) if (price and total_cap) else None,
        "pe_channel": _as_float(parts[39]),
        "pe_channel_basis": "腾讯第 39 位，滚动市盈率",
        "pb": _as_float(parts[46]),
    }


def parse_quote(payload: dict) -> dict:
    """市盈率只交原值：这一行不进 `pe_ttm`，因为两条通道的口径不一样，级联层要自己用市值 ÷ 归母净利 LTM 算。"""
    if not payload:
        return {"available": False, "source": "无"}
    if "fallback" in payload:
        parsed = _parse_tencent(str(payload.get("text") or ""))
        parsed["url_sha256"] = payload.get("sha256")
        return parsed
    if "unavailable" in payload:
        return {"available": False, "source": f"行情源均不可用: {payload['unavailable'][:80]}"}
    data = (payload or {}).get("data") or {}
    return {
        "available": data.get("f43") is not None,
        "source": "push2",
        "name": data.get("f58"),
        "price": _as_float(data.get("f43")),
        "total_shares": _as_float(data.get("f84")),
        "float_shares": _as_float(data.get("f85")),
        "total_market_cap": _as_float(data.get("f116")),
        "float_market_cap": _as_float(data.get("f117")),
        "bps": _as_float(data.get("f92")),
        "pe_channel": _as_float(data.get("f162")),
        "pe_channel_basis": "push2 f162，动态市盈率（最新中期归母按 12/月数 年化）",
        "pb": _as_float(data.get("f167")),
    }


def assemble(ticker: Ticker, payloads: dict[str, Any]) -> dict:
    balance = P.sort_desc(em_company.rows_of(payloads.get("balance_sheet")))
    income = P.sort_desc(em_company.rows_of(payloads.get("income_statement")))
    cashflow = P.sort_desc(em_company.rows_of(payloads.get("cash_flow")))
    main = P.sort_desc(em_company.rows_of(payloads.get("main_indicators")))
    heads = [P.report_date(r) for r in (P.latest(balance), P.latest(income), P.latest(cashflow)) if r]
    return {
        "code": ticker.code,
        "secucode": ticker.secucode,
        "as_of": max(heads) if heads else "",
        "balance": balance,
        "income": income,
        "cashflow": cashflow,
        "main": main,
        "consensus": parse_consensus(payloads.get("consensus")),
        "quote": parse_quote(payloads.get("quote")),
        "dividends": dividends.payouts(payloads.get("dividends")),
    }


def fetch(
    ticker: Ticker, store: RunStore, transport: Transport, progress: Callable[[str], None] | None = None
) -> dict:
    items = em_company.fetch_all(transport, ticker, progress=progress)
    for item in items:
        store.record(item)
    # 分红走 akshare 而不是东财 REST 通道，所以接在这六笔之后，不进 em_company。
    # 它失败也只在自己身上失败：`fetch` 永不抛错，缺凭证由级联那一行自己说明。
    if progress:
        progress("dividends")
    dividend_item = dividends.fetch(ticker.code)
    store.record(dividend_item)
    items.append(dividend_item)
    return assemble(ticker, {item.name: item.payload for item in items})


def replay(ticker: Ticker, store: RunStore) -> dict:
    return assemble(ticker, {name: store.load(name) for name in RAW_NAMES})
