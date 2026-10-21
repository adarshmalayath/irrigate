#!/usr/bin/env python3
"""Build Irrigate's Flask UI as a static GitHub Pages site."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOCKET_IO_TAG = '  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>'


def build(output: Path, api_base: str) -> None:
    output.mkdir(parents=True, exist_ok=True)

    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    for asset_ref in ('href="static/css/style.css"', 'src="static/js/app.js"'):
        if asset_ref not in html:
            raise ValueError(f"Expected Pages-relative asset reference was not found: {asset_ref}")

    if SOCKET_IO_TAG not in html:
        raise ValueError("Socket.IO script tag was not found in templates/index.html")
    config = f"  <script>window.IRRIGATE_API_BASE = {json.dumps(api_base.rstrip('/'))};</script>\n"
    html = html.replace(SOCKET_IO_TAG, config + SOCKET_IO_TAG, 1)

    static_output = output / "static"
    shutil.copytree(ROOT / "static", static_output, dirs_exist_ok=True)
    (output / "index.html").write_text(html, encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument(
        "--api-base",
        default="https://irrigate-v3dw.onrender.com",
        help="Base URL for the Flask + Socket.IO backend.",
    )
    args = parser.parse_args()
    build(args.output.resolve(), args.api_base)


if __name__ == "__main__":
    main()
