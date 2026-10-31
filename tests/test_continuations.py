from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ecommerce_ai_skills.runtime.api import RuntimeApplication
from ecommerce_ai_skills.runtime.continuations import MAX_CONTEXT_CHARS
from ecommerce_ai_skills.runtime.errors import ConflictError, NotFoundError, ValidationError
from ecommerce_ai_skills.runtime.storage import Database
from test_harness_integration import Provider, OpsClient


def setup_run(tmp_path):
    provider = Provider(ops=True)
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap("Continuation", "owner@example.test")["api_key"])
    app.agent_runs.knowledge_client = OpsClient()
    evidence = [{"source_id": p, "platform": p, "source_type": kind,
                 "observed_at": datetime.now(timezone.utc).isoformat(), "data": {"title": "sample"}}
                for p, kind in (("amazon", "amazon_listing"), ("shopify", "shopify_products"))]
    run = app.agent_runs.request(owner, "weekly_ops", "优化 Amazon listing title", evidence, "first", "request")
    bundle = app.agent_runs.execute(owner, run["id"], "execute")
    return app, owner, provider, bundle


def test_follow_up_reaudits_evidence_and_injects_bounded_platform_history(tmp_path):
    app, owner, provider, parent = setup_run(tmp_path)
    parent_id = parent["run"]["id"]
    replacement = dict(parent["run"]["evidence"][0]) | {"data": {"title": "updated"}}
    child = app.agent_runs.follow_up(owner, parent_id, "只检查规则差异，不改写正文", "follow", "follow", evidence=[replacement])
    assert child["id"] != parent_id
    assert child["evidence"][0]["data"] == {"title": "updated"}
    assert app.agent_runs.get(owner, parent_id)["run"] == parent["run"]
    context = app.db.get_continuation_context(owner.tenant_id, child["id"])
    assert context["parent_run_id"] == parent_id
    assert len(context["previous_findings"]) == 2
    assert len(json.dumps(context, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
    assert "ops:metrics:" not in json.dumps(child["evidence"])
    before = len(provider.calls)
    result = app.agent_runs.execute(owner, child["id"], "execute-child")
    calls = provider.calls[before:]
    assert calls[0][1] == "audit"
    for name, kind, payload in calls:
        if kind != "plan":
            continue
        history = payload["continuation_context"]
        platform = name.removeprefix("platform_").removesuffix("_operator")
        assert all(row["platform"] == platform for row in history["previous_findings"])
        assert history["feedback"] == "只检查规则差异，不改写正文"
    trace = app.agent_runs.trace(owner, child["id"])
    assert result["run"]["status"] == trace["status"] == "completed"
    assert any(row["kind"] == "context_assembly" for row in trace["artifacts"])
    assert any(row["kind"] == "specialist_working_memory" for row in trace["artifacts"])
    assert any(e["type"] == "task.tool_call" for e in trace["events"])


def test_follow_up_idempotency_includes_feedback(tmp_path):
    app, owner, _, parent = setup_run(tmp_path)
    args = (owner, parent["run"]["id"], "重新核查", "follow", "request")
    first = app.agent_runs.follow_up(*args)
    assert app.agent_runs.follow_up(*args)["id"] == first["id"]
    with pytest.raises(ConflictError):
        app.agent_runs.follow_up(owner, parent["run"]["id"], "另一条反馈", "follow", "request")


def test_follow_up_authorization_parent_state_and_input_validation(tmp_path):
    app, owner, _, parent = setup_run(tmp_path)
    other = app.auth.authenticate(app.bootstrap("Other", "other@example.test")["api_key"])
    with pytest.raises(NotFoundError):
        app.agent_runs.follow_up(other, parent["run"]["id"], "重新核查", "x", "x")
    with pytest.raises(NotFoundError):
        app.agent_runs.trace(other, parent["run"]["id"])
    with pytest.raises(ValidationError):
        app.agent_runs.follow_up(owner, parent["run"]["id"], "", "x", "x")
    pending = app.agent_runs.request(owner, "weekly_ops", "优化 Amazon listing title", parent["run"]["evidence"], "pending", "pending")
    with pytest.raises(ConflictError):
        app.agent_runs.follow_up(owner, pending["id"], "继续", "x", "x")


def test_third_turn_preserves_requests_without_recursive_context(tmp_path):
    app, owner, _, parent = setup_run(tmp_path)
    second = app.agent_runs.follow_up(owner, parent["run"]["id"], "不要改写正文", "second", "second")
    app.agent_runs.execute(owner, second["id"], "execute-second")
    third = app.agent_runs.follow_up(owner, second["id"], "只列证据", "third", "third")
    context = app.db.get_continuation_context(owner.tenant_id, third["id"])
    assert context["previous_requests"] == ["不要改写正文"]
    assert third["objective"] == parent["run"]["objective"] + "\n用户补充：只列证据"
    assert len(json.dumps(context, ensure_ascii=False)) <= MAX_CONTEXT_CHARS


def test_http_follow_up_and_trace_routes(tmp_path):
    from email.message import Message
    from legacy_http import _Handler

    app, owner, _, parent = setup_run(tmp_path)
    key = app.auth.issue_key(owner.tenant_id, owner.user_id)

    class Handler(_Handler):
        def __init__(self, path, body=None):
            self.path = path
            self.headers = Message()
            self.headers["Authorization"] = "Bearer " + key
            self.headers["Idempotency-Key"] = "http-follow-up"
            self.body = body or {}

        @property
        def app(self):
            return app

        def _body(self):
            return self.body

        def _json(self, status, value, request_id, **kwargs):
            self.out = (status, value)

    first = Handler(f"/v1/agent-runs/{parent['run']['id']}/follow-ups", {"feedback": "只检查规则"})
    first.do_POST()
    assert first.out[0] == 200 and first.out[1]["status"] == "requested"
    child = first.out[1]
    app.agent_runs.execute(owner, child["id"], "execute-http")
    trace = Handler(f"/v1/agent-runs/{child['id']}/trace")
    trace.do_GET()
    assert trace.out[0] == 200
    assert trace.out[1]["continuation"]["parent_run_id"] == parent["run"]["id"]
    invalid = Handler(f"/v1/agent-runs/{parent['run']['id']}/follow-ups", {"feedback": "继续", "unknown": True})
    invalid.do_POST()
    assert invalid.out[0] == 422
