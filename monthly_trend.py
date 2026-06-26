"""Build six-month historical + current-month projected billing trend series."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

from config import PROFILES

MONTHLY_HISTORY_COUNT = 6
PRIMARY_METRIC = "NetAmortizedCost"
FALLBACK_METRIC = "AmortizedCost"
LINKED_PRIMARY_METRIC = "AmortizedCost"
LINKED_FALLBACK_METRIC = "UnblendedCost"


def _metrics_for_scope(linked_account_id: str | None) -> tuple[str, str]:
    if linked_account_id:
        return LINKED_PRIMARY_METRIC, LINKED_FALLBACK_METRIC
    return PRIMARY_METRIC, FALLBACK_METRIC


def _metric_amount(metrics: dict, primary: str, fallback: str) -> float:
    if primary in metrics:
        return float(metrics[primary]["Amount"])
    if fallback in metrics:
        return float(metrics[fallback]["Amount"])
    return 0.0


def historical_month_ranges(today: date, count: int = MONTHLY_HISTORY_COUNT) -> list[tuple[date, date, str]]:
    """Full calendar months before the current month: (start, end_exclusive, yyyymm)."""
    y = today.year
    m = today.month
    ranges: list[tuple[date, date, str]] = []
    for _ in range(count):
        m -= 1
        if m < 1:
            m = 12
            y -= 1
        start = date(y, m, 1)
        end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        ranges.append((start, end, start.strftime("%Y%m")))
    ranges.reverse()
    return ranges


def fetch_monthly_totals(
    ce_client,
    ranges: list[tuple[date, date, str]],
    *,
    linked_account_id: str | None = None,
) -> dict[str, float]:
    if not ranges:
        return {}

    start = ranges[0][0]
    end = ranges[-1][1]
    primary, fallback = _metrics_for_scope(linked_account_id)

    filt_parts: list[dict] = []
    if linked_account_id:
        filt_parts.append(
            {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [linked_account_id]}}
        )
    filt = None
    if filt_parts:
        filt = filt_parts[0] if len(filt_parts) == 1 else {"And": filt_parts}

    totals: dict[str, float] = {}
    next_token = None
    while True:
        params: dict = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": [primary, fallback],
        }
        if filt:
            params["Filter"] = filt
        if next_token:
            params["NextPageToken"] = next_token

        resp = ce_client.get_cost_and_usage(**params)
        for period in resp.get("ResultsByTime", []):
            period_start = period.get("TimePeriod", {}).get("Start", "")
            if len(period_start) < 7:
                continue
            month_suffix = period_start[:4] + period_start[5:7]
            amount = _metric_amount(period.get("Total", {}), primary, fallback)
            totals[month_suffix] = totals.get(month_suffix, 0.0) + amount

        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    return totals


def _month_entry(
    month_suffix: str,
    label: str,
    total_usd: float,
    *,
    kind: str,
) -> dict:
    return {
        "month_suffix": month_suffix,
        "label": label,
        "total_usd": round(float(total_usd), 2),
        "kind": kind,
    }


def build_monthly_trend(ce_client, today: date, baseline: dict) -> dict:
    ranges = historical_month_ranges(today, MONTHLY_HISTORY_COUNT)
    org_totals = fetch_monthly_totals(ce_client, ranges)
    current_suffix = today.strftime("%Y%m")
    current_label = date(today.year, today.month, 1).strftime("%b %Y")

    org_months = [
        _month_entry(
            ms,
            start.strftime("%b %Y"),
            org_totals.get(ms, 0.0),
            kind="actual",
        )
        for start, _end, ms in ranges
    ]
    org_forecast = float(baseline.get("org", {}).get("forecast_month_end_usd", 0) or 0)
    org_months.append(
        _month_entry(current_suffix, current_label, org_forecast, kind="projected")
    )

    by_account: dict[str, dict] = {}
    for env_name, cfg in PROFILES.items():
        account_id = cfg["account"]
        acct = baseline.get("by_account", {}).get(account_id, {})
        if acct.get("error"):
            continue
        acct_totals = fetch_monthly_totals(
            ce_client, ranges, linked_account_id=account_id
        )
        months = [
            _month_entry(
                ms,
                start.strftime("%b %Y"),
                acct_totals.get(ms, 0.0),
                kind="actual",
            )
            for start, _end, ms in ranges
        ]
        forecast = float(acct.get("forecast_month_end_usd", 0) or 0)
        months.append(
            _month_entry(current_suffix, current_label, forecast, kind="projected")
        )
        by_account[account_id] = {
            "name": env_name,
            "account_id": account_id,
            "metric": LINKED_PRIMARY_METRIC,
            "months": months,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "month_suffix": current_suffix,
        "history_month_count": MONTHLY_HISTORY_COUNT,
        "org": {
            "metric": PRIMARY_METRIC,
            "months": org_months,
        },
        "by_account": by_account,
    }
