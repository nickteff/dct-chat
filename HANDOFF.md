# Handoff: speed on a slow Windows machine

You're an agent picking this up on the Windows machine where the problem shows. Read this, then `CLAUDE.md`
(architecture and the rules that must stay true). The work so far was done on a Mac; Windows is where it needs proving.

## The problem

On the user's Windows PC, before the fix:

- The board server took **36 seconds** to become ready (`[dct-chat] Board server ready (36s)`); on the Mac, about 4.
- Building one board took **minutes**; on the Mac, 25 to 45 seconds.

## Why it was slow (measured on the Mac)

Every `dct` command is a new Python process that imports dbt, DuckDB and the chart engine, so each pays a large fixed
start-up cost (`uv run python scripts/timing.py`):

| command | time |
|---|---|
| `dct --version` | 0.6s |
| `dct query ... "SELECT 1"` | 3.1s |
| `dct validate board.yml` | 4.4s |
| `dct render board.yml --format text` | 5.6s (again: 5.6s) |

A build ran about 8 of them (read the skills, explore the data, validate, render, fix, render again). If a process start is
about 9x slower on the Windows PC (the board server's 36s vs 4s suggests it), that is several minutes. Probable causes of
the slowdown: antivirus scanning of every process and file, slow process creation. **Not yet measured on that machine.**

## What was done (landed on `main`)

The app now loads the engine **once** (`dct_chat/engine.py`, a long-lived `ProjectSession`) and the agent calls it directly
through in-process tools (`dct_chat/tools.py`: `render_board`, `run_query`, `docs`). The agent has no shell any more.
Details, thumbnails, exports and the connection test use the same engine. Measured on the Mac with that engine loaded:
render 0.1s (1.2s the first time), query 0.02s. Start-up is also non-blocking: the page answers within seconds and the
terminal narrates the slow parts.

Real agent turns on the Mac after the change: the benchmark prompt ("Monthly revenue trend, with revenue by category and a
region filter") **23s** (was 25 to 45s); a second request 12s; an edit 4.5s (was about 20s); a linked dbt project turn 22s.
Most of what remains is the model thinking and writing, which no change here affects.

## Still launched as processes

1. **The board server (`dct serve`)**, started once at app start. On this PC that is likely the **single largest fixed
   cost left** (the 36s). It's paid once per app start, not per build. A possible next step: host its ASGI app inside this
   process (look at `dbt_charts.core.serve`) so it shares the already-loaded engine. Risks: its live-reload stream and
   route set; check before committing to it. Alternatively, accept it: it overlaps with everything else at start-up.
2. **`dbt parse`**, only when a dbt project is linked and its manifest is stale. It's dbt itself; leave it.

## Do this first on Windows (measure before changing anything)

PowerShell, in the repo:

```powershell
git pull
uv sync
uv run python scripts/timing.py          # how long each building block takes on THIS machine
uv run python scripts/smoke_test.py      # should end with ALL PASSED
uv run dct-chat                          # watch the terminal timings
```

Interpret `timing.py`:

- `start Python and exit` already slow (over about 1s): process creation or antivirus is the bottleneck. Per-command costs
  no longer matter much (the agent doesn't launch commands), but the board server start still does. Ask whether the repo
  folder can be excluded from antivirus scanning.
- `dct --version` slow but Python fast: importing the engine is slow. The app pays it once at start-up (look for
  `Chart engine ready (Ns)` in the terminal).
- `bash on PATH: NOT FOUND`: Claude Code's own shell tool won't run; install Git for Windows. (The agent's work doesn't need
  a shell, but Claude Code itself expects it on Windows.)

Then do **one real build in the browser**: "Monthly revenue trend, with revenue by category and a region filter".
Write down the time. On the Mac it's about 23s. If it's still minutes on Windows, find out which part (the engine
loading, the model, the board server, Claude Code starting) before changing code; the terminal narration and
`timing.py` are there to help.

## Keep working

- Isolation: every board path resolves through `_board_file(sid, name)`; the tools use `_board_rel`, which stays inside
  the session's folder. Never accept an arbitrary path. `scripts/smoke_test.py` checks this (including `../` and
  absolute paths).
- Cross-platform rules in `CLAUDE.md` (`runtime.tool()`, `runtime.child_env()`, explicit `encoding="utf-8"`).
- The smoke test must stay green on Windows, macOS and Linux (GitHub Actions runs it).
- The engine is synchronous and shared: calls are serialized with a lock on a worker thread. Don't remove the lock without
  proving the adapters are thread-safe.

## Environment notes for Windows

- Use PowerShell. A Claude Pro/Max/Team plan has no API key: log in with Claude Code (`claude`). Set a key with
  `$env:ANTHROPIC_API_KEY = "sk-ant-..."` only if the user has one from the Anthropic Console.
- `uv run` re-syncs the environment first, so don't run it from the same folder while the app is running if
  `pyproject.toml` changed.
- Ports: the app is 8800 and the board server 8801. Stop an old copy by port, not by name.
- The first `uv sync` downloads about 800MB (the Claude Agent SDK alone is about 110MB because it bundles Claude Code).
- Don't commit `workspaces/`, `workspace/charts/*`, or anything built from a client's data. They're git-ignored; keep it that way.

## Measurements from the Windows machine (fill this in)

```
(paste the output of: uv run python scripts/timing.py)

(then: the terminal's start-up lines, and the time of one real build)
```
