"""How long do the building blocks take on THIS machine?

    uv run python scripts/timing.py

A board build is mostly a handful of `dct` commands plus Claude's own thinking. This times each
kind of command separately, so a slow machine's bottleneck shows up in numbers (a virus scanner
slowing every process start looks very different from a slow network).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dct_chat.runtime import child_env, tool  # noqa: E402

BOARD = """title: Timing
source: examples_db
queries:
  q: SELECT region, SUM(revenue) AS revenue FROM ecommerce_orders GROUP BY 1
charts:
  c: {type: bar, query: q, x: region, y: revenue}
rows: [c]
"""


def timed(label: str, cmd: list[str], cwd: Path) -> float:
    start = time.perf_counter()
    done = subprocess.run(cmd, cwd=cwd, env=child_env(), capture_output=True, encoding="utf-8", errors="replace")
    took = time.perf_counter() - start
    print(f"{took:6.1f}s  {label}" + ("" if done.returncode == 0 else f"   (exit code {done.returncode})"), flush=True)
    return took


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(f"platform: {sys.platform}, python {sys.version.split()[0]}\n")
    ws = Path(tempfile.mkdtemp()) / "ws"
    (ws / "data").mkdir(parents=True)
    shutil.copy(ROOT / "workspace" / "dbt_charts.yml", ws / "dbt_charts.yml")
    shutil.copy(ROOT / "workspace" / "data" / "examples.duckdb", ws / "data" / "examples.duckdb")
    (ws / "board.yml").write_text(BOARD, encoding="utf-8")
    dct = tool("dct")

    timed("start Python and exit (baseline for any process start)", [sys.executable, "-c", "pass"], ws)
    timed("dct --version", [dct, "--version"], ws)
    timed("dct docs cheatsheet", [dct, "docs", "cheatsheet"], ws)
    timed("dct skills board-build (the agent reads this today)", [dct, "skills", "board-build"], ws)
    q = timed("dct query (one small SQL query)", [dct, "query", "examples_db", "SELECT 1 AS n"], ws)
    timed("dct validate board.yml", [dct, "validate", "board.yml"], ws)
    r = timed("dct render board.yml --format text  (first time)", [dct, "render", "board.yml", "--format", "text"], ws)
    timed("dct render board.yml --format text  (again)", [dct, "render", "board.yml", "--format", "text"], ws)

    claude = next((ROOT / ".venv").glob("**/claude_agent_sdk/_bundled/claude*"), None)
    if claude and claude.is_file():
        timed("Claude Code engine: start and print its version", [str(claude), "--version"], ws)
    bash = shutil.which("bash")
    print(f"\nbash on PATH: {bash or 'NOT FOUND'}")
    if bash:
        timed("bash -c 'echo hi' (how the agent's commands are launched on Windows)", [bash, "-c", "echo hi"], ws)

    print(f"\nA typical build runs about 8 dct commands, so roughly {8 * ((q + r) / 2):.0f}s of command time on this machine,")
    print("before Claude's own thinking time.")
