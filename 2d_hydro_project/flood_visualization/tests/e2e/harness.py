"""e2e 工具与页面封装（Playwright + Chromium，只读发布包）。

设计约束：
- 服务只绑 127.0.0.1，端口 8790（交付 8765 / web 组开发 8788 不用）；
- 断言一律走真实发布包 `outputs/web_bundle/<id>`，不写任何包内文件；
- 页面状态通过只读调试接口 `window.__fvDebug` 读取（web 组提供）。

pytest 以 basename 导入 conftest（tests/ 无 __init__.py），子目录的 conftest.py 会遮蔽根
conftest，因此 e2e 夹具写在 `tests/conftest.py`，本文件只放常量/函数/类。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = PROJECT_ROOT / "outputs" / "qa" / "evidence"

#: 久隆/宁明为 1×2 之前先确认：三地分块列数（由 images.json split 决定）
KNOWN_SPLIT = {"jiulong": (1, 1), "ningming": (1, 1), "guigang": (1, 2)}


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evidence(name: str) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    return EVIDENCE_DIR / name


def http_status(url: str, *, method: str = "HEAD", timeout: float = 15.0) -> int:
    """返回 HTTP 状态码；连接失败返回 0。"""
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            if method == "GET":
                resp.read(64)
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return 0


class AppPage:
    """页面句柄 + 常用交互/断言辅助。"""

    def __init__(self, session) -> None:
        self.session = session
        self.page = session.page
        self.url = session.url
        self.opened = False

    # ------------------------------------------------------------ 加载

    def open(self, *, timeout: int = 60000) -> AppPage:
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_ready(timeout=timeout)
        self.opened = True
        return self

    def wait_ready(self, *, timeout: int = 60000) -> None:
        """等调试状态就绪，并等界面面板真正绘制完成（避免读到空面板的竞态）。"""
        self.page.wait_for_function(
            "() => window.__fvDebug && window.__fvDebug.state() && window.__fvDebug.state().frameId !== null",
            timeout=timeout,
        )
        self.wait_panels()

    def wait_panels(self, *, timeout: int = 30000) -> None:
        """界面稳定判据：质量面板非空、帧读数非空。"""
        self.page.wait_for_function(
            """() => {
                const q = document.getElementById('quality-body');
                const f = document.getElementById('frame-readout');
                return !!(q && q.textContent.trim().length > 20 && f && f.textContent.trim().length > 2);
            }""",
            timeout=timeout,
        )

    def reload(self, *, timeout: int = 60000) -> AppPage:
        self.page.reload(wait_until="domcontentloaded")
        self.wait_ready(timeout=timeout)
        return self

    # ------------------------------------------------------------ 状态

    def state(self) -> dict[str, Any]:
        return self.page.evaluate("() => window.__fvDebug.state()")

    def query(self, lon: float, lat: float) -> Any:
        return self.page.evaluate("([lon, lat]) => window.__fvDebug.query(lon, lat)", [lon, lat])

    def has_map(self) -> bool:
        return bool(self.page.evaluate("() => window.__fvDebug.hasMap()"))

    def js_errors(self) -> list[str]:
        return list(self.page.evaluate("() => window.__fvErrors || []"))

    def text(self, selector: str) -> str:
        value = self.page.text_content(selector)
        return (value or "").strip()

    def selectors_count(self, selector: str) -> int:
        return int(self.page.eval_on_selector_all(selector, "els => els.length"))

    # ------------------------------------------------------------ 交互

    def wait_state(self, expr: str, *, timeout: int = 45000) -> None:
        """等待 __fvDebug.state() 满足 JS 表达式（expr 里用 s 表示 state）。"""
        self.page.wait_for_function(
            f"() => {{ const s = window.__fvDebug.state(); return ({expr}); }}", timeout=timeout
        )

    def set_frame(self, index: int, *, timeout: int = 45000) -> None:
        self.page.eval_on_selector(
            "#frame-slider",
            "(el, v) => { el.value = String(v); el.dispatchEvent(new Event('input', {bubbles:true})); }",
            index,
        )
        self.wait_state(f"s.index === {index}", timeout=timeout)

    def click_frame_button(self, selector: str, *, timeout: int = 45000) -> None:
        self.page.click(selector)
        self.page.wait_for_timeout(50)

    def switch_case(self, case_id: str, *, timeout: int = 90000) -> None:
        self.page.click(f".case-tab[data-case-id='{case_id}']")
        self.wait_state(f"s.caseId === '{case_id}' && s.frameId !== null", timeout=timeout)
        # 面板重绘发生在状态更新之后，必须等它落定再断言文本
        self.page.wait_for_function(
            "(id) => { const q = document.getElementById('quality-body');"
            " return !!(q && q.textContent.includes(id)); }",
            arg=case_id,
            timeout=timeout,
        )
        self.wait_panels(timeout=timeout)

    def preset(self, preset_id: str) -> None:
        self.page.click(f"button[data-preset-id='{preset_id}']")

    def visible_groups(self) -> dict[str, dict[str, Any]]:
        state = self.state()
        return {group: info for group, info in state["layers"].items() if info["visible"]}

    def layer_item_text(self, group: str) -> str:
        return self.text(f".layer-item[data-layer='{group}']")

    def check(self, selector: str) -> bool:
        return bool(self.page.eval_on_selector(selector, "el => el.checked"))

    def opacity_value(self, group: str) -> float:
        raw = self.page.eval_on_selector(f"#layer-op-{group}", "el => el.value")
        return float(raw) / 100.0

    def set_opacity(self, group: str, percent: int) -> None:
        self.page.eval_on_selector(
            f"#layer-op-{group}",
            "(el, v) => { el.value = String(v); el.dispatchEvent(new Event('input', {bubbles:true})); }",
            percent,
        )

    def notice(self) -> str:
        hidden = self.page.eval_on_selector("#notice-bar", "el => el.hidden")
        if hidden:
            return ""
        return self.text("#notice-bar")

    def wait_notice(self, *, keyword: str = "", timeout: int = 30000) -> str:
        """等提示条渲染完成（可选：包含指定关键词），返回提示文本。

        提示条在帧提交之后由异步回调写入，直接读会读到上一状态（空串）。
        """
        self.page.wait_for_function(
            """(kw) => {
                const el = document.getElementById('notice-bar');
                if (!el || el.hidden) return false;
                const text = (el.textContent || '').trim();
                if (text.length < 4) return false;
                return kw ? text.includes(kw) : true;
            }""",
            arg=keyword,
            timeout=timeout,
        )
        return self.text("#notice-bar")

    def chart_painted_pixels(self) -> int:
        return int(
            self.page.evaluate(
                """() => {
                    const c = document.getElementById('area-chart');
                    const ctx = c.getContext('2d');
                    const d = ctx.getImageData(0, 0, c.width, c.height).data;
                    let n = 0;
                    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n += 1;
                    return n;
                }"""
            )
        )

    def place_marker_at(self, lon: float, lat: float) -> None:
        """在模型域内点击地图中心（用于走真实 mouse.click 路径）。"""
        box = self.page.eval_on_selector(
            "#map", "el => { const r = el.getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height}; }"
        )
        self.page.mouse.click(box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)


class Timer:
    """简单秒表（性能测量用）。"""

    def __enter__(self) -> Timer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.elapsed = time.perf_counter() - self.started

    @property
    def ms(self) -> float:
        return self.elapsed * 1000.0


def write_perf(payload: dict[str, Any]) -> Path:
    path = EVIDENCE_DIR.parent / "perf_e2e.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_gz_json(path: Path) -> Any:  # pragma: no cover - 预留
    import gzip

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)
