"""FastAPI authentication dependencies and bounded request parsing."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import JSONResponse

from .errors import AuthenticationError, ValidationError
from .storage import Principal


bearer = HTTPBearer(auto_error=False)


def get_principal(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authorization: Bearer <eai_key> is required")
    return request.app.state.runtime.auth.authenticate(credentials.credentials)


class HTTPContext:
    """Request data only; endpoint selection and HTTP transport belong to FastAPI."""

    def __init__(self, request: Request, principal: Principal, body: bytes):
        self.app = request.app.state.runtime
        self.principal = principal
        self.request_id = request.state.request_id
        self.headers = request.headers
        self.path = request.url.path + ("?" + request.url.query if request.url.query else "")
        self.body_bytes = body

    def _principal(self) -> Principal:
        return self.principal

    @staticmethod
    def _json(status: int, value: Any, request_id: str, *, extra_headers: dict[str, str] | None = None):
        return JSONResponse(value, status_code=status, headers={"X-Request-ID": request_id, **(extra_headers or {})})

    def _body(self) -> dict[str, Any]:
        if len(self.body_bytes) > 1_000_000:
            raise ValidationError("request body exceeds 1 MB")
        value = json.loads(self.body_bytes.decode("utf-8")) if self.body_bytes else {}
        if not isinstance(value, dict):
            raise ValidationError("JSON body must be an object")
        return value

    def _body_fields(self, *, required: set[str], allowed: set[str] | None = None) -> dict[str, Any]:
        body = self._body()
        missing = sorted(required - set(body))
        extra = sorted(set(body) - (required if allowed is None else allowed))
        if missing:
            raise ValidationError("missing required fields: " + ", ".join(missing))
        if extra:
            raise ValidationError("unknown fields: " + ", ".join(extra))
        return body

    def _raw_body(self, *, max_bytes: int) -> bytes:
        if not self.body_bytes:
            raise ValidationError("request body is required")
        if len(self.body_bytes) > max_bytes:
            raise ValidationError(f"request body exceeds {max_bytes} bytes")
        return self.body_bytes

    @staticmethod
    def _query_int(params: dict[str, list[str]], name: str, *, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(params.get(name, [str(default)])[0])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValidationError(f"{name} must be between {minimum} and {maximum}")
        return value


async def get_context(request: Request, principal: Principal = Depends(get_principal)) -> HTTPContext:
    return HTTPContext(request, principal, await request.body())
