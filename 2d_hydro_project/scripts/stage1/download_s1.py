"""阶段1：批量下载选定的 Sentinel-1 原始景（Copernicus Dataspace）。

用法：
  1) .env 填 CDSE_TOKEN 或 CDSE_USERNAME/PASSWORD（模板 env.example.txt）
  2) uv --directory . run python scripts/stage1/download_s1.py
目标：E:/S1_gxhydro/sentinel1/<aoi>/（外置盘，用户 2026-10-24 指定）
规则（终稿 四章）：幂等续传（已下载完成且 >1GB 跳过）、token 自动刷新
（密码 token 约 1h 过期：25min 提前续 + 401 强制刷新）、有限重试、大小校验、日志落盘。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJ = Path(__file__).resolve().parents[2]
# 下载目标默认落在项目内 data/S1_gxhydro/sentinel1（与 verify_s1.py 一致）。
# 可用环境变量 S1_DEST 覆盖到项目外位置。
OUT_BASE = Path(os.environ.get("S1_DEST") or (PROJ / "data" / "S1_gxhydro" / "sentinel1"))
LOG = PROJ / "logs" / "s1_download.log"
RESULT_JSON = PROJ / "data" / "catalog" / "s1_downloads.json"
ENV_PATH = PROJ / ".env"

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DL = "https://download.dataspace.copernicus.eu/odata/v1/Products({pid})/$value"
TOKEN_REFRESH_AFTER_S = 25 * 60  # 密码 token 实际有效期约 60min，25min 主动续

SELECTED = [
    ("ningming", "S1A_IW_GRDH_1SDV_20240910T105833_20240910T105858_055602_06C990_104D.SAFE"),
    ("ningming", "S1A_IW_GRDH_1SDV_20240912T225045_20240912T225114_055638_06CAF6_AE7D.SAFE"),
    ("guigang", "S1A_IW_GRDH_1SDV_20240905T105022_20240905T105047_055529_06C6A3_3DD2.SAFE"),
    ("guigang", "S1A_IW_GRDH_1SDV_20240912T104222_20240912T104247_055631_06CAB0_5344.SAFE"),
    ("jiulong", "S1A_IW_GRDH_1SDV_20240905T105022_20240905T105047_055529_06C6A3_3DD2.SAFE"),
    ("jiulong", "S1A_IW_GRDH_1SDV_20240917T105023_20240917T105048_055704_06CD"),  # 截断名，按前缀匹配
]


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_env() -> dict[str, str]:
    env = {}
    if ENV_PATH.exists():
        for ln in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class TokenManager:
    """密码 token 约 1h 过期：主动 25min 续期；401 时强制刷新。"""

    def __init__(self) -> None:
        self._token: str | None = None
        self._ts = 0.0

    def _fetch(self) -> str:
        env = read_env()
        if env.get("CDSE_TOKEN"):
            return env["CDSE_TOKEN"]
        user, pwd = env.get("CDSE_USERNAME"), env.get("CDSE_PASSWORD")
        if not (user and pwd):
            raise SystemExit("Missing credentials: copy env.example.txt to .env and fill CDSE_TOKEN or username/password")
        r = requests.post(TOKEN_URL, data={
            "client_id": "cdse-public", "grant_type": "password", "username": user, "password": pwd,
        }, timeout=(10, 60))
        r.raise_for_status()
        return r.json()["access_token"]

    def get(self, force: bool = False) -> str:
        now = time.monotonic()
        if force or self._token is None or (now - self._ts) > TOKEN_REFRESH_AFTER_S:
            self._token = self._fetch()
            self._ts = now
            log("Token refreshed")
        return self._token


def load_ids() -> dict[str, str]:
    ids: dict[str, str] = {}
    for jf in ("s1_catalog.json", "s1_catalog_jiulong_wide.json"):
        p = PROJ / "data" / "catalog" / jf
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for aoi, rec in data.get("aois", {}).items():
            for prod in rec.get("products", []):
                ids[prod["name"]] = prod["id"]
    return ids


def main() -> None:
    tm = TokenManager()
    ids = load_ids()
    prev: dict = {}
    if RESULT_JSON.exists():
        try:
            prev = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    prev_ok = set(prev.get("ok", []))

    ok, fail = list(prev.get("ok", [])), []
    for aoi, name in SELECTED:
        dest = OUT_BASE / aoi / f"{name}.zip"
        if dest.exists() and dest.stat().st_size > 1e9:
            log(f"Skip (file present): {aoi}/{name}")
            continue
        if name in prev_ok:
            # 结果文件在案（校验过大小）但当前目标目录无文件：宁明 2 景在掉线的 E 盘上，跳过不重下
            log(f"Skip (recorded earlier, on offline E: drive): {aoi}/{name}")
            continue
        pid = ids.get(name) or next((v for k, v in ids.items() if k.startswith(name[:63])), None)
        if not pid:
            log(f"NOT IN CATALOG, skipped: {aoi}/{name}")
            fail.append(name)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = DL.format(pid=pid)
        no_resume = False  # Dataspace 实测不支持 Range（416），仅保留探测逻辑
        for attempt in range(1, 7):
            try:
                resume_from = 0
                if not no_resume and dest.exists():
                    resume_from = dest.stat().st_size
                    log(f"  Probe resume from {resume_from/1e9:.2f} GB")
                headers = {"Authorization": f"Bearer {tm.get()}"}
                if resume_from:
                    headers["Range"] = f"bytes={resume_from}-"
                log(f"Downloading {aoi}/{name} (attempt {attempt})")
                with requests.get(url, headers=headers, stream=True, timeout=(30, 300)) as r:
                    if r.status_code == 401:
                        raise HTTPError401()
                    if r.status_code == 416 or (resume_from and r.status_code == 200):
                        # 服务器不支持 Range（416）或忽略 Range（200 全量）：回退整景下载
                        no_resume = True
                        resume_from = 0
                        raise HTTPError416()
                    r.raise_for_status()
                    if r.status_code == 206:
                        chunk_total = int(r.headers.get("content-length", 0))
                        total = resume_from + chunk_total
                        mode = "ab"
                    else:
                        total = int(r.headers.get("content-length", 0))
                        mode = "wb"
                    done = resume_from if mode == "ab" else 0
                    with open(dest, mode) as fh:
                        for chunk in r.iter_content(chunk_size=4 << 20):
                            fh.write(chunk)
                            done += len(chunk)
                            if done % (256 << 20) < (4 << 20):
                                log(f"  {done/1e9:.2f} GB")
                if total and done != total:
                    raise IOError(f"incomplete {done}/{total}")
                log(f"Done {done/1e9:.2f} GB -> {dest}")
                ok.append(name)
                break
            except Exception as exc:  # noqa: BLE001
                is401 = isinstance(exc, HTTPError401) or "401" in str(exc)
                is416 = isinstance(exc, HTTPError416)
                log(f"  Failed: {type(exc).__name__}: {str(exc)[:120]}")
                if is416:
                    continue  # 立即整景重试（resume 已回退）
                if is401:
                    tm.get(force=True)
                    time.sleep(2)
                else:
                    time.sleep(10)
        else:
            fail.append(name)
    log(f"Batch finished: ok={len(set(ok))} fail={len(fail)}")
    RESULT_JSON.write_text(
        json.dumps({"ok": sorted(set(ok)), "fail": fail, "utc": datetime.now(timezone.utc).isoformat()},
                   ensure_ascii=False, indent=2), encoding="utf-8")


class HTTPError401(Exception):
    pass


class HTTPError416(Exception):
    pass


if __name__ == "__main__":
    main()
