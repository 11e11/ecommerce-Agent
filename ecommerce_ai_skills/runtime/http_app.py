"""Native ASGI transport: FastAPI routing, middleware, static UI and async SSE."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from ecommerce_ai_skills import __version__
from .api import RuntimeApplication, STATIC_MIME_OVERRIDES, WEB_ROOT
from .errors import (
    AuthenticationError, AuthorizationError, ConflictError, ConnectorError,
    NotFoundError, RateLimitError, RuntimeErrorBase, ValidationError,
)
from .evidence import CSVIngestor, XLSXIngestor
from .http_context import get_principal
from .storage import Principal

log = logging.getLogger("ecommerce_ai_skills.api")


def error_response(runtime, exc: Exception, request_id: str) -> JSONResponse:
    runtime.metrics.increment("http_errors_total")
    status = 500
    message = "Internal server error"
    name = type(exc).__name__
    if isinstance(exc, AuthenticationError): status = 401
    elif isinstance(exc, AuthorizationError): status = 403
    elif isinstance(exc, (NotFoundError, HTTPException)):
        status = exc.status_code if isinstance(exc, HTTPException) else 404
    elif isinstance(exc, ConflictError): status = 409
    elif isinstance(exc, ConnectorError): status = 502
    elif isinstance(exc, RateLimitError): status = 429
    elif isinstance(exc, (RuntimeErrorBase, RequestValidationError, ValueError, TypeError)): status = 422
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)): status = 400
    if isinstance(exc, RequestValidationError):
        if any(e["type"] == "json_invalid" for e in exc.errors()):
            status = 400
        message = "Invalid request fields: " + "; ".join(
            ".".join(str(p) for p in e["loc"]) + " (" + e["type"] + ")" for e in exc.errors()
        )
    elif isinstance(exc, HTTPException):
        message = "route not found" if status == 404 else str(exc.detail)
    elif status != 500:
        message = str(exc)
    # Never log raw bodies, query strings, credentials or validation inputs.
    log.warning("request_failed request_id=%s status=%s type=%s", request_id, status, name)
    headers = {"X-Request-ID": request_id}
    if isinstance(exc, RateLimitError): headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse({"error": {"type": name, "message": message}, "request_id": request_id}, status_code=status, headers=headers)


class RuntimeMiddleware:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        request_id = supplied[:128] if supplied else secrets.token_hex(16)
        scope.setdefault("state", {})["request_id"] = request_id
        self.runtime.metrics.increment("http_requests_total")
        path, method = scope["path"], scope["method"]
        started = False

        async def response_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                response_headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                response_headers.append((b"x-request-id", request_id.encode("latin-1", errors="replace")))
                message["headers"] = response_headers
                log.info("http method=%s path=%s status=%s", method, path, message["status"])
            await send(message)

        try:
            if path not in {"/healthz", "/readyz"} and not path.startswith("/app"):
                self.runtime.rate_limiter.check(str((scope.get("client") or ("local",))[0]))
            if method in {"POST", "PATCH", "PUT"}:
                media = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
                maximum = 1_000_000
                if path == "/v1/evidence-imports":
                    maximum = XLSXIngestor.MAX_RAW_BYTES if media == b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" else CSVIngestor.MAX_RAW_BYTES
                declared = headers.get(b"content-length")
                try:
                    length = int(declared) if declared is not None else None
                except ValueError as exc:
                    raise ValidationError("Content-Length must be an integer") from exc
                if length is not None and (length < 0 or length > maximum):
                    raise ValidationError(f"request body exceeds {maximum} bytes or has negative length")
                chunks, total = [], 0
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise ValidationError("request body ended early")
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > maximum:
                        raise ValidationError(f"request body exceeds {maximum} bytes")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                if length is not None and total != length:
                    raise ValidationError("request body ended before Content-Length")
                body = b"".join(chunks)
                replayed = False
                original_receive = receive

                async def body_receive():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()

                receive = body_receive
            await self.app(scope, receive, response_send)
        except Exception as exc:
            if started:
                raise
            await error_response(self.runtime, exc, request_id)(scope, receive, response_send)


def mission_cursor(value: str, source: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ValidationError(f"{source} must be a non-negative integer cursor")
    cursor = int(value)
    if cursor > 9_223_372_036_854_775_807:
        raise ValidationError(f"{source} is outside the supported cursor range")
    return cursor


def mission_after(request: Request) -> int:
    params = parse_qs(request.url.query, keep_blank_values=True)
    if set(params) - {"after"}:
        raise ValidationError("unknown query fields: " + ", ".join(sorted(set(params) - {"after"})))
    if len(params.get("after", [])) > 1:
        raise ValidationError("after must be provided at most once")
    marker = mission_cursor(params["after"][0], "after") if "after" in params else 0
    values = request.headers.getlist("Last-Event-ID")
    if len(values) > 1:
        raise ValidationError("Last-Event-ID must be provided at most once")
    return mission_cursor(values[0].strip(), "Last-Event-ID") if values else marker


def sse_frame(marker: int, kind: str, data: dict) -> bytes:
    return (f"id: {marker}\nevent: {kind}\ndata: " + json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n\n").encode("utf-8")


def create_app(runtime: RuntimeApplication) -> FastAPI:
    from .http_routes import router
    app = FastAPI(title="Commerce Agent Runtime", version=__version__)
    app.state.runtime = runtime
    app.add_middleware(RuntimeMiddleware, runtime=runtime)

    async def handle_error(request: Request, exc: Exception):
        return error_response(runtime, exc, getattr(request.state, "request_id", secrets.token_hex(16)))

    for error_type in (RuntimeErrorBase, RequestValidationError, HTTPException, ValueError, TypeError):
        app.add_exception_handler(error_type, handle_error)

    @app.get("/healthz", tags=["health"])
    def health():
        return {"status": "ok", "service": "ecommerce-ai-runtime"}

    @app.get("/readyz", tags=["health"])
    def ready():
        result = runtime.db.readiness()
        return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)

    @app.get("/v1/demo-session", tags=["demo"])
    def demo_session():
        if runtime.demo_session is None:
            raise NotFoundError("route not found")
        return JSONResponse(runtime.demo_session, headers={"Cache-Control": "no-store"})

    @app.get("/app", include_in_schema=False)
    @app.get("/app/", include_in_schema=False)
    @app.get("/app/{asset_path:path}", include_in_schema=False)
    def static_app(asset_path: str = "mission-control.html"):
        allowed = {"mission-control.html", "app.js", "i18n.js", "styles.css"}
        if asset_path not in allowed and not asset_path.startswith("assets/"):
            raise NotFoundError("route not found")
        path = (WEB_ROOT / asset_path).resolve()
        if not path.is_relative_to(WEB_ROOT.resolve()) or not path.is_file():
            raise NotFoundError("web application asset not found")
        media = STATIC_MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.get("/v1/mission-control/events", tags=["mission-control"], response_class=StreamingResponse)
    async def mission_events(request: Request, principal: Principal = Depends(get_principal)):
        runtime.auth.require(principal, "viewer")
        marker = mission_after(request)

        async def read_page(cursor):
            return await run_in_threadpool(runtime.db.read_mission_events, principal.tenant_id,
                                           after=cursor, limit=runtime.mission_event_batch_size)

        page = await read_page(marker)
        runtime.mission_connections.acquire(principal.tenant_id)
        released = False
        release_lock = threading.Lock()

        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    runtime.mission_connections.release(principal.tenant_id)

        async def events():
            nonlocal marker, page
            started = last_write = time.monotonic()
            delivered = 0
            reason = "lifetime_limit"
            try:
                while time.monotonic() - started < runtime.mission_event_max_lifetime_seconds:
                    if await request.is_disconnected():
                        return
                    if page["reset_required"]:
                        marker = int(page["reset_cursor"])
                        yield sse_frame(marker, "mission.reset", {"reason": "retention_gap", "cursor": marker, "snapshot_url": "/v1/mission-control"})
                        last_write = time.monotonic()
                        page = await read_page(marker)
                        continue
                    for event in page["events"]:
                        marker = int(event["cursor"])
                        yield sse_frame(marker, "mission.update", event)
                        delivered += 1
                        last_write = time.monotonic()
                        if delivered >= runtime.mission_event_max_backlog:
                            reason = "backlog_limit"
                            break
                    if delivered >= runtime.mission_event_max_backlog:
                        break
                    if page["has_more"]:
                        page = await read_page(marker)
                        continue
                    if time.monotonic() - last_write >= runtime.mission_event_heartbeat_seconds:
                        yield b": heartbeat\n\n"
                        last_write = time.monotonic()
                    await asyncio.sleep(min(runtime.mission_event_poll_seconds,
                        max(0.01, runtime.mission_event_max_lifetime_seconds - (time.monotonic() - started))))
                    page = await read_page(marker)
                yield sse_frame(marker, "mission.reconnect", {"reason": reason, "retry_after_seconds": 1, "cursor": marker})
            finally:
                release()

        return StreamingResponse(events(), media_type="text/event-stream", background=BackgroundTask(release), headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff",
        })

    app.include_router(router)
    return app
