"""`dct-chat`: start the chat app, optionally linked to a dbt project."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dct-chat",
        description="Build dbt charts dashboards by chatting. Runs on the bundled demo data "
        "unless you link a dbt project.",
    )
    parser.add_argument("--dbt-project", metavar="DIR", help="path to a dbt project (the folder with dbt_project.yml)")
    parser.add_argument("--target", metavar="NAME", help="dbt target to use (default: the profile's default)")
    parser.add_argument("--profiles-dir", metavar="DIR", help="folder holding profiles.yml (default: the dbt project, then ~/.dbt)")
    parser.add_argument("--workspace", metavar="DIR", help="where boards are kept (default: a folder per project here)")
    parser.add_argument("--port", type=int, help="port for the chat app (default 8800)")
    parser.add_argument("--preview-port", type=int, help="port for the board server behind it (default 8801)")
    args = parser.parse_args()

    # The server reads its settings from the environment when it is imported, so set them first.
    settings = {
        "DCT_CHAT_DBT_PROJECT": args.dbt_project and str(Path(args.dbt_project).expanduser().resolve()),
        "DCT_CHAT_DBT_TARGET": args.target,
        "DCT_CHAT_PROFILES_DIR": args.profiles_dir and str(Path(args.profiles_dir).expanduser().resolve()),
        "DCT_CHAT_WORKSPACE": args.workspace and str(Path(args.workspace).expanduser().resolve()),
        "DCT_CHAT_PORT": args.port and str(args.port),
        "DCT_CHAT_PREVIEW_PORT": args.preview_port and str(args.preview_port),
    }
    os.environ.update({k: v for k, v in settings.items() if v})

    from dct_chat.server import main as run

    run()


if __name__ == "__main__":
    main()
