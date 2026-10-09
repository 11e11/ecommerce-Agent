"""Deterministic evidence audit: what is on hand, how fresh, comparable, missing.

This module answers the accounting half of "is this evidence enough to answer
the objective" with plain code: which report types exist per platform, how old
the newest observation is against a configured threshold, which metric series
carry incompatible units/currencies/time grains, and which required skill
inputs nothing matches. The agent layer (``evidence_analyst``) only interprets
this report -- it does not get to invent completeness or freshness.

Two design choices worth remembering:

- Optional skill inputs never become gaps. A missing ``target_acos`` is an
  operator parameter, not missing data, and reporting it as a gap would train
  the reader to ignore gaps.
- Unmapped *required* inputs do become gaps. Saying "we cannot tell whether
  this input is covered" is the conservative answer; guessing that it is
  available is exactly the failure the audit exists to prevent.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .evidence_policy import (
    DEFAULT_FRESHNESS_DAYS,
    EVIDENCE,
    FRESHNESS_DAYS_BY_SOURCE_TYPE,
    PARAMETER,
    UNKNOWN,
    SKILL_INPUT_EVIDENCE,
    PLATFORM_BASELINE_REPORT_TYPES,
)

FRESHNESS_GAP_MARKER = "evidence-freshness"


def _parse_observed_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _age_days(observed: datetime, now: datetime) -> float:
    return round((now - observed).total_seconds() / 86400.0, 2)


def _metric_series(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the four comparability attributes for a metric observation."""
    data = source.get("data")
    if source.get("source_type") != "metric_observation" or not isinstance(data, dict):
        return None
    dimensions = data.get("dimensions")
    return {
        "metric_key": data.get("metric_key"),
        "unit": data.get("unit"),
        "currency": data.get("currency"),
        "time_grain": data.get("time_grain"),
        "dimensions": dimensions if isinstance(dimensions, dict) else {},
        "period_start": data.get("period_start"),
        "period_end": data.get("period_end"),
    }


def _signature(series: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            json.dumps(
                [
                    item.get("metric_key"),
                    item.get("unit"),
                    item.get("currency"),
                    item.get("time_grain"),
                    item.get("dimensions") or {},
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in series
        }
    )


def audit_evidence(
    evidence: Sequence[Mapping[str, Any]],
    *,
    skill_inputs_by_platform: Mapping[str, Mapping[str, Mapping[str, bool]]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the deterministic audit for one run's evidence.

    ``skill_inputs_by_platform`` maps platform -> skill id -> {input name:
    required}. Callers build it from the installed skill manifests, so the audit
    never carries a second copy of the input contract.
    """
    moment = now or datetime.now(timezone.utc)
    sources = [source for source in evidence if isinstance(source, Mapping)]
    by_platform: dict[str, list[Mapping[str, Any]]] = {}
    for source in sources:
        by_platform.setdefault(str(source.get("platform")), []).append(source)

    gaps: list[dict[str, Any]] = []
    platform_reports: list[dict[str, Any]] = []
    def gap_id(platform: str, what: str) -> str:
        digest = hashlib.sha256(what.encode("utf-8")).hexdigest()[:12]
        return f"gap-{platform}-{digest}"

    for platform in sorted(set(by_platform) | set(skill_inputs_by_platform)):
        items = by_platform.get(platform, [])
        report_types = sorted({str(item.get("source_type")) for item in items})
        observed = [
            stamp
            for stamp in (_parse_observed_at(item.get("observed_at")) for item in items)
            if stamp is not None
        ]
        latest = max(observed) if observed else None
        earliest = min(observed) if observed else None
        age = _age_days(latest, moment) if latest is not None else None
        threshold = min(
            (
                FRESHNESS_DAYS_BY_SOURCE_TYPE.get(report_type, DEFAULT_FRESHNESS_DAYS)
                for report_type in report_types
            ),
            default=DEFAULT_FRESHNESS_DAYS,
        )
        source_freshness = []
        for item in items:
            stamp = _parse_observed_at(item.get("observed_at"))
            source_age = _age_days(stamp, moment) if stamp else None
            source_threshold = FRESHNESS_DAYS_BY_SOURCE_TYPE.get(str(item.get("source_type")), DEFAULT_FRESHNESS_DAYS)
            status = ("unknown" if source_age is None or source_age < 0 else
                      "stale" if source_age > source_threshold else "fresh")
            source_freshness.append({"source_id": item.get("source_id"), "status": status,
                                     "age_days": source_age, "threshold_days": source_threshold})
        freshest_by_type = {}
        for item, row in zip(items, source_freshness):
            source_type = str(item.get("source_type"))
            previous = freshest_by_type.get(source_type)
            if previous is None or (row["age_days"] is not None and
                    (previous["age_days"] is None or row["age_days"] < previous["age_days"])):
                freshest_by_type[source_type] = row
        current_reports = list(freshest_by_type.values())
        if not current_reports or any(row["status"] == "unknown" for row in current_reports):
            freshness_status = "unknown"
            freshness_reason = "missing, invalid or future observation timestamp"
        elif all(row["status"] == "fresh" for row in current_reports):
            freshness_status = "fresh"
            freshness_reason = f"newest observation {age}d old (threshold {threshold}d)"
        else:
            freshness_status = "stale"
            freshness_reason = "stale report types: " + ", ".join(
                f"{kind} ({row['age_days']}d, threshold {row['threshold_days']}d)"
                for kind, row in freshest_by_type.items() if row["status"] == "stale")

        series = [item for item in (_metric_series(source) for source in items) if item]
        grouped = {}
        for row in series:
            key = _signature([row])[0]
            if key not in grouped:
                grouped[key] = dict(row) | {"count": 0}
            grouped[key]["count"] += 1
            for field, choose in (("period_start", min), ("period_end", max)):
                values = [value for value in (grouped[key].get(field), row.get(field)) if value is not None]
                grouped[key][field] = choose(values) if values else None
        series = list(grouped.values())
        metric_keys = {item.get("metric_key") for item in series if item.get("metric_key")}

        missing_inputs: list[dict[str, Any]] = []
        for skill_id, declared in sorted(skill_inputs_by_platform.get(platform, {}).items()):
            policy = SKILL_INPUT_EVIDENCE.get(skill_id) or {}
            for input_name, required in sorted(declared.items()):
                spec = policy.get(input_name) or {"kind": UNKNOWN}
                kind = spec.get("kind", UNKNOWN)
                matched: str | None = None
                if kind == EVIDENCE:
                    if spec.get("self_satisfied"):
                        matched = "evidence_audit"
                    else:
                        accepted = set(spec.get("source_types") or ())
                        matched = next(
                            (report_type for report_type in report_types if report_type in accepted),
                            None,
                        )
                        if matched is None and set(spec.get("metric_keys") or ()) & metric_keys:
                            matched = "metric_observation"
                entry: dict[str, Any] = {
                    "skill_id": skill_id,
                    "input": input_name,
                    "required": bool(required),
                    "kind": kind,
                    "matched_source_type": matched,
                }
                if required and matched is None and kind != PARAMETER:
                    identity = gap_id(platform, f"{skill_id}.{input_name}")
                    entry["gap_id"] = identity
                    gaps.append(
                        {
                            "id": identity,
                            "platform": platform,
                            "what": f"{skill_id}.{input_name}",
                            "why": (
                                "no imported report or metric observation matches this "
                                "required input"
                                if kind == EVIDENCE
                                else "required input is not mapped to any evidence type"
                            ),
                        }
                    )
                missing_inputs.append(entry)

        if not skill_inputs_by_platform.get(platform):
            for source_type in PLATFORM_BASELINE_REPORT_TYPES.get(platform, ()):
                if source_type not in report_types:
                    gaps.append({"id": gap_id(platform, source_type), "platform": platform,
                                 "what": source_type, "why": "baseline report missing"})
        if freshness_status in {"stale", "unknown"}:
            gaps.append(
                {
                    "id": gap_id(platform, FRESHNESS_GAP_MARKER),
                    "platform": platform,
                    "what": f"{platform}.{FRESHNESS_GAP_MARKER}",
                    "why": freshness_reason,
                }
            )

        platform_reports.append(
            {
                "platform": platform,
                "source_ids": sorted(str(item.get("source_id")) for item in items),
                "report_types": report_types,
                "observation_window": {
                    "earliest": earliest.isoformat() if earliest else None,
                    "latest": latest.isoformat() if latest else None,
                    "age_days": age,
                },
                "freshness": {
                    "status": freshness_status,
                    "threshold_days": threshold,
                    "reason": freshness_reason,
                    "sources": source_freshness,
                },
                "series": series,
                "metric_signature": _signature(series),
                "missing_for_skills": missing_inputs,
            }
        )

    comparable = [report for report in platform_reports if report["metric_signature"]]
    blockers: list[str] = []
    if len(comparable) < 2:
        blockers.append("fewer than two platforms expose metric observations")
    else:
        baseline = comparable[0]
        for other in comparable[1:]:
            if other["metric_signature"] != baseline["metric_signature"]:
                blockers.append(
                    f"{baseline['platform']} vs {other['platform']}: "
                    "metric_key/unit/currency/time_grain/dimensions differ"
                )

    hard_gaps = [gap for gap in gaps if FRESHNESS_GAP_MARKER not in gap["what"]]
    if not platform_reports:
        adequacy = "unknown"
    elif hard_gaps:
        adequacy = "insufficient"
    elif gaps:
        adequacy = "partial"
    else:
        adequacy = "supported"

    notes = [
        f"{len(platform_reports)} platform(s), {len(sources)} evidence source(s)",
        f"{len(gaps)} gap(s), {len(hard_gaps)} blocking required input(s)",
        "cross-platform comparison blocked: " + "; ".join(blockers)
        if blockers
        else "cross-platform comparison allowed by matching metric signatures",
    ]
    comparability = {"can_compare_across_platforms": not blockers, "blockers": blockers}
    for report in platform_reports:
        report["comparability"] = comparability

    return {
        "generated_at": moment.isoformat(),
        "source_ids": sorted(str(source.get("source_id")) for source in sources),
        "platforms": platform_reports,
        "comparability": comparability,
        "adequacy": adequacy,
        "gaps": gaps,
        "notes": notes,
    }
