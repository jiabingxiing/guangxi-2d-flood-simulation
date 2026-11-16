"""阶段1：S1 数据完整性校验与登记（项目内 data/S1_gxhydro/sentinel1 六景）。

用法：uv --directory . run python scripts/stage1/verify_s1.py
校验：zip CRC 测试（zipfile.testzip）+ manifest/safe 文件存在性；
登记：大小、文件数、CRC 结果 -> data/catalog/s1_downloads.json 更新。

S1 原始 ZIP 已随项目迁移到 data/S1_gxhydro/（.gitignore 已排除，不入库）；
可用环境变量 GX_S1_ROOT 覆盖到项目外位置。
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
_DEFAULT_S1 = PROJ / "data" / "S1_gxhydro" / "sentinel1"
BASE = Path(os.environ.get("GX_S1_ROOT") or _DEFAULT_S1)
OUT = PROJ / "data" / "catalog" / "s1_downloads.json"

EXPECT = {
    "ningming": [
        "S1A_IW_GRDH_1SDV_20240910T105833_20240910T105858_055602_06C990_104D.SAFE.zip",
        "S1A_IW_GRDH_1SDV_20240912T225045_20240912T225114_055638_06CAF6_AE7D.SAFE.zip",
    ],
    "guigang": [
        "S1A_IW_GRDH_1SDV_20240905T105022_20240905T105047_055529_06C6A3_3DD2.SAFE.zip",
        "S1A_IW_GRDH_1SDV_20240912T104222_20240912T104247_055631_06CAB0_5344.SAFE.zip",
    ],
    "jiulong": [
        "S1A_IW_GRDH_1SDV_20240905T105022_20240905T105047_055529_06C6A3_3DD2.SAFE.zip",
        "S1A_IW_GRDH_1SDV_20240917T105023_20240917T105048_055704_06CD90_3527.SAFE.zip",
    ],
}


def main() -> None:
    results = {"verified_utc": datetime.now(timezone.utc).isoformat(), "scenes": {}}
    all_ok = True
    for aoi, names in EXPECT.items():
        for name in names:
            f = BASE / aoi / name
            rec = {"path": str(f), "size_gb": round(f.stat().st_size / 1e9, 3) if f.exists() else None}
            if not f.exists():
                rec["crc_ok"] = False
                rec["error"] = "missing"
                all_ok = False
            else:
                try:
                    with zipfile.ZipFile(f) as z:
                        bad = z.testzip()  # 逐成员 CRC；返回首个坏文件名或 None
                        n = len(z.namelist())
                        has_manifest = any("manifest.safe" in x.lower() for x in z.namelist())
                        rec.update(n_members=n, has_manifest=has_manifest, crc_ok=bad is None)
                        if bad:
                            rec["first_bad"] = bad
                            all_ok = False
                except Exception as exc:  # noqa: BLE001
                    rec["crc_ok"] = False
                    rec["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    all_ok = False
            results["scenes"][f"{aoi}/{name}"] = rec
            print(f"{aoi}/{name[:52]}...  crc_ok={rec.get('crc_ok')}  "
                  f"size={rec.get('size_gb')}GB  members={rec.get('n_members')}")
    results["all_ok"] = all_ok
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("ALL OK" if all_ok else "!!! 存在坏文件，见 s1_downloads.json", "->", OUT)


if __name__ == "__main__":
    main()
