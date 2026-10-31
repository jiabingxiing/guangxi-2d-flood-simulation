"""Phase 2 四层 DEM 契约测试（计划 §3.2 / §5 Phase 2 / §7.1）。

测试分三类：

1. **合成算例（已知答案的小地形）**——先用可手算的答案验证算法，再相信真实 DEM：
   (a) 单个封闭洼地被填到溢出高程；
   (b) 有出口的洼地不被过度填高；
   (c) 平坦面处理：填洼后仍有平地时必须报告，不得声称"无平坦面"。
2. **真实洼地保护**——S1 常水像元不得被填高；有证据的真实洼地不被抹除。
3. **四层契约**——四层互不覆盖、DEM_HYDRO ≠ DEM_HYDRAULIC 可检测、
   契约字段齐全、源文件哈希不变、目标目录有 res.mass 时退出。

本文件只读源数据，全部写操作都在 `tmp_path` 内。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import OverwriteRefused, guard_no_overwrite, sha256_file  # noqa: E402
from scripts.stage3.prepare_jiulong_dem_layers import (  # noqa: E402
    DEM_SOURCE,
    LAYER_CONTRACT,
    LAYER_ORDER,
    NODATA,
    REASON_NONE,
    REASON_RESAMPLE_ARTEFACT,
    REASON_SEA_MSL_ANCHOR,
    SELECTIVE_FILL_POLICY,
    build_layer_stack,
    d8_flow_direction,
    depression_inventory,
    edge_connected,
    flat_surface_stats,
    flow_direction_change,
    priority_flood_fill,
    selective_fill,
)

CATALOG = PROJ / "data" / "catalog" / "jiulong_dem_layers.json"
LAYERS_ROOT = PROJ / "data" / "interim" / "jiulong" / "dem_layers"


# --------------------------------------------------------------------------- #
# 合成算例 (a)：单个封闭洼地被填到溢出高程
# --------------------------------------------------------------------------- #
def test_closed_depression_filled_to_spill_elevation() -> None:
    """四周高、中心低的封闭洼地：填到溢出高程（洼缘最低点），且不超过它。

    地形（行=南→北，仅表示相邻关系）：

        10 10 10 10 10
        10  9  9  9 10
        10  9  1  9 10        <- 中心 1
        10  9  9  9 10
        10 10 10 10 10

    边界全为 10，因此溢出高程 = 10；填洼后中心必须正好是 10。
    """
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 9.0, 9.0, 9.0, 10.0],
        [10.0, 9.0, 1.0, 9.0, 10.0],
        [10.0, 9.0, 9.0, 9.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    res = priority_flood_fill(dem)
    assert res.filled[2, 2] == pytest.approx(10.0), "封闭洼地中心必须填到溢出高程 10 m"
    # 抬升量账目：3x3 洼地中 8 个 9 m 格 + 1 个 1 m 格
    assert res.raised[2, 2] == pytest.approx(9.0)
    assert res.raised[1, 1] == pytest.approx(1.0)
    expected_volume = (9.0 + 8 * 1.0)
    assert res.raised.sum() == pytest.approx(expected_volume)
    # 边界像元不得被抬高
    assert res.raised[0, :].sum() == 0
    assert res.raised[-1, :].sum() == 0
    assert res.raised[:, 0].sum() == 0
    assert res.raised[:, -1].sum() == 0
    # 填洼后不得再有更低的内点（除边界出流口）
    assert np.all(res.filled >= dem)


def test_closed_depression_respects_lowest_spill_point() -> None:
    """溢出口偏低时，填到该溢出口高程，而不是更高。

        10 10  5 10 10          <- 北边界有一个 5 m 低口
        10  5  5  5 10          <- 洼缘与溢出口同为 5 m
        10  5  1  5 10
        10  5  5  5 10
        10 10 10 10 10

    溢出高程 = 5，因此中心填到 5，不是 10。
    """
    dem = np.array([
        [10.0, 10.0, 5.0, 10.0, 10.0],
        [10.0, 5.0, 5.0, 5.0, 10.0],
        [10.0, 5.0, 1.0, 5.0, 10.0],
        [10.0, 5.0, 5.0, 5.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    res = priority_flood_fill(dem)
    assert res.filled[2, 2] == pytest.approx(5.0), "应填到最低溢出口高程 5 m"
    assert res.raised.sum() == pytest.approx(4.0), "只有中心 1 m→5 m 被抬高 4 m"
    # 不得填到 10 m（那就是过度填高）
    assert res.filled[2, 2] < 10.0


def test_open_depression_not_overfilled() -> None:
    """洼地有向下游的出口时，填到出口高程即止，不被填到域边界高程。

        30 30 30 30 30
        30 10  5  2  1        <- 逐级下降到东边界出流口 1 m
        30 30 30 30 30

    这是有出口的谷地，**不是洼地**：任何像元都不应被抬高。
    """
    dem = np.array([
        [30.0, 30.0, 30.0, 30.0, 30.0],
        [30.0, 10.0, 5.0, 2.0, 1.0],
        [30.0, 30.0, 30.0, 30.0, 30.0],
    ])
    res = priority_flood_fill(dem)
    assert np.allclose(res.filled, dem), "有出口的坡面不得被填高"
    assert res.raised.max() == pytest.approx(0.0)
    assert res.protect_violations == 0


def test_depression_with_outlet_filled_only_to_outlet() -> None:
    """洼地底部低于出口：填到出口高程，且不越过出口。

        20 20 20 20 20
        20  4  4  4 20
        20  4  1  4  3        <- 东侧邻域 3 m 为出口方向
        20  4  4  4 20
        20 20 20 20 20

    溢出高程 = 4（洼缘），不是 20，也不是出口的 3。
    """
    dem = np.array([
        [20.0, 20.0, 20.0, 20.0, 20.0],
        [20.0, 4.0, 4.0, 4.0, 20.0],
        [20.0, 4.0, 1.0, 4.0, 3.0],
        [20.0, 4.0, 4.0, 4.0, 20.0],
        [20.0, 20.0, 20.0, 20.0, 20.0],
    ])
    res = priority_flood_fill(dem)
    assert res.filled[2, 2] == pytest.approx(4.0), "填到洼缘高程 4 m，不得填到 20 m"
    assert res.filled[2, 4] == pytest.approx(3.0), "出口像元本身不得被抬高"
    assert res.raised[2, 2] == pytest.approx(3.0)


def test_valid_mask_prevents_fill_from_crossing_nodata() -> None:
    """NoData 像元不得被填，也不得作为传播通道。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0],
        [10.0, 1.0, NODATA, 10.0],
        [10.0, 10.0, 10.0, 10.0],
    ])
    valid = dem > NODATA + 1.0
    res = priority_flood_fill(dem, valid=valid)
    assert res.filled[1, 2] == pytest.approx(NODATA), "NoData 像元必须保持 NoData"
    assert res.filled[1, 1] == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# 合成算例 (c)：平坦面处理——填洼后仍有平地时必须报告
# --------------------------------------------------------------------------- #
def test_flat_surface_reported_after_fill() -> None:
    """填洼会产生平地；必须能检出并报告，不得声称"无平坦面"。

    与计划 §2.3 对应：禁止表述"Priority-Flood 不会制造平坦面"。
    """
    dem = np.array([
        [5.0, 5.0, 5.0, 5.0, 5.0],
        [5.0, 5.0, 5.0, 5.0, 5.0],
        [5.0, 5.0, 1.0, 5.0, 5.0],
        [5.0, 5.0, 5.0, 5.0, 5.0],
        [5.0, 5.0, 5.0, 5.0, 5.0],
    ])
    res = priority_flood_fill(dem)
    stats = flat_surface_stats(res.filled)
    # 整幅地形填平后完全平坦：每个像元都没有严格更低的邻域
    assert stats["no_downslope_cells"] == 25
    assert stats["level_neighbour_cells"] == 25
    # 填洼后严格洼点必须为 0（填洼确实消除了闭合洼地）
    assert stats["strict_pit_cells"] == 0
    # 输出必须是"数量 + 免责声明"，不能是平坦面存在性结论
    assert "statement" in stats
    assert "不对平坦面是否存在下结论" in stats["statement"]
    # D8 在完全平坦地形上无下游
    flow = d8_flow_direction(res.filled)
    assert np.all(flow == -1), "完全平坦地形上 D8 无下游"


def test_flat_surface_count_reported_for_real_terrain_shape() -> None:
    """填洼后等值面像元数必须增加（填洼确实造出平地），且严格洼点归零。

    注意：严格洼点要求**全部**邻域严格更高，因此两个相邻的等值低格不构成
    严格洼点。这里只放 1 个低格，确保填洼前确实存在严格洼点。
    """
    dem = np.array([
        [9.0, 9.0, 9.0, 9.0, 9.0, 9.0],
        [9.0, 3.0, 3.0, 3.0, 3.0, 9.0],
        [9.0, 3.0, 1.0, 3.0, 3.0, 9.0],
        [9.0, 3.0, 3.0, 3.0, 3.0, 9.0],
        [9.0, 9.0, 9.0, 9.0, 9.0, 9.0],
    ])
    before = flat_surface_stats(dem)
    res = priority_flood_fill(dem)
    after = flat_surface_stats(res.filled)
    assert before["strict_pit_cells"] == 1, "填洼前应有 1 个严格洼点"
    assert after["strict_pit_cells"] == 0, "填洼后不得残留严格洼点"
    # 填洼确实造出平地：内圈 3x4 全部变成 9 m
    assert after["level_neighbour_cells"] > before["level_neighbour_cells"]
    assert after["no_downslope_cells"] > before["no_downslope_cells"]


def test_flat_stats_never_returns_affirmative_no_flat_claim() -> None:
    """平坦面统计的措辞不得出现"无平坦面"的肯定结论。"""
    stats = flat_surface_stats(np.arange(9, dtype=float).reshape(3, 3))
    blob = json.dumps(stats, ensure_ascii=False)
    assert "Priority-Flood 不会制造平坦面" not in blob
    assert "statement" in stats


# --------------------------------------------------------------------------- #
# 真实洼地保护：S1 常水像元不得被填高
# --------------------------------------------------------------------------- #
def test_protected_water_cells_never_raised() -> None:
    """受保护像元（S1 常水）在 Priority-Flood 中永不被抬高。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    protect = np.zeros(dem.shape, dtype=bool)
    protect[2, 2] = True  # 中心为 S1 常水
    res = priority_flood_fill(dem, protect=protect)
    assert res.filled[2, 2] == pytest.approx(0.5), "受保护像元必须保持原值"
    assert res.raised[2, 2] == pytest.approx(0.0)
    assert res.protect_violations == 0
    # 邻域仍按正常规则填高（保护不阻碍周边排水分析）
    assert res.filled[1, 1] == pytest.approx(10.0)


def test_protection_reduces_raise_versus_no_protection() -> None:
    """有保护 vs 无保护的抬升量必须可对比，且保护版对受保护像元抬升为 0。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    protect = np.zeros(dem.shape, dtype=bool)
    protect[2, 2] = True
    guarded = priority_flood_fill(dem, protect=protect)
    unguarded = priority_flood_fill(dem, protect=None)
    assert unguarded.raised[2, 2] > 0.0, "无保护时该像元本会被抬高"
    assert guarded.raised[2, 2] == pytest.approx(0.0)
    assert guarded.raised.sum() < unguarded.raised.sum()


def test_layer_stack_never_raises_s1_water() -> None:
    """四层构建中，DEM_HYDRO 不得抬高任何 S1 常水像元（真实洼地保护）。"""
    rng = np.random.default_rng(7)
    raw = rng.uniform(-3.0, 40.0, size=(20, 20))
    valid = np.ones(raw.shape, dtype=bool)
    s1 = np.zeros(raw.shape, dtype=bool)
    s1[8:12, 8:12] = True
    raw[s1] = -6.0  # 常水处为负值（本项目实测情形）
    sea = edge_connected(s1)
    stack = build_layer_stack(raw, valid=valid, s1_water=s1, sea=sea,
                              cell_area_m2=40000.0)
    hydro = stack.layers["DEM_HYDRO"]
    repaired = stack.layers["DEM_REPAIRED"]
    assert np.all(hydro[s1] <= repaired[s1] + 1e-9), "DEM_HYDRO 抬高了 S1 常水像元"
    assert stack.fill.protect_violations == 0


def test_real_depression_kept_in_hydraulic_layer() -> None:
    """DEM_HYDRAULIC 必须保留真实洼地：不做任何填洼。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    valid = np.ones(dem.shape, dtype=bool)
    s1 = np.zeros(dem.shape, dtype=bool)
    # 用宽松策略确保该洼地会被 DEM_HYDRO 填（5x5 洼地超过默认 max_cells=9）
    permissive = dict(SELECTIVE_FILL_POLICY, max_raise_m=100.0, max_cells=100)
    stack = build_layer_stack(dem, valid=valid, s1_water=s1, sea=s1,
                              cell_area_m2=40000.0, fill_policy=permissive)
    hydraulic = stack.layers["DEM_HYDRAULIC"]
    assert hydraulic[2, 2] == pytest.approx(0.5), "水动力层必须保留真实洼地底部"
    assert np.allclose(hydraulic, stack.layers["DEM_REPAIRED"]), (
        "DEM_HYDRAULIC 只等于 DEM_REPAIRED，不做任何填洼"
    )
    assert stack.layers["DEM_HYDRO"][2, 2] > hydraulic[2, 2], "DEM_HYDRO 应已填高该洼地"


def test_protected_water_acts_as_sink_and_is_documented() -> None:
    """受保护常水像元会成为有意保留的洼点：必须可检测并登记，不得隐藏。

    "不抹除有证据真实洼地"优先于"完全无洼 D8 场"（计划 §3.5 / §8 风险表）。
    """
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    valid = np.ones(dem.shape, dtype=bool)
    s1 = np.zeros(dem.shape, dtype=bool)
    s1[2, 2] = True  # 洼底是 S1 常水
    permissive = dict(SELECTIVE_FILL_POLICY, max_raise_m=100.0, max_cells=100)
    guarded = build_layer_stack(dem, valid=valid, s1_water=s1, sea=s1,
                                cell_area_m2=40000.0, fill_policy=permissive)
    unguarded = build_layer_stack(dem, valid=valid, s1_water=np.zeros_like(s1), sea=s1,
                                  cell_area_m2=40000.0, fill_policy=permissive)
    # 保护版：常水像元保持原值 0.5 m，周围照常填到 10 m
    assert guarded.layers["DEM_HYDRO"][2, 2] == pytest.approx(0.5)
    assert guarded.layers["DEM_HYDRO"][1, 1] == pytest.approx(10.0)
    # 无保护版：整洼填到 10 m
    assert unguarded.layers["DEM_HYDRO"][2, 2] == pytest.approx(10.0)
    # 保护代价必须可量化：保护版填得少
    assert guarded.fill.raised.sum() < unguarded.fill.raised.sum()
    # 有意保留的洼点必须被平坦面统计检出（不得声称无洼点）
    assert flat_surface_stats(guarded.layers["DEM_HYDRO"])["strict_pit_cells"] == 1
    assert flat_surface_stats(unguarded.layers["DEM_HYDRO"])["strict_pit_cells"] == 0


def test_protection_is_order_independent() -> None:
    """保护结果不得依赖堆弹出顺序：同样输入重复运行必须逐位相同。"""
    rng = np.random.default_rng(3)
    dem = rng.uniform(0.0, 20.0, size=(30, 30))
    s1 = np.zeros(dem.shape, dtype=bool)
    s1[10:14, 10:14] = True
    dem[s1] = 1.0
    first = priority_flood_fill(dem, protect=s1)
    second = priority_flood_fill(dem.copy(), protect=s1)
    assert np.array_equal(first.filled, second.filled), "保护填洼结果不确定"
    assert np.all(first.filled[s1] == dem[s1]), "受保护像元必须保持原值"


def test_protect_violations_counted() -> None:
    """保护违规计数必须可用（本项目要求恒为 0）。"""
    dem = np.full((5, 5), 10.0)
    dem[2, 2] = 1.0
    s1 = np.zeros(dem.shape, dtype=bool)
    s1[2, 2] = True
    res = priority_flood_fill(dem, protect=s1)
    assert res.protect_violations == 0
    assert res.raised[2, 2] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 选择性填洼：只填微坑，大洼地只登记
# --------------------------------------------------------------------------- #
def test_selective_fill_skips_large_depression() -> None:
    """超过局部性上限的洼地只登记、不填。"""
    dem = np.full((12, 12), 20.0)
    dem[3:9, 3:9] = 2.0   # 6x6 = 36 格洼地，超过 max_cells=9
    valid = np.ones(dem.shape, dtype=bool)
    protect = np.zeros(dem.shape, dtype=bool)
    policy = dict(SELECTIVE_FILL_POLICY, max_cells=9, max_raise_m=100.0)
    res = selective_fill(dem, valid=valid, protect=protect, policy=policy, cell_area_m2=40000.0)
    assert res.applied["n_applied"] == 0
    assert res.registered_only["n_registered"] >= 1
    assert np.allclose(res.filled, dem), "大洼地不得被填"
    assert res.registered_only["fill_volume_m3"] > 0


def test_selective_fill_applies_small_depression() -> None:
    """符合策略的微坑被填入。"""
    dem = np.full((9, 9), 20.0)
    dem[4, 4] = 19.5  # 单格微坑，抬高 0.5 m
    valid = np.ones(dem.shape, dtype=bool)
    protect = np.zeros(dem.shape, dtype=bool)
    policy = dict(SELECTIVE_FILL_POLICY, max_cells=9, max_raise_m=1.0)
    res = selective_fill(dem, valid=valid, protect=protect, policy=policy, cell_area_m2=40000.0)
    assert res.applied["n_applied"] == 1
    assert res.filled[4, 4] == pytest.approx(20.0)
    assert res.registered_only["n_registered"] == 0


def test_selective_fill_skips_deep_depression() -> None:
    """抬升量超过策略上限的洼地只登记、不填（避免改写真实洼地）。"""
    dem = np.full((9, 9), 20.0)
    dem[4, 4] = 5.0  # 抬高 15 m，远超 1 m 上限
    valid = np.ones(dem.shape, dtype=bool)
    protect = np.zeros(dem.shape, dtype=bool)
    res = selective_fill(dem, valid=valid, protect=protect, cell_area_m2=40000.0)
    assert res.applied["n_applied"] == 0
    assert res.registered_only["n_registered"] == 1
    assert res.filled[4, 4] == pytest.approx(5.0)


def test_selective_fill_policy_is_preregistered() -> None:
    """填洼策略必须预先登记（不得按结果事后调整）。"""
    assert SELECTIVE_FILL_POLICY["registered_before_processing"] is True
    assert "max_raise_m" in SELECTIVE_FILL_POLICY
    assert "max_cells" in SELECTIVE_FILL_POLICY
    assert SELECTIVE_FILL_POLICY["max_raise_evidence"], "策略阈值必须有证据出处"


# --------------------------------------------------------------------------- #
# 四层契约：互不覆盖 / 差异可检测 / 字段齐全
# --------------------------------------------------------------------------- #
def test_four_layers_do_not_overwrite_each_other() -> None:
    """四层必须是独立数组；改一层不得改动另一层。"""
    rng = np.random.default_rng(11)
    raw = rng.uniform(0.0, 30.0, size=(15, 15))
    valid = np.ones(raw.shape, dtype=bool)
    s1 = np.zeros(raw.shape, dtype=bool)
    stack = build_layer_stack(raw, valid=valid, s1_water=s1, sea=s1, cell_area_m2=40000.0)
    arrays = [stack.layers[name] for name in LAYER_ORDER]
    for i, a in enumerate(arrays):
        for j, b in enumerate(arrays):
            if i == j:
                continue
            assert a is not b, f"{LAYER_ORDER[i]} 与 {LAYER_ORDER[j]} 共享同一数组"
    before = {name: stack.layers[name].copy() for name in LAYER_ORDER}
    stack.layers["DEM_HYDRO"][0, 0] = 12345.0
    for name in LAYER_ORDER:
        if name == "DEM_HYDRO":
            continue
        assert np.array_equal(stack.layers[name], before[name]), f"{name} 被连带修改"


def test_hydro_and_hydraulic_difference_detected() -> None:
    """DEM_HYDRO ≠ DEM_HYDRAULIC 时必须可检测，且 DEM_HYDRO 不是水动力输入。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    valid = np.ones(dem.shape, dtype=bool)
    s1 = np.zeros(dem.shape, dtype=bool)
    permissive = dict(SELECTIVE_FILL_POLICY, max_raise_m=100.0, max_cells=100)
    stack = build_layer_stack(dem, valid=valid, s1_water=s1, sea=s1,
                              cell_area_m2=40000.0, fill_policy=permissive)
    hydro = stack.layers["DEM_HYDRO"]
    hydraulic = stack.layers["DEM_HYDRAULIC"]
    differs = int(np.count_nonzero(np.abs(hydro - hydraulic) > 1e-9))
    assert differs > 0, "DEM_HYDRO 与 DEM_HYDRAULIC 的差异必须可检测"
    assert not np.allclose(hydro, hydraulic)
    # 差异必须只在 DEM_HYDRO 抬高方向（不得下切）
    assert np.all(hydro >= hydraulic - 1e-9)


def test_hydro_equals_hydraulic_when_nothing_filled() -> None:
    """无可填洼地时两层可以相同；此时契约仍要求单独登记两层。"""
    dem = np.array([[5.0, 4.0, 3.0], [4.0, 3.0, 2.0], [3.0, 2.0, 1.0]])
    valid = np.ones(dem.shape, dtype=bool)
    s1 = np.zeros(dem.shape, dtype=bool)
    stack = build_layer_stack(dem, valid=valid, s1_water=s1, sea=s1, cell_area_m2=40000.0)
    assert np.allclose(stack.layers["DEM_HYDRO"], stack.layers["DEM_HYDRAULIC"])
    # 但四层仍是四个独立数组
    assert stack.layers["DEM_HYDRO"] is not stack.layers["DEM_HYDRAULIC"]


def test_layer_contract_table_matches_plan() -> None:
    """四层契约必须逐条落实计划 §3.2 表格的用途/允许操作/必须记录。"""
    assert set(LAYER_ORDER) == {"DEM_RAW", "DEM_REPAIRED", "DEM_HYDRO", "DEM_HYDRAULIC"}
    for name in LAYER_ORDER:
        contract = LAYER_CONTRACT[name]
        assert contract["purpose"], f"{name} 缺用途"
        assert contract["allowed_operations"], f"{name} 缺允许操作"
        assert contract["forbidden_operations"], f"{name} 缺禁止操作"
        assert contract["downstream_use"], f"{name} 缺下游用途"
    # 计划 §3.2：DEM_RAW 不修改
    assert "不修改" in LAYER_CONTRACT["DEM_RAW"]["purpose"]
    # 计划 §3.2：DEM_HYDRAULIC 保留真实洼地
    assert "保留真实洼地" in LAYER_CONTRACT["DEM_HYDRAULIC"]["purpose"]
    # 计划 §3.2：DEM_HYDRO 不得直接替代 DEM_HYDRAULIC
    assert any("替代" in op for op in LAYER_CONTRACT["DEM_HYDRO"]["forbidden_operations"])
    # 计划 §3.2：DEM_HYDRO 允许操作仅"选择性填洼"
    assert any("选择性填洼" in op for op in LAYER_CONTRACT["DEM_HYDRO"]["allowed_operations"])


def test_repaired_layer_reason_codes_are_traceable() -> None:
    """DEM_REPAIRED 每个改动像元必须有原因码，且只允许两类有来源的原因。"""
    raw = np.array([
        [5.0, -1.0, -2.0, 5.0],
        [5.0, -0.5, -3.0, 5.0],
        [5.0, 5.0, 5.0, 5.0],
        [5.0, 5.0, 5.0, 5.0],
    ])
    valid = np.ones(raw.shape, dtype=bool)
    s1 = np.zeros(raw.shape, dtype=bool)
    s1[1, 1] = True
    sea = edge_connected(np.array([
        [False, False, False, False],
        [True, False, False, False],
        [False, False, False, False],
        [False, False, False, False],
    ]))
    block_mean = np.full(raw.shape, 1.5)  # 源块均值 >= 0 → 重采样欠冲
    stack = build_layer_stack(raw, valid=valid, s1_water=s1, sea=sea, block_mean=block_mean,
                              cell_area_m2=40000.0)
    reason = stack.reason
    changed = reason != REASON_NONE
    # 每个改动像元必须能追到原因码
    assert int(np.count_nonzero(changed)) > 0
    allowed = {REASON_RESAMPLE_ARTEFACT, REASON_SEA_MSL_ANCHOR}
    assert set(np.unique(reason[changed])).issubset(allowed), (
        f"出现未登记的原因码：{sorted(set(np.unique(reason[changed])) - allowed)}"
    )
    # 未被修的负值保持负值（不做全域钳位）
    assert stack.layers["DEM_REPAIRED"][2, 1] == pytest.approx(5.0)


def test_no_global_clamping_of_negative_values() -> None:
    """负值不得被全域钳位：无证据的负值必须保留在 DEM_REPAIRED 中。"""
    raw = np.full((6, 6), 8.0)
    raw[2:4, 2:4] = -4.0  # 4 格负值，不与 S1 水体重合
    valid = np.ones(raw.shape, dtype=bool)
    s1 = np.zeros(raw.shape, dtype=bool)
    sea = np.zeros(raw.shape, dtype=bool)
    stack = build_layer_stack(raw, valid=valid, s1_water=s1, sea=sea, cell_area_m2=40000.0)
    repaired = stack.layers["DEM_REPAIRED"]
    assert np.allclose(repaired[2:4, 2:4], -4.0), "无证据负值不得被钳位或插值"
    assert int(np.count_nonzero(stack.reason)) == 0, "无证据时不得产生任何修复原因码"


def test_no_minimum_slope_or_uniform_incision() -> None:
    """不得默认施加全域最小坡度，也不得做全域固定刻蚀。"""
    dem = np.full((8, 8), 12.0)
    valid = np.ones(dem.shape, dtype=bool)
    s1 = np.zeros(dem.shape, dtype=bool)
    stack = build_layer_stack(dem, valid=valid, s1_water=s1, sea=s1, cell_area_m2=40000.0)
    for name in ("DEM_RAW", "DEM_REPAIRED", "DEM_HYDRAULIC"):
        assert np.allclose(stack.layers[name], 12.0), f"{name} 施加了额外坡度或刻蚀"
    # DEM_HYDRO 只允许抬高，不允许下切（刻蚀）
    assert np.all(stack.layers["DEM_HYDRO"] >= stack.layers["DEM_REPAIRED"] - 1e-9)


def test_depression_inventory_reports_volume_and_protection() -> None:
    """洼地登记必须给出面积、体积、最大抬升与是否含受保护水体。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    valid = np.ones(dem.shape, dtype=bool)
    s1 = np.zeros(dem.shape, dtype=bool)
    s1[2, 2] = True
    filled = priority_flood_fill(dem, protect=s1).filled
    inv = depression_inventory(dem, filled, cell_area_m2=40000.0, valid=valid, protect=s1)
    # 受保护像元（洼底）保持 0.5 m，其余 8 个 2 m 格被填到 10 m
    assert inv["filled_cells"] == 8, "受保护像元不得计入填高"
    assert inv["n_depressions"] == 1
    assert inv["fill_volume_m3"] == pytest.approx(8 * 8.0 * 40000.0)
    assert inv["max_raise_m"] == pytest.approx(8.0)
    # 填高连通域不含受保护像元（它本身没被抬高）
    assert inv["with_protected_water_count"] == 0
    # 洼地体积账目与 Priority-Flood 的抬升总量一致
    assert inv["fill_volume_m3"] == pytest.approx(
        float(priority_flood_fill(dem, protect=s1).raised.sum()) * 40000.0)


def test_flow_direction_change_detects_rewiring() -> None:
    """流向变化必须可量化（填洼会改变 D8 指向）。"""
    dem = np.array([
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 2.0, 0.5, 2.0, 10.0],
        [10.0, 2.0, 2.0, 2.0, 10.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
    ])
    valid = np.ones(dem.shape, dtype=bool)
    before = d8_flow_direction(dem, valid=valid)
    after = d8_flow_direction(priority_flood_fill(dem).filled, valid=valid)
    change = flow_direction_change(before, after)
    assert change["changed_cells"] > 0
    assert change["undefined_after_cells"] >= change["undefined_before_cells"]


# --------------------------------------------------------------------------- #
# 已生成产物的契约检查（未生成时跳过，保证 CI 在 Phase 2 前后都可用）
# --------------------------------------------------------------------------- #
def _load_catalog() -> dict:
    if not CATALOG.is_file():
        pytest.skip("jiulong_dem_layers.json 尚未生成（Phase 2 未运行）")
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_catalog_covers_four_layers_with_required_fields() -> None:
    """catalog 必须逐层给出任务书要求的全部字段。"""
    doc = _load_catalog()
    assert doc["layer_order"] == list(LAYER_ORDER)
    required = (
        "purpose", "allowed_operations", "forbidden_operations", "parent_layer",
        "operations_applied", "changed_cells", "changed_area_km2", "max_abs_change_m",
        "fill_volume_m3", "flow_direction_change", "flat_surface_check",
        "uncertainty", "rollback_path", "sha256", "crs", "resolution_m", "nodata",
    )
    for name in LAYER_ORDER:
        rec = doc["layers"][name]
        missing = [k for k in required if k not in rec]
        assert not missing, f"{name} 缺字段：{missing}"
        assert rec["crs"] == "EPSG:32649"
        assert float(rec["resolution_m"]) > 0
        assert rec["rollback_path"]
        assert rec["uncertainty"]


def test_catalog_declares_vertical_datum_unclosed() -> None:
    """垂直基准未闭合（G7）：不得给绝对高程结论。"""
    doc = _load_catalog()
    datum = doc["vertical_datum"]
    assert datum["status"] == "unclosed"
    assert datum["datum_offset_m"] is None, "不得填猜测的基准偏移常数"
    for name in LAYER_ORDER:
        assert doc["layers"][name]["uncertainty"]["vertical_datum"]["status"] == "unclosed"


def test_catalog_flat_surface_reported_not_denied() -> None:
    """填洼后必须报告平坦面数量，且不得出现"无平坦面"的肯定表述。"""
    doc = _load_catalog()
    flat = doc["flat_surface_summary"]
    for key in ("DEM_RAW", "DEM_REPAIRED", "DEM_HYDRO", "DEM_HYDRAULIC"):
        assert key in flat
        assert "no_downslope_cells" in flat[key]
    blob = json.dumps(doc, ensure_ascii=False)
    assert "Priority-Flood 不会制造平坦面" not in blob
    assert "无平坦面" not in blob


def test_catalog_reports_zero_protection_violations() -> None:
    """真实洼地保护：catalog 必须报告 0 违规。"""
    doc = _load_catalog()
    prot = doc["real_depression_protection"]
    assert prot["raised_in_dem_hydro_cells"] == 0
    assert prot["raised_with_protection_cells"] == 0
    assert doc["layers"]["DEM_HYDRO"]["protect_violations"] == 0


def test_catalog_hydro_not_substituting_hydraulic() -> None:
    """DEM_HYDRO 不得直接替代 DEM_HYDRAULIC。"""
    doc = _load_catalog()
    sub = doc["substitution_rules"]
    assert sub["enforced"] is True
    assert sub["hydro_differs_from_hydraulic_cells"] > 0
    assert sub["hydro_equals_hydraulic"] is False


def test_catalog_source_hash_recorded_and_source_unchanged() -> None:
    """源 DEM 哈希必须登记，且与磁盘当前哈希一致（只读约束）。"""
    doc = _load_catalog()
    rec = doc["sources"]["dem_source"]
    assert rec["sha256"]
    assert rec["sha256"] == sha256_file(DEM_SOURCE), "源 DEM 被改写了"
    assert doc["sources"]["s1_water"]["sha256"]


def test_layer_outputs_written_with_metadata() -> None:
    """每层 GeoTIFF 必须写 CRS/分辨率/nodata，且 meta.json 带 sha256。"""
    if not LAYERS_ROOT.is_dir():
        pytest.skip("dem_layers 尚未生成（Phase 2 未运行）")
    import rasterio

    doc = _load_catalog()
    for name in LAYER_ORDER:
        layer_dir = LAYERS_ROOT / name.lower()
        tif = layer_dir / f"{name.lower()}.tif"
        assert tif.is_file(), f"{name} 缺 GeoTIFF"
        with rasterio.open(tif) as ds:
            assert ds.crs is not None and ds.crs.to_epsg() == 32649
            assert float(ds.res[0]) == pytest.approx(float(doc["grid"]["cellsize_m"]))
            assert ds.nodata == pytest.approx(NODATA)
        meta = json.loads((layer_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["sha256"] == sha256_file(tif), f"{name} meta 哈希与实际文件不符"
        assert meta["layer"] == name


def test_layer_dirs_refuse_overwrite_when_res_mass_present(tmp_path: Path) -> None:
    """目标目录出现 res.mass 时必须拒绝写入（终稿 §10）。"""
    target = tmp_path / "dem_layers"
    (target / "results").mkdir(parents=True)
    (target / "results" / "res.mass").write_text("Time\n", encoding="utf-8")
    with pytest.raises(OverwriteRefused, match="禁止覆盖"):
        guard_no_overwrite(target)


def test_layers_root_contains_no_res_mass() -> None:
    """dem_layers 是派生数据目录，不得含 res.mass（否则会被 g15 误计为 run）。"""
    if not LAYERS_ROOT.is_dir():
        pytest.skip("dem_layers 尚未生成（Phase 2 未运行）")
    assert not list(LAYERS_ROOT.rglob("res.mass"))


def test_sea_anchor_can_be_disabled() -> None:
    """海面基准锚定必须是显式可关闭的选项（不是不可见假设）。"""
    raw = np.array([
        [-5.0, 4.0, 4.0],
        [4.0, 4.0, 4.0],
        [4.0, 4.0, 4.0],
    ])
    valid = np.ones(raw.shape, dtype=bool)
    s1 = np.zeros(raw.shape, dtype=bool)
    s1[0, 0] = True
    sea = np.zeros(raw.shape, dtype=bool)
    sea[0, 0] = True
    on = build_layer_stack(raw, valid=valid, s1_water=s1, sea=sea, cell_area_m2=40000.0)
    off = build_layer_stack(raw, valid=valid, s1_water=s1, sea=sea, cell_area_m2=40000.0,
                            anchor_sea=False)
    assert on.layers["DEM_REPAIRED"][0, 0] == pytest.approx(0.0), "默认应锚定为 0 m"
    assert on.reason[0, 0] == REASON_SEA_MSL_ANCHOR
    assert off.layers["DEM_REPAIRED"][0, 0] == pytest.approx(-5.0), "关闭后应保留负值"
    assert off.reason[0, 0] == REASON_NONE
    assert int(np.count_nonzero(on.reason)) > int(np.count_nonzero(off.reason))


def test_sea_anchor_only_applies_to_negative_cells() -> None:
    """海面锚定只处理负值；正值海面像元不得被改写为 0。"""
    raw = np.array([
        [2.9, 4.0, 4.0],
        [4.0, 4.0, 4.0],
        [4.0, 4.0, 4.0],
    ])
    valid = np.ones(raw.shape, dtype=bool)
    s1 = np.zeros(raw.shape, dtype=bool)
    s1[0, 0] = True
    stack = build_layer_stack(raw, valid=valid, s1_water=s1, sea=s1, cell_area_m2=40000.0)
    assert stack.layers["DEM_REPAIRED"][0, 0] == pytest.approx(2.9), "正值不得被钳为 0"
    assert stack.reason[0, 0] == REASON_NONE


def test_georeferencing_mismatch_is_recorded() -> None:
    """既有 dem.asc 头声明的北缘与数据实际位置不一致时必须登记，不得静默。"""
    doc = _load_catalog()
    geo = doc["georeferencing_check"]
    assert geo["status"] in ("asc_header_shift_confirmed", "unexplained", "reference_missing")
    if geo["status"] == "asc_header_shift_confirmed":
        assert abs(geo["mismatch_m"]) > 0.0
        assert geo["matched_fraction"] > 0.99
        assert geo["note"]


# --------------------------------------------------------------------------- #
# CLI 契约：--dry-run / --run-tag / --out-dir / --log-json（计划 §4）
# --------------------------------------------------------------------------- #
def test_cli_accepts_common_args() -> None:
    """脚本必须实现计划 §4 要求的全部 CLI 参数。"""
    from scripts.stage3.prepare_jiulong_dem_layers import main as layer_main

    # --help 走 SystemExit(0)，借此确认参数已注册
    with pytest.raises(SystemExit) as exc:
        layer_main(["--help"])
    assert exc.value.code == 0


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """--dry-run 不得写任何派生文件。"""
    from scripts.stage3.prepare_jiulong_dem_layers import main as layer_main

    out = tmp_path / "dry"
    log = tmp_path / "dry.json"
    assert layer_main(["--dry-run", "--out-dir", str(out), "--log-json", str(log)]) == 0
    assert not out.exists(), "--dry-run 写出了输出目录"
    assert not log.exists(), "--dry-run 写出了审计 JSON"


def test_res_mass_guard_blocks_write(tmp_path: Path) -> None:
    """目标目录已有 res.mass 时脚本必须中止（终稿 §10）。"""
    from scripts.stage3.prepare_jiulong_dem_layers import main as layer_main

    out = tmp_path / "guarded"
    (out / "results").mkdir(parents=True)
    (out / "results" / "res.mass").write_text("Time\n", encoding="utf-8")
    with pytest.raises(OverwriteRefused, match="禁止覆盖"):
        layer_main(["--out-dir", str(out), "--log-json", str(tmp_path / "x.json")])
    # 且不得写出任何层目录
    assert not (out / "dem_raw").exists()
