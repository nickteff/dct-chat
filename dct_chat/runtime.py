"""Small cross-platform helpers: finding the tools installed next to this Python, and
building a child-process environment that behaves the same on macOS, Linux and Windows."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
VENV_BIN = Path(sys.executable).parent  # `bin/` on macOS and Linux, `Scripts\` on Windows


def tool(name: str) -> str:
    """Path to a console script installed alongside this Python (`dct`, `dbt`).

    `shutil.which` applies PATHEXT, so this finds `dct.exe` on Windows.
    """
    found = shutil.which(name, path=str(VENV_BIN))
    return found or str(VENV_BIN / (f"{name}.exe" if IS_WINDOWS else name))


def child_env() -> dict[str, str]:
    """Environment for subprocesses (dct, dbt, the agent).

    - Our own tools come first on PATH. Windows spells the variable `Path`, so find the
      existing key instead of adding a second one that differs only in case.
    - Python in the children reads and writes UTF-8. Windows consoles default to a legacy
      code page, which breaks on characters like ✓ or an en dash in a board title.
    """
    env = dict(os.environ)
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[key] = f"{VENV_BIN}{os.pathsep}{env.get(key, '')}"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
