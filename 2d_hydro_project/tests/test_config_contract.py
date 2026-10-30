"""阶段0配置契约测试（终稿 十四章 1/4 项在配置层的落地）。

检查：
- base.yaml 约定字段齐全，datum_offset_m 保持 null（禁止预填，终稿 2.1/六）
- 三案例 EPSG 为有效投影 CRS 且单位为米
- 案例 AOI 经度范围落在该 UTM 带经度范围内（自洽性）
- 事件候选必须显式声明 status（不得把未核验当已核验，终稿 3.2）
"""
from pathlib import Path

import pytest
import yaml
from pyproj import CRS

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"

CASES = {
    "jiulong": 32649,
    "ningming": 32648,
    "guigang": 32649,
}


def _load(rel: str) -> dict:
    path = CONFIG_DIR / rel
    assert path.exists(), f"缺少配置文件: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_base_conventions_present() -> None:
    base = _load("base.yaml")
    conv = base["conventions"]
    assert conv["vertical_datum"]["datum_offset_m"] is None, "datum_offset_m 禁止预填常数"
    assert conv["time"]["rule"], "时间基准规则缺失"
    assert conv["raster_alignment"]["rule"], "栅格对齐规则缺失"
    gates = base["quality_gates"]["mass_balance"]
    assert gates["benchmark_target"] == 1.0e-4
    assert gates["event_initial_target"] == 1.0e-3
    assert base["run_management"]["no_overwrite"] is True


@pytest.mark.parametrize("case,expected_epsg", sorted(CASES.items()))
def test_case_crs_valid_projected_metre(case: str, expected_epsg: int) -> None:
    cfg = _load(f"cases/{case}.yaml")
    epsg = cfg["crs"]["epsg"]
    assert epsg == expected_epsg, f"{case} EPSG 与计划核验结果不一致"
    crs = CRS.from_epsg(epsg)
    assert crs.is_projected, f"{case} CRS 必须是投影坐标系（禁止经纬度像元当米宽）"
    assert crs.axis_info[0].unit_name == "metre"


@pytest.mark.parametrize("case", sorted(CASES))
def test_case_aoi_inside_utm_zone(case: str) -> None:
    cfg = _load(f"cases/{case}.yaml")
    epsg = cfg["crs"]["epsg"]
    zone = epsg - 32600
    lon_min, lon_max = -180 + (zone - 1) * 6, -180 + zone * 6
    west, _, east, _ = cfg["aoi"]["provisional_bbox_wgs84"]
    assert lon_min <= west < lon_max, f"{case} AOI 西界越出 UTM {zone}N 经度范围"
    assert lon_min < east <= lon_max, f"{case} AOI 东界越出 UTM {zone}N 经度范围"


@pytest.mark.parametrize("case", sorted(CASES))
def test_case_mandatory_fields(case: str) -> None:
    cfg = _load(f"cases/{case}.yaml")
    assert cfg["case"] == case
    assert cfg["status"].startswith("stage"), "status 必须显式标注阶段"
    assert cfg["vertical_datum"]["datum_offset_m"] is None
    assert cfg["aoi"]["provisional_bbox_wgs84"] and cfg["aoi"]["confirm_pending"]
    for ev in cfg["events"]["candidates"]:
        assert "status" in ev and ev["status"], f"{case} 事件 {ev.get('id')} 缺少 status"
        assert "已核验" not in ev["status"], "未核验事件不得写成『已核验』"


@pytest.mark.parametrize("case", sorted(CASES))
def test_implementation_recorded(case: str) -> None:
    cfg = _load(f"cases/{case}.yaml")
    impl = cfg["implementation"]
    assert impl["mode"] == "degraded-public-data"
    assert isinstance(impl["resolution_m"], int) and impl["resolution_m"] > 0
    assert impl["run_ids"], f"{case} 缺实际 run id"
    # 计划分辨率与实际分辨率必须分开记录，允许不同
    assert cfg["grid"]


def test_model_manifest_gates() -> None:
    manifest = _load("model_manifest.yaml")
    assert manifest["primary_candidate"]["name"] == "LISFLOOD-FP"
    assert manifest["native_code"]["role"] == "仅研究/对照"
    assert len(manifest["benchmarks_required"]) == 7, "终稿十一章 7 项基准必须全部登记"
