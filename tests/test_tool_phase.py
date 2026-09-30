"""Tool-phase contract: graph-declared research tools executed over MCP.

Covers the three gates (graph policy, provider capability, knowledge client),
whitelist enforcement at the executor, result truncation, capability-negotiated
degradation, and one real stdio round-trip against the bundled MCP server.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ecommerce_ai_skills.runtime.api import RuntimeApplication
from ecommerce_ai_skills.runtime.agent_graphs import (
    AgentGraphService,
    default_graph_definition,
)
from ecommerce_ai_skills.runtime.errors import ValidationError
from ecommerce_ai_skills.runtime.knowledge_client import (
    MAX_TOOL_RESULT_CHARS,
    McpStdioKnowledgeClient,
)
from ecommerce_ai_skills.runtime.storage import Database


def _weekly_evidence() -> list[dict]:
    return [
        {
            "source_id": "shopify-products-2026-08-22",
            "platform": "shopify",
            "source_type": "shopify_products",
            "observed_at": "2026-08-22T09:00:00+08:00",
            "data": [{"id": "p-1", "title": "Neck Fan", "status": "active"}],
        },
        {
            "source_id": "ads-report-2026-w34",
            "platform": "amazon",
            "source_type": "amazon_ads_export",
            "observed_at": "2026-08-22T09:05:00+08:00",
            "data": [{"campaign": "launch", "spend": 120, "sales": 240}],
        },
    ]


class FakeKnowledgeClient:
    def __init__(self, *, big_result: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self._big_result = big_result

    def call(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        if self._big_result:
            return "x" * (MAX_TOOL_RESULT_CHARS + 1000)
        return f"knowledge-result-for-{name}"


class ResearchProvider:
    """Fixture provider: fixture-style complete() plus a research() phase that
    calls offered tools through the executor the council injected."""

    def __init__(self, *, tool_calls_before_stop: int = 1, force_tool: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.research_calls: list[tuple[str, list[str]]] = []
        self.lock = threading.Lock()
        self._tool_calls_before_stop = tool_calls_before_stop
        self._force_tool = force_tool

    def configuration(self):
        return "fixture", "fixture-model"

    def research(self, *, agent_name, research_brief, tools, tool_executor, max_tool_calls, safety_identifier):
        with self.lock:
            self.research_calls.append((agent_name, [tool["name"] for tool in tools]))
        notes = []
        for index in range(min(self._tool_calls_before_stop, max_tool_calls)):
            if not tools:
                break
            tool = tools[index % len(tools)]
            name = self._force_tool or tool["name"]
            arguments = {"query": "compliance rules"} if name.endswith("search_knowledge") else {}
            result = tool_executor(name, arguments)
            notes.append({"tool": name, "arguments": arguments, "result": result})
        return notes

    def complete(self, *, agent_name, instructions, payload, output_schema, safety_identifier):
        with self.lock:
            self.calls.append((agent_name, payload))
        if agent_name == "operations_reviewer":
            return {
                "verdict": "approved",
                "issues": [],
                "evidence_refs": [s["source_id"] for s in payload["evidence_catalog"]],
                "limitations": payload["manager_report"].get("limitations", []),
            }
        if agent_name == "store_manager":
            catalog = payload["evidence_catalog"]
            primary = catalog[0]["source_id"]
            return {
                "executive_summary": "Prioritize the evidence-backed campaign review.",
                "priorities": [
                    {
                        "rank": 1,
                        "title": "Review launch campaign efficiency",
                        "why_now": "The supplied weekly ads export shows current spend and sales.",
                        "evidence_refs": [primary],
                        "platforms": [catalog[0]["platform"]],
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
                "limitations": ["Only supplied evidence was reviewed."],
            }
        platform = payload["target_platform"]
        evidence = [
            source for source in payload.get("evidence", [])
            if platform == "cross_platform"
            or source["platform"] in {platform, "cross_platform"}
        ]
        source_id = evidence[0]["source_id"] if evidence else (
            payload["specialist_findings"]["evidence_analyst"]["findings"][0]["evidence_refs"][0]
        )
        return {
            "platform": platform,
            "summary": f"{agent_name} completed an evidence-bound review.",
            "findings": [
                {
                    "title": "Review current campaign",
                    "severity": "warning",
                    "confidence": "medium",
                    "evidence_refs": [source_id],
                    "recommendation": "Validate profitability before changing bids.",
                }
            ],
            "data_gaps": ["Order history was not supplied."],
        }


def _application(tmp_path: Path, provider) -> tuple[RuntimeApplication, object]:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    principal = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    return app, principal


def test_tool_enabled_analyst_runs_research_and_persists_notes(tmp_path: Path) -> None:
    provider = ResearchProvider()
    client = FakeKnowledgeClient()
    app, principal = _application(tmp_path, provider)
    app.agent_runs.knowledge_client = client

    run = app.agent_runs.request(
        principal, "weekly_ops",
        "Find the most important evidence-backed actions for this week.",
        _weekly_evidence(), "tool-run", "tool-request",
    )
    bundle = app.agent_runs.execute(principal, run["id"], "tool-execute")

    assert bundle["run"]["status"] == "completed"
    assert bundle["run"]["review_status"] == "approved"
    # The research phase ran exactly for the tool-enabled role, with the
    # graph-declared whitelist as the offered tool set.
    assert provider.research_calls == [(
        "evidence_analyst",
        ["opc.search_knowledge", "opc.get_constraints", "opc.read_chapter"],
    )]
    analyst_payload = next(
        payload for name, payload in provider.calls if name == "evidence_analyst"
    )
    assert analyst_payload["research_notes"][0]["result"] == "knowledge-result-for-opc.search_knowledge"
    # Strict roles never see a research phase or notes.
    specialist_payload = next(
        payload for name, payload in provider.calls if name == "platform_amazon_operator"
    )
    assert "research_notes" not in specialist_payload
    # Every invocation is audited as a bounded task event.
    events = app.db.list_agent_events(principal.tenant_id, run["id"])
    tool_events = [event for event in events if event["event_type"] == "task.tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["payload"]["tool"] == "opc.search_knowledge"
    assert tool_events[0]["payload"]["agent_name"] == "evidence_analyst"


def test_whitelist_refusal_is_audited_and_does_not_fail_the_run(tmp_path: Path) -> None:
    provider = ResearchProvider(force_tool="opc.ops_proposals")
    client = FakeKnowledgeClient()
    app, principal = _application(tmp_path, provider)
    app.agent_runs.knowledge_client = client

    run = app.agent_runs.request(
        principal, "weekly_ops",
        "Find the most important evidence-backed actions for this week.",
        _weekly_evidence(), "refuse-run", "refuse-request",
    )
    bundle = app.agent_runs.execute(principal, run["id"], "refuse-execute")

    assert bundle["run"]["status"] == "completed"
    events = app.db.list_agent_events(principal.tenant_id, run["id"])
    tool_events = [event for event in events if event["event_type"] == "task.tool_call"]
    assert tool_events and tool_events[0]["payload"].get("refused") is True
    analyst_payload = next(
        payload for name, payload in provider.calls if name == "evidence_analyst"
    )
    assert analyst_payload["research_notes"][0]["result"].startswith("ERROR:")


def test_research_results_are_truncated_before_reaching_a_prompt(tmp_path: Path) -> None:
    provider = ResearchProvider()
    app, principal = _application(tmp_path, provider)
    app.agent_runs.knowledge_client = FakeKnowledgeClient(big_result=True)

    run = app.agent_runs.request(
        principal, "weekly_ops",
        "Find the most important evidence-backed actions for this week.",
        _weekly_evidence(), "truncate-run", "truncate-request",
    )
    bundle = app.agent_runs.execute(principal, run["id"], "truncate-execute")

    assert bundle["run"]["status"] == "completed"
    analyst_payload = next(
        payload for name, payload in provider.calls if name == "evidence_analyst"
    )
    note_result = analyst_payload["research_notes"][0]["result"]
    assert len(note_result) == MAX_TOOL_RESULT_CHARS + len("\n...[truncated, 1000 chars omitted]")
    events = app.db.list_agent_events(principal.tenant_id, run["id"])
    tool_events = [event for event in events if event["event_type"] == "task.tool_call"]
    assert tool_events[0]["payload"].get("truncated") is True


def test_provider_without_research_capability_degrades_to_single_shot(tmp_path: Path) -> None:
    class SingleShotProvider(ResearchProvider):
        research = None  # capability negotiation sees None and degrades

    provider = SingleShotProvider()
    app, principal = _application(tmp_path, provider)
    app.agent_runs.knowledge_client = FakeKnowledgeClient()

    run = app.agent_runs.request(
        principal, "weekly_ops",
        "Find the most important evidence-backed actions for this week.",
        _weekly_evidence(), "degrade-run", "degrade-request",
    )
    bundle = app.agent_runs.execute(principal, run["id"], "degrade-execute")

    assert bundle["run"]["status"] == "completed"
    assert bundle["run"]["review_status"] == "approved"
    analyst_payload = next(
        payload for name, payload in provider.calls if name == "evidence_analyst"
    )
    assert "research_notes" not in analyst_payload
    events = app.db.list_agent_events(principal.tenant_id, run["id"])
    assert not [event for event in events if event["event_type"] == "task.tool_call"]


def test_graph_rejects_unbounded_or_empty_tool_policies() -> None:
    definition = default_graph_definition()

    over_budget = default_graph_definition()
    over_budget["nodes"][0]["tool_policy"]["max_tool_calls"] = 9
    with pytest.raises(ValidationError, match="between 1 and 8"):
        AgentGraphService.validate_definition(over_budget)

    zero_budget = default_graph_definition()
    zero_budget["nodes"][0]["tool_policy"]["max_tool_calls"] = 0
    with pytest.raises(ValidationError, match="between 1 and 8"):
        AgentGraphService.validate_definition(zero_budget)

    empty_tools = default_graph_definition()
    empty_tools["nodes"][0]["tool_policy"]["allowed_tools"] = []
    with pytest.raises(ValidationError, match="non-empty subset"):
        AgentGraphService.validate_definition(empty_tools)


def test_bundled_mcp_server_round_trip_over_stdio() -> None:
    dist = Path(__file__).resolve().parents[1] / "ecommerce_ai_skills" / "package_data" / "dist"
    client = McpStdioKnowledgeClient(dist_path=dist)
    try:
        result = client.call("opc.search_knowledge", {"query": "buy box"})
    finally:
        client.close()
    assert "buy box" in result.lower() or "chapter" in result.lower()
