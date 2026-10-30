"""阶段2：安装 w64devkit（自包含 MinGW-w64 GCC + GNU make，GitHub Releases 经代理）。

用法：uv --directory . run python scripts/stage2/setup_w64devkit.py
落位 D:\\w64devkit；幂等（已存在则跳过）。校验：g++ --version。
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path("D:/w64devkit")
ASSET = "w64devkit-x64-2.10.1.7z.exe"
URL = f"https://github.com/skeeto/w64devkit/releases/download/v2.10.1/{ASSET}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    if ROOT.exists():
        log(f"{ROOT} 已存在，跳过")
        return
    archive = Path("D:/AppData/Local/Temp") / ASSET
    if not archive.exists() or archive.stat().st_size < 60_000_000:
        session = requests.Session()
        session.trust_env = True  # GitHub 需走代理
        for attempt in range(1, 4):
            try:
                log(f"下载 {URL} 第{attempt}次")
                r = session.get(URL, stream=True, timeout=(15, 120), headers=UA)
                r.raise_for_status()
                done = 0
                with open(archive, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if done % (32 << 20) < (1 << 20):
                            log(f"  {done/1e6:6.1f} MB")
                if done < 60_000_000:
                    raise IOError(f"下载不完整: {done} 字节")
                log(f"下载完成 {done/1e6:.1f} MB")
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                log(f"  失败: {type(exc).__name__}: {str(exc)[:100]}")
        else:
            raise SystemExit(f"下载失败: {last}")
    # 7z SFX 控制台自解压：-y 静默，-o 指定输出目录（解出 w64devkit/ 子目录）
    log("自解压到 D:/ ...")
    subprocess.run([str(archive), "x", "-y", "-oD:/"], check=True, timeout=600)
    gxx = ROOT / "bin" / "g++.exe"
    ver = subprocess.run([str(gxx), "--version"], capture_output=True, text=True, timeout=60).stdout.splitlines()[0]
    log(f"校验: {ver}")
    print(f"W64DEVKIT_ROOT={ROOT}")


if __name__ == "__main__":
    main()
