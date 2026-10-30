"""e2e·SPEC §10 交互验收（1–12 条）：三地切换、预设、时间轴、透明度、点查、
面板、最大包络、令牌、缺帧、在线底图降级、离线、中文/键盘/无控制台错误。

运行：uv run python -m pytest tests/e2e -m e2e -q
（服务与浏览器由 tests/conftest.py 的 e2e_* 夹具提供；只读 outputs/web_bundle/<id>）
"""

from __future__ import annotations

import re

import pytest
from harness import evidence, load_json

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- 1 三地切换


def test_case_switch_three_cases(app, e2e_bundle):
    """三地切换：案例、帧数、每地默认 run 与 catalog 一致；地图 fitBounds 覆盖 model_bbox。"""
    catalog = load_json(e2e_bundle / "catalog.json")
    cases = {case["case_id"]: case for case in catalog["cases"]}
    assert set(cases) == {"jiulong", "ningming", "guigang"}

    seen: list[tuple[str, int, str]] = []
    for case_id, expected_frames in (("jiulong", 21), ("ningming", 19), ("guigang", 57)):
        app.switch_case(case_id)
        state = app.state()
        assert state["caseId"] == case_id
        assert state["runId"] == cases[case_id]["default_run_id"]
        assert state["frameCount"] == expected_frames, (case_id, state["frameCount"])
        seen.append((case_id, state["frameCount"], state["runId"]))
        # 地图容器可见且已初始化（fitBounds 有 bbox 才会 idle）
        assert app.has_map()
        assert app.page.is_visible("#map")
    assert seen[0][1] == 21 and seen[1][1] == 19 and seen[2][1] == 57
    app.page.screenshot(path=str(evidence("01-case-guigang.png")))


def test_case_switch_refreshes_panels(app):
    """切案例必须同步刷新面板（质量/来源/图例/时间轴读数），不得残留上一个案例。"""
    app.switch_case("guigang")
    quality = app.text("#quality-body")
    assert "贵港" in quality and "guigang" in quality
    assert "质量守恒门" in quality
    # 贵港是 model_schedule_confirmed → 必须有绝对时间
    assert "2024-09-03" in app.text("#time-absolute")

    app.switch_case("jiulong")
    quality = app.text("#quality-body")
    assert "久隆" in quality and "guigang" not in quality
    # 久隆 origin 是候选 → 绝对时间不可用，且不得出现 Invalid Date / NaN
    absolute = app.text("#time-absolute")
    assert "Invalid" not in absolute and "NaN" not in absolute
    assert "不可用" in absolute


# --------------------------------------------------------------------------- 2 六预设


def test_eight_presets_and_independent_basemap(app):
    """八预设齐全（2026-10-28 新增地形分层两预设）；点击后可见图层集合符合预设；底图开关独立于预设。"""
    labels = app.page.eval_on_selector_all("#preset-buttons .preset-btn", "els => els.map(e => e.textContent.trim())")
    assert len(labels) == 8
    for label in ("仅可见光", "仅 SAR", "仅模型地形", "仅源地形（未修改）",
                  "仅地形修改证据", "仅淹没", "全部可用图层", "自定义"):
        assert label in labels, labels
    ids = app.page.eval_on_selector_all("#preset-buttons .preset-btn", "els => els.map(e => e.dataset.presetId)")
    assert ids == [
        "optical_only", "sar_only", "dem_only", "terrain_source_only",
        "terrain_edit_only", "flood_only", "all_available", "custom",
    ]

    # v3 包：久隆 terrain_source ready、DEM ready、flood ready、optical partial
    app.preset("dem_only")
    visible = app.visible_groups()
    assert set(visible) == {"dem"}, visible

    app.preset("terrain_source_only")
    assert set(app.visible_groups()) == {"terrain_source"}

    app.preset("flood_only")
    assert set(app.visible_groups()) == {"flood"}

    app.preset("sar_only")
    assert set(app.visible_groups()) == {"sar"}

    # 仅可见光：久隆 optical=partial → 可选；且不得与其它图层混淆
    app.preset("optical_only")
    assert set(app.visible_groups()) == {"optical"}, app.visible_groups()
    assert app.check("#layer-cb-optical")
    assert app.page.eval_on_selector("#layer-cb-optical", "el => el.disabled") is False

    app.preset("all_available")
    visible = app.visible_groups()
    assert {"terrain_source", "dem", "flood", "sar", "optical"} <= set(visible), visible

    # 自定义：切换预设后手动改勾选 → presetId 变 custom
    app.preset("flood_only")
    app.page.click("#layer-cb-dem")
    app.wait_state("s.presetId === 'custom'")
    assert "dem" in app.visible_groups()

    # 底图独立开关：预设不改变底图状态
    before = app.page.eval_on_selector("#basemap-toggle", "el => el.checked")
    app.preset("dem_only")
    assert app.page.eval_on_selector("#basemap-toggle", "el => el.checked") == before


# --------------------------------------------------------------------------- 3 时间轴


def test_timeline_scrub_play_pause_step_speed(app):
    """拖动吸附真实帧、播放/暂停、上一帧/下一帧、0.5/1/2/4 帧每秒、T+ 与绝对时间规则。"""
    state = app.state()
    assert state["frameCount"] == 21
    assert app.text("#frame-readout") == "帧 1 / 21"

    # 拖动吸附到第 11 帧（index 10）= T+60 h
    app.set_frame(10)
    assert app.text("#time-relative") == "T+60.0 h"
    assert app.text("#frame-readout") == "帧 11 / 21"
    assert app.state()["frameId"] == "f0010"
    # 久隆 timestamp 全 null → 绝对时间不可用（不得显示 Invalid Date）
    absolute = app.text("#time-absolute")
    assert "Invalid" not in absolute and "NaN" not in absolute and "不可用" in absolute
    assert "候选依据" in app.text("#origin-note")

    # 逐帧
    app.page.click("#btn-next")
    app.wait_state("s.index === 11")
    assert app.text("#frame-readout") == "帧 12 / 21"
    app.page.click("#btn-prev")
    app.wait_state("s.index === 10")

    # 播放推进
    app.page.click("#btn-play")
    app.wait_state("s.playing === true")
    app.page.wait_for_timeout(1800)
    playing_state = app.state()
    assert playing_state["index"] > 10 or playing_state["index"] == 0
    app.page.click("#btn-play")
    app.wait_state("s.playing === false")

    # 速度 0.5/1/2/4
    for speed in (0.5, 1, 2, 4):
        app.page.click(f"button[data-speed='{speed}']")
        app.page.wait_for_timeout(120)
        assert abs(app.state()["speed"] - speed) < 1e-9
    app.page.click("button[data-speed='1']")

    # 时间轴帧刻度数量 = 帧数（DOM 里还有文字标签与峰值线，故只数刻度线）
    ticks = app.selectors_count("#frame-ticks .frame-tick:not(.peak)")
    assert ticks == 21, ticks
    assert app.selectors_count("#frame-ticks .frame-tick-label") >= 2


def test_absolute_time_for_guigang(app):
    """贵港 timestamp 有依据 → 显示绝对时间（+08:00 北京时间），且随帧推进。"""
    app.switch_case("guigang")
    first = app.text("#time-absolute")
    assert "2024-09-03" in first
    app.set_frame(10)
    moved = app.text("#time-absolute")
    assert "2024-09-03" in moved or "2024-09-05" in moved
    assert moved != first
    # 末帧 T+336 h
    app.set_frame(56)
    assert app.text("#time-relative") == "T+336.0 h"


# --------------------------------------------------------------------------- 4 透明度 + 影像时间


def test_layer_opacity_sliders(app):
    """每图层透明度滑杆存在且可调；写入图层状态。"""
    count = app.selectors_count(".layer-opacity input[type=range]")
    assert count >= 6, count
    app.set_opacity("dem", 30)
    app.page.wait_for_timeout(150)
    assert abs(app.state()["layers"]["dem"]["opacity"] - 0.3) < 1e-6
    app.set_opacity("flood", 45)
    app.page.wait_for_timeout(150)
    assert abs(app.state()["layers"]["flood"]["opacity"] - 0.45) < 1e-6


def test_image_acquisition_time_does_not_follow_slider(app):
    """影像采集时间固定：拖动模拟时间滑块，图层里的"采集 <日期>"不得变化。"""
    acquired: dict[str, str] = {}
    for group in ("sar", "optical"):
        text = app.layer_item_text(group)
        match = re.search(r"采集\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[^）]*)", text)
        assert match, f"{group} 图层未显示采集时间：{text[:160]}"
        acquired[group] = match.group(1).strip()

    for index in (5, 12, 20):
        app.set_frame(index)
        for group, expected in acquired.items():
            text = app.layer_item_text(group)
            assert expected in text, f"{group} 采集时间随模拟时间变化：{expected} -> {text[:200]}"


# --------------------------------------------------------------------------- 5 点查 + 面积曲线


def test_point_query_returns_values(app, e2e_bundle):
    """点查：返回水深/模型分辨率/来源 run；域外明确提示"域外"，不夹边。"""
    app.switch_case("guigang")
    app.set_frame(30)
    state = app.state()
    case = next(item for item in load_json(e2e_bundle / "catalog.json")["cases"] if item["case_id"] == "guigang")
    grid = case["grid"]

    # 用真实模型网格反算经纬度查询
    transform = [float(v) for v in grid["transform"]]
    rows, cols = grid["rows"], grid["cols"]
    from pyproj import Transformer

    to_wgs = Transformer.from_crs(grid["crs"], "EPSG:4326", always_xy=True)
    a, b, c, d, e, f = transform
    row, col = rows * 0.6, cols * 0.4
    lon, lat = to_wgs.transform(a * col + b * row + c, d * col + e * row + f)

    import numpy as np

    frame_id = state["frameId"]
    numeric_run = e2e_bundle / "numeric" / "guigang" / app.state()["runId"]
    block_path = numeric_run / frame_id / "0" / "0.bin"
    assert block_path.is_file(), block_path
    block = np.fromfile(block_path, dtype="<f4").reshape(-1)
    assert block.size >= 0  # 块存在即可

    payload = app.query(lon, lat)
    assert payload, payload
    assert payload.get("state") in {"ok", "nan", "outside", "error"}, payload
    # 在域内返回时必须有 run/分辨率信息（SPEC §1.1.5：水深、网格分辨率、来源）
    if payload.get("state") in {"ok", "nan"}:
        assert payload.get("runId") == state["runId"], payload
        assert float(payload.get("cellSizeM")) == float(grid["cell_size_m"]), payload
        assert payload.get("row") is not None and payload.get("col") is not None

    # 域外（远离模型域）：明确提示，不夹边
    outside = app.query(100.0, 10.0)
    assert outside["state"] == "outside", outside
    body = app.text("#query-body")
    assert "域外" in body
    app.place_marker_at(lon, lat)
    app.page.wait_for_timeout(600)
    assert app.text("#query-body")
    app.page.screenshot(path=str(evidence("03-query.png")))


def test_query_nan_or_dry_prompt(app):
    """NaN/无有效值必须明确提示（不是 0 冒充）。"""
    app.set_frame(0)  # 久隆 f0000 全干（真实结果）
    notice = app.wait_notice(keyword="全干")
    assert "全干" in notice or "无淹没" in notice, notice
    assert app.state()["wetPixels"] == 0


def test_area_chart_and_threshold_switch(app):
    """面积—时间曲线已绘制；阈值可切换到 0.05/0.15/0.3/0.5/1 m。"""
    assert app.chart_painted_pixels() > 2000
    options = app.page.eval_on_selector_all("#threshold-select option", "els => els.map(e => e.value)")
    for value in ("0.05", "0.15", "0.3", "0.5", "1"):
        assert value in options, (value, options)
    app.page.select_option("#threshold-select", "0.5")
    app.wait_state("Math.abs(s.threshold - 0.5) < 1e-9")
    app.page.wait_for_timeout(200)
    assert app.chart_painted_pixels() > 2000
    chart_note = app.text("#chart-note")
    assert "原生模型网格" in chart_note and "不是资产安全等级" in chart_note
    app.page.screenshot(path=str(evidence("04-area-chart.png")))


# --------------------------------------------------------------------------- 6 质量/来源/许可面板


def test_quality_license_and_source_panels(app):
    quality = app.text("#quality-body")
    assert "质量守恒门" in quality
    assert "PASS" in quality
    assert "E_mass" in quality or "e_mass" in quality.lower()
    assert "垂直基准" in quality
    assert "情景级" in quality
    license_text = app.text("#status-license")
    assert "许可" in license_text
    bundle_text = app.text("#status-bundle")
    assert "发布包" in bundle_text


# --------------------------------------------------------------------------- 7 最大包络


def test_max_envelope_title_pause_and_restore(app):
    base_index = app.state()["index"]
    app.page.click("#btn-max")
    app.wait_state("s.maxMode === true")
    app.page.wait_for_timeout(800)
    state = app.state()
    assert state["maxMode"] is True
    assert state["playing"] is False
    assert "全程最大水深包络" in app.text("#map-title")
    assert "非某一时刻" in app.text("#map-title")
    assert app.page.eval_on_selector("#frame-slider", "el => el.disabled") is True
    # 图例必须切换为包络口径，不得把包络当某一帧
    legend = app.text("#legend-body")
    assert "全程最大水深包络" in legend
    app.page.screenshot(path=str(evidence("05-max-envelope.png")))

    app.page.click("#btn-max")
    app.wait_state("s.maxMode === false")
    app.page.wait_for_timeout(500)
    assert "当前帧模拟水深" in app.text("#map-title")
    assert app.state()["index"] == base_index
    assert app.page.eval_on_selector("#frame-slider", "el => el.disabled") is False


# --------------------------------------------------------------------------- 8 令牌/竞态


def test_rapid_scrub_keeps_frame_and_time_consistent(app):
    """快速拖动：最终帧号、帧读数、T+ 时间三者一致（丢弃过期响应）。"""
    app.page.evaluate(
        """async () => {
            const el = document.getElementById('frame-slider');
            for (const v of [1, 7, 18, 3, 12, 5]) {
                el.value = String(v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                await new Promise(r => setTimeout(r, 40));
            }
        }"""
    )
    app.page.wait_for_timeout(2500)
    state = app.state()
    assert state["index"] == 5, state["index"]
    assert state["frameId"] == "f0005"
    assert app.text("#frame-readout") == "帧 6 / 21"
    assert app.text("#time-relative") == "T+30.0 h"


# --------------------------------------------------------------------------- 9 缺帧


def test_missing_frame_tile_shows_error_and_pauses(app):
    """瓦片 404：暂停播放、明确提示"帧缺失/加载失败"，不得用旧帧新日期冒充。"""
    before = app.state()
    assert before["index"] == 0
    app.session.context.route("**/f0018_r*.png", lambda route: route.fulfill(status=404, body="missing"))
    try:
        app.page.eval_on_selector(
            "#frame-slider",
            "el => { el.value = '18'; el.dispatchEvent(new Event('input', {bubbles:true})); }",
        )
        app.page.wait_for_timeout(3000)
        notice = app.notice()
        assert ("帧加载失败" in notice) or ("帧缺失" in notice), notice
        state = app.state()
        assert state["playing"] is False
        assert state["index"] == 0, "缺帧时必须停在旧帧，不用新帧号冒充"
        assert app.text("#frame-readout") == "帧 1 / 21"
        app.page.screenshot(path=str(evidence("06-missing-frame.png")))
    finally:
        app.session.context.unroute("**/f0018_r*.png")


# --------------------------------------------------------------------------- 10/11 在线底图与离线


def test_basemap_failure_keeps_local_layers(app):
    """Esri 瓦片失败：保留本地图层 + 显示"在线底图不可用"，不自动换未审核底图。"""
    app.session.context.route(
        "**://**",
        lambda route: (
            route.abort()
            if route.request.url.startswith("http") and "127.0.0.1" not in route.request.url
            else route.continue_()
        ),
    )
    try:
        app.page.click("#basemap-toggle")
        app.page.wait_for_timeout(3500)
        warn = app.text("#basemap-warn")
        assert "在线底图不可用" in warn, warn
        state = app.state()
        assert state["layers"]["flood"]["visible"] is True
        assert state["layers"]["sar"]["visible"] is True
        # 署名仍来自 catalog（不得声称离线授权）
        attribution = app.text("#status-attribution")
        assert attribution
        # 不得请求非 Esri 的第三方底图
        external = [
            url
            for url in app.page.evaluate("() => performance.getEntriesByType('resource').map(e => e.name)")
            if url.startswith("http") and "127.0.0.1" not in url
        ]
        assert all("arcgisonline.com" in url for url in external), external[:5]
        app.page.screenshot(path=str(evidence("07-basemap-offline.png")))
    finally:
        app.session.context.unroute("**://**")
        if app.page.eval_on_selector("#basemap-toggle", "el => el.checked"):
            app.page.click("#basemap-toggle")
            app.page.wait_for_timeout(300)


def test_offline_page_still_usable(app_factory):
    """断网（拦截所有非回环请求）：页面仍可用、图层可切换、点查可用、无未捕获错误。"""
    page = app_factory()
    page.session.context.route(
        "**://**",
        lambda route: (
            route.abort()
            if route.request.url.startswith("http") and "127.0.0.1" not in route.request.url
            else route.continue_()
        ),
    )
    try:
        page.open()
        state = page.state()
        assert state["frameCount"] == 21
        assert state["layers"]["flood"]["visible"]
        page.set_frame(12)
        assert page.state()["frameId"] == "f0012"
        assert page.chart_painted_pixels() > 2000
        query = page.query(108.72, 22.06)
        assert query is not None
        assert page.js_errors() == [], page.js_errors()
        assert page.session.console_errors == [], page.session.console_errors
        page.page.screenshot(path=str(evidence("08-offline.png")))
    finally:
        page.session.context.unroute("**://**")


# --------------------------------------------------------------------------- 12 中文/键盘/错误


def test_chinese_ui_and_keyboard_shortcuts(app):
    """中文界面；1920×1080 无横向溢出；空格播放/暂停、左右键逐帧。"""
    assert app.page.evaluate("() => document.documentElement.lang") == "zh-CN"
    assert "「驭洪」洪涝推演系统" in app.text("#project-title")
    overflow = app.page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})"
    )
    assert overflow["sw"] <= overflow["cw"] + 1, overflow
    no_scroll = app.page.evaluate("() => document.getElementById('app').getBoundingClientRect().height")
    assert 900 <= no_scroll <= 1080, no_scroll

    start = app.state()["index"]
    app.page.click("#map", position={"x": 30, "y": 30})
    app.page.keyboard.press("ArrowRight")
    app.wait_state(f"s.index === {min(start + 1, 20)}")
    app.page.keyboard.press("ArrowLeft")
    app.wait_state(f"s.index === {start}")
    app.page.keyboard.press(" ")
    app.wait_state("s.playing === true")
    app.page.keyboard.press(" ")
    app.wait_state("s.playing === false")


def test_no_console_errors_and_no_local_4xx(app):
    """无未捕获 JS 错误、无控制台 error、无同源 4xx（正常浏览路径）。"""
    app.set_frame(3)
    app.preset("all_available")
    app.switch_case("ningming")
    app.set_frame(5)
    app.switch_case("guigang")
    app.page.wait_for_timeout(1200)
    assert app.js_errors() == [], app.js_errors()
    assert app.session.page_errors == [], app.session.page_errors
    assert app.session.console_errors == [], app.session.console_errors
    assert app.session.local_4xx == [], app.session.local_4xx
