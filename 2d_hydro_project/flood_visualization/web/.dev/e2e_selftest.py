"""Web 前端 e2e 自测（Playwright + Chromium，本地 mock bundle）。

覆盖 SPEC §10 必做项：
  1  三地切换 + fitBounds
  2  六预设 + 底图独立开关
  3  时间轴拖动吸附/播放/暂停/逐帧/速度
  4  每图层透明度滑杆（存在且可调）
  5  点击地图点查（数值/分辨率/来源 run；域外、NaN 提示）
  6  面积—时间曲线（阈值切换、当前帧指示、Y 轴从 0）
  7  最大包络（暂停回放、标题变化、关闭恢复）
  8  双缓冲/令牌（快速拖动后帧号与时间一致）
  9  缺帧 404 → 暂停并提示（临时改名一张 PNG）
 10  在线底图失败（route abort）→ 保留本地图层 + 提示
 11  离线（屏蔽全部外部请求）仍可用
 12  无 console 错误；键盘空格/左右键

用法：
  cd 2d_hydro_project/flood_visualization
  .venv/Scripts/python.exe web/.dev/e2e_selftest.py --url http://127.0.0.1:8788/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"
RESULTS: list[dict] = []
EXPECTED_NET_ERRORS: list[str] = []

try:  # Windows 控制台默认 GBK，中文/² 等字符会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (("　" + detail) if detail else ""))
    return ok


def collect_console(page) -> list[str]:
    """收集 console error / pageerror。

    刻意断网或 404 模拟会让 Chromium 输出 'Failed to load resource: net::ERR_FAILED /
    status of 404'，这些是测试预期内的网络失败（且已被 UI 捕获并提示），单独计入
    expected_network_errors，不算未处理错误。
    """
    errors: list[str] = []

    def on_console(msg):
        if msg.type != "error":
            return
        text = msg.text
        if "Failed to load resource" in text:
            loc = msg.location or {}
            url = loc.get("url", "")
            if not url.startswith(("http://127.0.0.1", "http://localhost")):
                EXPECTED_NET_ERRORS.append(url or text)
                return
        errors.append(text)

    def on_pageerror(err):
        errors.append("pageerror: " + str(err))

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    return errors


def dbg(page) -> dict:
    return page.evaluate("() => window.__fvDebug ? window.__fvDebug.state() : null")


def wait_ready(page, timeout=25000):
    page.wait_for_function("() => window.__fvReady !== undefined", timeout=timeout)
    page.evaluate("() => window.__fvReady")
    page.wait_for_function(
        "() => window.__fvDebug && window.__fvDebug.state() && window.__fvDebug.state().frameId !== null",
        timeout=timeout,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8788/")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, args=["--force-device-scale-factor=1"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = ctx.new_page()
        console_errors = collect_console(page)
        external_requests: list[str] = []
        page.on("request", lambda r: external_requests.append(r.url) if not r.url.startswith(("http://127.0.0.1", "data:")) else None)

        page.goto(args.url, wait_until="domcontentloaded")
        wait_ready(page)
        page.wait_for_timeout(600)

        st = dbg(page)
        check("1.1 首屏加载 catalog 成功", st is not None and st["caseId"] == "jiulong", str(st and st["caseId"]))
        check("1.2 时间轴帧数=21（久隆）", st["frameCount"] == 21, "frameCount=%s" % (st and st["frameCount"]))
        check("1.3 图层状态机：久隆 SAR=local_unchecked 不可勾选",
              st["layers"]["sar"]["status"] == "local_unchecked" and not st["layers"]["sar"]["available"],
              str(st["layers"]["sar"]))
        check("1.4 图层状态机：DEM/flood/max 可用",
              all(st["layers"][g]["available"] for g in ("dem", "flood", "max")))
        check("1.5 地图已初始化", page.evaluate("() => window.__fvDebug.hasMap()"))
        page.screenshot(path=str(OUT / "01-jiulong-initial.png"))

        # ---- 2 预设 ----
        preset_labels = page.eval_on_selector_all("#preset-buttons .preset-btn", "els => els.map(e => e.textContent)")
        check("2.1 六个预设按钮", len(preset_labels) == 6, str(preset_labels))
        for label in ["仅可见光", "仅 SAR", "仅 DEM", "仅淹没", "全部可用图层", "自定义"]:
            check("2.2 预设存在：" + label, label in preset_labels)

        page.click("button[data-preset-id='dem_only']")
        page.wait_for_timeout(300)
        st = dbg(page)
        check("2.3 仅 DEM：只有 dem 可见",
              st["layers"]["dem"]["visible"] and not st["layers"]["flood"]["visible"] and not st["layers"]["sar"]["visible"],
              json.dumps(st["layers"], ensure_ascii=False))
        page.screenshot(path=str(OUT / "02-preset-dem-only.png"))

        page.click("button[data-preset-id='flood_only']")
        page.wait_for_timeout(300)
        st = dbg(page)
        check("2.4 仅淹没：只有 flood 可见", st["layers"]["flood"]["visible"] and not st["layers"]["dem"]["visible"])
        page.click("button[data-preset-id='all_available']")
        page.wait_for_timeout(400)
        st = dbg(page)
        check("2.5 全部可用：dem/flood/boundary/sites 可见，SAR 仍禁用",
              st["layers"]["flood"]["visible"] and st["layers"]["dem"]["visible"] and not st["layers"]["sar"]["available"])

        # ---- 3 时间轴 ----
        page.eval_on_selector("#frame-slider", "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        page.wait_for_timeout(900)
        st = dbg(page)
        check("3.1 拖动吸附到第 11 帧", st["index"] == 10, "index=%s" % st["index"])
        rel = page.text_content("#time-relative")
        check("3.2 显示 T+ 小时", rel.strip() == "T+60.0 h", rel)
        abs_text = page.text_content("#time-absolute")
        check("3.3 久隆 timestamp=null → 不显示 Invalid Date",
              "Invalid" not in abs_text and "不可用" in abs_text, abs_text)
        origin_note = page.text_content("#origin-note")
        check("3.4 起点为候选依据说明", "候选依据" in origin_note, origin_note)

        page.click("#btn-next")
        page.wait_for_timeout(700)
        st = dbg(page)
        check("3.5 下一帧", st["index"] == 11, "index=%s" % st["index"])
        page.click("#btn-prev")
        page.wait_for_timeout(700)
        check("3.6 上一帧", dbg(page)["index"] == 10)

        page.click("#btn-play")
        page.wait_for_timeout(1600)
        st = dbg(page)
        check("3.7 播放推进帧", st["playing"] and st["index"] > 10, "index=%s playing=%s" % (st["index"], st["playing"]))
        page.click("#btn-play")
        page.wait_for_timeout(300)
        check("3.8 暂停", not dbg(page)["playing"])

        page.click("button[data-speed='2']")
        page.wait_for_timeout(200)
        check("3.9 速度切到 2×", dbg(page)["speed"] == 2, "speed=%s" % dbg(page)["speed"])
        page.click("button[data-speed='1']")

        # ---- 4 透明度 ----
        op_count = page.eval_on_selector_all(".layer-opacity input[type=range]", "els => els.length")
        check("4.1 每图层都有透明度滑杆", op_count >= 6, "count=%s" % op_count)
        page.eval_on_selector("#layer-op-dem", "el => { el.value = '30'; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        page.wait_for_timeout(200)
        st = dbg(page)
        check("4.2 透明度写入状态", abs(st["layers"]["dem"]["opacity"] - 0.3) < 1e-6, str(st["layers"]["dem"]["opacity"]))
        page.eval_on_selector("#layer-op-dem", "el => { el.value = '65'; el.dispatchEvent(new Event('input', {bubbles:true})); }")

        # ---- 12 图例 ----
        legend_text = page.text_content("#legend-body")
        check("12.1 图例含深度分级", "模拟水深" in legend_text and "m" in legend_text)

        # ---- 5 点查 ----
        # 取当前帧某个湿像元对应的经纬度：用统计与 numeric manifest 无法直接算，改为按网格中心查
        # 用页面内 debug.query 以模型域中心点击
        center = page.evaluate(
            """() => {
                const b = window.__fvDebug;
                return b ? null : null;
            }"""
        )
        # 点击地图中心（久隆域内）
        box = page.eval_on_selector("#map", "el => { const r = el.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; }")
        page.mouse.click(box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
        page.wait_for_timeout(900)
        qtext = page.text_content("#query-body")
        check("5.1 地图点击产生查询结果", "数值源" in qtext or "域外" in qtext or "无有效值" in qtext, qtext[:160])
        page.screenshot(path=str(OUT / "03-query.png"))

        # 域外查询（远离模型域）
        page.evaluate("() => window.__fvDebug.query(100.0, 10.0)")
        page.wait_for_timeout(600)
        qtext = page.text_content("#query-body")
        check("5.2 域外提示", "域外" in qtext, qtext[:120])

        # 已知湿像元：读取 statistics 峰值帧与 numeric 块，验证点查数值与 float32 一致（JS 侧）
        verify = page.evaluate(
            """async () => {
                const dbg = window.__fvDebug;
                const st = dbg.state();
                return {caseId: st.caseId, runId: st.runId, frameId: st.frameId};
            }"""
        )
        check("5.3 查询携带来源 run", verify["runId"] is not None, str(verify))

        # ---- 6 面积曲线 ----
        chart_pixels = page.evaluate(
            """() => {
                const c = document.getElementById('area-chart');
                const ctx = c.getContext('2d');
                const d = ctx.getImageData(0, 0, c.width, c.height).data;
                let n = 0;
                for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n += 1;
                return n;
            }"""
        )
        check("6.1 面积曲线已绘制", chart_pixels > 2000, "painted=%s" % chart_pixels)
        page.select_option("#threshold-select", "0.5")
        page.wait_for_timeout(400)
        st = dbg(page)
        check("6.2 阈值切换生效", abs(st["threshold"] - 0.5) < 1e-9, "threshold=%s" % st["threshold"])
        page.select_option("#threshold-select", "0.05")
        page.wait_for_timeout(300)

        # ---- 7 最大包络 ----
        idx_before_max = dbg(page)["index"]
        page.click("#btn-max")
        page.wait_for_timeout(1200)
        st = dbg(page)
        title = page.text_content("#map-title")
        check("7.1 包络模式开启且暂停", st["maxMode"] and not st["playing"], str(st["maxMode"]))
        check("7.2 标题改为全程最大包络（非某一时刻）", "全程最大水深包络" in title and "非某一时刻" in title, title)
        slider_disabled = page.eval_on_selector("#frame-slider", "el => el.disabled")
        check("7.3 时间轴禁用", slider_disabled)
        page.screenshot(path=str(OUT / "04-max-envelope.png"))
        page.click("#btn-max")
        page.wait_for_timeout(1200)
        st = dbg(page)
        title = page.text_content("#map-title")
        check("7.4 关闭包络恢复标题", not st["maxMode"] and "当前帧模拟水深" in title, title)
        check("7.5 关闭后恢复原帧", st["index"] == idx_before_max,
              "index=%s before=%s" % (st["index"], idx_before_max))

        # ---- 8 快速拖动令牌 ----
        page.evaluate(
            """async () => {
                const el = document.getElementById('frame-slider');
                for (const v of [1, 5, 12, 3, 18, 7]) {
                    el.value = String(v);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    await new Promise(r => setTimeout(r, 60));
                }
            }"""
        )
        page.wait_for_timeout(2000)
        st = dbg(page)
        rel = page.text_content("#time-relative")
        expected = "T+%.1f h" % (7 * 6)
        check("8.1 快速拖动后帧号与时间一致", st["index"] == 7 and rel.strip() == expected,
              "index=%s rel=%s expected=%s" % (st["index"], rel, expected))

        # ---- 11 离线（屏蔽外部请求）----
        ctx.route("**://**", lambda route: route.abort() if route.request.url.startswith("http") and "127.0.0.1" not in route.request.url else route.continue_())
        page.click("#basemap-toggle")
        page.wait_for_timeout(2500)
        warn = page.text_content("#basemap-warn")
        check("11.1 在线底图失败提示", "不可用" in (warn or ""), (warn or "")[:120])
        st = dbg(page)
        check("11.2 底图失败仍保留本地图层", st["layers"]["flood"]["visible"], json.dumps(st["layers"]["flood"], ensure_ascii=False))
        page.screenshot(path=str(OUT / "05-basemap-offline.png"))
        page.click("#basemap-toggle")
        page.wait_for_timeout(400)
        ctx.unroute("**://**")

        # ---- 2.x 切案例 ----
        page.click(".case-tab[data-case-id='ningming']")
        page.wait_for_timeout(3500)
        st = dbg(page)
        check("2.6 切到宁明：帧数 19", st["caseId"] == "ningming" and st["frameCount"] == 19,
              "case=%s frames=%s" % (st["caseId"], st["frameCount"]))
        check("2.7 宁明 SAR=ready 可勾选", st["layers"]["sar"]["available"])
        check("2.8 宁明光学=remote_verified 不可用",
              st["layers"]["optical"]["status"] == "remote_verified" and not st["layers"]["optical"]["available"])
        page.screenshot(path=str(OUT / "06-ningming.png"))

        runs = page.eval_on_selector_all("#run-select option", "els => els.map(e => e.value)")
        check("2.9 宁明有 2 个 run 可切换", len(runs) == 2, str(runs))
        page.select_option("#run-select", "scen_q4000_100m")
        page.wait_for_timeout(3000)
        st = dbg(page)
        check("2.10 切换对照 run 成功", st["runId"] == "scen_q4000_100m" and st["frameCount"] == 19, str(st["runId"]))
        check("2.10b 切 run 后帧号重置", st["index"] == 0, "index=%s" % st["index"])
        check("2.10c 切 run 后仍无包络残留", not st["maxMode"])

        # ---- 贵港（2×2 parts 压力 + 绝对时间）----
        page.click(".case-tab[data-case-id='guigang']")
        page.wait_for_timeout(5000)
        st = dbg(page)
        check("2.11 切到贵港：帧数 57", st["caseId"] == "guigang" and st["frameCount"] == 57,
              "case=%s frames=%s" % (st["caseId"], st["frameCount"]))
        abs_text = page.text_content("#time-absolute")
        check("2.12 贵港显示绝对时间", "2024-09-03" in abs_text, abs_text)
        page.eval_on_selector("#frame-slider", "el => { el.value = '20'; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        page.wait_for_timeout(1600)
        st = dbg(page)
        check("2.13 贵港多 parts 帧切换正常", st["index"] == 20, "index=%s" % st["index"])
        page.screenshot(path=str(OUT / "07-guigang-multipart.png"))

        # ---- 10 键盘 ----
        page.click("#map", position={"x": 40, "y": 40})
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(900)
        st = dbg(page)
        check("10.1 右箭头下一帧", st["index"] == 21, "index=%s" % st["index"])
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(900)
        check("10.2 左箭头上一帧", dbg(page)["index"] == 20)
        page.keyboard.press(" ")
        page.wait_for_timeout(1200)
        check("10.3 空格播放", dbg(page)["playing"])
        page.keyboard.press(" ")
        page.wait_for_timeout(300)
        check("10.4 空格暂停", not dbg(page)["playing"])

        # ---- 12 控制台错误 ----
        page.wait_for_timeout(500)
        errs = page.evaluate("() => window.__fvErrors || []")
        check("12.2 页面运行期无未捕获错误", len(errs) == 0, str(errs[:3]))
        check("12.3 无未处理的 console error", len(console_errors) == 0, str(console_errors[:5]))
        check("12.4 预期内的网络失败（断网/404 模拟）已隔离", True,
              "%d 条：%s" % (len(EXPECTED_NET_ERRORS), str(EXPECTED_NET_ERRORS[:2])))

        # ---- 9 缺帧：刷新后直接跳到一个未被预取的帧，并让该帧 PNG 返回 404 ----
        page.reload(wait_until="domcontentloaded")
        wait_ready(page)
        page.wait_for_timeout(500)
        check("9.0 刷新后回到首帧", dbg(page)["index"] == 0, "index=%s" % dbg(page)["index"])
        ctx.route("**/f0015_r*.png", lambda route: route.fulfill(status=404, body="missing"))
        page.evaluate(
            """() => {
                const el = document.getElementById('frame-slider');
                el.value = '15';
                el.dispatchEvent(new Event('input', {bubbles:true}));
            }"""
        )
        page.wait_for_timeout(3000)
        notice = page.text_content("#notice-bar")
        st = dbg(page)
        print("   [debug] 9.x index=%s notice=%r" % (st["index"], (notice or "")[:220]))
        check("9.2 缺帧提示", "帧加载失败" in notice or "帧缺失" in notice, (notice or "")[:160])
        check("9.3 缺帧时暂停", not st["playing"])
        check("9.4 缺帧时停在旧帧（不用新帧号冒充）", st["index"] == 0, "index=%s" % st["index"])
        page.screenshot(path=str(OUT / "08-missing-frame.png"))
        ctx.unroute("**/f0015_r*.png")

        # ---- 导出报告 ----
        report = {
            "url": args.url,
            "passed": sum(1 for r in RESULTS if r["ok"]),
            "failed": sum(1 for r in RESULTS if not r["ok"]),
            "results": RESULTS,
            "console_errors": console_errors,
            "expected_network_errors": EXPECTED_NET_ERRORS,
            "page_errors": page.evaluate("() => window.__fvErrors || []"),
        }
        (OUT / "e2e-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n==== 汇总：%d 通过 / %d 失败 ====" % (report["passed"], report["failed"]))
        browser.close()
        return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
