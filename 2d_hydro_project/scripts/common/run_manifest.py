"""每 run 可追溯清单：配置快照、代码哈希、模型版本、输入哈希、资源与质量门。

用法（在案例脚本内）：
    from scripts.common.run_manifest import prepare_run_dir, write_run_manifest
    prepare_run_dir(RUN_DIR)              # 已有 res.mass 时拒绝覆盖
    write_run_manifest(RUN_DIR, ...)

注意：本模块只写 manifest，不改变模型数值。prepare_run_dir 的 no-overwrite
是终稿 §10 的硬约束；需要重跑时请使用新的 run tag/目录。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
REQUIRED_KEYS = (
    "run_id", "generated_utc", "config_snapshot", "code_hashes", "model_version",
    "input_hashes", "threads", "peak_rss_mb", "disk_output_mb", "mass_gate", "warnings",
)


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while blk := f.read(chunk):
            h.update(blk)
    return h.hexdigest()


def code_targets() -> list[Path]:
    files = []
    for base in ("src", "scripts", "tests", "configs"):
        p = PROJ / base
        if not p.exists():
            continue
        files += [f for f in p.rglob("*") if f.is_file()
                  and f.suffix in (".py", ".yaml", ".yml", ".toml")]
    files += [PROJ / "pyproject.toml", PROJ / "uv.lock"]
    return sorted({f for f in files if f.exists()})


def code_tree_hash() -> tuple[str, dict[str, str]]:
    per = {}
    for p in code_targets():
        per[p.relative_to(PROJ).as_posix()] = sha256(p)
    tree = hashlib.sha256("".join(f"{k}:{v}\n" for k, v in sorted(per.items())).encode()).hexdigest()
    return tree, per


def prepare_run_dir(path: Path) -> None:
    """若目录已有 res.mass，拒绝覆盖（终稿 §10 no_overwrite）。"""
    mass = path / "results" / "res.mass"
    if mass.exists():
        raise FileExistsError(
            f"{path} 已有 res.mass；终稿 §10 禁止覆盖。请使用新的 run tag/目录。"
        )
    path.mkdir(parents=True, exist_ok=True)


def write_run_manifest(run_dir: Path, *, config_snapshot: dict, model_version: str,
                       peak_rss_mb: float | None = None, disk_output_mb: float | None = None,
                       mass_gate: dict | None = None, warnings: list[str] | None = None,
                       started_utc: str | None = None) -> dict:
    tree, per_file = code_tree_hash()
    inputs = {}
    input_manifest = PROJ / "data" / "catalog" / "input_manifest.json"
    if input_manifest.exists():
        try:
            inputs = json.loads(input_manifest.read_text(encoding="utf-8")).get("files", {})
        except Exception:
            inputs = {}
    disk = disk_output_mb
    if disk is None:
        disk = round(sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file()) / 1e6, 1)
    rec = {
        "run_id": run_dir.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "started_utc": started_utc,
        "config_snapshot": config_snapshot,
        "code_hashes": {"tree_sha256": tree, "files": per_file},
        "model_version": model_version,
        "input_hashes": inputs,
        "threads": {"OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"), "cpu_count": os.cpu_count()},
        "peak_rss_mb": peak_rss_mb,
        "disk_output_mb": disk,
        "mass_gate": mass_gate or {},
        "warnings": warnings or [],
    }
    missing = [k for k in REQUIRED_KEYS if k not in rec]
    if missing:
        raise ValueError(f"manifest 缺字段: {missing}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def write_run_manifest_best_effort(
    run_dir: Path, *, config_snapshot: dict | None = None,
    model_version: str = "LISFLOOD-FP 8.1.0", peak_rss_mb: float | None = None,
    mass_gate: dict | None = None, warnings: list[str] | None = None,
    started_utc: str | None = None,
) -> dict | None:
    """写运行清单；清单失败时返回 None，不改变模型运行状态。"""
    try:
        existing_path = run_dir / "run_manifest.json"
        existing = {}
        if existing_path.is_file():
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        if config_snapshot is None:
            meta = run_dir / "meta.json"
            config_snapshot = {}
            if meta.is_file():
                config_snapshot = json.loads(meta.read_text(encoding="utf-8"))
        if peak_rss_mb is None:
            peak_rss_mb = existing.get("peak_rss_mb")
        if started_utc is None:
            started_utc = existing.get("started_utc")
        return write_run_manifest(
            run_dir,
            config_snapshot=config_snapshot,
            model_version=model_version,
            peak_rss_mb=peak_rss_mb,
            mass_gate=mass_gate,
            warnings=warnings,
            started_utc=started_utc,
        )
    except Exception as exc:  # 清单是追溯增强，不覆盖模型结果状态
        print(f"[run_manifest] 写入失败：{run_dir}；{type(exc).__name__}: {exc}", flush=True)
        return None
