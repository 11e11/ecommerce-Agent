"""Bounded cross-turn context; historical conclusions are never new evidence."""

from __future__ import annotations

import json
from typing import Any

from .errors import ValidationError


MAX_CONTEXT_CHARS = 6_000


def build_continuation(parent: dict[str, Any], feedback: str) -> dict[str, Any]:
    if not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= 1_000:
        raise ValidationError("feedback must be a string between 1 and 1000 characters")
    run = parent["run"]
    previous = next((a["content"] for a in parent.get("artifacts", [])
                     if a.get("kind") == "continuation_context"), {})
    context: dict[str, Any] = {
        "parent_run_id": run["id"],
        "root_objective": previous.get("root_objective", run["objective"]),
        "previous_objective": run["objective"],
        "feedback": feedback.strip(),
        "previous_requests": (previous.get("previous_requests", []) +
                              ([previous["feedback"]] if previous.get("feedback") else []))[-3:],
        "policy": "Historical findings are hypotheses, not evidence. Revalidate sources, freshness and gaps; do not cite history as a source.",
        "previous_findings": [],
    }
    # Keep only the immediate parent's platform findings, never recursive history.
    for task in parent.get("tasks", []):
        if task.get("role") != "platform_specialist":
            continue
        result = task.get("result") or {}
        if not result:
            result = next((a["content"] for a in reversed(parent.get("artifacts", []))
                           if a.get("task_id") == task["id"] and a.get("kind") == "specialist_finding"), {})
        entry = {"platform": result.get("platform"),
                 "summary": str(result.get("summary", ""))[:500],
                 "data_gaps": [str(x)[:160] for x in result.get("data_gaps", [])[:5]]}
        if entry["platform"]:
            context["previous_findings"].append(entry)
    while len(json.dumps(context, ensure_ascii=False)) > MAX_CONTEXT_CHARS and context["previous_findings"]:
        context["previous_findings"].pop()
    while len(json.dumps(context, ensure_ascii=False)) > MAX_CONTEXT_CHARS and context["previous_requests"]:
        context["previous_requests"].pop(0)
    return context


def context_for_platform(context: dict[str, Any] | None, platform: str) -> dict[str, Any] | None:
    if context is None:
        return None
    return dict(context) | {"previous_findings": [
        row for row in context.get("previous_findings", []) if row.get("platform") == platform
    ]}


def execution_trace(bundle: dict[str, Any]) -> dict[str, Any]:
    """Readable projection of persisted execution, excluding raw tool payloads."""
    names = {row["id"]: row["agent_name"] for row in bundle["tasks"]}
    kinds = {"agent_plan", "agent_reflection", "specialist_working_memory", "context_assembly"}
    return {
        "run_id": bundle["run"]["id"], "status": bundle["run"]["status"],
        "review_status": bundle["run"]["review_status"],
        "continuation": next((a["content"] for a in bundle["artifacts"] if a["kind"] == "continuation_context"), None),
        "artifacts": [{"agent": names.get(a.get("task_id")), "kind": a["kind"],
                       "attempt": a.get("attempt"), "content": a["content"]}
                      for a in bundle["artifacts"] if a["kind"] in kinds],
        "events": [{"agent": names.get(e.get("task_id")), "type": e["event_type"],
                    "payload": e["payload"]} for e in bundle["events"]
                   if e["event_type"].startswith(("task.plan", "task.step", "task.tool", "task.reflection", "task.replan"))],
    }
