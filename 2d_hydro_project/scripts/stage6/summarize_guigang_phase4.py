"""Phase 2+4 结果汇总：把 guigang_phase4_scenarios.json 转成可读对照表 + 交叉核对。

只读 JSON，不碰 run 目录。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
SRC = PROJ / "data" / "catalog" / "guigang_phase4_scenarios.json"
TAGS = ("baseline_realdem", "flow_only_v1", "dualloss_v1", "rainloss_high_v1")
CELL_AREA = 100.0 * 100.0
DOMAIN_CELLS = 247530
DOMAIN_AREA = DOMAIN_CELLS * CELL_AREA


def main() -> None:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    R = d["runs"]
    b = R["baseline_realdem"]["res_max"]

    print("=" * 104)
    print("表 1  res.max 汇总（全时段最大值场）与相对基线变化")
    print("=" * 104)
    print(f"{'run':22s}{'wet_cells':>11s}{'wet_km2':>10s}{'Δwet%':>9s}"
          f"{'patches':>9s}{'Δpatch%':>10s}{'max_patch_km2':>15s}{'max_share%':>12s}")
    for tag in TAGS:
        m = R[tag]["res_max"]
        dw = 100.0 * (m["wet_cells"] - b["wet_cells"]) / b["wet_cells"]
        dp = 100.0 * (m["n_patches"] - b["n_patches"]) / b["n_patches"]
        print(f"{tag:22s}{m['wet_cells']:11d}{m['wet_area_km2']:10.2f}{dw:+9.1f}"
              f"{m['n_patches']:9d}{dp:+10.1f}{m['max_patch_km2']:15.2f}"
              f"{m['max_patch_share_pct']:12.1f}")

    print()
    print("=" * 104)
    print("表 2  斑块构成（小斑块占比）")
    print("=" * 104)
    for tag in TAGS:
        m = R[tag]["res_max"]
        print(f"{tag:22s} n_le4={m['n_le4_cells']:6d} ({m['n_le4_pct']:5.1f}%)   "
              f"n_le9={m['n_le9_cells']:6d} ({m['n_le9_pct']:5.1f}%)   "
              f"max_depth={m['max_depth_m']:6.2f} m")

    print()
    print("=" * 104)
    print("表 3  水量账目与质量门（E_mass 口径 = g15）")
    print("=" * 104)
    for tag in TAGS:
        ms = R[tag]["mass"]
        print(f"{tag:22s} V_rain={ms['v_rain_m3']:14.1f}  V_in={ms['v_in_integrated_m3']:14.1f}  "
              f"V_out={ms['v_out_integrated_m3']:14.1f}  V_end={ms['v_end_m3']:14.1f}  "
              f"E_mass={ms['e_mass_132']:.3e} {ms['gate_1e-3']}")

    print()
    print("=" * 104)
    print("表 4  净雨体积交叉核对（res.mass 的 V_rain ÷ 全域面积 应为 meta 登记的净雨 mm）")
    print("=" * 104)
    for tag in TAGS:
        ms = R[tag]["mass"]
        mm = ms["v_rain_m3"] / DOMAIN_AREA * 1000.0
        rain_meta = R[tag]["meta_rain"] or {}
        # 基线 meta.json 早于 --rain 改造，无 rain 段：按实际输入登记为 on
        mode = rain_meta.get("mode", "on(基线,meta 无 rain 段)")
        meta_mm = rain_meta.get("net_total_mm")
        if meta_mm is None:
            meta_mm = f"{mm:.4f}(由 V_rain 反算)"
        print(f"{tag:22s} mode={mode:28s} V_rain→{mm:8.4f} mm   meta={meta_mm}")

    print()
    print("=" * 104)
    print("表 5  逐帧关键窗口（t=72~216h，Q 仍基流或刚起步，湿面积增长几乎全靠降雨）")
    print("=" * 104)
    hdr = f"{'t_h':>6s}" + "".join(f"{t[:18]:>20s}" for t in TAGS)
    print(hdr)
    print(f"{'':6s}" + "".join(f"{'wet_km2/patches':>20s}" for _ in TAGS))
    for t_h in (72.0, 96.0, 108.0, 120.0, 144.0, 168.0, 216.0, 336.0):
        cells = [f"{t_h:6.0f}"]
        for tag in TAGS:
            ser = R[tag].get("series", [])
            hit = next((s for s in ser if abs(s["t_h"] - t_h) < 1e-6), None)
            cells.append(f"{hit['wet_area_km2']:9.2f}/{hit['n_patches']:<8d}"
                         if hit else f"{'--':>20s}")
        print("".join(cells))

    print()
    print("=" * 104)
    print("表 6  帧级湿像元差异分解（基线与 flow_only_v1，判断降雨专属像元）")
    print("=" * 104)
    import numpy as np
    from scripts.common.grid_io import read_ascii_grid

    base_dir = PROJ / "outputs" / "guigang" / "2024-09-yagi" / \
        "scen_yagi_q11500_100m_dt2p5_choff_realdem" / "results"
    fo_dir = PROJ / "outputs" / "guigang" / "2024-09-yagi" / \
        "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1" / "results"
    print(f"{'seq':>4s}{'t_h':>6s}{'base_wet':>10s}{'fo_wet':>9s}{'both':>8s}"
          f"{'rain_only':>11s}{'fo_only':>9s}{'rain_share%':>13s}")
    for seq in (12, 16, 18, 20, 24, 36, 48, 56):
        bp = base_dir / f"res-{seq:04d}.wd"
        fp = fo_dir / f"res-{seq:04d}.wd"
        if not (bp.is_file() and fp.is_file()):
            continue
        bw = read_ascii_grid(bp).data > 0.05
        fw = read_ascii_grid(fp).data > 0.05
        both = int((bw & fw).sum())
        ronly = int((bw & ~fw).sum())
        fonly = int((fw & ~bw).sum())
        tot = int(bw.sum())
        print(f"{seq:4d}{seq * 6:6d}{tot:10d}{int(fw.sum()):9d}{both:8d}"
              f"{ronly:11d}{fonly:9d}{100.0 * ronly / max(tot, 1):13.1f}")

    print()
    print("=" * 104)
    print("表 7  三个新 run 的 wall time / exit / peak RSS（来自 run_manifest + run.log）")
    print("=" * 104)
    for tag in ("flow_only_v1", "dualloss_v1", "rainloss_high_v1"):
        rec = R[tag]
        mf = PROJ / "outputs" / "guigang" / "2024-09-yagi" / rec["run"]
        man = json.loads((mf / "run_manifest.json").read_text(encoding="utf-8"))
        log = (mf / "run.log").read_text(encoding="utf-8", errors="replace")
        loop = [ln for ln in log.splitlines() if ln.startswith("loop time")]
        print(f"{tag:22s} peak_rss_mb={man.get('peak_rss_mb')}  "
              f"disk_mb={man.get('disk_output_mb')}  "
              f"mass_gate={man.get('mass_gate')}  {loop[0] if loop else 'no loop time'}")


if __name__ == "__main__":
    main()
