"""Execution controls shared by real Specialist model calls.

The model chooses tools inside research(); this controller owns lifecycle,
deadlines and durable phase transitions, independently of provider prompts.
"""
from __future__ import annotations

import json
from typing import Any, Callable


class AgentLoop:
    def __init__(self, memory: Any, recorder: Any):
        self.memory = memory
        self.recorder = recorder
        self.phase = "created"
        self.sequence = 0

    def call(self, phase: str, callback: Callable, payload: dict) -> Any:
        if self.phase == "stopped":
            raise RuntimeError("agent loop already stopped")
        if self.memory.seconds_left <= 0:
            raise TimeoutError("specialist wall clock deadline reached")
        transitions = {"created": {"plan", "act"}, "plan": {"act", "reflect"},
                       "act": {"act", "reflect", "final"},
                       "reflect": {"plan", "final"}}
        if phase not in transitions.get(self.phase, set()):
            raise RuntimeError(f"invalid agent loop transition: {self.phase} -> {phase}")
        self.sequence += 1
        self.phase = phase
        self.recorder.event("task.loop.transition", {
            "phase": phase, "sequence": self.sequence,
            "tool_calls_left": self.memory.tool_calls_left,
            "seconds_left": round(self.memory.seconds_left, 3),
        })
        try:
            result = callback(payload, self.memory)
        except Exception as exc:
            self.recorder.event("task.loop.error", {
                "phase": phase, "error_type": type(exc).__name__,
            })
            raise
        return result

    def stop(self, reason: str) -> None:
        self.phase = "stopped"
        self.recorder.event("task.loop.stopped", {
            "phase": "stopped", "reason": reason, "sequence": self.sequence,
            "tool_calls_used": self.memory.tool_calls_used,
        })


def assemble_context(payload: dict, *, limit: int = 48_000) -> tuple[dict, dict]:
    """Bound JSON payload characters; never mutate source evidence or IDs.

    This is a character ceiling, not a token estimate. Tools/instructions are
    separate provider inputs. Omitted content remains in durable artifacts.
    """
    original = json.dumps(payload, ensure_ascii=False)
    result = json.loads(original)
    omitted: list[str] = []

    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False))

    def shorten(value: Any, maximum: int) -> Any:
        if isinstance(value, str) and len(value) > maximum:
            return value[:maximum] + " [excerpt truncated; full source retained]"
        if isinstance(value, list):
            return [shorten(item, maximum) for item in value[:20]]
        if isinstance(value, dict):
            return {key: (item if key in {"source_id", "id", "platform", "source_type"}
                          else shorten(item, maximum)) for key, item in value.items()}
        return value

    for maximum in (2000, 500, 120):
        if size() <= limit:
            break
        for key in ("evidence", "research_notes", "knowledge_candidates", "specialist_findings",
                    "continuation_context", "revision_feedback"):
            if key in result:
                result[key] = shorten(result[key], maximum)
                if key not in omitted:
                    omitted.append(key)
    if omitted:
        result["context_limits"] = {
            "excerpted_fields": omitted,
            "instruction": "Excerpts may omit relevant facts. Do not infer absence from truncation; use tools or report a gap.",
        }
    if size() > limit:
        raise ValueError("required Specialist context exceeds payload character budget")
    return result, {"original_chars": len(original), "injected_chars": size(),
                    "limit_chars": limit, "excerpted_fields": omitted}
