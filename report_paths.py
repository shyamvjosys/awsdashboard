"""Shared paths and discovery for yyyymm-prefixed billing report files."""

from __future__ import annotations

import glob
import json
import os
from datetime import date, datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(SCRIPT_DIR, "reports")


def current_month_suffix(when: date | None = None) -> str:
    when = when or datetime.now(timezone.utc).date()
    return when.strftime("%Y%m")


def report_paths(month_suffix: str) -> dict[str, str]:
    return {
        "baseline": os.path.join(REPORTS_DIR, f"{month_suffix}_billing_baseline.json"),
        "report": os.path.join(REPORTS_DIR, f"{month_suffix}_billing_trend_report.txt"),
        "recommendations": os.path.join(
            REPORTS_DIR, f"{month_suffix}_billing_recommendations.json"
        ),
        "monthly_trend": os.path.join(
            REPORTS_DIR, f"{month_suffix}_billing_monthly_trend.json"
        ),
    }


def find_latest_month() -> tuple[str | None, dict[str, str] | None]:
    pattern = os.path.join(REPORTS_DIR, "*_billing_baseline.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None, None
    basename = os.path.basename(files[0])
    month_suffix = basename.split("_billing_baseline")[0]
    return month_suffix, report_paths(month_suffix)


def load_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
