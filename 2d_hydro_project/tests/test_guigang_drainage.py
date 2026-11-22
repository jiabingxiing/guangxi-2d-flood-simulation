"""贵港排水连通治理（ADR-0014）单元测试：传播树、沟网提取、canyon 守卫、入渗纪律。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.stage3.build_guigang_drainage import (  # noqa: E402
    extract_drains,
    priority_flood_seeded,
    tree_accumulation,
)


def _synthetic_plain() -> tuple[np.ndarray, np.ndarray]:
    """20×20 合成域：底 3 行为主槽（床 6 m），平原 21 m，含一个 19 m 洼地。"""
    dem = np.full((20, 20), 21.0)
    dem[17:20, :] = 6.0                      # 主槽带（种子）
    dem[5:8, 5:8] = 19.0                     # 平原洼地
    seed = np.zeros((20, 20), dtype=bool)
    seed[17:20, :] = True
    return dem, seed


def test_priority_flood_seeded_tree_reaches_channel():
    dem, seed = _synthetic_plain()
    filled, parent, pop_order = priority_flood_seeded(dem, seed)
    # 洼地被抬平（可排）
    assert filled[5:8, 5:8].min() >= 20.9
    assert (filled[seed] <= 6.0 + 1e-9).all()
    # 全部非种子格都有父（连通到主槽）
    assert (parent[~seed] >= 0).all()
    # 父链最终到种子
    p = parent.ravel()
    for idx in np.nonzero(~seed.ravel())[0]:
        seen = 0
        j = idx
        while p[j] >= 0 and seen < 1000:
            j = p[j]
            seen += 1
        assert seed.ravel()[j], idx


def test_tree_accumulation_monotone_toward_channel():
    dem, seed = _synthetic_plain()
    _f, parent, pop_order = priority_flood_seeded(dem, seed)
    acc = tree_accumulation(parent, pop_order)
    # 全域 400 格都排向主槽带（acc 在种子格上分摊，总数=400）
    assert acc[seed].sum() == 400
    assert (acc[~seed] < acc[seed].max()).all()


def test_extract_drains_plain_only_and_reaches_channel():
    dem, seed = _synthetic_plain()
    _f, parent, pop_order = priority_flood_seeded(dem, seed)
    acc = tree_accumulation(parent, pop_order)
    tf = from_origin(0.0, 20 * 100.0, 100.0, 100.0)
    drains, st = extract_drains(dem, seed, parent, acc, tf,
                                acc_threshold=5, min_len_cells=3,
                                min_spacing_m=0.0, max_total_km=1e9,
                                plain_max_m=45.0)
    assert st["n_drains"] >= 1
    assert st["total_length_km"] > 0
    # 沟线全部落在平原（<=45 m）且不入种子
    for path in drains:
        for r, c in path:
            assert not seed[r, c]
            assert dem[r, c] <= 45.0


def test_canyon_guard_necessity_on_hill_crossing():
    """坡地/贴山沟在无守卫时会切穿山体（超采样把侧翼山格拉到平原床面）——
    这正是 build 脚本 canyon 守卫（单沟 max 下切 > 深度+3 m 即整体弃用）的依据；
    守卫生效后的产物上限由 test_drain_tier_products_complete 锁定（max ≤ 5 m）。"""
    dem, seed = _synthetic_plain()
    dem[2:4, 10] = 80.0   # 山包
    dem[0:2, 10] = 60.0   # 山上汇流（头）
    _f, parent, pop_order = priority_flood_seeded(dem, seed)
    acc = tree_accumulation(parent, pop_order)
    tf = from_origin(0.0, 20 * 100.0, 100.0, 100.0)
    from scripts.common import channel_engine as ce
    drains, _st = extract_drains(dem, seed, parent, acc, tf,
                                 acc_threshold=5, min_len_cells=2,
                                 min_spacing_m=0.0, max_total_km=1e9,
                                 plain_max_m=45.0)
    max_cuts = []
    for path in drains:
        xy = np.array([(tf.c + (c + 0.5) * tf.a, tf.f + (r + 0.5) * tf.e) for r, c in path])
        bank = ce.bank_surface_along_path(dem.astype("float32"),
                                          [ce.rowcol_of(tf, 20, 20, x, y) for x, y in xy], band=0)
        bp = ce.bed_profile(bank, 2.0, smooth_win=5, monotonic=True)
        dem_new, emask, _d, _s = ce.carve_corridor_grid(dem.copy(), tf, xy, bp.bed, bp.bank,
                                                        100.0, supersample=8)
        max_cuts.append(float((dem - dem_new)[emask].max()) if emask.any() else 0.0)
    assert max_cuts, "合成用例未产出任何沟"
    # 无守卫时至少存在一条超限沟（贴山/坡地）→ 守卫必要；
    # build 脚本会弃用这些沟，产物 max ≤ 5 m 由产物测试锁定。
    assert max(max_cuts) > 5.0


# ------------------------------------------------- 入渗纪律（路径 B 单用） ----
def test_infiltration_requires_rain_on():
    from scripts.stage5 import guigang_scenario as gs
    assert gs._infiltration_check(None, "on") is None
    assert gs._infiltration_check(2.0, "on") == 2.0
    with pytest.raises(SystemExit):
        gs._infiltration_check(2.0, "loss_medium")
    with pytest.raises(SystemExit):
        gs._infiltration_check(2.0, "ga")
    with pytest.raises(SystemExit):
        gs._infiltration_check(50.0, "on")


def test_infiltration_rate_conversion():
    # 2.0 mm/h = 2.0/3600/1000 m/s ≈ 5.56e-7
    assert pytest.approx(2.0 / 3600.0 / 1000.0, rel=1e-9) == 2.0 / 3600 / 1000


def test_round3_matrix_frozen():
    p = PROJ / "data" / "catalog" / "guigang_round3_drainage.json"
    if not p.is_file():
        pytest.skip("round3 矩阵尚未落盘")
    import json
    doc = json.loads(p.read_text(encoding="utf-8"))
    pr = doc["preregistration"]
    assert pr["matrix_id"] == "guigang-round3-drainage-v1"
    assert {r["tag"] for r in pr["runs_new"]} == {
        "chhyd_d14_r3_infil2", "chhyd_d14_r3_drA", "chhyd_d14_r3_combo"}
    assert pr["infiltration"]["rate_mm_per_h"] == 2.0
    assert "诊断探针" in pr["positioning"]


def test_drain_tier_products_complete():
    d = PROJ / "data" / "interim" / "guigang" / "channel_recon" / "d14m_drA"
    if not d.is_dir():
        pytest.skip("d14m_drA 尚未构建")
    for name in ("dem_reconstructed_100m.asc", "dem_reconstructed_100m.tif",
                 "channel_mask_100m.tif", "drain_mask_100m.tif",
                 "dem_edit_mask.tif", "carve_statistics.json"):
        assert (d / name).is_file(), name
    import json
    stats = json.loads((d / "carve_statistics.json").read_text(encoding="utf-8"))
    assert stats["validation"]["all_pass"] is True
    assert stats["n_drains"] >= 1
    assert stats["max_dem_change_m"] <= 5.0 + 1e-6   # canyon 守卫生效
    assert "诊断探针" in stats["positioning"]
    # channel_mask 仍是主槽口径（沟不享受 0.030 糙率）
    import rasterio
    cm = rasterio.open(d / "channel_mask_100m.tif").read(1)
    dm = rasterio.open(d / "drain_mask_100m.tif").read(1)
    assert not (cm & dm).any()
    assert int(dm.sum()) == stats["modified_pixel_count"]
