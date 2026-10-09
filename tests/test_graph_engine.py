from __future__ import annotations

from dataclasses import dataclass

from ecommerce_ai_skills.runtime.graph_engine import build_run_graph


@dataclass(frozen=True)
class Spec:
    name: str
    platform: str = "cross_platform"


def build_test_graph(
    audit_spec: Spec,
    specialists: list[Spec],
    *,
    cross_spec: Spec | None,
    failing: set[str] | None = None,
):
    started: list[str] = []
    called: list[str] = []
    downstream: list[str] = []
    failures = failing or set()

    def start(spec: Spec) -> None:
        started.append(spec.name)

    def audit(spec: Spec) -> dict:
        called.append(spec.name)
        if spec.name in failures:
            raise RuntimeError(f"{spec.name} failed")
        return {"audit": spec.name}

    def specialist(spec: Spec, findings: dict) -> dict:
        assert set(started) == {audit_spec.name, *(item.name for item in specialists)}
        # The audit is a serial prerequisite: specialists plan against it.
        assert audit_spec.name in findings, "specialists must run after the audit"
        called.append(spec.name)
        if spec.name in failures:
            raise RuntimeError(f"{spec.name} failed")
        return {"agent": spec.name}

    def cross(findings: dict) -> dict:
        downstream.append("cross")
        assert {audit_spec.name, *(item.name for item in specialists)} <= set(findings)
        return {"agent": "cross"}

    def manager(findings: dict) -> dict:
        downstream.append("manager")
        return {"finding_names": sorted(findings)}

    def reviewer(findings: dict, report: dict) -> dict:
        downstream.append("reviewer")
        return {"finding_names": sorted(findings), "report": report}

    graph = build_run_graph(
        audit_spec=audit_spec,
        specialist_specs=specialists,
        cross_spec=cross_spec,
        start_agent_task=start,
        run_audit=audit,
        run_specialist=specialist,
        run_cross=cross,
        run_manager=manager,
        run_reviewer=reviewer,
        max_workers=20,
    )
    return graph, started, called, downstream


ANALYST = Spec("evidence_analyst")


def test_compiled_graph_has_all_execution_nodes() -> None:
    graph, _, _, _ = build_test_graph(
        ANALYST, [Spec("platform_amazon_operator", "amazon")], cross_spec=None
    )

    assert set(graph.get_graph().nodes) >= {
        "__start__",
        "__end__",
        "dispatcher",
        "evidence_analyst",
        "analyst_barrier",
        "specialist",
        "specialist_barrier",
        "cross_platform_controller",
        "store_manager",
        "operations_reviewer",
    }


def test_audit_runs_before_specialists_and_all_agents_start_up_front() -> None:
    specialists = [
        Spec("platform_amazon_operator", "amazon"),
        Spec("platform_shopify_operator", "shopify"),
    ]
    cross_spec = Spec("cross_platform_controller")
    graph, started, called, downstream = build_test_graph(
        ANALYST, specialists, cross_spec=cross_spec
    )

    result = graph.invoke({"findings": {}, "failure": None})

    assert started == [ANALYST.name, *(spec.name for spec in specialists)]
    assert called[0] == ANALYST.name
    assert set(called) == set(started)
    assert downstream == ["cross", "manager", "reviewer"]
    assert set(result["findings"]) == {*started, cross_spec.name}


def test_single_marketplace_skips_cross_platform_controller() -> None:
    specialists = [Spec("platform_amazon_operator", "amazon")]
    graph, _, _, downstream = build_test_graph(ANALYST, specialists, cross_spec=None)

    result = graph.invoke({"findings": {}, "failure": None})

    assert downstream == ["manager", "reviewer"]
    assert "cross_platform_controller" not in result["review"]["finding_names"]


def test_specialist_failure_waits_for_fanout_and_routes_to_end() -> None:
    specialists = [
        Spec("platform_amazon_operator", "amazon"),
        Spec("platform_shopify_operator", "shopify"),
    ]
    graph, started, called, downstream = build_test_graph(
        ANALYST,
        specialists,
        cross_spec=Spec("cross_platform_controller"),
        failing={"platform_amazon_operator"},
    )

    result = graph.invoke({"findings": {}, "failure": None})

    assert set(called) == set(started)
    assert isinstance(result["failure"], RuntimeError)
    assert downstream == []
    assert "report" not in result
    assert "review" not in result


def test_audit_failure_stops_before_any_specialist_work() -> None:
    specialists = [Spec("platform_amazon_operator", "amazon")]
    graph, started, called, downstream = build_test_graph(
        ANALYST, specialists, cross_spec=None, failing={ANALYST.name}
    )

    result = graph.invoke({"findings": {}, "failure": None})

    assert started == [ANALYST.name, "platform_amazon_operator"]
    assert called == [ANALYST.name]
    assert isinstance(result["failure"], RuntimeError)
    assert downstream == []
