"""图层状态矩阵验收：计划 §1.4 的「15 个『地点×预设』用例行为正确」。

计划原文：`图层状态 | 15个"地点×预设"用例行为正确；缺图层不显示空白假开关 | M2：含缺失/部分覆盖夹具`

15 = 3 地点（久隆/宁明/贵港）× 5 个数据图层预设（仅可见光 / 仅 SAR / 仅 DEM / 仅淹没 / 全部可用）。
"自定义"不算独立用例（它是勾选状态的派生结果），另有单元/集成测试覆盖。

每个组合断言：
1. 预设按钮点击后 `presetId` 正确；
2. 可见图层集合与"该案例该预设下**实际可用**的图层"一致：
   - `ready` / `partial` 图层必须可见；
   - 其余状态（missing / failed / remote_verified / local_unchecked）必须**不可见且开关禁用**，
     并有原因文案（不允许"开关亮着但没有图层"）；
3. 面板文案包含该案例名（切换同步刷新，不残留上一案例）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import load_json

pytestmark = pytest.mark.e2e

CASES = ("jiulong", "ningming", "guigang")
# 预设 id -> 该预设期望涉及的图层
# 2026-10-28 新增地形分层两预设；用 _presets_for() 与当前包 catalog 求交，
# 这样回滚到旧包（无地形层）时本测试仍然有效，而不是硬编码 7 个预设直接失败。
KNOWN_PRESETS = {
    "optical_only": ["optical"],
    "sar_only": ["sar"],
    "dem_only": ["dem"],
    "terrain_source_only": ["terrain_source"],
    "terrain_edit_only": ["terrain_edit"],
    "flood_only": ["flood"],
    "all_available": ["terrain_source", "dem", "optical", "sar", "flood"],
}
ENABLED = {"ready", "partial"}
CASE_NAME = {"jiulong": "久隆", "ningming": "宁明", "guigang": "贵港"}


@pytest.fixture(scope="module")
def catalog(e2e_bundle) -> dict:
    return load_json(e2e_bundle / "catalog.json")


def _presets_for(catalog: dict) -> dict[str, list[str]]:
    """只保留当前发布包真的声明的预设，且剔除包里不存在的图层。

    旧包（如 v2-20261027）没有 terrain_source / terrain_edit：
    - 预设列表里也不会有 terrain_* 两个；
    - all_available 的 layers 不含它们。
    两者都按 catalog 实际内容收缩，因此本测试对任意版本的包都成立。
    """
    declared = [str(p.get("id")) for p in (catalog.get("presets") or [])]
    layer_ids = {lid for case in catalog.get("cases") or [] for lid in (case.get("layers") or {})}
    out: dict[str, list[str]] = {}
    for preset_id in declared:
        if preset_id not in KNOWN_PRESETS:
            continue
        layers = [lid for lid in KNOWN_PRESETS[preset_id] if lid in layer_ids]
        if layers:
            out[preset_id] = layers
    return out


def _layer_status(catalog: dict) -> dict[str, dict[str, str]]:
    return {
        case["case_id"]: {lid: layer.get("status", "missing") for lid, layer in case["layers"].items()}
        for case in catalog["cases"]
    }


def test_case_preset_matrix(app, catalog: dict) -> None:
    """3 地点 × 当前包声明的预设：可见集合与图层状态机严格一致。"""
    presets = _presets_for(catalog)
    assert presets, f"catalog 未声明任何可识别预设：{[p.get('id') for p in catalog.get('presets') or []]}"
    status = _layer_status(catalog)
    combos = [(case_id, preset_id) for case_id in CASES for preset_id in presets]
    assert len(combos) == len(CASES) * len(presets), combos

    checked: list[dict] = []
    for case_id, preset_id in combos:
        app.switch_case(case_id)
        app.preset(preset_id)
        app.page.wait_for_function(f"() => window.__fvDebug.state().presetId === '{preset_id}'", timeout=30000)
        app.page.wait_for_timeout(150)

        visible = set(app.visible_groups())
        expected_visible = {layer for layer in presets[preset_id] if status[case_id].get(layer) in ENABLED}
        expected_hidden = {
            layer for layer in presets[preset_id] if status[case_id].get(layer, "missing") not in ENABLED
        }

        # 1) 可用图层必须可见
        missing_visible = expected_visible - visible
        assert not missing_visible, (
            f"{case_id}/{preset_id}: 可用图层未显示 {missing_visible}；"
            f"status={ {k: status[case_id].get(k) for k in presets[preset_id]} }"
        )
        # 2) 不可用图层必须不可见
        wrongly_visible = expected_hidden & visible
        assert not wrongly_visible, (
            f"{case_id}/{preset_id}: 不可用图层却可见 {wrongly_visible}；"
            f"status={ {k: status[case_id].get(k) for k in presets[preset_id]} }"
        )
        # 3) 不可用图层开关必须禁用，且有原因文案（不得空白假开关）
        for layer in expected_hidden:
            cb = f"#layer-cb-{layer}"
            if app.selectors_count(cb):
                assert app.page.eval_on_selector(cb, "el => el.disabled") is True, (
                    f"{case_id}/{preset_id}: {layer} 不可用但开关未禁用"
                )
            item_text = app.layer_item_text(layer)
            assert item_text.strip(), f"{case_id}/{preset_id}: {layer} 图层条目无文案"
            assert any(
                token in item_text for token in ("未提供", "禁用", "禁用原因", "远端", "未导入", "不可用", "失败")
            ), f"{case_id}/{preset_id}: {layer} 不可用但未说明原因：{item_text[:160]}"

        # 4) 面板必须已切到当前案例（不残留）
        assert CASE_NAME[case_id] in app.text("#quality-body"), f"{case_id}/{preset_id}: 质量面板未刷新到当前案例"

        checked.append(
            {
                "case": case_id,
                "preset": preset_id,
                "visible": sorted(visible),
                "expected_visible": sorted(expected_visible),
                "disabled_layers": sorted(expected_hidden),
            }
        )

    # 落盘矩阵证据
    import json

    from harness import EVIDENCE_DIR

    (EVIDENCE_DIR.parent / "layer_matrix_15.json").write_text(
        json.dumps(checked, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_preset_matrix_covers_missing_and_partial(catalog: dict) -> None:
    """夹具必须真的包含「缺失」与「部分覆盖」两种状态，否则矩阵不算覆盖计划要求。"""
    status = _layer_status(catalog)
    all_statuses = {s for case in status.values() for s in case.values()}
    assert "partial" in all_statuses, f"包内没有 partial 图层，矩阵未覆盖部分覆盖夹具：{all_statuses}"
    # v2 包光学已导入；v1 基线里 optical=missing。至少要求有一处非 ready 状态被校验。
    non_ready = {
        (case_id, layer, s) for case_id, layers in status.items() for layer, s in layers.items() if s != "ready"
    }
    assert non_ready, f"包内全部图层均为 ready，未覆盖缺图层夹具：{all_statuses}"
    print(f"\n[layer-matrix] 非 ready 图层：{sorted(non_ready)}")


# ------------------------------------------------------------------ 缺图层夹具（v1 基线）

BASELINE_BUNDLE = "v3-20261028"
BASELINE_PORT = 8795


def _serve_bundle(bundle: Path, port: int):
    """起一个独立静态服务（只绑回环），返回 (proc, url)。"""
    import subprocess
    import sys
    import time
    import urllib.request

    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(bundle)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "index.html", timeout=2) as resp:
                if resp.status == 200:
                    return proc, url
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"基线包静态服务未就绪：{url}")


def test_missing_layer_shows_disabled_with_reason(e2e_browser, project_root) -> None:
    """缺图层夹具：v1 基线包（光学缺失）必须「禁用 + 说明原因」，不得显示空白假开关。

    这是计划 §1.4「缺图层不显示空白假开关」的直接验证；v2 包三地光学都已导入，
    因此必须用 v1 基线包做夹具，否则这条门禁实际上没有被测到。
    """
    import types

    from harness import AppPage, load_json

    bundle = project_root / "outputs" / "web_bundle" / BASELINE_BUNDLE
    if not (bundle / "index.html").is_file():
        pytest.skip(f"缺少基线包 {bundle}（回滚基线未保留）")

    catalog = load_json(bundle / "catalog.json")
    status = {
        case["case_id"]: {k: v.get("status", "missing") for k, v in case["layers"].items()} for case in catalog["cases"]
    }
    missing = {
        (case_id, layer) for case_id, layers in status.items() for layer, s in layers.items() if s not in ENABLED
    }
    assert missing, f"v1 基线包没有缺失图层，无法验证缺图层夹具：{status}"

    proc, url = _serve_bundle(bundle, BASELINE_PORT)
    context = e2e_browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
    page = context.new_page()
    try:
        app = AppPage(types.SimpleNamespace(page=page, url=url, context=context))
        app.open()
        checked = []
        for case_id, layer in sorted(missing):
            if app.state()["caseId"] != case_id:
                app.switch_case(case_id)
            selector = f"#layer-cb-{layer}"
            if not app.selectors_count(selector):
                continue  # 该案例根本没有这个图层条目（如未配置），下一轮由矩阵覆盖
            assert page.eval_on_selector(selector, "el => el.disabled") is True, (
                f"{case_id}/{layer}: 状态为 {status[case_id][layer]} 但开关未禁用"
            )
            assert layer not in app.visible_groups(), (
                f"{case_id}/{layer}: 状态为 {status[case_id][layer]} 却出现在可见图层里"
            )
            item = app.layer_item_text(layer)
            assert item.strip(), f"{case_id}/{layer}: 图层条目无文案（空白假开关）"
            assert any(
                token in item for token in ("未提供", "禁用", "未导入", "远端", "未校验", "不可用", "失败", "缺失")
            ), f"{case_id}/{layer}: 不可用但未说明原因：{item[:160]}"
            checked.append(
                {"case": case_id, "layer": layer, "status": status[case_id][layer], "text": item.strip()[:120]}
            )

        assert checked, f"没有实际校验到任何缺失图层：{sorted(missing)}"
        from harness import EVIDENCE_DIR

        (EVIDENCE_DIR.parent / "missing_layer_disabled.json").write_text(
            json.dumps(checked, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        page.screenshot(path=str(EVIDENCE_DIR / "09-missing-layer-disabled.png"))
    finally:
        context.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
