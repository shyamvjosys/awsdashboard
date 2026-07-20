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


def fetch_monthly_totals_by_service(
    ce_client,
    ranges: list[tuple[date, date, str]],
    *,
    linked_account_id: str | None = None,
) -> dict[str, dict[str, float]]:
    """Return {service: {yyyymm: amount}} for closed calendar months."""
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

    by_service: dict[str, dict[str, float]] = {}
    next_token = None
    while True:
        params: dict = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": [primary, fallback],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
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
            for group in period.get("Groups", []):
                service = group["Keys"][0] or "Unknown"
                amount = _metric_amount(group["Metrics"], primary, fallback)
                if abs(amount) < 0.0001:
                    continue
                by_service.setdefault(service, {})
                by_service[service][month_suffix] = (
                    by_service[service].get(month_suffix, 0.0) + amount
                )

        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    return by_service


def _org_history_totals(history: dict) -> dict[str, float]:
    org = history.get("org", {})
    if isinstance(org, dict) and "totals" in org:
        return org["totals"]
    return org if isinstance(org, dict) else {}


def _org_history_by_service(history: dict) -> dict[str, dict[str, float]]:
    org = history.get("org", {})
    if isinstance(org, dict) and "by_service" in org:
        return org["by_service"]
    return history.get("org_by_service", {})


def fetch_monthly_history(ce_client, today: date) -> dict:
    """Fetch closed-month totals once; stored in baseline for reuse until month-end."""
    ranges = historical_month_ranges(today, MONTHLY_HISTORY_COUNT)
    org_totals = fetch_monthly_totals(ce_client, ranges)
    org_by_service = fetch_monthly_totals_by_service(ce_client, ranges)
    by_account: dict[str, dict] = {}
    for env_name, cfg in PROFILES.items():
        account_id = cfg["account"]
        by_account[account_id] = {
            "name": env_name,
            "totals": fetch_monthly_totals(
                ce_client, ranges, linked_account_id=account_id
            ),
            "by_service": fetch_monthly_totals_by_service(
                ce_client, ranges, linked_account_id=account_id
            ),
        }
    return {
        "history_month_count": MONTHLY_HISTORY_COUNT,
        "org": {
            "totals": org_totals,
            "by_service": org_by_service,
        },
        "by_account": by_account,
    }


def ensure_monthly_service_history(ce_client, baseline: dict, today: date) -> bool:
    """Backfill per-service monthly history into baseline when missing."""
    history = baseline.setdefault("monthly_history", {})
    org_by_service = _org_history_by_service(history)
    if org_by_service:
        return False

    print("Backfilling per-service monthly history (one-time Cost Explorer fetch)...")
    ranges = historical_month_ranges(
        today, history.get("history_month_count", MONTHLY_HISTORY_COUNT)
    )
    org_by_service = fetch_monthly_totals_by_service(ce_client, ranges)
    org_totals = _org_history_totals(history)
    if not org_totals:
        org_totals = fetch_monthly_totals(ce_client, ranges)

    history["org"] = {"totals": org_totals, "by_service": org_by_service}
    history.pop("org_by_service", None)

    for env_name, cfg in PROFILES.items():
        account_id = cfg["account"]
        acct_hist = history.setdefault("by_account", {}).setdefault(
            account_id, {"name": env_name, "totals": {}}
        )
        acct_hist["name"] = env_name
        if not acct_hist.get("totals"):
            acct_hist["totals"] = fetch_monthly_totals(
                ce_client, ranges, linked_account_id=account_id
            )
        acct_hist["by_service"] = fetch_monthly_totals_by_service(
            ce_client, ranges, linked_account_id=account_id
        )
    return True


def _baseline_today(baseline: dict) -> date:
    month_suffix = baseline.get("month_suffix", "")
    if len(month_suffix) == 6:
        return date(int(month_suffix[:4]), int(month_suffix[4:6]), 1)
    return datetime.now(timezone.utc).date()


def _history_totals_for_filter(
    history: dict, account_id: str, service: str | None
) -> dict[str, float]:
    if account_id == "org":
        if service:
            return _org_history_by_service(history).get(service, {})
        return _org_history_totals(history)

    acct_hist = history.get("by_account", {}).get(account_id, {})
    if service:
        return acct_hist.get("by_service", {}).get(service, {})
    return acct_hist.get("totals", {})


def _forecast_for_filter(baseline: dict, account_id: str, service: str | None) -> float:
    if account_id == "org":
        scope = baseline.get("org", {})
    else:
        scope = baseline.get("by_account", {}).get(account_id, {})

    if service:
        for row in scope.get("comparison", []):
            if row.get("service") == service:
                return float(row.get("forecast_month_end_usd", 0) or 0)
        mtd_services = scope.get("mtd", {}).get("services", {})
        last_services = scope.get("last_month", {}).get("services", {})
        mtd = float(mtd_services.get(service, 0) or 0)
        if mtd > 0:
            periods = baseline.get("periods", {})
            days_elapsed = periods.get("current_mtd", {}).get("days_elapsed", 1) or 1
            days_in_month = periods.get("current_mtd", {}).get("days_in_month", 30)
            return mtd / days_elapsed * days_in_month
        return float(last_services.get(service, 0) or 0)

    return float(scope.get("forecast_month_end_usd", 0) or 0)


def _metric_for_filter(account_id: str) -> str:
    if account_id == "org":
        return PRIMARY_METRIC
    return LINKED_PRIMARY_METRIC


def build_monthly_trend_series(
    baseline: dict,
    *,
    account_id: str = "org",
    service: str | None = None,
) -> dict:
    """Build six-month history + current-month projection for account/service filters."""
    history = baseline.get("monthly_history")
    if not history:
        raise ValueError("baseline missing monthly_history; re-fetch baseline with --force")

    today = _baseline_today(baseline)
    ranges = historical_month_ranges(today, history.get("history_month_count", MONTHLY_HISTORY_COUNT))
    totals = _history_totals_for_filter(history, account_id, service)
    current_suffix = baseline.get("month_suffix") or today.strftime("%Y%m")
    current_label = date(today.year, today.month, 1).strftime("%b %Y")
    forecast = _forecast_for_filter(baseline, account_id, service)

    months = [
        _month_entry(
            ms,
            start.strftime("%b %Y"),
            totals.get(ms, 0.0),
            kind="actual",
        )
        for start, _end, ms in ranges
    ]
    months.append(
        _month_entry(current_suffix, current_label, forecast, kind="projected")
    )

    return {
        "metric": _metric_for_filter(account_id),
        "months": months,
        "account_id": account_id,
        "service": service,
    }


def build_monthly_trend_from_baseline(baseline: dict) -> dict:
    """Build monthly trend cache (org + per-account totals) from baseline."""
    history = baseline.get("monthly_history")
    if not history:
        raise ValueError("baseline missing monthly_history; re-fetch baseline with --force")

    today = _baseline_today(baseline)
    current_suffix = baseline.get("month_suffix") or today.strftime("%Y%m")
    org_series = build_monthly_trend_series(baseline, account_id="org")

    by_account: dict[str, dict] = {}
    history_accounts = history.get("by_account", {})
    for env_name, cfg in PROFILES.items():
        account_id = cfg["account"]
        acct = baseline.get("by_account", {}).get(account_id, {})
        if acct.get("error"):
            continue
        acct_series = build_monthly_trend_series(baseline, account_id=account_id)
        by_account[account_id] = {
            "name": history_accounts.get(account_id, {}).get("name", env_name),
            "account_id": account_id,
            "metric": acct_series["metric"],
            "months": acct_series["months"],
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "month_suffix": current_suffix,
        "history_month_count": history.get("history_month_count", MONTHLY_HISTORY_COUNT),
        "org": {
            "metric": org_series["metric"],
            "months": org_series["months"],
        },
        "by_account": by_account,
    }
