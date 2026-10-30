"""测试共享夹具：路径、真实配置、合成 ASCII 栅格与合成 run 目录。

合成样本一律由本文件生成（小网格 6×8），不复制父项目大文件。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = PROJECT_ROOT.parent

# 合成网格默认几何：8 列 × 6 行、200 m 像元（与久隆 200 m 情景同量级，但位置是假设值）
SYNTH_ROWS = 6
SYNTH_COLS = 8
SYNTH_CELL = 200.0
SYNTH_XLL = 246900.0
SYNTH_YLL = 2456400.0
SYNTH_NODATA = -9999.0

HEADER_ORDER = ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value")


def write_ascii_grid(
    path: str | Path,
    values: np.ndarray,
    *,
    xll: float = SYNTH_XLL,
    yll: float = SYNTH_YLL,
    cellsize: float = SYNTH_CELL,
    nodata: float = SYNTH_NODATA,
) -> Path:
    """把二维数组写成 ESRI ASCII（6 行头 + 行优先；行 0 = 最北行）。"""
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"write_ascii_grid 只接受二维数组，实得 {arr.shape}")
    rows, cols = arr.shape
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "ncols": cols,
        "nrows": rows,
        "xllcorner": xll,
        "yllcorner": yll,
        "cellsize": cellsize,
        "NODATA_value": nodata,
    }
    lines = [f"{key} {header[key]}" for key in HEADER_ORDER[:-1]]
    lines.append(f"NODATA_value {nodata}")
    lines.extend(" ".join(f"{value:.6f}" for value in row) for row in arr)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def synthetic_depth(
    rows: int = SYNTH_ROWS,
    cols: int = SYNTH_COLS,
    *,
    wet_value: float = 0.75,
    nodata: float = SYNTH_NODATA,
) -> np.ndarray:
    """确定的合成水深场：干地 0、一片湿地 wet_value、四角 NoData。"""
    arr = np.zeros((rows, cols), dtype=np.float64)
    wet_r0, wet_r1 = max(0, rows // 6), max(1, (rows * 2) // 3)
    wet_c0, wet_c1 = max(0, cols // 4), max(1, (cols * 3) // 4)
    arr[wet_r0:wet_r1, wet_c0:wet_c1] = wet_value
    arr[0, 0] = nodata
    arr[rows - 1, cols - 1] = nodata
    return arr


def write_frame(
    results_dir: str | Path,
    seq: int,
    values: np.ndarray,
    *,
    xll: float = SYNTH_XLL,
    yll: float = SYNTH_YLL,
    cellsize: float = SYNTH_CELL,
) -> Path:
    """写一帧 results/res-XXXX.wd。"""
    results_dir = Path(results_dir)
    return write_ascii_grid(results_dir / f"res-{seq:04d}.wd", values, xll=xll, yll=yll, cellsize=cellsize)


def make_run_dir(
    base: str | Path,
    seqs: Iterable[int] = (0, 1, 2),
    *,
    values_fn: Callable[[int], np.ndarray] | None = None,
    rows: int = SYNTH_ROWS,
    cols: int = SYNTH_COLS,
) -> Path:
    """构造合成 run 目录：base/results/res-XXXX.wd（+ 可选 max）。"""
    run_dir = Path(base)
    results = run_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    for seq in seqs:
        values = values_fn(seq) if values_fn else synthetic_depth(rows, cols)
        write_frame(results, seq, values)
    return run_dir


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def parent_root() -> Path:
    return PARENT_ROOT


@pytest.fixture(scope="session")
def config(project_root: Path):
    """真实项目配置（configs/default.yaml）；只读。"""
    from floodviz.config import load_config

    return load_config(project_root / "configs" / "default.yaml")


@pytest.fixture(scope="session")
def ascii_writer() -> Callable[..., Path]:
    return write_ascii_grid


@pytest.fixture(scope="session")
def depth_field() -> Callable[..., np.ndarray]:
    return synthetic_depth


@pytest.fixture(scope="session")
def frame_writer() -> Callable[..., Path]:
    return write_frame


@pytest.fixture(scope="session")
def run_builder() -> Callable[..., Path]:
    return make_run_dir


@pytest.fixture
def config_copy(tmp_path: Path) -> Callable[[], Path]:
    """把真实 configs/ 复制到临时目录，返回可修改的 configs/default.yaml 路径。

    复制后 `parent_root: ".."` 解析到 tmp_path（存在），因此不需要父项目真实数据。
    """

    def _copy() -> Path:
        target = tmp_path / "proj"
        shutil.copytree(PROJECT_ROOT / "configs", target / "configs")
        return target / "configs" / "default.yaml"

    return _copy


def patch_text(path: Path, old: str, new: str) -> None:
    """在文本文件里做一次精确替换；替换不生效直接报错（防止测试自己失效）。"""
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"测试变异目标不存在于 {path.name}：{old!r}")
    patched = text.replace(old, new, 1)
    if patched == text:  # pragma: no cover - 防御性
        raise AssertionError(f"测试变异未生效：{path.name} {old!r}")
    path.write_text(patched, encoding="utf-8")


def load_yaml_text(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def dump_json(path: Path, obj: Any) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_json(path: Path) -> Any:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def as_str_list(values: Sequence[Any]) -> list[str]:
    return [str(v) for v in values]


# ---------------------------------------------------------------- 集成测试夹具
# 说明：这些夹具必须放在 tests/ 根 conftest，不能放 tests/integration/conftest.py。
# pytest 以 basename 作为模块名导入 conftest（tests/ 无 __init__.py 时），
# 子目录再加一个 conftest.py 会遮蔽根 conftest 模块，导致 `from conftest import ...` 失败。

#: 小样本：久隆默认 run 前 3 帧
INTEGRATION_SMOKE_BUNDLE = "qa_smoke"
INTEGRATION_SMOKE_FRAMES = 3
#: 全量：久隆默认 run 21 帧
INTEGRATION_FULL_BUNDLE = "qa_jiulong_full"


@pytest.fixture(scope="session")
def integration_config():
    """真实配置（只读）。"""
    from floodviz.config import load_config

    return load_config(PROJECT_ROOT / "configs" / "default.yaml")


@pytest.fixture(scope="session")
def source_run_dir(integration_config) -> Path:
    """久隆默认 run 的父项目只读源目录。"""
    return integration_config.case("jiulong").run("scen_free_200m_sv6h").source_dir


def _prepare_dev_bundle(config, bundle_id: str, expected_frames: int | None = None, **kwargs) -> Path:
    """在 outputs/build/<bundle_id> 生成开发用包（不触碰正式 web_bundle）。

    若已存在且帧数符合预期则直接复用，避免与 pipeline 的全量构建并发写盘。
    """
    import dataclasses
    import json

    from floodviz.pipeline import prepare_bundle

    dev_config = dataclasses.replace(config, bundle_root=(config.root / "outputs" / "build").resolve())
    target = dev_config.bundle_root / bundle_id
    catalog_path = target / "catalog.json"
    if expected_frames is not None and catalog_path.is_file():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            run = catalog["cases"][0]["runs"][0]
            if int(run.get("frame_count", -1)) == expected_frames:
                return target
        except (KeyError, IndexError, ValueError):
            pass
    if catalog_path.is_file():
        shutil.rmtree(target, ignore_errors=True)
    return prepare_bundle(dev_config, bundle_id, **kwargs)


@pytest.fixture(scope="session")
def smoke_bundle(integration_config) -> Path:
    """久隆前 3 帧小样本包 → outputs/build/qa_smoke。"""
    return _prepare_dev_bundle(
        integration_config,
        INTEGRATION_SMOKE_BUNDLE,
        expected_frames=INTEGRATION_SMOKE_FRAMES,
        only=("jiulong",),
        frames_limit=INTEGRATION_SMOKE_FRAMES,
    )


@pytest.fixture(scope="session")
def full_bundle(integration_config) -> Path:
    """久隆全量 21 帧包 → outputs/build/qa_jiulong_full。"""
    return _prepare_dev_bundle(
        integration_config,
        INTEGRATION_FULL_BUNDLE,
        expected_frames=21,
        only=("jiulong",),
    )


# ---------------------------------------------------------------- e2e 夹具
# e2e 只读"可服务的发布包"：优先正式包 outputs/web_bundle/<id>（需已含 index.html），
# 否则回退到 qa 自建的 mock 包 outputs/build/qa_e2e_mock（web/.dev/mock_bundle 的副本
# + scripts/build_web.py 复制的 assets），绝不写入 outputs/web_bundle/**。

#: 正式发布包 id（SPEC §4 示例）；存在且含 index.html 时 e2e 用它
E2E_RELEASE_BUNDLE = "v6-20261029"
#: 最近几个发布包：e2e 只要其中一个可服务即可（优先最新 v6-20261029）
E2E_KNOWN_BUNDLES = ("v6-20261029", "v3-20261028", "v2-20261027")
#: qa 自建 mock 包（web 组 mock 数据 + 真实 build_web 产物）
E2E_MOCK_BUNDLE = "qa_e2e_mock"
E2E_MOCK_SOURCE = PROJECT_ROOT / "web" / ".dev" / "mock_bundle"


def _bundle_is_servable(path: Path) -> bool:
    return (path / "index.html").is_file() and (path / "assets" / "app.js").is_file()


@pytest.fixture(scope="session")
def e2e_bundle(project_root: Path) -> Path:
    """e2e 服务的发布包目录（只读）。

    可用环境变量 FLOODVIZ_E2E_BUNDLE 指定包名（回归对比旧包时用），
    否则按 E2E_KNOWN_BUNDLES 顺序取第一个可服务的发布包，最后回退 mock 包。
    """
    override = os.environ.get("FLOODVIZ_E2E_BUNDLE", "").strip()
    candidates = (override,) if override else E2E_KNOWN_BUNDLES
    for bundle_id in candidates:
        release = project_root / "outputs" / "web_bundle" / bundle_id
        if _bundle_is_servable(release):
            return release
    mock = project_root / "outputs" / "build" / E2E_MOCK_BUNDLE
    if _bundle_is_servable(mock):
        return mock
    return mock


@pytest.fixture(scope="session")
def e2e_is_release(e2e_bundle: Path) -> bool:
    return e2e_bundle.name in E2E_KNOWN_BUNDLES


E2E_PORT_DEFAULT = 8790


def _e2e_server_start(bundle: Path, port: int):
    """起回环静态服务（只读 bundle）；返回 (proc, url)。"""
    import socket
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"e2e 端口 {port} 被占用（8765 是交付端口、8788 是 web 组开发端口）：{exc}") from exc

    cmd = [
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--bind",
        "127.0.0.1",
        "--directory",
        str(bundle),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "index.html", timeout=2) as resp:
                if resp.status == 200:
                    return proc, url
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"e2e 静态服务未就绪：{url}")


@pytest.fixture(scope="session")
def e2e_server(e2e_bundle: Path):
    """(url, bundle_path)；会话级复用，退出时终止 http.server。"""
    import contextlib

    proc, url = _e2e_server_start(e2e_bundle, E2E_PORT_DEFAULT)
    try:
        yield url, e2e_bundle
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


@pytest.fixture(scope="session")
def e2e_url(e2e_server) -> str:
    return e2e_server[0]


@pytest.fixture(scope="session")
def e2e_browser(e2e_url: str):
    """会话级 Chromium 实例（headless，1920×1080 由各 page 的 context 决定）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--force-device-scale-factor=1"])
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def e2e_session(e2e_browser, e2e_url: str):
    """单个测试独占的浏览器上下文 + page，并提供控制台错误与本地 4xx 收集。"""
    context = e2e_browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
        device_scale_factor=1,
    )
    page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    local_4xx: list[str] = []
    network_failures: list[str] = []

    def on_console(msg):
        if msg.type != "error":
            return
        text = msg.text
        # 刻意断网 / 404 模拟会输出 Failed to load resource；只统计同源页面脚本错误
        if "Failed to load resource" in text:
            return
        console_errors.append(text)

    def on_pageerror(err):
        page_errors.append(str(err))

    def on_response(resp):
        url = resp.url
        if url.startswith(e2e_url) and resp.status >= 400:
            local_4xx.append(f"{resp.status} {url}")

    def on_request_failed(request):
        # 测试自己触发的 route 拦截不计入（见 network_failures 的用途说明）
        network_failures.append(f"{request.url} :: {request.failure}")

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)

    session = type(
        "E2ESession",
        (),
        {
            "context": context,
            "page": page,
            "url": e2e_url,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "local_4xx": local_4xx,
            "network_failures": network_failures,
        },
    )()
    try:
        yield session
    finally:
        context.close()


@pytest.fixture
def app(e2e_session):
    """已加载到首帧可交互的页面（AppPage 封装，见 tests/e2e/harness.py）。"""
    from harness import AppPage

    page = AppPage(e2e_session)
    page.open()
    return page


@pytest.fixture
def app_factory(e2e_session):
    """按需创建 AppPage（用于需要先注册 route 再加载的场景）。"""
    from harness import AppPage

    return lambda: AppPage(e2e_session)
