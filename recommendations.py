"""
Generate cost trend recommendations from billing baseline data.

Uses OpenAI when OPENAI_API_KEY is set; otherwise applies usage-type heuristics.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

PRIMARY_METRIC = "NetAmortizedCost"
DRILLDOWN_TOP_N = 15
MIN_DAILY_DELTA_USD = 1.0


def _top_movers(comparison: list[dict], n: int = DRILLDOWN_TOP_N) -> list[dict]:
    movers = [c for c in comparison if abs(c.get("delta_daily_usd", 0)) >= MIN_DAILY_DELTA_USD]
    movers.sort(key=lambda x: abs(x.get("delta_daily_usd", 0)), reverse=True)
    return movers[:n]


def _usage_type_deltas(
    last_month: dict[str, float],
    mtd: dict[str, float],
) -> list[dict[str, Any]]:
    keys = set(last_month) | set(mtd)
    rows = []
    for key in keys:
        lm = float(last_month.get(key, 0))
        mt = float(mtd.get(key, 0))
        delta = mt - lm
        if abs(delta) < 0.01:
            continue
        rows.append(
            {
                "usage_type": key,
                "last_month_usd": round(lm, 2),
                "mtd_usd": round(mt, 2),
                "delta_usd": round(delta, 2),
            }
        )
    rows.sort(key=lambda x: abs(x["delta_usd"]), reverse=True)
    return rows[:10]


def _heuristic_recommendation(
    *,
    account_name: str,
    account_id: str,
    service: str,
    trend: str,
    delta_daily_usd: float,
    usage_deltas: list[dict],
) -> dict[str, Any]:
    direction = "higher" if trend == "up" else "lower"
    summary = (
        f"{service} daily spend is ${abs(delta_daily_usd):,.2f}/day {direction} "
        f"than last month's average for {account_name}."
    )

    likely_causes: list[str] = []
    actions: list[str] = []

    svc_lower = service.lower()
    for row in usage_deltas[:5]:
        ut = row["usage_type"]
        delta = row["delta_usd"]
        sign = "increased" if delta > 0 else "decreased"
        likely_causes.append(f"{ut}: {sign} by ${abs(delta):,.2f} (MTD vs last month usage mix)")

    if not likely_causes:
        if trend == "up":
            likely_causes.append("No usage-type drill-down available; overall service spend rose.")
        else:
            likely_causes.append("No usage-type drill-down available; overall service spend fell.")

    if "elastic compute" in svc_lower or service == "Amazon Elastic Compute Cloud - Compute":
        if trend == "up":
            actions.extend(
                [
                    "Review EC2 instance count and new instance launches in Cost Explorer.",
                    "Check for stopped-to-running changes and Auto Scaling events.",
                ]
            )
        else:
            actions.append("Verify rightsizing, scheduling, or instance terminations drove savings.")

    elif "relational database" in svc_lower:
        if trend == "up":
            actions.extend(
                [
                    "Check Aurora I/O, storage growth, and new DB instances or replicas.",
                    "Review Performance Insights for query or connection spikes.",
                ]
            )
        else:
            actions.append("Confirm instance downsizing, I/O reduction, or deleted databases.")

    elif "cloudwatch" in svc_lower:
        if trend == "up":
            actions.extend(
                [
                    "Inspect log ingestion volume and custom metric counts.",
                    "Review log retention and high-cardinality metrics.",
                ]
            )
        else:
            actions.append("Check log retention reductions or metric cleanup.")

    elif "elastic container" in svc_lower or "ecs" in svc_lower or "fargate" in svc_lower:
        if trend == "up":
            actions.extend(
                [
                    "Review ECS/Fargate task count, CPU/memory allocation, and new services.",
                ]
            )
        else:
            actions.append("Verify scaled-down services or reduced task counts.")

    elif "simple storage" in svc_lower or "s3" in svc_lower:
        if trend == "up":
            actions.extend(
                [
                    "Review S3 storage class transitions, request rates, and data transfer.",
                ]
            )
        else:
            actions.append("Check lifecycle policies or reduced storage footprint.")

    elif "kafka" in svc_lower or "msk" in svc_lower:
        if trend == "up":
            actions.append("Review MSK broker count, instance types, and storage growth.")
        else:
            actions.append("Verify broker reductions or cluster changes.")

    else:
        if trend == "up":
            actions.append(f"Open AWS Cost Explorer for {service} and compare daily spend.")
            actions.append("Check for new resources or usage spikes in the linked account.")
        else:
            actions.append(f"Review resource decommissioning or usage reductions for {service}.")

    return {
        "account_id": account_id,
        "account_name": account_name,
        "service": service,
        "trend": trend,
        "delta_daily_usd": round(delta_daily_usd, 2),
        "summary": summary,
        "likely_causes": likely_causes[:5],
        "actions": actions[:4],
        "confidence": "medium" if usage_deltas else "low",
        "source": "heuristic",
    }


def _openai_recommendation(context: dict) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    prompt = (
        "You are an AWS cost analyst. Given the JSON context below, explain why spend "
        "is trending up or down and suggest 2-4 concrete investigation steps. "
        "Respond with JSON only: "
        '{"summary":"...","likely_causes":["..."],"actions":["..."],"confidence":"low|medium|high"}'
        f"\n\nContext:\n{json.dumps(context, indent=2)}"
    )

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(content)
        return parsed
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, IndexError):
        return None


def build_recommendation(
    *,
    account_name: str,
    account_id: str,
    service: str,
    comparison_row: dict,
    usage_drilldown: dict | None,
) -> dict[str, Any]:
    trend = comparison_row["trend"]
    delta_daily = float(comparison_row["delta_daily_usd"])
    last_ut = (usage_drilldown or {}).get("last_month_usage_types", {})
    mtd_ut = (usage_drilldown or {}).get("mtd_usage_types", {})
    usage_deltas = _usage_type_deltas(last_ut, mtd_ut)

    llm_context = {
        "account_name": account_name,
        "account_id": account_id,
        "service": service,
        "trend": trend,
        "delta_daily_usd": delta_daily,
        "last_month_total_usd": comparison_row.get("last_month_total_usd"),
        "mtd_total_usd": comparison_row.get("mtd_total_usd"),
        "last_month_daily_avg_usd": comparison_row.get("last_month_daily_avg_usd"),
        "mtd_daily_avg_usd": comparison_row.get("mtd_daily_avg_usd"),
        "forecast_month_end_usd": comparison_row.get("forecast_month_end_usd"),
        "usage_type_deltas": usage_deltas,
    }

    llm = _openai_recommendation(llm_context)
    if llm:
        return {
            "account_id": account_id,
            "account_name": account_name,
            "service": service,
            "trend": trend,
            "delta_daily_usd": round(delta_daily, 2),
            "summary": llm.get("summary", ""),
            "likely_causes": llm.get("likely_causes", [])[:5],
            "actions": llm.get("actions", [])[:4],
            "confidence": llm.get("confidence", "medium"),
            "source": "openai",
        }

    return _heuristic_recommendation(
        account_name=account_name,
        account_id=account_id,
        service=service,
        trend=trend,
        delta_daily_usd=delta_daily,
        usage_deltas=usage_deltas,
    )


def generate_recommendations(baseline: dict) -> dict:
    """Build recommendations payload for org and each linked account."""
    items: list[dict] = []
    usage_drilldown = baseline.get("usage_drilldown", {})

    org_name = "Organization (all accounts)"
    org_id = "org"
    for row in _top_movers(baseline.get("org", {}).get("comparison", [])):
        service = row["service"]
        items.append(
            build_recommendation(
                account_name=org_name,
                account_id=org_id,
                service=service,
                comparison_row=row,
                usage_drilldown=(usage_drilldown.get("org") or {}).get(service),
            )
        )

    for account_id, acct in baseline.get("by_account", {}).items():
        for row in _top_movers(acct.get("comparison", [])):
            service = row["service"]
            items.append(
                build_recommendation(
                    account_name=acct.get("name", account_id),
                    account_id=account_id,
                    service=service,
                    comparison_row=row,
                    usage_drilldown=(usage_drilldown.get(account_id) or {}).get(service),
                )
            )

    return {
        "generated_at": baseline.get("generated_at"),
        "month_suffix": baseline.get("month_suffix"),
        "metric": baseline.get("metric", PRIMARY_METRIC),
        "recommendation_count": len(items),
        "items": items,
    }
