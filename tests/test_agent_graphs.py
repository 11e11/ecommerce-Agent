from __future__ import annotations

import copy
import json
import sqlite3
import threading
from email.message import Message
from pathlib import Path

import pytest

from ecommerce_ai_skills.runtime.agent_graphs import default_graph_definition
from ecommerce_ai_skills.runtime.agents import WeeklyOpsCouncil
from ecommerce_ai_skills.runtime.api import RuntimeApplication, _Handler
from ecommerce_ai_skills.runtime.errors import (
    AuthorizationError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from ecommerce_ai_skills.runtime.storage import Database


def evidence(*platforms: str) -> list[dict]:
    return [
        {
            "source_id": f"{platform}-source",
            "platform": platform,
            "source_type": f"{platform}_snapshot",
            "observed_at": "2026-08-26T09:00:00+08:00",
            "data": {"record_count": index + 1},
        }
        for index, platform in enumerate(platforms)
    ]


class GraphProvider:
    def __init__(self, verdict: str = "approved", *, bad_reviewer_ref: bool = False):
        self.verdict = verdict
        self.bad_reviewer_ref = bad_reviewer_ref
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def configuration(self):
        return "graph_fixture", "graph-fixture-v1"

    def complete(self, *, agent_name, instructions, payload, output_schema, safety_identifier):
        with self.lock:
            self.calls.append((agent_name, payload))
        if agent_name == "store_manager":
            source = payload["evidence_catalog"][0]
            platform = source["platform"]
            return {
                "executive_summary": "Review the evidence-bound operating priority.",
                "priorities": [
                    {
                        "rank": 1,
                        "title": "Review current performance",
                        "why_now": "The supplied source is current.",
                        "evidence_refs": [source["source_id"]],
                        "platforms": [platform],
                        "expected_impact": "Clarify the next operator decision.",
                        "confidence": "medium",
                        "recommended_owner": f"platform_{platform}_operator",
                        "downstream_action": "Prepare a proposal without applying it.",
                        "action_type": "external_change",
                        "requires_approval": True,
                        "metric_claim": {
                            "operation": (
                                "observe"
                                if source["source_type"] == "metric_observation"
                                else "none"
                            ),
                            "observation_refs": (
                                [source["source_id"]]
                                if source["source_type"] == "metric_observation"
                                else []
                            ),
                        },
                    }
                ],
                "risks": [],
                "limitations": ["Only supplied evidence was reviewed."],
            }
        if agent_name == "operations_reviewer":
            refs = [source["source_id"] for source in payload["evidence_catalog"]]
            if self.bad_reviewer_ref:
                refs = ["unknown-source"]
            issues = []
            if self.verdict != "approved":
                source = payload["evidence_catalog"][0]
                issues = [
                    {
                        "code": "needs_revision",
                        "message": "The manager must strengthen the limitation statement.",
                        "severity": "warning",
                        "evidence_refs": [source["source_id"]],
                        "platforms": [source["platform"]],
                    }
                ]
            return {
                "verdict": self.verdict,
                "revision_target": "manager" if self.verdict == "revision_required" else "none",
                "revision_platform": "",
                "issues": issues,
                "evidence_refs": refs,
                "limitations": [
                    *payload["manager_report"].get("limitations", []),
                    "Independent L7 review used only the evidence catalog.",
                ],
            }
        platform = payload["target_platform"]
        sources = payload.get("evidence") or []
        if sources:
            source_id = sources[0]["source_id"]
        else:
            source_id = next(
                finding["evidence_refs"][0]
                for value in payload["specialist_findings"].values()
                for finding in value["findings"]
            )
        return {
            "platform": platform,
            "summary": f"{agent_name} completed.",
            "findings": [
                {
                    "title": "Evidence-bound finding",
                    "severity": "info",
                    "confidence": "medium",
                    "evidence_refs": [source_id],
                    "recommendation": "Keep the decision behind operator review.",
                }
            ],
            "data_gaps": [],
        }


def test_schema_v16_graph_persistence_rbac_tenant_and_immutable_publish(tmp_path: Path) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    first = app.bootstrap("First", "owner@example.com")
    owner = app.auth.authenticate(first["api_key"])
    viewer_id = app.db.create_user(owner.tenant_id, "viewer@example.com", "viewer")
    viewer = app.db.principal_for_user(owner.tenant_id, viewer_id)

    graphs = app.agent_graphs.list(viewer)
    assert len(graphs) == 1 and graphs[0]["published_version_id"]
    default_version = app.agent_graphs.get_version(owner, graphs[0]["published_version_id"])
    assert default_version["status"] == "published"
    assert default_version["definition_hash"]
    assert len(default_version["execution_contract_hash"]) == 64
    assert {node["role"] for node in default_version["definition"]["nodes"]} == {
        "evidence_analyst", "platform_specialist", "cross_controller", "manager", "reviewer"
    }
    node_policies = {
        node["role"]: node["tool_policy"]
        for node in default_version["definition"]["nodes"]
    }
    assert node_policies["evidence_analyst"] == {
        "allowed_tools": [
            "opc.get_constraints", "opc.read_chapter", "opc.search_knowledge",
        ],
        "max_tool_calls": 6,
    }
    assert all(node_policies[role]["max_tool_calls"] == 6
               for role in ("platform_specialist", "cross_controller"))
    assert all(node_policies[role] == {"allowed_tools": [], "max_tool_calls": 0}
               for role in ("manager", "reviewer"))
    with pytest.raises(AuthorizationError):
        app.agent_graphs.create(viewer, "Viewer graph", default_graph_definition(), "viewer-create")

    created = app.agent_graphs.create(
        owner, "Second graph", default_graph_definition(), "graph-create"
    )
    graph_id = created["graph"]["id"]
    version_id = created["versions"][0]["id"]
    published = app.agent_graphs.publish(owner, graph_id, version_id, "graph-publish")
    assert published["status"] == "published"
    with app.db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE agent_graph_versions SET definition_json='{}' WHERE id=?",
                (version_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM agent_graph_versions WHERE id=?", (version_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE agent_graph_versions SET execution_contract_hash=? WHERE id=?",
                ("0" * 64, version_id),
            )

    second = app.bootstrap("Second", "other@example.com")
    outsider = app.auth.authenticate(second["api_key"])
    with pytest.raises(NotFoundError):
        app.agent_graphs.get_version(outsider, version_id)

    reloaded = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    assert reloaded.agent_graphs.get(owner, graph_id)["versions"][0]["definition_hash"] == published["definition_hash"]


def test_agent_child_rows_reject_cross_tenant_direct_writes(tmp_path: Path) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    first = app.bootstrap("First", "owner@example.com")
    second = app.bootstrap("Second", "other@example.com")
    owner = app.auth.authenticate(first["api_key"])
    outsider = app.auth.authenticate(second["api_key"])
    graph = app.agent_graphs.ensure_default(owner)
    run = app.agent_runs.request(owner, "weekly_ops", "优化 Amazon listing title; Cross tenant FK", evidence("amazon"), "fk", "fk", graph_version_id=graph["id"])
    with app.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="tenant ownership"):
        conn.execute(
            "UPDATE agent_runs SET requested_by=? WHERE id=?",
            (outsider.user_id, run["id"]),
        )
    with app.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="graph binding"):
        conn.execute(
            "UPDATE agent_runs SET graph_version_hash=NULL WHERE id=?",
            (run["id"],),
        )
    with app.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="tenant ownership"):
        conn.execute(
            "INSERT INTO agent_tasks(id,tenant_id,run_id,agent_name,skill_ids_json,status,created_at) VALUES(?,?,?,?,?,?,?)",
            ("bad-task", outsider.tenant_id, run["id"], "bad", "[]", "pending", "2026-08-26T00:00:00Z"),
        )
    with app.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="tenant ownership"):
        conn.execute(
            "INSERT INTO agent_artifacts(id,tenant_id,run_id,kind,attempt,content_json,created_at) VALUES(?,?,?,?,?,?,?)",
            ("bad-artifact", outsider.tenant_id, run["id"], "bad", 1, "{}", "2026-08-26T00:00:00Z"),
        )
    with app.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="tenant ownership"):
        conn.execute(
            "INSERT INTO agent_events(id,tenant_id,run_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            ("bad-event", outsider.tenant_id, run["id"], "bad", "{}", "2026-08-26T00:00:00Z"),
        )
    with app.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="tenant ownership"):
        conn.execute(
            """INSERT INTO agent_evaluations(
               id,tenant_id,run_id,evaluator_version,passed,score,details_json,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "bad-evaluation", outsider.tenant_id, run["id"], "bad", 0, 0.0, "{}",
                outsider.user_id, "2026-08-26T00:00:00Z",
            ),
        )


def test_v15_database_reopens_with_v16_tenant_integrity(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'production',
                created_at TEXT NOT NULL
            );
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id,email)
            );
            CREATE UNIQUE INDEX uq_users_tenant_id ON users(tenant_id,id);
            CREATE TABLE agent_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL,
                workflow TEXT NOT NULL,
                objective TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                platforms_json TEXT NOT NULL,
                requested_by TEXT NOT NULL REFERENCES users(id),
                status TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(tenant_id,idempotency_key)
            );
            CREATE TABLE agent_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                agent_name TEXT NOT NULL,
                skill_ids_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(run_id,agent_name)
            );
            CREATE TABLE runtime_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO tenants VALUES('legacy-tenant','Legacy','production','2026-08-25T00:00:00Z');
            INSERT INTO users VALUES('legacy-owner','legacy-tenant','owner@example.com','owner','2026-08-25T00:00:00Z');
            INSERT INTO agent_runs VALUES(
                'legacy-run','legacy-tenant','legacy-key','weekly_ops','Legacy run',
                '[{"source_id":"legacy-source","platform":"amazon","source_type":"legacy_snapshot","observed_at":"2026-08-25T00:00:00Z","data":{"value":1}}]',
                '["amazon"]',
                'legacy-owner','completed','fixture',NULL,1,NULL,
                '2026-08-25T00:00:00Z','2026-08-25T00:01:00Z','2026-08-25T00:01:00Z'
            );
            INSERT INTO agent_tasks VALUES(
                'legacy-task','legacy-tenant','legacy-run','store_manager','[]','completed',1,NULL,
                '2026-08-25T00:00:00Z','2026-08-25T00:00:10Z','2026-08-25T00:01:00Z'
            );
            INSERT INTO runtime_meta VALUES('schema_version','15');
            """
        )
    migrated = Database(path)
    with migrated.connect() as conn:
        assert conn.execute("SELECT value FROM runtime_meta WHERE key='schema_version'").fetchone()["value"] == "22"
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"uq_agent_runs_tenant_id", "uq_agent_tasks_tenant_id", "uq_agent_evaluations_tenant_id"} <= names
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_runs)")}
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_tasks)")}
        assert {
            "graph_version_id", "graph_version_hash", "metric_observation_ids_json",
            "review_status", "origin", "parent_daily_ops_run_id",
            "parent_daily_ops_attempt", "parent_daily_ops_lease_token",
        } <= run_columns
        assert {"graph_node_key", "role", "tool_policy_json"} <= task_columns
        legacy = conn.execute(
            "SELECT review_status,origin FROM agent_runs WHERE id='legacy-run'"
        ).fetchone()
        assert legacy["review_status"] == "pending"
        assert legacy["origin"] == "manual"
        triggers = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert {
            "agent_runs_graph_binding_insert",
            "agent_runs_tenant_ownership_insert",
            "agent_tasks_tenant_ownership_insert",
            "agent_artifacts_tenant_ownership_insert",
            "agent_events_tenant_ownership_insert",
            "agent_evaluations_tenant_ownership_insert",
        } <= triggers
        # Even a tampered legacy status and historical report cannot satisfy
        # the graph + Reviewer lineage gate introduced by L7.
        conn.execute(
            "UPDATE agent_runs SET review_status='approved' WHERE id='legacy-run'"
        )
        conn.execute(
            """INSERT INTO agent_artifacts(
               id,tenant_id,run_id,task_id,kind,attempt,content_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "legacy-report", "legacy-tenant", "legacy-run", "legacy-task",
                "weekly_ops_report", 1,
                json.dumps(
                    {
                        "executive_summary": "Legacy",
                        "priorities": [
                            {
                                "rank": 1,
                                "title": "Must not surface",
                                "why_now": "No Reviewer exists.",
                                "evidence_refs": ["legacy-source"],
                                "platforms": ["amazon"],
                                "expected_impact": "none",
                                "confidence": "low",
                                "recommended_owner": "human_operator",
                                "downstream_action": "Review only.",
                                "requires_approval": True,
                                "action_type": "analysis",
                                "metric_claim": {
                                    "operation": "none", "observation_refs": []
                                },
                            }
                        ],
                        "risks": [],
                        "limitations": ["Pre-L7 run."],
                    }
                ),
                "2026-08-25T00:01:00Z",
            ),
        )

    app = RuntimeApplication(migrated, agent_provider=GraphProvider())
    owner = migrated.principal_for_user("legacy-tenant", "legacy-owner")
    assert app.briefing.get(owner, "amazon")["priorities"] == []
    assert app.evaluator.evaluate(owner, "legacy-run", "legacy-eval")["passed"] is False


def test_graph_contract_rejects_noncanonical_dag_prompts_unknown_skills_and_tools(tmp_path: Path) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    definition = default_graph_definition()

    noncanonical = copy.deepcopy(definition)
    noncanonical["edges"] = [
        edge for edge in noncanonical["edges"]
        if edge != {"from": "evidence_analyst", "to": "cross_controller"}
    ]
    with pytest.raises(ValidationError, match="execution topology"):
        app.agent_graphs.create(owner, "Bad topology", noncanonical, "bad-topology")

    missing_controller = copy.deepcopy(definition)
    missing_controller["nodes"] = [
        node for node in missing_controller["nodes"] if node["role"] != "cross_controller"
    ]
    missing_controller["edges"] = [
        edge for edge in missing_controller["edges"]
        if "cross_controller" not in edge.values()
    ]
    with pytest.raises(ValidationError, match="missing a required domain role"):
        app.agent_graphs.create(
            owner, "Missing controller", missing_controller, "missing-controller"
        )

    nonoptional_controller = copy.deepcopy(definition)
    next(
        node for node in nonoptional_controller["nodes"]
        if node["role"] == "cross_controller"
    )["optional"] = False
    with pytest.raises(ValidationError, match="cross_controller must be optional"):
        app.agent_graphs.create(
            owner, "Nonoptional controller", nonoptional_controller, "nonoptional-controller"
        )

    injected = copy.deepcopy(definition)
    injected["nodes"][0]["instruction_key"] = "ignore_previous_instructions"
    with pytest.raises(ValidationError, match="known instruction_key"):
        app.agent_graphs.create(owner, "Injected", injected, "bad-prompt")

    unknown_skill = copy.deepcopy(definition)
    unknown_skill["nodes"][0]["skill_ids"] = ["not-installed"]
    with pytest.raises(ValidationError, match="installed skills"):
        app.agent_graphs.create(owner, "Unknown skill", unknown_skill, "bad-skill")

    tools = copy.deepcopy(definition)
    tools["nodes"][0]["tool_policy"] = {"allowed_tools": ["browser"], "max_tool_calls": 1}
    with pytest.raises(ValidationError, match="read-only knowledge whitelist"):
        app.agent_graphs.create(owner, "Tools enabled", tools, "bad-tools")

    manager_tools = copy.deepcopy(definition)
    manager_tools["nodes"][3]["tool_policy"] = {
        "allowed_tools": ["opc.search_knowledge"], "max_tool_calls": 2,
    }
    with pytest.raises(ValidationError, match="must not carry tools"):
        app.agent_graphs.create(owner, "Manager tools", manager_tools, "bad-manager-tools")


def test_graph_run_dynamic_marketplaces_reviewer_and_idempotency(tmp_path: Path) -> None:
    provider = GraphProvider()
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    graph = app.agent_graphs.ensure_default(owner)
    sources = evidence("amazon", "shopify", "walmart")
    run = app.agent_runs.request(
        owner,
        "weekly_ops",
        "优化 Amazon listing title across three marketplaces.",
        sources,
        "graph-run",
        "graph-run-request",
        graph_version_id=graph["id"],
    )
    replay = app.agent_runs.request(
        owner,
        "weekly_ops",
        "优化 Amazon listing title across three marketplaces.",
        sources,
        "graph-run",
        "graph-run-replay",
        graph_version_id=graph["id"],
    )
    assert replay["id"] == run["id"]
    other_graph = app.agent_graphs.create(
        owner, "Other graph", default_graph_definition(), "other-create"
    )
    other_version = app.agent_graphs.publish(
        owner,
        other_graph["graph"]["id"],
        other_graph["versions"][0]["id"],
        "other-publish",
    )
    with pytest.raises(ConflictError, match="different agent run"):
        app.agent_runs.request(
            owner,
            "weekly_ops",
            "优化 Amazon listing title across three marketplaces.",
            sources,
            "graph-run",
            "graph-run-conflict",
            graph_version_id=other_version["id"],
        )

    bundle = app.agent_runs.execute(owner, run["id"], "execute")
    assert bundle["run"]["review_status"] == "approved"
    assert bundle["run"]["origin"] == "manual"
    assert bundle["run"]["parent_daily_ops_run_id"] is None
    assert bundle["run"]["graph_version_id"] == graph["id"]
    assert {task["agent_name"] for task in bundle["tasks"]} == {
        "evidence_analyst",
        "platform_amazon_operator",
        "platform_shopify_operator",
        "platform_walmart_operator",
        "cross_platform_controller",
        "store_manager",
        "operations_reviewer",
    }
    assert {task["role"] for task in bundle["tasks"]} == {
        "evidence_analyst", "platform_specialist", "cross_controller", "manager", "reviewer"
    }
    task_policies = {
        task["role"]: task["tool_policy"] for task in bundle["tasks"]
    }
    assert task_policies["evidence_analyst"]["max_tool_calls"] == 6
    assert set(task_policies["evidence_analyst"]["allowed_tools"]) == {
        "opc.search_knowledge", "opc.get_constraints", "opc.read_chapter",
    }
    assert all(task_policies[role]["max_tool_calls"] == 6
               for role in ("platform_specialist", "cross_controller"))
    assert all(task_policies[role] == {"allowed_tools": [], "max_tool_calls": 0}
               for role in ("manager", "reviewer"))
    call_names = [name for name, _ in provider.calls]
    assert call_names[-1] == "operations_reviewer"
    cross_index = call_names.index("cross_platform_controller")
    assert all(
        call_names.index(name) < cross_index
        for name in {
            "evidence_analyst",
            "platform_amazon_operator",
            "platform_shopify_operator",
            "platform_walmart_operator",
        }
    )
    assert cross_index < call_names.index("store_manager")
    assert call_names.index("store_manager") < call_names.index("operations_reviewer")
    reviewer_payload = next(
        payload for name, payload in provider.calls if name == "operations_reviewer"
    )
    assert reviewer_payload["evidence"] == sources
    assert {
        "evidence_analyst",
        "platform_amazon_operator",
        "platform_shopify_operator",
        "platform_walmart_operator",
        "cross_platform_controller",
    } <= set(reviewer_payload["specialist_findings"])
    assert {artifact["kind"] for artifact in bundle["artifacts"]} >= {
        "manager_synthesis", "reviewer_verdict", "weekly_ops_report"
    }
    evaluation = app.evaluator.evaluate(owner, run["id"], "eval")
    assert evaluation["passed"] is True
    assert next(
        item for item in evaluation["details"]["checks"] if item["name"] == "reviewer_approval"
    )["passed"] is True


def test_router_selects_multiple_skills_per_supported_platform_and_blocks_unknown(tmp_path: Path) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    run = app.agent_runs.request(
        owner, "weekly_ops", "优化 Amazon listing title 和 PPC 广告预算",
        evidence("amazon", "shopify"), "multi-route", "multi-route-request",
    )
    route = app.db.get_agent_route(owner.tenant_id, run["id"])
    assert route["skills"] == ["ecom-advertising", "ecom-listing"]
    assert route["by_platform"]["amazon"] == ["ecom-advertising", "ecom-listing"]
    assert route["by_platform"]["shopify"] == ["ecom-listing"]
    bundle = app.agent_runs.execute(owner, run["id"], "multi-route-execute")
    skills = {task["agent_name"]: task["skill_ids"] for task in bundle["tasks"]}
    assert skills["platform_amazon_operator"] == route["by_platform"]["amazon"]
    assert skills["platform_shopify_operator"] == route["by_platform"]["shopify"]
    assert app.db.get_agent_route(owner.tenant_id, run["id"]) == route
    with pytest.raises(ValidationError, match="Skill Router"):
        app.agent_runs.request(
            owner, "weekly_ops", "Unrelated internal placeholder task",
            evidence("amazon"), "unmatched-route", "unmatched-route-request",
        )
    with pytest.raises(ValidationError, match="insufficient constraint coverage"):
        app.agent_runs.request(
            owner, "weekly_ops", "Amazon video-ad caption rules",
            evidence("amazon"), "low-coverage-route", "low-coverage-request",
        )


@pytest.mark.parametrize("target", ["platform_specialist", "cross_controller", "manager"])
def test_reviewer_directed_revision_replays_only_affected_nodes(tmp_path: Path, target: str) -> None:
    class RevisionProvider(GraphProvider):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        def complete(self, *, agent_name, instructions, payload, output_schema, safety_identifier):
            if agent_name == "operations_reviewer":
                self.verdict = "revision_required" if self.reviews == 0 else "approved"
                result = super().complete(
                    agent_name=agent_name, instructions=instructions, payload=payload,
                    output_schema=output_schema, safety_identifier=safety_identifier,
                )
                self.reviews += 1
                result["revision_target"] = target if self.verdict == "revision_required" else "none"
                result["revision_platform"] = "amazon" if target == "platform_specialist" and self.verdict == "revision_required" else ""
                return result
            return super().complete(
                agent_name=agent_name, instructions=instructions, payload=payload,
                output_schema=output_schema, safety_identifier=safety_identifier,
            )

    provider = RevisionProvider()
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    run = app.agent_runs.request(
        owner, "weekly_ops", "优化 Amazon listing title",
        evidence("amazon", "shopify"), f"revise-{target}", f"revise-{target}-request",
    )
    bundle = app.agent_runs.execute(owner, run["id"], f"revise-{target}-execute")
    assert bundle["run"]["review_status"] == "approved"
    attempts = {task["agent_name"]: task["attempt_count"] for task in bundle["tasks"]}
    assert attempts["operations_reviewer"] == attempts["store_manager"] == 2
    assert attempts["platform_shopify_operator"] == 1
    assert attempts["platform_amazon_operator"] == (2 if target == "platform_specialist" else 1)
    assert attempts["cross_platform_controller"] == (1 if target == "manager" else 2)
    revised_target = {
        "platform_specialist": "platform_amazon_operator",
        "cross_controller": "cross_platform_controller",
        "manager": "store_manager",
    }[target]
    revised_payloads = [payload for name, payload in provider.calls if name == revised_target]
    assert "revision_feedback" in revised_payloads[-1]
    assert app.evaluator.evaluate(owner, run["id"], f"revise-{target}-eval")["passed"] is True


def test_run_fails_closed_when_installed_execution_contract_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    graph = app.agent_graphs.ensure_default(owner)
    run = app.agent_runs.request(
        owner,
        "weekly_ops",
        "优化 Amazon listing title under the installed execution contract.",
        evidence("amazon"),
        "execution-contract-run",
        "execution-contract-request",
        graph_version_id=graph["id"],
    )
    monkeypatch.setattr(app.agent_graphs, "execution_contract_hash", lambda: "0" * 64)
    with pytest.raises(ConflictError, match="execution contract changed after"):
        app.agent_runs.execute(owner, run["id"], "execution-contract-execute")
    assert app.agent_runs.get(owner, run["id"])["run"]["status"] == "failed"
    with pytest.raises(ConflictError, match="execution contract changed"):
        app.agent_runs.request(
            owner,
            "weekly_ops",
            "Do not bind a new run to a stale published graph.",
            evidence("amazon"),
            "execution-contract-stale-request",
            "execution-contract-stale-request",
            graph_version_id=graph["id"],
        )


def _metric_import(app: RuntimeApplication, owner, name: str, currency: str):
    imported = app.evidence_imports.import_csv(
        owner,
        raw=(
            "ASIN,Sessions,Units Ordered,Ordered Product Sales,Currency Code\n"
            f"A1,20,2,100.00,{currency}\n"
        ).encode(),
        platform="amazon",
        report_type="amazon_business_report",
        filename=f"{name}.csv",
        observed_at="2026-08-26T09:00:00+08:00",
        idempotency_key=f"import-{name}",
        request_id=f"import-{name}",
    )
    app.metric_observations.materialize(
        owner, imported["id"], f"metrics-{name}", f"metrics-{name}"
    )
    observations = app.metric_observations.list_observations(
        owner, evidence_import_id=imported["id"], metric_key="revenue"
    )["observations"]
    return observations[0]


def test_metric_observations_are_tenant_safe_bounded_and_currency_isolated(tmp_path: Path) -> None:
    provider = GraphProvider()
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    usd = _metric_import(app, owner, "usd", "USD")
    eur = _metric_import(app, owner, "eur", "EUR")
    run = app.agent_runs.request(
        owner,
        "weekly_ops",
        "优化 Amazon listing title using normalized metrics without cross-currency aggregation.",
        None,
        "metric-run",
        "metric-request",
        metric_observation_ids=[usd["id"], eur["id"]],
    )
    assert run["metric_observation_ids"] == [usd["id"], eur["id"]]
    bundle = app.agent_runs.execute(owner, run["id"], "metric-execute")
    amazon_payload = next(
        payload for name, payload in provider.calls if name == "platform_amazon_operator"
    )
    snapshots = amazon_payload["evidence"]
    assert len(snapshots) == 2
    assert {source["data"]["currency"] for source in snapshots} == {"USD", "EUR"}
    assert all(source["source_type"] == "metric_observation" for source in snapshots)
    assert all(len(json.dumps(source).encode()) < 50_000 for source in snapshots)
    assert bundle["run"]["review_status"] == "approved"

    with app.db.transaction() as conn:
        row = conn.execute(
            """SELECT id,content_json FROM agent_artifacts
               WHERE run_id=? AND kind='weekly_ops_report' AND attempt=?""",
            (run["id"], bundle["run"]["attempt_count"]),
        ).fetchone()
        report = json.loads(row["content_json"])
        report["priorities"][0]["evidence_refs"] = [
            source["source_id"] for source in snapshots
        ]
        report["priorities"][0]["metric_claim"] = {
            "operation": "observe",
            "observation_refs": [source["source_id"] for source in snapshots],
        }
        conn.execute(
            "UPDATE agent_artifacts SET content_json=? WHERE id=?",
            (json.dumps(report), row["id"]),
        )
    assert app.briefing.get(owner, "amazon")["priorities"] == []

    with pytest.raises(ValidationError, match="reserved for tenant-owned"):
        app.agent_runs.request(
            owner,
            "weekly_ops",
            "Reject an inline source pretending to be a normalized metric.",
            [
                {
                    "source_id": "metric_observation:forged",
                    "platform": "amazon",
                    "source_type": "metric_observation",
                    "observed_at": "2026-08-26T00:00:00Z",
                    "data": {"value_decimal": "999999"},
                }
            ],
            "forged-metric",
            "forged-metric",
        )

    outsider = app.auth.authenticate(app.bootstrap("B", "other@example.com")["api_key"])
    with pytest.raises(NotFoundError):
        app.agent_runs.request(
            outsider,
            "weekly_ops",
            "Try to read another tenant metric observation.",
            None,
            "cross-tenant-metric",
            "cross-tenant-metric",
            metric_observation_ids=[usd["id"]],
        )


def test_reviewer_revision_and_failure_are_persisted_and_not_consumed(tmp_path: Path) -> None:
    app = RuntimeApplication(
        Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider("revision_required")
    )
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    run = app.agent_runs.request(
        owner, "weekly_ops", "优化 Amazon listing title and review this source independently.",
        evidence("amazon"), "revision-run", "revision-request",
    )
    bundle = app.agent_runs.execute(owner, run["id"], "revision-execute")
    assert bundle["run"]["status"] == "completed"
    assert bundle["run"]["review_status"] == "revision_required"
    assert next(task for task in bundle["tasks"] if task["role"] == "reviewer")["attempt_count"] == 2
    assert app.briefing.get(owner, "amazon")["priorities"] == []
    evaluation = app.evaluator.evaluate(owner, run["id"], "revision-eval")
    assert evaluation["passed"] is False

    failing = RuntimeApplication(
        Database(tmp_path / "failure.sqlite"),
        agent_provider=GraphProvider(bad_reviewer_ref=True),
    )
    failing_owner = failing.auth.authenticate(
        failing.bootstrap("A", "owner@example.com")["api_key"]
    )
    failed_run = failing.agent_runs.request(
        failing_owner, "weekly_ops", "优化 Amazon listing title and reject an invalid citation.",
        evidence("amazon"), "failed-review", "failed-review-request",
    )
    with pytest.raises(ExternalServiceError, match="reviewer cited unknown evidence"):
        failing.agent_runs.execute(failing_owner, failed_run["id"], "failed-review-execute")
    failed = failing.agent_runs.get(failing_owner, failed_run["id"])
    assert failed["run"]["status"] == "failed"
    assert failed["run"]["review_status"] == "pending"
    reviewer = next(task for task in failed["tasks"] if task["role"] == "reviewer")
    assert reviewer["status"] == "failed"

    with pytest.raises(ExternalServiceError, match="must contain an issue"):
        WeeklyOpsCouncil._validate_reviewer(
            {
                "verdict": "rejected",
                "revision_target": "none",
                "revision_platform": "",
                "issues": [],
                "evidence_refs": ["amazon-source"],
                "limitations": ["The report is not safe to consume."],
            },
            {"amazon-source": "amazon"},
            {"priorities": [], "risks": [], "limitations": []},
        )


def test_retry_consumes_only_final_reviewer_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    run = app.agent_runs.request(
        owner,
        "weekly_ops",
        "优化 Amazon listing title; retry after Reviewer finalization failed.",
        evidence("amazon"),
        "reviewer-retry",
        "reviewer-retry-request",
    )
    original = app.db.complete_agent_run
    calls = 0

    def fail_first_finalization(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient finalization failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(app.db, "complete_agent_run", fail_first_finalization)
    with pytest.raises(ExternalServiceError, match="workflow execution failed"):
        app.agent_runs.execute(owner, run["id"], "reviewer-retry-first")
    monkeypatch.setattr(app.db, "complete_agent_run", original)
    completed = app.agent_runs.execute(owner, run["id"], "reviewer-retry-second")
    attempt = completed["run"]["attempt_count"]
    assert attempt == 2
    reviewer_artifacts = [
        artifact for artifact in completed["artifacts"]
        if artifact["kind"] == "reviewer_verdict"
    ]
    assert [artifact["attempt"] for artifact in reviewer_artifacts] == [1, 2]
    assert app.briefing.get(owner, "amazon")["brief_run_id"] == run["id"]
    assert app.evaluator.evaluate(owner, run["id"], "reviewer-retry-eval")["passed"] is True


def test_retry_before_reviewer_uses_reviewer_task_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = GraphProvider()
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap("A", "owner@example.com")["api_key"])
    run = app.agent_runs.request(
        owner,
        "weekly_ops",
        "优化 Amazon listing title; retry after Manager fails.",
        evidence("amazon"),
        "pre-reviewer-retry",
        "pre-reviewer-retry-request",
    )
    original = provider.complete
    failed_manager = False

    def fail_manager_once(**kwargs):
        nonlocal failed_manager
        if kwargs["agent_name"] == "store_manager" and not failed_manager:
            failed_manager = True
            raise ExternalServiceError("transient manager failure")
        return original(**kwargs)

    monkeypatch.setattr(provider, "complete", fail_manager_once)
    with pytest.raises(ExternalServiceError, match="transient manager failure"):
        app.agent_runs.execute(owner, run["id"], "pre-reviewer-retry-first")
    completed = app.agent_runs.execute(owner, run["id"], "pre-reviewer-retry-second")
    reviewer = next(task for task in completed["tasks"] if task["role"] == "reviewer")
    assert completed["run"]["attempt_count"] == 2
    assert reviewer["attempt_count"] == 1
    assert app.briefing.get(owner, "amazon")["brief_run_id"] == run["id"]
    assert app.evaluator.evaluate(
        owner, run["id"], "pre-reviewer-retry-eval"
    )["passed"] is True


def test_manager_and_reviewer_deterministic_safety_controls() -> None:
    priority = {
        "rank": 1,
        "title": "Unsafe change",
        "why_now": "A source was supplied.",
        "evidence_refs": ["source-a"],
        "platforms": ["amazon"],
        "expected_impact": "Unknown",
        "confidence": "low",
        "recommended_owner": "human_operator",
        "downstream_action": "Adjust campaign bid now.",
        "action_type": "analysis",
        "requires_approval": False,
        "metric_claim": {"operation": "none", "observation_refs": []},
    }
    manager = {
        "executive_summary": "Unsafe",
        "priorities": [priority],
        "risks": [
            {
                "risk": "A second source carries risk.",
                "mitigation": "Review it.",
                "evidence_refs": ["source-b"],
                "platforms": ["amazon"],
                "metric_claim": {"operation": "none", "observation_refs": []},
            }
        ],
        "limitations": ["Keep this limitation verbatim."],
    }
    with pytest.raises(ExternalServiceError, match="must require human approval"):
        WeeklyOpsCouncil._validate_refs(
            manager,
            {"source-a": "amazon", "source-b": "amazon"},
            manager=True,
            valid_owners={"human_operator"},
        )

    approved = {
        "verdict": "approved",
        "revision_target": "none",
        "revision_platform": "",
        "issues": [],
        "evidence_refs": ["source-a"],
        "limitations": ["Keep this limitation verbatim."],
    }
    with pytest.raises(ExternalServiceError, match="omitted manager evidence"):
        WeeklyOpsCouncil._validate_reviewer(
            approved,
            {"source-a": "amazon", "source-b": "amazon"},
            manager,
        )
    approved["evidence_refs"] = ["source-a", "source-b"]
    approved["limitations"] = []
    with pytest.raises(ExternalServiceError, match="omitted a manager limitation"):
        WeeklyOpsCouncil._validate_reviewer(
            approved,
            {"source-a": "amazon", "source-b": "amazon"},
            manager,
        )

    metric_manager = copy.deepcopy(manager)
    metric_manager["priorities"][0].update(
        {
            "downstream_action": "Review the metric relationship.",
            "action_type": "analysis",
            "requires_approval": False,
            "evidence_refs": ["metric-usd", "metric-eur"],
            "metric_claim": {
                "operation": "compare",
                "observation_refs": ["metric-usd", "metric-eur"],
            },
        }
    )
    metric_manager["risks"] = []
    metric_evidence = [
        {
            "source_id": source_id,
            "platform": "amazon",
            "source_type": "metric_observation",
            "observed_at": "2026-08-26T00:00:00Z",
            "data": {
                "metric_key": "revenue",
                "unit": "currency",
                "currency": currency,
                "dimensions": {},
                "time_grain": "day",
                "period_start": "2026-08-25T00:00:00Z",
                "period_end": "2026-08-26T00:00:00Z",
            },
        }
        for source_id, currency in (("metric-usd", "USD"), ("metric-eur", "EUR"))
    ]
    with pytest.raises(ExternalServiceError, match="mixes currency"):
        WeeklyOpsCouncil._validate_manager_metric_claims(
            metric_manager, metric_evidence
        )
    metric_manager["priorities"][0]["metric_claim"]["operation"] = "observe"
    with pytest.raises(ExternalServiceError, match="exactly one observation"):
        WeeklyOpsCouncil._validate_manager_metric_claims(
            metric_manager, metric_evidence
        )
    with pytest.raises(ExternalServiceError, match="issue values"):
        WeeklyOpsCouncil._validate_reviewer(
            {
                "verdict": "revision_required",
                "revision_target": "manager",
                "revision_platform": "",
                "issues": [
                    {
                        "code": "INVALID CODE",
                        "message": "Fix the report.",
                        "severity": "warning",
                        "evidence_refs": ["amazon-source"],
                        "platforms": ["amazon"],
                    }
                ],
                "evidence_refs": ["amazon-source"],
                "limitations": ["The report needs revision."],
            },
            {"amazon-source": "amazon"},
            {"priorities": [], "risks": [], "limitations": []},
        )


def test_v15_pending_run_migrates_and_binds_default_before_execute(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    app = RuntimeApplication(Database(path), agent_provider=GraphProvider())
    bootstrap = app.bootstrap("A", "owner@example.com")
    owner = app.auth.authenticate(bootstrap["api_key"])
    run = app.agent_runs.request(
        owner, "weekly_ops", "优化 Amazon listing title in this migrated pending run.",
        evidence("amazon"), "legacy-pending", "legacy-request",
    )
    with app.db.transaction() as conn:
        conn.execute(
            """UPDATE agent_runs
               SET graph_version_id=NULL,graph_version_hash=NULL,review_status='approved'
               WHERE id=?""",
            (run["id"],),
        )
        conn.execute("UPDATE runtime_meta SET value='15' WHERE key='schema_version'")

    migrated = RuntimeApplication(Database(path), agent_provider=GraphProvider())
    migrated_owner = migrated.auth.authenticate(bootstrap["api_key"])
    completed = migrated.agent_runs.execute(migrated_owner, run["id"], "legacy-execute")
    assert completed["run"]["status"] == "completed"
    assert completed["run"]["graph_version_id"]
    assert completed["run"]["graph_version_hash"]
    assert completed["run"]["review_status"] == "approved"


def test_agent_graph_api_routes_and_run_fields(tmp_path: Path) -> None:
    app = RuntimeApplication(Database(tmp_path / "runtime.sqlite"), agent_provider=GraphProvider())
    bootstrap = app.bootstrap("A", "owner@example.com")

    class Handler(_Handler):
        def __init__(self, method: str, path: str, body=None):
            self.path = path
            self.headers = Message()
            self.headers["Authorization"] = f"Bearer {bootstrap['api_key']}"
            self.headers["Idempotency-Key"] = "graph-api"
            self.body = body or {}
            self.method = method
            self.out = None

        @property
        def app(self):
            return app

        def _body(self):
            return self.body

        def _json(self, status, value, request_id, **kwargs):
            self.out = (status, value)

        def run(self):
            getattr(self, f"do_{self.method}")()
            return self.out

    listed = Handler("GET", "/v1/agent-graphs").run()
    assert listed[0] == 200 and len(listed[1]["graphs"]) == 1
    graph_id = listed[1]["graphs"][0]["id"]
    detail = Handler("GET", f"/v1/agent-graphs/{graph_id}").run()
    version_id = detail[1]["versions"][0]["id"]
    assert detail[1]["versions"][0]["definition_hash"]
    assert Handler("GET", f"/v1/agent-graph-versions/{version_id}").run()[0] == 200
    created = Handler(
        "POST",
        "/v1/agent-graphs",
        {"name": "API graph", "definition": default_graph_definition()},
    ).run()
    assert created[0] == 201
    api_graph_id = created[1]["graph"]["id"]
    modified = default_graph_definition()
    modified["nodes"][0]["skill_ids"] = ["ecom-applicability", "ecom-listing"]
    draft = Handler(
        "POST",
        f"/v1/agent-graphs/{api_graph_id}/versions",
        {"definition": modified},
    ).run()
    assert draft[0] == 201 and draft[1]["status"] == "draft"
    published = Handler(
        "POST",
        f"/v1/agent-graph-versions/{draft[1]['id']}/publish",
        {},
    ).run()
    assert published[0] == 200 and published[1]["status"] == "published"
    run = Handler(
        "POST",
        "/v1/agent-runs",
        {
            "workflow": "weekly_ops",
                "objective": "优化 Amazon listing title with the selected graph version.",
            "evidence": evidence("amazon"),
            "graph_version_id": version_id,
            "metric_observation_ids": [],
        },
    ).run()
    assert run[0] == 200 and run[1]["graph_version_id"] == version_id
