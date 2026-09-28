from __future__ import annotations

from dataclasses import dataclass

from ecommerce_ai_skills.runtime.graph_engine import build_run_graph


@dataclass(frozen=True)
class Spec:
    name: str


def build_test_graph(
    specs: list[Spec],
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

    def specialist(spec: Spec) -> dict:
        assert set(started) == {item.name for item in specs}
        called.append(spec.name)
        if spec.name in failures:
            raise RuntimeError(f"{spec.name} failed")
        return {"agent": spec.name}

    def cross(findings: dict) -> dict:
        downstream.append("cross")
        assert set(findings) == {item.name for item in specs}
        return {"agent": "cross"}

    def manager(findings: dict) -> dict:
        downstream.append("manager")
        return {"finding_names": sorted(findings)}

    def reviewer(findings: dict, report: dict) -> dict:
        downstream.append("reviewer")
        return {"finding_names": sorted(findings), "report": report}

    graph = build_run_graph(
        initial_specs=specs,
        cross_spec=cross_spec,
        start_agent_task=start,
        run_specialist=specialist,
        run_cross=cross,
        run_manager=manager,
        run_reviewer=reviewer,
        max_workers=20,
    )
    return graph, started, called, downstream


def test_compiled_graph_has_all_execution_nodes() -> None:
    specs = [Spec("evidence_analyst"), Spec("platform_amazon_operator")]
    graph, _, _, _ = build_test_graph(specs, cross_spec=None)

    assert set(graph.get_graph().nodes) >= {
        "__start__",
        "__end__",
        "dispatcher",
        "evidence_analyst",
        "specialist",
        "specialist_barrier",
        "cross_platform_controller",
        "store_manager",
        "operations_reviewer",
    }


def test_send_fans_out_all_initial_agents_before_provider_work() -> None:
    specs = [
        Spec("evidence_analyst"),
        Spec("platform_amazon_operator"),
        Spec("platform_shopify_operator"),
    ]
    cross_spec = Spec("cross_platform_controller")
    graph, started, called, downstream = build_test_graph(
        specs,
        cross_spec=cross_spec,
    )

    result = graph.invoke({"findings": {}, "failure": None})

    assert started == [spec.name for spec in specs]
    assert set(called) == set(started)
    assert downstream == ["cross", "manager", "reviewer"]
    assert set(result["findings"]) == {*started, cross_spec.name}


def test_single_marketplace_skips_cross_platform_controller() -> None:
    specs = [Spec("evidence_analyst"), Spec("platform_amazon_operator")]
    graph, _, _, downstream = build_test_graph(specs, cross_spec=None)

    result = graph.invoke({"findings": {}, "failure": None})

    assert downstream == ["manager", "reviewer"]
    assert set(result["findings"]) == {spec.name for spec in specs}
    assert "cross_platform_controller" not in result["review"]["finding_names"]


def test_specialist_failure_waits_for_fanout_and_routes_to_end() -> None:
    specs = [
        Spec("evidence_analyst"),
        Spec("platform_amazon_operator"),
        Spec("platform_shopify_operator"),
    ]
    graph, started, called, downstream = build_test_graph(
        specs,
        cross_spec=Spec("cross_platform_controller"),
        failing={"platform_amazon_operator"},
    )

    result = graph.invoke({"findings": {}, "failure": None})

    assert started == [spec.name for spec in specs]
    assert set(called) == set(started)
    assert isinstance(result["failure"], RuntimeError)
    assert downstream == []
    assert "report" not in result
    assert "review" not in result
