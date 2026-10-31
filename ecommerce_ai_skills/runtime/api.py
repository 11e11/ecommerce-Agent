"""Authenticated JSON API for the runtime vertical slice.

FastAPI provides the HTTP boundary and Uvicorn runs the ASGI application.
The MCP adapter remains separately available.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import mimetypes
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ecommerce_ai_skills import __version__

# mimetypes reads the Windows registry, where installed apps can remap
# extensions (e.g. .svg -> image/svg); pin the types the UI relies on.
STATIC_MIME_OVERRIDES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".woff2": "font/woff2",
}

from .actions import ActionService
from .agent_graphs import AgentGraphService
from .accounts import MarketplaceAccountService
from .assurance import AssuranceService
from .ads_gates import AdsCapabilityGateService
from .ads_adapter_status import AdsAdapterStatusService
from .agents import (
    AgentProvider,
    AnthropicMessagesProvider,
    DeepSeekResponsesProvider,
    OpenAIResponsesProvider,
    WeeklyOpsCouncil,
)
from .auth import AuthService
from .briefing import BriefingService
from .daily_ops import DailyOpsService
from .evidence import CSVIngestor, EvidenceImportService, REPORT_SPECS, XLSXIngestor
from .evals import WorkflowEvaluator
from .jobs import JobService, ScheduleService
from .metric_observations import MetricObservationService, SUPPORTED_REPORT_TYPES
from .proposals import ProposalService
from .pilot import PilotService
from .provider_smoke import ProviderSmokeService
from .errors import AuthenticationError, AuthorizationError, ConflictError, ConnectorError, NotFoundError, RateLimitError, RuntimeErrorBase, ValidationError
from .observability import JsonFormatter, Metrics, RateLimiter
from .report_recipes import ReportRecipeService
from .report_syncs import ReportSyncService
from .storage import Database, Principal

log = logging.getLogger("ecommerce_ai_skills.api")
WEB_ROOT = Path(__file__).resolve().parent / "web"


class _MissionConnectionLimiter:
    """Single-process SSE admission control for the ASGI application."""

    def __init__(self, global_limit: int, tenant_limit: int):
        self.global_limit = max(1, int(global_limit))
        self.tenant_limit = max(1, int(tenant_limit))
        self._lock = threading.Lock()
        self._global_active = 0
        self._tenant_active: dict[str, int] = {}

    def acquire(self, tenant_id: str) -> None:
        with self._lock:
            tenant_active = self._tenant_active.get(tenant_id, 0)
            if self._global_active >= self.global_limit:
                log.warning("mission_stream_connection_rejected scope=global")
                raise RateLimitError(
                    "Mission Control SSE connection limit exceeded",
                    retry_after=5,
                )
            if tenant_active >= self.tenant_limit:
                log.warning("mission_stream_connection_rejected scope=tenant")
                raise RateLimitError(
                    "Mission Control SSE connection limit exceeded",
                    retry_after=5,
                )
            self._global_active += 1
            self._tenant_active[tenant_id] = tenant_active + 1

    def release(self, tenant_id: str) -> None:
        with self._lock:
            active = self._tenant_active.get(tenant_id, 0)
            if active <= 1:
                self._tenant_active.pop(tenant_id, None)
            else:
                self._tenant_active[tenant_id] = active - 1
            self._global_active = max(0, self._global_active - 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "global_active": self._global_active,
                "tenant_active": dict(self._tenant_active),
            }


def _default_agent_provider() -> AgentProvider:
    """Pick the provider from the environment, defaulting to OpenAI.

    EAI_AGENT_PROVIDER selects; an explicitly injected provider always wins over
    it. The default stays OpenAI so existing deployments are unaffected by the
    Anthropic provider merely existing. An unknown value fails loudly rather than
    silently falling back — a typo that quietly routes to a different vendor is
    worse than a startup error.
    """
    name = os.environ.get("EAI_AGENT_PROVIDER", "").strip().lower()
    if not name or name in {"openai", "openai_responses"}:
        return OpenAIResponsesProvider()
    if name in {"anthropic", "anthropic_messages"}:
        return AnthropicMessagesProvider()
    if name in {"deepseek", "deepseek_responses"}:
        return DeepSeekResponsesProvider()
    raise ValidationError(
        f"EAI_AGENT_PROVIDER must be 'openai', 'anthropic', or 'deepseek', got {name!r}"
    )


class RuntimeApplication:
    def __init__(
        self,
        db: Database,
        *,
        rate_limit_per_minute: int = 120,
        agent_provider: AgentProvider | None = None,
        provider_smoke_openai_provider: OpenAIResponsesProvider | None = None,
        mission_event_poll_seconds: float = 1.0,
        mission_event_heartbeat_seconds: float = 15.0,
        mission_event_max_lifetime_seconds: float = 300.0,
        mission_event_batch_size: int = 100,
        mission_event_max_backlog: int = 500,
        mission_event_max_connections: int = 20,
        mission_event_max_connections_per_tenant: int = 4,
    ):
        self.db = db
        self.auth = AuthService(db)
        self.assurance = AssuranceService(db,self.auth)
        self.agent_graphs = AgentGraphService(db, self.auth)
        self.accounts = MarketplaceAccountService(db, self.auth)
        resolved_agent_provider = agent_provider or _default_agent_provider()
        smoke_openai_provider = provider_smoke_openai_provider
        if smoke_openai_provider is None and isinstance(
            resolved_agent_provider, OpenAIResponsesProvider
        ):
            smoke_openai_provider = resolved_agent_provider
        self.provider_smoke = ProviderSmokeService(
            db,
            self.auth,
            self.accounts,
            openai_provider=smoke_openai_provider,
        )
        self.ads_gates = AdsCapabilityGateService(db, self.auth)
        self.ads_adapter_status = AdsAdapterStatusService(db, self.auth)
        self.report_recipes = ReportRecipeService(db, self.auth)
        self.evidence_imports = EvidenceImportService(db, self.auth)
        self.metric_observations = MetricObservationService(db, self.auth)
        self.report_syncs = ReportSyncService(
            db, self.auth, self.evidence_imports, self.metric_observations
        )
        self.actions = ActionService(db, self.auth, self.evidence_imports)
        self.proposals = ProposalService(db, self.auth, self.actions)
        self.pilot = PilotService(db, self.auth)
        self.briefing = BriefingService(db, self.auth)
        self.agent_runs = WeeklyOpsCouncil(
            db,
            self.auth,
            resolved_agent_provider,
            evidence_resolver=self.evidence_imports.resolve,
            graph_service=self.agent_graphs,
        )
        self.daily_ops = DailyOpsService(db, self.auth, self.agent_runs)
        self.jobs = JobService(db, self.auth, self.agent_runs)
        self.schedules = ScheduleService(
            db, self.auth, self.agent_runs, self.jobs
        )
        self.evaluator = WorkflowEvaluator(db, self.auth)
        self.metrics = Metrics()
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self.mission_event_poll_seconds = max(0.01, float(mission_event_poll_seconds))
        self.mission_event_heartbeat_seconds = max(
            0.01, float(mission_event_heartbeat_seconds)
        )
        self.mission_event_max_lifetime_seconds = max(
            0.01, float(mission_event_max_lifetime_seconds)
        )
        self.mission_event_batch_size = max(
            1, min(int(mission_event_batch_size), 200)
        )
        self.mission_event_max_backlog = max(
            1, min(int(mission_event_max_backlog), 10_000)
        )
        self.mission_connections = _MissionConnectionLimiter(
            mission_event_max_connections,
            mission_event_max_connections_per_tenant,
        )
        self.demo_session: dict[str, str] | None = None
        self.demo_key_id: str | None = None

    def bootstrap(self, name: str, email: str) -> dict[str, str]:
        tenant_id, user_id = self.db.create_tenant(name, email)
        # Install the tenant's required graph before issuing the one-time key;
        # failures before this point therefore cannot strand an undisclosed key.
        principal = self.db.principal_for_user(tenant_id, user_id)
        self.agent_graphs.ensure_default(principal)
        api_key = self.auth.issue_key(tenant_id, user_id)
        return {"tenant_id": tenant_id, "user_id": user_id, "api_key": api_key}

    @staticmethod
    def _validate_bind_host(host: str, allow_public: bool) -> None:
        if allow_public:
            return
        if host == "localhost":
            return
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValidationError("non-loopback host requires --allow-public")
        if not address.is_loopback:
            raise ValidationError("non-loopback host requires --allow-public")


def create_app(runtime: RuntimeApplication):
    """Create the native FastAPI app around an explicitly supplied runtime."""
    from .http_app import create_app as factory
    return factory(runtime)


def build_server(app: RuntimeApplication, host: str = "127.0.0.1", port: int = 8787, *, allow_public: bool = False):
    from .asgi_server import ASGIServer
    RuntimeApplication._validate_bind_host(host, allow_public)
    return ASGIServer(create_app(app), host, port)


def serve(app: RuntimeApplication, host: str = "127.0.0.1", port: int = 8787, *, allow_public: bool = False,
          stop_event: threading.Event | None = None, httpd=None) -> None:
    RuntimeApplication._validate_bind_host(host, allow_public)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    server = httpd or build_server(app, host, port, allow_public=allow_public)
    if stop_event is not None:
        server.manage_signals = False
        def stop_server():
            stop_event.wait()
            server.shutdown()
        threading.Thread(target=stop_server, name="asgi-shutdown", daemon=True).start()
    if allow_public:
        log.warning("public bind explicitly enabled; terminate TLS at an authenticated reverse proxy")
    log.info("FastAPI runtime listening on http://%s:%d", host, server.server_port)
    try:
        server.serve_forever()
    finally:
        if stop_event is not None:
            stop_event.set()
        server.server_close()
