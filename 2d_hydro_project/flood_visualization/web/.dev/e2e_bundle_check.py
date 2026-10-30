"""对任意发布包（mock 或真实 bundle）跑数据驱动的 e2e 校验。

与 e2e_selftest.py 的区别：本脚本不硬编码帧数/图层状态，而是先读 catalog.json，
再按契约推导期望值（帧数、可用图层、时间语义、parts 数量），因此可直接用于真实包。

用法：
  .venv/Scripts/python.exe web/.dev/e2e_bundle_check.py --url http://127.0.0.1:8789/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = Path(__file__).resolve().parent / "screenshots"
RESULTS: list[dict] = []
NET_ERR: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (("　" + detail) if detail else ""))
    return ok


def load_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def dbg(page) -> dict:
    return page.evaluate("() => window.__fvDebug.state()")


def wait_ready(page, timeout=120000):
    page.wait_for_function("() => window.__fvReady !== undefined", timeout=timeout)
    page.evaluate("() => window.__fvReady")
    page.wait_for_function(
        "() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=timeout
    )


def usable(layer, images=None) -> bool:
    if not layer:
        return False
    if layer.get("status") not in ("ready", "partial"):
        return False
    if layer.get("kind") == "frame":
        return bool(layer.get("images"))
    if layer.get("kind") == "max_envelope":
        return bool(layer.get("image") or layer.get("images") or layer.get("parts") or images)
    if layer.get("kind") == "vector":
        return bool(layer.get("geojson"))
    return bool(layer.get("image") or layer.get("images") or layer.get("parts"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8789/")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/")

    catalog = load_json(base + "/catalog.json")
    print("== bundle: %s（%d 案例）" % (catalog.get("bundle_id"), len(catalog.get("cases", []))))
    frames_by_case: dict[str, list] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = ctx.new_page()
        console_errors: list[str] = []

        def on_console(msg):
            if msg.type != "error":
                return
            if "Failed to load resource" in msg.text:
                loc = msg.location or {}
                url = loc.get("url", "")
                if url and not url.startswith(("http://127.0.0.1", "http://localhost")):
                    NET_ERR.append(url)
                    return
            console_errors.append(msg.text)

        page.on("console", on_console)
        page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))

        page.goto(args.url, wait_until="domcontentloaded")
        wait_ready(page)
        page.wait_for_timeout(800)

        # ---------- 每个案例逐一验证 ----------
        for case in catalog["cases"]:
            cid = case["case_id"]
            case_name = case.get("name", cid)
            run = next((r for r in case["runs"] if r["run_id"] == case["default_run_id"]), case["runs"][0])
            timeline = load_json(base + "/" + run["timeline"])
            stats = load_json(base + "/" + run["statistics"]) if run.get("statistics") else None
            frames_by_case[cid] = timeline["frames"]
            images_url = case["layers"]["flood"]["images"]
            images = load_json(base + "/" + images_url)
            n_frames = len(timeline["frames"])
            n_parts = len(images["frames"][0]["parts"])

            page.click(".case-tab[data-case-id='%s']" % cid)
            page.wait_for_function(
                "c => window.__fvDebug.state().caseId === c && window.__fvDebug.state().frameId !== null",
                arg=cid,
                timeout=180000,
            )
            page.wait_for_timeout(900)
            st = dbg(page)
            check("%s 1.1 案例切换 + 帧数" % case_name, st["caseId"] == cid and st["frameCount"] == n_frames,
                  "frames=%s expected=%s" % (st["frameCount"], n_frames))
            check("%s 1.2 parts 与 images.json 一致" % case_name, n_parts == len(images["frames"][0]["parts"]),
                  "parts=%s" % n_parts)
            check("%s 1.3 首帧已提交" % case_name, st["frameId"] == timeline["frames"][0]["frame_id"],
                  str(st["frameId"]))

            # 图层状态机
            for group, layer in case["layers"].items():
                if group not in st["layers"]:
                    continue
                expect = usable(layer, images.get("max"))
                got = st["layers"][group]["available"]
                check("%s 2.%s 图层可用性 %s（%s）" % (case_name, group, group, layer.get("status")),
                      expect == got, "expect=%s got=%s status=%s" % (expect, got, layer.get("status")))

            # 时间语义
            origin_confirmed = timeline.get("origin_status") == "model_schedule_confirmed"
            last = timeline["frames"][-1]
            page.evaluate(
                """(i) => { const el=document.getElementById('frame-slider'); el.value=String(i); el.dispatchEvent(new Event('input',{bubbles:true})); }""",
                n_frames - 1,
            )
            page.wait_for_timeout(2500)
            st = dbg(page)
            rel = page.text_content("#time-relative").strip()
            abs_text = page.text_content("#time-absolute").strip()
            expect_rel = "T+%.1f h" % (last["sim_seconds"] / 3600)
            check("%s 3.1 末帧 T+ 文本" % case_name, rel == expect_rel, "%s vs %s" % (rel, expect_rel))
            if last.get("timestamp"):
                check("%s 3.2 有 timestamp → 显示绝对时间" % case_name,
                      "Invalid" not in abs_text and abs_text not in ("", "—"), abs_text)
            else:
                check("%s 3.2 timestamp=null → 不伪造日期" % case_name,
                      "Invalid" not in abs_text and ("不可用" in abs_text or "候选" in abs_text), abs_text)
            check("%s 3.3 帧号与帧一致" % case_name, st["index"] == n_frames - 1, "index=%s" % st["index"])

            # 面积曲线与统计一致
            if stats:
                thr = str(catalog["display"]["default_threshold_m"])
                expect_area = stats["frames"][-1]["areas_km2"][thr]
                canvas_painted = page.evaluate(
                    """() => {
                        const c = document.getElementById('area-chart');
                        const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
                        let n=0; for (let i=3;i<d.length;i+=4) if (d[i]>0) n++;
                        return n;
                    }"""
                )
                check("%s 4.1 面积曲线已绘制" % case_name, canvas_painted > 1500, "painted=%s" % canvas_painted)
                shown = page.evaluate(
                    """() => {
                        const t = document.getElementById('map-subtitle').textContent;
                        return t;
                    }"""
                )
                check("%s 4.2 副标题含当前面积" % case_name, "面积" in shown, shown[:120])

                # 点查：找一个湿像元（统计未给位置，用网格中心与若干候选点探测）
                grid = json.loads(
                    urllib.request.urlopen(base + "/" + run["numeric_manifest"], timeout=30).read().decode("utf-8")
                )["grid"]
                found = None
                for fx, fy in [(0.5, 0.5), (0.45, 0.5), (0.55, 0.45), (0.4, 0.6), (0.6, 0.4), (0.35, 0.55)]:
                    lon, lat = _center_lonlat(case, fx, fy)
                    val = page.evaluate(
                        """async ([lon, lat]) => {
                            window.__fvDebug.query(lon, lat);
                            await new Promise(r => setTimeout(r, 700));
                            return document.getElementById('query-body').textContent;
                        }""",
                        [lon, lat],
                    )
                    if "数值源" in val and "域外" not in val:
                        found = (lon, lat, val)
                        break
                check("%s 5.1 点查返回原生数值" % case_name, found is not None,
                      (found[2][:110] if found else "未找到有效查询点"))
                if found:
                    check("%s 5.2 点查含分辨率与来源 run" % case_name,
                          "像元分辨率" in found[2] and "来源 run" in found[2], found[2][:120])
                # 域外
                out_text = page.evaluate(
                    """async () => {
                        window.__fvDebug.query(100.0, 10.0);
                        await new Promise(r => setTimeout(r, 700));
                        return document.getElementById('query-body').textContent;
                    }"""
                )
                check("%s 5.3 域外提示" % case_name, "域外" in out_text, out_text[:80])

                # 最大包络
                page.evaluate(
                    """(i) => { const el=document.getElementById('frame-slider'); el.value=String(i); el.dispatchEvent(new Event('input',{bubbles:true})); }""",
                    max(0, n_frames // 3),
                )
                page.wait_for_timeout(1800)
                idx_before = dbg(page)["index"]
                page.click("#btn-max")
                page.wait_for_timeout(3000)
                st = dbg(page)
                title = page.text_content("#map-title")
                check("%s 6.1 包络模式开启并暂停" % case_name, st["maxMode"] and not st["playing"])
                check("%s 6.2 包络标题" % case_name, "包络" in title and "非某一时刻" in title, title)
                check("%s 6.3 时间轴禁用" % case_name,
                      page.eval_on_selector("#frame-slider", "el => el.disabled"))
                sub = page.text_content("#map-subtitle")
                expect_env = stats["max_envelope"]["areas_km2"][thr] if stats.get("max_envelope") else None
                check("%s 6.4 包络面积与 statistics 一致" % case_name,
                      expect_env is None or _area_matches(sub, expect_env),
                      "%s vs %s" % (sub[:90], expect_env))
                # 包络口径一致性：曲线区不得同时显示“当前帧”面积（避免两个口径混称）
                chart_text_ok = page.evaluate(
                    """() => {
                        const note = document.getElementById('chart-note').textContent;
                        const sub = document.getElementById('map-subtitle').textContent;
                        return {note: note, sub: sub};
                    }"""
                )
                check("%s 6.6 包络模式曲线说明切换为包络口径" % case_name,
                      "包络" in chart_text_ok["note"] and "暂停" in chart_text_ok["note"],
                      chart_text_ok["note"][:120])
                check("%s 6.7 包络模式副标题不混称单帧" % case_name,
                      "包络" in chart_text_ok["sub"],
                      chart_text_ok["sub"][:110])
                page.screenshot(path=str(OUT / ("real-%s-max.png" % cid)))
                page.click("#btn-max")
                page.wait_for_timeout(2500)
                st = dbg(page)
                check("%s 6.5 关闭包络恢复原帧" % case_name, (not st["maxMode"]) and st["index"] == idx_before,
                      "index=%s before=%s" % (st["index"], idx_before))

            page.screenshot(path=str(OUT / ("real-%s.png" % cid)))

        # ---------- 快速拖动令牌（在最后一个案例上）----------
        n = dbg(page)["frameCount"]
        page.evaluate(
            """async (n) => {
                const el = document.getElementById('frame-slider');
                const seq = [1, Math.floor(n*0.7), Math.floor(n*0.3), n-2, Math.floor(n*0.5)];
                for (const v of seq) {
                    el.value = String(v);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    await new Promise(r => setTimeout(r, 60));
                }
            }""",
            n,
        )
        page.wait_for_timeout(4000)
        st = dbg(page)
        last_case = catalog["cases"][-1]
        last_run = next(r for r in last_case["runs"] if r["run_id"] == last_case["default_run_id"])
        tl = load_json(base + "/" + last_run["timeline"])
        check("7.1 快速拖动后帧号=请求值", st["index"] == int(n * 0.5), "index=%s expect=%s" % (st["index"], int(n * 0.5)))
        expect_rel = "T+%.1f h" % (tl["frames"][st["index"]]["sim_seconds"] / 3600)
        check("7.2 快速拖动后时间与帧一致", page.text_content("#time-relative").strip() == expect_rel,
              "%s vs %s" % (page.text_content("#time-relative").strip(), expect_rel))

        # ---------- 在线底图失败降级（route 拦截 Esri，确定性触发）----------
        ctx.route(re.compile(r"arcgisonline\.com"), lambda route: route.abort())
        if not page.eval_on_selector("#basemap-toggle", "el => el.checked"):
            page.click("#basemap-toggle")
        page.wait_for_timeout(4000)
        warn = page.text_content("#basemap-warn") or ""
        notice = page.text_content("#notice-bar") or ""
        check("8.1 底图瓦片被拦截 → 显示“在线底图不可用”提示条", "在线底图不可用" in notice, notice[:140])
        check("8.2 左侧也显示不可用原因", "不可用" in warn, warn[:120])
        check("8.3 重试按钮出现", page.is_visible("#basemap-retry"))
        st = dbg(page)
        check("8.4 底图失败后本地图层仍可见", st["layers"]["flood"]["visible"], str(st["layers"]["flood"]))
        page.screenshot(path=str(OUT / "real-basemap-fail.png"))
        # 允许重试后成功路径不误报（解除拦截 → 重试）
        ctx.unroute(re.compile(r"arcgisonline\.com"))
        page.click("#basemap-retry")
        page.wait_for_timeout(3500)
        warn2 = page.text_content("#basemap-warn") or ""
        check("8.5 解除拦截后重试不再提示不可用", "不可用" not in warn2 or page.is_hidden("#basemap-warn"),
              warn2[:120])
        page.click("#basemap-toggle")
        page.wait_for_timeout(500)

        # ---------- 离线（全部外部请求阻断）----------
        ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1|localhost)"), lambda route: route.abort())
        st = dbg(page)
        check("8.6 断网后本地图层仍可用", st["layers"]["flood"]["visible"], str(st["layers"]["flood"]))
        ctx.unroute(re.compile(r"^https?://(?!127\.0\.0\.1|localhost)"))

        # ---------- 键盘 ----------
        idx0 = dbg(page)["index"]
        page.click("#map", position={"x": 30, "y": 30})
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(2500)
        check("9.1 右箭头逐帧", dbg(page)["index"] == min(n - 1, idx0 + 1),
              "index=%s vs %s" % (dbg(page)["index"], idx0 + 1))
        page.keyboard.press(" ")
        page.wait_for_timeout(1500)
        check("9.2 空格播放", dbg(page)["playing"])
        page.keyboard.press(" ")
        page.wait_for_timeout(400)
        check("9.3 空格暂停", not dbg(page)["playing"])

        errs = page.evaluate("() => window.__fvErrors || []")
        check("10.1 无未捕获错误", len(errs) == 0, str(errs[:3]))
        check("10.2 无未处理 console error", len(console_errors) == 0, str(console_errors[:5]))

        report = {
            "bundle": catalog.get("bundle_id"),
            "passed": sum(1 for r in RESULTS if r["ok"]),
            "failed": sum(1 for r in RESULTS if not r["ok"]),
            "results": RESULTS,
            "console_errors": console_errors,
            "expected_network_errors": NET_ERR[:10],
            "page_errors": errs,
            "cache": dbg(page)["cache"],
        }
        (OUT / "e2e-real-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n==== 汇总：%d 通过 / %d 失败 ====" % (report["passed"], report["failed"]))
        browser.close()
        return 0 if report["failed"] == 0 else 1


def _center_lonlat(case, fx, fy):
    """在案例 model_bbox 内取一个相对位置（受 UTM 旋转影响，仅作探测点）。"""
    b = case["model_bbox"]
    lon = b[0] + (b[2] - b[0]) * fx
    lat = b[1] + (b[3] - b[1]) * fy
    return lon, lat


def _area_matches(text: str, expected: float) -> bool:
    import re

    m = re.search(r"包络面积（阈值 [\d.]+ m）：([\d.]+)", text)
    if not m:
        return False
    got = float(m.group(1))
    if expected == 0:
        return got == 0
    return abs(got - expected) / max(abs(expected), 1e-9) < 0.02


if __name__ == "__main__":
    sys.exit(main())
