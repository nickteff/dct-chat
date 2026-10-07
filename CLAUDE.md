# dct-chat: notes for working on this code

> **Working on speed on a Windows machine? Read `HANDOFF.md` first.** It has the measurements, what has been done (an in-process engine instead of a `dct` process per command), and what is left to measure and do.

A chat UI where Claude builds [dbt charts](https://github.com/dbt-labs/dbt-charts) dashboards. A browser talks to a FastAPI app; each browser has its own Claude Agent SDK session; boards are YAML files rendered by the dbt charts engine and shown inline in the chat.

## Run it

```bash
uv sync
uv run dct-chat                                  # demo data, http://localhost:8800
uv run dct-chat --dbt-project /path/to/dbt       # a real dbt project
```

Two processes run: this app on 8800, and `dct serve` (the board server) on 8801, started and watched by the app. To test a change without touching a running copy, start another on other ports (`--port 8820 --preview-port 8821`, and `--workspace DIR` for separate boards).

`uv run python scripts/smoke_test.py` runs everything except a live Claude turn (sessions and isolation, boards, details, exports, a linked dbt project, stale-server cleanup, the agent's path guards) and needs no credentials. GitHub Actions runs it on Linux, macOS and Windows. A live agent turn has only been checked by hand, by driving the UI with Playwright.

## Layout

- `dct_chat/server.py`: sessions, the agent, endpoints, the board proxy. Sections are marked by banner comments.
- `dct_chat/engine.py`: the dbt charts engine, imported once and held open (`ProjectSession`). All calls are serialized with a lock and run on a worker thread. Rendering takes ~0.1s warm; a fresh `dct` process takes 3 to 6s just to start.
- `dct_chat/tools.py`: the agent's tools (`render_board`, `run_query`, `docs`), served in-process by the SDK. Each session gets its own set, bound to a lookup that only resolves boards inside that session's folder.
- `dct_chat/project.py`: reads a dbt project, runs `dbt parse`, summarizes the manifest, reads columns from the warehouse.
- `dct_chat/cli.py`: the `dct-chat` command. It sets environment variables *before* importing the server, which reads them at import.
- `dct_chat/static/index.html`: the whole UI (HTML, CSS, JS), no build step.
- `workspace/`: the demo project (`dbt_charts.yml` plus `data/examples.duckdb`, the playground's sample data from dbt charts, Apache-2.0; see `THIRD_PARTY_NOTICES.md`). Boards are written to `workspace/charts/<session>/`, which is git-ignored. A linked dbt project gets `workspaces/<project>/` instead.

## How a turn works

1. The page POSTs `/api/chat`; the server streams Server-Sent Events: `delta` (text tokens), `text` (a full message), `phase` (`learn`/`explore`/`write`/`check`, drives the progress card, never shows commands), `suggest` (follow-up chips), `board` (a board changed), `error`, `done`.
2. The agent runs with the session's folder as its working directory and writes `<name>.yml` there with the Write tool, then calls `render_board` (validate, run, report warnings) and fixes what it reports. It has no shell: its built-in tools are limited to Read/Write/Edit/Glob/Grep.
3. After the turn the server compares board file mtimes before and after, and emits `board` for each change. This works however the file was written.
4. Text the agent writes *before* a tool call is narration; the UI drops it and keeps the reply after the last call.

## Things that must stay true

- **Session isolation.** A browser is identified by a random `dct_sid` cookie, validated against `[a-f0-9]{16}` because it becomes a folder name. Every endpoint resolves boards through `_board_file(sid, name)`, which requires the file to be inside that session's folder. The board proxy only forwards `/<own sid>/...`, `/static/...` and `/__livereload`.
- **The agent can't leave its folder.** A `PreToolUse` hook (`_guard_paths`) confines the file tools, and the engine tools only resolve board names through `_board_rel`, which stays inside the session's folder. An allow rule on a tool does *not* confine it; a hook's deny does. A canary test found this the hard way: with a bare `Read` allowed, one session read another's board.
- **Boards are served from the app's own origin** (the proxy), so the page can read an iframe's height and size the card to fit. Board pages use root-relative URLs, so there is no URL prefix.
- **Embedded boards ask the page to apply filters.** Inside an iframe a board posts `dbt-variable-change` / `dbt-board-navigate` and waits; the page reloads the iframe with the new query string. Without that handler filters just spin.
- **Never write into a linked dbt project.** It's linked by `dbt_project_dir:` in a generated `dbt_charts.yml`. The only thing written there is dbt's own `target/` and `logs/` from `dbt parse`.

## Gotchas

- Stopping the app: kill by port (`kill $(lsof -ti tcp:8800 -sTCP:LISTEN)` on macOS/Linux), not `pkill -f dct-chat`, which also hits other copies. After a hard kill the old board server can linger; the app clears a stale one on its port at startup.
- Cross-platform rules: find tools with `runtime.tool()` (`.exe` on Windows; only the board server and `dbt parse` still run as subprocesses), build child environments with `runtime.child_env()`, and always pass `encoding="utf-8"` to file reads and writes (Windows defaults to cp1252). The smoke test deliberately starts the app *without* a UTF-8 override so these slips get caught.
- `uv run` re-syncs the environment first. If `pyproject.toml` changed it can swap installed packages under a running app.
- macOS `sed -i` needs an argument (`sed -i ''`); GNU `sed` doesn't.
- `dct doctor` reports the adapter as unconfigured when a project is linked with `dbt_project_dir:`, even though queries work. The connection test runs a real `SELECT 1` through the engine instead. In-process, the dbt link is `FilesystemProject(workspace, dbt_root=...)`: `ProjectSession.open()` ignores the `dbt_project_dir:` config key.
- Thumbnails are cached by the board file's mtime (`workspace/.thumbs/<session>/`). The thumbnail URL carries the mtime so a cached copy is never stale.

## Ideas not built yet

Paste a screenshot to rebuild a dashboard (dct ships a `board-replicate` skill), a theme picker, undo and version history for boards, "ask about this data point", per-user warehouse credentials, delete/rename boards, a test for a live agent turn.
