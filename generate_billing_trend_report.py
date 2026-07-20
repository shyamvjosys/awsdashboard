#!/usr/bin/env python3
"""
AWS billing trend report generator.

Fetches org-wide and per-linked-account spend from the payer Cost Explorer view,
compares last calendar month to current month-to-date, writes yyyymm-prefixed
artifacts under ./reports/, and generates cost recommendations.

Caches spend in {yyyymm}_billing_baseline.json for the calendar month (Cost Explorer
calls only when that file is missing, unless --force). Month-end forecasts are
refreshed from GetCostForecast on every run. On each run, deletes other files
under ./reports/ and regenerates the text report, recommendations, and monthly
trend from the cached baseline.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from config import PAYER_PROFILE, PROFILES, configured_aws_profiles
from monthly_trend import (
    build_monthly_trend_from_baseline,
    ensure_monthly_service_history,
    fetch_monthly_history,
)
from recommendations import generate_recommendations
from report_paths import REPORTS_DIR, clean_derived_reports, report_paths
from report_views import attach_linked_accounts_view

PRIMARY_METRIC = "NetAmortizedCost"
FALLBACK_METRIC = "AmortizedCost"
LINKED_PRIMARY_METRIC = "AmortizedCost"
LINKED_FALLBACK_METRIC = "UnblendedCost"
CE_REGION = "us-east-1"
DRILLDOWN_TOP_N = 15
MIN_DAILY_DELTA_USD = 1.0
# Matches AWS Billing console month-end forecast (GetCostForecast MONTHLY).
FORECAST_METRIC_ORG = "NET_UNBLENDED_COST"
FORECAST_METRIC_LINKED = "NET_UNBLENDED_COST"


def billing_periods(today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    current_start = date(today.year, today.month, 1)
    if today.month == 1:
        last_start = date(today.year - 1, 12, 1)
        last_end = date(today.year, 1, 1)
    else:
        last_start = date(today.year, today.month - 1, 1)
        last_end = current_start

    mtd_end = today + timedelta(days=1)
    days_in_current_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed = (mtd_end - current_start).days

    return {
        "month_suffix": today.strftime("%Y%m"),
        "last_month": {
            "start": last_start.isoformat(),
            "end": last_end.isoformat(),
            "days": (last_end - last_start).days,
            "label": last_start.strftime("%B %Y"),
        },
        "current_mtd": {
            "start": current_start.isoformat(),
            "end": mtd_end.isoformat(),
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_current_month,
            "label": current_start.strftime("%B %Y"),
        },
    }


def get_payer_ce_client():
    if PAYER_PROFILE in configured_aws_profiles():
        session = boto3.Session(profile_name=PAYER_PROFILE)
    else:
        print(f"Warning: payer profile '{PAYER_PROFILE}' not found in ~/.aws/config.")
        first_profile = next(iter(PROFILES.values()))["profile"]
        session = boto3.Session(profile_name=first_profile)
    return session.client("ce", region_name=CE_REGION)


def _metrics_for_scope(linked_account_id: str | None) -> tuple[str, str]:
    """
    Org-wide payer view: NetAmortizedCost (invoice-aligned).
    Linked accounts: AmortizedCost — NetAmortizedCost often nets RI/SP to ~$0 per account.
    """
    if linked_account_id:
        return LINKED_PRIMARY_METRIC, LINKED_FALLBACK_METRIC
    return PRIMARY_METRIC, FALLBACK_METRIC


def _metric_amount(metrics: dict, primary: str, fallback: str) -> tuple[float, str]:
    if primary in metrics:
        return float(metrics[primary]["Amount"]), primary
    if fallback in metrics:
        return float(metrics[fallback]["Amount"]), fallback
    return 0.0, primary


def fetch_service_costs(
    ce_client,
    start: date,
    end: date,
    *,
    linked_account_id: str | None = None,
) -> tuple[dict[str, float], str]:
    filt_parts: list[dict] = []
    if linked_account_id:
        filt_parts.append(
            {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [linked_account_id]}}
        )
    if not filt_parts:
        filt = None
    elif len(filt_parts) == 1:
        filt = filt_parts[0]
    else:
        filt = {"And": filt_parts}

    primary_metric, fallback_metric = _metrics_for_scope(linked_account_id)

    services: dict[str, float] = {}
    metric_used = primary_metric
    next_token = None

    while True:
        params: dict = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": [primary_metric, fallback_metric],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if filt:
            params["Filter"] = filt
        if next_token:
            params["NextPageToken"] = next_token

        resp = ce_client.get_cost_and_usage(**params)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                service = group["Keys"][0] or "Unknown"
                amount, used = _metric_amount(group["Metrics"], primary_metric, fallback_metric)
                if used == fallback_metric and metric_used == primary_metric and abs(amount) > 0:
                    metric_used = fallback_metric
                if abs(amount) < 0.0001:
                    continue
                services[service] = services.get(service, 0.0) + amount

        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    return services, metric_used


def fetch_usage_type_costs(
    ce_client,
    start: date,
    end: date,
    service: str,
    *,
    linked_account_id: str | None = None,
) -> dict[str, float]:
    filt_parts = [{"Dimensions": {"Key": "SERVICE", "Values": [service]}}]
    if linked_account_id:
        filt_parts.append(
            {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [linked_account_id]}}
        )
    filt = {"And": filt_parts} if len(filt_parts) > 1 else filt_parts[0]

    primary_metric, fallback_metric = _metrics_for_scope(linked_account_id)
    usage_types: dict[str, float] = {}
    next_token = None

    while True:
        params: dict = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": [primary_metric, fallback_metric],
            "Filter": filt,
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        }
        if next_token:
            params["NextPageToken"] = next_token

        resp = ce_client.get_cost_and_usage(**params)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                usage_type = group["Keys"][0] or "Unknown"
                amount, _ = _metric_amount(group["Metrics"], primary_metric, fallback_metric)
                if abs(amount) < 0.0001:
                    continue
                usage_types[usage_type] = usage_types.get(usage_type, 0.0) + amount

        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    return usage_types


def build_comparison(
    last_services: dict[str, float],
    mtd_services: dict[str, float],
    *,
    last_days: int,
    mtd_days_elapsed: int,
    days_in_current_month: int,
) -> list[dict]:
    all_services = set(last_services) | set(mtd_services)
    rows = []

    for service in all_services:
        last_total = float(last_services.get(service, 0))
        mtd_total = float(mtd_services.get(service, 0))
        last_daily = last_total / last_days if last_days else 0.0
        mtd_daily = mtd_total / mtd_days_elapsed if mtd_days_elapsed else 0.0
        delta_daily = mtd_daily - last_daily
        pct = ((mtd_daily - last_daily) / last_daily * 100.0) if last_daily > 0 else None
        forecast = mtd_daily * days_in_current_month
        forecast_pct = (
            ((forecast - last_total) / last_total * 100.0) if last_total > 0 else None
        )
        forecast_delta_total = forecast - last_total

        if abs(delta_daily) < 0.001:
            trend = "flat"
        elif delta_daily > 0:
            trend = "up"
        else:
            trend = "down"

        if abs(forecast_delta_total) < 0.01:
            forecast_trend = "flat"
        elif forecast_delta_total > 0:
            forecast_trend = "up"
        else:
            forecast_trend = "down"

        rows.append(
            {
                "service": service,
                "last_month_total_usd": round(last_total, 2),
                "last_month_daily_avg_usd": round(last_daily, 2),
                "mtd_total_usd": round(mtd_total, 2),
                "mtd_daily_avg_usd": round(mtd_daily, 2),
                "delta_daily_usd": round(delta_daily, 2),
                "pct_change_daily": round(pct, 1) if pct is not None else None,
                "forecast_month_end_usd": round(forecast, 2),
                "forecast_vs_last_month_pct": (
                    round(forecast_pct, 1) if forecast_pct is not None else None
                ),
                "forecast_trend": forecast_trend,
                "trend": trend,
            }
        )

    rows.sort(key=lambda r: abs(r["delta_daily_usd"]), reverse=True)
    return rows


def build_scope_block(
    last_services: dict[str, float],
    mtd_services: dict[str, float],
    periods: dict,
) -> dict:
    last_days = periods["last_month"]["days"]
    mtd_days = periods["current_mtd"]["days_elapsed"]
    days_in_month = periods["current_mtd"]["days_in_month"]

    last_total = sum(last_services.values())
    mtd_total = sum(mtd_services.values())
    comparison = build_comparison(
        last_services,
        mtd_services,
        last_days=last_days,
        mtd_days_elapsed=mtd_days,
        days_in_current_month=days_in_month,
    )

    mtd_daily_org = mtd_total / mtd_days if mtd_days else 0.0
    forecast_total = mtd_daily_org * days_in_month

    return {
        "last_month": {
            "total_usd": round(last_total, 2),
            "daily_avg_usd": round(last_total / last_days, 2) if last_days else 0.0,
            "services": {k: round(v, 2) for k, v in sorted(last_services.items(), key=lambda x: -x[1])},
        },
        "mtd": {
            "total_usd": round(mtd_total, 2),
            "daily_avg_usd": round(mtd_daily_org, 2),
            "services": {k: round(v, 2) for k, v in sorted(mtd_services.items(), key=lambda x: -x[1])},
        },
        "forecast_month_end_usd": round(forecast_total, 2),
        "comparison": comparison,
    }


def _recalc_forecast_row(row: dict) -> None:
    last_total = float(row.get("last_month_total_usd", 0) or 0)
    forecast = float(row.get("forecast_month_end_usd", 0) or 0)
    forecast_pct = ((forecast - last_total) / last_total * 100.0) if last_total > 0 else None
    forecast_delta = forecast - last_total
    row["forecast_vs_last_month_pct"] = (
        round(forecast_pct, 1) if forecast_pct is not None else None
    )
    if abs(forecast_delta) < 0.01:
        row["forecast_trend"] = "flat"
    elif forecast_delta > 0:
        row["forecast_trend"] = "up"
    else:
        row["forecast_trend"] = "down"


def _linear_month_end_forecast(scope: dict, days_in_month: int) -> float:
    mtd_daily = float(scope.get("mtd", {}).get("daily_avg_usd", 0) or 0)
    return mtd_daily * days_in_month


def fetch_month_end_forecast(
    ce_client,
    today: date,
    *,
    linked_account_id: str | None = None,
) -> float:
    """AWS Cost Explorer month-end forecast (same source as Billing console)."""
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)

    metric = FORECAST_METRIC_LINKED if linked_account_id else FORECAST_METRIC_ORG
    params: dict = {
        "TimePeriod": {"Start": today.isoformat(), "End": next_month.isoformat()},
        "Granularity": "MONTHLY",
        "Metric": metric,
    }
    if linked_account_id:
        params["Filter"] = {
            "Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [linked_account_id]}
        }

    resp = ce_client.get_cost_forecast(**params)
    return float(resp.get("Total", {}).get("Amount", 0) or 0)


def apply_aws_forecasts(ce_client, baseline: dict, today: date) -> None:
    """Replace linear MTD extrapolation with Cost Explorer forecasts."""
    periods = baseline.get("periods", {})
    days_in_month = periods.get("current_mtd", {}).get("days_in_month", 30)
    forecast_at = datetime.now(timezone.utc).isoformat()

    org = baseline["org"]
    linear_org = _linear_month_end_forecast(org, days_in_month)
    aws_org = fetch_month_end_forecast(ce_client, today)
    org["forecast_month_end_usd"] = round(aws_org, 2)
    org["forecast_method"] = "aws_cost_explorer"
    org["forecast_metric"] = FORECAST_METRIC_ORG
    org["forecast_updated_at"] = forecast_at

    scale_org = aws_org / linear_org if linear_org > 0 else 1.0
    for row in org.get("comparison", []):
        row["forecast_month_end_usd"] = round(
            float(row.get("forecast_month_end_usd", 0) or 0) * scale_org, 2
        )
        _recalc_forecast_row(row)

    for account_id, acct in baseline.get("by_account", {}).items():
        if acct.get("error"):
            continue
        linear_acct = _linear_month_end_forecast(acct, days_in_month)
        aws_acct = fetch_month_end_forecast(
            ce_client, today, linked_account_id=account_id
        )
        acct["forecast_month_end_usd"] = round(aws_acct, 2)
        acct["forecast_method"] = "aws_cost_explorer"
        acct["forecast_metric"] = FORECAST_METRIC_LINKED
        acct["forecast_updated_at"] = forecast_at

        scale_acct = aws_acct / linear_acct if linear_acct > 0 else 1.0
        for row in acct.get("comparison", []):
            row["forecast_month_end_usd"] = round(
                float(row.get("forecast_month_end_usd", 0) or 0) * scale_acct, 2
            )
            _recalc_forecast_row(row)

    baseline["forecast_method"] = "aws_cost_explorer"
    baseline["forecast_metric"] = FORECAST_METRIC_ORG
    baseline["forecast_updated_at"] = forecast_at


def collect_usage_drilldown(
    ce_client,
    comparison: list[dict],
    *,
    linked_account_id: str | None,
    periods: dict,
) -> dict[str, dict]:
    last_start = date.fromisoformat(periods["last_month"]["start"])
    last_end = date.fromisoformat(periods["last_month"]["end"])
    mtd_start = date.fromisoformat(periods["current_mtd"]["start"])
    mtd_end = date.fromisoformat(periods["current_mtd"]["end"])

    movers = [c for c in comparison if abs(c["delta_daily_usd"]) >= MIN_DAILY_DELTA_USD]
    movers.sort(key=lambda x: abs(x["delta_daily_usd"]), reverse=True)
    movers = movers[:DRILLDOWN_TOP_N]

    drilldown: dict[str, dict] = {}
    for row in movers:
        service = row["service"]
        try:
            last_ut = fetch_usage_type_costs(
                ce_client, last_start, last_end, service, linked_account_id=linked_account_id
            )
            mtd_ut = fetch_usage_type_costs(
                ce_client, mtd_start, mtd_end, service, linked_account_id=linked_account_id
            )
            drilldown[service] = {
                "last_month_usage_types": {k: round(v, 2) for k, v in last_ut.items()},
                "mtd_usage_types": {k: round(v, 2) for k, v in mtd_ut.items()},
            }
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            print(f"    Warning: usage drill-down for {service} failed ({code})")

    return drilldown


def fetch_baseline_from_aws(ce_client, periods: dict) -> dict:
    last_start = date.fromisoformat(periods["last_month"]["start"])
    last_end = date.fromisoformat(periods["last_month"]["end"])
    mtd_start = date.fromisoformat(periods["current_mtd"]["start"])
    mtd_end = date.fromisoformat(periods["current_mtd"]["end"])

    print("Fetching organization-wide spend...")
    org_last, metric = fetch_service_costs(ce_client, last_start, last_end)
    org_mtd, metric_mtd = fetch_service_costs(ce_client, mtd_start, mtd_end)
    if metric_mtd != metric:
        metric = metric_mtd

    org_block = build_scope_block(org_last, org_mtd, periods)
    usage_drilldown: dict[str, dict] = {
        "org": collect_usage_drilldown(
            ce_client, org_block["comparison"], linked_account_id=None, periods=periods
        )
    }

    by_account: dict[str, dict] = {}
    for env_name, cfg in PROFILES.items():
        account_id = cfg["account"]
        print(f"  Fetching {env_name} ({account_id})...")
        try:
            acct_last, _ = fetch_service_costs(
                ce_client, last_start, last_end, linked_account_id=account_id
            )
            acct_mtd, _ = fetch_service_costs(
                ce_client, mtd_start, mtd_end, linked_account_id=account_id
            )
            block = build_scope_block(acct_last, acct_mtd, periods)
            by_account[account_id] = {
                "name": env_name,
                "account_id": account_id,
                **block,
            }
            usage_drilldown[account_id] = collect_usage_drilldown(
                ce_client,
                block["comparison"],
                linked_account_id=account_id,
                periods=periods,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            print(f"    ERROR ({code}): {msg}")
            by_account[account_id] = {
                "name": env_name,
                "account_id": account_id,
                "error": f"{code}: {msg}",
                "last_month": {"total_usd": 0, "daily_avg_usd": 0, "services": {}},
                "mtd": {"total_usd": 0, "daily_avg_usd": 0, "services": {}},
                "forecast_month_end_usd": 0,
                "comparison": [],
            }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "month_suffix": periods["month_suffix"],
        "metric": metric,
        "linked_account_metric": LINKED_PRIMARY_METRIC,
        "payer_profile": PAYER_PROFILE,
        "periods": periods,
        "org": org_block,
        "by_account": by_account,
        "usage_drilldown": usage_drilldown,
    }


def write_text_report(baseline: dict, output_path: str) -> None:
    lines: list[str] = []
    w = lines.append
    periods = baseline["periods"]
    org = baseline["org"]
    now = baseline.get("generated_at", "")

    w("=" * 100)
    w("AWS BILLING TREND REPORT")
    w(f"Generated: {now}")
    w(f"Month suffix: {baseline.get('month_suffix')}")
    w(f"Org metric: {baseline.get('metric')}")
    w(f"Linked account metric: {baseline.get('linked_account_metric', LINKED_PRIMARY_METRIC)}")
    w(f"Payer profile: {baseline.get('payer_profile')}")
    w("=" * 100)
    w("")
    w("EXECUTIVE SUMMARY")
    w("-" * 100)
    w(f"Last month ({periods['last_month']['label']}):     ${org['last_month']['total_usd']:>14,.2f}")
    w(f"Current MTD ({periods['current_mtd']['label']}):  ${org['mtd']['total_usd']:>14,.2f}")
    w(f"MTD daily average:                    ${org['mtd']['daily_avg_usd']:>14,.2f}")
    w(f"Forecast month-end:                   ${org['forecast_month_end_usd']:>14,.2f}")
    delta = org["forecast_month_end_usd"] - org["last_month"]["total_usd"]
    w(f"Forecast vs last month:               ${delta:>14,.2f}")
    w("")

    w("LAST MONTH — TOP SERVICES")
    w("-" * 100)
    for svc, amt in list(org["last_month"]["services"].items())[:20]:
        w(f"  {svc:<55} ${amt:>12,.2f}")
    w("")

    w("COST DRIVERS — TRENDING UP (org)")
    w("-" * 100)
    up = [c for c in org["comparison"] if c["trend"] == "up" and c["delta_daily_usd"] >= MIN_DAILY_DELTA_USD]
    up.sort(key=lambda x: x["delta_daily_usd"], reverse=True)
    for row in up[:20]:
        pct = row["pct_change_daily"]
        pct_s = f"{pct:+.1f}%" if pct is not None else "n/a"
        w(
            f"  {row['service']:<45} "
            f"Δ ${row['delta_daily_usd']:>8,.2f}/day ({pct_s})  "
            f"forecast ${row['forecast_month_end_usd']:,.2f}"
        )
    if not up:
        w("  (none above threshold)")
    w("")

    w("COST DRIVERS — TRENDING DOWN (org)")
    w("-" * 100)
    down = [c for c in org["comparison"] if c["trend"] == "down" and abs(c["delta_daily_usd"]) >= MIN_DAILY_DELTA_USD]
    down.sort(key=lambda x: x["delta_daily_usd"])
    for row in down[:20]:
        pct = row["pct_change_daily"]
        pct_s = f"{pct:+.1f}%" if pct is not None else "n/a"
        w(
            f"  {row['service']:<45} "
            f"Δ ${row['delta_daily_usd']:>8,.2f}/day ({pct_s})  "
            f"forecast ${row['forecast_month_end_usd']:,.2f}"
        )
    if not down:
        w("  (none above threshold)")
    w("")

    w("LINKED ACCOUNTS — FORECAST SUMMARY")
    w("-" * 100)
    for account_id, acct in sorted(
        baseline.get("by_account", {}).items(),
        key=lambda x: x[1].get("forecast_month_end_usd", 0),
        reverse=True,
    ):
        if acct.get("error"):
            w(f"  {acct['name']} ({account_id}): ERROR — {acct['error']}")
            continue
        w(
            f"  {acct['name']:<20} ({account_id})  "
            f"last ${acct['last_month']['total_usd']:>10,.2f}  "
            f"mtd ${acct['mtd']['total_usd']:>10,.2f}  "
            f"forecast ${acct['forecast_month_end_usd']:>10,.2f}"
        )
    w("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def load_baseline(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AWS billing trend reports.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch spend data from Cost Explorer even if baseline file exists.",
    )
    parser.add_argument(
        "--month",
        help="Target month as yyyymm (defaults to current month).",
    )
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    periods = billing_periods(today)
    month_suffix = args.month or periods["month_suffix"]
    periods["month_suffix"] = month_suffix
    paths = report_paths(month_suffix)

    os.makedirs(REPORTS_DIR, exist_ok=True)

    removed = clean_derived_reports()
    if removed:
        print(f"Removed {len(removed)} derived report file(s) (kept baseline cache).")

    baseline: dict | None = None
    if os.path.isfile(paths["baseline"]) and not args.force:
        print(f"Using cached baseline: {paths['baseline']}")
        baseline = load_baseline(paths["baseline"])
        if "monthly_history" not in baseline:
            print("Backfilling monthly history into baseline (one-time Cost Explorer fetch)...")
            ce_client = get_payer_ce_client()
            try:
                baseline["monthly_history"] = fetch_monthly_history(ce_client, today)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "Unknown")
                msg = exc.response.get("Error", {}).get("Message", str(exc))
                print(f"Monthly history fetch failed ({code}): {msg}")
                sys.exit(1)
            save_json(paths["baseline"], baseline)
    else:
        print("Fetching billing data from AWS Cost Explorer...")
        print(f"  Last month: {periods['last_month']['start']} → {periods['last_month']['end']}")
        print(f"  Current MTD: {periods['current_mtd']['start']} → {periods['current_mtd']['end']}")
        ce_client = get_payer_ce_client()
        try:
            baseline = fetch_baseline_from_aws(ce_client, periods)
            baseline["monthly_history"] = fetch_monthly_history(ce_client, today)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            print(f"Cost Explorer failed ({code}): {msg}")
            sys.exit(1)
        save_json(paths["baseline"], baseline)
        print(f"Baseline written: {paths['baseline']}")

    if baseline is None:
        print("No baseline data available.")
        sys.exit(1)

    view_backfill = "linked_accounts_view" not in baseline
    print("Refreshing month-end forecasts from AWS Cost Explorer...")
    ce_client = get_payer_ce_client()
    try:
        if ensure_monthly_service_history(ce_client, baseline, today):
            print("Per-service monthly history saved to baseline.")
        apply_aws_forecasts(ce_client, baseline, today)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        print(f"Forecast refresh failed ({code}): {msg}")
        sys.exit(1)

    attach_linked_accounts_view(baseline)
    save_json(paths["baseline"], baseline)
    if view_backfill:
        print("Linked accounts view backfilled into baseline.")

    write_text_report(baseline, paths["report"])
    print(f"Text report written: {paths['report']}")

    print("Generating recommendations...")
    recs = generate_recommendations(baseline)
    save_json(paths["recommendations"], recs)
    print(f"Recommendations written: {paths['recommendations']} ({recs['recommendation_count']} items)")

    print("Building monthly trend from cached baseline...")
    monthly_trend = build_monthly_trend_from_baseline(baseline)
    save_json(paths["monthly_trend"], monthly_trend)
    print(f"Monthly trend written: {paths['monthly_trend']}")

    org = baseline["org"]
    print("")
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Month suffix:        {month_suffix}")
    print(f"Last month total:    ${org['last_month']['total_usd']:,.2f}")
    print(f"MTD total:           ${org['mtd']['total_usd']:,.2f}")
    print(f"Forecast month-end:  ${org['forecast_month_end_usd']:,.2f}")


if __name__ == "__main__":
    main()
