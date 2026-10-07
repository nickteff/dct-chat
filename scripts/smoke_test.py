"""End-to-end smoke test of everything except the Claude agent turn. Needs no credentials, and
runs the same on macOS, Linux and Windows (it is what CI runs on all three).

    uv run python scripts/smoke_test.py

It starts the real app twice (demo data, then a linked dbt project), and checks: sessions and
isolation, board rendering, details, thumbnails, exports, unicode, the dbt project link,
connection checks, stale board-server cleanup, the agent's tools and file guards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dct_chat.runtime import child_env, tool  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail == "" else f"   [{str(detail)[:300]}]"), flush=True)
    if not ok:
        FAILS.append(name)


BOARD = """title: Revenue – Smoke Test
source: {source}
queries:
  by_region: |
    SELECT region, SUM(revenue) AS revenue FROM {table} GROUP BY 1 ORDER BY 2 DESC
charts:
  bars: {{type: bar, query: by_region, x: region, y: revenue}}
rows: [bars]
"""


def start_app(port: int, workspace: Path, *extra: str, log: Path) -> subprocess.Popen[bytes]:
    cmd = [sys.executable, "-m", "dct_chat.cli", "--port", str(port), "--preview-port", str(port + 1), "--workspace", str(workspace), *extra]
    # Start it the way a user does: no UTF-8 override. Forcing UTF-8 here would hide any place the
    # app reads or writes a file in the platform's default encoding (cp1252 on Windows).
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log.open("wb"), stderr=subprocess.STDOUT)
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"app exited early (code {proc.returncode}):\n{log.read_text(errors='replace')[-1500:]}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    raise SystemExit("app did not start in time:\n" + log.read_text(errors="replace")[-1500:])


def stop_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop the app and anything it started (the board server)."""
    try:
        parent = psutil.Process(proc.pid)
        victims = [*parent.children(recursive=True), parent]
    except psutil.Error:
        return
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=10)


def board_servers(port: int) -> list[int]:
    """PIDs of the board servers on a port, one per server. On Windows a server is a launcher plus
    a Python child with the same command line, so keep only the top of each such pair."""
    matched = {}
    for p in psutil.process_iter(["cmdline", "ppid"]):
        cmd = p.info["cmdline"] or []
        if "serve" in cmd and "--port" in cmd and cmd[cmd.index("--port") + 1 : cmd.index("--port") + 2] == [str(port)]:
            matched[p.pid] = p.info["ppid"]
    return sorted(pid for pid, parent in matched.items() if parent not in matched)


def demo_workspace(tmp: Path) -> Path:
    ws = tmp / "demo_ws"
    (ws / "data").mkdir(parents=True)
    shutil.copy(ROOT / "workspace" / "dbt_charts.yml", ws / "dbt_charts.yml")
    shutil.copy(ROOT / "workspace" / "data" / "examples.duckdb", ws / "data" / "examples.duckdb")
    return ws


def put_board(ws: Path, sid: str, name: str, source: str, table: str) -> None:
    folder = ws / "charts" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yml").write_text(BOARD.format(source=source, table=table), encoding="utf-8")


def demo_tests(tmp: Path) -> None:
    print("\n== demo data ==")
    ws, port = demo_workspace(tmp), 8850
    log = tmp / "demo.log"
    app = start_app(port, ws, log=log)
    try:
        a = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180)
        b = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180)
        check("page loads and issues a session cookie", a.get("/").status_code == 200 and bool(a.cookies.get("dct_sid")))
        b.get("/")
        sid = a.cookies["dct_sid"]
        check("two browsers get different sessions", sid != b.cookies.get("dct_sid"))
        check("demo mode reported", a.get("/api/project").json()["mode"] == "demo")
        check("connection test passes", a.post("/api/project/check").json()["ok"], a.post("/api/project/check").text)

        put_board(ws, sid, "smoke", "examples_db", "ecommerce_orders")
        boards = a.get("/api/boards").json()
        check("board is listed, with its unicode title intact", [x["title"] for x in boards["boards"]] == ["Revenue – Smoke Test"], boards)
        page = a.get(f"{boards['base']}/smoke/")
        check("board renders through the proxy", page.status_code == 200 and "<svg" in page.text, page.status_code)
        info = a.get("/api/info/smoke")
        check("details report the query and its 4 rows", info.status_code == 200 and info.json()["queries"][0]["rows"] == 4, info.text)
        check("thumbnail is a PNG", a.get("/api/thumb/smoke").content[:8] == b"\x89PNG\r\n\x1a\n")
        for fmt, magic in (("png", b"\x89PNG"), ("pdf", b"%PDF"), ("svg", b"<"), ("html", b"<")):
            r = a.get(f"/api/export/{fmt}/smoke")
            check(f"export {fmt}", r.status_code == 200 and r.content.lstrip()[: len(magic)] == magic, r.status_code)

        check("another session sees none of these boards", b.get("/api/boards").json()["boards"] == [])
        check("another session can't open the board page", b.get(f"/{sid}/smoke/").status_code == 404)
        check("another session can't get its thumbnail", b.get("/api/thumb/smoke").status_code == 404)
        check("another session can't get its details or exports", b.get("/api/info/smoke").status_code in (404, 422) and b.get("/api/export/svg/smoke").status_code in (404, 422))
        check("path traversal is refused", b.get(f"/{b.cookies['dct_sid']}/../{sid}/smoke/").status_code == 404)

        # a crashed app leaves its board server behind; the next start must replace it, not stack up
        stop_one = psutil.Process(app.pid)
        stop_one.kill()
        stop_one.wait(10)
        orphans = board_servers(port + 1)
        check("(setup) board server outlives a crashed app", len(orphans) == 1, orphans)
        app = start_app(port, ws, log=log)
        now = board_servers(port + 1)
        check("restart replaces the stale board server with exactly one new one", len(now) == 1 and now[0] not in orphans, {"before": orphans, "after": now})
        check("boards still render after the restart", httpx.Client(base_url=f"http://127.0.0.1:{port}", cookies={"dct_sid": sid}, timeout=180).get(f"/{sid}/smoke/").status_code == 200)
    finally:
        stop_tree(app)
    check("nothing left running after the app stops", board_servers(port + 1) == [], board_servers(port + 1))


def dbt_tests(tmp: Path) -> None:
    print("\n== linked dbt project ==")
    proj = tmp / "mini_dbt"
    shutil.copytree(ROOT / "tests" / "fixtures" / "mini_dbt", proj)
    for step in ("seed", "run"):
        done = subprocess.run([tool("dbt"), step, "--project-dir", str(proj), "--profiles-dir", str(proj)], cwd=proj, env=child_env(), capture_output=True, encoding="utf-8", errors="replace")
        check(f"(setup) dbt {step}", done.returncode == 0, done.stdout[-400:])
    ws, port = tmp / "dbt_ws", 8852
    app = start_app(port, ws, "--dbt-project", str(proj), log=tmp / "dbt.log")
    try:
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=240)
        c.get("/")
        sid = c.cookies["dct_sid"]
        st = c.get("/api/project").json()
        check("project linked: dbt mode, no error", st["mode"] == "dbt" and not st["error"], st)
        check("manifest read: 1 model on duckdb", st["models"] == 1 and st["adapter"] == "duckdb", st)
        check("connection test passes", c.post("/api/project/check").json()["ok"], c.post("/api/project/check").text)
        put_board(ws, sid, "viaref", "warehouse", "{{ ref('orders_by_region') }}")
        info = c.get("/api/info/viaref")
        check("a board using ref() resolves and returns rows", info.status_code == 200 and info.json()["queries"][0]["rows"] == 4, info.text)
        check("the ref() board renders", c.get(f"/{sid}/viaref/").status_code == 200)
        check("refresh re-reads the project", c.post("/api/project/refresh").json()["models"] == 1)
        check("generated config left the dbt project untouched", not (proj / "dbt_charts.yml").exists())
    finally:
        stop_tree(app)


TOOLS_CHECK = r"""
import asyncio, json
from dct_chat import server as S, tools
from dct_chat.engine import Engine

ws = S.WORKSPACE
a, b = "a" * 16, "b" * 16
for sid in (a, b):
    (ws / "charts" / sid).mkdir(parents=True)
board = (ws / "charts" / a / "mine.yml")
board.write_text(open(ws / "template.yml", encoding="utf-8").read().replace("TITLE", "First title"), encoding="utf-8")

out = {
    "own": str(S._board_rel(a, "mine").as_posix()),
    "own_with_extension": str(S._board_rel(a, "mine.yml").as_posix()),
    "other_session": S._board_rel(b, "mine"),
    "traversal": S._board_rel(b, "../" + a + "/mine"),
    "absolute": S._board_rel(b, str(board.with_suffix(""))),
}

async def main():
    engine = Engine(ws, None)
    await engine.start()
    mine = lambda name: S._board_rel(a, name)
    theirs = lambda name: S._board_rel(b, name)
    r = await tools.render_board(engine, mine, {"board": "mine"})
    out["render"] = [r["is_error"], r["content"][0]["text"]]
    board.write_text(board.read_text(encoding="utf-8").replace("First title", "Second title"), encoding="utf-8")
    out["render_after_edit"] = (await tools.render_board(engine, mine, {"board": "mine"}))["content"][0]["text"]
    out["other_render"] = (await tools.render_board(engine, theirs, {"board": "mine"}))["is_error"]
    out["traversal_render"] = (await tools.render_board(engine, theirs, {"board": "../" + a + "/mine"}))["is_error"]
    q = await tools.run_query(engine, "examples_db", {"sql": "SELECT COUNT(*) AS n FROM ecommerce_orders"})
    out["query"] = [q["is_error"], q["content"][0]["text"]]
    out["bad_query"] = (await tools.run_query(engine, "examples_db", {"sql": "SELECT * FROM no_such_table"}))["is_error"]
    out["docs"] = (await tools.docs(engine, {"search": "bar chart"}))["content"][0]["text"][:200]

asyncio.run(main())
print("RESULT" + json.dumps(out, default=str))
"""


def tool_tests(tmp: Path) -> None:
    print("\n== agent tools (in-process engine) ==")
    import json

    ws = demo_workspace(tmp / "tools")
    (ws / "template.yml").write_text(BOARD.replace("Revenue – Smoke Test", "TITLE").format(source="examples_db", table="ecommerce_orders"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}  # as a user runs it
    env["DCT_CHAT_WORKSPACE"] = str(ws)
    done = subprocess.run([sys.executable, "-c", TOOLS_CHECK], cwd=ROOT, env=env, capture_output=True, encoding="utf-8", errors="replace")
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), None)
    check("(setup) tools script ran", line is not None, done.stderr[-600:] or done.stdout[-600:])
    if line is None:
        return
    out = json.loads(line[len("RESULT"):])
    own = out["own"]
    check("a session resolves its own board, with or without .yml", own.startswith("charts/aaaaaaaaaaaaaaaa/mine") and out["own_with_extension"] == own, own)
    check("another session's name, a ../ hop and an absolute path all resolve to nothing", out["other_session"] is None and out["traversal"] is None and out["absolute"] is None, out)
    check("render_board tool: ok, with what each chart shows", out["render"][0] is False and "status: ok" in out["render"][1] and "4 rows" in out["render"][1], out["render"])
    check("render_board sees an edit made after the first render", "Second title" in out["render_after_edit"], out["render_after_edit"][:200])
    check("render_board refuses another session's board and a traversal", out["other_render"] is True and out["traversal_render"] is True)
    import duckdb

    with duckdb.connect(str(ws / "data" / "examples.duckdb"), read_only=True) as con:
        expected = con.execute("SELECT COUNT(*) FROM ecommerce_orders").fetchone()[0]
    check("run_query tool returns the right row count", out["query"][0] is False and f'"n": {expected}' in out["query"][1], out["query"])
    check("run_query reports a bad query as an error", out["bad_query"] is True)
    check("docs tool returns chart documentation", "chart" in out["docs"].lower() and len(out["docs"]) > 100, out["docs"])


def guard_tests() -> None:
    print("\n== agent guards ==")
    from dct_chat.server import _outside_folder as outside

    folder = Path(tempfile.mkdtemp()) / "mine"
    folder.mkdir()
    check("file tools: own files allowed", not any(outside(folder, p) for p in ("a.yml", "sub/a.yml", "*.yml", "**/*.yml", str(folder / "a.yml"))))
    check("file tools: other places refused", all(outside(folder, p) for p in ("../x.yml", "../*/*.yml", "sub/../../x.yml", str(folder.parent / "other" / "x.yml"))))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(f"platform: {sys.platform}, python {sys.version.split()[0]}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        tmp = Path(t)
        guard_tests()
        tool_tests(tmp)
        demo_tests(tmp)
        dbt_tests(tmp)
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
