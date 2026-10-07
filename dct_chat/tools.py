"""The agent's tools: render a board, run a query, look up the docs.

They run inside this process and call the already-loaded engine, so they cost milliseconds
instead of the seconds a fresh `dct` process costs. Each session gets its own set; `lookup` maps
a board name to a path inside that session's folder and nothing else, so a tool can't be pointed
at another session's files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from dct_chat.engine import Engine, EngineError, format_query, format_render

TIMEOUT = 180  # seconds: a slow warehouse shouldn't leave the agent waiting forever
MAX_ROWS = 50

# Maps a board name to its path relative to the workspace, or None if this session has no such board.
BoardLookup = Callable[[str], Path | None]


def _reply(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def render_board(engine: Engine, lookup: BoardLookup, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("board", "")).strip()
    path = lookup(name)
    if path is None:
        return _reply(f"No board named {name!r} yet. Write it to {name.removesuffix('.yml') or '<name>'}.yml in your working folder first.", True)
    try:
        result = await asyncio.wait_for(engine.render(path, "text", args.get("variables") or None), TIMEOUT)
    except EngineError as exc:
        return _reply(str(exc), True)
    except TimeoutError:
        return _reply(f"Rendering took longer than {TIMEOUT}s. A query may be too slow; try narrowing it.", True)
    return _reply(format_render(result), result.status == "failed")


async def run_query(engine: Engine, default_source: str, args: dict[str, Any]) -> dict[str, Any]:
    sql = str(args.get("sql", "")).strip()
    if not sql:
        return _reply("Give the SQL to run.", True)
    limit = max(1, min(int(args.get("limit") or 20), MAX_ROWS))
    source = str(args.get("source") or default_source)
    try:
        result = await asyncio.wait_for(engine.query(sql, source, limit), TIMEOUT)
    except EngineError as exc:
        return _reply(str(exc), True)
    except TimeoutError:
        return _reply(f"The query took longer than {TIMEOUT}s.", True)
    return _reply(format_query(result, limit), not result.get("success"))


async def docs(engine: Engine, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _reply(await engine.docs(args.get("topic"), args.get("search")))
    except EngineError as exc:
        return _reply(str(exc), True)


def build_server(engine: Engine, lookup: BoardLookup, default_source: str) -> Any:
    """The in-process tool server for one session; its tools appear as `mcp__dct__<name>`."""

    @tool(
        "render_board",
        "Validate a board you wrote, run its queries and render it. Returns the status, any errors, "
        "what each chart shows (row counts, value ranges, KPI values) and every warning with its fix. "
        "Call it after every write or edit, and fix everything it reports. Takes a fraction of a second.",
        {
            "type": "object",
            "properties": {
                "board": {"type": "string", "description": "The board's name, e.g. 'sales' for sales.yml in your working folder."},
                "variables": {"type": "object", "description": "Optional values for the board's filters, to test them, e.g. {\"region\": \"North\"}."},
            },
            "required": ["board"],
        },
    )
    async def _render(args: dict[str, Any]) -> dict[str, Any]:
        return await render_board(engine, lookup, args)

    @tool(
        "run_query",
        "Run a SQL query against the data and get back rows. Use it to look at values you need (distinct "
        "filter options, date ranges). It takes the same SQL as a board, including {{ ref('model') }} for dbt models.",
        {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "source": {"type": "string", "description": f"Which source to query. Default: {default_source}."},
                "limit": {"type": "integer", "description": f"Rows to return, up to {MAX_ROWS}. Default 20."},
            },
            "required": ["sql"],
        },
    )
    async def _query(args: dict[str, Any]) -> dict[str, Any]:
        return await run_query(engine, default_source, args)

    @tool(
        "docs",
        "Look up the dbt charts YAML reference: chart types, fields, layout, variables. Call with no arguments for the "
        "list of topics, topic='charts' for a section, or search='small multiples' to search.",
        {
            "type": "object",
            "properties": {"topic": {"type": "string"}, "search": {"type": "string"}},
        },
    )
    async def _docs(args: dict[str, Any]) -> dict[str, Any]:
        return await docs(engine, args)

    return create_sdk_mcp_server("dct", tools=[_render, _query, _docs])
