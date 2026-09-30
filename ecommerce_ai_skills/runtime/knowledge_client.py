"""Sync bridge to the bundled MCP knowledge server over stdio.

The runtime's own agents consume the same read-only knowledge tools the MCP
server offers to external clients, so one MCP surface serves two audiences.
Calls are serialized behind a lock: knowledge reads are fast local operations
and determinism beats parallelism here. Tool results are truncated before they
reach a prompt — an unbounded chapter dump is a context bug, not a feature.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any, Protocol

from .errors import ConnectorNotConfiguredError, ExternalServiceError

MAX_TOOL_RESULT_CHARS = 4_000
MAX_RESEARCH_NOTES_CHARS = 16_000


def truncate_text(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return text[:limit] + f"\n...[truncated, {omitted} chars omitted]", True


class KnowledgeToolClient(Protocol):
    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke one allow-listed knowledge tool and return its text output."""


class McpStdioKnowledgeClient:
    """Spawn the bundled MCP server as a subprocess and speak MCP over stdio.

    The session lives on a background event-loop thread; sync callers share it
    through ``run_coroutine_threadsafe`` behind a lock. The subprocess is the
    real MCP server (same package, same integrity-checked dist), so agents and
    external clients see byte-identical tools.
    """

    def __init__(self, dist_path: str | Path | None = None, timeout_seconds: float = 30.0):
        self._dist = str(Path(dist_path).resolve()) if dist_path else None
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._start_error: list[Exception] = []

    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ConnectorNotConfiguredError(
                'MCP SDK is not installed; run: pip install "mcp>=1.28,<2"'
            ) from exc
        dist = self._dist or str(
            Path(__file__).resolve().parents[1] / "package_data" / "dist"
        )
        if not Path(dist).exists():
            raise ConnectorNotConfiguredError(f"MCP knowledge dist is missing: {dist}")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ecommerce_ai_skills.cli", "mcp", "--dist", dist],
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="mcp-knowledge-client",
        )
        self._thread.start()
        self._stop = asyncio.Event()

        async def _main() -> None:
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set()
                        await self._stop.wait()
            except Exception as exc:  # surfaced to the first caller
                self._start_error.append(exc)
                self._ready.set()
            finally:
                # Signaled only after both context managers fully unwound, so
                # close() never stops a loop that is still shutting down stdio.
                self._done.set()

        asyncio.run_coroutine_threadsafe(_main(), self._loop)
        if not self._ready.wait(timeout=self._timeout):
            raise ExternalServiceError("MCP knowledge client did not start in time")
        if self._session is None:
            detail = f": {self._start_error[0]}" if self._start_error else ""
            raise ExternalServiceError(
                f"MCP knowledge client failed to start{detail}"
            )
        return self._session

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        with self._lock:
            session = self._ensure_session()

            async def _invoke() -> Any:
                return await session.call_tool(name, arguments)

            future = asyncio.run_coroutine_threadsafe(_invoke(), self._loop)
            try:
                result = future.result(timeout=self._timeout)
            except Exception as exc:
                raise ExternalServiceError(f"MCP tool call failed: {exc}") from exc
        if getattr(result, "isError", False):
            raise ExternalServiceError(f"MCP tool {name} returned an error result")
        parts = []
        for item in getattr(result, "content", None) or []:
            if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str):
                parts.append(item.text)
        return "\n".join(parts)

    def close(self) -> None:
        with self._lock:
            if self._loop is None or self._stop is None:
                return
            # Event.set() is not a coroutine; schedule it with call_soon.
            self._loop.call_soon_threadsafe(self._stop.set)
            if not self._done.wait(timeout=self._timeout):
                raise ExternalServiceError("MCP knowledge client did not shut down in time")
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=self._timeout)
            self._loop.close()
            self._loop = None
            self._thread = None
            self._session = None
