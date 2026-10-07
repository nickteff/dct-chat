"""End-to-end smoke test of everything except the Claude agent turn. Needs no credentials, and
runs the same on macOS, Linux and Windows (it is what CI runs on all three).

    uv run python scripts/smoke_test.py

It starts the real app twice (demo data, then a linked dbt project), and checks: sessions and
isolation, board rendering, details, thumbnails, exports, unicode, the dbt project link,
connection checks, stale board-server cleanup, and the agent's path guards.
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


def board_servers(port: int) -> list[psutil.Process]:
    found = []
    for p in psutil.process_iter(["cmdline"]):
        cmd = p.info["cmdline"] or []
        if "serve" in cmd and "--port" in cmd and cmd[cmd.index("--port") + 1 : cmd.index("--port") + 2] == [str(port)]:
            found.append(p)
    return found


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
        check("(setup) board server outlives a crashed app", len(board_servers(port + 1)) == 1)
        app = start_app(port, ws, log=log)
        check("restart replaces the stale board server", len(board_servers(port + 1)) == 1, [p.pid for p in board_servers(port + 1)])
        check("boards still render after the restart", httpx.Client(base_url=f"http://127.0.0.1:{port}", cookies={"dct_sid": sid}, timeout=180).get(f"/{sid}/smoke/").status_code == 200)
    finally:
        stop_tree(app)


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


def guard_tests() -> None:
    print("\n== agent guards ==")
    from dct_chat.server import _ESCAPES_FOLDER as shell, _outside_folder as outside

    folder = Path(tempfile.mkdtemp()) / "mine"
    folder.mkdir()
    check("file tools: own files allowed", not any(outside(folder, p) for p in ("a.yml", "sub/a.yml", "*.yml", "**/*.yml", str(folder / "a.yml"))))
    check("file tools: other places refused", all(outside(folder, p) for p in ("../x.yml", "../*/*.yml", "sub/../../x.yml", str(folder.parent / "other" / "x.yml"))))
    block = ["dct render ../x.yml", "dct render ..\\x\\y.yml", "dct render C:\\Users\\a\\x.yml", "dct render C:/Users/a/x.yml", "dct render \\\\srv\\share\\x.yml", "dct render ~/x.yml", "dct render /etc/x", "dct render $(cat x)"]
    allow = ["dct validate a.yml && dct render a.yml --format text", 'dct query s "SELECT a / b FROM t"', "dct render a.yml 2>/dev/null", 'dct query s "SELECT \'https://x.com/a\'"']
    check("shell guard: escapes refused", all(shell.search(c) for c in block), [c for c in block if not shell.search(c)])
    check("shell guard: normal commands allowed", not any(shell.search(c) for c in allow), [c for c in allow if shell.search(c)])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(f"platform: {sys.platform}, python {sys.version.split()[0]}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        tmp = Path(t)
        guard_tests()
        demo_tests(tmp)
        dbt_tests(tmp)
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
