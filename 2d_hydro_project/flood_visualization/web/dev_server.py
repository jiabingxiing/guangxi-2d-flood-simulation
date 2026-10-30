#!/usr/bin/env python3
"""前端开发用静态服务（仅联调，不是交付脚本）。

映射规则（等价于 scripts/build_web.py 打包后的布局）：
  /                    -> web/index.html
  /assets/<rest>       -> web/<rest>          （styles.css / app.js / map.js / timeline.js / vendor/**）
  /<其它>               -> <bundle>/<其它>     （catalog.json / cases/** / frames/** / numeric/**）

用法：
  .venv/Scripts/python.exe web/dev_server.py --bundle web/.dev/mock_bundle --port 8788
  .venv/Scripts/python.exe web/dev_server.py --bundle outputs/web_bundle/v3-20261028 --port 8788
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MIME_OVERRIDE = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".geojson": "application/json; charset=utf-8",
    ".bin": "application/octet-stream",
    ".png": "image/png",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


class Handler(SimpleHTTPRequestHandler):
    web_dir: Path
    bundle_dir: Path

    def translate_path(self, path: str) -> str:  # noqa: D102
        clean = path.split("?", 1)[0].split("#", 1)[0]
        clean = clean.lstrip("/")
        if clean in ("", "index.html"):
            return str((self.web_dir / "index.html").resolve())
        if clean.startswith("assets/"):
            rest = clean[len("assets/") :]
            p_target = self.web_dir / rest
            if not p_target.exists() and (self.web_dir / "assets" / rest).exists():
                return str((self.web_dir / "assets" / rest).resolve())
            if not p_target.exists() and (self.bundle_dir / clean).exists():
                return str((self.bundle_dir / clean).resolve())
            return str(p_target.resolve())
        return str((self.bundle_dir / clean).resolve())

    def guess_type(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in MIME_OVERRIDE:
            return MIME_OVERRIDE[ext]
        return mimetypes.guess_type(path)[0] or "application/octet-stream"

    def end_headers(self) -> None:  # noqa: D102
        path = self.path.split("?", 1)[0]
        if path.startswith("/assets/") or path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        if os.environ.get("DEV_SERVER_QUIET") == "1":
            return
        sys.stderr.write("[dev] " + (fmt % args) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--bundle", required=True, help="bundle 目录（含 catalog.json）")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    web_dir = here
    bundle_dir = Path(args.bundle).resolve()
    if not (bundle_dir / "catalog.json").exists():
        print(f"[dev] 警告：{bundle_dir} 下没有 catalog.json", file=sys.stderr)
    Handler.web_dir = web_dir
    Handler.bundle_dir = bundle_dir
    httpd = ThreadingHTTPServer((args.host, args.port), partial(Handler))
    print(f"[dev] web={web_dir}")
    print(f"[dev] bundle={bundle_dir}")
    print(f"[dev] http://{args.host}:{args.port}/")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
