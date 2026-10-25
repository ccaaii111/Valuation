"""Single-row sensitivity attribution for the assumption ladder.

For every row the panel needs three things side by side: how far the human moved it from the
machine baseline, and what the valuation would look like if only that one row were reverted.
"""
from __future__ import annotations

from typing import Any

from backend.model.assumptions import DERIVES, MONEY_UNITS
from backend.model.dcf import (
    DERIVED_INPUTS,
    ENGINE_INPUTS,
    apply_overrides,
    discount_rates,
    evaluate,
    inputs_from_ladder,
    valuation_bridge,
)


def flat_rows(ladder: dict) -> list[dict]:
    """摊平假设链，并把「这一行浏览器能不能改、用什么控件」一次算好。

    面板与推导图共用这份判断：两处都能改数，判据分叉就会出现「图上能改、面板里是锁的」这种假 bug。
    """
    rows: list[dict] = []
    for group in ladder.get("groups", []):
        for row in group.get("rows", []):
            # Baselines written before the edit field existed carry no preference; the unit encodes the
            # same rule, so recover it instead of downgrading every stored batch to absolute boxes.
            # derives 同理：老批次没这一项，但上游关系是表的属性而非批次的属性，回填后推导图不必重跑取数。
            rows.append(
                {
                    **row,
                    "group": group["id"],
                    "group_title": group["title"],
                    "edit": row.get("edit") or ("delta" if row.get("unit") in MONEY_UNITS else "value"),
                    "derives": list(row.get("derives") or DERIVES.get(row["key"], ())),
                    "editable": row["key"] in ENGINE_INPUTS,
                    "derived": row["key"] in DERIVED_INPUTS,
                    "value_type": value_type(row["baseline"]),
                }
            )
    return rows


def value_type(value: Any) -> str:
    """Which control the panel should render, read off the value itself rather than the unit string."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "year_map"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def _change_pct(baseline: Any, current: Any) -> Any:
    if isinstance(baseline, dict) and isinstance(current, dict):
        out = {}
        for year, base in baseline.items():
            now = current.get(year)
            if isinstance(base, (int, float)) and isinstance(now, (int, float)) and base:
                out[str(year)] = round(now / base - 1.0, 6)
        return out or None
    if isinstance(baseline, bool) or isinstance(current, bool):
        return None
    if isinstance(baseline, (int, float)) and isinstance(current, (int, float)) and baseline:
        return round(current / baseline - 1.0, 6)
    return None


def attribute(
    ladder: dict,
    overrides: dict[str, Any] | None = None,
    method: str = "fcff",
    terminal: str = "gordon",
    mid_year: bool | None = None,
    rate: float | None = None,
) -> dict:
    overrides = overrides or {}
    baseline_inputs = inputs_from_ladder(ladder)
    current_inputs = apply_overrides(baseline_inputs, overrides)

    baseline_run = evaluate(baseline_inputs, method, terminal, rate=rate, mid_year=mid_year)
    current_run = evaluate(current_inputs, method, terminal, rate=rate, mid_year=mid_year)
    per_share = current_run.per_share

    base_equity, base_wacc = discount_rates(baseline_inputs)
    curr_equity, curr_wacc = discount_rates(current_inputs)

    rows: list[dict] = []
    ladder_keys: set[str] = set()
    for row in flat_rows(ladder):
        key = row["key"]
        ladder_keys.add(key)
        stored = current_inputs.get(key, row["baseline"])
        touched = key in overrides
        # 「改过」只说人工点过的那一行。级联行是自己挪的（上游被改了、`recompute` 重导过它），
        # 把它算成人的改动会双计贡献，还会让不可编辑的显示行混进「未定价的改动」。
        changed = touched and stored != row["baseline"]
        current_value = stored
        reference = row["baseline"]
        # 级联那两行比的是「引擎在基线上折出来的值」，不是存进批次的那个数：存档按 6 位取整后，用取整
        # 过的行反算往往差最后一位，零改动也会挂上牌、改动列还会读出 "-0.00%"——那是存档的舍入，不是改动。
        baseline_derived = _round(base_wacc if key == "wacc" else base_equity, 6)
        if key in DERIVED_INPUTS:
            # The stored row is only where the cascade started; printing it unchanged would let the panel
            # show a rate the engine never used. Rounded like build_ladder rounds it, so a matching
            # baseline does not read as an edit.
            derived_now = _round(curr_wacc if key == "wacc" else curr_equity, 6)
            current_value = stored if derived_now is None else derived_now
            reference = baseline_derived if baseline_derived is not None else row["baseline"]
        # 动了但没被人工点过的行＝被波及的派生行；面板与推导图用它挂「级联」的牌。
        recomputed = (not touched and stored != row["baseline"]) or (
            key in DERIVED_INPUTS and current_value != reference
        )
        entry = {
            "key": key,
            "label": row["label"],
            "unit": row["unit"],
            "group": row["group"],
            "group_title": row["group_title"],
            "kind": row.get("kind"),
            "source": row.get("source"),
            "confidence": row.get("confidence"),
            "note": row.get("note"),
            # 老批次的 note 还没分家（数字混在说明里），这里只补一个空字段，不去猜哪半是凭证。
            "evidence": row.get("evidence") or "",
            "baseline": row["baseline"],
            "current": current_value,
            "changed": changed,
            "recomputed": recomputed,
            "editable": row["editable"],
            "derived": row["derived"],
            "value_type": row["value_type"],
            "edit": row["edit"],
            "derives": row["derives"],
            "change_pct": _change_pct(reference, current_value),
            "contribution_pct": None,
            "value_if_reverted": None,
            "rate_if_reverted": None,
        }
        if changed and key in ENGINE_INPUTS and per_share:
            # Revert through the same merge the panel uses, so dropping one row also drops whatever that
            # row had re-derived. Patching the flat dict left a half state (rate back to baseline while
            # the growth path it had pulled stayed moved), which made the attribution columns incomparable.
            reverted_inputs = apply_overrides(
                baseline_inputs, {k: v for k, v in overrides.items() if k != key}
            )
            reverted = evaluate(reverted_inputs, method, terminal, rate=rate, mid_year=mid_year)
            if reverted.per_share is not None and baseline_run.per_share:
                entry["value_if_reverted"] = round(reverted.per_share, 4)
                # The rate this row implied before the edit: for rf/β/ERP/税率 the cascade shows up here,
                # which is the difference between "this row moved the discount rate" and "it moved nothing".
                entry["rate_if_reverted"] = _round(reverted.rate, 6)
                # Denominator is the BASELINE per-share, so a single edit's attribution equals the total
                # change the panel shows, and the two columns are directly comparable.
                entry["contribution_pct"] = round((per_share - reverted.per_share) / baseline_run.per_share, 6)
        rows.append(entry)

    edited = [r for r in rows if r["changed"]]
    return {
        "method": method,
        "terminal": terminal,
        "mid_year": current_run.mid_year,
        "rate": current_run.rate,
        # Baseline vs current, so the panel can show the cascade rather than infer it from the rows.
        "rates": {
            "cost_of_equity": [_round(base_equity, 6), _round(curr_equity, 6)],
            "wacc": [_round(base_wacc, 6), _round(curr_wacc, 6)],
        },
        "baseline_per_share": _round(baseline_run.per_share),
        "per_share": _round(per_share),
        "total_change_pct": (
            _round(per_share / baseline_run.per_share - 1.0, 6) if (baseline_run.per_share and per_share) else None
        ),
        "edited_row_count": len(edited),
        # Single-row sensitivities are not additive, so this sum will not reconcile with the total.
        "contribution_sum_pct": _round(sum(r["contribution_pct"] or 0.0 for r in edited), 6) if edited else None,
        "unpriced_edits": [r["key"] for r in edited if not r["editable"]],
        "rows": rows,
        "schedule": [line.to_json() for line in current_run.lines],
        # 引擎最后几步（营收→EBIT→NOPAT→FCFF→现值→终值→EV→股权→每股）。值读自上面这一次 run，
        # 面板与推导图共用同一份，谁也不许为了画图再跑一遍引擎。
        "valuation": valuation_bridge(current_run, ladder_keys),
        "warnings": current_run.warnings,
        "baseline_warnings": baseline_run.warnings,
    }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)
