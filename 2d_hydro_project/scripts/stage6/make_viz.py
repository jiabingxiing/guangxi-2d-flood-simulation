"""汇报可视化资产（PPT 用）：图片 / 动图 / 表格图，输出 outputs/viz/。

受众：非专业汇报场景。设计：中文大字号、一图一结论、16:9 友好。
资产：
  fig1_overview_maps.png   三案例最大水深地图（站址标注）
  fig2_ningming_vs_obs.png 宁明 模拟 vs 卫星观测 三色命中图
  fig3_guigang_wse.png     贵港市区水位过程线（站基面，公开锚点对照）
  fig4_jiulong_robust.png  久隆五情景稳健性（面积条形 + 站址水深恒定线）
  fig5_ningming_heatmap.png 宁明 CSI 敏感性热力图（Q×糙率）
  fig6_summary_table.png   三案例成效汇总表
  anim_ningming.gif        宁明淹没演化（6h/帧）
  anim_guigang.gif         贵港淹没演化（12h/帧）
运行：uv run --with matplotlib python scripts/stage6/make_viz.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FuncAnimation, PillowWriter
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
VIZ = PROJ / "outputs" / "viz"
VIZ.mkdir(parents=True, exist_ok=True)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

WET_THR = 0.15  # 宁明主阈值（预注册，ADR-0006：不得按 CSI 选阈值；0.05/0.3 仅敏感性）


def read_asc(p: Path):
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    h = {l.split()[0].lower(): float(l.split()[1]) for l in hdr}
    return np.loadtxt(p, skiprows=6), h


def extent_of(hdr):
    return (hdr["xllcorner"], hdr["xllcorner"] + hdr["ncols"] * hdr["cellsize"],
             hdr["yllcorner"], hdr["yllcorner"] + hdr["nrows"] * hdr["cellsize"])


def site_xy(lon, lat, epsg):
    return Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)


RUNS = {
    "ningming": {"dir": PROJ / "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
                 "epsg": 32648, "site": ("宁明县城站", 107.0719, 22.1403)},
    "jiulong": {"dir": PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m",
                "epsg": 32649, "site": ("久隆500kV站", 108.7161, 22.0638),
                "sea_mask": PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m/sea_mask.npy"},
    "guigang": {"dir": PROJ / "outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m",
                "epsg": 32649, "site": ("贵港市区", 109.60, 23.11)},
}
SITE_INFO = {
    "ningming": "站址±2格最大水深 10.3 m\n(约100m邻域口径, ADR-0006)",
    "jiulong": "站址±2格最大水深 0.55 m\n(像元中心 0.008 m 基本不积水)",
    "guigang": "市区站址像元最大水深 10.5 m\n(±200m 口径 r200)",
}
TITLES = {"ningming": "宁明 · 2026-08 台风紫檀", "jiulong": "久隆 · 2024-09 台风摩羯",
          "guigang": "贵港 · 2024-09 郁江洪水"}


def fig1_overview():
    fig, axes = plt.subplots(1, 3, figsize=(13.33, 5.2), dpi=150)
    for ax, (key, cfg) in zip(axes, RUNS.items()):
        rmax, hdr = read_asc(cfg["dir"] / "results" / "res.max")
        sea = None
        if cfg.get("sea_mask") and cfg["sea_mask"].exists():
            sea = np.load(cfg["sea_mask"])
            if sea.shape != rmax.shape:
                sea = None
        rmax_use = np.where(sea, np.nan, rmax) if sea is not None else rmax
        ext = extent_of(hdr)
        wet = np.isfinite(rmax_use) & (rmax_use > WET_THR)
        im = ax.imshow(np.ma.masked_where(~wet, rmax_use), cmap="YlGnBu",
                       vmin=0, vmax=max(3.0, np.percentile(rmax_use[wet], 99) if wet.any() else 3.0),
                       extent=ext, origin="upper", interpolation="nearest")
        ax.imshow(np.ma.masked_where(wet, np.zeros_like(rmax_use)),
                  cmap="Greys", vmin=0, vmax=6, alpha=0.25, extent=ext, origin="upper",
                  interpolation="nearest")
        x, y = site_xy(*cfg["site"][1:], cfg["epsg"])
        ax.plot(x, y, marker="*", color="#d32f2f", ms=18, mec="white", mew=1.2, zorder=5)
        ax.set_title(TITLES[key], fontsize=13, fontweight="bold")
        ax.text(0.02, 0.98, SITE_INFO[key], transform=ax.transAxes, va="top", ha="left",
                fontsize=10.5, bbox=dict(fc="white", alpha=0.85, ec="#cccccc"))
        ax.set_xticks([])
        ax.set_yticks([])
        cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
        cb.set_label("最大水深 (m)", fontsize=10)
        cb.ax.tick_params(labelsize=9)
    fig.suptitle("三案例洪水淹没模拟 · 最大水深（情景级成果）", fontsize=16, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(VIZ / "fig1_overview_maps.png", bbox_inches="tight")
    plt.close(fig)


def fig2_ningming_vs_obs():
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import Affine
    from rasterio.warp import reproject
    cfg = RUNS["ningming"]
    # ADR-0006：验证用 8/24 观测时刻最近 6h 帧（78h = frame 13），不用 res.max
    frame_t_h, frame_idx = 78.0, 13
    wd, hdr = read_asc(cfg["dir"] / "results" / f"res-{frame_idx:04d}.wd")
    cell = hdr["cellsize"]
    tgt = Affine(cell, 0, hdr["xllcorner"], 0, -cell,
                 hdr["yllcorner"] + wd.shape[0] * cell)
    obs = np.zeros_like(wd, dtype="uint8")
    w06 = np.zeros_like(wd, dtype="uint8")
    with rasterio.open(PROJ / "data/interim/ningming/water_new_0824.tif") as ds:
        reproject(ds.read(1), obs, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32648", resampling=Resampling.nearest)
    with rasterio.open(PROJ / "data/interim/ningming/water_0806.tif") as ds:
        reproject(ds.read(1), w06, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32648", resampling=Resampling.nearest)
    sim = (wd > WET_THR) & ~(w06 == 1)  # 新增淹没口径（剔除 8/6 常水）
    ob = obs == 1
    rgb = np.full(wd.shape + (3,), 1.0)
    rgb[sim & ob] = (0.18, 0.49, 0.20)      # 命中
    rgb[sim & ~ob] = (0.78, 0.16, 0.16)     # 模拟偏多
    rgb[~sim & ob] = (0.09, 0.40, 0.76)     # 漏报
    H = int((sim & ob).sum()); FA = int((sim & ~ob).sum()); M = int((~sim & ob).sum())
    pod = H / max(H + M, 1)
    csi = H / max(H + FA + M, 1)
    far = FA / max(H + FA, 1)
    fig, ax = plt.subplots(figsize=(11.2, 7.0), dpi=150)
    ax.imshow(rgb, extent=extent_of(hdr), origin="upper")
    x, y = site_xy(*cfg["site"][1:], cfg["epsg"])
    ax.plot(x, y, marker="*", color="black", ms=16, mec="white", mew=1.2)
    ax.set_xticks([]); ax.set_yticks([])
    handles = [plt.Rectangle((0, 0), 1, 1, fc=(0.18, 0.49, 0.20)),
               plt.Rectangle((0, 0), 1, 1, fc=(0.78, 0.16, 0.16)),
               plt.Rectangle((0, 0), 1, 1, fc=(0.09, 0.40, 0.76))]
    ax.legend(handles, ["模拟命中观测", "模拟偏多（保守上界）", "漏报"], loc="lower left",
              fontsize=11, framealpha=0.9)
    ax.set_title(f"宁明：模拟 vs 卫星观测（8/24 新增淹没，主阈值 0.15 m，78h 帧）\n"
                 f"{pod:.0%} 的实际新增淹没区被命中",
                 fontsize=14, fontweight="bold")
    ax.text(0.985, 0.03, f"时刻切片口径：78h 帧（±6h，ADR-0006）\n"
                         f"POD={pod:.2f}  CSI={csi:.2f}  FAR={far:.2f}\n"
                         f"观测=高分三号新增淹没产品 49.5 km²\n"
                         f"模拟面积约 1.9× 偏大（保守上界解读；\n无排水网+均匀雨+喀斯特未建模）",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=10.5,
            bbox=dict(fc="white", alpha=0.88, ec="#cccccc"))
    fig.tight_layout()
    fig.savefig(VIZ / "fig2_ningming_vs_obs.png", bbox_inches="tight")
    plt.close(fig)


def fig3_guigang_wse():
    # ADR-0006 湿像元口径：G7 垂直基准未确认，模型 DEM 高程与站基面水位不可直接相减；
    # 两套曲线并排展示，只作时点/量级诊断对照（apparent offset 非常数）。
    v = json.loads((RUNS["guigang"]["dir"] / "verify.json").read_text(encoding="utf-8"))
    ser = v["wse_series"]
    t = [s["t_h"] for s in ser]
    zwet = [s["wse_wet_max_m"] for s in ser]
    fig, ax = plt.subplots(figsize=(11.2, 5.8), dpi=150)
    ax.plot(t, zwet, lw=2.2, color="#1565c0",
            label="模拟市区窗口湿像元 WSE（DEM 高程面）")
    for th, tv, lbl in ((228.0, 43.09, "贵港站 09-12T14 43.09 m（站基面）"),
                        (270.0, 44.00, "09-14T05 44.00 m（站基面）")):
        ax.plot([th], [tv], "o", ms=10, color="#c62828", zorder=5)
        ax.annotate(lbl, (th, tv), xytext=(-150, 10), textcoords="offset points",
                    fontsize=10.5, color="#c62828",
                    arrowprops=dict(arrowstyle="-", color="#c62828"))
    ax.annotate("两套高程不同基面（G7 未确认），不可直接相减：\n"
                "apparent offset +21.17 / +24.00 m，非常数 → 不作基准率定",
                xy=(0.02, 0.965), xycoords="axes fraction", va="top", fontsize=10.5,
                bbox=dict(fc="#fff8e1", alpha=0.92, ec="#ef6c00"))
    ax.set_xlabel("模拟时间（小时，自 09-03 起）", fontsize=11)
    ax.set_ylabel("高程/水位 (m)", fontsize=11)
    ax.set_title("贵港：模拟湿像元水位过程与公开洪峰时点（诊断对照，非率定）",
                 fontsize=14, fontweight="bold")
    ax.legend(fontsize=10.5, loc="lower right")
    ax.grid(alpha=0.25, lw=0.6)
    fig.tight_layout()
    fig.savefig(VIZ / "fig3_guigang_wse.png", bbox_inches="tight")
    plt.close(fig)


def fig4_jiulong_robust():
    s = json.loads((PROJ / "data/catalog/jiulong_scenarios_summary.json").read_text(encoding="utf-8"))
    order = ["free", "z0", "z05", "z10", "ztide"]
    names = ["自由出流", "海面 0 m", "海面 +0.5 m", "海面 +1.0 m", "天文潮时变"]
    areas = [s[k]["wet_area_km2"] for k in order]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12.4, 5.4), dpi=150)
    bars = a1.bar(names, areas, color=["#5c6bc0", "#5c6bc0", "#5c6bc0", "#5c6bc0", "#26a69a"])
    a1.set_ylabel("淹没面积 (km²)", fontsize=11)
    a1.set_title("淹没面积随潮位情景变化小", fontsize=13, fontweight="bold")
    for b, v_ in zip(bars, areas):
        a1.text(b.get_x() + b.get_width() / 2, v_ + 3, f"{v_:.0f}", ha="center", fontsize=10.5)
    a1.set_ylim(0, 330)
    a1.tick_params(axis="x", labelrotation=14)
    depths = [s[k]["anchor_depth_r2_m"] for k in order]
    a2.plot(names, depths, "o-", color="#c62828", lw=2, ms=8)
    a2.set_ylabel("站址±2格最大水深 (m)", fontsize=11)
    a2.set_ylim(0, 1.0)
    a2.set_title("站址±2格(约400m见方)水深恒 0.55 m\n像元中心 0.008 m 基本不积水",
                 fontsize=13, fontweight="bold")
    for i, d in enumerate(depths):
        a2.text(i, d + 0.04, f"{d:.2f}", ha="center", fontsize=10.5)
    a2.tick_params(axis="x", labelrotation=14)
    fig.suptitle("久隆：换 5 种下游潮位情景，站址结论不变 —— 雨成内涝型\n"
                 "（旧 1.68 m 为站址西南 1.28 km 洼地值，非站址，ADR-0006）",
                 fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(VIZ / "fig4_jiulong_robust.png", bbox_inches="tight")
    plt.close(fig)


def fig5_heatmap():
    e = json.loads((PROJ / "data/catalog/ningming_ensemble.json").read_text(encoding="utf-8"))
    qs = [2500, 4000, 6000]
    ns = [0.035, 0.06, 0.10]
    mat = np.zeros((len(ns), len(qs)))
    for r in e["runs"]:
        if r.get("rain_scale", 1.0) != 1.0 or "thresholds" not in r:
            continue
        qi = qs.index(int(r["q_peak"]))
        ni = ns.index(r["fpfric"])
        mat[ni, qi] = r["thresholds"]["0.15"]["csi"]  # 主阈值（预注册），非事后最优档
    fig, ax = plt.subplots(figsize=(8.6, 5.6), dpi=150)
    im = ax.imshow(mat, cmap="RdYlGn", vmin=0.28, vmax=0.37)
    for i in range(len(ns)):
        for j in range(len(qs)):
            ax.text(j, i, f"{mat[i, j]:.3f}", ha="center", va="center", fontsize=12,
                    fontweight="bold" if (i, j) == (2, 2) else "normal")
    ax.set_xticks(range(3), [f"上游洪峰\n{q} m³/s" for q in qs], fontsize=11)
    ax.set_yticks(range(3), [f"糙率 {n}" for n in ns], fontsize=11)
    ax.set_title("宁明一致性(CSI)对参数的灵敏度（主阈值 0.15 m，78h 帧）\n"
                 "CSI 全区间约 0.28~0.36；面积偏大主因不在 Q/糙率（见 B0/B3）",
                 fontsize=13, fontweight="bold")
    cb = fig.colorbar(im, ax=ax, fraction=0.046)
    cb.set_label("CSI 一致性", fontsize=10.5)
    fig.tight_layout()
    fig.savefig(VIZ / "fig5_ningming_heatmap.png", bbox_inches="tight")
    plt.close(fig)


def fig6_table():
    rows = [
        ["案例 / 事件", "与独立证据对照", "关键结论（情景级）"],
        ["宁明 · 2026-08 紫檀", "卫星新增淹没命中 POD 0.77\nCSI 0.36（主阈值 0.15 m）",
         "空间型态可用；面积约 1.9× 偏大\n（保守上界解读，非率定值）"],
        ["久隆 · 2024-09 摩羯", "5 种潮位情景结果一致",
         "站址±2格 0.55 m 基本不积水\n（1.68 m 为 1.28 km 外洼地）"],
        ["贵港 · 2024-09 郁江", "面积对上游 Q ±30% 仅 +7%\n公开洪峰时点作诊断对照",
         "湿像元水位仅诊断（G7 基准未确认）\n网格敏感：200 m 只作粗筛查"],
        ["质量保障", "E_mass：36 PASS + 3 FAIL-EXPLAINED\n（HFIX/HVAR 明示，门禁 1e-3）",
         "冻结重跑逐位一致\n42 项自动化测试全过"],
    ]
    fig, ax = plt.subplots(figsize=(12.6, 5.6), dpi=150)
    ax.axis("off")
    tb = ax.table(cellText=rows[1:], colLabels=rows[0], loc="center", cellLoc="center",
                  colWidths=[0.24, 0.34, 0.42])
    tb.auto_set_font_size(False)
    tb.set_fontsize(11.5)
    tb.scale(1, 2.5)
    for j in range(3):
        tb[0, j].set_facecolor("#283593")
        tb[0, j].set_text_props(color="white", fontweight="bold")
    for i in range(1, len(rows)):
        tb[i, 0].set_text_props(fontweight="bold")
        if i % 2 == 0:
            for j in range(3):
                tb[i, j].set_facecolor("#e8eaf6")
    ax.set_title("三案例模拟成效汇总", fontsize=15, fontweight="bold", pad=28)
    fig.tight_layout()
    fig.savefig(VIZ / "fig6_summary_table.png", bbox_inches="tight")
    plt.close(fig)


def make_gif(key, step, fps, vmax_factor=3.0):
    cfg = RUNS[key]
    res = cfg["dir"] / "results"
    wds = sorted(res.glob("res-*.wd"))[::step]
    frames = []
    hdr0 = None
    for w in wds:
        a, hdr = read_asc(w)
        hdr0 = hdr0 or hdr
        frames.append(np.ma.masked_where(a <= 0.05, a))
    vmax = max(vmax_factor, float(np.percentile(frames[-1].compressed(), 99)) if frames[-1].count() else 3)
    fig, ax = plt.subplots(figsize=(8.8, 6.2), dpi=110)
    im = ax.imshow(frames[0], cmap="YlGnBu", vmin=0, vmax=vmax,
                   extent=extent_of(hdr0), origin="upper", interpolation="nearest")
    x, y = site_xy(*cfg["site"][1:], cfg["epsg"])
    ax.plot(x, y, marker="*", color="#d32f2f", ms=17, mec="white", mew=1.2, zorder=5)
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, fraction=0.046)
    cb.set_label("水深 (m)", fontsize=11)
    title = ax.set_title("", fontsize=14, fontweight="bold")
    hours = 6.0 * step

    def upd(i):
        im.set_data(frames[i])
        title.set_text(f"{TITLES[key]} · 淹没演化   T+{i * hours:.0f} 小时")
        return im, title

    ani = FuncAnimation(fig, upd, frames=len(frames), interval=600)
    out = VIZ / f"anim_{key}.gif"
    ani.save(out, writer=PillowWriter(fps=fps))
    plt.close(fig)
    print("->", out, f"({len(frames)} 帧)")


if __name__ == "__main__":
    fig1_overview()
    fig2_ningming_vs_obs()
    fig3_guigang_wse()
    fig4_jiulong_robust()
    fig5_heatmap()
    fig6_table()
    if "--gif" in sys.argv:  # GIF 几何未变时无需重制（省 CPU）
        make_gif("ningming", step=1, fps=2)
        make_gif("guigang", step=2, fps=2)
    print("全部资产 ->", VIZ)
