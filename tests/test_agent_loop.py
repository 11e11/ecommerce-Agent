import json

import pytest

from ecommerce_ai_skills.runtime.agent_loop import AgentLoop, assemble_context
from ecommerce_ai_skills.runtime.harness import WorkingMemory, budget_for


class Recorder:
    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))


def test_context_budget_preserves_ids_and_original_sources():
    payload = {"objective": "check rules", "evidence": [
        {"source_id": "source-1", "data": {"text": "a" * 100_000}},
        {"source_id": "source-2", "data": {"text": "b" * 100_000}},
    ]}
    result, accounting = assemble_context(payload)
    assert accounting["injected_chars"] <= 48_000
    assert [e["source_id"] for e in result["evidence"]] == ["source-1", "source-2"]
    assert len(payload["evidence"][0]["data"]["text"]) == 100_000
    assert "evidence" in result["context_limits"]["excerpted_fields"]
    assert json.loads(json.dumps(result))["objective"] == "check rules"


def test_required_context_is_never_silently_dropped():
    with pytest.raises(ValueError, match="budget"):
        assemble_context({"objective": "x" * 50_000})


def test_loop_records_real_calls_errors_and_stop():
    recorder = Recorder()
    memory = WorkingMemory(budget_for("standard"))
    loop = AgentLoop(memory, recorder)
    assert loop.call("act", lambda payload, state: payload["query"], {"query": "rules"}) == "rules"
    with pytest.raises(OSError):
        loop.call("act", lambda *_: (_ for _ in ()).throw(OSError()), {})
    loop.stop("evidence_gap")
    assert recorder.events[-1][1]["reason"] == "evidence_gap"
    assert any(kind == "task.loop.error" for kind, _ in recorder.events)
    with pytest.raises(RuntimeError, match="stopped"):
        loop.call("final", lambda *_: None, {})


def test_deadline_prevents_model_dispatch():
    memory = WorkingMemory(budget_for("standard"))
    memory.started_monotonic -= 1000
    with pytest.raises(TimeoutError):
        AgentLoop(memory, Recorder()).call("plan", lambda *_: pytest.fail("must not dispatch"), {})
