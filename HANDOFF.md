# Handoff: making dct-chat fast on a slow Windows machine

You're an agent picking this up on the Windows machine where the problem shows. Read this first, then `CLAUDE.md`
(architecture and the rules that must stay true). Everything here was written on a Mac; Windows is where it needs proving.

## The problem

On the user's Windows PC:

- The board server took **36 seconds** to become ready (`[dct-chat] Board server ready (36s)`); on the Mac it takes about 4.
- Building one board takes **minutes**; on the Mac a build takes 25 to 45 seconds.
- The very first `uv run dct-chat` was also "about a minute", which is partly expected (a one-time install of about 800MB).

## What is measured, and what is only a theory

**Measured (Mac, `uv run python scripts/timing.py`):** every `dct` command pays a large fixed start-up cost, because each
one is a new Python process that imports dbt, DuckDB and the chart engine:

| command | time |
|---|---|
| `dct --version` | 0.6s |
| `dct docs cheatsheet` | 2.4s |
| `dct skills board-build` | 0.5s |
| `dct query ... "SELECT 1"` | **3.1s** |
| `dct validate board.yml` | **4.4s** |
| `dct render board.yml --format text` | **5.6s** (again: 5.6s, so it isn't cached) |

**Measured (Mac, in-process prototype):** with the engine already loaded in a long-lived process, the same work is
**render 1.2s the first time then 0.1s; a query 0.02s.** (First-time import and `warm_process()` cost about 4s, paid once.)

**Theory, not yet measured on Windows:** a board build runs about 8 `dct` commands (read skills, explore data, validate,
render, fix, render again). If a process start is about 9x slower on this PC (the board server's 36s vs 4s suggests that),
8 commands cost several minutes. Likely causes of the slowdown on Windows: antivirus real-time scanning of every new
process and every imported file, slow process creation, and a cold bytecode cache. Nothing here is proven yet.

## Do this first (measure before changing anything)

PowerShell, in the repo:

```powershell
git pull
uv sync
uv run python scripts/timing.py
```

`scripts/timing.py` prints how long each building block takes on this machine, including `python -c pass` (the cost of
starting any process), the bundled Claude Code engine, and whether `bash` is on the PATH. **Record the output in the
section at the bottom of this file and commit it.** Then:

```powershell
uv run python scripts/smoke_test.py     # everything except a live Claude turn; should print ALL PASSED
```

Interpret the timing numbers:

- `python -c pass` already slow (over about 1s) means process creation or antivirus is the bottleneck, and the fix is
  fewer process launches (below), plus an antivirus exclusion for the repo folder if the user can arrange one.
- `python -c pass` fast but `dct --version` slow means importing the engine is slow; the in-process engine (below) fixes it.
- `bash` NOT FOUND means Claude Code's shell tool can't run here at all; install Git for Windows. The in-process engine
  removes the agent's need for a shell for its own work.

## The planned fix: stop launching a process per command

Give the agent **in-process tools** instead of `Bash(dct:*)`. The app loads the engine once at start-up and the tools call
it directly (the SDK runs tools inside this process: `claude_agent_sdk.tool` and `create_sdk_mcp_server`).

Tools, created per session so each can only reach that session's folder:

- `render_board(board, variables?)`: validate, compile and render; return status, errors, warnings and the text summary.
- `run_query(sql, source?, limit?)`: run SQL (`{{ ref() }}` allowed) and return columns and rows.
- `docs(topic?, search?)`: `dbt_charts.agent_api.docs.docs(...)` returns the same content as `dct docs`.

Then: restrict the built-in tools (`ClaudeAgentOptions(tools=["Read","Write","Edit","Glob","Grep"])`), drop `Bash` and
`_guard_bash`, keep `_guard_paths` for the file tools, rewrite `SYSTEM_PROMPT` and `DBT_DATA` to name the new tools, and
stop telling the agent to read `dct skills` (that is two more process launches and about 40KB of text every turn; write a
short design guide into the prompt instead). Update `_tool_phase` for the new tool names.

Working prototype (demo workspace; this is exactly what I ran on the Mac):

```python
from pathlib import Path
import dbt_charts.agent_api as api
from dbt_charts.agent_api.project_session import ProjectSession
from dbt_charts.agent_api._paths import resolve_board_or_error

api.warm_process()                                   # once, at start-up
session = ProjectSession.open(Path("/path/to/workspace"))
board = resolve_board_or_error(Path("charts/<sid>/board.yml"), session.project)   # relative to the project root
result = session.render_board(board=board, format="text")        # .status, .data (text), .warnings, .validation_errors, .chart_errors
q = session.execute_query("SELECT ...", source="examples_db", limit=20)
q.model_dump(mode="json", exclude_none=True)        # success, columns, data, errors, row_count, truncated
```

Shape of a failed render: `status: "failed"` with `validation_errors: [{code, message, path, range{file,start_line}, ...}]`;
a runtime failure is `status: "partial"` with `chart_errors`. Format those compactly for the model (code, message,
file:line); don't dump the whole JSON.

### Known problems to solve (found in the prototype)

1. **A linked dbt project doesn't work in-process yet.** `ProjectSession.open(workspace)` ignores the `dbt_project_dir:`
   key in the generated `dbt_charts.yml`, so `ref()` fails with *"dbt_profile source requires a dbt project, but this
   adapter has no dbt_project_path configured"* (and a `WARN-DBT-MANIFEST-MISSING`). The CLI handles it, and so does
   `dct query`. `FilesystemProject(root, dbt_root=...)` takes a `dbt_root`; try
   `ProjectSession.from_project(FilesystemProject(workspace, dbt_root=dbt_project_path))`. Read how
   `dbt_charts/cli/` builds its project to confirm. **Verify with the `tests/fixtures/mini_dbt` fixture** (the smoke test
   already seeds and runs it).
2. **Thread safety.** The engine is synchronous. Run calls with `asyncio.to_thread` and serialize them with a lock until
   proven safe; sessions share one engine.
3. **Start-up order.** Importing the engine takes a while on a slow machine. Do it in the background and make tool calls
   `await` a readiness event. The app must still answer HTTP immediately (it does now).
4. **Other places that still launch `dct`:** `_render` in `server.py` (Details, thumbnails and exports shell out to
   `dct render`), `discover_columns` and `check_connection` in `project.py`, and the board server itself. After the agent
   tools, convert these to the in-process session too. `dbt parse` can stay a subprocess.

### Keep working

- Isolation: every board path resolves through `_board_file(sid, name)`. The new tools must do the same and never accept
  an arbitrary path. Add a smoke-test check that `render_board` refuses `../other/x`.
- Cross-platform rules in `CLAUDE.md` (`runtime.tool()`, `runtime.child_env()`, explicit `encoding="utf-8"`).
- `uv run python scripts/smoke_test.py` must stay green on Windows, macOS and Linux (GitHub Actions runs it).
- After the change, compare real build times: `scripts/timing.py` for the building blocks, and one real turn in the browser
  ("Monthly revenue trend, with revenue by category and a region filter"). On the Mac that turn takes 25 to 45 seconds today.

## Environment notes for Windows

- Use PowerShell. Set a key with `$env:ANTHROPIC_API_KEY = "sk-ant-..."` only if the user has an API key; a Claude Pro/Max/Team
  plan has no key, so log in with Claude Code (`claude`) instead.
- Claude Code on Windows needs Git for Windows (its shell). Check `bash --version`.
- `uv run` re-syncs the environment first, so don't run it while the app is running from the same folder if `pyproject.toml`
  changed.
- Ports: the app is 8800 and the board server 8801. Stop an old copy by port, not by name.
- The first `uv sync` downloads about 800MB (the Claude Agent SDK alone is about 110MB because it bundles Claude Code).
- Don't commit `workspaces/`, `workspace/charts/*`, or anything built from a client's data. They're git-ignored; keep it that way.

## Status when this was written

- `main` is green on Windows, macOS and Linux in CI for the smoke test (34 checks on Windows). The slowness is a *performance*
  problem, not a correctness one; CI can't see it because its machines are fast and it doesn't run a live agent turn.
- The in-process engine described above is **designed and prototyped but not implemented in the repo.** Check `git log` for
  a commit mentioning it before you start, in case the Mac side already landed it.

## Measurements from the Windows machine (fill this in)

```
(paste the output of: uv run python scripts/timing.py)
```
