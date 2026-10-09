"""Bounded specialist harness: plan, working memory, reflect, then answer.

The specialist used to be a single bounded tool loop followed by one structured
answer. That is a retrieval call, not an agent: nothing records what it meant
to look for, nothing tracks what it already knows, and nothing checks whether
the plan was actually covered before the answer is written.

This module adds the missing three steps -- plan, working memory, reflect --
without loosening any boundary:

- **Plan-and-execute skeleton, ReAct inside.** The plan says which questions the
  platform analysis must answer; each execution round still uses the existing
  bounded tool loop, so a surprising tool result can change the next query.
- **Working memory is owned by the harness, not the model.** Between rounds the
  harness injects a bounded summary (``MAX_INJECTION_CHARS``), keeping context
  size decoupled from the number of steps. Full text stays addressable by
  evidence id.
- **At most one re-plan.** The reflection step decides "sufficient / partial /
  insufficient" and may ask for exactly one revised plan; after that the run
  delivers with an explicit gap instead of looping.

Everything here is provider-agnostic: callers pass closures that perform the
plan / research / reflect / final calls, so this module can be unit-tested with
fakes and never needs to know how a provider formats a request.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from .errors import ExternalServiceError, ValidationError

# --- schemas ---------------------------------------------------------------

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "objective_restatement",
        "steps",
        "needs_from_other_platforms",
        "assumptions",
        "would_abstain_if",
    ],
    "properties": {
        "objective_restatement": {"type": "string"},
        "steps": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step_id", "question", "tools", "gap_id", "expected_evidence", "done_when"],
                "properties": {
                    "step_id": {"type": "integer", "minimum": 1},
                    "question": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    # "" means "not tied to an audited gap"; strict structured
                    # outputs cannot carry a nullable string.
                    "gap_id": {"type": "string"},
                    "expected_evidence": {"type": "string"},
                    "done_when": {"type": "string"},
                },
            },
        },
        "needs_from_other_platforms": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["platform", "what", "why"],
                "properties": {
                    "platform": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
        "assumptions": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "would_abstain_if": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
    },
}

REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "sufficiency",
        "covered_steps",
        "uncovered_steps",
        "why_insufficient",
        "replan",
        "abandoned",
    ],
    "properties": {
        "sufficiency": {"type": "string", "enum": ["sufficient", "partial", "insufficient"]},
        "covered_steps": {"type": "array", "items": {"type": "integer"}},
        "uncovered_steps": {"type": "array", "items": {"type": "integer"}},
        "why_insufficient": {"type": "string"},
        "replan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["needed", "changes"],
            "properties": {
                "needed": {"type": "boolean"},
                "changes": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
            },
        },
        "abandoned": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
    },
}

SUFFICIENCY_LEVELS = ("sufficient", "partial", "insufficient")

# --- budgets ---------------------------------------------------------------


@dataclass(frozen=True)
class TierBudget:
    """Per-effort-tier limits. Values are policy, not physics: tune with evals."""

    name: str
    plan_enabled: bool
    max_plan_steps: int
    max_tool_calls: int
    max_ops_pulls: int
    max_replans: int
    deadline_seconds: int


TIER_BUDGETS: dict[str, TierBudget] = {
    "simple": TierBudget("simple", False, 0, 4, 0, 0, 120),
    "standard": TierBudget("standard", True, 5, 6, 2, 1, 240),
    "deep": TierBudget("deep", True, 8, 8, 2, 1, 360),
}
DEFAULT_TIER = "standard"


def budget_for(tier: str | None) -> TierBudget:
    return TIER_BUDGETS.get(str(tier or DEFAULT_TIER), TIER_BUDGETS[DEFAULT_TIER])


# Simple tasks keep the chapter-level knowledge tools; the chunk-level hybrid
# retriever and the read-only tenant data tools are what the heavier tiers buy.
SIMPLE_TOOLS = ("opc.search_knowledge", "opc.get_constraints", "opc.read_chapter")
EXTENDED_TOOLS = SIMPLE_TOOLS + (
    "opc.hybrid_search",
    "opc.ops_briefing",
    "opc.ops_metrics",
    "opc.ops_proposals",
    "opc.ops_evidence",
)


def allowed_tools_for(tier: str | None, graph_tools: Sequence[str]) -> tuple[str, ...]:
    """Intersect the tier's tool set with the published graph contract."""
    granted = set(graph_tools)
    wanted = EXTENDED_TOOLS if budget_for(tier).plan_enabled else SIMPLE_TOOLS
    return tuple(tool for tool in wanted if tool in granted)


PLAN_INSTRUCTIONS = (
    " You are in the planning step. Write the plan a careful operator would "
    "follow for this platform: restate the objective in your own words, then "
    "list the questions the analysis must answer. Every step may only use the "
    "tools offered to you; set gap_id to the audited gap the step closes, or an "
    "empty string when it does not close one. Declare what you would need from "
    "other platforms, and the conditions under which you would abstain. Return "
    "JSON only."
)

REFLECTION_INSTRUCTIONS = (
    " You are in the reflection step. Compare the plan with what the evidence "
    "actually shows: mark every step covered or uncovered (exactly one of the "
    "two lists), state whether the evidence is sufficient, and say what you are "
    "abandoning. Ask for a revised plan only when one change would materially "
    "close the gap. Return JSON only."
)


# --- working memory --------------------------------------------------------

MAX_INJECTION_CHARS = 2_000
MAX_KNOWN_ENTRIES = 12
GIST_CHARS = 120


@dataclass
class WorkingMemory:
    """Bounded, harness-owned state for one specialist task."""

    budget: TierBudget
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_monotonic: float = field(default_factory=time.monotonic)
    plan: dict[str, Any] | None = None
    known: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    steps_done: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_used: int = 0
    ops_pulls_used: int = 0
    replans_used: int = 0
    duplicate_calls: int = 0
    _seen_calls: set[str] = field(default_factory=set)

    # -- accounting --

    @property
    def tool_calls_left(self) -> int:
        return max(0, self.budget.max_tool_calls - self.tool_calls_used)

    @property
    def ops_pulls_left(self) -> int:
        return max(0, self.budget.max_ops_pulls - self.ops_pulls_used)

    @property
    def replans_left(self) -> int:
        return max(0, self.budget.max_replans - self.replans_used)

    @property
    def seconds_left(self) -> float:
        elapsed = time.monotonic() - self.started_monotonic
        return max(0.0, self.budget.deadline_seconds - elapsed)

    def expired(self) -> bool:
        return self.seconds_left <= 0 or self.tool_calls_left == 0

    def exhausted_reason(self) -> str | None:
        if self.seconds_left <= 0:
            return "wall clock deadline reached"
        if self.tool_calls_left == 0:
            return "tool call budget exhausted"
        return None

    # -- mutation --

    def allow_tool_call(self, name: str, arguments_digest: str) -> bool:
        """Reject an immediately repeated tool call; record an admitted one."""
        key = f"{name}:{arguments_digest}"
        self.tool_calls_used += 1
        if key in self._seen_calls:
            self.duplicate_calls += 1
            return False
        self._seen_calls.add(key)
        return True

    def note_ops_pull(self) -> None:
        self.ops_pulls_used += 1

    def note_evidence(self, entries: Sequence[Mapping[str, Any]]) -> None:
        seen = {item.get("source_id") for item in self.known}
        for entry in entries:
            source_id = entry.get("source_id")
            if not source_id or source_id in seen:
                continue
            data = entry.get("data") if isinstance(entry.get("data"), dict) else entry
            text = str(data.get("excerpt") or data.get("heading") or "")
            self.known.append(
                {
                    "source_id": str(source_id),
                    "kind": str(data.get("kind") or entry.get("source_type") or "evidence"),
                    "heading": str(data.get("heading") or "")[:GIST_CHARS],
                    "gist": " ".join(text.split())[:GIST_CHARS],
                }
            )
            seen.add(source_id)
        self.known = self.known[-MAX_KNOWN_ENTRIES:]

    def note_pending_questions(self, questions: Sequence[str]) -> None:
        for question in questions:
            text = " ".join(str(question).split())
            if text and text not in self.open_questions:
                self.open_questions.append(text)

    def note_steps(self, steps: Sequence[Mapping[str, Any]]) -> None:
        for step in steps:
            self.steps_done.append(dict(step))

    # -- bounded injection --

    def injection(self) -> dict[str, Any]:
        """Return the bounded summary handed back to the model each round."""
        plan_steps = []
        if self.plan:
            plan_steps = [
                {
                    "step_id": step.get("step_id"),
                    "question": step.get("question"),
                    "tools": list(step.get("tools") or []),
                    "gap_id": step.get("gap_id") or None,
                }
                for step in self.plan.get("steps") or []
            ]
        payload: dict[str, Any] = {
            "plan": plan_steps,
            "known": [dict(item) for item in self.known[:MAX_KNOWN_ENTRIES]],
            "open_questions": list(self.open_questions[:8]),
            "gaps": [dict(gap) for gap in self.gaps[:8]],
            "steps_done": [dict(step) for step in self.steps_done[-8:]],
            "budget": {
                "tool_calls_left": self.tool_calls_left,
                "ops_pulls_left": self.ops_pulls_left,
                "replans_left": self.replans_left,
                "seconds_left": int(self.seconds_left),
            },
        }
        return _shrink(payload, MAX_INJECTION_CHARS)


def _shrink(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Drop the least useful memory entries until the summary fits the limit."""
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))

    while size(payload) > limit and payload["known"]:
        payload["known"].pop()
    while size(payload) > limit and payload["open_questions"]:
        payload["open_questions"].pop()
    while size(payload) > limit and payload["gaps"]:
        payload["gaps"].pop()
    if size(payload) > limit:
        for key in ("steps_done", "plan"):
            while size(payload) > limit and payload[key]:
                payload[key].pop()
    if size(payload) > limit:
        return {"budget": payload["budget"], "summary_truncated": True}
    return payload


# --- validation ------------------------------------------------------------


def validate_plan(
    plan: Any,
    *,
    budget: TierBudget,
    allowed_tools: Sequence[str],
    gap_ids: Sequence[str],
    platforms: Sequence[str],
) -> dict[str, Any]:
    """Normalise and check a plan before any tool call is allowed."""
    if not isinstance(plan, dict) or set(plan) != set(PLAN_SCHEMA["required"]):
        raise ExternalServiceError("planner output fields did not match the plan schema")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ExternalServiceError("planner returned no steps")
    if budget.max_plan_steps and len(steps) > budget.max_plan_steps:
        raise ExternalServiceError(
            f"planner returned {len(steps)} steps; the {budget.name} tier allows "
            f"{budget.max_plan_steps}"
        )
    allowed = set(allowed_tools)
    known_gaps = set(gap_ids)
    valid_platforms = set(platforms) | {"cross_platform"}
    normalised_steps = []
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or set(step) != set(
            PLAN_SCHEMA["properties"]["steps"]["items"]["required"]
        ):
            raise ExternalServiceError("planner step fields did not match the plan schema")
        if isinstance(step.get("step_id"), bool) or step.get("step_id") != position:
            raise ExternalServiceError("planner step ids must be contiguous from 1")
        tools = step.get("tools")
        if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
            raise ExternalServiceError("planner step tools must be a list of names")
        unknown_tools = sorted(set(tools) - allowed)
        if unknown_tools:
            raise ExternalServiceError(
                "planner requested tools outside this role's allowlist: "
                + ", ".join(unknown_tools)
            )
        gap_id = step.get("gap_id") or ""
        if gap_id and gap_id not in known_gaps:
            raise ExternalServiceError(f"planner referenced an unknown gap: {gap_id}")
        for key, limit in (("question", 300), ("expected_evidence", 300), ("done_when", 200)):
            if not isinstance(step.get(key), str) or not step[key].strip() or len(step[key]) > limit:
                raise ExternalServiceError(f"planner step {key} is empty or exceeds its bounds")
        normalised_steps.append(
            {
                "step_id": position,
                "question": str(step.get("question", "")),
                "tools": sorted(tools),
                "gap_id": gap_id,
                "expected_evidence": str(step.get("expected_evidence", "")),
                "done_when": str(step.get("done_when", "")),
            }
        )
    needs = plan.get("needs_from_other_platforms")
    if not isinstance(needs, list) or len(needs) > 3:
        raise ExternalServiceError("planner needs_from_other_platforms must be a list")
    for need in needs:
        if not isinstance(need, dict) or set(need) != {"platform", "what", "why"}:
            raise ExternalServiceError("planner cross-platform needs are malformed")
        if str(need.get("platform")) not in valid_platforms:
            raise ExternalServiceError(
                f"planner requested an unknown platform: {need.get('platform')}"
            )
    for key, maximum in (("assumptions", 5), ("would_abstain_if", 3)):
        values = plan.get(key)
        if not isinstance(values, list) or len(values) > maximum or any(not isinstance(item, str) for item in values):
            raise ExternalServiceError(f"planner {key} exceeded its schema bounds")
    return {
        "objective_restatement": str(plan.get("objective_restatement", "")),
        "steps": normalised_steps,
        "needs_from_other_platforms": [dict(item) for item in needs],
        "assumptions": [str(item) for item in plan.get("assumptions") or []],
        "would_abstain_if": [str(item) for item in plan.get("would_abstain_if") or []],
    }


def validate_reflection(
    reflection: Any,
    *,
    step_ids: Sequence[int],
    budget_exhausted: bool,
    replans_left: int,
) -> dict[str, Any]:
    """Check that the reflection partitions the plan and respects the budget."""
    if not isinstance(reflection, dict) or set(reflection) != set(REFLECTION_SCHEMA["required"]):
        raise ExternalServiceError("reflection output fields did not match the schema")
    level = reflection.get("sufficiency")
    if level not in SUFFICIENCY_LEVELS:
        raise ExternalServiceError("reflection returned an unknown sufficiency level")
    covered = reflection.get("covered_steps")
    uncovered = reflection.get("uncovered_steps")
    for value in (covered, uncovered):
        if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise ExternalServiceError("reflection step lists must contain integers")
    if sorted(list(covered) + list(uncovered)) != sorted(step_ids):
        raise ExternalServiceError(
            "reflection must partition the plan into covered and uncovered steps"
        )
    why = str(reflection.get("why_insufficient") or "").strip()
    if level != "sufficient" and not why:
        raise ExternalServiceError("reflection must explain a non-sufficient verdict")
    replan = reflection.get("replan")
    if not isinstance(replan, dict) or set(replan) != {"needed", "changes"}:
        raise ExternalServiceError("reflection replan block is malformed")
    if not isinstance(replan.get("needed"), bool):
        raise ExternalServiceError("reflection replan needed must be boolean")
    for values, maximum in ((replan.get("changes"), 5), (reflection.get("abandoned"), 5)):
        if not isinstance(values, list) or len(values) > maximum or any(not isinstance(item, str) for item in values):
            raise ExternalServiceError("reflection changes/abandoned exceed schema bounds")
    needed = replan["needed"]
    if needed and (budget_exhausted or replans_left <= 0):
        needed = False
    normalised = {
        "sufficiency": level,
        "covered_steps": sorted(covered),
        "uncovered_steps": sorted(uncovered),
        "why_insufficient": why,
        "replan": {"needed": needed, "changes": [str(item) for item in replan.get("changes") or []]},
        "abandoned": [str(item) for item in reflection.get("abandoned") or []],
    }
    if budget_exhausted:
        outstanding = [
            step_id
            for step_id in step_ids
            if step_id not in normalised["covered_steps"]
        ]
        for step_id in outstanding:
            marker = f"step {step_id} not completed before the budget ran out"
            if marker not in normalised["abandoned"]:
                normalised["abandoned"].append(marker)
        if outstanding and normalised["sufficiency"] == "sufficient":
            normalised["sufficiency"] = "partial"
            normalised["why_insufficient"] = "planned steps remain uncovered at the execution limit"
    return normalised


def plan_executed_records(
    plan: Mapping[str, Any] | None, reflection: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Turn the plan and its reflection into the per-step outcome the report shows."""
    if not plan:
        return []
    covered = set((reflection or {}).get("covered_steps") or [])
    abandoned = {str(item) for item in (reflection or {}).get("abandoned") or []}
    records = []
    for step in plan.get("steps") or []:
        step_id = int(step.get("step_id"))
        if step_id in covered:
            outcome = "done"
        elif any(f"step {step_id} " in item for item in abandoned):
            outcome = "skipped"
        else:
            outcome = "partial"
        records.append(
            {
                "step_id": step_id,
                "outcome": outcome,
                "note": str(step.get("question", ""))[:200],
            }
        )
    return records


# --- harness ---------------------------------------------------------------


class TraceRecorder(Protocol):
    def artifact(self, kind: str, content: dict[str, Any]) -> None:
        """Persist one harness artifact (plan, reflection, audit)."""

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Persist one harness event (step started, observed, refused)."""


class NullRecorder:
    """Default recorder: keeps the harness usable from tests without storage."""

    def artifact(self, kind: str, content: dict[str, Any]) -> None:
        return None

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


@dataclass
class HarnessOutcome:
    result: dict[str, Any]
    memory: WorkingMemory
    plan: dict[str, Any] | None
    reflection: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)


PlanCallable = Callable[[dict[str, Any], "WorkingMemory"], dict[str, Any]]
ResearchCallable = Callable[[dict[str, Any], "WorkingMemory"], Sequence[dict[str, Any]]]
ReflectCallable = Callable[[dict[str, Any], "WorkingMemory"], dict[str, Any]]
FinalCallable = Callable[[dict[str, Any], "WorkingMemory"], dict[str, Any]]


def run_specialist_harness(
    *,
    budget: TierBudget,
    allowed_tools: Sequence[str],
    gap_ids: Sequence[str],
    platforms: Sequence[str],
    base_payload: Mapping[str, Any],
    plan_call: PlanCallable,
    research_call: ResearchCallable,
    reflect_call: ReflectCallable,
    final_call: FinalCallable,
    recorder: TraceRecorder | None = None,
    tier_rationale: str = "",
    initial_gaps: Sequence[Mapping[str, str]] = (),
) -> HarnessOutcome:
    """Run plan -> execute -> reflect -> (one re-plan) -> final answer.

    ``simple`` keeps the previous single-round behaviour: no plan call, no
    reflection call, and no plan/reflection artifacts -- the tier table is the
    only switch, so the cheap path and the autonomous path cannot drift.
    """
    trace = recorder or NullRecorder()
    memory = WorkingMemory(budget=budget)
    for gap in initial_gaps:
        if isinstance(gap, Mapping) and gap.get("id"):
            memory.gaps.append({"id": str(gap.get("id")), "what": str(gap.get("what") or "")})
    warnings: list[str] = []
    if not budget.plan_enabled:
        notes = list(research_call(dict(base_payload), memory))
        payload = dict(base_payload) | {"research_notes": list(notes)}
        payload["evidence_sufficiency"] = {
            "level": "sufficient" if (base_payload.get("evidence_audit") or {}).get("adequacy") == "supported" else "partial",
            "reason": "simple tier uses the evidence audit without planning or reflection",
        }
        payload["plan_executed"] = []
        result = final_call(payload, memory)
        if any(str(gap.get("id", "")).startswith("tool-unavailable") for gap in memory.gaps):
            result["evidence_sufficiency"] = {"level": "insufficient", "reason": "research tools unavailable"}
        return HarnessOutcome(result=result, memory=memory, plan=None, reflection=None, warnings=["plan_disabled_simple_tier"])

    def make_plan(reason: str) -> dict[str, Any]:
        plan = validate_plan(
            plan_call(
                dict(base_payload) | {"memory": memory.injection(), "plan_reason": reason,
                                      "previous_reflection": reflection if reason == "replan" else None},
                memory,
            ),
            budget=budget,
            allowed_tools=allowed_tools,
            gap_ids=gap_ids,
            platforms=platforms,
        )
        memory.plan = plan
        memory.note_pending_questions([step["question"] for step in plan["steps"]])
        trace.artifact(
            "agent_plan",
            {
                "plan": plan,
                "platform": base_payload["target_platform"],
                "tier": budget.name,
                "tier_rationale": tier_rationale,
                "reason": reason,
                "steps": len(plan["steps"]),
            },
        )
        trace.event("task.plan.created", {"steps": len(plan["steps"]), "reason": reason})
        return plan

    reflection = None
    try:
        plan = make_plan("initial")
    except (TimeoutError, ExternalServiceError):
        if memory.seconds_left > 0:
            raise
        result = _abstention(base_payload, None, "planning deadline reached")
        return HarnessOutcome(result, memory, None, None, ["deadline_reached"])
    observed_steps: set[int] = set()

    def execute(current_plan: dict[str, Any], round_index: int) -> list[dict[str, Any]]:
        observed_steps.clear()
        notes = []
        for step in current_plan["steps"]:
            if memory.expired():
                break
            trace.event("task.step.started", {"round": round_index, "step_id": step["step_id"]})
            try:
                observed = list(research_call(
                    dict(base_payload) | {"memory": memory.injection(), "plan": current_plan,
                                          "step": step, "round": round_index}, memory))
            except (TimeoutError, ExternalServiceError) as exc:
                memory.gaps.append({"id": "research-unavailable", "what": str(exc)[:160]})
                observed = []
            notes.extend(observed)
            memory.note_evidence(_notes_as_evidence(observed))
            memory.note_steps([{"step_id": step["step_id"], "outcome": "observed", "note": step["question"][:120]}])
            observed_steps.add(step["step_id"])
            memory.open_questions = [question for question in memory.open_questions if question != step["question"]]
            trace.event("task.step.observed", {"round": round_index, "step_id": step["step_id"],
                                             "notes": len(observed), "tool_calls_used": memory.tool_calls_used})
        return notes

    notes = execute(plan, 0)
    def reflect(current_plan: dict[str, Any], current_notes: list[dict[str, Any]]) -> dict[str, Any]:
        step_ids = [step["step_id"] for step in current_plan["steps"]]
        fallback = {"sufficiency": "insufficient", "covered_steps": [], "uncovered_steps": step_ids,
                    "why_insufficient": "reflection unavailable or deadline reached",
                    "replan": {"needed": False, "changes": []}, "abandoned": []}
        if memory.seconds_left <= 0:
            raw = fallback
        else:
            try:
                raw = reflect_call(dict(base_payload) | {"plan": current_plan,
                    "research_notes": current_notes, "memory": memory.injection()}, memory)
            except (TimeoutError, ExternalServiceError):
                raw = fallback
        result = validate_reflection(raw, step_ids=step_ids, budget_exhausted=memory.expired(),
                                     replans_left=memory.replans_left)
        unexecuted = set(step_ids) - observed_steps
        if unexecuted:
            result["covered_steps"] = [item for item in result["covered_steps"] if item not in unexecuted]
            result["uncovered_steps"] = sorted(set(result["uncovered_steps"]) | unexecuted)
            result["abandoned"] += [f"step {item} skipped: research budget exhausted" for item in sorted(unexecuted)]
            if result["sufficiency"] == "sufficient":
                result["sufficiency"] = "partial"
            result["why_insufficient"] = "planned research steps were not executed within budget"
        if any(str(gap.get("id", "")).startswith(("tool-unavailable", "research-unavailable")) for gap in memory.gaps):
            result["sufficiency"] = "insufficient"
            result["why_insufficient"] = "required research tools were unavailable; evidence has unresolved gaps"
        return result

    reflection = reflect(plan, notes)
    trace.artifact("agent_reflection", {"reflection": reflection, "round": 0})
    trace.event(
        "task.reflection.completed",
        {"sufficiency": reflection["sufficiency"], "round": 0, "replan": reflection["replan"]["needed"]},
    )

    if reflection["replan"]["needed"]:
        memory.replans_used += 1
        trace.event("task.replan.requested", {"changes": reflection["replan"]["changes"]})
        plan = make_plan("replan")
        notes = notes + execute(plan, 1)
        reflection = reflect(plan, notes)
        trace.artifact("agent_reflection", {"reflection": reflection, "round": 1})
        trace.event(
            "task.reflection.completed",
            {"sufficiency": reflection["sufficiency"], "round": 1, "replan": False},
        )

    executed = plan_executed_records(plan, reflection)
    memory.note_steps(executed)
    payload = dict(base_payload) | {
        "plan": plan,
        "plan_executed": executed,
        "research_notes": notes,
        "memory": memory.injection(),
        "reflection": reflection,
        "evidence_sufficiency": {
            "level": reflection["sufficiency"],
            "reason": reflection["why_insufficient"]
            or "the plan was covered by the evidence on hand",
        },
    }
    if memory.seconds_left <= 0:
        result = _abstention(base_payload, plan, "specialist deadline reached")
    else:
        try:
            result = final_call(payload, memory)
        except (TimeoutError, ExternalServiceError):
            if memory.seconds_left > 0:
                raise
            result = _abstention(base_payload, plan, "specialist deadline reached")
    if result["evidence_sufficiency"]["level"] != "insufficient" or memory.seconds_left > 0:
        result["evidence_sufficiency"] = payload["evidence_sufficiency"]
        result["plan_executed"] = executed
    if memory.duplicate_calls:
        warnings.append(f"duplicate_tool_calls_refused:{memory.duplicate_calls}")
    if memory.exhausted_reason():
        warnings.append(f"budget_exhausted:{memory.exhausted_reason()}")
    return HarnessOutcome(
        result=result, memory=memory, plan=plan, reflection=reflection, warnings=warnings
    )


def _notes_as_evidence(notes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project tool notes into memory entries without copying whole payloads."""
    entries: list[dict[str, Any]] = []
    for note in notes:
        evidence = note.get("evidence")
        if isinstance(evidence, list):
            entries.extend(item for item in evidence if isinstance(item, dict))
    return entries


def _abstention(payload: Mapping[str, Any], plan: Mapping[str, Any] | None, reason: str) -> dict[str, Any]:
    return {"platform": payload["target_platform"], "summary": reason, "findings": [],
            "data_gaps": [reason], "evidence_sufficiency": {"level": "insufficient", "reason": reason},
            "plan_executed": [{"step_id": step["step_id"], "outcome": "skipped", "note": reason}
                              for step in (plan or {}).get("steps", [])]}


def enforce_plan_executed(result: Mapping[str, Any], plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """Guarantee the answer reports exactly the planned steps."""
    if not plan:
        if result.get("plan_executed"):
            raise ValidationError("plan_executed must be empty when the tier plans nothing")
        return dict(result)
    expected = [step["step_id"] for step in plan.get("steps") or []]
    executed = result.get("plan_executed")
    if not isinstance(executed, list):
        raise ExternalServiceError("specialist omitted plan_executed")
    reported = [item.get("step_id") for item in executed if isinstance(item, dict)]
    if sorted(reported) != sorted(expected):
        raise ExternalServiceError(
            "plan_executed must report exactly the planned steps: "
            f"planned {sorted(expected)}, reported {sorted(reported)}"
        )
    return dict(result)
