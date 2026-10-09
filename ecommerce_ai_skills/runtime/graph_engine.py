"""LangGraph execution engine for the versioned Weekly Ops graph contract.

This module owns scheduling only. Database state transitions, provider calls,
validation, audit, and graph-version enforcement remain in the control plane.

The topology is deliberately two-phase: the evidence analyst audits the whole
evidence base first (what exists, how fresh it is, which required inputs are
missing), and only then do the platform specialists fan out in parallel. That
ordering is what lets a specialist plan against a known gap list instead of
discovering half-way through that the data it needs was never imported.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict

from langgraph.types import Send
from langgraph.graph import END, START, StateGraph


def _merge_findings(
    current: dict[str, dict[str, Any]] | None,
    update: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Merge parallel specialist results into one deterministic state field."""
    return {**(current or {}), **(update or {})}


def _first_failure(
    current: BaseException | None,
    update: BaseException | None,
) -> BaseException | None:
    """Retain the first specialist failure observed by the graph reducer."""
    return current if current is not None else update


class WeeklyOpsState(TypedDict, total=False):
    """State exchanged by the compiled Weekly Ops execution graph."""

    findings: Annotated[dict[str, dict[str, Any]], _merge_findings]
    failure: Annotated[BaseException | None, _first_failure]
    report: dict[str, Any]
    review: dict[str, Any]
    spec: Any


RunAudit = Callable[[Any], dict[str, Any]]
RunSpecialist = Callable[[Any, dict[str, dict[str, Any]]], dict[str, Any]]
RunCross = Callable[[dict[str, dict[str, Any]]], dict[str, Any]]
RunManager = Callable[[dict[str, dict[str, Any]]], dict[str, Any]]
RunReviewer = Callable[
    [dict[str, dict[str, Any]], dict[str, Any]],
    dict[str, Any],
]


def build_run_graph(
    *,
    audit_spec: Any,
    specialist_specs: Sequence[Any],
    cross_spec: Any | None,
    start_agent_task: Callable[[Any], None],
    run_audit: RunAudit,
    run_specialist: RunSpecialist,
    run_cross: RunCross,
    run_manager: RunManager,
    run_reviewer: RunReviewer,
    max_workers: int,
) -> Any:
    """Compile one version-bound run contract into a LangGraph StateGraph.

    The graph deliberately has no checkpointer or interrupt. Durable run/task
    state and whole-run retry semantics are owned by the existing database
    control plane.
    """
    specialists = tuple(specialist_specs)
    concurrency = max(1, min(max_workers, 3))
    graph = StateGraph(WeeklyOpsState)

    def dispatch(_: WeeklyOpsState) -> dict[str, Any]:
        # Preserve the old attempt semantics: every task becomes running before
        # any provider work is submitted, including the specialists that only
        # start after the audit barrier.
        start_agent_task(audit_spec)
        for spec in specialists:
            start_agent_task(spec)
        return {}

    def fan_out_audit(_: WeeklyOpsState) -> list[Send]:
        return [Send("evidence_analyst", {"spec": audit_spec})]

    def fan_out_specialists(state: WeeklyOpsState) -> Any:
        if not specialists:
            return "specialist_barrier"
        findings = state.get("findings", {})
        return [
            Send("specialist", {"spec": spec, "findings": findings})
            for spec in specialists
        ]

    def run_audit_node(state: WeeklyOpsState) -> dict[str, Any]:
        spec = state["spec"]
        try:
            result = run_audit(spec)
        except Exception as exc:
            # The audit is a prerequisite: without it the specialists would plan
            # against an unknown evidence base.
            return {"failure": exc}
        return {"findings": {spec.name: result}}

    def run_specialist_node(state: WeeklyOpsState) -> dict[str, Any]:
        spec = state["spec"]
        try:
            result = run_specialist(spec, state.get("findings", {}))
        except Exception as exc:
            # Parallel branches must all reach the super-step barrier before
            # the run fails, matching the former as_completed loop.
            return {"failure": exc}
        return {"findings": {spec.name: result}}

    def analyst_barrier(_: WeeklyOpsState) -> dict[str, Any]:
        return {}

    def specialist_barrier(_: WeeklyOpsState) -> dict[str, Any]:
        return {}

    def route_after_audit(state: WeeklyOpsState) -> Any:
        if state.get("failure") is not None:
            return END
        return fan_out_specialists(state)

    def route_after_specialists(state: WeeklyOpsState) -> str:
        if state.get("failure") is not None:
            return END
        if cross_spec is not None:
            return "cross_platform_controller"
        return "store_manager"

    def run_cross_node(state: WeeklyOpsState) -> dict[str, Any]:
        result = run_cross(state.get("findings", {}))
        return {"findings": {cross_spec.name: result}}

    def run_manager_node(state: WeeklyOpsState) -> dict[str, Any]:
        return {"report": run_manager(state.get("findings", {}))}

    def run_reviewer_node(state: WeeklyOpsState) -> dict[str, Any]:
        return {
            "review": run_reviewer(
                state.get("findings", {}),
                state["report"],
            )
        }

    graph.add_node("dispatcher", dispatch)
    graph.add_node("evidence_analyst", run_audit_node)
    graph.add_node("analyst_barrier", analyst_barrier)
    graph.add_node("specialist", run_specialist_node)
    graph.add_node("specialist_barrier", specialist_barrier)
    graph.add_node("cross_platform_controller", run_cross_node)
    graph.add_node("store_manager", run_manager_node)
    graph.add_node("operations_reviewer", run_reviewer_node)

    graph.add_edge(START, "dispatcher")
    graph.add_conditional_edges(
        "dispatcher",
        fan_out_audit,
        ["evidence_analyst"],
    )
    graph.add_edge("evidence_analyst", "analyst_barrier")
    graph.add_conditional_edges(
        "analyst_barrier",
        route_after_audit,
        [END, "specialist", "specialist_barrier"],
    )
    graph.add_edge("specialist", "specialist_barrier")
    graph.add_conditional_edges(
        "specialist_barrier",
        route_after_specialists,
        [END, "cross_platform_controller", "store_manager"],
    )
    graph.add_edge("cross_platform_controller", "store_manager")
    graph.add_edge("store_manager", "operations_reviewer")
    graph.add_edge("operations_reviewer", END)

    compiled = graph.compile()
    return compiled.with_config({"max_concurrency": concurrency})
