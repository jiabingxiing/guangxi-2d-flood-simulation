"""久隆治理共用栅格 I/O 与审计工具（Phase 0–5 共用）。

设计约束（对应计划 §3.3、§4、§7.1）：

1. **单元面积从栅格头读取，不写死**。100 m 与 200 m 网格面积差 4 倍，
   任何"4/9 像元"阈值都必须在真实 cellsize 下换算。
2. **不覆盖已有 res.mass 的 run 目录**（终稿 §10）。`guard_no_overwrite`
   是唯一写入口的前置检查。
3. 所有写出 JSON 都带 `generated_utc`、脚本名、输入哈希，便于追溯。
4. 本模块只读模型输出，不改写 raw run 的任何文件。

本模块不依赖 scipy / scikit-image（当前 uv 环境未安装）；连通域实现在
`patch_analysis.py`，只用 numpy。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
NODATA_DEFAULT = -9999.0
HEADER_KEYS = ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value")


class GridError(ValueError):
    """栅格头或数据不合法。"""


class OverwriteRefused(RuntimeError):
    """目标 run 目录已有 res.mass；按终稿 §10 拒绝覆盖。"""


@dataclass(frozen=True)
class AsciiGrid:
    """ESRI ASCII 栅格：6 行头 + 数值主体（首行=最北行）。

    `cell_area_m2` 由 `cellsize` 计算，是本项目所有物理面积阈值的唯一来源。
    """

    data: np.ndarray
    header: dict[str, float]

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.header["nrows"]), int(self.header["ncols"]))

    @property
    def cell_size_m(self) -> float:
        return float(self.header["cellsize"])

    @property
    def cell_area_m2(self) -> float:
        return self.cell_size_m * self.cell_size_m

    @property
    def nodata(self) -> float:
        return float(self.header.get("nodata_value", NODATA_DEFAULT))

    def area_km2(self, mask: np.ndarray) -> float:
        """掩膜像元的物理面积（km²）。"""
        return float(np.count_nonzero(mask)) * self.cell_area_m2 / 1e6

    def cells_for_area(self, area_km2: float) -> int:
        """把物理面积阈值换算成当前网格的像元数（向上取整，至少 1）。"""
        if area_km2 <= 0:
            return 1
        return max(1, int(np.ceil(area_km2 * 1e6 / self.cell_area_m2)))

    def to_meta(self) -> dict[str, Any]:
        """网格元数据摘要（写入所有审计 JSON）。"""
        return {
            "ncols": int(self.header["ncols"]),
            "nrows": int(self.header["nrows"]),
            "cellsize_m": self.cell_size_m,
            "cell_area_m2": self.cell_area_m2,
            "xllcorner": float(self.header["xllcorner"]),
            "yllcorner": float(self.header["yllcorner"]),
            "nodata": self.nodata,
        }


def read_ascii_header(path: str | Path) -> dict[str, float]:
    """读 ESRI ASCII 头 6 行，返回小写键名 dict。"""
    path = Path(path)
    header: dict[str, float] = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(6):
            line = fh.readline()
            if not line:
                raise GridError(f"{path} 头部不足 6 行")
            parts = line.split()
            if len(parts) < 2:
                raise GridError(f"{path} 头部行不合法：{line!r}")
            header[parts[0].strip().lower()] = float(parts[1])
    missing = [k for k in HEADER_KEYS if k not in header]
    if missing:
        raise GridError(f"{path} 头部缺少字段 {missing}；实得 {sorted(header)}")
    return header


def read_ascii_grid(
    path: str | Path,
    *,
    dtype: type = np.float32,
    nodata_to_nan: bool = False,
    expected_shape: tuple[int, int] | None = None,
) -> AsciiGrid:
    """读整幅 ESRI ASCII 栅格。"""
    path = Path(path)
    header = read_ascii_header(path)
    shape = (int(header["nrows"]), int(header["ncols"]))
    if expected_shape is not None and shape != expected_shape:
        raise GridError(f"{path} 头声明形状 {shape} 与期望 {expected_shape} 不符")
    data = np.loadtxt(path, skiprows=6, dtype=dtype, ndmin=2)
    if data.shape != shape:
        raise GridError(f"{path} 数据形状 {data.shape} 与头声明 {shape} 不符")
    if nodata_to_nan:
        data = np.where(data == dtype(header["nodata_value"]), np.nan, data).astype(dtype)
    return AsciiGrid(data=data, header=header)


def write_ascii_grid(path: str | Path, grid: AsciiGrid, *, fmt: str = "%.3f") -> Path:
    """写 ESRI ASCII 栅格（制表符分隔，6 行头）。

    注意：本函数只用于**派生产物**。写入 raw run 目录属于禁止操作，
    调用方必须先经 `guard_no_overwrite`。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    h = grid.header
    header_text = (
        f"ncols         {int(h['ncols'])}\n"
        f"nrows         {int(h['nrows'])}\n"
        f"xllcorner     {float(h['xllcorner']):.1f}\n"
        f"yllcorner     {float(h['yllcorner']):.1f}\n"
        f"cellsize      {float(h['cellsize']):.1f}\n"
        f"NODATA_value  {float(h.get('nodata_value', NODATA_DEFAULT)):.1f}\n"
    )
    with path.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(header_text)
        np.savetxt(fh, grid.data, fmt=fmt, delimiter="\t")
    return path


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def guard_no_overwrite(target_dir: str | Path) -> None:
    """目标目录已有 results/res.mass 时拒绝写入（终稿 §10 硬约束）。"""
    target = Path(target_dir)
    mass = target / "results" / "res.mass"
    if mass.is_file():
        raise OverwriteRefused(
            f"{target} 已有 results/res.mass；终稿 §10 禁止覆盖。请改用新的 run tag/输出目录。"
        )


def write_json(path: str | Path, payload: Any) -> Path:
    """原子写 JSON（UTF-8，缩进 2）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_frames(results_dir: str | Path) -> list[tuple[int, Path]]:
    """列出 results/res-XXXX.wd，按序号升序返回 [(seq, path)]。"""
    results = Path(results_dir)
    if not results.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    for child in results.iterdir():
        if not child.is_file():
            continue
        name = child.name
        if not (name.startswith("res-") and name.endswith(".wd")):
            continue
        seq_text = name[len("res-"):-len(".wd")]
        if len(seq_text) != 4 or not seq_text.isdigit():
            continue
        out.append((int(seq_text), child))
    out.sort(key=lambda item: item[0])
    return out


def load_sea_mask(run_dir: str | Path, shape: tuple[int, int]) -> np.ndarray | None:
    """读 run 的 sea_mask.npy；形状不符时抛 GridError（不静默退化）。"""
    path = Path(run_dir) / "sea_mask.npy"
    if not path.is_file():
        return None
    mask = np.asarray(np.load(path)).astype(bool)
    if mask.shape != shape:
        raise GridError(f"{path} 形状 {mask.shape} 与网格 {shape} 不符")
    return mask


def run_meta(run_dir: str | Path) -> dict[str, Any]:
    """读 run 的 meta.json；缺失返回空 dict。"""
    path = Path(run_dir) / "meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def savefig_png(rgba_or_gray: np.ndarray, path: str | Path) -> Path:
    """写 PNG（经 GDAL/rasterio，环境无 PIL/matplotlib）。

    支持 uint8 (H,W) 灰度、(H,W,3) RGB、(H,W,4) RGBA。
    """
    import rasterio
    from rasterio.transform import from_origin

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(rgba_or_gray)
    if arr.ndim == 2:
        count, bands = 1, arr[np.newaxis, :, :]
    elif arr.ndim == 3:
        count, bands = arr.shape[2], np.moveaxis(arr, 2, 0)
    else:
        raise ValueError(f"不支持的图像形状：{arr.shape}")
    if arr.dtype != np.uint8:
        bands = np.clip(bands, 0, 255).astype(np.uint8)
    height, width = bands.shape[1], bands.shape[2]
    with rasterio.open(
        path, "w", driver="PNG", height=height, width=width, count=count,
        dtype="uint8", transform=from_origin(0, height, 1, 1),
    ) as dst:
        for idx in range(count):
            dst.write(bands[idx], idx + 1)
    return path


def frame_timeline(
    run_dir: str | Path, save_interval_s: float, *, sim_seconds: float | None = None
) -> list[dict[str, Any]]:
    """帧序号 → 模拟时刻（秒 / 小时）。saveint 从 run.par 校验，避免口径漂移。"""
    parsed = parse_run_par(run_dir)
    declared = parsed.get("saveint")
    if declared is not None and abs(float(declared) - float(save_interval_s)) > 1e-6:
        raise GridError(
            f"{run_dir} 的 run.par saveint={declared} 与传入 {save_interval_s} 不一致；"
            "帧时间口径必须唯一"
        )
    sim = float(parsed.get("sim_time", sim_seconds or 0.0))
    out = []
    for seq, _path in run_frames(Path(run_dir) / "results"):
        t = seq * float(save_interval_s)
        out.append({
            "seq": seq,
            "t_s": t,
            "t_h": round(t / 3600.0, 3),
            "beyond_sim_end": bool(sim and t > sim + 1e-6),
        })
    return out


def parse_run_par(run_dir: str | Path) -> dict[str, Any]:
    """解析 run.par 的键值行；返回 {key_lower: value(float|str)}。"""
    path = Path(run_dir) / "run.par"
    if not path.is_file():
        return {}
    out: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            out[parts[0].lower()] = True
            continue
        key = parts[0].lower()
        try:
            out[key] = float(parts[1])
        except ValueError:
            out[key] = parts[1]
    return out


class AuditLog:
    """脚本级审计日志：记录输入哈希、参数、每步结果，最后原子写 JSON。"""

    def __init__(self, script: str, *, run_tag: str | None = None, dry_run: bool = False):
        self.payload: dict[str, Any] = {
            "script": script,
            "generated_utc": utc_now(),
            "run_tag": run_tag,
            "dry_run": bool(dry_run),
            "inputs": {},
            "parameters": {},
            "steps": [],
            "warnings": [],
            "errors": [],
        }

    def add_input(self, key: str, path: str | Path, *, hash_file: bool = True) -> None:
        p = Path(path)
        rec: dict[str, Any] = {"path": str(p), "exists": p.is_file()}
        if p.is_file():
            rec["bytes"] = p.stat().st_size
            if hash_file:
                rec["sha256"] = sha256_file(p)
        self.payload["inputs"][key] = rec

    def add_input_dir(self, key: str, path: str | Path, pattern: str = "*") -> None:
        p = Path(path)
        files = sorted(f for f in p.glob(pattern) if f.is_file()) if p.is_dir() else []
        self.payload["inputs"][key] = {
            "path": str(p),
            "exists": p.is_dir(),
            "file_count": len(files),
            "sha256_of_names": sha256_text("\n".join(f.name for f in files)),
        }

    def param(self, **kwargs: Any) -> None:
        self.payload["parameters"].update(kwargs)

    def step(self, name: str, **detail: Any) -> None:
        self.payload["steps"].append({"name": name, "utc": utc_now(), **detail})

    def warn(self, message: str) -> None:
        self.payload["warnings"].append(message)

    def error(self, message: str) -> None:
        self.payload["errors"].append(message)

    def write(self, path: str | Path) -> Path:
        self.payload["finished_utc"] = utc_now()
        return write_json(path, self.payload)


def add_common_args(parser: Any) -> None:
    """为所有治理脚本加统一 CLI 参数（计划 §4）。"""
    parser.add_argument("--dry-run", action="store_true",
                        help="只计算并打印审计，不写任何派生文件")
    parser.add_argument("--run-tag", default=None,
                        help="输出子目录后缀，便于多次试验并存")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="显式输出目录（默认 diagnostics/jiulong 或 data/catalog）")
    parser.add_argument("--log-json", type=Path, default=None,
                        help="审计 JSON 落盘路径；缺省写默认位置")
