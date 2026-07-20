#!/usr/bin/env python3
"""Flask dashboard for AWS billing trend reports (reads ./reports cache only)."""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request

from report_paths import (
    REPORTS_DIR,
    current_month_suffix,
    find_latest_month,
    load_json,
    report_paths,
)
from report_views import linked_accounts_view
from monthly_trend import build_monthly_trend_series

app = Flask(__name__)


def _resolve_month() -> str | None:
    requested = request.args.get("month") or request.view_args.get("month") if request.view_args else None
    if requested:
        return requested
    month, _ = find_latest_month()
    if month:
        return month
    return current_month_suffix()


def _load_bundle(month_suffix: str) -> dict | None:
    paths = report_paths(month_suffix)
    baseline = load_json(paths["baseline"])
    if not baseline:
        return None
    recommendations = load_json(paths["recommendations"]) or {
        "items": [],
        "recommendation_count": 0,
    }
    return {
        "month_suffix": month_suffix,
        "paths": paths,
        "baseline": baseline,
        "recommendations": recommendations,
    }


def _account_options(baseline: dict) -> list[dict]:
    options = [{"id": "org", "name": "Organization (all accounts)"}]
    for account_id, acct in sorted(
        baseline.get("by_account", {}).items(),
        key=lambda x: x[1].get("name", x[0]),
    ):
        options.append(
            {
                "id": account_id,
                "name": acct.get("name", account_id),
            }
        )
    return options


def _scope_data(baseline: dict, account_id: str) -> dict:
    if account_id == "org":
        return baseline.get("org", {})
    return baseline.get("by_account", {}).get(account_id, {})


def _filter_comparison(scope: dict, service: str | None) -> list[dict]:
    rows = list(scope.get("comparison", []))
    if service:
        rows = [r for r in rows if r.get("service") == service]
    return rows


def _filter_recommendations(recs: dict, account_id: str, service: str | None) -> list[dict]:
    items = recs.get("items", [])
    if account_id != "org":
        items = [i for i in items if i.get("account_id") == account_id]
    else:
        items = [i for i in items if i.get("account_id") == "org"]
    if service:
        items = [i for i in items if i.get("service") == service]
    return items


@app.route("/")
def dashboard():
    month_suffix = _resolve_month()
    bundle = _load_bundle(month_suffix) if month_suffix else None

    return render_template(
        "dashboard.html",
        month_suffix=month_suffix,
        has_data=bundle is not None,
    )


@app.route("/api/data")
def api_data():
    month_suffix = request.args.get("month") or _resolve_month()
    if not month_suffix:
        return jsonify({"error": "No report month available"}), 404

    bundle = _load_bundle(month_suffix)
    if not bundle:
        return jsonify({"error": f"No baseline for {month_suffix}"}), 404

    account_id = request.args.get("account", "org")
    service = request.args.get("service") or None

    baseline = bundle["baseline"]
    scope = _scope_data(baseline, account_id)
    comparison = _filter_comparison(scope, service)
    recommendations = _filter_recommendations(bundle["recommendations"], account_id, service)

    metric = baseline.get("metric", "NetAmortizedCost")
    forecast_metric = baseline.get("forecast_metric", "NET_UNBLENDED_COST")
    if account_id != "org":
        metric = baseline.get("linked_account_metric", "AmortizedCost")
        forecast_metric = scope.get("forecast_metric", "NET_UNBLENDED_COST")

    services = sorted(
        {
            row["service"]
            for row in scope.get("comparison", [])
        }
    )

    payload = {
        "month_suffix": month_suffix,
        "generated_at": baseline.get("generated_at"),
        "forecast_updated_at": baseline.get("forecast_updated_at"),
        "metric": metric,
        "forecast_metric": forecast_metric,
        "forecast_method": scope.get("forecast_method", baseline.get("forecast_method")),
        "linked_account_metric": baseline.get("linked_account_metric", "AmortizedCost"),
        "periods": baseline.get("periods"),
        "account_id": account_id,
        "account_name": (
            "Organization (all accounts)"
            if account_id == "org"
            else scope.get("name", account_id)
        ),
        "service_filter": service,
        "accounts": _account_options(baseline),
        "services": services,
        "summary": {
            "last_month_total_usd": scope.get("last_month", {}).get("total_usd", 0),
            "last_month_daily_avg_usd": scope.get("last_month", {}).get("daily_avg_usd", 0),
            "mtd_total_usd": scope.get("mtd", {}).get("total_usd", 0),
            "mtd_daily_avg_usd": scope.get("mtd", {}).get("daily_avg_usd", 0),
            "forecast_month_end_usd": scope.get("forecast_month_end_usd", 0),
        },
        "comparison": comparison,
        "recommendations": recommendations,
        "top_last_month_services": list(scope.get("last_month", {}).get("services", {}).items())[:15],
        "top_mtd_services": list(scope.get("mtd", {}).get("services", {}).items())[:15],
    }
    if account_id == "org":
        view = linked_accounts_view(baseline)
        payload["service_account_breakdown"] = view["service_account_breakdown"]
        payload["account_comparison"] = view["account_comparison"]
        payload["account_service_breakdown"] = view["account_service_breakdown"]

    try:
        payload["monthly_trend"] = build_monthly_trend_series(
            baseline,
            account_id=account_id,
            service=service,
        )
    except ValueError:
        payload["monthly_trend"] = {"metric": metric, "months": []}

    return jsonify(payload)


@app.route("/api/months")
def api_months():
    if not os.path.isdir(REPORTS_DIR):
        return jsonify({"months": []})
    months = sorted(
        {
            name.split("_billing_baseline")[0]
            for name in os.listdir(REPORTS_DIR)
            if name.endswith("_billing_baseline.json")
        },
        reverse=True,
    )
    return jsonify({"months": months})


if __name__ == "__main__":
    os.makedirs(REPORTS_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
