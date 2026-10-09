from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent_outputs
from ecommerce_ai_skills.runtime import harness
from ecommerce_ai_skills.runtime.evidence_audit import audit_evidence
from ecommerce_ai_skills.runtime.errors import ExternalServiceError


def plan():
    return agent_outputs.plan_output({"objective": "listing title"})


class Recorder:
    def __init__(self):
        self.artifacts = []
        self.events = []

    def artifact(self, kind, content):
        self.artifacts.append((kind, content))

    def event(self, kind, payload):
        self.events.append((kind, payload))


def run_harness(*, tier="standard", planner=None, researcher=None, reflector=None, recorder=None):
    return harness.run_specialist_harness(
        budget=harness.budget_for(tier), allowed_tools=harness.EXTENDED_TOOLS,
        gap_ids=["gap-amazon-1"], platforms=["amazon", "shopify"],
        base_payload={"target_platform": "amazon", "objective": "listing title",
                      "evidence_audit": {"adequacy": "supported"}},
        plan_call=planner or (lambda payload, memory: plan()),
        research_call=researcher or (lambda payload, memory: []),
        reflect_call=reflector or (lambda payload, memory: agent_outputs.reflection_output(payload)),
        final_call=lambda payload, memory: {"platform": "amazon", "summary": "review", "findings": [],
                    "data_gaps": [], "evidence_sufficiency": payload["evidence_sufficiency"],
                    "plan_executed": payload["plan_executed"]}, recorder=recorder)


def test_simple_has_no_plan_or_reflection():
    recorder = Recorder()
    result = run_harness(tier="simple", planner=lambda *_: pytest.fail("simple must not plan"),
                         reflector=lambda *_: pytest.fail("simple must not reflect"), recorder=recorder)
    assert result.plan is None and result.reflection is None
    assert not recorder.artifacts


def test_executes_each_step_and_replans_only_once():
    calls = []
    planner_payloads = []
    recorder = Recorder()

    def planner(payload, memory):
        planner_payloads.append(payload)
        return plan()

    def research(payload, memory):
        calls.append((payload["round"], payload["step"]["step_id"]))
        return []

    def reflect(payload, memory):
        result = agent_outputs.reflection_output(payload)
        result.update(sufficiency="partial", covered_steps=[1], uncovered_steps=[2], why_insufficient="missing sales")
        result["replan"] = {"needed": True, "changes": ["search for missing sales"]}
        return result

    result = run_harness(planner=planner, researcher=research, reflector=reflect, recorder=recorder)
    assert calls == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert result.memory.replans_used == 1
    assert result.reflection["replan"]["needed"] is False
    assert planner_payloads[1]["previous_reflection"]["replan"]["changes"] == ["search for missing sales"]
    assert len([kind for kind, _ in recorder.artifacts if kind == "agent_plan"]) == 2


def test_plan_bounds_whitelist_and_gap_validation():
    for mutation in (lambda p: p["steps"][0].update(tools=["write"]),
                     lambda p: p["steps"][0].update(gap_id="unknown"),
                     lambda p: p.update(steps=p["steps"] * 3),
                     lambda p: p.update(needs_from_other_platforms=[{"platform": "unknown", "what": "x", "why": "y"}])):
        invalid = plan()
        mutation(invalid)
        with pytest.raises(ExternalServiceError):
            harness.validate_plan(invalid, budget=harness.budget_for("standard"),
                allowed_tools=harness.EXTENDED_TOOLS, gap_ids=[], platforms=["amazon"])


def test_memory_bounded_even_with_oversized_plan_and_steps():
    memory = harness.WorkingMemory(harness.budget_for("deep"))
    memory.plan = plan()
    for step in memory.plan["steps"]:
        step["question"] = "x" * 10000
    memory.note_steps([{"step_id": 1, "note": "x" * 10000}])
    memory.note_evidence([{"source_id": str(index), "excerpt": "fact" * 500} for index in range(50)])
    assert len(memory.known) == 12
    assert len(json.dumps(memory.injection(), ensure_ascii=False, sort_keys=True)) <= 2000
    assert memory.allow_tool_call("search", "same")
    assert not memory.allow_tool_call("search", "same")
    assert memory.tool_calls_used == 2


def test_deadline_abstains_without_more_model_calls():
    def researcher(payload, memory):
        memory.started_monotonic = time.monotonic() - 999
        return []
    result = run_harness(researcher=researcher, reflector=lambda *_: pytest.fail("expired must not reflect"))
    assert result.result["evidence_sufficiency"]["level"] == "insufficient"
    assert not result.result["findings"]


def test_reflection_cannot_claim_unexecuted_steps_covered():
    def researcher(payload, memory):
        memory.tool_calls_used = memory.budget.max_tool_calls
        return []
    result = run_harness(researcher=researcher)
    assert result.reflection["sufficiency"] == "partial"
    assert result.reflection["covered_steps"] == [1]
    assert result.reflection["uncovered_steps"] == [2]
    assert result.result["plan_executed"][1]["outcome"] == "skipped"


def source(platform="amazon", kind="amazon_listing", *, age=0):
    return {"source_id": platform + kind, "platform": platform, "source_type": kind,
            "observed_at": (datetime(2026, 10, 9, tzinfo=timezone.utc) - timedelta(days=age)).isoformat(),
            "data": {"title": "fan"}}


def test_audit_per_source_freshness_threshold_and_stable_gaps():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    requirements = {"amazon": {"ecom-listing": {"product_info": True, "keywords": True}}}
    fresh = audit_evidence([source(age=14)], skill_inputs_by_platform=requirements, now=now)
    stale = audit_evidence([source(age=14.01), source(kind="amazon_business_report")],
                           skill_inputs_by_platform=requirements, now=now)
    assert fresh["platforms"][0]["freshness"]["status"] == "fresh"
    assert stale["platforms"][0]["freshness"]["status"] == "stale"
    assert fresh["gaps"][0]["id"] == stale["gaps"][0]["id"]
    assert "not mapped" in fresh["gaps"][0]["why"]


def test_audit_missing_platform_and_currency_mismatch():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    rows = []
    for platform, currency in (("amazon", "USD"), ("shopify", "EUR")):
        row = source(platform, "metric_observation")
        row["data"] = {"metric_key": "revenue", "unit": "amount", "currency": currency,
                       "time_grain": "day", "dimensions": {}, "period_start": "2026-10-08", "period_end": "2026-10-09"}
        rows.append(row)
    audit = audit_evidence(rows, skill_inputs_by_platform={"amazon": {}, "shopify": {}, "ebay": {"unknown_skill": {"x": True}}}, now=now)
    assert not audit["comparability"]["can_compare_across_platforms"]
    assert any(gap["platform"] == "ebay" for gap in audit["gaps"])
    assert all(report["series"][0]["count"] == 1 for report in audit["platforms"] if report["series"])
