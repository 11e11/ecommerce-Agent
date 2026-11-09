from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecommerce_ai_skills.demo_seed import DemoSeedProvider
from ecommerce_ai_skills.runtime.api import RuntimeApplication, create_app
from ecommerce_ai_skills.runtime.storage import Database


@pytest.fixture
def http_runtime(tmp_path):
    runtime = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=DemoSeedProvider(),
        rate_limit_per_minute=10000, mission_event_poll_seconds=0.01,
        mission_event_heartbeat_seconds=0.01, mission_event_max_lifetime_seconds=0.04)
    credentials = runtime.bootstrap("ASGI", "owner@example.test")
    app = create_app(runtime)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + credentials["api_key"]
        yield runtime, client, credentials


def test_all_documented_routes_registered_and_native_validation(http_runtime):
    runtime, client, _ = http_runtime
    assert isinstance(client.app, FastAPI)
    schema = client.get("/openapi.json").json()
    contract = yaml.safe_load(Path("openapi/runtime-api.yaml").read_text(encoding="utf-8"))
    for path, operations in contract["paths"].items():
        assert path in schema["paths"], path
        assert set(operations) & {"get", "post", "patch"} <= set(schema["paths"][path])
    assert schema["components"]["schemas"]["AgentRunRequest"]["additionalProperties"] is False
    assert client.get("/docs").status_code == 200
    invalid = client.post("/v1/agent-runs", json={"workflow": "weekly_ops", "objective": 123, "unexpected": "SECRET"})
    assert invalid.status_code == 422
    assert "SECRET" not in invalid.text
    assert invalid.headers["x-request-id"] == invalid.json()["request_id"]
    assert client.post("/v1/users", json={"email": "bad@example.test"}).status_code == 422
    assert client.get("/v1/missing").status_code == 404
    assert client.delete("/v1/me").status_code == 405


def test_every_get_route_dispatches_without_internal_errors(http_runtime):
    _, client, _ = http_runtime
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        if "get" not in methods or path == "/v1/mission-control/events":
            continue
        url = re.sub(r"\{[^}]+\}", "missing-id", path)
        response = client.get(url)
        assert response.status_code < 500, (url, response.status_code, response.text)


def test_mutation_routes_reject_incomplete_requests_without_internal_errors(http_runtime):
    _, client, _ = http_runtime
    for path, methods in client.get("/openapi.json").json()["paths"].items():
        if path == "/v1/api-keys/rotate":
            continue  # Rotation is a valid empty request and would revoke this test's key.
        for method in ("post", "patch"):
            if method not in methods:
                continue
            response = client.request(method.upper(), re.sub(r"\{[^}]+\}", "missing-id", path), json={})
            assert response.status_code < 500 and response.status_code != 401, (method, path, response.text)


def test_agent_run_follow_up_trace_and_cross_tenant_isolation(http_runtime):
    runtime, client, bootstrap = http_runtime
    sources = [{"source_id": "amazon", "platform": "amazon", "source_type": "amazon_listing",
                "observed_at": datetime.now(timezone.utc).isoformat(), "data": {"title": "sample"}}]
    response = client.post("/v1/agent-runs", headers={"Idempotency-Key": "first"}, json={
        "workflow": "weekly_ops", "objective": "优化 Amazon listing title", "evidence": sources, "revision_support": False})
    assert response.status_code == 200, response.text
    first = response.json()["id"]
    assert client.post(f"/v1/agent-runs/{first}/execute").json()["run"]["status"] == "completed"
    follow = client.post(f"/v1/agent-runs/{first}/follow-ups", headers={"Idempotency-Key": "second"}, json={"feedback": "只列规则，不改写正文"})
    assert follow.status_code == 200, follow.text
    second = follow.json()["id"]
    assert client.post(f"/v1/agent-runs/{second}/execute").status_code == 200
    trace = client.get(f"/v1/agent-runs/{second}/trace").json()
    assert trace["continuation"]["parent_run_id"] == first
    assert any(row["kind"] == "context_assembly" for row in trace["artifacts"])
    other = runtime.bootstrap("Other", "other@example.test")
    forbidden = client.get(f"/v1/agent-runs/{first}", headers={"Authorization": "Bearer " + other["api_key"]})
    assert forbidden.status_code == 404
    unauthenticated = client.get("/v1/me", headers={"Authorization": "invalid"})
    assert unauthenticated.status_code == 401
    principal = runtime.auth.authenticate(bootstrap["api_key"])
    viewer_id = runtime.db.create_user(principal.tenant_id, "viewer@example.test", "viewer")
    viewer_key = runtime.auth.issue_key(principal.tenant_id, viewer_id)
    assert client.post("/v1/users", json={"email": "new@example.test", "role": "viewer"},
                       headers={"Authorization": "Bearer " + viewer_key}).status_code == 403


def test_raw_upload_json_errors_size_limits_and_static_headers(http_runtime):
    _, client, _ = http_runtime
    response = client.post("/v1/evidence-imports", content=b"ASIN,Sessions,Units Ordered,Ordered Product Sales,Currency Code\nA,100,5,25,USD\n", headers={
        "Content-Type": "text/csv", "X-Evidence-Platform": "amazon", "X-Evidence-Type": "amazon_business_report",
        "X-Evidence-Filename": "sales.csv", "X-Evidence-Observed-At": datetime.now(timezone.utc).isoformat(),
        "Idempotency-Key": "upload"})
    assert response.status_code == 200, response.text
    assert client.get("/v1/evidence-imports").json()["imports"]
    assert client.post("/v1/users", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/v1/users", json=[]).status_code == 422
    assert client.post("/v1/users", content=b"x" * 1_000_001).status_code == 422
    for path in ("/app", "/app/app.js", "/app/styles.css", "/app/assets/icons/check.svg"):
        asset = client.get(path)
        assert asset.status_code == 200, path
        assert asset.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in asset.headers["content-security-policy"]
    assert client.get("/app/assets/../../api.py").status_code == 404


def test_sse_cursors_heartbeats_and_connection_cleanup(http_runtime):
    runtime, client, credentials = http_runtime
    runtime.db.create_job(credentials["tenant_id"], credentials["user_id"], "stream-job", "agent_run.execute", {"run_id": "demo"})
    stream = client.get("/v1/mission-control/events?after=0")
    assert stream.status_code == 200
    assert "text/event-stream" in stream.headers["content-type"]
    assert "mission.update" in stream.text
    assert "mission.reconnect" in stream.text
    assert runtime.mission_connections.snapshot()["global_active"] == 0
    assert client.get("/v1/mission-control/events?after=bad").status_code == 422
    assert client.get("/v1/mission-control/events?api_key=SECRET").status_code == 422
    assert client.get("/v1/mission-control/events?after=0&after=1").status_code == 422
    assert client.get("/v1/mission-control/events", headers=[("Last-Event-ID", "1"), ("Last-Event-ID", "2")]).status_code == 422
    for _ in range(runtime.mission_connections.tenant_limit):
        runtime.mission_connections.acquire(credentials["tenant_id"])
    try:
        rejected = client.get("/v1/mission-control/events")
        assert rejected.status_code == 429 and rejected.headers["Retry-After"]
    finally:
        for _ in range(runtime.mission_connections.tenant_limit):
            runtime.mission_connections.release(credentials["tenant_id"])
    assert runtime.mission_connections.snapshot()["global_active"] == 0


def test_xlsx_upload_uses_native_bounded_body(http_runtime):
    import io
    openpyxl = pytest.importorskip("openpyxl")
    _, client, _ = http_runtime
    workbook = openpyxl.Workbook()
    workbook.active.append(["ASIN", "Sessions", "Units Ordered", "Ordered Product Sales", "Currency Code"])
    workbook.active.append(["A", 100, 5, 25, "USD"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    response = client.post("/v1/evidence-imports", content=buffer.getvalue(), headers={
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "X-Evidence-Platform": "amazon", "X-Evidence-Type": "amazon_business_report",
        "X-Evidence-Filename": "sales.xlsx", "X-Evidence-Observed-At": datetime.now(timezone.utc).isoformat(),
        "Idempotency-Key": "upload-xlsx"})
    assert response.status_code == 200, response.text


def test_rate_limit_and_request_id(tmp_path):
    runtime = RuntimeApplication(Database(tmp_path / "limited.sqlite"), rate_limit_per_minute=1)
    with TestClient(create_app(runtime)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/me").status_code == 401
        limited = client.get("/v1/me", headers={"X-Request-ID": "test-id"})
        assert limited.status_code == 429
        assert limited.headers["Retry-After"]
        assert limited.headers["X-Request-ID"] == "test-id"


def test_sync_service_does_not_block_health_endpoint(http_runtime, monkeypatch):
    import threading

    runtime, _, bootstrap = http_runtime
    entered, release = threading.Event(), threading.Event()

    def slow_briefing(*args):
        entered.set()
        release.wait(timeout=5)
        return {"status": "finished"}

    monkeypatch.setattr(runtime.briefing, "get", slow_briefing)

    async def run():
        transport = httpx.ASGITransport(app=create_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            job = asyncio.create_task(client.get("/v1/briefing", headers={"Authorization": "Bearer " + bootstrap["api_key"]}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                assert (await asyncio.wait_for(client.get("/healthz"), timeout=1)).status_code == 200
            finally:
                release.set()
                await job

    asyncio.run(run())


def test_uvicorn_real_http_start_and_graceful_stop(http_runtime):
    import threading
    import time
    from ecommerce_ai_skills.runtime.api import build_server, serve

    runtime, _, credentials = http_runtime
    stop = threading.Event()
    server = build_server(runtime, "127.0.0.1", 0)
    failures = []

    def run():
        try:
            serve(runtime, httpd=server, stop_event=stop)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    response = client.get("/healthz", timeout=1)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, failures
                time.sleep(0.05)
            response = client.get("/v1/me", headers={"Authorization": "Bearer " + credentials["api_key"]})
            assert response.json()["tenant_id"] == credentials["tenant_id"]
    finally:
        stop.set()
        worker.join(timeout=10)
        server.server_close()
    assert not worker.is_alive()
    assert not failures
