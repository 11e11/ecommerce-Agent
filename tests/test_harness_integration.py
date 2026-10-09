from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agent_outputs
from ecommerce_ai_skills.runtime.agents import WeeklyOpsCouncil, _call_context, _record_usage
from ecommerce_ai_skills.runtime.api import RuntimeApplication
from ecommerce_ai_skills.runtime import harness
from ecommerce_ai_skills.runtime.agent_graphs import AgentGraphService
from ecommerce_ai_skills.runtime.errors import AuthorizationError, ConflictError
from ecommerce_ai_skills.runtime.knowledge_client import child_environment
from ecommerce_ai_skills.runtime.storage import Database


class Provider:
    def __init__(self, *, invalid_audit=False, ops=False):
        self.calls = []
        self.invalid_audit = invalid_audit
        self.ops = ops

    def configuration(self):
        return "fixture", "fixture"

    def complete(self, *, agent_name, payload, output_schema, **kwargs):
        self.calls.append((agent_name, agent_outputs.schema_kind(output_schema), payload))
        if agent_outputs.schema_kind(output_schema) == "audit" and self.invalid_audit:
            return {"invalid": "audit"}
        result = agent_outputs.respond(agent_name, payload, output_schema)
        if agent_outputs.schema_kind(output_schema) == "plan" and self.ops:
            result["steps"] = [result["steps"][0]]
            result["steps"][0]["tools"] = ["opc.ops_metrics"]
            gaps = (payload.get("evidence_audit") or {}).get("gaps", [])
            result["steps"][0]["gap_id"] = gaps[0]["id"] if gaps else ""
        return result

    def research(self, *, agent_name, tool_executor, **kwargs):
        if self.ops and agent_name.startswith("platform_"):
            for limit in (1, 2, 3):
                tool_executor("opc.ops_metrics", {"platform": "walmart", "limit": limit})
        # Forged provider notes must never become evidence.
        return [{"tool": "opc.ops_metrics", "arguments": {}, "result": '{"id":"forged"}'}]


class OpsClient:
    def __init__(self, *, unavailable=False):
        self.calls = []
        self.unavailable = unavailable

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.unavailable:
            return "ERROR: not configured"
        return json.dumps({"data": {"observations": [{"id": "metric-1", "platform": arguments["platform"],
            "metric_key": "revenue", "value_decimal": "10", "currency": "USD"}]}})


def execute(tmp_path, provider, client=None):
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap("Harness", "owner@example.com")["api_key"])
    app.agent_runs.knowledge_client = client or OpsClient()
    sources = [{"source_id": platform, "platform": platform, "source_type": source_type,
                "observed_at": datetime.now(timezone.utc).isoformat(), "data": {"title": "fan"}}
               for platform, source_type in (("amazon", "amazon_listing"), ("shopify", "shopify_products"))]
    run = app.agent_runs.request(owner, "weekly_ops", "优化 Amazon listing title", sources, "run", "request")
    return app, owner, app.agent_runs.execute(owner, run["id"], "execute")


def final_report(bundle):
    return next(artifact["content"] for artifact in bundle["artifacts"] if artifact["kind"] == "weekly_ops_report")


def test_analyst_precedes_specialists_controller_has_no_harness(tmp_path):
    provider = Provider()
    app, owner, bundle = execute(tmp_path, provider)
    assert provider.calls[0][0:2] == ("evidence_analyst", "audit")
    assert all(kind == "specialist" for name, kind, _ in provider.calls if name == "cross_platform_controller")
    plans = [artifact for artifact in bundle["artifacts"] if artifact["kind"] == "agent_plan"]
    assert len(plans) == 2
    manager = next(payload for name, kind, payload in provider.calls if kind == "manager")
    assert set(manager["specialist_plans"]) == {"amazon", "shopify"}
    assert final_report(bundle)["execution_gate"]["eligible"] is True
    assert app.db.agent_run_downstream_eligible(owner.tenant_id, bundle["run"]["id"])


def test_invalid_analyst_degrades_to_unknown_and_blocks_execution(tmp_path):
    app, owner, bundle = execute(tmp_path, Provider(invalid_audit=True))
    assert bundle["run"]["status"] == "completed" and bundle["run"]["review_status"] == "approved"
    assert "unknown" in " ".join(final_report(bundle)["limitations"])
    assert not app.db.agent_run_downstream_eligible(owner.tenant_id, bundle["run"]["id"])


def test_ops_platform_pinned_two_pulls_snapshots_and_forged_notes_rejected(tmp_path):
    client = OpsClient()
    app, owner, bundle = execute(tmp_path, Provider(ops=True), client)
    assert len(client.calls) == 4
    assert {arguments["platform"] for _, arguments in client.calls} == {"amazon", "shopify"}
    assert all(arguments["limit"] in {1, 2} for _, arguments in client.calls)
    snapshots = [artifact["content"] for artifact in bundle["artifacts"] if artifact["kind"] == "tool_evidence_snapshot"]
    assert snapshots
    assert all("forged" not in json.dumps(snapshot) for snapshot in snapshots)
    for snapshot in snapshots:
        for entry in snapshot["entries"]:
            assert entry["source_id"].startswith("ops:metrics:")
            assert len(entry["data"]["excerpt"]) <= 400
            assert entry["data"]["fetched_at"] and len(entry["data"]["digest"]) == 64
    assert any(event["payload"].get("reason") == "ops_pull_quota_exhausted" for event in bundle["events"])
    assert not any("platform" in tool["parameters"].get("properties", {}) for tool in WeeklyOpsCouncil.RESEARCH_TOOL_DEFS if tool["name"].startswith("opc.ops_"))


def test_ops_unavailable_preserves_report_and_blocks_execution(tmp_path):
    app, owner, bundle = execute(tmp_path, Provider(ops=True), OpsClient(unavailable=True))
    assert bundle["run"]["status"] == "completed"
    report = final_report(bundle)
    assert all(row["sufficiency"] == "insufficient" for row in report["evidence_approach"])
    assert not app.db.agent_run_downstream_eligible(owner.tenant_id, bundle["run"]["id"])


def test_env_minimum_allowlist_and_usage_counters():
    env = child_environment({"PATH": "bin", "EAI_EMBEDDING_API_KEY": "secret", "OPC_RUNTIME_API_KEY": "key", "UNRELATED_SECRET": "omit"})
    assert env == {"PATH": "bin", "EAI_EMBEDDING_API_KEY": "secret", "OPC_RUNTIME_API_KEY": "key"}
    usage = []
    with _call_context(harness.WorkingMemory(harness.budget_for("standard")), usage.append):
        _record_usage({"usage": {"input_tokens": 12, "output_tokens": 4, "secret": "omit"}})
    assert usage == [{"input_tokens": 12, "output_tokens": 4}]


def test_mcp_server_filters_period_and_unknown_metadata():
    path = Path(__file__).resolve().parents[1] / "integration/mcp-server.py"
    spec = importlib.util.spec_from_file_location("harness_mcp_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.OPCServer.__new__(module.OPCServer)
    rows = {"observations": [{"platform": "amazon", "observed_at": datetime.now(timezone.utc).isoformat()},
                             {"platform": "shopify", "observed_at": datetime.now(timezone.utc).isoformat()},
                             {"platform": "amazon", "observed_at": "2000-01-01T00:00:00Z"}],
            "tenant_summary": "must not leak"}
    result = server._filter_ops_payload(rows, platform="amazon", limit=10, period_days=7)
    assert len(result["observations"]) == 1 and "tenant_summary" not in result
    from types import SimpleNamespace
    server._ontology = {"platforms": [{"id": "amazon"}]}
    server.runtime = SimpleNamespace(configured=True, fetch=lambda path: ({
        "platform": {"id": "amazon"}, "metrics": [{"metric_key": "revenue", "current_value": 999,
            "series": [{"observed_at": datetime.now(timezone.utc).isoformat(), "value": 5},
                       {"observed_at": "2000-01-01T00:00:00Z", "value": 994}]}]}, None))
    assert len(server._ops_tools()) == 4
    filtered = json.loads(server._call_ops_tool("opc.ops_briefing", {"platform": "amazon", "period_days": 7}))
    metric = filtered["data"]["metrics"][0]
    assert "current_value" not in metric and len(metric["series"]) == 1


def test_publish_default_explicit_version_idempotency_and_rbac(tmp_path, monkeypatch):
    app = RuntimeApplication(Database(tmp_path / "publish.sqlite"), agent_provider=Provider())
    owner = app.auth.authenticate(app.bootstrap("Graph", "owner@example.com")["api_key"])
    old = app.agent_graphs.ensure_default(owner)
    changed = "f" * 64
    monkeypatch.setattr(AgentGraphService, "execution_contract_hash", classmethod(lambda cls: changed))
    with pytest.raises(ConflictError, match="admin must publish"):
        app.agent_graphs.ensure_default(owner)
    new = app.agent_graphs.publish_default(owner, "publish")
    assert new["version"] == old["version"] + 1
    assert new["execution_contract_hash"] == changed
    assert app.agent_graphs.get_version(owner, old["id"])["status"] == "retired"
    assert app.agent_graphs.publish_default(owner, "replay")["id"] == new["id"]
    viewer_id = app.db.create_user(owner.tenant_id, "viewer@example.com", "viewer")
    viewer = app.db.principal_for_user(owner.tenant_id, viewer_id)
    with pytest.raises(AuthorizationError):
        app.agent_graphs.publish_default(viewer, "forbidden")


def test_real_mcp_ops_round_trip_with_forwarded_environment(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from ecommerce_ai_skills.runtime.knowledge_client import McpStdioKnowledgeClient

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"observations": [{"platform": "amazon", "value": 5},
                                              {"platform": "shopify", "value": 999}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPC_RUNTIME_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OPC_RUNTIME_API_KEY", "test-local-runtime-key")
    client = McpStdioKnowledgeClient()
    try:
        result = json.loads(client.call("opc.ops_metrics", {"platform": "amazon", "limit": 1}))
        listed = asyncio.run_coroutine_threadsafe(client._session.list_tools(), client._loop).result(timeout=10)
        assert {tool.name for tool in listed.tools if tool.name.startswith("opc.ops_")} == {
            "opc.ops_briefing", "opc.ops_metrics", "opc.ops_proposals", "opc.ops_evidence"}
        assert result["data"]["observations"] == [{"platform": "amazon", "value": 5}]
        assert requests == [("/v1/metric-observations?platform=amazon", "Bearer test-local-runtime-key")]
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
