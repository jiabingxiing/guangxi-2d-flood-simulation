"""单元测试：南方五省区水情解析器（海南每日/三防阈值、广西应急时间线、反混淆 fail-closed）。"""
from __future__ import annotations

from hydro_refine.monitors.five_provinces_harvester import (
    parse_generic_link_index,
    parse_guangxi_emergency_responses,
    parse_hainan_daily_homepage,
    parse_hainan_sqmap_thresholds,
)


def test_hainan_daily_homepage_parser() -> None:
    sample = (
        "【每日水雨情】：3月8日8时至3月9日8时，全省14个市县降雨，最大降雨出现在五指山，累计雨量为24.7mm。"
        "全省大型水库水位正常，松涛水库186.06米、大广坝水库129.29米。"
        "全省江河水位正常，其中三大江河南渡江龙塘水位7.58米、万泉河加积水位1.05米、昌化江乐东水位133.61米。"
    )
    parsed = parse_hainan_daily_homepage(sample)
    assert parsed["available"] is True
    assert parsed["max_rain_station"] == "五指山"
    assert parsed["max_rain_mm"] == 24.7
    names = {r["station_name"]: r["water_level_m"] for r in parsed["records"]}
    assert names["松涛水库"] == 186.06
    assert names["南渡江龙塘"] == 7.58
    assert names["万泉河加积"] == 1.05
    assert names["昌化江乐东"] == 133.61


def test_hainan_sqmap_thresholds_parser() -> None:
    html = """
    <script>
    marker.attr('title','站名：龙塘(海口)河道站\\r\\n警戒水位：10.5\\r\\n保证水位：12.0');
    marker.attr('title','站名：松涛水库\\r\\n汛限水位：190.5');
    var count_hdcwx=0; var count_hdcjj=1; var count_skcxx=0;
    </script>
    """
    out = parse_hainan_sqmap_thresholds(html)
    assert out["n_stations_cataloged"] == 2
    assert out["exceed_warning_river_count"] == 1
    assert out["all_stations"][0]["warning_level_m"] == 10.5
    assert out["all_stations"][1]["flood_limit_level_m"] == 190.5


def test_guangxi_emergency_responses_parser() -> None:
    html = (
        '<li>2026-08-31 <a href="./t28083332.shtml">自治区水利厅将贵港市的洪水防御三级应急响应调整为四级</a></li>'
        '<li>2026-08-25 <a href="./t28068345.shtml">自治区水利厅针对贵港市将洪水防御四级应急响应提升为三级</a></li>'
    )
    items = parse_guangxi_emergency_responses(html)
    assert len(items) == 2
    assert items[0]["date"] == "2026-08-31"
    assert "贵港市" in items[0]["title"]
    assert parse_generic_link_index(html)
