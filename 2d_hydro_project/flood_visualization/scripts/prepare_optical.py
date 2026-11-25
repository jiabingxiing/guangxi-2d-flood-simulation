"""准备三案例（久隆/宁明/贵港）光学与 SAR 显示资产，并登记状态。

只做显示与登记，不重跑模型、不改父项目数据。产出（相对子项目根）：

- ``data/processed/optical/<case>/<asset_id>.png``（RGBA，遮云/无数据透明，最长边 ≤4096）
- ``data/processed/optical/<case>/<asset_id>.json``（四角经纬度、覆盖统计、状态、许可署名）
- ``data/processed/optical/<case>/<asset_id>_footprint.geojson``
- ``data/processed/assets/sar/<asset_id>.png``（二值水体，模型网格 1:1）
- ``data/processed/assets/sar/<asset_id>.json`` + ``<asset_id>_footprint.geojson``
- ``data/processed/assets/manifest.json``（机器可读总清单，供 lead 合并 catalog）
- ``data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf`` + ``data/external/optical/README.md``
- ``docs/optical_assets.md``

用法::

    $env:PYTHONUTF8='1'
    uv run python scripts/prepare_optical.py                # 生成全部（已存在则跳过）
    uv run python scripts/prepare_optical.py --force        # 强制重建
    uv run python scripts/prepare_optical.py --layer optical --case ningming
    uv run python scripts/prepare_optical.py --download     # 远程场景整景下载后裁剪（兜底）

状态机只用：remote_verified / local_unchecked / ready / partial / missing / failed。
只有 ready/partial 可启用；失败写 failed 并记录原因，不虚报。

网络说明：AWS S3 匿名 COG 用 ``/vsicurl/`` 窗口读取。本机 GDAL 使用 Windows
schannel，无法打开位于非 ASCII 路径下的 certifi CA 证书包，因此默认关闭 SSL 校验
（``GDAL_HTTP_UNSAFESSL=YES``）；S3 数据为公开只读影像，且输出会与检索报告里已核验的
SCL 统计交叉核对。若远程读取仍失败，脚本自动整景下载到系统临时目录、裁剪后删除。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

GDAL_ENV_DEFAULTS = {
    # COG 远端读取：只读需要的数据块，不列目录
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_TIMEOUT": "180",
    "GDAL_HTTP_CONNECTTIMEOUT": "60",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    # 见模块 docstring：Windows schannel 打不开非 ASCII 路径下的 CA 包。
    # 公开只读数据 + SCL 统计交叉核对；如该配置被显式设为 NO，脚本走整景下载兜底。
    "GDAL_HTTP_UNSAFESSL": "YES",
}
for _k, _v in GDAL_ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import itertools  # noqa: E402

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.features import geometry_mask  # noqa: E402
from rasterio.transform import Affine  # noqa: E402
from rasterio.warp import reproject, transform_bounds, transform_geom  # noqa: E402
from rasterio.windows import Window, from_bounds  # noqa: E402
from rasterio.windows import bounds as window_bounds  # noqa: E402

SCRIPT_REL = "scripts/prepare_optical.py"
FV_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = FV_ROOT.parent
TASK_ROOT = PROJECT_ROOT.parent

# 真外部资源（源 DEM 衍生目录）：不写死盘符。按 env → 项目内默认解析。
# 子工程只读父项目，故此处独立实现解析，不反向 import 父项目 scripts。
def _resolve_optical_root_default() -> Path | None:
    env = os.environ.get("GX_OPTICAL_ROOT")
    if env:
        cand = Path(env)
        if cand.exists():
            return cand
    local = PROJECT_ROOT / "configs" / "paths.local.yaml"
    if local.is_file():
        try:
            import yaml

            doc = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            raw = doc.get("OPTICAL_ROOT") or doc.get("optical_root")
            if raw and Path(str(raw)).exists():
                return Path(str(raw))
        except (OSError, ValueError, ImportError):
            pass
    cand = PROJECT_ROOT / "data" / "raw" / "optical"
    return cand if cand.exists() else None


OPTICAL_ROOT = FV_ROOT / "data" / "processed" / "optical"
ASSETS_ROOT = FV_ROOT / "data" / "processed" / "assets"
SAR_ROOT = ASSETS_ROOT / "sar"
MANIFEST_PATH = ASSETS_ROOT / "manifest.json"
EXTERNAL_OPTICAL = FV_ROOT / "data" / "external" / "optical"
LICENSE_PDF_SRC = TASK_ROOT / "plans" / "visualization_research" / "Sentinel_Data_Legal_Notice.pdf"
LICENSE_PDF_DST = EXTERNAL_OPTICAL / "licenses" / "Sentinel_Data_Legal_Notice.pdf"
DOC_PATH = FV_ROOT / "docs" / "optical_assets.md"

SENTINEL_COG_BASE = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs"
STAC_ITEM_BASE = "https://earth-search.aws.element84.com/v1/collections/sentinel-s2-l2a/items"

STATUS_VALUES = ("remote_verified", "local_unchecked", "ready", "partial", "missing", "failed")
ENABLED_STATUSES = ("ready", "partial")
TRANSPARENT_SCL = (0, 3, 8, 9, 10)  # NoData / 云影 / 云(薄/中/厚)
CLOUD_SCL = (8, 9, 10)
CLEAR_SCL = (4, 5, 6)

SAR_WATER_RGB = (58, 134, 255)  # 与 configs/layers.yaml 的 binary_water 图例 #3a86ff 一致
SAR_WATER_ALPHA = 255
# 源 20 m/12.5 m 水体掩膜聚合到模型格后的水体判定阈值（与父项目 s1_to_grid: frac > 0.2 一致）
WATER_FRACTION_THRESHOLD = 0.2
# 光学/背景类显示图预算：长边 ≤2048 px、单图 ≤4 MB（lead 2026-10-27 要求；SAR 二值图不受限）
OPTICAL_MAX_EDGE = 2048
OPTICAL_MAX_BYTES = 4 * 1024 * 1024
OPTICAL_MIN_EDGE = 1024  # 收缩下限，避免为了压体积把背景降到不可用

SENTINEL_LICENSE = {
    "name": "Copernicus Sentinel Data Legal Notice（免费、开放、需署名；允许复制/传播/修改/组合）",
    "document": "data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf",
    "document_source": "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice",
    "redistribution": "allowed_with_attribution",
}

MODEL_RUNS = {
    "jiulong": {
        "run_id": "scen_free_200m_sv6h",
        "run_dir": PROJECT_ROOT / "outputs" / "jiulong" / "2024-09-yagi" / "scen_free_200m_sv6h",
        "crs": "EPSG:32649",
    },
    "ningming": {
        "run_id": "scen_q6000_n100_100m",
        "run_dir": PROJECT_ROOT / "outputs" / "ningming" / "2026-08-zitan" / "scen_q6000_n100_100m",
        "crs": "EPSG:32648",
    },
    "guigang": {
        "run_id": "scen_yagi_q11500_30m_b5",
        "run_dir": PROJECT_ROOT / "outputs" / "guigang" / "2024-09-yagi" / "scen_yagi_q11500_30m_b5",
        "crs": "EPSG:32649",
    },
}

OPTICAL_SCENES = {
    "ningming": {
        "asset_id": "ningming_s2_20260903",
        "scene_id": "S2A_48QYK_20260903_0_L2A",
        "tile_path": "48/Q/YK/2026/9/S2A_48QYK_20260903_0_L2A",
        "aoi_wgs84": [106.96, 22.06, 107.2, 22.26],
        "datetime_utc": "2026-09-03T03:32:14.867Z",
        "datetime_local": "2026-09-03T11:32:14+08:00",
        "data_year": 2026,
        "label": "可见光背景（Sentinel-2 L2A TCI，2026-09-03）",
        "role": "background",
        "summary": "Sentinel-2 L2A 真彩色，2026-09-03 过境；晚于 2026-08 洪峰，仅作背景，不是洪峰观测",
        "note": "采集时间不随模拟时间滑块变化；SCL 云/云影/NoData 透明",
        "expected": {
            "valid_coverage_percent": 100.0,
            "cloud_percent_of_valid": 0.0,
            "cloud_shadow_percent_of_valid": 0.0,
        },
        "evidence": "plans/宁明_贵港_公开可见光影像检索结果.md",
    },
    "guigang": {
        "asset_id": "guigang_s2_20241003",
        "scene_id": "S2B_49QCF_20241003_0_L2A",
        "tile_path": "49/Q/CF/2024/10/S2B_49QCF_20241003_0_L2A",
        "aoi_wgs84": [109.3, 22.98, 109.95, 23.32],
        "datetime_utc": "2024-10-03T03:21:43.556Z",
        "datetime_local": "2024-10-03T11:21:43+08:00",
        "data_year": 2024,
        "label": "可见光背景（Sentinel-2 L2A TCI，2024-10-03）",
        "role": "background",
        "summary": "Sentinel-2 L2A 真彩色，2024-10-03 过境；洪峰（09-12）后约 3 周，属事件后背景，不是洪峰观测",
        "note": "AOI 内约 3.16% NoData 透明空缺；不得宣称整域覆盖",
        "expected": {
            "valid_coverage_percent": 96.8428,
            "cloud_percent_of_valid": 0.0005,
            "cloud_shadow_percent_of_valid": 0.0,
        },
        "evidence": "plans/宁明_贵港_公开可见光影像检索结果.md",
    },
}

# 本地 RGB 源：媒体根由 _resolve_optical_root_default() 解析（env GX_OPTICAL_ROOT
# → configs/paths.local.yaml → data/raw/optical）；缺失时光学层降级为 missing，
# 不阻断发布。历史 D: 绝对路径已失效。
_OPTICAL_SRC_ROOT = _resolve_optical_root_default()
_JIU_RGB_REL = (
    Path("淹没分析 7.8") / "data" / "500kV久隆站附近"
    / "灾前可见光" / "SVN3-01_20260403_L4D0000293335_02_6042500742470001_RGB.tiff"
)

LOCAL_OPTICAL = {
    "jiulong": {
        "asset_id": "jiulong_rgb_20260403",
        "path": (_OPTICAL_SRC_ROOT / _JIU_RGB_REL) if _OPTICAL_SRC_ROOT else None,
        "datetime_local": "2026-04-03（仅日期，精确过境时刻未核实）",
        "label": "可见光背景（本地 RGB，2026-04-03）",
        "role": "background",
        "summary": "2026 年影像，非 2024 事件同期；本地商业 RGB（SVN3-01，EPSG:4326，4.7 GB），仅部分覆盖模型域",
        "note": "2026 年影像，非 2024 事件同期，仅部分覆盖；来源/传感器/许可未核实，仅内部研究展示",
    },
}

SAR_SOURCES = {
    "jiulong": {
        "asset_id": "jiulong_s1_20240917",
        "path": PROJECT_ROOT / "data" / "processed" / "s1_water" / "jiulong_20240917T105023_water20m.tif",
        "sigma_path": (
            PROJECT_ROOT / "data" / "processed" / "s1_water" / "jiulong_20240917T105023_sigma20m.tif"
        ),
        "sensor": "Sentinel-1A IW GRDH",
        "scene_id": "S1A_IW_GRDH_1SDV_20240917T105023_20240917T105048_055704_06CD90_3527",
        "acquired_utc": "2024-09-17T10:50:23Z",
        "acquired_local": "2024-09-17T18:50:23+08:00",
        "time_basis": "文件名 UTC 感测开始时刻（父项目 s1_water_extract.py 同口径），北京时间为 UTC+8",
        "observed_state": "退水后（事后）过境；父项目验证链判定为退水后证据",
        "label": "SAR 水体（S1 2024-09-17 退水后，二值掩膜）",
        "summary": "S1 20 m 提取的二值水体掩膜（σ⁰_VV 固定阈值 -17 dB，父项目 v2），渲染到模型网格；不是水深",
        "data_year": 2024,
    },
    "ningming": {
        "asset_id": "ningming_gf3_20260824",
        "path": PROJECT_ROOT / "data" / "interim" / "ningming" / "water_0824.tif",
        "sigma_path": None,
        "sensor": "GF-3（高分三号）SAR 水体掩膜（本地供方验证数据）",
        "scene_id": "GF3_8月24日水体_宁明县（本地 shapefile 栅格化）",
        "acquired_utc": None,
        "acquired_local": "2026-08-24（仅日期；精确过境时刻未核实，父项目登记 time_uncertainty_h=6）",
        "time_basis": "本地数据仅给出日期；父项目 gaps_risks G23 登记时刻不确定 ±6 h",
        "observed_state": "近洪峰；父项目主口径取 78 h 帧（8/24 08:00 中心 ±6 h）",
        "label": "SAR 水体（GF-3 2026-08-24 近洪峰，二值掩膜）",
        "summary": "GF-3 8/24 近洪峰水体掩膜（含常年水体），栅格化到 12.5 m 后渲染到模型网格；不是水深",
        "data_year": 2026,
    },
    "guigang": {
        "asset_id": "guigang_s1_20240912",
        "path": PROJECT_ROOT / "data" / "processed" / "s1_water" / "guigang_20240912T104222_water20m.tif",
        "sigma_path": (
            PROJECT_ROOT / "data" / "processed" / "s1_water" / "guigang_20240912T104222_sigma20m.tif"
        ),
        "sensor": "Sentinel-1A IW GRDH",
        "scene_id": "S1A_IW_GRDH_1SDV_20240912T104222_20240912T104247_055638_06CAF6_AE7D",
        "acquired_utc": "2024-09-12T10:42:22Z",
        "acquired_local": "2024-09-12T18:42:22+08:00",
        "time_basis": "文件名 UTC 感测开始时刻，北京时间为 UTC+8",
        "observed_state": "洪峰期（略早于站峰 09-12T14）",
        "label": "SAR 水体（S1 2024-09-12 洪峰期，二值掩膜）",
        "summary": (
            "S1 20 m 二值水体掩膜（σ⁰_VV -17 dB，父项目 v2），仅覆盖模型域东部一部分（约两成面积），"
            "必须显示足迹"
        ),
        "data_year": 2024,
    },
}

DISABLED_ASSETS = [
    {
        "case_id": "jiulong",
        "layer": "sar",
        "asset_id": "jiulong_local_sar_after_unknown",
        "path": (
            str(_OPTICAL_SRC_ROOT / "淹没分析 7.8" / "data" / "灾后SAR_久隆区域.tif")
            if _OPTICAL_SRC_ROOT else None
        ),
        "status": "local_unchecked",
        "enabled": False,
        "reason": "传感器/日期/处理链未核实；不得进入发布包，不得标 ready",
    },
]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[prepare_optical] {msg}", flush=True)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def rel(path: Path) -> str:
    """返回相对子项目根的 POSIX 路径（便于跨机器与 web 包使用）。"""
    try:
        return path.resolve().relative_to(FV_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def round_coord(value: float, ndigits: int = 7) -> float:
    return round(float(value), ndigits)


def rect_polygon_wgs84(west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }


def densified_ring_wgs84(crs: str, west: float, south: float, east: float, north: float, n: int = 32) -> list:
    """投影坐标矩形边界加密后转 WGS84，返回闭合环。"""
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    pts = []
    for i in range(n):
        pts.append((west + (east - west) * i / n, north))
    for i in range(n):
        pts.append((east, north - (north - south) * i / n))
    for i in range(n):
        pts.append((east - (east - west) * i / n, south))
    for i in range(n):
        pts.append((west, south + (north - south) * i / n))
    ring = [[round_coord(lon), round_coord(lat)] for lon, lat in (tr.transform(x, y) for x, y in pts)]
    ring.append(ring[0])
    return ring


def corners_wgs84(crs: str, west: float, south: float, east: float, north: float) -> list:
    """返回显示图四角 [[lon,lat]×4]：左上、右上、右下、左下。"""
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    order = [(west, north), (east, north), (east, south), (west, south)]
    return [[round_coord(lon), round_coord(lat)] for lon, lat in (tr.transform(x, y) for x, y in order)]


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_geojson(path: Path, ring: list, properties: dict) -> None:
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": properties,
    }
    payload = {
        "type": "FeatureCollection",
        "name": properties.get("asset_id", path.stem),
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [feature],
    }
    write_json(path, payload)


def write_geojson_multipolygon(path: Path, polygons: list, properties: dict) -> None:
    """多边形足迹。``polygons`` 是环列表，每个环 = [[lon,lat], ...]（已闭合）。

    GeoJSON 嵌套：Polygon = [环...]；MultiPolygon = [[环...], ...]。
    """
    geometry = (
        {"type": "Polygon", "coordinates": [polygons[0]]}
        if len(polygons) == 1
        else {"type": "MultiPolygon", "coordinates": [[ring] for ring in polygons]}
    )
    payload = {
        "type": "FeatureCollection",
        "name": properties.get("asset_id", path.stem),
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [{"type": "Feature", "geometry": geometry, "properties": properties}],
    }
    write_json(path, payload)


def _perpendicular_distance(point: list, start: list, end: list) -> float:
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy)
    if norm == 0.0:
        return math.hypot(px - ax, py - ay)
    return abs(dy * px - dx * py + bx * ay - by * ax) / norm


def simplify_ring(ring: list, tolerance: float) -> list:
    """Douglas–Peucker 简化闭合环（保留首尾闭合），去掉像元阶梯噪声。"""
    if len(ring) <= 4 or tolerance <= 0:
        return ring
    points = ring[:-1] if ring[0] == ring[-1] else list(ring)
    if len(points) <= 3:
        return ring
    # 闭合环从最远两点切成两段分别简化，避免把整圈压成一条线
    first = points[0]
    far_idx = max(range(len(points)), key=lambda i: math.hypot(points[i][0] - first[0],
                                                              points[i][1] - first[1]))
    kept: list[list] = [first]

    def _dp(seg: list) -> None:
        stack = [(0, len(seg) - 1)]
        keep = {0, len(seg) - 1}
        while stack:
            lo, hi = stack.pop()
            if hi - lo < 2:
                continue
            dist, idx = 0.0, lo
            for i in range(lo + 1, hi):
                d = _perpendicular_distance(seg[i], seg[lo], seg[hi])
                if d > dist:
                    dist, idx = d, i
            if dist > tolerance:
                keep.add(idx)
                stack.append((lo, idx))
                stack.append((idx, hi))
        for i in sorted(keep):
            if i > 0:
                kept.append(seg[i])

    _dp(points[: far_idx + 1])
    _dp([*points[far_idx:], first])
    if kept[-1] != first:
        kept.append(first)
    return kept


def data_footprint_polygons(valid: np.ndarray, extent_wgs84: list, min_pixels: int = 64) -> list:
    """从显示图有效像元掩膜提取真实数据足迹外环（WGS84，按面积从大到小）。

    用于非矩形覆盖的影像（如本地旋转条带），避免用外接矩形冒充数据范围。
    只保留外环（不含内洞），按最小像元数过滤碎片，并用 Douglas–Peucker
    以 1 像素容差去掉像元阶梯噪声。
    """
    from rasterio import features

    west, south, east, north = extent_wgs84
    height, width = valid.shape
    transform = Affine((east - west) / width, 0.0, west, 0.0, -(north - south) / height, north)
    px_area = abs(transform.a * transform.e)
    min_area = px_area * min_pixels
    tolerance = min(abs(transform.a), abs(transform.e))  # 1 像素容差
    found: list[tuple[float, list]] = []
    for geom, value in features.shapes(valid.astype("uint8"), mask=valid, transform=transform):
        if not value:
            continue
        polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else list(geom["coordinates"])
        for rings in polygons:
            ring = rings[0]  # 只取外环
            area = 0.0
            for (x1, y1), (x2, y2) in itertools.pairwise(ring):
                area += x1 * y2 - x2 * y1
            area = abs(area) / 2.0
            if area < min_area:
                continue
            simplified = simplify_ring([[float(x), float(y)] for x, y in ring], tolerance)
            if len(simplified) < 4:
                continue
            found.append((area, [[round_coord(x), round_coord(y)] for x, y in simplified]))
    found.sort(key=lambda item: item[0], reverse=True)
    return [ring for _area, ring in found]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_rgba_png(path: Path, rgba: np.ndarray) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(np.ascontiguousarray(rgba, dtype=np.uint8), mode="RGBA")
    img.save(path, format="PNG", optimize=True)
    with Image.open(path) as check:
        width, height = check.size
        if (width, height) != (rgba.shape[1], rgba.shape[0]):
            raise RuntimeError(f"PNG size mismatch: {width}x{height} != {rgba.shape[1]}x{rgba.shape[0]}")
    return {
        "width": int(rgba.shape[1]),
        "height": int(rgba.shape[0]),
        "bytes": int(path.stat().st_size),
        "sha256": sha256_file(path),
    }


def display_size_for(native_w: int, native_h: int, max_edge: int) -> tuple[int, int, float]:
    """按最长边上限等比缩放，返回 (out_w, out_h, 缩放比)；缩放比 1.0 表示 1:1。"""
    longest = max(native_w, native_h)
    scale = 1.0 if longest <= max_edge else max_edge / float(longest)
    out_w = max(1, round(native_w * scale))
    out_h = max(1, round(native_h * scale))
    return out_w, out_h, scale


def reusable_asset(json_path: Path, args, layer: str) -> dict | None:
    """判断已有资产能否复用（幂等重跑）。返回已存的 entry 或 None 需要重建。"""
    if args.force or not json_path.is_file():
        return None
    try:
        entry = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    png_path = FV_ROOT / entry.get("png", {}).get("path", "")
    if not png_path.is_file():
        return None
    if layer == "optical":
        png = entry.get("png", {})
        if png.get("max_edge_px") != args.max_edge:
            return None  # 尺寸策略变了，必须重导出
        if int(png.get("bytes") or 0) > OPTICAL_MAX_BYTES:
            return None  # 旧产物超预算
        if max(int(png.get("width") or 0), int(png.get("height") or 0)) > args.max_edge:
            return None
    else:
        if entry.get("sar_mode") != args.sar_mode:
            return None
        if entry.get("source", {}).get("path") != str(
            (SAR_SOURCES[entry["case_id"]]["sigma_path"] if args.sar_mode == "backscatter"
             else SAR_SOURCES[entry["case_id"]]["path"]).replace("\\", "/")
        ):
            return None
    return entry


def _rescale_rgba(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    return np.asarray(
        Image.fromarray(np.ascontiguousarray(rgba, dtype="uint8"), mode="RGBA").resize(
            (width, height), Image.Resampling.LANCZOS
        )
    )


def _png_size_bytes(rgba: np.ndarray) -> int:
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba, dtype="uint8"), mode="RGBA").save(
        buffer, format="PNG", optimize=True
    )
    return buffer.tell()


def save_rgba_png_with_budget(
    path: Path,
    rgba: np.ndarray,
    *,
    max_bytes: int = OPTICAL_MAX_BYTES,
    min_edge: int = OPTICAL_MIN_EDGE,
    resample_name: str = "area",
    bisect_rounds: int = 4,
) -> dict:
    """保存 PNG；超体积预算时二分查找"能装下预算的最大尺寸"（保留 alpha）。

    返回的 meta 含 width/height/bytes/sha256 以及 source_width/source_height/
    resample/bisect_rounds/size_budget_bytes/scale，供 catalog 与文档如实登记。
    缩小只改变像素密度，不改变四角经纬度（地理范围不变）。
    """
    source_w, source_h = int(rgba.shape[1]), int(rgba.shape[0])
    meta = save_rgba_png(path, rgba)
    base_meta = {"source_width": source_w, "source_height": source_h, "resample": resample_name,
                 "size_budget_bytes": max_bytes}
    if meta["bytes"] <= max_bytes:
        meta.update({**base_meta, "bisect_rounds": 0, "scale": 1.0, "budget_met": True})
        return meta

    floor_scale = min(1.0, min_edge / float(max(source_w, source_h)))
    floor_w = max(1, round(source_w * floor_scale))
    floor_h = max(1, round(source_h * floor_scale))
    floor_rgba = _rescale_rgba(rgba, floor_w, floor_h)
    if _png_size_bytes(floor_rgba) > max_bytes:
        log(f"  警告：缩到长边 {min_edge} px 仍超预算 {max_bytes / 2**20:.1f} MiB，按该尺寸落盘")
        meta = save_rgba_png(path, floor_rgba)
        meta.update({**base_meta, "bisect_rounds": 0, "scale": round(floor_scale, 4), "budget_met": False})
        return meta

    lo, hi = floor_scale, 1.0
    best = floor_rgba
    rounds = 0
    for _ in range(bisect_rounds):
        rounds += 1
        mid = (lo + hi) / 2.0
        candidate = _rescale_rgba(rgba, max(1, round(source_w * mid)),
                                  max(1, round(source_h * mid)))
        if _png_size_bytes(candidate) <= max_bytes:
            best = candidate
            lo = mid
        else:
            hi = mid
        if hi - lo <= 0.02:
            break
    meta = save_rgba_png(path, best)
    meta.update({**base_meta, "bisect_rounds": rounds, "scale": round(lo, 4),
                 "budget_met": meta["bytes"] <= max_bytes})
    log(
        f"  压体积：{source_w}x{source_h} → {meta['width']}x{meta['height']}"
        f"（{meta['bytes'] / 2**20:.2f} MiB ≤ {max_bytes / 2**20:.1f} MiB，二分 {rounds} 轮）"
    )
    return meta


# --------------------------------------------------------------------------- #
# 模型网格
# --------------------------------------------------------------------------- #
def read_model_grid(case_id: str) -> dict:
    """从父项目 run 目录的首个 res-*.wd 头读出模型网格（只读）。"""
    spec = MODEL_RUNS[case_id]
    run_dir = spec["run_dir"]
    results = run_dir / "results"
    if not results.is_dir():
        raise FileNotFoundError(f"模型结果目录不存在: {results}")
    frames = sorted(results.glob("res-*.wd"))
    if not frames:
        raise FileNotFoundError(f"未找到 res-*.wd: {results}")
    header: dict[str, float] = {}
    for line in frames[0].read_text(encoding="utf-8", errors="replace").splitlines()[:6]:
        parts = line.split()
        if len(parts) >= 2:
            header[parts[0].lower()] = float(parts[1])
    rows = int(header["nrows"])
    cols = int(header["ncols"])
    cell = float(header["cellsize"])
    xll = float(header["xllcorner"])
    yll = float(header["yllcorner"])
    affine = Affine(cell, 0.0, xll, 0.0, -cell, yll + rows * cell)
    crs = spec["crs"]
    west, south, east, north = xll, yll, xll + cols * cell, yll + rows * cell
    bounds_wgs84 = [
        round_coord(v, 6)
        for v in transform_bounds(crs, "EPSG:4326", west, south, east, north, densify_pts=21)
    ]
    return {
        "case_id": case_id,
        "run_id": spec["run_id"],
        "run_dir_rel": rel(run_dir),
        "crs": crs,
        "rows": rows,
        "cols": cols,
        "cell_size_m": cell,
        "cell_area_m2": cell * cell,
        "affine": affine,
        "transform": [affine.a, affine.b, affine.c, affine.d, affine.e, affine.f],
        "native_bounds": [west, south, east, north],
        "bounds_wgs84": bounds_wgs84,
        "grid_cells": rows * cols,
        "grid_area_km2": rows * cols * cell * cell / 1e6,
        "frame_files": len(frames),
        "header_file": rel(frames[0]),
        "nodata": header.get("nodata_value"),
    }


def grid_slice(affine: Affine, row0: int, row1: int, col0: int, col1: int) -> list:
    """格网子矩形（half-open）在模型 CRS 中的 [west, south, east, north]。"""
    west = affine.c + col0 * affine.a
    east = affine.c + col1 * affine.a
    north = affine.f + row0 * affine.e
    south = affine.f + row1 * affine.e
    return [west, south, east, north]


# --------------------------------------------------------------------------- #
# 远程 COG（/vsicurl，失败则整景下载到临时目录）
# --------------------------------------------------------------------------- #
def download_whole(url: str, temp_dir: Path) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + "_" + Path(urlparse(url).path).name
    dest = temp_dir / name
    free = shutil.disk_usage(temp_dir).free
    log(f"下载整景兜底: {url} -> {dest}（临时目录剩余 {free / 2**30:.1f} GiB）")
    request = urllib.request.Request(url, headers={"User-Agent": "floodviz-prepare-optical/1.0"})
    started = time.time()
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as fh:
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
            if total % (32 * 1024 * 1024) < 1024 * 1024:
                log(f"  已下载 {total / 2**20:.0f} MiB")
    log(f"下载完成 {dest.stat().st_size / 2**20:.0f} MiB，用时 {time.time() - started:.0f} s")
    return dest


@contextlib.contextmanager
def open_remote_cog(url: str, *, temp_dir: Path, label: str, force_download: bool):
    """打开远端 COG；打开失败时整景下载到临时目录并在退出时删除。"""
    if force_download:
        local = download_whole(url, temp_dir)
        try:
            with rasterio.open(local) as dataset:
                yield dataset
        finally:
            with contextlib.suppress(OSError):
                local.unlink()
        return
    try:
        dataset = rasterio.open(url)
    except Exception as exc:
        log(f"  /vsicurl 打开失败（{label}）: {type(exc).__name__}: {exc}；改用整景下载")
        local = download_whole(url, temp_dir)
        try:
            with rasterio.open(local) as dataset:
                yield dataset
        finally:
            with contextlib.suppress(OSError):
                local.unlink()
        return
    try:
        yield dataset
    finally:
        dataset.close()


# --------------------------------------------------------------------------- #
# 光学资产
# --------------------------------------------------------------------------- #
def _scl_full_res_stats(scl_ds, aoi: list, proj_bounds: list) -> dict:
    """全分辨率 20 m SCL 按 AOI 多边形统计（与检索报告同口径）。"""
    west, south, east, north = aoi
    geom = transform_geom("EPSG:4326", scl_ds.crs, rect_polygon_wgs84(west, south, east, north))
    win = from_bounds(*proj_bounds, scl_ds.transform)
    col0 = math.floor(win.col_off)
    row0 = math.floor(win.row_off)
    col1 = math.ceil(win.col_off + win.width)
    row1 = math.ceil(win.row_off + win.height)
    win_int = Window(col0, row0, max(1, col1 - col0), max(1, row1 - row0))
    arr = scl_ds.read(1, window=win_int, boundless=True, fill_value=0)
    inside = geometry_mask([geom], arr.shape, scl_ds.window_transform(win_int), invert=True)
    values = arr[inside]
    total = int(values.size)
    counts = {str(i): int(np.count_nonzero(values == i)) for i in range(12)}
    valid = int(np.count_nonzero(values != 0))
    cloud = sum(counts[str(i)] for i in CLOUD_SCL)
    shadow = counts["3"]
    return {
        "method": "full-resolution 20 m SCL; AOI polygon; pixel centers (与检索报告同口径)",
        "scl_resolution_m": list(scl_ds.res),
        "aoi_pixels": total,
        "valid_pixels": valid,
        "valid_coverage_percent": round(100.0 * valid / total, 4) if total else 0.0,
        "nodata_pixels": counts["0"],
        "nodata_percent_of_aoi": round(100.0 * counts["0"] / total, 4) if total else 0.0,
        "cloud_pixels": cloud,
        "cloud_percent_of_valid": round(100.0 * cloud / valid, 4) if valid else 0.0,
        "cloud_shadow_pixels": shadow,
        "cloud_shadow_percent_of_valid": round(100.0 * shadow / valid, 4) if valid else 0.0,
        "cloud_percent_of_aoi": round(100.0 * cloud / total, 4) if total else 0.0,
        "clear_456_percent_of_aoi": round(100.0 * sum(counts[str(i)] for i in CLEAR_SCL) / total, 4)
        if total
        else 0.0,
        "scl_counts": counts,
    }


def prepare_remote_optical(case_id: str, scene: dict, grid: dict, args) -> dict:
    asset_id = scene["asset_id"]
    scene_dir = f"{SENTINEL_COG_BASE}/{scene['tile_path']}"
    tci_url = f"{scene_dir}/TCI.tif"
    scl_url = f"{scene_dir}/SCL.tif"
    stac_url = f"{STAC_ITEM_BASE}/{scene['scene_id']}"
    out_png = OPTICAL_ROOT / case_id / f"{asset_id}.png"
    out_json = OPTICAL_ROOT / case_id / f"{asset_id}.json"
    out_footprint = OPTICAL_ROOT / case_id / f"{asset_id}_footprint.geojson"

    if out_png.exists() and out_json.exists() and not args.force:
        cached = reusable_asset(out_json, args, "optical")
        if cached is not None:
            log(f"复用已存在的 optical 资产: {rel(out_png)}")
            return cached
        log(f"已有 optical 资产不满足当前参数（--max-edge {args.max_edge} 或体积预算），重建: "
            f"{rel(out_png)}")

    aoi = scene["aoi_wgs84"]
    log(f"[optical] {case_id} {scene['scene_id']} 读取 TCI/SCL …")
    with (
        open_remote_cog(tci_url, temp_dir=args.temp_dir, label="TCI", force_download=args.download) as tci,
        open_remote_cog(scl_url, temp_dir=args.temp_dir, label="SCL", force_download=args.download) as scl,
    ):
        proj_bounds = list(transform_bounds("EPSG:4326", tci.crs, *aoi, densify_pts=21))
        win = from_bounds(*proj_bounds, tci.transform)
        col0 = max(0.0, win.col_off)
        row0 = max(0.0, win.row_off)
        col1 = min(float(tci.width), win.col_off + win.width)
        row1 = min(float(tci.height), win.row_off + win.height)
        if col1 - col0 <= 0 or row1 - row0 <= 0:
            raise RuntimeError(f"AOI 与场景 {scene['scene_id']} 不相交")
        win = Window(col0, row0, col1 - col0, row1 - row0)
        pixel_extent = list(window_bounds(win, tci.transform))  # left, bottom, right, top

        native_w = round(win.width)
        native_h = round(win.height)
        out_w, out_h, display_scale = display_size_for(native_w, native_h, args.max_edge)
        resampling = Resampling.average if display_scale < 1.0 else Resampling.nearest
        tci_rgb = tci.read([1, 2, 3], window=win, out_shape=(out_h, out_w), resampling=resampling)

        scl_win = from_bounds(*pixel_extent, scl.transform)
        scl_disp = scl.read(
            1,
            window=scl_win,
            out_shape=(out_h, out_w),
            resampling=Resampling.nearest,
            boundless=True,
            fill_value=0,
        )
        stats = _scl_full_res_stats(scl, aoi, proj_bounds)
        tci_crs = str(tci.crs)

    transparent = np.isin(scl_disp, TRANSPARENT_SCL)
    rgba = np.zeros((out_h, out_w, 4), dtype="uint8")
    rgba[..., 0] = tci_rgb[0]
    rgba[..., 1] = tci_rgb[1]
    rgba[..., 2] = tci_rgb[2]
    rgba[..., 3] = np.where(transparent, 0, 255).astype("uint8")
    rgba[transparent, :3] = 0
    png_meta = save_rgba_png_with_budget(out_png, rgba, resample_name="average+nearest_scl")
    # 显示图若为压体积再次缩小，透明像元统计要按落盘尺寸重算
    if png_meta["width"] != out_w or png_meta["height"] != out_h:
        resized_scl = np.asarray(
            Image.fromarray(transparent.astype("uint8") * 255, mode="L").resize(
                (png_meta["width"], png_meta["height"]), Image.Resampling.NEAREST
            )
        )
        transparent = resized_scl > 127
        out_w, out_h = png_meta["width"], png_meta["height"]

    coverage_pct = stats["valid_coverage_percent"]
    cloud_pct = stats["cloud_percent_of_valid"]
    status = "ready" if coverage_pct >= 99.5 and cloud_pct <= 1.0 else "partial"
    checks = {}
    for key, expected in scene["expected"].items():
        delta = round(stats[key] - expected, 4)
        checks[key] = {"expected_from_plan": expected, "measured": stats[key], "delta": delta,
                       "tolerance": 1.0, "pass": abs(delta) <= 1.0}
    cross_check_failed = [k for k, v in checks.items() if not v["pass"]]

    transparent_px = int(np.count_nonzero(transparent))
    acquisition = {
        "datetime_utc": scene["datetime_utc"],
        "datetime_local": scene["datetime_local"],
        "time_zone": "Asia/Shanghai",
        "time_basis": "STAC 条目的 datetime（UTC），北京时间为 UTC+8",
    }
    entry = {
        "schema_version": "1.0",
        "case_id": case_id,
        "layer": "optical",
        "asset_id": asset_id,
        "kind": "raster",
        "status": status,
        "enabled": status in ENABLED_STATUSES,
        "role": scene["role"],
        "label": scene["label"],
        "summary": scene["summary"],
        "note": scene["note"],
        "source": {
            "type": "sentinel2_l2a_cog",
            "scene_id": scene["scene_id"],
            "provider": "AWS Open Data: sentinel-2-l2a-cogs（匿名 HTTPS）",
            "source_url": tci_url,
            "scl_url": scl_url,
            "stac_item_url": stac_url,
            "source_resolution_m": {"tci": 10.0, "scl": 20.0},
            "evidence": scene["evidence"],
        },
        "acquisition": acquisition,
        "aoi": {
            "model_aoi_wgs84": aoi,
            "clip_extent_wgs84": [round_coord(v) for v in transform_bounds(tci.crs, "EPSG:4326",
                                                                          *pixel_extent, densify_pts=21)],
        },
        "png": {
            "path": rel(out_png),
            **png_meta,
            "crs": tci_crs,
            "pixel_extent_projected": [round(v, 2) for v in pixel_extent],
            "coordinates_tl_tr_br_bl": corners_wgs84(tci_crs, *pixel_extent),
            "native_clip_pixels": {"width": native_w, "height": native_h},
            "source_resolution_m": 10.0,
            "display_resolution_m": round(native_w * 10.0 / png_meta["width"], 2),
            "resampling": "average（TCI 缩采样）+ nearest（SCL 掩膜）",
            "display_scale": round(display_scale, 4),
            "max_edge_px": args.max_edge,
            "note": "缩小仅改变像素密度，四角经纬度仍为整幅裁剪范围",
        },
        "footprint_geojson": rel(out_footprint),
        "coverage": {
            **stats,
            "display_pixels": {"total": out_h * out_w, "transparent": transparent_px,
                               "transparent_percent": round(100.0 * transparent_px / (out_h * out_w), 4)},
            "alpha_source": "SCL classification resampled nearest to display grid",
        },
        "mask": {
            "transparent_scl_classes": list(TRANSPARENT_SCL),
            "transparent_class_meaning": {
                "0": "NoData", "3": "云影", "8": "云（中概率）", "9": "云（高概率）", "10": "薄卷云",
            },
            "clear_classes_kept": [4, 5, 6, 7, 11, 1, 2],
            "note": "只把 0/3/8/9/10 置透明；其余类别保留，未做人工云判读",
        },
        "license": {
            **SENTINEL_LICENSE,
            "attribution_original": f"Copernicus Sentinel data {scene['data_year']}",
            "attribution_modified": f"Contains modified Copernicus Sentinel data {scene['data_year']}",
            "modification_note": "裁剪到模型 AOI、重采样显示尺寸、按 SCL 云/云影/NoData 置透明（RGB 值未做拉伸）",
        },
        "qa": {
            "cross_check": checks,
            "cross_check_failed": cross_check_failed,
            "manual_visual_review": "未执行（未做人工目视验收）",
            "status_rule": "valid_coverage_percent >= 99.5 且 cloud_percent_of_valid <= 1.0 → ready；否则 partial",
            "status_reason": (
                f"AOI 有效覆盖 {coverage_pct}%，有效像元云占比 {cloud_pct}% → "
                + ("ready" if status == "ready" else "partial（存在 NoData/云空缺）")
            ),
        },
        "catalog_target": {
            "image": f"frames/{case_id}/optical/{asset_id}.png",
            "footprint": f"cases/{case_id}/optical_footprint_{asset_id}.geojson",
        },
        "generated_utc": utc_now_iso(),
        "command": f"uv run python {SCRIPT_REL} --layer optical --case {case_id}",
        "notes": [
            "采集时间不随模拟时间滑块变化；本景不是洪峰同期观测。",
            "SCL 为自动分类，不等同人工云检测；未对残留薄云做人工剔除。",
            "PNG 是裁剪后的显示图，原始整景未入库。",
        ],
    }
    if cross_check_failed:
        entry["notes"].append("SCL 统计与检索报告不一致，需人工复核：" + ", ".join(cross_check_failed))
    write_geojson(
        out_footprint,
        densified_ring_wgs84(tci_crs, *pixel_extent),
        {
            "case_id": case_id,
            "layer": "optical",
            "asset_id": asset_id,
            "role": "data_footprint",
            "crs": "EPSG:4326",
            "frame_crs": tci_crs,
            "acquisition": scene["datetime_local"],
            "status": status,
            "note": "本资产显示范围足迹；SCL 云/云影/NoData 像元在图内透明",
        },
    )
    write_json(out_json, entry)
    log(
        f"  完成 optical/{case_id}/{asset_id}: {png_meta['width']}x{png_meta['height']}px, "
        f"{png_meta['bytes'] / 2**20:.2f} MiB, 状态 {status}, 覆盖 {coverage_pct}%"
    )
    return entry


def prepare_local_optical(case_id: str, spec: dict, grid: dict, args) -> dict:
    asset_id = spec["asset_id"]
    src_path: Path | None = spec["path"]
    if src_path is None:
        # 外部影像根未配置：降级为 missing，不阻断发布（契约要求不得假装 ready）
        raise FileNotFoundError(
            f"本地影像根未配置，光学层降级 missing：{asset_id}。\n"
            "  设置 GX_OPTICAL_ROOT=<含『淹没分析 7.8』的目录>，\n"
            "  或把影像放到 2d_hydro_project/data/raw/optical/"
        )
    source_label = src_path.name
    out_png = OPTICAL_ROOT / case_id / f"{asset_id}.png"
    out_json = OPTICAL_ROOT / case_id / f"{asset_id}.json"
    out_footprint = OPTICAL_ROOT / case_id / f"{asset_id}_footprint.geojson"
    if out_png.exists() and out_json.exists() and not args.force:
        cached = reusable_asset(out_json, args, "optical")
        if cached is not None:
            log(f"复用已存在的 optical 资产: {rel(out_png)}")
            return cached
        log(f"已有 optical 资产不满足当前参数（--max-edge {args.max_edge} 或体积预算），重建: "
            f"{rel(out_png)}")
    if not src_path.is_file():
        raise FileNotFoundError(f"本地影像不存在: {src_path}")

    with rasterio.open(src_path) as src:
        src_bounds = list(transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21))
        model = grid["bounds_wgs84"]
        clip = [
            max(src_bounds[0], model[0]),
            max(src_bounds[1], model[1]),
            min(src_bounds[2], model[2]),
            min(src_bounds[3], model[3]),
        ]
        if clip[2] <= clip[0] or clip[3] <= clip[1]:
            raise RuntimeError(f"本地影像 {source_label} 与模型域不相交")
        win = from_bounds(*clip, src.transform)
        native_w = round(win.width)
        native_h = round(win.height)
        out_w, out_h, display_scale = display_size_for(native_w, native_h, args.max_edge)
        rgb = src.read([1, 2, 3], window=win, out_shape=(out_h, out_w), resampling=Resampling.average)
        src_crs = str(src.crs)
        src_nodata = src.nodata
        src_masks = src.read_masks(1, window=win, out_shape=(out_h, out_w), resampling=Resampling.nearest)

    valid = (rgb[0] != 0) | (rgb[1] != 0) | (rgb[2] != 0)
    valid &= src_masks > 0
    rgba = np.zeros((out_h, out_w, 4), dtype="uint8")
    rgba[..., 0] = rgb[0]
    rgba[..., 1] = rgb[1]
    rgba[..., 2] = rgb[2]
    rgba[..., 3] = np.where(valid, 255, 0).astype("uint8")
    rgba[~valid, :3] = 0
    png_meta = save_rgba_png_with_budget(out_png, rgba, resample_name="average")
    if png_meta["width"] != out_w or png_meta["height"] != out_h:
        resized = np.asarray(
            Image.fromarray(valid.astype("uint8") * 255, mode="L").resize(
                (png_meta["width"], png_meta["height"]), Image.Resampling.NEAREST
            )
        )
        valid = resized > 127
        out_w, out_h = png_meta["width"], png_meta["height"]

    # 真实覆盖：把显示图有效像元掩膜重投影到模型网格（不用外接矩形冒充覆盖）
    display_transform = Affine(
        (clip[2] - clip[0]) / out_w, 0.0, clip[0], 0.0, -(clip[3] - clip[1]) / out_h, clip[3]
    )
    cell_cover = np.full((grid["rows"], grid["cols"]), np.nan, dtype="float32")
    reproject(
        source=valid.astype("float32"),
        destination=cell_cover,
        src_transform=display_transform,
        src_crs="EPSG:4326",
        src_nodata=None,
        dst_transform=grid["affine"],
        dst_crs=grid["crs"],
        dst_nodata=np.nan,
        resampling=Resampling.average,
        num_threads=2,
    )
    cell_cover = np.nan_to_num(cell_cover, nan=0.0)
    covered = cell_cover > 0.5
    covered_cells = int(np.count_nonzero(covered))
    coverage_pct = round(100.0 * covered_cells / grid["grid_cells"], 3)
    transparent_px = int(np.count_nonzero(~valid))
    status = "partial" if coverage_pct < 99.5 else "ready"
    footprint_polygons = data_footprint_polygons(valid, clip)

    entry = {
        "schema_version": "1.0",
        "case_id": case_id,
        "layer": "optical",
        "asset_id": asset_id,
        "kind": "raster",
        "status": status,
        "enabled": status in ENABLED_STATUSES,
        "role": spec["role"],
        "label": spec["label"],
        "summary": spec["summary"],
        "note": spec["note"],
        "source": {
            "type": "local_geotiff",
            "path": str(src_path).replace("\\", "/"),
            "file_name": source_label,
            "file_bytes": src_path.stat().st_size,
            "crs": src_crs,
            "source_resolution_deg": [abs(float(src.res[0])), abs(float(src.res[1]))],
            "nodata": src_nodata,
            "provenance_note": "本地项目影像（SVN3-01 命名）；卫星/传感器/许可/处理链未核实",
        },
        "acquisition": {
            "datetime_local": spec["datetime_local"],
            "time_zone": "Asia/Shanghai",
            "time_basis": "文件名日期 20260403；精确过境时刻未核实",
            "date_only": True,
        },
        "aoi": {
            "model_bbox_wgs84": grid["bounds_wgs84"],
            "clip_extent_wgs84": [round_coord(v) for v in clip],
            "source_bounds_wgs84": [round_coord(v) for v in src_bounds],
            "note": "仅保留与模型域 WGS84 包络相交的部分",
        },
        "png": {
            "path": rel(out_png),
            **png_meta,
            "crs": "EPSG:4326",
            "coordinates_tl_tr_br_bl": [
                [round_coord(clip[0]), round_coord(clip[3])],
                [round_coord(clip[2]), round_coord(clip[3])],
                [round_coord(clip[2]), round_coord(clip[1])],
                [round_coord(clip[0]), round_coord(clip[1])],
            ],
            "native_clip_pixels": {"width": native_w, "height": native_h},
            "source_resolution_deg": round(abs(float(src.res[0])), 9),
            "source_resolution_m_approx": round(abs(float(src.res[0])) * 111_320.0, 2),
            "display_scale": round(display_scale, 4),
            "max_edge_px": args.max_edge,
            "note": "缩小仅改变像素密度，四角经纬度仍为整幅裁剪范围",
        },
        "coverage": {
            "coverage_percent_of_model_grid": coverage_pct,
            "covered_model_cells": covered_cells,
            "model_grid_cells": grid["grid_cells"],
            "model_grid_area_km2": grid["grid_area_km2"],
            "covered_area_km2": round(covered_cells * grid["cell_area_m2"] / 1e6, 3),
            "display_pixels": {
                "total": out_h * out_w,
                "transparent": transparent_px,
                "transparent_percent": round(100.0 * transparent_px / (out_h * out_w), 4),
            },
            "footprint_polygon_count": len(footprint_polygons),
            "alpha_source": "声明 nodata=0 + 掩膜（全 0 像元透明）；无云/云影分类可用",
        },
        "footprint_geojson": rel(out_footprint),
        "mask": {
            "transparent_pixels": "输入 nodata=0（全 0 像元）与掩膜无效像元",
            "note": "本地影像无 SCL 类云分类；未做云/云影遮罩，透明只表示无数据",
        },
        "license": {
            "name": "未核实（本地项目影像，来源/授权待确认）",
            "document": None,
            "redistribution": "unknown_internal_only",
            "attribution_original": "本地项目影像（SVN3-01），来源未核实",
            "attribution_modified": "本地项目影像（SVN3-01）裁剪，来源未核实",
            "modification_note": "裁剪到模型域包络、重采样显示尺寸；来源/许可未核实，公开发布前须确认",
        },
        "qa": {
            "manual_visual_review": "未执行（未做人工目视验收）",
            "status_rule": "覆盖模型网格中心像元比例 < 99.5% → partial",
            "status_reason": f"仅覆盖模型域 {coverage_pct}%（{covered_cells}/{grid['grid_cells']} 格）→ partial",
        },
        "catalog_target": {
            "image": f"frames/{case_id}/optical/{asset_id}.png",
            "footprint": f"cases/{case_id}/optical_footprint_{asset_id}.geojson",
        },
        "generated_utc": utc_now_iso(),
        "command": f"uv run python {SCRIPT_REL} --layer optical --case {case_id}",
        "notes": [
            "2026-04 背景，非 2024 事件同期（模型事件为 2024-09）。",
            "只覆盖模型域一部分，状态 partial；不得当整域背景。",
            "本地影像来源/传感器/许可未核实；仅限内部研究展示，公开再分发前须确认。",
            "整景 4.7 GB 未入库，只保留本裁剪小图。",
        ],
    }
    if footprint_polygons:
        write_geojson_multipolygon(
            out_footprint,
            footprint_polygons,
            {
                "case_id": case_id,
                "layer": "optical",
                "asset_id": asset_id,
                "role": "data_footprint",
                "crs": "EPSG:4326",
                "acquisition": spec["datetime_local"],
                "status": status,
                "coverage_percent_of_model_grid": coverage_pct,
                "polygon_count": len(footprint_polygons),
                "note": "按显示图有效像元反算的真实数据足迹（外环）；不是外接矩形",
            },
        )
    else:
        write_geojson(
            out_footprint,
            densified_ring_wgs84("EPSG:4326", *clip),
            {
                "case_id": case_id,
                "layer": "optical",
                "asset_id": asset_id,
                "role": "data_footprint",
                "crs": "EPSG:4326",
                "acquisition": spec["datetime_local"],
                "status": status,
                "coverage_percent_of_model_grid": coverage_pct,
                "note": "回退为裁剪外接矩形（未能从有效像元提取足迹）",
            },
        )
    write_json(out_json, entry)
    log(
        f"  完成 optical/{case_id}/{asset_id}: {png_meta['width']}x{png_meta['height']}px, "
        f"{png_meta['bytes'] / 2**20:.2f} MiB, 状态 {status}, 覆盖模型域 {coverage_pct}%"
    )
    return entry


# --------------------------------------------------------------------------- #
# SAR 资产
# --------------------------------------------------------------------------- #
def _sar_water_arrays(src_path: Path, grid: dict) -> dict:
    """读取 SAR 水体掩膜窗口并重投影到模型网格，返回水体分数与有效掩膜。"""
    with rasterio.open(src_path) as src:
        if str(src.crs) != grid["crs"]:
            raise RuntimeError(f"SAR CRS {src.crs} 与模型 CRS {grid['crs']} 不一致")
        win = from_bounds(*grid["native_bounds"], src.transform)
        col0 = max(0.0, win.col_off)
        row0 = max(0.0, win.row_off)
        col1 = min(float(src.width), win.col_off + win.width)
        row1 = min(float(src.height), win.row_off + win.height)
        if col1 - col0 <= 0 or row1 - row0 <= 0:
            raise RuntimeError("SAR 数据与模型域不相交")
        win = Window(col0, row0, col1 - col0, row1 - row0)
        nodata = src.nodata
        fill = 255 if nodata is not None and float(nodata) == 255.0 else 0
        band = src.read(1, window=win, boundless=True, fill_value=fill)
        src_transform = src.window_transform(win)
        src_crs = str(src.crs)
        src_meta = {
            "path": str(src_path).replace("\\", "/"),
            "bytes": src_path.stat().st_size,
            "crs": src_crs,
            "shape": [int(src.height), int(src.width)],
            "res": [float(src.res[0]), float(src.res[1])],
            "dtypes": [str(d) for d in src.dtypes],
            "nodata": None if nodata is None else float(nodata),
            "bounds": [float(v) for v in src.bounds],
            "window_used": [round(col0), round(row0), round(win.width), round(win.height)],
            "sha256": sha256_file(src_path) if src_path.stat().st_size < 10 * 1024 * 1024 else None,
        }

    fraction = np.full((grid["rows"], grid["cols"]), -1.0, dtype="float32")
    reproject(
        source=(band == 1).astype("float32"),
        destination=fraction,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=None if src_meta["nodata"] is None else src_meta["nodata"],
        dst_transform=grid["affine"],
        dst_crs=grid["crs"],
        dst_nodata=-1.0,
        resampling=Resampling.average,
        num_threads=2,
    )
    cover = np.full((grid["rows"], grid["cols"]), np.nan, dtype="float32")
    valid_src = (
        np.ones(band.shape, dtype="float32")
        if src_meta["nodata"] is None
        else (band != fill).astype("float32")
    )
    reproject(
        source=valid_src,
        destination=cover,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=None,
        dst_transform=grid["affine"],
        dst_crs=grid["crs"],
        dst_nodata=np.nan,
        resampling=Resampling.average,
        num_threads=2,
    )
    cover = np.nan_to_num(cover, nan=0.0)
    observed = cover > 0.5  # 与父项目 s1_to_grid 口径一致：源像元多数覆盖才算有效
    water = (fraction > WATER_FRACTION_THRESHOLD) & observed
    water_majority = (fraction >= 0.5) & observed
    src_px_km2 = abs(float(src_meta["res"][0]) * float(src_meta["res"][1])) / 1e6
    return {
        "fraction": fraction,
        "cover": cover,
        "observed": observed,
        "water": water,
        "water_majority": water_majority,
        "raw_water_area_km2": round(float(np.count_nonzero(band == 1)) * src_px_km2, 3),
        "source_meta": src_meta,
    }


def _footprint_from_observed(observed: np.ndarray, affine: Affine) -> dict:
    rows = np.nonzero(observed.any(axis=1))[0]
    cols = np.nonzero(observed.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        raise RuntimeError("SAR 有效覆盖为空")
    row0, row1 = int(rows.min()), int(rows.max()) + 1
    col0, col1 = int(cols.min()), int(cols.max()) + 1
    extent = grid_slice(affine, row0, row1, col0, col1)
    fill_ratio = float(observed[row0:row1, col0:col1].mean())
    return {"bounds": extent, "row0": row0, "row1": row1, "col0": col0, "col1": col1,
            "fill_ratio_of_bbox": round(fill_ratio, 4)}


def prepare_sar(case_id: str, spec: dict, grid: dict, args) -> dict:
    asset_id = spec["asset_id"]
    mode = args.sar_mode
    suffix = "_sigma" if mode == "backscatter" else ""
    src_path: Path = spec["sigma_path"] if mode == "backscatter" else spec["path"]
    if src_path is None:
        raise FileNotFoundError(f"{case_id} 无后向散射源，无法用 --sar-mode backscatter")
    if not src_path.is_file():
        raise FileNotFoundError(f"SAR 源文件不存在: {src_path}")

    out_png = SAR_ROOT / f"{asset_id}{suffix}.png"
    out_json = SAR_ROOT / f"{asset_id}{suffix}.json"
    out_footprint = SAR_ROOT / f"{asset_id}{suffix}_footprint.geojson"
    if out_png.exists() and out_json.exists() and not args.force:
        cached = reusable_asset(out_json, args, "sar")
        if cached is not None:
            log(f"复用已存在的 sar 资产: {rel(out_png)}")
            return cached
        log(f"已有 sar 资产与当前 --sar-mode {args.sar_mode} 不符，重建: {rel(out_png)}")

    log(f"[sar] {case_id} {src_path.name} ({mode}) → 模型网格 {grid['rows']}x{grid['cols']}")
    arr = _sar_water_arrays(src_path, grid)
    src_meta = arr["source_meta"]
    observed = arr["observed"]
    water = arr["water"]
    if mode == "backscatter":
        db = np.where(observed, arr["fraction"], np.nan)
        valid_db = db[np.isfinite(db)]
        p2, p98 = (float(v) for v in np.percentile(valid_db, [2, 98])) if valid_db.size else (None, None)
        if p2 is None or p98 is None or p98 <= p2:
            raise RuntimeError("后向散射有效值不足，无法拉伸")
        grey = np.clip((db - p2) / (p98 - p2), 0.0, 1.0)
        grey = np.where(np.isfinite(grey), grey, 0.0)
        rgba = np.zeros((grid["rows"], grid["cols"], 4), dtype="uint8")
        grey_u8 = (grey * 255).astype("uint8")
        rgba[..., 0] = grey_u8
        rgba[..., 1] = grey_u8
        rgba[..., 2] = grey_u8
        rgba[..., 3] = np.where(observed, 255, 0).astype("uint8")
        water = None
        legend = "sar_db_greyscale"
        stats_extra = {"db_stretch_p2": round(p2, 3), "db_stretch_p98": round(p98, 3),
                       "db_note": "灰度拉伸仅供显示，不是水深；色标范围见 db_stretch"}
    else:
        rgba = np.zeros((grid["rows"], grid["cols"], 4), dtype="uint8")
        rgba[water, 0] = SAR_WATER_RGB[0]
        rgba[water, 1] = SAR_WATER_RGB[1]
        rgba[water, 2] = SAR_WATER_RGB[2]
        rgba[water, 3] = SAR_WATER_ALPHA
        legend = "binary_water"
        stats_extra = {
            "color_rgb": list(SAR_WATER_RGB),
            "aggregation": (
                f"源 20 m/12.5 m 二值水体像元平均到模型格 → 水体分数 > {WATER_FRACTION_THRESHOLD} 判为水体；"
                "有效覆盖（源像元多数落在格内）才显示，否则透明"
            ),
            "water_fraction_threshold": WATER_FRACTION_THRESHOLD,
        }
    png_meta = save_rgba_png(out_png, rgba)

    footprint = _footprint_from_observed(observed, grid["affine"])
    extent = footprint["bounds"]
    ring = densified_ring_wgs84(grid["crs"], *extent)
    # PNG 覆盖整个模型网格（观测外透明），四角必须用整网格范围，不能用足迹范围
    png_corners = corners_wgs84(grid["crs"], *grid["native_bounds"])
    cell_km2 = grid["cell_area_m2"] / 1e6
    observed_cells = int(np.count_nonzero(observed))
    coverage_pct = round(100.0 * observed_cells / grid["grid_cells"], 3)
    water_cells = int(np.count_nonzero(water)) if water is not None else None
    majority_cells = int(np.count_nonzero(arr["water_majority"]))
    if water is not None:
        stats_extra["water_area_km2_majority_threshold"] = round(majority_cells * cell_km2, 3)
        stats_extra["water_cells_majority_threshold"] = majority_cells
        stats_extra["source_raw_water_area_km2"] = arr["raw_water_area_km2"]
        stats_extra["water_area_uncertainty_note"] = (
            "不同聚合阈值下面积差异明显：源像元原始水体 "
            f"{arr['raw_water_area_km2']} km²，模型格 frac>{WATER_FRACTION_THRESHOLD} 为 "
            f"{round(water_cells * cell_km2, 3)} km²，frac≥0.5 为 "
            f"{round(majority_cells * cell_km2, 3)} km²。"
            "只有原始值代表影像水体面积；模型格面积仅作显示参考，不得当淹没面积。"
        )
    status = "ready" if coverage_pct >= 99.5 else "partial"
    if footprint["fill_ratio_of_bbox"] < 0.98:
        note_footprint = "足迹取有效格网外接矩形，内部可能仍有空洞（填充率见 coverage.footprint_fill_ratio_of_bbox）"
    else:
        note_footprint = "足迹取有效格网外接矩形"

    entry = {
        "schema_version": "1.0",
        "case_id": case_id,
        "layer": "sar",
        "asset_id": f"{asset_id}{suffix}",
        "kind": "raster",
        "status": status,
        "enabled": status in ENABLED_STATUSES,
        "role": "sar_observation",
        "label": spec["label"] + ("（后向散射灰度）" if mode == "backscatter" else ""),
        "legend": legend,
        "summary": spec["summary"] + ("；本文件为灰度后向散射显示，不是水深" if mode == "backscatter" else ""),
        "note": "SAR 不是水深，不能共用深度图例；采集时刻与模拟事件不同期，仅范围证据",
        "sar_mode": mode,
        "source": {
            "type": "local_geotiff",
            "sensor": spec["sensor"],
            "scene_id": spec["scene_id"],
            "path": src_meta["path"],
            "file_bytes": src_meta["bytes"],
            "sha256": src_meta["sha256"],
            "crs": src_meta["crs"],
            "resolution_m": src_meta["res"],
            "nodata": src_meta["nodata"],
            "catalog_reference": "2d_hydro_project/data/catalog/s1_water_masks.json（S1 链）"
            if case_id != "ningming"
            else "2d_hydro_project/scripts/stage1/rasterize_water.py（GF-3 掩膜栅格化）",
        },
        "acquisition": {
            "datetime_utc": spec["acquired_utc"],
            "datetime_local": spec["acquired_local"],
            "time_zone": "Asia/Shanghai",
            "time_basis": spec["time_basis"],
            "observed_state": spec["observed_state"],
        },
        "grid": {
            "crs": grid["crs"],
            "rows": grid["rows"],
            "cols": grid["cols"],
            "transform": grid["transform"],
            "cell_size_m": grid["cell_size_m"],
        },
        "png": {
            "path": rel(out_png),
            **png_meta,
            "crs": grid["crs"],
            "coordinates_tl_tr_br_bl": png_corners,
            "grid_extent_projected": [round(v, 2) for v in grid["native_bounds"]],
            "grid_bounds_wgs84": grid["bounds_wgs84"],
            "note": "模型网格 1:1 渲染，行 0 = 最北行；四角为整模型网格范围，观测足迹见 footprint_geojson",
        },
        "footprint_geojson": rel(out_footprint),
        "coverage": {
            "coverage_percent_of_model_grid": coverage_pct,
            "observed_model_cells": observed_cells,
            "model_grid_cells": grid["grid_cells"],
            "model_grid_area_km2": grid["grid_area_km2"],
            "observed_area_km2": round(observed_cells * cell_km2, 3),
            "water_cells": water_cells,
            "water_area_km2": None if water_cells is None else round(water_cells * cell_km2, 3),
            "footprint_extent_projected": [round(v, 2) for v in extent],
            "footprint_fill_ratio_of_bbox": footprint["fill_ratio_of_bbox"],
            "footprint_bounds_wgs84": [
                round_coord(v) for v in transform_bounds(grid["crs"], "EPSG:4326", *extent, densify_pts=21)
            ],
            "footprint_note": note_footprint,
            "source_bounds_projected": [round(v, 2) for v in src_meta["bounds"]],
            "source_bounds_wgs84": [
                round_coord(v) for v in transform_bounds(src_meta["crs"], "EPSG:4326",
                                                         *src_meta["bounds"], densify_pts=21)
            ],
            **stats_extra,
        },
        "license": (
            {
                **SENTINEL_LICENSE,
                "attribution_original": f"Copernicus Sentinel data {spec['data_year']}",
                "attribution_modified": f"Contains modified Copernicus Sentinel data {spec['data_year']}",
                "modification_note": (
                    "父项目 S1 链 v2 定标与 -17 dB 阈值提取水体，再重采样到模型网格并显示"
                    "（σ⁰ 后向散射 → 二值水体掩膜）"
                ),
            }
            if case_id != "ningming"
            else {
                "name": "未核实（GF-3 本地供方数据；父项目 compliance_checklist 登记授权待确认）",
                "document": None,
                "redistribution": "unknown_internal_only",
                "attribution_original": "GF-3（高分三号）数据：本地供方提供，署名待核实",
                "attribution_modified": "GF-3（高分三号）水体掩膜：本地供方提供并栅格化，署名待核实",
                "modification_note": "provider 水体 shapefile 栅格化到 12.5 m，再重采样到模型网格并显示",
            }
        ),
        "qa": {
            "manual_visual_review": "未执行（未做人工目视验收）",
            "status_rule": "有效覆盖模型格 > 99.5% → ready；部分覆盖 → partial（必须显示足迹）",
            "status_reason": f"覆盖模型网格 {coverage_pct}%（{observed_cells}/{grid['grid_cells']} 格）→ {status}",
            "water_area_note": "water_area_km2 仅为 SAR 二值掩膜像元面积，不是淹没水深/体积",
        },
        "catalog_target": {
            "image": f"frames/{case_id}/sar.png",
            "footprint": f"cases/{case_id}/sar_footprint.geojson",
        },
        "generated_utc": utc_now_iso(),
        "command": f"uv run python {SCRIPT_REL} --layer sar --case {case_id}"
        + (" --sar-mode backscatter" if mode == "backscatter" else ""),
        "notes": [
            "SAR 不是水深；不可与 depth_classes 深度图例共用。",
            "只有水体像元不透明，其余透明；观测范围由 footprint 表达。",
            "采集时刻与模拟事件不同期，仅作范围/背景证据。",
        ]
        + ([] if status == "ready" else ["部分覆盖模型域，必须显示足迹，不宣称全覆盖。"]),
    }
    write_geojson(
        out_footprint,
        ring,
        {
            "case_id": case_id,
            "layer": "sar",
            "asset_id": entry["asset_id"],
            "role": "data_footprint",
            "crs": "EPSG:4326",
            "frame_crs": grid["crs"],
            "acquisition": spec["acquired_local"],
            "status": status,
            "coverage_percent_of_model_grid": coverage_pct,
            "water_area_km2": entry["coverage"]["water_area_km2"],
            "note": "SAR 有效观测足迹；不是水深范围",
        },
    )
    write_json(out_json, entry)
    log(
        f"  完成 sar/{entry['asset_id']}: {png_meta['width']}x{png_meta['height']}px, "
        f"{png_meta['bytes'] / 1024:.1f} KiB, 状态 {status}, 覆盖 {coverage_pct}%, "
        f"水体 {entry['coverage']['water_area_km2']} km²"
    )
    return entry


# --------------------------------------------------------------------------- #
# 许可、清单、文档
# --------------------------------------------------------------------------- #
def copy_license_files() -> dict:
    LICENSE_PDF_DST.parent.mkdir(parents=True, exist_ok=True)
    pdf_copied = False
    if LICENSE_PDF_SRC.is_file():
        if (
            not LICENSE_PDF_DST.exists()
            or LICENSE_PDF_DST.stat().st_size != LICENSE_PDF_SRC.stat().st_size
            or LICENSE_PDF_DST.stat().st_mtime < LICENSE_PDF_SRC.stat().st_mtime
        ):
            shutil.copy2(LICENSE_PDF_SRC, LICENSE_PDF_DST)
            pdf_copied = True
        pdf_sha = sha256_file(LICENSE_PDF_DST)
    else:
        pdf_sha = None
    readme = EXTERNAL_OPTICAL / "README.md"
    readme.write_text(
        "# data/external/optical —— 只放条款与说明，不放整景影像\n\n"
        "本目录只登记外部数据的授权文件与说明。整景原始影像不入库；\n"
        "裁剪后的显示小图在 `data/processed/optical/` 与 `data/processed/assets/`。\n\n"
        "## licenses/Sentinel_Data_Legal_Notice.pdf\n\n"
        "- 来源：https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice\n"
        "- 本地副本来自 `plans/visualization_research/Sentinel_Data_Legal_Notice.pdf`（已核验 HTTP 200）。\n"
        "- Sentinel 原始数据公开传播署名：`Copernicus Sentinel data [Year]`；\n"
        "  修改/裁剪/重采样后署名：`Contains modified Copernicus Sentinel data [Year]`。\n"
        "- 本子项目只做裁剪、重采样与 SCL 遮罩，故使用修改版署名（宁明 2026、贵港/久隆 2024）。\n\n"
        "## 未覆盖的第三方数据\n\n"
        "- GF-3（宁明 2026-08-24 水体掩膜）：本地供方数据，授权/署名未核实（父项目 compliance_checklist 开放项）。\n"
        "- 久隆本地 RGB（SVN3-01，2026-04-03）：来源/传感器/许可未核实。\n"
        "  两者公开发布前必须补齐授权确认；在此之前只作内部研究展示。\n",
        encoding="utf-8",
    )
    return {
        "pdf_path": rel(LICENSE_PDF_DST),
        "pdf_source": str(LICENSE_PDF_SRC).replace("\\", "/"),
        "pdf_sha256": pdf_sha,
        "pdf_copied_now": pdf_copied,
        "readme": rel(readme),
    }


def collect_assets(fresh: list[dict], args) -> list[dict]:
    """汇总磁盘上全部已生成的资产 entry（本次新建的优先）。

    这样即使只用 ``--layer sar`` 重跑，manifest 也不会丢掉光学资产。
    """
    by_id: dict[str, dict] = {}
    for directory in (OPTICAL_ROOT, SAR_ROOT):
        if not directory.is_dir():
            continue
        for json_path in sorted(directory.rglob("*.json")):
            try:
                entry = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log(f"跳过无法解析的资产元数据 {json_path}: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(entry, dict) or "asset_id" not in entry or "png" not in entry:
                continue
            by_id[(entry["case_id"], entry["layer"], entry["asset_id"])] = entry
    for entry in fresh:
        by_id[(entry["case_id"], entry["layer"], entry["asset_id"])] = entry
    assets = list(by_id.values())
    layer_order = {"optical": 0, "sar": 1}
    case_order = {case: idx for idx, case in enumerate(MODEL_RUNS)}
    assets.sort(key=lambda e: (case_order.get(e["case_id"], 99), layer_order.get(e["layer"], 9),
                               e["asset_id"]))
    return assets


def build_manifest(assets: list[dict], licenses: dict, errors: list[dict], args) -> dict:
    by_case: dict[str, dict] = {}
    for case in MODEL_RUNS:
        by_case[case] = {"case_id": case, "layers": {}}
    for entry in assets:
        case = entry["case_id"]
        slot = "optical" if entry["layer"] == "optical" else "sar"
        slot_data = by_case.setdefault(case, {"case_id": case, "layers": {}})["layers"]
        # 同一案例同一图层若有多份（如 SAR water + sigma），只把默认口径写进 layers_by_case
        if slot in slot_data and entry.get("sar_mode") not in (None, args.sar_mode):
            continue
        png = entry["png"]
        acq = entry["acquisition"]
        license_info = entry.get("license", {})
        slot_data[slot] = {
            "asset_id": entry["asset_id"],
            "catalog_slot": slot,
            "layer": entry["layer"],
            "status": entry["status"],
            "enabled": entry["enabled"],
            "role": entry.get("role"),
            "label": entry["label"],
            "summary": entry["summary"],
            "note": entry["note"],
            "acquisition": {
                "datetime_utc": acq.get("datetime_utc"),
                "datetime_local": acq.get("datetime_local"),
                "time_zone": acq.get("time_zone"),
                "time_basis": acq.get("time_basis"),
            },
            "png": {
                "path": png["path"],
                "width": png["width"],
                "height": png["height"],
                "source_width": png.get("source_width", png["width"]),
                "source_height": png.get("source_height", png["height"]),
                "bytes": png["bytes"],
                "sha256": png["sha256"],
                "crs": png["crs"],
                "coordinates_tl_tr_br_bl": png["coordinates_tl_tr_br_bl"],
                "resample": png.get("resample"),
                **({"max_edge_px": png["max_edge_px"]} if "max_edge_px" in png else {}),
            },
            "footprint_geojson": entry["footprint_geojson"],
            "coverage": entry["coverage"],
            "license": {
                "name": license_info.get("name"),
                "document": license_info.get("document"),
                "redistribution": license_info.get("redistribution"),
            },
            "attribution": license_info.get("attribution_modified"),
            "attribution_original": license_info.get("attribution_original"),
            "modification_note": license_info.get("modification_note"),
            "catalog_entry": {
                "status": entry["status"],
                "label": entry["label"],
                "kind": "raster",
                "asset_id": entry["asset_id"],
                "acquired_at": acq.get("datetime_local"),
                "summary": entry["summary"],
                "footprint": entry["catalog_target"]["footprint"],
                "image": entry["catalog_target"]["image"],
                "role": entry.get("role"),
                "note": entry["note"],
                **({"legend": entry["legend"]} if entry.get("legend") else {}),
                "coordinates_tl_tr_br_bl": png["coordinates_tl_tr_br_bl"],
                "width": png["width"],
                "height": png["height"],
                "crs": png["crs"],
            },
        }
    payload = {
        "schema_version": "1.0",
        "generated_utc": utc_now_iso(),
        "generator": {
            "script": SCRIPT_REL,
            "command": "uv run python " + SCRIPT_REL + " " + " ".join(sys.argv[1:]).strip(),
            "python": sys.version.split()[0],
            "rasterio": rasterio.__version__,
            "numpy": np.__version__,
            "pillow": Image.__version__,
            "max_edge_px": args.max_edge,
            "optical_max_bytes": OPTICAL_MAX_BYTES,
            "sar_mode": args.sar_mode,
        },
        "status_enum": list(STATUS_VALUES),
        "enabled_statuses": list(ENABLED_STATUSES),
        "counts": {
            "total": len(assets) + len(errors),
            "ok": len(assets),
            "failed": len(errors),
            "by_layer": {
                layer: sum(1 for e in assets if e["layer"] == layer) for layer in ("optical", "sar")
            },
            "by_status": {
                status: sum(1 for e in assets if e["status"] == status) for status in STATUS_VALUES
            },
        },
        "layers_by_case": {case: value["layers"] for case, value in by_case.items()},
        "assets": assets,
        "failed_assets": errors,
        "disabled_assets": DISABLED_ASSETS,
        "licenses": {"sentinel": licenses},
        "notes": [
            "路径均相对 flood_visualization 子项目根。",
            "layers_by_case 中 catalog_slot 为 'optical'/'sar' 的条目对应该案例 catalog.layers 的单一图层；"
            "catalog_entry 可直接合并进 catalog.json（lead 负责）。",
            "只有 ready/partial 可启用；failed/missing/local_unchecked 一律禁用并显示原因。",
            "SAR 不是水深，不能与 depth_classes 共用图例。",
            "光学采集时间固定，不随模拟时间滑块变化。",
            "光学 PNG 长边 ≤2048 px、单图 ≤4 MB；缩小只改像素密度，四角经纬度仍是整幅范围。",
            "本清单按磁盘上全部已生成资产汇总，单图层重跑不会丢失其它图层。",
        ],
    }
    return payload


def _fmt_size(num_bytes: int) -> str:
    if num_bytes >= 2**20:
        return f"{num_bytes / 2**20:.2f} MiB"
    return f"{num_bytes / 1024:.1f} KiB"


def _status_usage(assets: list[dict], status: str) -> str:
    """把某状态下的资产列成 `案例/图层/asset_id`，供文档状态表如实说明。"""
    hits = [f"{e['case_id']}/{e['layer']}/`{e['asset_id']}`" for e in assets if e["status"] == status]
    return "、".join(hits) if hits else "无"


def _asset_md_table(assets: list[dict]) -> str:
    lines = [
        "| 案例 | 图层 | asset_id | 状态 | 采集时间（北京） | 输出 PNG | 尺寸 px | 体积 | 有效覆盖 | 许可 |",
        "|---|---|---|---|---|---|---|---|---:|---|",
    ]
    for entry in assets:
        cov = entry["coverage"]
        coverage_txt = (
            f"AOI 有效 {cov['valid_coverage_percent']}%"
            if entry["layer"] == "optical" and "valid_coverage_percent" in cov
            else f"模型网格 {cov['coverage_percent_of_model_grid']}%"
        )
        lines.append(
            "| {case} | {layer} | {asset} | **{status}** | {acq} | `{png}` | {w}×{h} | {size} | {cov} | {lic} |".format(
                case=entry["case_id"],
                layer=entry["layer"],
                asset=entry["asset_id"],
                status=entry["status"],
                acq=entry["acquisition"].get("datetime_local", "未知"),
                png=entry["png"]["path"],
                w=entry["png"]["width"],
                h=entry["png"]["height"],
                size=_fmt_size(entry["png"]["bytes"]),
                cov=coverage_txt,
                lic="Sentinel（修改后署名）"
                if entry["license"].get("redistribution") == "allowed_with_attribution"
                else "未核实（内部）",
            )
        )
    return "\n".join(lines)


def _asset_section_md(entry: dict) -> str:
    cov = entry["coverage"]
    lines = [f"### {entry['case_id']} / {entry['layer']} / `{entry['asset_id']}`", ""]
    lines.append(f"- 状态：**{entry['status']}**（{entry['qa']['status_reason']}）")
    lines.append(f"- 角色：{entry.get('role')}；标签：{entry['label']}")
    lines.append(f"- 摘要：{entry['summary']}")
    lines.append(f"- 备注：{entry['note']}")
    lines.append(f"- 输出：`{entry['png']['path']}`（**{entry['png']['width']}×{entry['png']['height']} px**，"
                 f"**{_fmt_size(entry['png']['bytes'])}**，sha256 `{entry['png']['sha256'][:16]}…`）")
    if "source_width" in entry["png"]:
        png = entry["png"]
        native = png.get("native_clip_pixels") or {}
        lines.append(
            f"- 尺寸账目：原生裁剪 **{native.get('width', '?')}×{native.get('height', '?')} px** → "
            f"按长边 {png.get('max_edge_px', OPTICAL_MAX_EDGE)} px 采样 "
            f"**{png['source_width']}×{png['source_height']} px** → 压预算落盘 "
            f"**{png['width']}×{png['height']} px**"
            f"（重采样 {png.get('resample')}，二分 {png.get('bisect_rounds', 0)} 轮，"
            f"scale {png.get('scale', 1.0)}，预算 ≤{OPTICAL_MAX_BYTES // 2**20} MiB，"
            f"达标 {png.get('budget_met', True)}）"
        )
    lines.append(f"- 元数据：同目录 `.json`（`{Path(entry['png']['path']).with_suffix('.json').as_posix()}`）")
    lines.append(f"- 足迹：`{entry['footprint_geojson']}`")
    lines.append("- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）："
                 f"`{entry['png']['coordinates_tl_tr_br_bl']}`")
    src = entry["source"]
    if entry["layer"] == "optical" and src.get("source_url"):
        lines.append(f"- 来源场景：`{src['scene_id']}`（AWS Open Data Sentinel-2 L2A COG，匿名 HTTPS）")
        lines.append(f"  - TCI：{src['source_url']}")
        lines.append(f"  - SCL：{src['scl_url']}")
        lines.append(f"  - STAC：{src['stac_item_url']}")
        lines.append(
            f"  - 原始分辨率：TCI {src['source_resolution_m']['tci']} m，"
            f"SCL {src['source_resolution_m']['scl']} m"
        )
    elif entry["layer"] == "optical":
        lines.append(f"- 来源：本地 `{src['path']}`（{_fmt_size(src['file_bytes'])}，{src['crs']}，"
                     f"分辨率 {src['source_resolution_deg']}°，nodata={src['nodata']}）")
        lines.append(f"  - 来源说明：{src.get('provenance_note', '未核实')}")
    else:
        lines.append(f"- 来源：{src['sensor']}；`{src['scene_id']}`")
        lines.append(f"  - 本地栅格：`{src['path']}`（{_fmt_size(src['file_bytes'])}，{src['crs']}，"
                     f"{src['resolution_m']} m，nodata={src['nodata']}，"
                     f"sha256 `{(src.get('sha256') or '未计算')[:16]}…`）")

    acq = entry["acquisition"]
    lines.append(f"- 采集时间（北京）：{acq.get('datetime_local')}；UTC：{acq.get('datetime_utc')}；"
                 f"依据：{acq['time_basis']}")
    if entry["layer"] == "sar":
        lines.append(f"- 观测状态：{acq['observed_state']}")

    if entry["layer"] == "optical" and "valid_coverage_percent" in cov:
        lines.append(f"- 裁剪范围 WGS84 `[w,s,e,n]`：{entry['aoi']['clip_extent_wgs84']}")
        lines.append(f"- 遮罩：SCL 类别 {entry['mask']['transparent_scl_classes']} 置透明（NoData/云影/云）；"
                     f"透明像元占显示图 {cov['display_pixels']['transparent_percent']}%")
        lines.append(
            "- 覆盖统计（20 m SCL 全分辨率、AOI 多边形掩膜）："
            f"有效覆盖 {cov['valid_coverage_percent']}%，云 {cov['cloud_percent_of_valid']}%、"
            f"云影 {cov['cloud_shadow_percent_of_valid']}%（均占有效像元），"
            f"NoData {cov['nodata_percent_of_aoi']}%（占 AOI）"
        )
        for key, value in entry["qa"]["cross_check"].items():
            lines.append(f"  - 交叉核对 `{key}`：实测 {value['measured']} vs 检索报告 {value['expected_from_plan']}"
                         f"（Δ {value['delta']}，容差 {value['tolerance']}）→ "
                         + ("通过" if value["pass"] else "**不一致**"))
        lines.append("- 检索证据：`plans/宁明_贵港_公开可见光影像检索结果.md`、"
                     "`plans/visualization_research/optical_aoi_quality.json`")
    elif entry["layer"] == "optical":
        lines.append(f"- 裁剪范围 WGS84 `[w,s,e,n]`：{entry['aoi']['clip_extent_wgs84']}")
        lines.append(f"- 来源影像范围 WGS84：{entry['aoi']['source_bounds_wgs84']}")
        lines.append(f"- 遮罩：{entry['mask']['note']}")
        lines.append(f"- 覆盖统计：覆盖模型网格 {cov['coverage_percent_of_model_grid']}%"
                     f"（{cov['covered_model_cells']}/{cov['model_grid_cells']} 格，"
                     f"{cov['covered_area_km2']} km²）；显示图透明像元 "
                     f"{cov['display_pixels']['transparent_percent']}%")
    else:
        lines.append(f"- 渲染：模型网格 {entry['grid']['rows']}×{entry['grid']['cols']}"
                     f"（{entry['grid']['crs']}，格 {entry['grid']['cell_size_m']} m）；"
                     f"图例 `{entry['legend']}`")
        lines.append(f"- 覆盖统计：有效覆盖模型网格 {cov['coverage_percent_of_model_grid']}%"
                     f"（{cov['observed_model_cells']}/{cov['model_grid_cells']} 格，"
                     f"{cov['observed_area_km2']} km²）")
        if cov.get("water_cells") is not None:
            lines.append(f"- 水体：{cov['water_cells']} 个模型格 ≈ {cov['water_area_km2']} km²"
                         f"（二值掩膜显示面积，不是水深/淹没面积）")
            lines.append(f"  - 源像元原始水体面积 {cov['source_raw_water_area_km2']} km²；"
                         f"frac≥0.5 聚合 {cov['water_area_km2_majority_threshold']} km²")
            lines.append(f"  - 口径提醒：{cov['water_area_uncertainty_note']}")
        lines.append(f"- 足迹：{cov['footprint_note']}；外接矩形填充率 "
                     f"{cov['footprint_fill_ratio_of_bbox']}；WGS84 包络 {cov['footprint_bounds_wgs84']}")
        lines.append(f"- 聚合口径：{cov.get('aggregation')}")
        lines.append(f"- 源数据覆盖 WGS84：{cov['source_bounds_wgs84']}")

    lic = entry["license"]
    lines.append(f"- 许可：{lic['name']}")
    lines.append(f"  - 原始数据署名：`{lic['attribution_original']}`")
    lines.append(f"  - 修改后署名：`{lic['attribution_modified']}`")
    lines.append(f"  - 修改说明：{lic['modification_note']}")
    lines.append(f"  - 条款文件：{lic.get('document') or '无（授权未核实）'}")
    lines.append(f"- 生成命令：`{entry['command']}`")
    lines.append(f"- 生成时间 UTC：{entry['generated_utc']}")
    if entry.get("notes"):
        lines.append("- 其他说明：")
        lines.extend(f"  - {item}" for item in entry["notes"])
    lines.append("")
    return "\n".join(lines)


def render_docs(manifest: dict, licenses: dict) -> None:
    assets = manifest["assets"]
    errors = manifest["failed_assets"]
    counts = manifest["counts"]
    lines = [
        "# 光学与 SAR 显示资产登记（flood_visualization）",
        "",
        f"> 生成时间 UTC：{manifest['generated_utc']} ｜ 生成命令：`{manifest['generator']['command']}`",
        "> 机器可读清单：[data/processed/assets/manifest.json](../data/processed/assets/manifest.json)；"
        "契约见 [SPEC.md §9](../SPEC.md)。",
        "> 本文件由 `scripts/prepare_optical.py` 按实测结果生成；数字为本次运行实测值。",
        "> 下文各资产的「来源 / 本地栅格」路径是生成本文件时的登记值；若文件早于存储迁移，"
        "这些绝对路径可能已失效，现役位置以 `GX_OPTICAL_ROOT` 解析与父项目 `scripts/common/paths.py` 为准。",
        "",
        "## 1. 资产总表",
        "",
        "状态统计：" + "，".join(
            f"{status} {counts['by_status'].get(status, 0)}" for status in STATUS_VALUES
        ) + f"；失败 {counts['failed']} 个。只有 `ready/partial` 可启用。",
        "",
        _asset_md_table(assets),
        "",
        "## 2. 逐资产说明",
        "",
    ]
    for entry in assets:
        lines.append(_asset_section_md(entry))
    if errors:
        lines.extend(["## 3. 失败资产（failed）", ""])
        for err in errors:
            lines.append(f"- {err['case_id']} / {err['layer']} / `{err['asset_id']}`："
                         f"{err['error_type']}: {err['error']}")
        lines.append("")
    else:
        lines.extend(["## 3. 失败资产（failed）", "", "本次运行无失败资产。", ""])

    lines.extend(
        [
            "## 4. 未启用 / 禁用资产",
            "",
            "| 案例 | asset_id | 状态 | 路径 | 原因 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in manifest["disabled_assets"]:
        lines.append(
            f"| {item['case_id']} | `{item['asset_id']}` | {item['status']} | `{item['path']}` | {item['reason']} |"
        )
    lines.extend(
        [
            "",
            "- `灾后SAR_久隆区域.tif`：传感器/日期/处理链未核实 → `local_unchecked`，**禁用**；"
            "本子项目不使用，也不复制进发布包。",
            "- 检索报告中的其它候选景（宁明 2026-08-12、贵港 2024-10-31 / 09-13 / 09-03）未导入；"
            "其中宁明 08-12 景 AOI 云占比 31.48%，不能按整景低云采用。",
            "- `宁明县8月24日SAR.tif`（原始 GF-3 栅格）未直接使用；本次使用父项目栅格化的二值水体掩膜 "
            "`data/interim/ningming/water_0824.tif`。`water_new_0824.tif` 是 8/24 新增淹没层（剔除 8/6 常水），"
            "本次未用，仅登记为备选。",
            "",
            "## 5. 状态机与判定口径",
            "",
            "| 状态 | 含义 | 本次实际用例 |",
            "|---|---|---|",
            "| `remote_verified` | 远端可读、未本地化 | 本次无：远程 S2 场景已裁成本地 PNG |",
            f"| `local_unchecked` | 本地有数据但未通过质检 | {_status_usage(assets, 'local_unchecked')} |",
            f"| `ready` | 通过发布质检且覆盖达标 | {_status_usage(assets, 'ready')} |",
            f"| `partial` | 通过质检但覆盖不全 | {_status_usage(assets, 'partial')} |",
            f"| `missing` | 未提供 | {_status_usage(assets, 'missing')} |",
            f"| `failed` | 处理失败 | {_status_usage(assets, 'failed')} |",
            "",
            "判定规则（脚本内固定）：",
            "",
            "- 光学：全分辨率 20 m SCL 按 AOI 多边形统计，有效覆盖 ≥99.5% 且有效像元云占比 ≤1.0%"
            " → `ready`，否则 `partial`。",
            "- 光学（本地影像）：显示图有效像元重投影到模型网格，覆盖 ≥99.5% → `ready`，否则 `partial`。",
            "- SAR：重投影到模型网格后有效观测格网占比 ≥99.5% → `ready`，否则 `partial`（必须显示足迹）。",
            "- 云/云影/NoData：SCL 类别 0/3/8/9/10 在显示图中 alpha=0；不做平滑填充冒充观测。",
            "",
            "## 6. 许可与署名",
            "",
            "- Sentinel（S2 光学、S1 SAR）：Copernicus Sentinel Data Legal Notice，免费开放，"
            "允许复制/传播/修改/组合。",
            "  - 条款 PDF：[Sentinel_Data_Legal_Notice.pdf]"
            "(../data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf)"
            f"（sha256 `{(licenses.get('pdf_sha256') or '')[:16]}…`）。",
            "- 原始数据公开传播署名：`Copernicus Sentinel data [Year]`；",
            "- 本子项目产物为裁剪/重采样/遮罩后的修改版，署名："
            "`Contains modified Copernicus Sentinel data [Year]`（宁明 2026，贵港/久隆 2024）。",
            "- GF-3（宁明 SAR）与本地 RGB（久隆光学）：授权未核实，仅限内部研究展示；"
            "公开再分发前必须补齐授权确认。",
            "",
            "## 7. 复现命令",
            "",
            "```powershell",
            "cd 2d_hydro_project/flood_visualization",
            "$env:PYTHONUTF8='1'",
            "uv run python scripts/prepare_optical.py --force              # 全量重建",
            "uv run python scripts/prepare_optical.py --layer optical      # 只做光学",
            "uv run python scripts/prepare_optical.py --layer sar          # 只做 SAR",
            "uv run python scripts/prepare_optical.py --download           # 远程读取失败时整景下载兜底",
            "```",
            "",
            "依赖：venv 内 numpy / rasterio / pyproj / pillow（见 `pyproject.toml`）。"
            "远程 S2 场景约 186–319 MB/景，脚本只做 COG 窗口读取；**整景不入库**，"
            "仅失败兜底时下载到系统临时目录，用完删除。",
            "",
            "## 8. 已知限制与风险",
            "",
            "1. SCL 是自动分类，未做人工云判读；残留薄云/云影可能仍在图中。",
            "2. 贵港光学约 3.16% NoData 空缺透明；不得宣称整域覆盖，也不得把 2024-10 影像当洪峰观测。",
            "3. 贵港 SAR 只覆盖模型域东部一部分；页面必须显示足迹，不得宣称全市覆盖。",
            "4. 久隆光学为 2026-04 本地影像，非 2024 事件同期，且仅覆盖部分模型域；来源/许可未核实。",
            "5. 宁明 GF-3 SAR 精确过境时刻未核实（父项目登记 ±6 h）；GF-3 授权未核实。",
            "6. SAR 二值水体掩膜不是水深/淹没深度，不能与深度图例共用；水体面积只作范围参考。",
            "7. 远程读取为通过本机代理的匿名 HTTPS（关闭证书校验，原因见脚本文档串）；"
            "已用检索报告的 SCL 统计交叉核对，差异见各资产 `qa.cross_check`。",
            "8. 未执行人工目视验收（`manual_visual_review` 未完成）；`ready` 仅表示自动质检与覆盖门禁通过。",
            "",
        ]
    )
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(args) -> int:
    started = time.time()
    for directory in (OPTICAL_ROOT, SAR_ROOT, EXTERNAL_OPTICAL / "licenses"):
        directory.mkdir(parents=True, exist_ok=True)
    licenses = copy_license_files()
    log(f"许可 PDF：{licenses['pdf_path']}（sha256 {(licenses['pdf_sha256'] or '缺失')[:16]}…）")

    cases = args.case or list(MODEL_RUNS)
    layers = args.layer or ["optical", "sar"]
    assets: list[dict] = []
    errors: list[dict] = []

    for case_id in cases:
        grid: dict | None = None
        try:
            grid = read_model_grid(case_id)
            log(
                f"[{case_id}] 模型网格 {grid['rows']}×{grid['cols']} {grid['crs']} "
                f"格 {grid['cell_size_m']} m，WGS84 {grid['bounds_wgs84']}"
            )
        except Exception as exc:
            log(f"[{case_id}] 读取模型网格失败: {type(exc).__name__}: {exc}")
            for layer in layers:
                asset_id = (OPTICAL_SCENES.get(case_id) or LOCAL_OPTICAL.get(case_id) or SAR_SOURCES[case_id])[
                    "asset_id"
                ]
                errors.append({"case_id": case_id, "layer": layer, "asset_id": asset_id,
                               "status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
            continue

        if "optical" in layers and case_id in OPTICAL_SCENES:
            try:
                assets.append(prepare_remote_optical(case_id, OPTICAL_SCENES[case_id], grid, args))
            except Exception as exc:
                log(f"[optical:{case_id}] 失败: {type(exc).__name__}: {exc}")
                errors.append({"case_id": case_id, "layer": "optical",
                               "asset_id": OPTICAL_SCENES[case_id]["asset_id"], "status": "failed",
                               "error_type": type(exc).__name__, "error": str(exc)})
        if "optical" in layers and case_id in LOCAL_OPTICAL:
            try:
                assets.append(prepare_local_optical(case_id, LOCAL_OPTICAL[case_id], grid, args))
            except Exception as exc:
                log(f"[optical-local:{case_id}] 失败: {type(exc).__name__}: {exc}")
                errors.append({"case_id": case_id, "layer": "optical",
                               "asset_id": LOCAL_OPTICAL[case_id]["asset_id"], "status": "failed",
                               "error_type": type(exc).__name__, "error": str(exc)})
        if "sar" in layers:
            try:
                assets.append(prepare_sar(case_id, SAR_SOURCES[case_id], grid, args))
            except Exception as exc:
                log(f"[sar:{case_id}] 失败: {type(exc).__name__}: {exc}")
                errors.append({"case_id": case_id, "layer": "sar",
                               "asset_id": SAR_SOURCES[case_id]["asset_id"]
                               + ("_sigma" if args.sar_mode == "backscatter" else ""),
                               "status": "failed", "error_type": type(exc).__name__, "error": str(exc)})

    # 清单汇总磁盘上全部已生成资产（单图层重跑不丢其它图层）
    all_assets = collect_assets(assets, args)
    manifest = build_manifest(all_assets, licenses, errors, args)
    write_json(MANIFEST_PATH, manifest)
    if not args.no_docs:
        render_docs(manifest, licenses)
        log(f"文档：{rel(DOC_PATH)}")
    log(
        f"清单：{rel(MANIFEST_PATH)}；本次生成 {len(assets)} 个，磁盘资产合计 {len(all_assets)} 个，"
        f"失败 {len(errors)} 个，用时 {time.time() - started:.1f} s"
    )
    for entry in all_assets:
        log(
            f"  - {entry['case_id']:8s} {entry['layer']:7s} {entry['asset_id']:24s} "
            f"{entry['status']:8s} {entry['png']['width']}x{entry['png']['height']} "
            f"{_fmt_size(entry['png']['bytes'])}"
        )
    for err in errors:
        log(f"  ! {err['case_id']:8s} {err['layer']:7s} {err['asset_id']}: {err['error']}")
    return 0 if not errors else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="准备光学与 SAR 显示资产并登记状态")
    parser.add_argument("--case", action="append", choices=list(MODEL_RUNS), help="只处理指定案例（可重复）")
    parser.add_argument("--layer", action="append", choices=["optical", "sar"], help="只处理指定图层（可重复）")
    parser.add_argument("--force", action="store_true", help="已存在的资产也重建")
    parser.add_argument(
        "--max-edge",
        type=int,
        default=OPTICAL_MAX_EDGE,
        help=f"光学显示图最长边像素上限（默认 {OPTICAL_MAX_EDGE}，上限 4096 由 web 纹理决定）",
    )
    parser.add_argument("--sar-mode", choices=["water", "backscatter"], default="water",
                        help="SAR 渲染模式：二值水体（默认）或后向散射灰度")
    parser.add_argument("--download", action="store_true", help="远程场景先整景下载再裁剪（兜底）")
    parser.add_argument("--temp-dir", type=Path, default=Path(tempfile.gettempdir()) / "floodviz_optical_tmp",
                        help="整景下载兜底临时目录")
    parser.add_argument("--no-docs", action="store_true", help="不重写 docs/optical_assets.md")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_edge > 4096:
        log("--max-edge 超过 4096，按 4096 处理（web 纹理上限）")
        args.max_edge = 4096
    args.temp_dir = Path(args.temp_dir)
    try:
        return run(args)
    except KeyboardInterrupt:
        log("中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
