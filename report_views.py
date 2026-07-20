"""Build org-level drill-down views from billing baseline data."""

from __future__ import annotations


def _account_summary_row(account_id: str, acct: dict, periods: dict) -> dict | None:
    if acct.get("error"):
        return None

    last_days = periods["last_month"]["days"]
    mtd_days = periods["current_mtd"]["days_elapsed"]
    days_in_month = periods["current_mtd"]["days_in_month"]

    last_total = float(acct.get("last_month", {}).get("total_usd", 0) or 0)
    mtd_total = float(acct.get("mtd", {}).get("total_usd", 0) or 0)
    last_daily = last_total / last_days if last_days else 0.0
    mtd_daily = mtd_total / mtd_days if mtd_days else 0.0
    delta_daily = mtd_daily - last_daily
    forecast = float(acct.get("forecast_month_end_usd", 0) or 0)
    pct = ((mtd_daily - last_daily) / last_daily * 100.0) if last_daily > 0 else None
    forecast_pct = ((forecast - last_total) / last_total * 100.0) if last_total > 0 else None
    forecast_delta = forecast - last_total

    if abs(delta_daily) < 0.001:
        trend = "flat"
    elif delta_daily > 0:
        trend = "up"
    else:
        trend = "down"

    if abs(forecast_delta) < 0.01:
        forecast_trend = "flat"
    elif forecast_delta > 0:
        forecast_trend = "up"
    else:
        forecast_trend = "down"

    return {
        "account_id": account_id,
        "account_name": acct.get("name", account_id),
        "last_month_total_usd": round(last_total, 2),
        "last_month_daily_avg_usd": round(last_daily, 2),
        "mtd_total_usd": round(mtd_total, 2),
        "mtd_daily_avg_usd": round(mtd_daily, 2),
        "delta_daily_usd": round(delta_daily, 2),
        "pct_change_daily": round(pct, 1) if pct is not None else None,
        "forecast_month_end_usd": round(forecast, 2),
        "forecast_vs_last_month_pct": round(forecast_pct, 1) if forecast_pct is not None else None,
        "forecast_trend": forecast_trend,
        "trend": trend,
    }


def build_account_comparison(baseline: dict) -> list[dict]:
    periods = baseline.get("periods", {})
    rows: list[dict] = []
    for account_id, acct in baseline.get("by_account", {}).items():
        row = _account_summary_row(account_id, acct, periods)
        if row is None:
            continue
        if row["last_month_total_usd"] + row["mtd_total_usd"] < 0.01:
            continue
        rows.append(row)
    rows.sort(key=lambda r: abs(float(r.get("delta_daily_usd", 0) or 0)), reverse=True)
    return rows


def build_account_service_breakdown(baseline: dict) -> dict[str, list[dict]]:
    breakdown: dict[str, list[dict]] = {}
    for account_id, acct in baseline.get("by_account", {}).items():
        if acct.get("error"):
            continue
        services: list[dict] = []
        for row in acct.get("comparison", []):
            last_total = float(row.get("last_month_total_usd", 0) or 0)
            mtd_total = float(row.get("mtd_total_usd", 0) or 0)
            if last_total + mtd_total < 0.01:
                continue
            services.append(dict(row))
        services.sort(
            key=lambda r: abs(float(r.get("delta_daily_usd", 0) or 0)),
            reverse=True,
        )
        if services:
            breakdown[account_id] = services
    return breakdown


def build_service_account_breakdown(baseline: dict) -> dict[str, list[dict]]:
    breakdown: dict[str, list[dict]] = {}
    for account_id, acct in baseline.get("by_account", {}).items():
        if acct.get("error"):
            continue
        account_name = acct.get("name", account_id)
        for row in acct.get("comparison", []):
            service = row.get("service")
            if not service:
                continue
            last_total = float(row.get("last_month_total_usd", 0) or 0)
            mtd_total = float(row.get("mtd_total_usd", 0) or 0)
            if last_total + mtd_total < 0.01:
                continue
            breakdown.setdefault(service, []).append(
                {
                    "account_id": account_id,
                    "account_name": account_name,
                    **{k: v for k, v in row.items() if k != "service"},
                }
            )
    for service in breakdown:
        breakdown[service].sort(
            key=lambda r: abs(float(r.get("delta_daily_usd", 0) or 0)),
            reverse=True,
        )
    return breakdown


def attach_linked_accounts_view(baseline: dict) -> dict:
    """Add precomputed org drill-down views to baseline (in place)."""
    baseline["linked_accounts_view"] = {
        "account_comparison": build_account_comparison(baseline),
        "account_service_breakdown": build_account_service_breakdown(baseline),
        "service_account_breakdown": build_service_account_breakdown(baseline),
    }
    return baseline


def linked_accounts_view(baseline: dict) -> dict:
    view = baseline.get("linked_accounts_view")
    if view:
        return view
    return {
        "account_comparison": build_account_comparison(baseline),
        "account_service_breakdown": build_account_service_breakdown(baseline),
        "service_account_breakdown": build_service_account_breakdown(baseline),
    }
