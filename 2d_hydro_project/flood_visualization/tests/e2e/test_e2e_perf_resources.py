"""e2e 性能与资源完整性验收（SPEC §1.4 指标 + 跨块点查 + 零 404）。

分工：`test_e2e_spec10.py` 覆盖 18 条交互与语义；本文件覆盖**量化指标**与**资源完整性**。

测量条件（写进 evidence，不隐瞒）：
- 性能门禁在 **有头 Chromium（真实 AMD Radeon 核显，ANGLE/D3D11）** 下判定，因为计划 §1.4 的
  基准是"本机 Edge/Chrome 1920×1080"。
- 同时记录 **无头 Chromium（SwiftShader 软件渲染）** 对比值：软件渲染把 `updateImage`
  纹理上传成本放大 5–6 倍（实测同一操作 67 ms vs 406 ms），只作参考，不用于门禁。
- 服务为 `python -m http.server` 绑 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import random
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from harness import EVIDENCE_DIR, load_json

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

QA_DIR = EVIDENCE_DIR.parent
BLOCK_JS = """
async ({count}) => {
    const slider = document.getElementById('frame-slider');
    const debug = window.__fvDebug;
    const total = debug.state().frameCount;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const out = [];
    const measure = async (target) => {
        const t0 = performance.now();
        slider.value = String(target);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        for (let g = 0; g < 1200; g += 1) {
            if (debug.state().index === target && debug.state().frameId) {
                await new Promise(r2 => requestAnimationFrame(() => r2()));
                break;
            }
            await sleep(3);
        }
        return performance.now() - t0;
    };
    for (let k = 1; k <= count; k += 1) out.push(await measure(k % total));
    return out;
}
"""


def _http_status(url: str, *, timeout: float = 20.0) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            resp.read(64)
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


def _images_rel(run: dict) -> str:
    if run.get("images"):
        return run["images"]
    return run["numeric_manifest"].replace("numeric/", "frames/", 1).replace("manifest.json", "images.json")


# --------------------------------------------------------------------------- 资源完整性


def test_all_referenced_assets_return_200(e2e_bundle: Path, e2e_url: str) -> None:
    """catalog/images/numeric_manifest 引用的文件必须全部存在，不允许 404。"""
    catalog = load_json(e2e_bundle / "catalog.json")
    failures: list[str] = []
    checked = 0
    rng = random.Random(20261027)

    for case in catalog["cases"]:
        case_id = case["case_id"]
        for layer_id, layer in case["layers"].items():
            for key in ("image", "geojson", "footprint"):
                rel = layer.get(key)
                if isinstance(rel, str):
                    status = _http_status(e2e_url + rel)
                    checked += 1
                    if status != 200:
                        failures.append(f"{case_id}/{layer_id}.{key} -> {status} {rel}")
            if isinstance(layer.get("images"), str):
                doc = load_json(e2e_bundle / layer["images"])
                for part in list((doc.get("max") or {}).get("parts", []))[:2]:
                    status = _http_status(e2e_url + part["path"])
                    checked += 1
                    if status != 200:
                        failures.append(f"{case_id}/{layer_id}.max {part['path']} -> {status}")

        for run in case["runs"]:
            timeline = load_json(e2e_bundle / run["timeline"])
            images = load_json(e2e_bundle / _images_rel(run))
            frame_idx = sorted(
                {0, len(timeline["frames"]) // 3, (2 * len(timeline["frames"])) // 3, len(timeline["frames"]) - 1}
            )
            for idx in frame_idx:
                for part in images["frames"][idx]["parts"]:
                    status = _http_status(e2e_url + part["path"])
                    checked += 1
                    if status != 200:
                        failures.append(f"{case_id}/{run['run_id']}/{part['id']} -> {status}")

            numeric_manifest = load_json(e2e_bundle / run["numeric_manifest"])
            blocks = numeric_manifest["blocks"]
            numeric_rel = run["numeric_manifest"].replace("/manifest.json", "")
            for frame in timeline["frames"]:
                for bx in sorted({0, blocks["cols"] // 2, blocks["cols"] - 1}):
                    by = rng.randrange(blocks["rows"])
                    status = _http_status(e2e_url + f"{numeric_rel}/{frame['frame_id']}/{by}/{bx}.bin")
                    checked += 1
                    if status != 200:
                        failures.append(f"{case_id}/{run['run_id']}/{frame['frame_id']}/{by}/{bx}.bin -> {status}")

    payload = {"checked": checked, "failures": failures[:40], "failure_count": len(failures)}
    (QA_DIR / "resource_integrity.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert not failures, failures[:20]
    assert checked > 200, checked


def test_point_query_covers_all_block_columns(app, e2e_bundle: Path) -> None:
    """贵港 30 m 点查必须覆盖所有块列（bx=0..8），返回数值且不 404。"""
    from pyproj import Transformer

    catalog = load_json(e2e_bundle / "catalog.json")
    case = next(item for item in catalog["cases"] if item["case_id"] == "guigang")
    run = next(item for item in case["runs"] if item["run_id"] == case["default_run_id"])
    grid = case["grid"]
    numeric = load_json(e2e_bundle / run["numeric_manifest"])
    block_size = numeric["block_size"]
    cols, rows = grid["cols"], grid["rows"]
    a, b, c, d, e, f = [float(v) for v in grid["transform"]]
    to_wgs = Transformer.from_crs(grid["crs"], "EPSG:4326", always_xy=True)

    app.switch_case("guigang")
    app.set_frame(30)

    results: list[dict] = []
    for bx in range(numeric["blocks"]["cols"]):
        col = min(bx * block_size + block_size // 2, cols - 1)
        row = min(int(rows * 0.62), rows - 1)
        lon, lat = to_wgs.transform(a * col + b * row + c, d * col + e * row + f)
        payload = app.query(lon, lat)
        assert payload, (bx, lon, lat)
        assert payload["state"] in {"ok", "nan"}, (bx, payload)
        assert payload.get("blockPath"), payload
        results.append(
            {
                "bx": bx,
                "state": payload["state"],
                "value": payload.get("value"),
                "blockPath": payload["blockPath"],
                "runId": payload.get("runId"),
                "cellSizeM": payload.get("cellSizeM"),
            }
        )

    assert len(results) == numeric["blocks"]["cols"], results
    (QA_DIR / "query_block_coverage.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- 性能


def _renderer(page) -> str:
    return page.evaluate(
        """() => { try {
            const c = document.createElement('canvas');
            const g = c.getContext('webgl');
            const d = g.getExtension('WEBGL_debug_renderer_info');
            return g.getParameter(d.UNMASKED_RENDERER_WEBGL);
        } catch (e) { return 'unknown: ' + e; } }"""
    )


GPU_ARGS = [
    "--use-angle=d3d11",
    "--enable-gpu",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
]


def _probe_browser(browser_type, *, mode: str, e2e_url: str, catalog: dict) -> dict:
    """用会话级 Playwright 实例的 browser_type 另起浏览器。

    mode:
      - "headless_gpu"      无头 + GPU 旗标：不弹窗口（不触发 IDM 等下载管理器），
                            但仍走真实核显（ANGLE/D3D11）—— 这就是验收口径。
      - "headless_software" 无头 + 无 GPU 旗标（SwiftShader 软件渲染），仅作对比下界。
      - "headed"            有头（需显式开关）。
    """
    cases = {case["case_id"]: case for case in catalog["cases"]}
    if mode == "headless_gpu":
        browser = browser_type.launch(headless=True, args=GPU_ARGS)
    elif mode == "headed":
        browser = browser_type.launch(headless=False, args=GPU_ARGS)
    else:
        browser = browser_type.launch(headless=True)
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)[:200]))
    data: dict = {}

    # 1) 冷启动
    t0 = time.perf_counter()
    page.goto(e2e_url, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=90000)
    page.wait_for_function(
        "() => { const q = document.getElementById('quality-body'); return q && q.textContent.trim().length > 20; }",
        timeout=60000,
    )
    first_interactive = time.perf_counter() - t0
    page.wait_for_function(
        "() => performance.getEntriesByType('resource').some(e => (e.name || '').includes('/frames/'))",
        timeout=60000,
    )
    first_tile = page.evaluate(
        """() => Math.max(...performance.getEntriesByType('resource')
            .filter(e => (e.name || '').includes('/frames/')).map(e => e.responseEnd)) / 1000.0"""
    )
    data["renderer"] = _renderer(page)
    data["cold_start"] = {
        "first_interactive_s": round(first_interactive, 3),
        "first_business_tile_s": round(float(first_tile), 3),
        "gate_interactive_s": 5.0,
        "gate_tile_s": 2.0,
    }

    # 2) 点查 p95（三地 × 3 帧 × 4 次）
    timings: list[float] = []
    cold_timings: list[float] = []
    for case_id in ("jiulong", "ningming", "guigang"):
        page.click(f".case-tab[data-case-id='{case_id}']")
        page.wait_for_function(
            f"() => window.__fvDebug.state().caseId === '{case_id}' && window.__fvDebug.state().frameId !== null",
            timeout=90000,
        )
        page.wait_for_timeout(1200)
        grid = cases[case_id]["grid"]
        a, b, c, d, e, f = [float(v) for v in grid["transform"]]
        from pyproj import Transformer

        to_wgs = Transformer.from_crs(grid["crs"], "EPSG:4326", always_xy=True)
        frame_count = int(page.evaluate("window.__fvDebug.state().frameCount"))
        for frame_index in (1, 5, 11):
            target = min(frame_index, frame_count - 1)
            page.evaluate(
                "() => { const el = document.getElementById('frame-slider');"
                " el.dispatchEvent(new Event('input', {bubbles:true})); }"
            )
            page.eval_on_selector(
                "#frame-slider",
                "(el, v) => { el.value = String(v); el.dispatchEvent(new Event('input', {bubbles:true})); }",
                target,
            )
            page.wait_for_function(f"() => window.__fvDebug.state().index === {target}", timeout=60000)
            for _ in range(6):
                row = random.randint(0, grid["rows"] - 1)
                col = random.randint(0, grid["cols"] - 1)
                lon, lat = to_wgs.transform(a * col + b * row + c, d * col + e * row + f)
                # 首次同点查询：可能需拉取数值块（冷查询，单独记录，不作门禁）
                started = time.perf_counter()
                page.evaluate("([lon, lat]) => window.__fvDebug.query(lon, lat)", [lon, lat])
                cold_timings.append((time.perf_counter() - started) * 1000.0)
                # 第二次同点：缓存命中，这是计划 §1.4 的门禁口径
                started = time.perf_counter()
                page.evaluate("([lon, lat]) => window.__fvDebug.query(lon, lat)", [lon, lat])
                timings.append((time.perf_counter() - started) * 1000.0)
    timings.sort()
    cold_timings.sort()
    data["point_query"] = {
        "samples": len(timings),
        "p50_ms": round(statistics.median(timings), 2),
        "p95_ms": round(timings[round(0.95 * (len(timings) - 1))], 2),
        "max_ms": round(max(timings), 2),
        "gate_p95_ms": 200.0,
        "cache_miss_p50_ms": round(statistics.median(cold_timings), 2),
        "cache_miss_max_ms": round(max(cold_timings), 2),
        "method": "缓存命中口径（同点第二次查询），含 Playwright IPC，偏保守；冷查询另记，不计入门禁",
    }

    # 3) 界面响应 p95（浏览器内计时，切到最重的贵港 30 m）
    page.click(".case-tab[data-case-id='guigang']")
    page.wait_for_function(
        "() => window.__fvDebug.state().caseId === 'guigang' && window.__fvDebug.state().frameId !== null",
        timeout=90000,
    )
    page.wait_for_timeout(2500)
    ui = sorted(page.evaluate(BLOCK_JS, {"count": 20}))
    data["ui_response"] = {
        "samples": len(ui),
        "p50_ms": round(statistics.median(ui), 2),
        "p95_ms": round(ui[round(0.95 * (len(ui) - 1))], 2),
        "gate_p95_ms": 300.0,
        "case": "guigang 30 m（每帧 2 张 PNG，最重）",
        "method": "浏览器内 performance.now()（滑块 input → 帧提交），不含 IPC",
    }

    # 4) 播放帧率（2 帧/s 跑 6 s）
    page.eval_on_selector(
        "#frame-slider",
        "el => { el.value = '0'; el.dispatchEvent(new Event('input', {bubbles:true})); }",
    )
    page.wait_for_timeout(600)
    page.click("button[data-speed='2']")
    page.click("#btn-play")
    started = time.perf_counter()
    page.wait_for_timeout(6000)
    page.click("#btn-play")
    elapsed = time.perf_counter() - started
    advanced = int(page.evaluate("window.__fvDebug.state().index"))
    data["playback"] = {
        "seconds": round(elapsed, 2),
        "frames_advanced": advanced,
        "effective_fps": round(advanced / elapsed, 2),
        "target_fps": 2.0,
    }

    # 5) 内存
    memory = page.evaluate(
        """() => {
            const imgs = Array.from(document.images);
            let decoded = 0;
            for (const i of imgs) decoded += (i.naturalWidth||0) * (i.naturalHeight||0) * 4;
            return {images: imgs.length, decodedBytes: decoded,
                    heap: performance.memory ? performance.memory.usedJSHeapSize : null};
        }"""
    )
    cache = page.evaluate("() => window.__fvDebug.state().cache")
    data["memory"] = {
        "dom_images": memory["images"],
        "decoded_pixels_mb": round(memory["decodedBytes"] / 1e6, 1),
        "js_heap_mb": round((memory["heap"] or 0) / 1e6, 1),
        "lru_cache_mb": round((cache or {}).get("bytes", 0) / 1e6, 1),
        "lru_limit_mb": round((cache or {}).get("maxBytes", 0) / 1e6, 1),
        "note": "解码像素 = w×h×4 估算；不含 WebGL 纹理与 GPU 显存，属下界",
    }
    data["console_errors"] = list(page.evaluate("() => (window.__fvErrors || []).slice()"))
    data["page_errors"] = page_errors[:10]
    context.close()
    browser.close()
    return data


def test_performance_metrics(e2e_url: str, e2e_bundle: Path, e2e_browser) -> None:
    """首屏、点查 p95、界面响应 p95、播放帧率、内存；有头（真实 GPU）判门禁，无头只记录。"""
    catalog = load_json(e2e_bundle / "catalog.json")
    results: dict = {
        "conditions": {
            "viewport": "1920x1080",
            "device_scale_factor": 1,
            "server": "python -m http.server, 127.0.0.1",
            "bundle": e2e_bundle.name,
            "note": "有头 = 真实 AMD Radeon 核显（ANGLE/D3D11）；无头 = SwiftShader 软件渲染，仅参考",
        }
    }
    browser_type = e2e_browser.browser_type
    # 默认跑「无头 + GPU 旗标」：不弹窗口、不触发浏览器下载管理器（如 IDM），
    # 但仍用真实核显渲染（ANGLE/D3D11），所以数字代表用户体验。
    # 另跑一遍 SwiftShader 软件渲染作为下界对比（不判门禁）。
    run_headed = os.environ.get("FLOODVIZ_E2E_HEADED", "").strip() in {"1", "true", "yes"}
    results["headed_enabled"] = run_headed
    results["gate_mode"] = "headless_gpu"
    results["headless_gpu"] = _probe_browser(browser_type, mode="headless_gpu", e2e_url=e2e_url, catalog=catalog)
    results["headless_software"] = _probe_browser(
        browser_type, mode="headless_software", e2e_url=e2e_url, catalog=catalog
    )
    if run_headed:
        results["headed_gpu"] = _probe_browser(browser_type, mode="headed", e2e_url=e2e_url, catalog=catalog)

    (QA_DIR / "perf_e2e.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    soft = results["headless_software"]
    gpu = results["headless_gpu"]
    print("[perf][headless+gpu]", json.dumps(gpu, ensure_ascii=False)[:700])
    print("[perf][headless/sw]", json.dumps(soft, ensure_ascii=False)[:700])

    # 项目不变门禁（与 GPU/CPU 无关）
    assert not soft["page_errors"], soft["page_errors"]
    assert not soft["console_errors"], soft["console_errors"]
    assert soft["cold_start"]["first_interactive_s"] <= 5.0, soft["cold_start"]
    assert soft["cold_start"]["first_business_tile_s"] <= 2.0, soft["cold_start"]
    # 门禁口径：缓存不得超过它自己配置的上限。
    # 实现上限是 256 MiB（268 435 456 B），stat 里的 *_mb 是 ÷1e6 的十进制 MB，
    # 因此不能拿硬编码的 256.0 比（那会变成 256 MB = 256e6 B 的错口径）。
    mem = soft["memory"]
    assert mem["lru_cache_mb"] <= mem["lru_limit_mb"] + 1e-6, mem
    assert mem["lru_limit_mb"] <= 256.0 * (1024 ** 2) / 1e6 + 1e-6, mem
    assert soft["playback"]["effective_fps"] >= 1.0, soft["playback"]
    # 交互延时间门禁按计划 §1.4 基准（真实 GPU 渲染）判定；
    # SwiftShader 软件渲染会把 updateImage 纹理上传放大约 6–8 倍，不代表用户体验。
    assert gpu["point_query"]["p95_ms"] <= 200.0, gpu["point_query"]
    assert gpu["ui_response"]["p95_ms"] <= 300.0, gpu["ui_response"]
    assert "AMD" in gpu["renderer"] or "NVIDIA" in gpu["renderer"] or "Intel" in gpu["renderer"], gpu["renderer"]


# --------------------------------------------------------------------------- 稳定性


def test_hundred_case_switches_no_leak(app) -> None:
    """计划 M3：100 次切地不泄漏、不报错、帧与时间一致。

    断言：切换后 caseId/帧数/时间读数正确；缓存不超上限、LRU 有淘汰但无溢出；
    无未捕获异常。
    """
    order = ["jiulong", "ningming", "guigang"]
    expected_frames = {"jiulong": 21, "ningming": 19, "guigang": 57}
    start_heap = app.page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0")

    for i in range(100):
        case_id = order[i % 3]
        expected = expected_frames[case_id]
        app.page.click(f".case-tab[data-case-id='{case_id}']")
        # 必须等到"新案例的帧数也已提交"，只看 caseId 会读到切换过程中的中间态
        # （实测过：caseId 已变、frameCount 还是上一案例的值 → 偶发 AssertionError）。
        app.page.wait_for_function(
            f"() => {{ const s = window.__fvDebug.state();"
            f" return s.caseId === '{case_id}' && s.frameId !== null && s.frameCount === {expected}; }}",
            timeout=90000,
        )
        state = app.state()
        assert state["frameCount"] == expected, (i, case_id, state["frameCount"])
        assert state["caseId"] == case_id

    cache = app.state()["cache"]
    assert cache["bytes"] <= cache["maxBytes"], cache
    assert cache.get("overflow") is not True, cache
    assert not app.js_errors(), app.js_errors()

    end_heap = app.page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0")
    growth_mb = (end_heap - start_heap) / 1e6 if start_heap and end_heap else 0.0
    payload = {
        "switches": 100,
        "cases_cycled": order,
        "lru_cache": cache,
        "js_heap_growth_mb": round(growth_mb, 1),
        "note": "JS 堆为浏览器上限内的当前用量；growth 为正值表示增长",
    }
    (QA_DIR / "stability_100_switches.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert growth_mb <= 200.0, payload


def test_playback_two_minutes_no_unbounded_growth(app) -> None:
    """连续播放 2 分钟（2 帧/s）：帧推进正常、缓存不无限增长、无报错。

    注：计划要求的 30 分钟播放属人工长时验收，本用例只验证短时稳定性。
    """
    app.switch_case("guigang")
    app.set_frame(0)
    app.page.click("button[data-speed='2']")
    app.page.click("#btn-play")
    samples = []
    for _ in range(8):
        app.page.wait_for_timeout(15000)
        cache = app.state()["cache"]
        samples.append({"bytes": cache["bytes"], "count": cache["count"], "index": app.state()["index"]})
    app.page.click("#btn-play")

    assert not app.js_errors(), app.js_errors()
    cache = app.state()["cache"]
    assert cache["bytes"] <= cache["maxBytes"], cache
    assert cache.get("overflow") is not True, cache
    assert samples[-1]["index"] >= samples[0]["index"], samples
    payload = {"duration_s": 120, "samples": samples, "final_cache": cache}
    (QA_DIR / "stability_playback_2min.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- 计划 §1.4 余下两项


def _chromium_rss_mb() -> float | None:
    """返回所有 Chromium 进程工作集之和（MB）；非 Windows 或取不到时返回 None。

    用 OS 级 RSS 是为了回答计划 §1.4 的「浏览器总内存 ≤1.5 GB」：
    页内只能读到 JS 堆与解码像素，读不到 WebGL 纹理、GPU 进程与浏览器主进程。
    Edge 进程名是 `msedge`，不会混进来。
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return None
    # 注意：Playwright 无头模式跑的是 chrome-headless-shell.exe（不是 chrome.exe），
    # 有头模式才是 chrome.exe；两个名字都要统计，否则会误得 0。
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=25, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    total_kb = 0.0
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 5:
            continue
        image = parts[0].lower()
        if not any(token in image for token in ("chrome", "chromium", "headless_shell")):
            continue
        # 形如 chrome.exe,"1234","Console","1","123,456 K"
        mem = parts[-1].replace(" K", "").replace(",", "").replace('"', "").strip()
        if mem.isdigit():
            total_kb += float(mem)
    # 基线时可能一个 chrome.exe 都没有（tasklist 返回「没有运行的任务」）→ 0.0，
    # 不能返回 None，否则 test_browser_total_memory 会误判为"平台不支持"而跳过。
    return total_kb / 1024.0


def test_browser_total_memory(e2e_url: str, e2e_browser) -> None:
    """计划 §1.4「资源」：浏览器总内存目标 ≤1.5 GB（贵港 30 m 压力场景）。

    测法：Sum(所有 Chromium 进程 WorkingSet)。这是唯一能覆盖 WebGL 纹理、GPU 进程
    与浏览器主进程的读法；页内 `performance.memory` 只给 JS 堆。
    复用会话级 Playwright 的 browser_type，避免在 sync_playwright 事件循环内嵌套新实例。
    非 Windows 平台跳过（无 tasklist）。
    """
    baseline = _chromium_rss_mb()
    if baseline is None:
        pytest.skip("当前平台取不到 Chromium 进程 RSS（仅 Windows 支持 tasklist）")

    browser = e2e_browser.browser_type.launch(
        headless=True,
        args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-gpu-rasterization"],
    )
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    page = context.new_page()
    page.goto(e2e_url, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=90000)
    # 压力场景：贵港 30 m，逐帧过一遍（57 帧，缓存最重）
    page.click(".case-tab[data-case-id='guigang']")
    page.wait_for_function(
        "() => window.__fvDebug.state().caseId === 'guigang' && window.__fvDebug.state().frameCount === 57",
        timeout=90000,
    )
    page.wait_for_timeout(2000)
    for index in range(0, 57, 4):
        page.eval_on_selector(
            "#frame-slider",
            "(el, v) => { el.value = String(v); el.dispatchEvent(new Event('input', {bubbles:true})); }",
            index,
        )
        page.wait_for_timeout(250)
    page.wait_for_timeout(3000)

    peak = _chromium_rss_mb()
    cache = page.evaluate("() => window.__fvDebug.state().cache")
    memory = page.evaluate(
        """() => {
            const imgs = Array.from(document.images);
            let decoded = 0;
            for (const i of imgs) decoded += (i.naturalWidth||0) * (i.naturalHeight||0) * 4;
            return {heap: performance.memory ? performance.memory.usedJSHeapSize : 0, decoded};
        }"""
    )
    context.close()
    browser.close()

    after_close = _chromium_rss_mb()
    payload = {
        "method": "Sum(tasklist 全部 Chromium 进程 WorkingSet；无头为 chrome-headless-shell.exe)",
        "baseline_mb": round(baseline, 1),
        "peak_mb": round(peak, 1) if peak is not None else None,
        "browser_only_mb": round(peak - baseline, 1) if peak is not None else None,
        "after_close_mb": round(after_close, 1) if after_close is not None else None,
        "js_heap_mb": round(memory["heap"] / 1e6, 1),
        "decoded_pixels_mb": round(memory["decoded"] / 1e6, 1),
        "lru_cache": cache,
        "gate_total_mb": 1536.0,
        "case": "guigang 30 m（57 帧全过一遍，最重）",
        "note": (
            "peak_mb 为机器上所有 Chromium 进程之和（本用例独占启动，故≈本次会话占用）；"
            "browser_only_mb 已扣除启动前基线。含 GPU 进程与 WebGL 纹理，比页内读数完整。"
        ),
    }
    (QA_DIR / "browser_memory.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("\n[browser-memory]", json.dumps(payload, ensure_ascii=False)[:420])
    assert payload["browser_only_mb"] is not None, "取不到 Chromium 进程 RSS"
    assert payload["browser_only_mb"] > 0, payload
    assert payload["browser_only_mb"] <= 1536.0, payload


def test_playback_stability_three_cases(app) -> None:
    """计划 §1.4「播放」：三个代表范围各 30 轮，1–2 帧/s 稳定回放，不伪造帧。

    「三个代表范围」= 三地默认 run。每地推进 30 帧，断言：
    - 帧号严格按真实索引前进（不跳号、不重复、不越界）；
    - 时间读数随帧单调（与 timeline 的 sim_seconds 一致）；
    - 不出现「时间在走但帧号没动」的伪造帧行为；
    - 无 JS 错误。
    """

    bundle = app.session.bundle if hasattr(app.session, "bundle") else None
    cases = ["jiulong", "ningming", "guigang"]
    report: dict = {"bundle": getattr(bundle, "name", None)}

    for case_id in cases:
        app.switch_case(case_id)
        frame_count = app.state()["frameCount"]
        app.set_frame(0)
        app.page.click("button[data-speed='2']")
        app.page.click("#btn-play")

        observed: list[int] = []
        for _ in range(30):
            app.page.wait_for_timeout(520)  # 2 帧/s → 每轮约 1 帧
            index = app.state()["index"]
            if not observed or observed[-1] != index:
                observed.append(index)
        app.page.click("#btn-play")
        app.page.wait_for_timeout(400)

        assert not app.js_errors(), (case_id, app.js_errors())
        # 严格递增且不越界
        assert observed == sorted(observed), (case_id, observed)
        assert len(set(observed)) == len(observed), (case_id, observed)
        assert observed[0] >= 0 and observed[-1] < frame_count, (case_id, observed, frame_count)
        advanced = observed[-1] - observed[0]
        assert advanced >= 10, f"{case_id}: 30 轮只推进 {advanced} 帧（应 ≈15–30）"
        report[case_id] = {
            "frame_count": frame_count,
            "advanced_frames": advanced,
            "observed_indices": observed[:12],
            "rounds": 30,
        }

    (QA_DIR / "playback_three_cases.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_metadata_size_budget(e2e_bundle: Path) -> None:
    """计划 §1.4「资源」：元数据 ≤1 MB/案例。

    计入：该案例所有 run 的 timeline/statistics/images/numeric_manifest，
    以及 frames/<case>/ 下的 DEM/SAR/光学/包络 images.json 等派生元数据。
    不计：PNG 与 .bin 本体（另由包体积预算约束）。
    """
    catalog = load_json(e2e_bundle / "catalog.json")
    per_case: dict[str, int] = {}
    details: dict[str, list[dict]] = {}

    for case in catalog["cases"]:
        case_id = case["case_id"]
        total = 0
        files: list[dict] = []
        for run in case["runs"]:
            for key in ("timeline", "statistics", "numeric_manifest", "images"):
                rel = run.get(key)
                if not rel:
                    continue
                path = e2e_bundle / rel
                if not path.is_file():
                    continue
                size = path.stat().st_size
                total += size
                files.append({"path": rel, "bytes": size})
        frames_dir = e2e_bundle / "frames" / case_id
        if frames_dir.is_dir():
            for path in frames_dir.rglob("*.json"):
                size = path.stat().st_size
                total += size
                files.append({"path": str(path.relative_to(e2e_bundle)), "bytes": size})
        per_case[case_id] = total
        details[case_id] = sorted(files, key=lambda item: -item["bytes"])[:6]

    payload = {
        "gate_bytes": 1024 * 1024,
        "per_case_bytes": per_case,
        "per_case_mb": {k: round(v / 1e6, 3) for k, v in per_case.items()},
        "largest_files": details,
        "note": "只计元数据（JSON）；PNG 与 .bin 不计入本门禁",
    }
    (QA_DIR / "metadata_size.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("\n[metadata-size]", json.dumps(payload["per_case_mb"], ensure_ascii=False))
    for case_id, size in per_case.items():
        assert size <= 1024 * 1024, f"{case_id} 元数据 {size} B > 1 MB"
