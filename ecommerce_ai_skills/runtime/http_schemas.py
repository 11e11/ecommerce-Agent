"""Validated request contracts for the Agent API."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: str
    objective: str = Field(min_length=5, max_length=1000)
    evidence: list[dict[str, Any]] | None = None
    evidence_import_ids: list[str] | None = None
    graph_version_id: str | None = None
    metric_observation_ids: list[str] | None = None


class FollowUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback: str = Field(min_length=1, max_length=1000)
    objective: str | None = Field(default=None, min_length=5, max_length=1000)
    evidence: list[dict[str, Any]] | None = None
    evidence_import_ids: list[str] | None = None
