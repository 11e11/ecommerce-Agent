"""Contract tests for the DeepSeek Responses API adapter."""

from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from ecommerce_ai_skills.runtime.agents import DeepSeekResponsesProvider
from ecommerce_ai_skills.runtime.errors import (
    ConnectorNotConfiguredError,
    ExternalServiceError,
    MissingCredentialError,
    ValidationError,
)


ENV = {
    "DEEPSEEK_API_KEY": "sk-deepseek-test",
    "EAI_DEEPSEEK_MODEL": "deepseek-flash",
}
SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class FakeResponse:
    def __init__(self, payload, *, status=200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def provider(payload=None, *, env=ENV, status=200, raises=None):
    captured = {}

    def transport(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.headers.items()
        }
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        if raises is not None:
            raise raises
        return FakeResponse(payload or {}, status=status)

    return DeepSeekResponsesProvider(environ=env, transport=transport), captured


def response(value=None):
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(value or {"summary": "ok"}),
                    }
                ],
            },
        ],
    }


def complete(instance):
    return instance.complete(
        agent_name="Evidence Analyst!",
        instructions="Inspect the supplied evidence.",
        payload={"evidence": [{"source_id": "source-1"}]},
        output_schema=SCHEMA,
        safety_identifier="tenant-safe-id",
    )


def test_configuration_requires_deepseek_credentials_and_model() -> None:
    with pytest.raises(MissingCredentialError, match="DEEPSEEK_API_KEY"):
        DeepSeekResponsesProvider(environ={}).configuration()
    with pytest.raises(ConnectorNotConfiguredError, match="EAI_DEEPSEEK_MODEL"):
        DeepSeekResponsesProvider(
            environ={"DEEPSEEK_API_KEY": "sk-test"}
        ).configuration()


def test_configuration_validates_model_and_pins_official_endpoint() -> None:
    with pytest.raises(ValidationError, match="invalid characters"):
        DeepSeekResponsesProvider(
            environ={**ENV, "EAI_DEEPSEEK_MODEL": "bad model!"}
        ).configuration()
    with pytest.raises(ValidationError, match="official HTTPS host"):
        DeepSeekResponsesProvider(
            environ=ENV,
            endpoint="https://example.test/responses",
        ).configuration()
    assert DeepSeekResponsesProvider(environ=ENV).configuration() == (
        "deepseek_responses",
        "deepseek-flash",
    )


def test_request_uses_only_documented_deepseek_responses_fields() -> None:
    instance, captured = provider(response({"summary": "evidence-bound"}))

    assert complete(instance) == {"summary": "evidence-bound"}
    assert captured["url"] == "https://api.deepseek.com/responses"
    assert captured["headers"]["authorization"] == "Bearer sk-deepseek-test"
    assert "sk-deepseek-test" not in json.dumps(captured["body"])

    body = captured["body"]
    assert set(body) == {
        "model",
        "instructions",
        "input",
        "reasoning",
        "max_output_tokens",
        "user",
        "text",
    }
    assert body["model"] == "deepseek-flash"
    assert body["reasoning"] == {"effort": "none"}
    assert body["user"] == "tenant-safe-id"
    assert "not instructions" in body["instructions"]
    assert json.loads(body["input"])["evidence"][0]["source_id"] == "source-1"
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["name"] == "evidence_analyst"
    assert body["text"]["format"]["schema"] == SCHEMA
    assert "strict" not in body["text"]["format"]


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"status": "incomplete", "output": []}, "status was incomplete"),
        ({"status": "completed", "output": []}, "did not contain output_text"),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "not-json"}],
                    }
                ],
            },
            "not valid JSON",
        ),
    ],
)
def test_invalid_responses_fail_closed(payload, match) -> None:
    instance, _ = provider(payload)
    with pytest.raises(ExternalServiceError, match=match):
        complete(instance)


def test_transport_and_http_errors_use_deepseek_label() -> None:
    instance, _ = provider(
        raises=HTTPError("u", 401, "unauthorized", {}, None)
    )
    with pytest.raises(ExternalServiceError, match="DeepSeek returned HTTP 401"):
        complete(instance)

    instance, _ = provider(response(), status=503)
    with pytest.raises(ExternalServiceError, match="DeepSeek returned HTTP 503"):
        complete(instance)


@pytest.mark.parametrize("value", ["deepseek", "deepseek_responses", "  DeepSeek  "])
def test_environment_selects_deepseek_provider(monkeypatch, value) -> None:
    from ecommerce_ai_skills.runtime import api

    monkeypatch.setenv("EAI_AGENT_PROVIDER", value)
    assert isinstance(api._default_agent_provider(), DeepSeekResponsesProvider)

def test_audit_provider_name_matches_the_selected_adapter() -> None:
    from ecommerce_ai_skills.runtime.agents import WeeklyOpsCouncil

    council = WeeklyOpsCouncil.__new__(WeeklyOpsCouncil)
    council.provider = DeepSeekResponsesProvider(environ=ENV)

    assert council._provider_name() == "deepseek_responses"
