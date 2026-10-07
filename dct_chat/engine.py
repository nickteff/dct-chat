"""The dbt charts engine, loaded once and used in this process.

Launching `dct` for every step costs seconds of start-up each time (it re-imports dbt, DuckDB and
the chart engine), which on a slow Windows machine adds up to minutes per board. Here the engine
is imported once, kept open, and called directly: a render takes a fraction of a second and a
query a few milliseconds.

All calls are serialized with a lock (the session shares adapters and DuckDB connections) and run
on a worker thread, so the web app stays responsive while one runs.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DOCS_CHARS = 9000  # most of a docs section the agent gets back in one call


class EngineError(Exception):
    """A request the engine couldn't carry out; the message is fit to show the agent."""


class Engine:
    def __init__(self, workspace: Path, dbt_project: Path | None = None) -> None:
        self.workspace = workspace
        self.dbt_project = dbt_project
        self.started_in: float | None = None
        self._session: Any = None
        self._lock = threading.Lock()
        self._ready = asyncio.Event()
        self._failed: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------

    def _open(self) -> None:
        """Build the project session. Imports are here so merely importing this module is cheap."""
        from dbt_charts.agent_api.project_session import ProjectSession
        from dbt_charts.cli.filesystem_project import FilesystemProject

        # dbt_root links the dbt project, so ref() and source() resolve against its manifest.
        project = FilesystemProject(self.workspace, dbt_root=self.dbt_project)
        self._session = ProjectSession.from_project(project, cache=None)

    async def start(self) -> None:
        """Load the engine. Takes seconds (much longer on a slow machine); callers wait on `wait_ready`."""
        began = time.monotonic()

        def boot() -> None:
            import dbt_charts.agent_api as api

            api.warm_process()
            self._open()

        try:
            await asyncio.to_thread(boot)
        except Exception as exc:  # noqa: BLE001 — recorded, then reported to whoever asks for the engine
            self._failed = f"The chart engine failed to start: {type(exc).__name__}: {exc}"
        self.started_in = time.monotonic() - began
        self._ready.set()

    async def wait_ready(self) -> None:
        await self._ready.wait()
        if self._failed:
            raise EngineError(self._failed)

    async def refresh(self) -> None:
        """Re-open the session, e.g. after the dbt project's manifest was rebuilt."""
        await self.wait_ready()

        def reopen() -> None:
            with self._lock:
                self._session.close()
                self._open()

        await asyncio.to_thread(reopen)

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        await self.wait_ready()

        def locked() -> Any:
            with self._lock:
                return fn(self._session, *args, **kwargs)

        return await asyncio.to_thread(locked)

    # -- operations --------------------------------------------------------------------------

    async def render(
        self,
        board: Path,
        fmt: str,
        variables: dict[str, Any] | None = None,
        **options: Any,
    ) -> Any:
        """Render a board (path relative to the workspace). Returns the engine's render result:
        `.status`, `.data` (text, bytes or a dict by format), `.warnings`, `.validation_errors`,
        `.chart_errors`. Raises `EngineError` if the board can't be found."""
        return await self._run(_render, board, fmt, variables, options)

    async def query(self, sql: str, source: str, limit: int = 20) -> dict[str, Any]:
        """Run SQL (`{{ ref() }}` allowed) against a source; the result as a plain dict."""
        return await self._run(_query, sql, source, limit)

    async def docs(self, topic: str | None, search: str | None) -> str:
        """The offline YAML reference, as text for the agent. No session needed."""
        await self.wait_ready()
        return await asyncio.to_thread(_docs, topic, search)


# These run under the lock, on a worker thread, with the open session.


def _render(session: Any, board: Path, fmt: str, variables: dict[str, Any] | None, options: dict[str, Any]) -> Any:
    from dbt_charts.agent_api._paths import resolve_board_or_error
    from dbt_charts.core.diagnostics import Diagnostic

    resolved = resolve_board_or_error(board, session.project)
    if isinstance(resolved, Diagnostic):
        raise EngineError(f"{resolved.code}: {resolved.message}")
    return session.render_board(board=resolved, format=fmt, variables=variables or None, **options)


def _query(session: Any, sql: str, source: str, limit: int) -> dict[str, Any]:
    result = session.execute_query(sql, source=source, limit=limit)
    return dict(result.model_dump(mode="json", exclude_none=True))


def _docs(topic: str | None, search: str | None) -> str:
    from dbt_charts.agent_api.docs import docs

    result = docs(topic=topic or None, search=search or None, limit=4)
    if result.errors:
        return "\n".join([*result.errors, *result.hints])
    if result.mode == "topic" and result.topic:
        return result.topic.content[:DOCS_CHARS]
    if result.mode == "search":
        hits = [f"## {h.title}: {h.section}\n{h.content[:1500]}" for h in result.search]
        return "\n\n".join(hits) or "No matches."
    return "Topics (ask for one with topic=...):\n" + "\n".join(f"- {t.id}: {t.description}" for t in result.topics)


# -- text for the agent ----------------------------------------------------------------------


def _where(diag: Any) -> str:
    line = getattr(getattr(diag, "range", None), "start_line", None)
    return f" (line {line})" if line else ""


def format_render(result: Any) -> str:
    """A render result as compact text: status, any errors, what each chart shows, and fixes."""
    lines = [f"status: {result.status}"]
    for err in [*(result.validation_errors or []), *(result.chart_errors or [])]:
        lines.append(f"ERROR {err.code}{_where(err)}: {err.message}")
    if result.board_error:
        lines.append(f"ERROR {result.board_error.code}: {result.board_error.message}")
    if isinstance(result.data, str) and result.data.strip():
        lines.append(result.data.strip())  # includes a "Warnings" section
    fixes = [f"- {w.code}: {w.fix}" for w in (result.warnings or []) if getattr(w, "fix", None)]
    if fixes:
        lines.append("How to fix:\n" + "\n".join(fixes))
    if result.status == "ok" and not result.warnings:
        lines.append("No errors and no warnings.")
    return "\n".join(lines)


def format_query(result: dict[str, Any], limit: int) -> str:
    """Query rows as compact JSON lines, with the column list and any errors."""
    if not result.get("success"):
        return "QUERY FAILED: " + "; ".join(result.get("errors") or ["unknown error"])
    rows = (result.get("data") or [])[:limit]
    head = f"columns: {', '.join(result.get('columns') or [])}\nrows: {len(rows)}" + (" (more exist)" if result.get("truncated") else "")
    return head + "\n" + "\n".join(json.dumps(r, default=str) for r in rows)
