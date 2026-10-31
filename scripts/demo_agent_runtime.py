"""Run two turns through the real DAG with explicitly scripted model/tool adapters.

No credentials, network, marketplace changes, or canned claim of live LLM reasoning.
"""
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


class ScenarioProvider(DemoSeedProvider):
    def complete(self, *, output_schema, payload, **kwargs):
        result = super().complete(output_schema=output_schema, payload=payload, **kwargs)
        required = output_schema["required"]
        if "objective_restatement" in required:
            question = "Read current-process" if payload.get("plan_reason") == "replan" else "Read rule-change"
            result["steps"] = [{"step_id": 1, "question": question,
                "tools": ["opc.search_knowledge"], "gap_id": "",
                "expected_evidence": "Versioned demonstration document", "done_when": "Document read"}]
        elif "sufficiency" in required:
            missing = not any("5 days" in str(n.get("result")) for n in payload.get("research_notes", []))
            if missing:
                result.update(sufficiency="partial", covered_steps=[], uncovered_steps=[1],
                    why_insufficient="Rule changed but current process is not known",
                    replan={"needed": True, "changes": ["Read current-process"]})
        elif "findings" in required:
            result["summary"] = "演示规则要求3天，现行流程为5天，存在冲突；仅提出修订建议。"
            if payload.get("continuation_context"):
                result["summary"] = "按用户反馈保留原文，仅列出3天规则与5天流程的冲突及出处。"
            candidates = payload.get("knowledge_candidates") or []
            if candidates:
                result["findings"][0]["knowledge_citations"] = [
                    {"source_id": c["source_id"], "quote": c["excerpt"][:80]} for c in candidates
                ]
        elif "priorities" in required:
            result["executive_summary"] = "演示规则由7天改为3天，现有5天流程需人工复核。"
            if payload.get("continuation_context"):
                result["executive_summary"] = "按反馈仅列冲突和证据，保留原文，不生成改写文本。"
        return result

    def research(self, *, tool_executor, tools, research_brief, **kwargs):
        if not any(t["name"] == "opc.search_knowledge" for t in tools):
            return []
        # This adapter is scripted so the exact loop is reproducible offline.
        query = "current-process" if "Current step: Read current-process" in research_brief else "rule-change"
        tool_executor("opc.search_knowledge", {"query": query})
        return []


class ScenarioTools:
    def call(self, name, arguments):
        if name != "opc.search_knowledge":
            raise ValueError("Unknown demonstration tool")
        current = arguments.get("query") == "current-process"
        return json.dumps([{"id": "demo.process" if current else "demo.rule",
            "title": "Synthetic process" if current else "Synthetic rule change",
            "summary": "Current process allows 5 days." if current else "Old rule: 7 days. New rule: 3 days."}])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with TemporaryDirectory(prefix="agent-runtime-two-turns-") as directory:
        app = RuntimeApplication(Database(Path(directory) / "runtime.sqlite"), agent_provider=ScenarioProvider())
        owner = app.auth.authenticate(app.bootstrap("Runtime demo", "demo@example.test")["api_key"])
        app.agent_runs.knowledge_client = ScenarioTools()
        sources = [{"source_id": platform, "platform": platform, "source_type": source_type,
            "observed_at": datetime.now(timezone.utc).isoformat(), "data": {"title": "Synthetic document"}}
            for platform, source_type in (("amazon", "amazon_listing"), ("shopify", "shopify_products"))]
        first = app.agent_runs.request(owner, "weekly_ops", "优化 Amazon listing title", sources, "turn-1", "turn-1")
        app.agent_runs.execute(owner, first["id"], "execute-1")
        second = app.agent_runs.follow_up(owner, first["id"], "保留原文，只列出规则与流程冲突和证据", "turn-2", "turn-2")
        app.agent_runs.execute(owner, second["id"], "execute-2")
        print("模式：离线模拟模型与文档；真实 DAG、Runtime、白名单、记忆、续办和审核代码。")
        for turn, run in enumerate((first, second), 1):
            trace = app.agent_runs.trace(owner, run["id"])
            print(f"\n第{turn}轮：{trace['status']} / {trace['review_status']}")
            if trace["continuation"]:
                print("用户反馈：", trace["continuation"]["feedback"])
                print("历史摘要字符数：", len(json.dumps(trace["continuation"], ensure_ascii=False)))
            for event in trace["events"]:
                if event["type"] in {"task.plan.created", "task.tool_call", "task.reflection.completed", "task.replan.requested"}:
                    print(event["agent"], event["type"], json.dumps(event["payload"], ensure_ascii=False))
            contexts = [a["content"] for a in trace["artifacts"] if a["kind"] == "context_assembly"]
            print("上下文组装：", json.dumps(contexts, ensure_ascii=False))
            print("记忆快照数量：", sum(a["kind"] == "specialist_working_memory" for a in trace["artifacts"]))
            report = next(a["content"] for a in app.agent_runs.get(owner, run["id"])["artifacts"]
                          if a["kind"] == "weekly_ops_report")
            print("最终摘要：", report["executive_summary"])


if __name__ == "__main__":
    main()
