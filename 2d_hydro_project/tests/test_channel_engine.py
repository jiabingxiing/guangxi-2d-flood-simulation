"""水力约束河槽重构引擎与贵港重构 DEM 契约测试（ADR-0012）。

锚点值来自 plans/无实测水下地形条件下的河槽水力刻蚀与几何重构算法（针对贵港干流漫滩）.txt
§2.4 的 Manning 反演算例（W=300, n=0.030, S=0.000116, z=2）与
data/catalog/guigang_channel_priors.json 预注册表。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common import channel_engine as ce  # noqa: E402
from scripts.common.grid_io import read_ascii_header  # noqa: E402

W_TOP, N_MAIN, SLOPE, Z = 300.0, 0.030, 0.000116, 2.0


# --------------------------------------------------------- Manning 反演 ----
@pytest.mark.parametrize(
    "q,expect",
    [(11500.0, 19.12), (8050.0, 14.92), (6900.0, 13.44), (5750.0, 11.90)],
)
def test_manning_depth_matches_plan_anchors(q, expect):
    d = ce.manning_trapezoid_depth(q, W_TOP, N_MAIN, SLOPE)
    assert abs(d - expect) <= 0.02


def test_manning_depth_monotonic_in_q():
    ds = [ce.manning_trapezoid_depth(q, W_TOP, N_MAIN, SLOPE)
          for q in (1000.0, 3000.0, 6000.0, 9000.0, 11000.0)]
    assert all(b > a for a, b in zip(ds, ds[1:]))


def test_manning_forward_matches_priors_alpha():
    priors = json.loads((PROJ / "data" / "catalog" / "guigang_channel_priors.json")
                        .read_text(encoding="utf-8"))
    for key, sc in priors["preregistration"]["depth_scenarios"].items():
        q = ce.manning_conveyance_q(float(sc["depth_m"]), W_TOP, N_MAIN, SLOPE)
        assert abs(q / 11500.0 - float(sc["alpha"])) <= 1e-3, key


def test_manning_rejects_impossible_flow():
    with pytest.raises(ValueError):
        ce.manning_trapezoid_depth(1e6, W_TOP, N_MAIN, SLOPE)


def test_bottom_width_rule_and_floor():
    assert ce.bottom_width_for(19.0, W_TOP) == pytest.approx(224.0)
    assert ce.bottom_width_for(10.0, W_TOP) == pytest.approx(260.0)
    # 深度过半顶宽时钳到 0.4W
    assert ce.bottom_width_for(80.0, W_TOP) == pytest.approx(0.4 * W_TOP)


# ------------------------------------------------------------ 断面形状 ----
def test_section_fraction_properties():
    b = ce.bottom_width_for(14.0, W_TOP)
    mid = (b / 2.0 + W_TOP / 2.0) / 2.0
    d = np.array([0.0, b / 2.0, mid, W_TOP / 2.0, W_TOP, 2 * W_TOP])
    f = np.asarray(ce.section_depth_fraction(d, W_TOP, b))
    assert f[0] == 1.0                       # 槽底
    assert f[1] == 1.0                       # 底宽边缘仍满深
    assert 0.0 < f[2] < 1.0                  # 岸坡过渡
    assert f[3] == pytest.approx(0.0)        # 岸顶
    assert (np.diff(f) <= 1e-12).all()        # 单调不升
    assert f[-1] == 0.0


def test_section_fraction_symmetric():
    b = ce.bottom_width_for(12.0, W_TOP)
    f = ce.section_depth_fraction(np.array([-120.0, 120.0]), W_TOP, b)
    assert float(f[0]) == pytest.approx(float(f[1]))


# ------------------------------------------------------------ 纵向控制 ----
def test_moving_mean_constant():
    x = np.full(50, 7.0)
    assert ce.moving_mean(x, 15) == pytest.approx(np.full(50, 7.0))


def test_bed_profile_monotonic_and_depth():
    bank = np.array([50.0, 48.0, 49.0, 45.0, 44.0, 46.0, 40.0, 39.0, 41.0, 38.0] * 4)
    bp = ce.bed_profile(bank, 12.0, smooth_win=3, monotonic=True)
    assert (np.diff(bp.bed) <= 1e-12).all()           # 顺流单调不升
    assert (bp.depth == 12.0).all()
    # 无累计取小时 bed = 平滑岸顶 − D（与 moving_mean 同口径）
    bp_free = ce.bed_profile(bank, 12.0, smooth_win=3, monotonic=False)
    assert bp_free.bed == pytest.approx(ce.moving_mean(bank, 3) - 12.0)


def test_bank_surface_local_min():
    dem = np.full((10, 10), 30.0)
    dem[5, 4] = 21.0
    rc = [(5, c) for c in range(3, 7)]
    surf = ce.bank_surface_along_path(dem, rc, band=1)
    assert surf.min() == pytest.approx(21.0)
    assert (surf <= 30.0 + 1e-9).all()


# ------------------------------------------------------------ 栅格刻蚀 ----
def _flat_grid(n=40, z0=50.0):
    dem = np.full((n, n), z0, dtype="float64")
    transform = from_origin(0.0, n * 100.0, 100.0, 100.0)
    return dem, transform


def test_carve_straight_channel_geometry():
    dem, transform = _flat_grid()
    n = 40
    path = np.column_stack([np.full(n, 2000.0), (np.arange(n) + 0.5) * 100.0])  # 竖直中线 col=20
    bank = np.full(n, 50.0)
    bed = bank - 10.0
    dem_new, mask, diff, stats = ce.carve_corridor_grid(
        dem, transform, path, bed, bank, 300.0, supersample=4)
    assert stats["modified_pixel_count"] > 0
    # 中线列刻到床面
    col = 20
    assert dem_new[10, col] == pytest.approx(40.0, abs=1e-6)
    # 走廊外零修改
    assert dem_new[0, 0] == pytest.approx(50.0)
    assert not mask[0, 0]
    # 宽度：|d|<150 m 内有刻蚀，|d|>=250 m 无
    assert mask[10, col] and mask[10, col - 1] and mask[10, col + 1]
    assert not mask[10, col - 3] and not mask[10, col + 3]
    # 下切不超过 D（平地）
    assert diff.max() <= 10.0 + 1e-6
    # 走廊内所有被刻格不低于床面
    assert dem_new[mask].min() >= 40.0 - 1e-6


def test_carve_never_raises_and_respects_min_rule():
    dem, transform = _flat_grid()
    dem[20, 18:23] = 35.0  # 天然洼地低于设计床面
    path = np.column_stack([np.full(40, 2000.0), (np.arange(40) + 0.5) * 100.0])
    bank = np.full(40, 50.0)
    bed = bank - 10.0
    dem_new, mask, _diff, _stats = ce.carve_corridor_grid(
        dem, transform, path, bed, bank, 300.0, supersample=4)
    # min 规则：自然洼地保持原值，不被抬高
    assert (dem_new[20, 18:23] == 35.0).all()
    # 全域不被抬高
    assert (dem_new <= dem + 1e-9).all()


def test_carve_terrain_above_bank_cuts_deeper_than_design():
    dem, transform = _flat_grid(z0=60.0)  # 地形高于岸顶参考
    path = np.column_stack([np.full(40, 2000.0), (np.arange(40) + 0.5) * 100.0])
    bank = np.full(40, 50.0)  # 岸顶参考取 50（局部最低）
    bed = bank - 10.0
    dem_new, _mask, diff, _stats = ce.carve_corridor_grid(
        dem, transform, path, bed, bank, 300.0, supersample=4)
    assert dem_new[10, 20] == pytest.approx(40.0)
    assert diff.max() == pytest.approx(20.0)  # 60 → 40


# ------------------------------------------------- 贵港产物契约（若已构建） ----
def test_guigang_corridor_grid_matches_baseline_run():
    base = PROJ / "outputs" / "guigang" / "2024-09-yagi" / \
        "scen_yagi_q11500_100m_dt2p5_choff_realdem" / "dem.asc"
    recon = PROJ / "data" / "interim" / "guigang" / "dem_layers" / "dem_raw_corridor_100m.asc"
    if not (base.is_file() and recon.is_file()):
        pytest.skip("基线 run 或重构走廊尚未构建")
    hb = read_ascii_header(base)
    hr = read_ascii_header(recon)
    for k in ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize"):
        assert hr[k] == pytest.approx(hb[k]), k


@pytest.mark.parametrize("depth", [10, 12, 14, 16, 19])
def test_guigang_recon_products_complete(depth):
    d = PROJ / "data" / "interim" / "guigang" / "channel_recon" / f"d{depth}m"
    if not d.is_dir():
        pytest.skip(f"d{depth}m 尚未构建")
    for name in ("dem_reconstructed_100m.asc", "dem_reconstructed_100m.tif",
                 "channel_mask_100m.tif", "dem_edit_mask.tif", "dem_edit_diff.tif",
                 "carve_statistics.json"):
        assert (d / name).is_file(), name
    stats = json.loads((d / "carve_statistics.json").read_text(encoding="utf-8"))
    assert stats["validation"]["all_pass"] is True
    assert stats["depth_m"] == depth
    assert stats["modified_pixel_count"] > 0
    # 重构层允许床面低于高程基准（datum 伪影，LISFLOOD 只用高程差）；
    # 只禁 nodata 级异常（G16 断言只适用于 raw 走廊）。
    with rasterio.open(d / "dem_reconstructed_100m.tif") as src:
        block = src.read(1)
    assert float(block.min()) > -100.0
    assert stats["dem_min_m"] == pytest.approx(float(block.min()), abs=0.01)


def test_guigang_carve_validation_report_consistent():
    p = PROJ / "data" / "catalog" / "guigang_channel_carve_validation.json"
    if not p.is_file():
        pytest.skip("验证报告尚未生成")
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert set(doc["scenarios"].keys()) == {"A_d10m", "B_d12m", "C_d14m", "D_d16m", "E_d19m"}
    for key, sc in doc["scenarios"].items():
        assert sc["validation_pass"] is True, key
        assert sc["conveyance"]["pass"] is True, key


def test_guigang_dem_layers_contract():
    p = PROJ / "data" / "catalog" / "guigang_dem_layers.json"
    if not p.is_file():
        pytest.skip("三层 DEM 契约尚未登记")
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["contract_version"] == "guigang_dem_layers_v1"
    assert doc["layers"]["dem_raw"]["edited_cells"] == 0
    assert doc["layers"]["dem_hydro"]["edited_cells"] == 0
    assert set(doc["layers"]["dem_hydraulic"].keys()) == {
        "A_d10m", "B_d12m", "C_d14m", "D_d16m", "E_d19m"}
    # raw 与 hydro 逐位相同（v1 零修改）
    raw = (PROJ / doc["layers"]["dem_raw"]["files"][0]).read_bytes()
    hydro = (PROJ / doc["layers"]["dem_hydro"]["files"][0]).read_bytes()
    assert raw == hydro


def test_guigang_centerline_registered():
    p = PROJ / "data" / "catalog" / "guigang_centerline_audit.json"
    g = PROJ / "data" / "interim" / "guigang" / "yujiang_centerline.geojson"
    if not (p.is_file() and g.is_file()):
        pytest.skip("中心线尚未构建")
    doc = json.loads(g.read_text(encoding="utf-8"))
    props = doc["features"][0]["properties"]
    assert props["source"]["source"] == "osm_overpass"
    assert props["length_m"] > 40000          # 郁江贵港段量级
    assert props["boundary_crossings"]["first_point_edge"] in ("S", "W")
    assert props["boundary_crossings"]["last_point_edge"] in ("E", "N")
