"""阶段2：安装 MSYS2/MinGW-w64 工具链（清华镜像，免管理员，落位 D:\\msys64）。

用法：env -u HTTP_PROXY -u HTTPS_PROXY uv --directory . run python scripts/stage2/setup_toolchain.py
步骤：下载 base 包(msys2-x86_64-latest.tar.xz) -> 解压 D:\\msys64 -> pacman 镜像切清华。
后续 pacman 安装 gcc/cmake/ninja 由 bash 执行（本脚本末尾打印命令）。
"""
from __future__ import annotations

import tarfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# 镜像按序回退（下载侧）
DIST = "https://repo.msys2.org/distrib/"
PKG = "msys2-x86_64-latest.tar.xz"
TARGET_ROOT = Path("D:/msys64")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
# pacman 包镜像：官方主机 -> 交大镜像（清华对 msys2 全路径 403）
TUNA_HOST_MAP = [("https://repo.msys2.org/", "https://mirror.sjtu.edu.cn/msys2/")]


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    if TARGET_ROOT.exists():
        log(f"{TARGET_ROOT} 已存在，跳过下载解压（如需重装请先人工删除）")
        return
    archive = Path("D:/AppData/Local/Temp") / PKG
    expected_min = 40_000_000  # 实际约 42.9MB；低于此视为坏响应
    tries: list[tuple[str, bool]] = [
        ("https://repo.msys2.org/distrib/", True),     # 官方经代理（此前 zenodo 经代理稳定）
        ("https://repo.msys2.org/distrib/", False),    # 官方直连
        ("https://mirror.sjtu.edu.cn/msys2/distrib/", False),  # 交大直连
    ]
    last_err: Exception | None = None
    done = 0
    for mirror, use_proxy in tries:
        session = requests.Session()
        session.trust_env = use_proxy
        for attempt in range(1, 4):
            url = mirror + PKG
            try:
                log(f"下载尝试 {url} proxy={use_proxy} 第{attempt}次")
                r = session.get(url, stream=True, timeout=(15, 90), headers=UA)
                r.raise_for_status()
                done = 0
                with open(archive, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if done % (32 << 20) < (1 << 20):
                            log(f"  {done/1e6:6.1f} MB")
                if done < expected_min:
                    raise IOError(f"响应体过小（{done} 字节），疑似空跳转页")
                log(f"下载完成 {done/1e6:.1f} MB")
                break
            except Exception as exc:  # noqa: BLE001 —— 下载器需捕获一切网络错误
                last_err = exc
                log(f"  失败: {type(exc).__name__}: {str(exc)[:100]}")
        else:
            continue
        break
    else:
        archive.unlink(missing_ok=True)
        raise SystemExit(f"全部镜像失败，最后错误: {last_err}")
    with tarfile.open(archive, "r:xz") as tf:
        tf.extractall("D:/")
    log(f"解压完成 -> {TARGET_ROOT}")
    archive.unlink(missing_ok=True)

    # pacman 镜像切清华：把官方主机替换为清华路径（mingw/$repo 与 msys 路径均正确映射）
    mirror_dir = TARGET_ROOT / "etc" / "pacman.d"
    for mf in mirror_dir.glob("mirrorlist*"):
        text = mf.read_text(encoding="utf-8", errors="replace")
        new = text
        for old_host, new_host in TUNA_HOST_MAP:
            new = new.replace(old_host, new_host)
        if new != text:
            mf.write_text(new, encoding="utf-8")
            log(f"镜像已切清华: {mf.name}")
    log("完成。下一步（bash）：D:/msys64/usr/bin/bash.exe -lc 'pacman -Sy --noconfirm --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-ninja'")


if __name__ == "__main__":
    main()
