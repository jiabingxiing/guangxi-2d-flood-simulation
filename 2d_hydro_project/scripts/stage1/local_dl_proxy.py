"""阶段1：本地下载代理 —— 给 IDM/浏览器提供免认证的本地下载链接。

背景：Dataspace 下载端点要求 Authorization: Bearer（IDM 账号密码无效），
网页下载报 DAT-ZIP-604 Token not found。本代理用 .env 凭证换取 token 并流式转发。

用法：uv --directory . run python scripts/stage1/local_dl_proxy.py   （后台保持运行）
本地链接：
  http://127.0.0.1:8899/dl/1  -> S1A...092405...06C6A3_3DD2.SAFE (1.70GB, 贵港+久隆 09-05)
  http://127.0.0.1:8899/dl/2  -> S1A...0912...06CAB0_5344.SAFE   (1.73GB, 贵港 09-12)
  http://127.0.0.1:8899/dl/3  -> S1A...0917...06CD90_3527.SAFE   (1.70GB, 久隆 09-17)
IDM/浏览器均可直接下载；文件名经 Content-Disposition 自动为 *.SAFE.zip。
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

PROJ = Path(__file__).resolve().parents[2]
ENV_PATH = PROJ / ".env"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

PRODUCTS = {
    "1": ("2e40846f-1476-4fc6-b6ac-db3fccab161d",
          "S1A_IW_GRDH_1SDV_20240905T105022_20240905T105047_055529_06C6A3_3DD2.SAFE.zip"),
    "2": ("9bc70245-d91b-4b0d-819e-83e6bc485aeb",
          "S1A_IW_GRDH_1SDV_20240912T104222_20240912T104247_055631_06CAB0_5344.SAFE.zip"),
    "3": ("5e5d68ef-a0a2-4072-846b-374287e321bf",
          "S1A_IW_GRDH_1SDV_20240917T105023_20240917T105048_055704_06CD90_3527.SAFE.zip"),
}

_token_cache: dict = {"t": None, "ts": 0.0}

# 上游一律直连（不依赖本机 Clash 7897——它可能已关闭；Copernicus 直连可达）
SESSION = requests.Session()
SESSION.trust_env = False


def get_token(force: bool = False) -> str:
    now = time.monotonic()
    if not force and _token_cache["t"] and now - _token_cache["ts"] < 25 * 60:
        return _token_cache["t"]
    env = {}
    for ln in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip()
    if env.get("CDSE_TOKEN"):
        tok = env["CDSE_TOKEN"]
    else:
        r = SESSION.post(TOKEN_URL, data={
            "client_id": "cdse-public", "grant_type": "password",
            "username": env["CDSE_USERNAME"], "password": env["CDSE_PASSWORD"],
        }, timeout=(10, 60))
        r.raise_for_status()
        tok = r.json()["access_token"]
    _token_cache.update(t=tok, ts=now)
    print(f"[{time.strftime('%H:%M:%S')}] token refreshed", flush=True)
    return tok


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 —— http.server 约定
        key = self.path.strip("/").split("/")[-1]
        if key not in PRODUCTS:
            self.send_error(404, "use /dl/1 /dl/2 /dl/3")
            return
        pid, fname = PRODUCTS[key]
        url = f"https://download.dataspace.copernicus.eu/odata/v1/Products({pid})/$value"
        try:
            upstream = SESSION.get(url, headers={"Authorization": f"Bearer {get_token()}"},
                                   stream=True, timeout=(30, 120))
            if upstream.status_code in (401, 403):  # token 失效：强刷重试一次
                upstream = SESSION.get(url, headers={"Authorization": f"Bearer {get_token(force=True)}"},
                                       stream=True, timeout=(30, 120))
            upstream.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            msg = f"upstream error: {type(exc).__name__}: {str(exc)[:150]}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        total = upstream.headers.get("Content-Length")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        if total:
            self.send_header("Content-Length", total)
        self.end_headers()
        sent = 0
        try:
            for chunk in upstream.iter_content(chunk_size=1 << 20):
                self.wfile.write(chunk)
                sent += len(chunk)
                if sent % (256 << 20) < (1 << 20):
                    print(f"[{time.strftime('%H:%M:%S')}] /dl/{key} {sent/1e9:.2f} GB", flush=True)
            print(f"[{time.strftime('%H:%M:%S')}] /dl/{key} DONE {sent/1e9:.2f} GB", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print(f"[{time.strftime('%H:%M:%S')}] /dl/{key} client aborted at {sent/1e9:.2f} GB", flush=True)

    def log_message(self, fmt, *args) -> None:  # 静默默认访问日志
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 8899), Handler)
    print("local download proxy on http://127.0.0.1:8899/dl/{1,2,3}", flush=True)
    srv.serve_forever()
