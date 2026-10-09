"""Deterministic harness demo; no external model or platform connection."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecommerce_ai_skills.demo_seed import DemoSeedProvider
from ecommerce_ai_skills.runtime.api import RuntimeApplication
from ecommerce_ai_skills.runtime.storage import Database


class DemoProvider(DemoSeedProvider):
    def complete(self, *, output_schema, payload, **kwargs):
        result = super().complete(output_schema=output_schema, payload=payload, **kwargs)
        if "objective_restatement" in output_schema["required"]:
            result["steps"][0]["tools"] = ["opc.search_knowledge", "opc.ops_metrics"]
        return result

    def research(self, *, tool_executor, tools, agent_name, **kwargs):
        offered = {tool["name"] for tool in tools}
        if "opc.search_knowledge" in offered:
            tool_executor("opc.search_knowledge", {"query": "listing title"})
        if "opc.ops_metrics" in offered:
            tool_executor("opc.ops_metrics", {"limit": 1})
        return []


class DemoTools:
    def call(self, name, arguments):
        if name == "opc.ops_metrics":
            return json.dumps({"data": {"observations": [{"id": "demo-observation",
                "platform": arguments["platform"], "metric_key": "revenue", "value_decimal": "10"}]}})
        return json.dumps([{"id": "demo.rule", "title": "Explicit Demo rule",
                            "summary": "Use supplied product facts to review listing titles."}])


def main():
    with TemporaryDirectory(prefix="specialist-harness-demo-") as directory:
        app = RuntimeApplication(Database(Path(directory) / "demo.sqlite"), agent_provider=DemoProvider())
        owner = app.auth.authenticate(app.bootstrap("Harness demo", "demo@example.test")["api_key"])
        app.agent_runs.knowledge_client = DemoTools()
        evidence = [{"source_id": platform, "platform": platform, "source_type": source_type,
                     "observed_at": datetime.now(timezone.utc).isoformat(), "data": {"title": "Demo fan"}}
                    for platform, source_type in (("amazon", "amazon_listing"), ("shopify", "shopify_products"))]
        run = app.agent_runs.request(owner, "weekly_ops", "优化 Amazon listing title", evidence, "demo", "demo-request")
        bundle = app.agent_runs.execute(owner, run["id"], "demo-execute")
        for event in bundle["events"]:
            if event["event_type"] in {"run.evidence_audit.computed", "task.plan.created", "task.step.started",
                                        "task.tool_call", "task.reflection.completed", "run.execution_gate"}:
                print(event["event_type"], json.dumps(event["payload"], ensure_ascii=False))
        report = next(artifact["content"] for artifact in bundle["artifacts"] if artifact["kind"] == "weekly_ops_report")
        print("取证思路", json.dumps(report["evidence_approach"], ensure_ascii=False))
        print("Reviewer", bundle["run"]["review_status"])


if __name__ == "__main__":
    main()
