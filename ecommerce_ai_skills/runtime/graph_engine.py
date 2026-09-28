"""LangGraph execution engine for the versioned Weekly Ops graph contract.

This module owns scheduling only. Database state transitions, provider calls,
validation, audit, and graph-version enforcement remain in the control plane.
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


RunSpecialist = Callable[[Any], dict[str, Any]]
RunCross = Callable[[dict[str, dict[str, Any]]], dict[str, Any]]
RunManager = Callable[[dict[str, dict[str, Any]]], dict[str, Any]]
RunReviewer = Callable[
    [dict[str, dict[str, Any]], dict[str, Any]],
    dict[str, Any],
]


def build_run_graph(
    *,
    initial_specs: Sequence[Any],
    cross_spec: Any | None,
    start_agent_task: Callable[[Any], None],
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
    specs = tuple(initial_specs)
    concurrency = max(1, min(max_workers, 3))
    graph = StateGraph(WeeklyOpsState)

    def dispatch(_: WeeklyOpsState) -> dict[str, Any]:
        # Preserve the old attempt semantics: every parallel task becomes
        # running before any provider work is submitted.
        for spec in specs:
            start_agent_task(spec)
        return {}

    def fan_out(_: WeeklyOpsState) -> list[Send]:
        return [
            Send(
                "evidence_analyst"
                if getattr(spec, "name", None) == "evidence_analyst"
                else "specialist",
                {"spec": spec},
            )
            for spec in specs
        ]

    def run_initial(state: WeeklyOpsState) -> dict[str, Any]:
        spec = state["spec"]
        try:
            result = run_specialist(spec)
        except Exception as exc:
            # Parallel branches must all reach the super-step barrier before
            # the run fails, matching the former as_completed loop.
            return {"failure": exc}
        return {"findings": {spec.name: result}}

    def specialist_barrier(_: WeeklyOpsState) -> dict[str, Any]:
        return {}

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
    graph.add_node("evidence_analyst", run_initial)
    graph.add_node("specialist", run_initial)
    graph.add_node("specialist_barrier", specialist_barrier)
    graph.add_node("cross_platform_controller", run_cross_node)
    graph.add_node("store_manager", run_manager_node)
    graph.add_node("operations_reviewer", run_reviewer_node)

    graph.add_edge(START, "dispatcher")
    graph.add_conditional_edges(
        "dispatcher",
        fan_out,
        ["evidence_analyst", "specialist"],
    )
    graph.add_edge("evidence_analyst", "specialist_barrier")
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
