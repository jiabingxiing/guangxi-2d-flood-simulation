"""阶段0/13.1：冻结输入哈希清单（test_run_manifest 的数据源）。

对关键输入与模型可执行文件计算 SHA-256，写入 data/catalog/input_manifest.json。
后续任何输入变更后重跑本脚本并核对差异（重跑范围判定依据，终稿 十六）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.paths import (  # noqa: E402
    EXTERNAL_DEFAULTS,
    PathResolutionError,
    external,
    rel_posix,
)

# 真外部资源用逻辑名标记，由 paths 解析到当前位置；不得写死盘符。
EXTERNAL_KEYS = {
    "dem_source_guangxi": "DEM_SOURCE",
}

TARGETS = {
    "dem_ningming_interim": "data/interim/ningming/dem_filled_12p5m_32648.tif",
    "dem_ningming_raw_clip": "data/interim/ningming/dem_12p5m_32648.tif",
    "mannings_ningming": "data/interim/ningming/mannings_12p5m_32648.tif",
    "water_0806_ningming": "data/interim/ningming/water_0806.tif",
    "water_new_0824_ningming": "data/interim/ningming/water_new_0824.tif",
    "era5_ningming_aug2026": "data/catalog/probes/ningming_aug2026_era5.json",
    "era5_jiulong_sep2024": "data/catalog/probes/jiulong_sep2024_era5_hourly.json",
    "era5_jiulong_sep2024_ext": "data/catalog/probes/jiulong_sep2024_era5_hourly_ext.json",
    "era5_guigang_sep2024": "data/catalog/probes/guigang_sep2024_era5_hourly.json",
    "lisflood_exe": "models/lisflood-fp-8.1/LISFLOOD-FP-trunk/lisflood.exe",
    "dem_source_jiulong_clip": "data/interim/jiulong/dem_12p5m_32649.tif",
    "dem_guigang_clip": "data/interim/guigang/dem_12p5m_32649.tif",
    "mannings_jiulong": "data/interim/jiulong/mannings_12p5m_32649.tif",
    "mannings_guigang": "data/interim/guigang/mannings_12p5m_32649.tif",
    "s1_guigang_0905_water": "data/processed/s1_water/guigang_20240905T105022_water20m.tif",
    "s1_guigang_0912_water": "data/processed/s1_water/guigang_20240912T104222_water20m.tif",
    "s1_guigang_0912_sigma": "data/processed/s1_water/guigang_20240912T104222_sigma20m.tif",
    "s1_jiulong_0905_water": "data/processed/s1_water/jiulong_20240905T105022_water20m.tif",
    "s1_jiulong_0917_water": "data/processed/s1_water/jiulong_20240917T105023_water20m.tif",
    "gf3_water_0806": "data/interim/ningming/water_0806.tif",
    "gf3_water_0824": "data/interim/ningming/water_0824.tif",
    "gf3_new_0824": "data/interim/ningming/water_new_0824.tif",
    "gf3_sar_0824": "data/raw/ningming_validation/淹没深度验证数据-崇左宁明县/宁明县8月24日SAR.tif",
    "config_base": "configs/base.yaml",
    "config_jiulong": "configs/cases/jiulong.yaml",
    "config_ningming": "configs/cases/ningming.yaml",
    "config_guigang": "configs/cases/guigang.yaml",
    "config_model_manifest": "configs/model_manifest.yaml",
}


def sha256(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while blk := f.read(chunk):
            h.update(blk)
    return h.hexdigest()


def main() -> None:
    out = {"generated_utc": datetime.now(timezone.utc).isoformat(),
           "algorithm": "sha256", "files": {}}
    for key, rel in TARGETS.items():
        p = PROJ / rel
        if not p.exists():
            out["files"][key] = {"path": rel, "missing": True}
            continue
        out["files"][key] = {"path": rel_posix(p), "bytes": p.stat().st_size,
                             "sha256": sha256(p)}
        print(key, out["files"][key]["bytes"], out["files"][key]["sha256"][:16])
    # 真外部资源：不写宿主盘符。`path` 写项目内默认位置（若资源在项目内）
    # 或写外部资源默认位置；两者都不可用时用 `external:<KEY>` 逻辑名占位，
    # 由 scripts/common/paths.py 的 external() 解析——消费者不得直接 open(path)。
    for key, ext_key in EXTERNAL_KEYS.items():
        found = external(ext_key)
        default_parts = EXTERNAL_DEFAULTS.get(ext_key, ())
        default_rel = "/".join(default_parts) if default_parts else f"external:{ext_key}"
        if found is None:
            out["files"][key] = {
                "path": default_rel, "external_id": ext_key, "missing": True,
                "note": f"外部资源未就位；设置 GX_{ext_key} 或放入 "
                        f"{default_rel} 后重跑本脚本",
            }
            print(key, "MISSING(external)", ext_key)
            continue
        rec = {"bytes": found.stat().st_size, "sha256": sha256(found),
               "external_id": ext_key}
        try:
            rec["path"] = rel_posix(found)
        except PathResolutionError:
            rec["path"] = default_rel  # 在项目外：写逻辑默认位置，不写盘符
        out["files"][key] = rec
        print(key, rec["bytes"], rec["sha256"][:16], "(external)")
    dst = PROJ / "data" / "catalog" / "input_manifest.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("->", dst)


if __name__ == "__main__":
    main()
