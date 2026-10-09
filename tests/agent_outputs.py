"""Schema-aware canned agent outputs for tests.

The council now asks one role for several different structures -- plan,
reflection, audit judgement, findings, synthesis, verdict -- so fixtures
dispatch on the schema they are handed instead of guessing from the agent name.
When a schema changes, this is the single place that has to follow.

Outputs stay deliberately boring: they cite the evidence they were given, echo
the harness-computed plan bookkeeping, and declare limitations for every gap
the deterministic audit reported, so a test that wants the happy path gets a
report that can pass the real validators.
"""

from __future__ import annotations

from typing import Any, Mapping

from ecommerce_ai_skills.runtime import harness
from ecommerce_ai_skills.runtime.agents import (
    EVIDENCE_AUDIT_SCHEMA,
    MANAGER_SCHEMA,
    REVIEWER_SCHEMA,
    SPECIALIST_SCHEMA,
)


def schema_kind(output_schema: Mapping[str, Any]) -> str:
    """Identify which structure a fixture is being asked for."""
    if output_schema is harness.PLAN_SCHEMA:
        return "plan"
    if output_schema is harness.REFLECTION_SCHEMA:
        return "reflection"
    if output_schema is EVIDENCE_AUDIT_SCHEMA:
        return "audit"
    if output_schema is SPECIALIST_SCHEMA:
        return "specialist"
    if output_schema is MANAGER_SCHEMA:
        return "manager"
    if output_schema is REVIEWER_SCHEMA:
        return "reviewer"
    required = set(output_schema.get("required") or ())
    if "objective_restatement" in required:
        return "plan"
    if "sufficiency" in required:
        return "reflection"
    if "adequacy" in required:
        return "audit"
    if "verdict" in required:
        return "reviewer"
    if "priorities" in required:
        return "manager"
    if "findings" in required:
        return "specialist"
    raise AssertionError(f"fixture has no canned output for schema {sorted(required)}")


def respond(
    agent_name: str,
    payload: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one valid canned output for whichever schema was requested."""
    kind = schema_kind(output_schema)
    if kind == "plan":
        return plan_output(payload)
    if kind == "reflection":
        return reflection_output(payload)
    if kind == "audit":
        return audit_output(payload)
    if kind == "specialist":
        return specialist_output(agent_name, payload)
    if kind == "manager":
        return manager_output(payload)
    return reviewer_output(payload)


def plan_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "objective_restatement": str(payload.get("objective") or ""),
        "steps": [
            {
                "step_id": 1,
                "question": "Which installed rules and thresholds bear on the objective?",
                # search_knowledge is offered in every tier, so a fixture plan
                # stays valid whichever tier the run lands on.
                "tools": ["opc.search_knowledge"] if "opc.search_knowledge" in payload.get("available_tools", ["opc.search_knowledge"]) else [],
                "gap_id": "",
                "expected_evidence": "rule names, thresholds, and their anchors",
                "done_when": "the governing rules are identified",
            },
            {
                "step_id": 2,
                "question": "Which supplied observation supports or contradicts the finding?",
                "tools": ["opc.search_knowledge"] if "opc.search_knowledge" in payload.get("available_tools", ["opc.search_knowledge"]) else [],
                "gap_id": "",
                "expected_evidence": "an observed value with its period",
                "done_when": "each finding cites a supplied observation",
            },
        ],
        "needs_from_other_platforms": [],
        "assumptions": ["The supplied evidence is a complete weekly snapshot."],
        "would_abstain_if": ["no supplied evidence matches the objective"],
    }


def reflection_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else {}
    step_ids = [int(step["step_id"]) for step in plan.get("steps") or []]
    return {
        "sufficiency": "sufficient",
        "covered_steps": step_ids,
        "uncovered_steps": [],
        "why_insufficient": "",
        "replan": {"needed": False, "changes": []},
        "abandoned": [],
    }


def audit_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    deterministic = payload.get("deterministic_audit")
    deterministic = deterministic if isinstance(deterministic, Mapping) else {}
    gaps = [gap for gap in deterministic.get("gaps") or [] if isinstance(gap, Mapping)]
    return {
        "adequacy": str(deterministic.get("adequacy") or "unknown"),
        "why": "Fixture audit: the accounting comes from deterministic_audit.",
        "ranked_gaps": [
            {
                "gap_id": str(gap["id"]),
                "impact": "the affected statement cannot be grounded",
                "risk_if_ignored": "an unverified claim could reach the report",
            }
            for gap in gaps[:10]
        ],
        "would_change_conclusion": [],
        "comparability_warnings": list(deterministic.get("comparability", {}).get("blockers") or []),
        "applicability_note": "Fixture applicability note.",
    }


def citation_for(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Quote the first offered tool-evidence candidate verbatim."""
    for candidate in payload.get("knowledge_candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        quote = " ".join(str(candidate.get("excerpt") or "").split())[:60]
        if quote:
            return [{"source_id": str(candidate["source_id"]), "quote": quote}]
    return []


def first_source_id(payload: Mapping[str, Any], platform: str) -> str:
    evidence = [
        source
        for source in payload.get("evidence") or []
        if platform == "cross_platform"
        or source.get("platform") in {platform, "cross_platform"}
    ]
    if evidence:
        return str(evidence[0]["source_id"])
    for result in (payload.get("specialist_findings") or {}).values():
        if isinstance(result, Mapping):
            for finding in result.get("findings") or []:
                refs = finding.get("evidence_refs") if isinstance(finding, Mapping) else None
                if refs:
                    return str(refs[0])
    catalog = payload.get("evidence_catalog") or []
    if catalog:
        return str(catalog[0]["source_id"])
    raise AssertionError("fixture has no evidence to cite")


def gap_limitations(payload: Mapping[str, Any]) -> list[str]:
    """Name every platform the audit flagged, so the report declares its gaps."""
    audit = payload.get("evidence_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    lines: list[str] = []
    for gap in audit.get("gaps") or []:
        if isinstance(gap, Mapping):
            lines.append(f"{gap.get('platform')}: {gap.get('what')} was not available.")
    return lines


def approach_rows(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """One evidence_approach row per marketplace in the run."""
    catalog = [row for row in payload.get("evidence_catalog") or [] if isinstance(row, Mapping)]
    findings = payload.get("specialist_findings") or {}
    levels = {
        str(result.get("platform")): str(
            (result.get("evidence_sufficiency") or {}).get("level") or "unknown"
        )
        for result in findings.values()
        if isinstance(result, Mapping) and result.get("platform")
    }
    return [{
            "platform": platform,
            "approach": "Reviewed that marketplace's supplied evidence and installed rules.",
            "sufficiency": levels.get(platform, "sufficient"),
        }
        for platform in sorted({str(row["platform"]) for row in catalog if row.get("platform") and row["platform"] != "cross_platform"})
    ]


def limitation_lines(payload: Mapping[str, Any]) -> list[str]:
    """Limitations a passing report needs: specialist shortfalls plus audit gaps."""
    findings = payload.get("specialist_findings") or {}
    lines = ["Only supplied evidence was reviewed."]
    for result in findings.values():
        if not isinstance(result, Mapping):
            continue
        platform = str(result.get("platform") or "")
        level = str((result.get("evidence_sufficiency") or {}).get("level") or "unknown")
        if platform and platform != "cross_platform" and level != "sufficient":
            lines.append(f"{platform}: the specialist reported {level} evidence.")
    lines.extend(gap_limitations(payload))
    audit = payload.get("evidence_audit") or {}
    if audit and audit.get("adequacy") != "supported":
        lines.append("Evidence audit adequacy: " + str(audit.get("adequacy")))
    return list(dict.fromkeys(lines))


def specialist_output(agent_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    platform = str(payload.get("target_platform") or "cross_platform")
    return {
        "platform": platform,
        "summary": f"{agent_name} completed an evidence-bound review.",
        "findings": [
            {
                "title": "Review current campaign",
                "severity": "warning",
                "confidence": "medium",
                "evidence_refs": [first_source_id(payload, platform)],
                "knowledge_citations": citation_for(payload),
                "recommendation": "Validate profitability before changing bids.",
            }
        ],
        "data_gaps": ["Order history was not supplied."],
        # The harness computes both of these; echoing them keeps plan_executed
        # exactly equal to the planned step ids.
        "evidence_sufficiency": payload.get("evidence_sufficiency")
        or {"level": "sufficient", "reason": "fixture"},
        "plan_executed": list(payload.get("plan_executed") or []),
    }


def manager_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    catalog = [row for row in payload.get("evidence_catalog") or [] if isinstance(row, Mapping)]
    primary = str(catalog[0]["source_id"]) if catalog else first_source_id(payload, "cross_platform")
    primary_platform = str(catalog[0]["platform"]) if catalog else "cross_platform"
    return {
        "executive_summary": "Prioritize the evidence-backed campaign review.",
        "priorities": [
            {
                "rank": 1,
                "title": "Review launch campaign efficiency",
                "why_now": "The supplied weekly export shows current spend and sales.",
                "evidence_refs": [primary],
                "knowledge_citations": citation_for(payload),
                "platforms": [primary_platform],
                "expected_impact": "Clarify whether budget should be reallocated.",
                "confidence": "medium",
                "recommended_owner": "human_operator",
                "downstream_action": "Prepare a bid-change proposal without applying it.",
                "action_type": "external_change",
                "requires_approval": True,
                "metric_claim": {"operation": "none", "observation_refs": []},
            }
        ],
        "risks": [],
        "limitations": limitation_lines(payload),
        "evidence_approach": approach_rows(payload),
    }


def reviewer_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    report = payload.get("manager_report")
    report = report if isinstance(report, Mapping) else {}
    refs = [str(row["source_id"]) for row in payload.get("evidence_catalog") or [] if row.get("source_id")]
    if not refs:
        refs = [
            str(ref)
            for item in report.get("priorities") or []
            for ref in (item.get("evidence_refs") or [])
        ]
    return {
        "verdict": "approved",
        "revision_target": "none",
        "revision_platform": "",
        "issues": [],
        "evidence_refs": refs,
        "limitations": list(report.get("limitations") or ["Fixture limitation."]),
    }
