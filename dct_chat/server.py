"""Chat UI backend: Claude Agent SDK sessions building dbt charts dashboards.

Each browser is a *session*, identified by a random cookie. A session owns:

- its agent conversation (one SDK client, freed when idle and resumed on demand);
- its boards, in ``workspace/charts/<session>/``, which is also the agent's working
  directory, so the agent cannot read or write another session's files.

One ``dct serve`` subprocess serves every session's boards; the proxy below only
forwards a session's own board paths. Warehouse access and the query cache are
shared by all sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import httpx
import psutil
import uvicorn
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel
from starlette.background import BackgroundTask

from dct_chat import project as dbt
from dct_chat.runtime import child_env, tool

ROOT = Path(__file__).resolve().parent.parent
# Link a real dbt project with DCT_CHAT_DBT_PROJECT (or `dct-chat --dbt-project`). Boards then
# go in a scratch workspace of their own, never into the dbt project.
DBT_PROJECT = os.environ.get("DCT_CHAT_DBT_PROJECT") or None
DBT_TARGET = os.environ.get("DCT_CHAT_DBT_TARGET") or None
PROFILES_DIR = os.environ.get("DCT_CHAT_PROFILES_DIR") or None
_default_workspace = (
    ROOT / "workspaces" / Path(DBT_PROJECT).expanduser().resolve().name if DBT_PROJECT else ROOT / "workspace"
)
WORKSPACE = Path(os.environ.get("DCT_CHAT_WORKSPACE", _default_workspace)).resolve()
CHARTS = WORKSPACE / "charts"
THUMBS = WORKSPACE / ".thumbs"
STATIC = Path(__file__).resolve().parent / "static"

APP_PORT = int(os.environ.get("DCT_CHAT_PORT", "8800"))
PREVIEW_PORT = int(os.environ.get("DCT_CHAT_PREVIEW_PORT", "8801"))
SESSION_TTL = int(os.environ.get("DCT_CHAT_SESSION_TTL", "1800"))  # idle seconds before an agent is freed
MAX_TURNS = int(os.environ.get("DCT_CHAT_MAX_TURNS", "4"))  # agent turns running at once, across sessions

COOKIE = "dct_sid"
SID_RE = re.compile(r"[a-f0-9]{16}")

SYSTEM_PROMPT = """\
You build dbt charts dashboards for the user.

- Start with `dct skills intro`, then follow the dct-board-build skill.
- __DATA__
- Your working directory is the user's private boards folder. Write each board to
  `<name>.yml` right here (never in a subfolder or elsewhere). After every edit run
  `dct validate <name>.yml && dct render <name>.yml --format text`
  and fix every warning before you answer.
- The finished board appears inline in this chat automatically once you are done.
  Don't give URLs, don't start servers, and don't paste the whole YAML back. Reply
  in two or three plain sentences: what the board shows and what you chose. If the
  user asks to see it, tell them it is shown right here in the conversation.
- When asked to change a board, edit the existing file rather than starting over.
- A message may begin with `[Context: board <name>, chart <id>]`: the user pointed
  at that chart. Change only that chart unless they say otherwise.
- Don't narrate your steps. Write nothing until your final reply.
- End your final reply with one line: `NEXT: <idea> | <idea> | <idea>`, three
  short follow-up requests (under eight words each) the user might want next.
- You are not done until `<name>.yml` exists and renders with no warnings.
  Never answer with a plan or a question when the request can be built now;
  choose sensible defaults and say what you chose.
- Be quick: the schema and syntax cheatsheet are below, so skip exploring the
  schema, read a skill at most once, and batch commands into one Bash call.
  Query only for values you still need (distinct filter options, date ranges).
"""

DEMO_DATA = (
    "Data: the `examples_db` source (DuckDB). Explore it with\n"
    "  `dct query examples_db \"SELECT ...\"` before writing any board."
)
DBT_DATA = (
    "Data: the `warehouse` source, a dbt project (`__NAME__`, adapter __ADAPTER__). Query the project's\n"
    "  models with `{{ ref('model') }}` and its sources with `{{ source('src', 'table') }}`; never\n"
    "  hard-code table names. Prefer marts (fct_/dim_) over staging models. Column lists below come\n"
    "  from the project's YAML and can be incomplete, so before relying on a column check it with\n"
    "  `dct query warehouse \"SELECT * FROM {{ ref('model') }} LIMIT 5\"`. Every board sets\n"
    "  `source: warehouse`."
)
DEMO_SUGGESTIONS = [
    "Monthly revenue trend, with revenue by category and a region filter",
    "A KPI row for revenue and units, plus a table of the top products",
    "Compare revenue by region as a bar chart and a small-multiples line",
]

MODELS = {
    "haiku": "Haiku (fastest)",
    "sonnet": "Sonnet",
    "opus": "Opus (most capable)",
}
DEFAULT_MODEL = os.environ.get("DCT_CHAT_MODEL", "sonnet")
EFFORT = os.environ.get("DCT_CHAT_EFFORT", "low")


def _schema_summary() -> str:
    """One line per example table (skipping lab_* fixtures): name(col type, ...)."""
    try:
        con = duckdb.connect(str(WORKSPACE / "data" / "examples.duckdb"), read_only=True)
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_name NOT LIKE 'lab\\_%' ESCAPE '\\'"
            " ORDER BY table_name, ordinal_position"
        ).fetchall()
        con.close()
    except duckdb.Error:
        return ""
    tables: dict[str, list[str]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, []).append(f"{column} {dtype}")
    return "\n".join(f"- {t}({', '.join(cols)})" for t, cols in tables.items())


def _cheatsheet() -> str:
    out = subprocess.run(
        [tool("dct"), "docs", "cheatsheet"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=WORKSPACE,
        env=child_env(),
        check=False,
    )
    return out.stdout.strip()


@lru_cache(maxsize=1)
def _cheatsheet_cached() -> str:
    return _cheatsheet()


def _system_prompt() -> str:
    """The agent's instructions, for the linked dbt project or the bundled demo data."""
    if _project.summary is not None and _project.project is not None:
        data = DBT_DATA.replace("__NAME__", _project.project.name).replace("__ADAPTER__", _project.summary.adapter or "unknown")
        parts = [SYSTEM_PROMPT.replace("__DATA__", data), _project.summary.prompt_text]
    else:
        parts = [SYSTEM_PROMPT.replace("__DATA__", DEMO_DATA)]
        if schema := _schema_summary():
            parts.append(f"Tables in `examples_db` (DuckDB, schema main):\n{schema}")
    if sheet := _cheatsheet_cached():
        parts.append(f"dbt charts syntax cheatsheet (from `dct docs cheatsheet`):\n{sheet}")
    return "\n\n".join(parts)


ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(dct:*)"]

# `dct` runs outside the agent's file permissions, so keep its commands inside the
# session's own folder: no parent-directory hops, no absolute paths, no ~.
_ESCAPES_FOLDER = re.compile(
    r"(^|[\s'\"=])\.\.([\\/]|$)"  # a `..` hop, with either slash
    r"|~"  # the home directory
    r"|\s/(?!dev/null)[\w.~-]"  # a POSIX absolute path
    r"|(^|[\s'\"=])[A-Za-z]:[\\/]"  # a Windows drive path (C:\ or C:/)
    r"|(^|[\s'\"=])\\\\"  # a UNC path (\\server\share)
    r"|\$\(|`"  # command substitution
)


async def _guard_bash(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
    command = str((input_data.get("tool_input") or {}).get("command", ""))
    if _ESCAPES_FOLDER.search(command):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Stay inside your boards folder: use relative file names only.",
            }
        }
    return {}


# ---------------------------------------------------------------------------
# The linked dbt project
# ---------------------------------------------------------------------------


@dataclass
class ProjectState:
    project: dbt.DbtProject | None = None
    summary: dbt.Summary | None = None
    error: str | None = None
    warning: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_project = ProjectState()


async def _setup_project(force: bool = False) -> None:
    """Read the dbt project, make sure its manifest is current, and digest it for the agent.

    Failures are recorded for the UI to show; they never stop the app from starting.
    """
    if not DBT_PROJECT:
        return
    async with _project.lock:
        _project.error = _project.warning = None
        try:
            project = dbt.load_project(DBT_PROJECT, DBT_TARGET, PROFILES_DIR)
            _project.project = project
            _project.warning = dbt.write_charts_config(WORKSPACE, project)
            if force or dbt.manifest_is_stale(project):
                if problem := await dbt.parse(project):
                    if not project.manifest.is_file():
                        raise dbt.ProjectError(problem)
                    _project.warning = f"Using the previous manifest. {problem}"
            summary = dbt.summarize(project)
            if summary.undocumented:  # the YAML describes no columns: ask the warehouse, so the agent needn't
                found = await dbt.discover_columns(WORKSPACE, summary.undocumented)
                if found:
                    summary = dbt.summarize(project, found)
            _project.summary = summary
        except dbt.ProjectError as exc:
            _project.error, _project.summary = str(exc), None


def _project_suggestions() -> list[str]:
    if _project.summary is None:
        return []
    names = [m.replace("_", " ") for m in _project.summary.marts] or []
    if not names:
        return ["What can I build from this project? Show me the main model as a table"]
    first, second = names[0], (names[1] if len(names) > 1 else None)
    return [
        f"A summary board for {first}: key totals and a breakdown",
        f"How has {first} changed over time?",
        f"Compare {second} by its main categories" if second else f"Top 10 rows of {first} as a table",
    ]


def _project_status() -> dict[str, object]:
    if not DBT_PROJECT:
        return {"mode": "demo", "name": "Demo data", "suggestions": DEMO_SUGGESTIONS}
    project, summary = _project.project, _project.summary
    return {
        "mode": "dbt",
        "name": project.name if project else Path(DBT_PROJECT).expanduser().name,
        "profile": project.profile if project else None,
        "target": DBT_TARGET or "profile default",
        "adapter": summary.adapter if summary else None,
        "models": summary.models if summary else 0,
        "sources": summary.sources if summary else 0,
        "metrics": summary.metrics if summary else 0,
        "manifest_at": summary.generated_at if summary else None,
        "error": _project.error,
        "warning": _project.warning,
        "suggestions": _project_suggestions(),
    }


def _outside_folder(folder: Path, raw: str) -> bool:
    """True if a tool path (maybe relative, maybe a glob pattern) leaves `folder`."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = folder / path
    # normpath collapses `..` without touching wildcards; resolve then follows symlinks
    target = Path(os.path.normpath(path)).resolve()
    return folder.resolve() not in (target, *target.parents)


def _guard_paths(folder: Path) -> Any:
    """PreToolUse hook: file tools may only touch this session's own folder.

    A hook's deny beats an allow rule, so this holds even though the tools are allowed.
    """

    async def guard(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        args = input_data.get("tool_input") or {}
        paths = [str(args[k]) for k in ("file_path", "path", "pattern", "glob", "notebook_path") if args.get(k)]
        if any(_outside_folder(folder, p) for p in paths):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "You can only use files in your own boards folder.",
                }
            }
        return {}

    return guard


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass
class Session:
    sid: str
    model: str = DEFAULT_MODEL
    client: ClaudeSDKClient | None = None
    sdk_id: str | None = None  # the agent's own session id, for resuming after it is freed
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)

    @property
    def dir(self) -> Path:
        return CHARTS / self.sid


_sessions: dict[str, Session] = {}
_turn_gate = asyncio.Semaphore(MAX_TURNS)


def _session(request: Request) -> Session:
    sid: str = request.state.sid
    sess = _sessions.get(sid)
    if sess is None:
        sess = _sessions[sid] = Session(sid)
    sess.last_used = time.monotonic()
    return sess


def _options(sess: Session, resume: bool) -> ClaudeAgentOptions:
    sess.dir.mkdir(parents=True, exist_ok=True)
    return ClaudeAgentOptions(
        cwd=str(sess.dir),  # the agent sees only this session's boards
        system_prompt={"type": "preset", "preset": "claude_code", "append": _system_prompt()},
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[_guard_bash]),
                HookMatcher(matcher="Read|Write|Edit|Glob|Grep|NotebookEdit", hooks=[_guard_paths(sess.dir)]),
            ]
        },
        env=child_env(),
        model=sess.model,
        effort=EFFORT,  # type: ignore[arg-type]
        include_partial_messages=True,
        max_turns=40,
        resume=sess.sdk_id if resume else None,
    )


async def _new_client(sess: Session, resume: bool = False) -> ClaudeSDKClient:
    if sess.client is not None:
        await sess.client.disconnect()
        sess.client = None
    if not resume:
        sess.sdk_id = None
    client = ClaudeSDKClient(options=_options(sess, resume and sess.sdk_id is not None))
    await client.connect()
    sess.client = client
    return client


async def _reap_idle_sessions() -> None:
    """Free the agent process of sessions that have been quiet; they resume on demand."""
    while True:
        await asyncio.sleep(30)
        now = time.monotonic()
        for sess in list(_sessions.values()):
            if sess.client is not None and not sess.lock.locked() and now - sess.last_used > SESSION_TTL:
                client, sess.client = sess.client, None
                await client.disconnect()


# ---------------------------------------------------------------------------
# Board server
# ---------------------------------------------------------------------------

_preview: subprocess.Popen[bytes] | None = None
# Boards are served by the `dct serve` subprocess. Proxying them through this app
# puts them on the chat page's origin, so the page can measure an embedded board
# and size its card to fit. Board pages use root-relative URLs, so no prefix.
_upstream = httpx.AsyncClient(base_url=f"http://127.0.0.1:{PREVIEW_PORT}", timeout=None)


def _stop_tree(procs: list[psutil.Process]) -> None:
    """Terminate processes and everything they started.

    A Windows console script (`dct.exe`) is a small launcher that runs Python as a child, so
    stopping only the launcher leaves the server running.
    """
    victims = list(procs)
    for proc in procs:
        try:
            victims += proc.children(recursive=True)
        except psutil.Error:
            pass
    for proc in victims:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=5)


def _clear_stale_preview() -> None:
    """Stop a board server left over from an earlier run of this app on our port.

    Only a process that is `... serve --port <ours>` for this workspace is touched; anything
    else holding the port is left alone and surfaces as a startup error.
    """
    stale = []
    for proc in psutil.process_iter(["cmdline"]):
        cmd = proc.info["cmdline"] or []
        if "serve" in cmd and "--port" in cmd and str(WORKSPACE) in cmd:
            if cmd[cmd.index("--port") + 1 : cmd.index("--port") + 2] == [str(PREVIEW_PORT)]:
                stale.append(proc)
    _stop_tree(stale)


async def _ensure_preview() -> None:
    """Start the board server if it isn't running, and wait until it answers."""
    global _preview
    if _preview is not None and _preview.poll() is None:
        return
    _clear_stale_preview()
    await asyncio.sleep(0.5)
    _preview = subprocess.Popen(
        [
            tool("dct"),
            "serve",
            "--port",
            str(PREVIEW_PORT),
            "--project-dir",
            str(WORKSPACE),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_env(),
    )
    for _ in range(60):
        try:
            await _upstream.get("/")
            return
        except httpx.TransportError:
            await asyncio.sleep(0.25)
    raise RuntimeError(f"board server did not start on port {PREVIEW_PORT}")


async def _prewarm_thumbs() -> None:
    """Generate any missing board thumbnails so the sidebar fills in quickly."""
    for session_dir in sorted(CHARTS.iterdir()) if CHARTS.exists() else []:
        if session_dir.is_dir() and SID_RE.fullmatch(session_dir.name):
            for name in _board_names(session_dir.name):
                await _thumb(session_dir.name, name)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await _setup_project()
    await _ensure_preview()
    background = [
        asyncio.create_task(_prewarm_thumbs()),
        asyncio.create_task(_reap_idle_sessions()),
    ]
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for sess in _sessions.values():
            if sess.client is not None:
                await sess.client.disconnect()
        if _preview is not None:
            try:
                _stop_tree([psutil.Process(_preview.pid)])
            except psutil.Error:
                pass
        await _upstream.aclose()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def session_cookie(request: Request, call_next: Any) -> Any:
    """Identify the browser by a random cookie, issuing one on first contact.

    The value becomes a folder name, so anything that isn't exactly our own format
    is discarded and replaced rather than trusted.
    """
    sid = request.cookies.get(COOKIE, "")
    fresh = not SID_RE.fullmatch(sid)
    if fresh:
        sid = secrets.token_hex(8)
    request.state.sid = sid
    response = await call_next(request)
    if fresh:
        response.set_cookie(COOKIE, sid, max_age=365 * 86400, httponly=True, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# Boards (always scoped to one session's folder)
# ---------------------------------------------------------------------------


def _board_name(sid: str, path: Path) -> str | None:
    """A board's name (path inside the session folder, no extension), else None."""
    try:
        rel = path.resolve().relative_to((CHARTS / sid).resolve())
    except ValueError:
        return None
    if rel.suffix not in (".yml", ".yaml") or any(p.startswith(("_", ".")) for p in rel.parts):
        return None
    return rel.with_suffix("").as_posix()


def _board_names(sid: str) -> list[str]:
    folder = CHARTS / sid
    if not folder.is_dir():
        return []
    return sorted(n for p in folder.rglob("*.y*ml") if (n := _board_name(sid, p)) is not None)


def _board_state(sid: str) -> dict[str, int]:
    """Modification time of every board in the session, keyed by name."""
    state = {}
    for name in _board_names(sid):
        if (path := _board_file(sid, name)) is not None:
            state[name] = path.stat().st_mtime_ns
    return state


def _board_file(sid: str, name: str) -> Path | None:
    """The board's file, only if it lives inside this session's folder."""
    folder = (CHARTS / sid).resolve()
    for ext in (".yml", ".yaml"):
        path = (folder / f"{name}{ext}").resolve()
        if path.is_file() and folder in path.parents:
            return path
    return None


def _board_title(path: Path) -> str:
    """The board's authored `title:` (top-level line), else a name made from its file."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip().strip("\"'") or path.stem
    return path.stem.replace("_", " ").replace("-", " ")


@app.get("/api/session")
async def session_info(request: Request) -> dict[str, str]:
    sess = _session(request)
    return {"sid": sess.sid, "base": f"/{sess.sid}"}


def _legacy_boards() -> list[Path]:
    """Boards from before sessions existed: they sit directly in charts/, owned by no one."""
    return sorted(CHARTS.glob("*.y*ml")) if CHARTS.exists() else []


@app.get("/api/legacy")
async def legacy() -> dict[str, int]:
    return {"count": len(_legacy_boards())}


@app.post("/api/legacy/adopt")
async def adopt_legacy(request: Request) -> dict[str, int]:
    """Move the unowned boards into this session. Explicit, so a stray request can't claim them."""
    sess = _session(request)
    sess.dir.mkdir(parents=True, exist_ok=True)
    moved = _legacy_boards()
    for path in moved:
        shutil.move(path, sess.dir / path.name)
    return {"moved": len(moved)}


@app.get("/api/boards")
async def boards(request: Request) -> dict[str, object]:
    sess = _session(request)
    items = []
    for name in _board_names(sess.sid):
        path = _board_file(sess.sid, name)
        if path is not None:
            items.append({"slug": name, "title": _board_title(path), "mtime": path.stat().st_mtime_ns})
    items.sort(key=lambda b: b["mtime"], reverse=True)  # newest first
    return {"boards": items, "base": f"/{sess.sid}"}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str


class ResetRequest(BaseModel):
    model: str | None = None


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _tool_phase(block: ToolUseBlock) -> str:
    """Coarse stage a tool call belongs to; the UI shows stages, never commands."""
    args = block.input
    if block.name in ("Write", "Edit"):
        return "write"
    if block.name == "Bash":
        command = str(args.get("command", ""))
        if "dct validate" in command or "dct render" in command:
            return "check"
        if "dct skills" in command or "dct docs" in command:
            return "learn"
        return "explore"
    return "explore"


def _split_next(text: str) -> tuple[str, list[str]]:
    """Separate a trailing `NEXT: a | b | c` line from a reply."""
    kept: list[str] = []
    ideas: list[str] = []
    for line in text.splitlines():
        if line.strip().upper().startswith("NEXT:"):
            ideas = [i.strip() for i in line.split(":", 1)[1].split("|") if i.strip()]
        else:
            kept.append(line)
    return "\n".join(kept).strip(), ideas[:3]


async def _stream_turn(sess: Session, message: str) -> AsyncIterator[str]:
    if DBT_PROJECT and _project.summary is None:
        yield _sse("error", {"message": f"The dbt project isn't connected. {_project.error or 'Open the project panel for details.'}"})
        return
    async with sess.lock, _turn_gate:
        try:
            if sess.client is None:
                try:
                    await _new_client(sess, resume=True)
                except Exception:  # noqa: BLE001 — an unresumable session just starts fresh
                    await _new_client(sess)
            client = sess.client
            assert client is not None
            before = _board_state(sess.sid)
            sess.last_used = time.monotonic()
            await client.query(message)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    delta = msg.event.get("delta") or {}
                    if (
                        msg.parent_tool_use_id is None
                        and msg.event.get("type") == "content_block_delta"
                        and delta.get("type") == "text_delta"
                    ):
                        yield _sse("delta", {"text": delta.get("text", "")})
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            text, ideas = _split_next(block.text)
                            if text:
                                yield _sse("text", {"text": text})
                            if ideas:
                                yield _sse("suggest", {"ideas": ideas})
                        elif isinstance(block, ToolUseBlock):
                            yield _sse("phase", {"phase": _tool_phase(block)})
                elif isinstance(msg, ResultMessage):
                    sess.sdk_id = msg.session_id
                    if msg.is_error:
                        yield _sse("error", {"message": msg.result or "The turn failed."})
            after = _board_state(sess.sid)
            for name in sorted(n for n, mtime in after.items() if before.get(n) != mtime):
                yield _sse("board", {"slug": name})
            yield _sse("done", {})
        except Exception as exc:  # noqa: BLE001 — surface any agent failure to the UI
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            sess.last_used = time.monotonic()


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(_stream_turn(_session(request), req.message), media_type="text/event-stream")


@app.post("/api/reset")
async def reset(request: Request, req: ResetRequest | None = None) -> dict[str, str]:
    """Start a fresh conversation. The session's boards are kept."""
    sess = _session(request)
    if req is not None and req.model in MODELS:
        sess.model = req.model
    async with sess.lock:
        await _new_client(sess)
    return {"model": sess.model}


@app.get("/api/models")
async def models(request: Request) -> dict[str, object]:
    return {"models": MODELS, "current": _session(request).model}


@app.post("/api/stop")
async def stop(request: Request) -> dict[str, bool]:
    """Interrupt this session's turn in progress; the stream ends and its lock is released."""
    sess = _session(request)
    if sess.client is not None:
        await sess.client.interrupt()
    return {"ok": True}


@app.get("/api/project")
async def project_status() -> dict[str, object]:
    return _project_status()


@app.post("/api/project/refresh")
async def project_refresh() -> dict[str, object]:
    """Re-run `dbt parse` and re-read the project. New conversations pick up the changes."""
    await _setup_project(force=True)
    return _project_status()


@app.post("/api/project/check")
async def project_check() -> dict[str, object]:
    """Run a trivial query through dct to prove the warehouse connection works."""
    source = dbt.SOURCE_NAME if DBT_PROJECT else "examples_db"
    ok, detail = await asyncio.to_thread(dbt.check_connection, WORKSPACE, source)
    return {"ok": ok, "detail": detail}


# ---------------------------------------------------------------------------
# Render-backed endpoints: details, export, thumbnails
# ---------------------------------------------------------------------------


async def _render(
    sid: str, name: str, fmt: str, params: list[tuple[str, str]], out: Path
) -> str | None:
    """Run `dct render` for one of the session's boards, with query params as variables.

    Returns an error message, or None on success.
    """
    board = _board_file(sid, name)
    if board is None:
        return f"No board named {name!r}."
    args = [
        tool("dct"), "render", str(board),
        "--format", fmt, "-o", str(out), "--project-dir", str(WORKSPACE),
        "--allow-chart-errors",
    ]
    for key, value in params:
        args += ["--var", f"{key}={value}"]
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=WORKSPACE, env=child_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not out.exists():
        return stderr.decode(errors="replace")[-400:] or "Render failed."
    return None


@app.get("/api/info/{slug:path}")
async def info(slug: str, request: Request) -> JSONResponse:
    """What a board is built from: each query's SQL, row count and columns."""
    sess = _session(request)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "board.json"
        error = await _render(sess.sid, slug, "data", request.query_params.multi_items(), out)
        if error:
            return JSONResponse({"error": error}, status_code=422)
        data = json.loads(out.read_text(encoding="utf-8"))
    queries = []
    for name, q in (data.get("queries") or {}).items():
        rows = q.get("rows") or []
        queries.append(
            {
                "name": name,
                "sql": q.get("sql"),
                "rows": len(rows),
                "columns": list(rows[0]) if rows else [],
            }
        )
    return JSONResponse(
        {
            "title": data.get("title"),
            "queries": queries,
            "charts": len(data.get("charts") or {}),
            "warnings": data.get("warnings") or [],
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )


THUMB_WIDTH = 240
_thumb_gate = asyncio.Semaphore(2)  # PNG renders are CPU-heavy; don't stampede
_thumb_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def _thumb(sid: str, name: str) -> tuple[Path | None, str | None]:
    """A small PNG of a board, cached until the board file changes."""
    board = _board_file(sid, name)
    if board is None:
        return None, f"No board named {name!r}."
    folder = THUMBS / sid
    stem = name.replace("/", "__")
    path = folder / f"{stem}-{board.stat().st_mtime_ns}.png"
    if path.exists():
        return path, None
    async with _thumb_locks.setdefault((sid, name), asyncio.Lock()):
        if not path.exists():
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                big = Path(tmp) / "board.png"
                async with _thumb_gate:
                    error = await _render(sid, name, "png", [], big)
                if error:
                    return None, error
                for old in folder.glob(f"{stem}-*.png"):
                    old.unlink(missing_ok=True)
                with Image.open(big) as img:
                    height = round(img.height * THUMB_WIDTH / img.width)
                    img.convert("RGB").resize((THUMB_WIDTH, height), Image.LANCZOS).save(path, optimize=True)
    return path, None


@app.get("/api/thumb/{slug:path}", response_model=None)
async def thumb(slug: str, request: Request) -> FileResponse | JSONResponse:
    path, error = await _thumb(_session(request).sid, slug)
    if path is None:
        return JSONResponse({"error": error}, status_code=404)
    # The URL carries the board's mtime, so a cached copy is never stale. Private: per session.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})


EXPORT_FORMATS = {"png", "pdf", "html", "svg"}


@app.get("/api/export/{fmt}/{slug:path}", response_model=None)
async def export(fmt: str, slug: str, request: Request) -> FileResponse | JSONResponse:
    """Render a board (with the current filters) to a downloadable file."""
    if fmt not in EXPORT_FORMATS:
        return JSONResponse({"error": f"Unsupported format {fmt!r}."}, status_code=400)
    sess = _session(request)
    tmp = Path(tempfile.mkdtemp())
    out = tmp / f"{Path(slug).name}.{fmt}"
    error = await _render(sess.sid, slug, fmt, request.query_params.multi_items(), out)
    if error:
        shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse({"error": error}, status_code=422)
    return FileResponse(
        out,
        filename=out.name,
        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
    )


# ---------------------------------------------------------------------------
# Page + board proxy
# ---------------------------------------------------------------------------


@app.get("/")
async def index(request: Request) -> FileResponse:
    _session(request)
    return FileResponse(STATIC / "index.html")


# Shared assets a board page loads, besides the board itself.
_SHARED_PATHS = {"static", "__livereload"}


@app.get("/{path:path}")
async def board_proxy(path: str, request: Request) -> Any:
    """Forward a board request to the board server, but only for this session's own boards."""
    sess = _session(request)
    first = path.split("/", 1)[0]
    if first != sess.sid and first not in _SHARED_PATHS:
        return JSONResponse({"error": "Not found."}, status_code=404)
    query = request.url.query
    target = f"/{path}" + (f"?{query}" if query else "")
    try:
        upstream = await _upstream.send(_upstream.build_request("GET", target), stream=True)
    except httpx.ConnectError:
        await _ensure_preview()
        upstream = await _upstream.send(_upstream.build_request("GET", target), stream=True)
    return StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        background=BackgroundTask(upstream.aclose),
    )


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=APP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
