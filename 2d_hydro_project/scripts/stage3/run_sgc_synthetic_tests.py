"""计划任务4 + 计划 §4.5（ADR-0020 护栏）：本地 LISFLOOD-FP 8.1.0 八算例能力核证。

在 60x40、dx=10 m 合成 DEM 上跑 8 个微型算例，检验本机 lisflood.exe 的
SGC 求解链路（SGC_enable/SGCwidth/SGCbank/SGCchan/SGCn/SGCr/SGCp）、降雨、
干湿转换与 HVAR 边界（关键字均对 models/ 源码 pars.cpp/input.cpp 核验）：

| 算例 | 目的 | 驱动 | 判据 |
|---|---|---|---|
| s1 蓄水 | 单洼地蓄水持稳 | 平坡 z=5 + 3x3 洼地 z=3（一圈 z=4 过渡沿）；雨 20 mm/h 至 1800 s | 坑内最小水深>=1.0 m；坑蓄水 2400→3600 s 漂移<=5%；E_mass<=1e-3 |
| s2 坡面 | 单向坡面片流 | 平面 z=8→2（北高南低）；雨 15 mm/h 恒定 3600 s | 末帧湿面积比>=80%（水深>1 mm 口径）；上半场全干 2x2 块<=20；E_mass<=1e-3 |
| s3 输水 | 河道输水不出槽 | 北缘 5 m³/s 恒定、南缘 FREE | 末端 Qout≈Qin（±20%）；E_mass<=1e-3；槽外湿像元<=2 |
| s4 漫溢 | 超满岸输水漫滩 | 北缘 40 m³/s 恒定 | 槽外湿像元>100；E_mass<=1e-3 |
| s5 汇合 | 两条支流汇合 | Y 形 SGC 河道；北缘两股 QVAR 各 5 m³/s（qin_w/qin_e 双序列双 N 行） | 末端 Qout≈10 m³/s（±10%）；干流无断湿；E_mass<=1e-3 |
| s6 退水 | 断流后退水 | 40 m³/s 至 1800 s 后归零 | 末帧湿像元<=峰值帧 20%；E_mass<=1e-3 |
| s7 干湿 | 干湿转换穿越 DepthThresh | 平坡 z=2 + 洼地 + 浅沟通南缘；2 m³/s 脉冲 600 s 后归零 | res.max/逐帧无 NaN/inf；末帧湿像元<=5；全程最大水深<=1.5 m；E_mass<=1e-3 |
| s8 边界水位 | 南缘 HVAR 水位缓升 | 平坡 z=5；南缘 HVAR 水面高程 5.0→6.2 m 线性 1800 s 后持稳 | 南缘 5 行带内湿像元>=50；res.max 有限；E_mass<=1e-3（G13 激波门） |

设计约束（均对源码核验/实测校准）：
1. 满岸水深 = r·w^p（sgc.cpp CalcSGCz）：r=0.55、w=8、p=0.4 → 约 1.26 m，
   满岸输水约 37 m³/s → 5 m³/s 在槽内（s3）、40 m³/s 必漫溢（s4），判据互斥成立；
   r 还须盖过南缘 FREE(0.0005) 出流雍水（槽内通量按坡度 0.0005 曼宁计算，
   CalcSGC_pointFREE；5 m³/s 稳态约 0.98 m）与入流波到口短暂涌高（约 1.24 m），
   否则出流断面溢槽（实测 r=0.3/0.5 南缘积水 8/4 格，r=0.55 全程 0 格）。
2. 入流只覆盖北缘河道单列：input.cpp 北缘周界索引 = floor((x−blx)/dx)，
   x∈[col·dx,(col+1)·dx) 只选中 col 列；QVAR 按「像元数 × value × dx」施加
   （sgc.cpp Q_multiplier=dx），故 .bdy 文件值 = Q_m3s/dx 时合计恰为 Q_m3s；
   同一边两条 QVAR 行各自按行内 x 区间落到互不重叠的周界索引（BC_Name 逐索引
   存储，input.cpp LoadBCVar 按名匹配序列）→ s5 双序列可行；若 exe 拒绝则回退
   一条全缘 QVAR（10 m³/s 分摊 60 列，value=Q/(60·dx)）并记 inflow_mode=
   combined_fallback。
3. massint 取 10 s：Qin/Qout 列是写出时刻的瞬时通量（BoundaryFlux），g15 的
   逐行梯形积分分辨不了 1 s 陡坎，实测 E_mass 假阳性 ~3e-3（模型自身
   Qerror/Verror 恒为 0.000）；故 s6 断流处改 [1740,1800] 线性退坡（1800 s 起
   严格为 0）并细分 massint，实测压回 1e-3 以下；s7 脉冲同法 [540,600] 退坡。
4. s1 雨量 20 mm/h×1800 s = 2400 m³；任务书示例 8x8 洼地（6400 m²）即使全部
   雨量入坑也仅 0.375 m，达不到 1.0 m 判据，故缩为 3x3 内坑（900 m²，满蓄
   1800 m³）+ 一圈 z=4 过渡沿；平坦地形薄层扩散受曼宁/DepthThresh 限制，
   汇流速率如实记录，判据按任务书原样评估、不为过门而调参。
5. s8 HVAR 序列值 = 水面高程（boundary.cpp HVAR3：hflow = h0 − DEM），故
   「水位抬升 0→1.2 m」写为高程 5.0→6.2 m；南缘 HVAR 入流水体按通量符号归入
   Qin（boundary.cpp 南缘 Qy<0 → qinb），g15 分母 max(V0+Vrain+∫Qin,1) 有效。

产物：run 目录 outputs/synthetic/sgc_<name>/（不在 g15 扫描根
outputs/{ningming,jiulong,guigang} 内，不污染质量门）；报告
diagnostics/synthetic/sgc_report.json；审计 logs/stage3/run_sgc_synthetic_tests.json。
已有 results/res.mass 的 run 目录直接复用解析（报告记 reused=true，不重跑）；
写盘 OSError 22 重试一次（间隔 30 s），仍失败记 write_blocked 不崩溃。

用法：uv run python scripts/stage3/run_sgc_synthetic_tests.py [--dry-run] [--run-tag X]
--dry-run 只写 run 输入文件、不调 exe；run 目录默认保留（--keep 仅显式声明，
本脚本从不删除任何目录）。写入前先过 guard_no_overwrite（终稿 §10）。
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    OverwriteRefused,
    add_common_args,
    guard_no_overwrite,
    read_ascii_grid,
    run_frames,
    utc_now,
    write_json,
)

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
SYNTH_ROOT = PROJ / "outputs" / "synthetic"
REPORT_PATH = PROJ / "diagnostics" / "synthetic" / "sgc_report.json"
AUDIT_PATH = PROJ / "logs" / "stage3" / "run_sgc_synthetic_tests.json"

NCOLS, NROWS, DX = 60, 40, 10.0
CHAN_COL, CHAN_WIDTH = 30, 8.0                            # 河道所在列（0 起）与宽度
SGC_N, SGC_R, SGC_P, SGC_CHAN = 0.03, 0.55, 0.4, 1        # 1 = 矩形断面（lisflood.cpp 默认）
SIM_TIME, TSTEP = 3600.0, 0.5
MASSINT, SAVEINT, FPFRIC = 10.0, 60.0, 0.035
WET_MIN_M, E_MASS_MAX = 0.05, 1e-3                        # 湿像元阈值 / 质量门上限（同 g15）
GRID_HEADER = {"ncols": NCOLS, "nrows": NROWS, "xllcorner": 0.0, "yllcorner": 0.0,
               "cellsize": DX, "nodata_value": -9999.0}

# s1：坑内最小水深与坑蓄水漂移判据（任务书 §4.5 S1）
S1_PIT_DEPTH_MIN_M, S1_DRIFT_MAX = 1.0, 0.05
S1_DRIFT_FRAME_SEQ = 40                                   # t = 40×SAVEINT = 2400 s
# s2：薄层片流湿判据（1 mm ≈ DepthThresh 量级；0.05 m 口径下片流水深 2-4 cm 会误判全干）
S2_WET_THRESH_M, S2_DRY_BLOCK_LIMIT = 0.001, 20
# s5：Y 形河道汇流点行、两支流量与干流判据
S5_JUNCTION_ROW, S5_QOUT_TOL = 20, 0.10
# s7：末帧湿像元上限与全程最大水深上限
S7_END_WET_MAX, S7_MAX_DEPTH_M = 5, 1.5
# s8：南缘水位带（最南 5 行）湿像元下限
S8_BAND_ROWS, S8_BAND_WET_MIN = 5, 50

CASES = {
    "s1": {"title": "蓄水",
           "purpose": "单洼地蓄水：平坡 z=5，中心 3x3 洼地 z=3（一圈 z=4 过渡沿）；"
                      "雨 20 mm/h 至 1800 s 后归零，无入流、无 SGC（纯 acceleration 求解）",
           "rain_mmh": 20.0, "rain_end_s": 1800.0},
    "s2": {"title": "坡面",
           "purpose": "单向坡面降雨：平面 z=8→2（北高南低）；雨 15 mm/h 恒定 3600 s，"
                      "薄层片流向南自由出流，无入流、无 SGC",
           "rain_mmh": 15.0, "rain_end_s": 3600.0},
    "s3": {"title": "输水", "inflow_q_m3s": 5.0,
           "purpose": "河道输水不出槽：5 m³/s 恒定入流全部经槽从南缘自由出流",
           "bdy": [(5.0, 0.0), (5.0, 3600.0)]},
    "s4": {"title": "漫溢", "inflow_q_m3s": 40.0,
           "purpose": "超满岸输水：40 m³/s 超过约 37 m³/s 满岸能力，向两侧漫滩",
           "bdy": [(40.0, 0.0), (40.0, 3600.0)]},
    "s5": {"title": "汇合", "inflow_q_m3s": 10.0,
           "purpose": "两条支流汇合：Y 形 SGC 河道（两条阶梯支流自北缘两角汇入中心列干流），"
                      "北缘两股 QVAR 各 5 m³/s（qin_w/qin_e 双序列双 N 行），末端干流出流 ≈10 m³/s",
           "arm_q_m3s": 5.0},
    "s6": {"title": "退水", "inflow_q_m3s": 40.0,
           "purpose": "断流退水：40 m³/s 至 1740 s 后线性退坡（1800 s 起严格为 0），末帧水面明显消退",
           "bdy": [(40.0, 0.0), (40.0, 1740.0), (0.0, 1800.0), (0.0, 3600.0)]},
    "s7": {"title": "干湿",
           "purpose": "干湿转换：平坡 z=2 + 中心 6x6 洼地 z=1.7 + 两列浅沟向南渐降排空；"
                      "2 m³/s 脉冲 600 s（[540,600] 线性退坡）后归零，洼地经浅沟穿越 DepthThresh 排干",
           "pulse_q_m3s": 2.0},
    "s8": {"title": "边界水位",
           "purpose": "边界水位变化：平坡 z=5；南缘 HVAR 水面高程 5.0→6.2 m 线性缓升 1800 s 后持稳"
                      "（24 h 缓升类比压缩到 30 min，G13 激波门），无雨、无入流、无 SGC",
           "stage_start_m": 5.0, "stage_end_m": 6.2, "ramp_end_s": 1800.0},
}


def bankfull_depth(width_m: float = CHAN_WIDTH, r: float = SGC_R, p: float = SGC_P) -> float:
    """SGC 满岸水深 = r·w^p（sgc.cpp CalcSGCz：z_bed = z_bank − r·w^p）。"""
    return r * width_m ** p


def build_grids() -> dict[str, np.ndarray]:
    """合成地形：平面坡 z=10（北）→5（南）；宽 8 m 河道沿中心列贯通南北；bank=DEM。"""
    z = 10.0 - 5.0 * np.arange(NROWS) / (NROWS - 1)
    dem = np.repeat(z[:, None], NCOLS, axis=1)
    width = np.zeros((NROWS, NCOLS))
    width[:, CHAN_COL] = CHAN_WIDTH
    return {"dem": dem, "width": width, "bank": dem.copy()}


def inflow_x_range() -> tuple[float, float]:
    """北缘入流 x 区间（自 blx 起算）：[col·dx,(col+1)·dx] 恰好只覆盖河道单列。"""
    return CHAN_COL * DX, (CHAN_COL + 1) * DX


def build_grids_s1() -> dict[str, np.ndarray]:
    """S1 地形：全平 z=5；中心 3x3 洼地 z=3、外一圈 z=4 平滑过渡沿（无 SGC）。

    任务书示例 8x8 洼地对 20 mm/h×1800 s 雨量（2400 m³）最多蓄 0.375 m，
    达不到「坑内水深 ≥1.0 m」判据，故缩为 3x3 内坑（900 m²，满蓄 1800 m³）
    + 一圈 z=4 过渡沿（16 格，再蓄 1600 m³）；见模块 docstring 约束 4。
    """
    dem = np.full((NROWS, NCOLS), 5.0)
    r0, c0 = NROWS // 2 - 2, NCOLS // 2 - 2          # 5x5 外框左上角（rows 18-22, cols 28-32）
    dem[r0:r0 + 5, c0:c0 + 5] = 4.0                  # 过渡沿
    dem[r0 + 1:r0 + 4, c0 + 1:c0 + 4] = 3.0          # 内坑 3x3（rows 19-21, cols 29-31）
    return {"dem": dem}


def build_grids_s2() -> dict[str, np.ndarray]:
    """S2 地形：单向坡面 z = 8 − 6·row/(nrows−1)（北 8 m → 南 2 m），无 SGC。"""
    z = 8.0 - 6.0 * np.arange(NROWS) / (NROWS - 1)
    return {"dem": np.repeat(z[:, None], NCOLS, axis=1)}


def s5_staircase_path(start_col: int, end_col: int, row_end: int) -> list[tuple[int, int]]:
    """(start_col,0) → (end_col,row_end) 的 4 连通阶梯路径：同行横补、跨行直落。

    每行沿上一行终点水平补齐到本行目标列，再垂直下降一行；相邻格必共边，
    保证 SGC 河道逐格连通（sgc.cpp 逐格按 SGCwidth 参与四向通量）。
    """
    cells = [(start_col, 0)]
    prev = start_col
    for r in range(1, row_end + 1):
        cur = start_col + (end_col - start_col) * r // row_end
        step = 1 if cur >= prev else -1
        cells.extend((c, r - 1) for c in range(prev + step, cur + step, step))
        cells.append((cur, r))
        prev = cur
    return cells


def build_grids_s5() -> dict[str, np.ndarray]:
    """S5 地形：S3 同款南倾平面（z=10→5）+ Y 形 SGC 河道（宽 8 m，bank=DEM）。

    两条支流自北缘两角（col 0 与 col 59）阶梯下切，在 (30,20) 汇入中心列干流，
    干流沿 col 30 贯通至南缘（row 39）。
    """
    base = build_grids()
    width = np.zeros((NROWS, NCOLS))
    paths = (
        s5_staircase_path(0, CHAN_COL, S5_JUNCTION_ROW),                 # 西支
        s5_staircase_path(NCOLS - 1, CHAN_COL, S5_JUNCTION_ROW),         # 东支
        [(CHAN_COL, r) for r in range(S5_JUNCTION_ROW, NROWS)],          # 干流
    )
    for path in paths:
        for c, r in path:
            width[r, c] = CHAN_WIDTH
    return {"dem": base["dem"], "width": width, "bank": base["bank"]}


def build_grids_s7() -> dict[str, np.ndarray]:
    """S7 地形：全平 z=2；中心 6x6 洼地 z=1.7（rows 17-22, cols 27-32）；cols 29-30
    浅沟自洼底向南渐降（0.02 m/格），使洼地可经南缘 FREE 排空。"""
    dem = np.full((NROWS, NCOLS), 2.0)
    r0, r1, c0, c1 = NROWS // 2 - 3, NROWS // 2 + 2, NCOLS // 2 - 3, NCOLS // 2 + 2
    dem[r0:r1 + 1, c0:c1 + 1] = 1.7
    for r in range(r1 + 1, NROWS):
        dem[r, NCOLS // 2 - 1:NCOLS // 2 + 1] = 1.7 - 0.02 * (r - r1)
    return {"dem": dem}


def build_grids_s8() -> dict[str, np.ndarray]:
    """S8 地形：全平 z=5（南缘 HVAR 抬升形成淹没带），无 SGC。"""
    return {"dem": np.full((NROWS, NCOLS), 5.0)}


def case_grids(name: str) -> dict[str, np.ndarray]:
    """按算例返回地形/河道栅格；s3/s4/s6 共用 build_grids，未知算例报错。"""
    if name in ("s3", "s4", "s6"):
        return build_grids()
    builders = {"s1": build_grids_s1, "s2": build_grids_s2, "s5": build_grids_s5,
                "s7": build_grids_s7, "s8": build_grids_s8}
    if name not in builders:
        raise ValueError(f"未知算例 {name}")
    return builders[name]()


def render_grid(data: np.ndarray, header: dict[str, float]) -> str:
    """ESRI ASCII 文本（6 行头 + TAB %.3f 体，首行=最北行），与 grid_io 写出同构。

    以文本形式返回是为让 build_case 可返回纯文件内容，单元测试无需真实 run 目录。
    """
    buf = io.StringIO()
    np.savetxt(buf, np.asarray(data), fmt="%.3f", delimiter="\t")
    h = header
    return (
        f"ncols     {int(h['ncols'])}\n"
        f"nrows     {int(h['nrows'])}\n"
        f"xllcorner     {float(h['xllcorner']):.1f}\n"
        f"yllcorner     {float(h['yllcorner']):.1f}\n"
        f"cellsize      {float(h['cellsize']):.1f}\n"
        f"NODATA_value  {float(h['nodata_value']):.1f}\n"
        + buf.getvalue()
    )


def assemble_par(*, dem: str = "dem.asc", bci: str = "bci.bci", bdy: str | None = None,
                 rainfall: str | None = None, sim_time: float = SIM_TIME,
                 tstep: float = TSTEP, massint: float = MASSINT, saveint: float = SAVEINT,
                 fpfric: float = FPFRIC, sgc: bool = True, sgc_width: str = "sgc_width.asc",
                 sgc_bank: str = "sgc_bank.asc", sgc_chan: int = SGC_CHAN,
                 sgc_n: float = SGC_N, sgc_r: float = SGC_R, sgc_p: float = SGC_P) -> str:
    """装配 run.par 文本：TAB 分隔 key\\tvalue；开关型参数（acceleration/SGC_enable）为裸词行。"""
    rows: list[tuple[str, object]] = [
        ("DEMfile", dem), ("bcifile", bci),
        ("resroot", "res"), ("dirroot", "results"),
        ("sim_time", sim_time), ("initial_tstep", tstep),
        ("massint", massint), ("saveint", saveint), ("fpfric", fpfric),
        ("acceleration", None),
    ]
    if bdy:
        rows.append(("bdyfile", bdy))
    if rainfall:
        rows.append(("rainfall", rainfall))
    if sgc:
        rows += [("SGC_enable", None), ("SGCwidth", sgc_width), ("SGCbank", sgc_bank),
                 ("SGCchan", sgc_chan), ("SGCn", sgc_n), ("SGCr", sgc_r), ("SGCp", sgc_p)]
    return "\n".join(k if v is None else f"{k}\t{v}" for k, v in rows) + "\n"


def render_bci(inflow_x_start: float, inflow_x_end: float) -> str:
    """北缘单列 QVAR 入流 + 南缘 FREE(0.0005)；未列出的 W/E 边自动闭合。"""
    return (f"N\t{inflow_x_start:.1f}\t{inflow_x_end:.1f}\tQVAR\tqin\n"
            "S\t-99999999\t99999999\tFREE 0.0005\n")


def render_bdy(
    points_q_m3s: list[tuple[float, float]], dx: float, *, name: str = "qin",
    comment: str = "", n_cells: int = 1,
) -> str:
    """写 QVAR 入流时序：文件值 = Q_m3s/(n_cells·dx)（求解器逐边界像元乘 dx），
    时间严格递增；n_cells 为该 bci 行 x 区间覆盖的边界像元数（默认单列）。"""
    times = [t for _, t in points_q_m3s]
    if len(times) < 2 or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("bdy 时间点必须 >=2 且严格递增")
    lines = [f"# {comment}" if comment else "# synthetic inflow", name,
             f"{len(points_q_m3s)} seconds"]
    lines += [f"{q / (n_cells * dx):.4f}\t{t:.0f}" for q, t in points_q_m3s]
    return "\n".join(lines) + "\n"


def render_bdy_multi(
    series: list[tuple[str, list[tuple[float, float]], int]], *, comment: str = ""
) -> str:
    """一条 .bdy 内多个命名 QVAR 序列（input.cpp LoadBCVar 逐段读取，首行注释跳过）。

    每段 = (序列名, (Q_m3s, 时间) 点列, 覆盖像元数)；文件值 = Q_m3s/(n_cells·dx)。
    """
    lines = [f"# {comment}" if comment else "# synthetic inflow"]
    for name, points, n_cells in series:
        lines += render_bdy(points, DX, name=name, n_cells=n_cells).splitlines()[1:]
    return "\n".join(lines) + "\n"


def render_rain(points_mmh: list[tuple[float, float]], *, comment: str = "") -> str:
    """rain 文件：首行注释被跳过（skipFirstLine+`#`），下一行必须是 `<count> seconds`
    （input.cpp LoadTimeSeries 不读序列名，与 .bdy 不同）；值单位 mm/h（换算 m/s）。"""
    lines = [f"# {comment}" if comment else "# synthetic rain",
             f"{len(points_mmh)} seconds"]
    lines += [f"{v:.4f}\t{t:.0f}" for v, t in points_mmh]
    return "\n".join(lines) + "\n"


def render_stage_series(
    points_m: list[tuple[float, float]], *, name: str, comment: str = ""
) -> str:
    """HVAR 水位序列：值 = 水面高程 m（boundary.cpp HVAR3 按 h0 − DEM 取水深）。"""
    lines = [f"# {comment}" if comment else "# synthetic stage series", name,
             f"{len(points_m)} seconds"]
    lines += [f"{v:.4f}\t{t:.0f}" for v, t in points_m]
    return "\n".join(lines) + "\n"


def render_bci_lines(lines: list[str]) -> str:
    """多行 .bci 文本（未列出的边自动闭合）；供多入流/HVAR 边界算例拼装。"""
    return "\n".join(lines) + "\n"


def build_case(name: str, *, inflow_mode: str = "two_series") -> dict[str, str]:
    """返回算例 <name> 的全部输入文件内容（文件名 → 文本）。

    s3/s4/s6 为既有 SGC 算例（保持原行为）；s1/s2/s5/s7/s8 为计划 §4.5 新增。
    s5 的 inflow_mode：two_series（默认，北缘两条 QVAR 行各 5 m³/s）或
    combined（exe 拒绝双序列时的回退：一条全缘 QVAR 共 10 m³/s）。
    """
    spec = CASES[name]
    if name in ("s3", "s4", "s6"):
        grids = build_grids()
        x0, x1 = inflow_x_range()
        meta = {
            "case": name, "title": spec["title"], "purpose": spec["purpose"],
            "grid": {"ncols": NCOLS, "nrows": NROWS, "dx_m": DX, "drop_north_south_m": 5.0},
            "sgc": {"width_m": CHAN_WIDTH, "n": SGC_N, "r": SGC_R, "p": SGC_P, "chan_type": SGC_CHAN,
                    "bankfull_depth_m": round(bankfull_depth(), 3)},
            "inflow": {"q_m3s": spec["inflow_q_m3s"], "column": CHAN_COL, "x_range_m": [x0, x1]},
            "sim_time_s": SIM_TIME, "generated_utc": utc_now(),
        }
        return {
            "dem.asc": render_grid(grids["dem"], GRID_HEADER),
            "sgc_width.asc": render_grid(grids["width"], GRID_HEADER),
            "sgc_bank.asc": render_grid(grids["bank"], GRID_HEADER),
            "bci.bci": render_bci(x0, x1),
            "inflow.bdy": render_bdy(spec["bdy"], DX, comment=f"SGC synthetic {name}: value = Q_m3s/dx"),
            "run.par": assemble_par(bdy="inflow.bdy"),
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        }
    if name == "s1":
        grids = build_grids_s1()
        meta = {
            "case": name, "title": spec["title"], "purpose": spec["purpose"],
            "grid": {"ncols": NCOLS, "nrows": NROWS, "dx_m": DX, "flat_z_m": 5.0},
            "depression": {"inner_cells": 9, "inner_z_m": 3.0, "ring_z_m": 4.0,
                           "inner_rows": [19, 21], "inner_cols": [29, 31],
                           "note": "任务书示例 8x8 对 2400 m³ 雨量蓄不到 1.0 m 水深，"
                                   "缩为 3x3 内坑 + 一圈 z=4 过渡沿（docstring 约束 4）"},
            "rain": {"file": "train.rain", "mmh_1": spec["rain_mmh"], "end_s": spec["rain_end_s"]},
            "boundary": "S FREE 0.0005，其余闭合",
            "sim_time_s": SIM_TIME, "generated_utc": utc_now(),
        }
        return {
            "dem.asc": render_grid(grids["dem"], GRID_HEADER),
            "bci.bci": render_bci_lines(["S\t-99999999\t99999999\tFREE 0.0005"]),
            "train.rain": render_rain([(spec["rain_mmh"], 0.0), (spec["rain_mmh"], spec["rain_end_s"] - 1.0),
                                       (0.0, spec["rain_end_s"]), (0.0, SIM_TIME)],
                                      comment=f"synthetic {name}: mm/h"),
            "run.par": assemble_par(bdy=None, rainfall="train.rain", sgc=False),
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        }
    if name == "s2":
        grids = build_grids_s2()
        meta = {
            "case": name, "title": spec["title"], "purpose": spec["purpose"],
            "grid": {"ncols": NCOLS, "nrows": NROWS, "dx_m": DX,
                     "z_north_m": 8.0, "z_south_m": 2.0},
            "rain": {"file": "train.rain", "mmh_1": spec["rain_mmh"], "end_s": spec["rain_end_s"]},
            "wet_thresh_m": S2_WET_THRESH_M,
            "boundary": "S FREE 0.0005，其余闭合",
            "sim_time_s": SIM_TIME, "generated_utc": utc_now(),
        }
        return {
            "dem.asc": render_grid(grids["dem"], GRID_HEADER),
            "bci.bci": render_bci_lines(["S\t-99999999\t99999999\tFREE 0.0005"]),
            "train.rain": render_rain([(spec["rain_mmh"], 0.0), (spec["rain_mmh"], SIM_TIME)],
                                      comment=f"synthetic {name}: mm/h"),
            "run.par": assemble_par(bdy=None, rainfall="train.rain", sgc=False),
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        }
    if name == "s5":
        grids = build_grids_s5()
        arm_q = spec["arm_q_m3s"]
        arm_points = [(arm_q, 0.0), (arm_q, SIM_TIME)]
        if inflow_mode == "two_series":
            xw = [0.0, DX]
            xe = [(NCOLS - 1) * DX, NCOLS * DX]
            bci = render_bci_lines([
                f"N\t{xw[0]:.1f}\t{xw[1]:.1f}\tQVAR\tqin_w",
                f"N\t{xe[0]:.1f}\t{xe[1]:.1f}\tQVAR\tqin_e",
                "S\t-99999999\t99999999\tFREE 0.0005",
            ])
            bdy = render_bdy_multi([("qin_w", arm_points, 1), ("qin_e", arm_points, 1)],
                                   comment=f"SGC synthetic {name}: two N-edge QVAR arms, value = Q_m3s/dx")
            inflow_meta = {"mode": "two_series", "total_q_m3s": 2 * arm_q,
                           "x_ranges_m": {"qin_w": xw, "qin_e": xe}}
        else:
            xall = [0.0, NCOLS * DX]
            bci = render_bci_lines([
                f"N\t{xall[0]:.1f}\t{xall[1]:.1f}\tQVAR\tqin",
                "S\t-99999999\t99999999\tFREE 0.0005",
            ])
            bdy = render_bdy([(2 * arm_q, 0.0), (2 * arm_q, SIM_TIME)], DX, name="qin",
                             n_cells=NCOLS,
                             comment=f"SGC synthetic {name} fallback: single N-edge QVAR, value = Q_m3s/(60*dx)")
            inflow_meta = {"mode": "combined", "total_q_m3s": 2 * arm_q,
                           "x_ranges_m": {"qin": xall}, "n_cells": NCOLS}
        meta = {
            "case": name, "title": spec["title"], "purpose": spec["purpose"],
            "grid": {"ncols": NCOLS, "nrows": NROWS, "dx_m": DX, "drop_north_south_m": 5.0},
            "sgc": {"width_m": CHAN_WIDTH, "n": SGC_N, "r": SGC_R, "p": SGC_P, "chan_type": SGC_CHAN,
                    "bankfull_depth_m": round(bankfull_depth(), 3),
                    "junction": {"row": S5_JUNCTION_ROW, "col": CHAN_COL}},
            "inflow": inflow_meta,
            "sim_time_s": SIM_TIME, "generated_utc": utc_now(),
        }
        return {
            "dem.asc": render_grid(grids["dem"], GRID_HEADER),
            "sgc_width.asc": render_grid(grids["width"], GRID_HEADER),
            "sgc_bank.asc": render_grid(grids["bank"], GRID_HEADER),
            "bci.bci": bci,
            "inflow.bdy": bdy,
            "run.par": assemble_par(bdy="inflow.bdy"),
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        }
    if name == "s7":
        grids = build_grids_s7()
        x0, x1 = (NCOLS // 2 - 1) * DX, (NCOLS // 2 + 1) * DX     # cols 29-30，2 个边界像元
        meta = {
            "case": name, "title": spec["title"], "purpose": spec["purpose"],
            "grid": {"ncols": NCOLS, "nrows": NROWS, "dx_m": DX, "flat_z_m": 2.0},
            "pit": {"cells": 36, "z_m": 1.7, "rows": [17, 22], "cols": [27, 32]},
            "trench": {"cols": [29, 30], "rows": [23, 39], "bed_drop_per_cell_m": 0.02,
                       "z_south_edge_m": 1.36},
            "inflow": {"q_m3s": spec["pulse_q_m3s"], "x_range_m": [x0, x1], "n_cells": 2,
                       "ramp_off_s": [540.0, 600.0]},
            "boundary": "N QVAR qin + S FREE 0.0005，其余闭合",
            "sim_time_s": SIM_TIME, "generated_utc": utc_now(),
        }
        return {
            "dem.asc": render_grid(grids["dem"], GRID_HEADER),
            "bci.bci": render_bci_lines([
                f"N\t{x0:.1f}\t{x1:.1f}\tQVAR\tqin",
                "S\t-99999999\t99999999\tFREE 0.0005",
            ]),
            "inflow.bdy": render_bdy([(spec["pulse_q_m3s"], 0.0), (spec["pulse_q_m3s"], 540.0),
                                      (0.0, 600.0), (0.0, SIM_TIME)], DX, name="qin",
                                     n_cells=2,
                                     comment=f"synthetic {name}: value = Q_m3s/(2*dx)"),
            "run.par": assemble_par(bdy="inflow.bdy", sgc=False),
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        }
    if name == "s8":
        grids = build_grids_s8()
        z0, z1 = spec["stage_start_m"], spec["stage_end_m"]
        meta = {
            "case": name, "title": spec["title"], "purpose": spec["purpose"],
            "grid": {"ncols": NCOLS, "nrows": NROWS, "dx_m": DX, "flat_z_m": 5.0},
            "hvar": {"series": "qhvar", "value_semantics": "水面高程 m（boundary.cpp h0 − DEM）",
                     "stage_m": [z0, z1], "ramp_end_s": spec["ramp_end_s"],
                     "band_rows": [NROWS - S8_BAND_ROWS, NROWS - 1]},
            "boundary": "S HVAR qhvar，其余闭合",
            "sim_time_s": SIM_TIME, "generated_utc": utc_now(),
        }
        return {
            "dem.asc": render_grid(grids["dem"], GRID_HEADER),
            "bci.bci": render_bci_lines(["S\t-99999999\t99999999\tHVAR\tqhvar"]),
            "inflow.bdy": render_stage_series([(z0, 0.0), (z1, spec["ramp_end_s"]), (z1, SIM_TIME)],
                                              name="qhvar",
                                              comment=f"synthetic {name}: water surface elevation m"),
            "run.par": assemble_par(bdy="inflow.bdy", sgc=False),
            "meta.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        }
    raise ValueError(f"未知算例 {name}")


def write_case_dir(run_dir: Path, contents: dict[str, str]) -> None:
    """把 build_case 的内容落盘；par/bci/bdy/栅格为 ASCII LF，meta.json 为 UTF-8。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    for fname, text in contents.items():
        enc = "utf-8" if fname.endswith(".json") else "ascii"
        (run_dir / fname).write_text(text, encoding=enc, newline="\n")


def run_exe(run_dir: Path, *, timeout_s: float = 1800.0) -> dict:
    """在 run_dir 内调用 lisflood.exe run.par，stdout+stderr 落 run.log。"""
    proc = subprocess.Popen([str(LISFLOOD), "run.par"], cwd=run_dir,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        out, _ = proc.communicate()
    (run_dir / "run.log").write_text(out, encoding="utf-8", errors="replace")
    return {"exit": proc.returncode, "timed_out": timed_out}


def has_fatal(log_text: str) -> list[str]:
    """run.log 致命特征（源码实际打印的串）：pars.cpp 的 "ERROR ..."、
    sgc.cpp 通道类型出错的 "should not be here"、SGC 不支持类 "not supported"。"""
    hits = []
    if "ERROR" in log_text:
        hits.append("ERROR")
    low = log_text.lower()
    hits += [n for n in ("not supported", "should not be here") if n in low]
    return hits


def mass_rows(text: str) -> list[dict]:
    """res.mass 表体解析（口径同 g15_mass_recheck：空白分列，兼容 CRLF）。"""
    lines = text.splitlines()
    keys = lines[0].split()
    return [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]


def e_mass_from_rows(rows: list[dict]) -> dict:
    """E_mass = |Ve − V0 − Vrain − (∫Qin − ∫Qout)| / max(V0+Vrain+∫Qin, 1)，梯形积分。"""
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"])
               for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"])
                for a, b in zip(rows, rows[1:]))
    v0, ve = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    return {"v0_m3": v0, "v_end_m3": ve, "v_rain_m3": v_rain,
            "v_in_m3": v_in, "v_out_m3": v_out,
            "e_mass": abs(resid) / max(v0 + v_rain + v_in, 1.0),
            "qout_last": rows[-1]["Qout"]}


def parse_res_mass(path: str | Path) -> dict:
    return e_mass_from_rows(mass_rows(Path(path).read_text(encoding="utf-8", errors="replace")))


def wet_counts(path: str | Path, channel_mask: np.ndarray) -> dict[str, int]:
    """湿像元计数（水深 > 0.05 m）：总数与槽外（非河道列）数。"""
    grid = read_ascii_grid(path)
    wet = grid.data > WET_MIN_M
    return {"wet_total": int(wet.sum()), "off_channel": int((wet & ~channel_mask).sum())}


def frame_wet_series(results_dir: str | Path, channel_mask: np.ndarray) -> list[dict]:
    """逐帧 res-XXXX.wd 湿像元序列（按帧号升序），用于 s6 退水趋势。"""
    out = []
    for seq, path in run_frames(results_dir):
        out.append({"seq": seq, "t_s": seq * SAVEINT, **wet_counts(path, channel_mask)})
    if not out:
        raise FileNotFoundError(f"{results_dir} 下没有 res-XXXX.wd 帧")
    return out


def frame_depths(results_dir: str | Path) -> dict[int, np.ndarray]:
    """逐帧水深数组（帧号 → float64 二维数组），用于 s1 坑蓄水 / s7 逐帧极值。"""
    frames = {seq: read_ascii_grid(path).data.astype(np.float64)
              for seq, path in run_frames(results_dir)}
    if not frames:
        raise FileNotFoundError(f"{results_dir} 下没有 res-XXXX.wd 帧")
    return frames


def count_dry_blocks_2x2(depth: np.ndarray, thresh_m: float) -> int:
    """全干（≤ thresh_m）2x2 块计数——s2 上半场棋盘伪影检查（向量化）。"""
    dry = depth <= thresh_m
    blocks = (dry[:-1, :-1] & dry[:-1, 1:] & dry[1:, :-1] & dry[1:, 1:])
    return int(blocks.sum())


def s5_stem_mask() -> np.ndarray:
    """s5 干流掩膜：col 30 自汇流点（row 20）至南缘，用于断湿检查。"""
    stem = np.zeros((NROWS, NCOLS), dtype=bool)
    stem[S5_JUNCTION_ROW:, CHAN_COL] = True
    return stem


def s8_band_mask() -> np.ndarray:
    """s8 南缘水位带掩膜（最南 S8_BAND_ROWS 行）。"""
    band = np.zeros((NROWS, NCOLS), dtype=bool)
    band[NROWS - S8_BAND_ROWS:, :] = True
    return band


def evaluate_case(
    name: str,
    *,
    e_mass: float,
    qout_last: float,
    wet_off_max: int,
    wet_end: int | None,
    wet_peak: int | None,
    inflow_q: float,
    pit_depth_min: float | None = None,
    pit_drift: float | None = None,
    wet_fraction: float | None = None,
    upper_dry_blocks: int | None = None,
    stem_dry_cells: int | None = None,
    end_wet_total: int | None = None,
    max_depth_run: float | None = None,
    south_band_wet: int | None = None,
    res_max_finite: bool | None = None,
) -> dict:
    """纯判据评估（不读文件，便于单测）：返回 {pass, criteria:{名:{value,limit,pass}}}。

    s1/s2/s5/s7/s8 的专属判据由对应 kwargs 给出；缺失（None）一律判 FAIL，
    不静默放行。s3/s4/s6 分支保持原口径不变。
    """
    checks = {"e_mass_le_1e-3": {"value": e_mass, "limit": E_MASS_MAX,
                                 "pass": bool(e_mass <= E_MASS_MAX)}}
    if name == "s3":
        checks["qout_within_20pct_of_qin"] = {
            "value": qout_last, "limit": inflow_q,
            "pass": bool(abs(qout_last - inflow_q) <= 0.2 * inflow_q)}
        checks["off_channel_wet_le_2"] = {"value": wet_off_max, "limit": 2,
                                          "pass": bool(wet_off_max <= 2)}
    elif name == "s4":
        checks["off_channel_wet_gt_100"] = {"value": wet_off_max, "limit": 100,
                                            "pass": bool(wet_off_max > 100)}
    elif name == "s6":
        ratio = (wet_end / wet_peak) if wet_peak else 1.0  # 峰值为 0 → 判 FAIL
        checks["end_wet_le_20pct_of_peak"] = {"value": ratio, "limit": 0.2,
                                              "pass": bool(wet_peak and ratio <= 0.2)}
    elif name == "s1":
        checks["pit_depth_min_ge_1m"] = {
            "value": pit_depth_min, "limit": S1_PIT_DEPTH_MIN_M,
            "pass": bool(pit_depth_min is not None and pit_depth_min >= S1_PIT_DEPTH_MIN_M)}
        checks["pit_volume_drift_le_5pct"] = {
            "value": pit_drift, "limit": S1_DRIFT_MAX,
            "pass": bool(pit_drift is not None and pit_drift <= S1_DRIFT_MAX)}
    elif name == "s2":
        checks["wet_fraction_ge_80pct"] = {
            "value": wet_fraction, "limit": 0.8,
            "pass": bool(wet_fraction is not None and wet_fraction >= 0.8)}
        checks["upper_dry_blocks_le_20"] = {
            "value": upper_dry_blocks, "limit": S2_DRY_BLOCK_LIMIT,
            "pass": bool(upper_dry_blocks is not None and upper_dry_blocks <= S2_DRY_BLOCK_LIMIT)}
    elif name == "s5":
        checks["qout_within_10pct_of_qin"] = {
            "value": qout_last, "limit": inflow_q,
            "pass": bool(abs(qout_last - inflow_q) <= S5_QOUT_TOL * inflow_q)}
        checks["stem_no_dry_break"] = {
            "value": stem_dry_cells, "limit": 0,
            "pass": bool(stem_dry_cells is not None and stem_dry_cells == 0)}
    elif name == "s7":
        checks["res_max_finite"] = {
            "value": res_max_finite, "limit": True, "pass": bool(res_max_finite)}
        checks["end_wet_le_5"] = {
            "value": end_wet_total, "limit": S7_END_WET_MAX,
            "pass": bool(end_wet_total is not None and end_wet_total <= S7_END_WET_MAX)}
        checks["max_depth_le_1p5m"] = {
            "value": max_depth_run, "limit": S7_MAX_DEPTH_M,
            "pass": bool(max_depth_run is not None and max_depth_run <= S7_MAX_DEPTH_M)}
    elif name == "s8":
        checks["south_band_wet_ge_50"] = {
            "value": south_band_wet, "limit": S8_BAND_WET_MIN,
            "pass": bool(south_band_wet is not None and south_band_wet >= S8_BAND_WET_MIN)}
        checks["res_max_finite"] = {
            "value": res_max_finite, "limit": True, "pass": bool(res_max_finite)}
    else:
        raise ValueError(f"未知算例 {name}")
    return {"pass": all(c["pass"] for c in checks.values()), "criteria": checks}


def log_tail(log_text: str, n: int = 15) -> str:
    return "\n".join(log_text.splitlines()[-n:])


def collect_case(name: str, run_dir: Path, *, run_rec: dict, reused: bool = False,
                 inflow_mode: str | None = None) -> dict:
    """解析既有 run 目录（res.mass / res.max / 帧）→ 指标 → 判据；不写任何文件。"""
    mass_path = run_dir / "results" / "res.mass"
    log_path = run_dir / "run.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    fatal = has_fatal(log_text)
    if not mass_path.is_file():
        return {"case": name, "pass": False, "error": "res.mass 未生成", "run": run_rec,
                "fatal": fatal, "log_tail": log_tail(log_text)}
    if reused and inflow_mode is None and name == "s5":
        meta_path = run_dir / "meta.json"      # 复用时回退模式以落盘 meta 为准
        if meta_path.is_file():
            inflow_mode = json.loads(meta_path.read_text(encoding="utf-8")
                                     ).get("inflow", {}).get("mode")
    mass = parse_res_mass(mass_path)
    spec = CASES[name]
    grids = case_grids(name)
    channel = grids["width"] > 0 if "width" in grids else np.zeros((NROWS, NCOLS), dtype=bool)
    results_dir = run_dir / "results"
    metrics: dict = {"wet_off_max": 0, "wet_end": None, "wet_peak": None}
    if name == "s6":
        series = frame_wet_series(results_dir, channel)
        metrics["wet_peak"] = max(s["wet_total"] for s in series)
        metrics["wet_end"] = series[-1]["wet_total"]
        metrics["wet_off_max"] = max(s["off_channel"] for s in series)
        wet_rec: dict = {"first": series[0], "peak": metrics["wet_peak"],
                         "end": metrics["wet_end"]}
    elif name in ("s3", "s4"):
        wet_rec = wet_counts(results_dir / "res.max", channel)
        metrics["wet_off_max"] = wet_rec["off_channel"]
    elif name == "s1":
        frames = frame_depths(results_dir)
        dem = grids["dem"]
        depression, inner = dem < 5.0, dem == 3.0
        end_seq = max(frames)
        metrics["pit_depth_min"] = float(frames[end_seq][inner].min())
        v_start = float(frames[S1_DRIFT_FRAME_SEQ][depression].sum() * DX * DX)
        v_end = float(frames[end_seq][depression].sum() * DX * DX)
        metrics["pit_drift"] = abs(v_end - v_start) / max(v_start, 1.0)
        wet_rec = {"pit_volume_frame40_m3": round(v_start, 1),
                   "pit_volume_end_m3": round(v_end, 1), "end_frame_seq": end_seq}
    elif name == "s2":
        frames = frame_depths(results_dir)
        end = frames[max(frames)]
        wet = end > S2_WET_THRESH_M
        metrics["wet_fraction"] = float(wet.sum() / wet.size)
        metrics["upper_dry_blocks"] = count_dry_blocks_2x2(end[:NROWS // 2], S2_WET_THRESH_M)
        wet_rec = {"wet_thresh_m": S2_WET_THRESH_M, "wet_total_end": int(wet.sum()),
                   "wet_total_end_005m": int((end > WET_MIN_M).sum())}
    elif name == "s5":
        resmax = read_ascii_grid(results_dir / "res.max").data.astype(np.float64)
        stem = s5_stem_mask()
        metrics["stem_dry_cells"] = int((resmax[stem] <= WET_MIN_M).sum())
        wet_rec = wet_counts(results_dir / "res.max", channel)
        metrics["wet_off_max"] = wet_rec["off_channel"]
    elif name == "s7":
        resmax = read_ascii_grid(results_dir / "res.max").data.astype(np.float64)
        frames = frame_depths(results_dir)
        metrics["res_max_finite"] = bool(np.isfinite(resmax).all()
                                         and all(np.isfinite(d).all() for d in frames.values()))
        metrics["end_wet_total"] = int((frames[max(frames)] > WET_MIN_M).sum())
        metrics["max_depth_run"] = float(max(d.max() for d in frames.values()))
        wet_rec = {"frames": len(frames), "end_wet_total": metrics["end_wet_total"],
                   "max_depth_m": round(metrics["max_depth_run"], 3)}
    elif name == "s8":
        resmax = read_ascii_grid(results_dir / "res.max").data.astype(np.float64)
        frames = frame_depths(results_dir)
        end = frames[max(frames)]
        band = s8_band_mask()
        metrics["south_band_wet"] = int(((resmax > WET_MIN_M) & band).sum())
        metrics["res_max_finite"] = bool(np.isfinite(resmax).all())
        wet_rec = {"band_wet_resmax": metrics["south_band_wet"],
                   "band_wet_end": int(((end > WET_MIN_M) & band).sum()),
                   "wet_total_end": int((end > WET_MIN_M).sum())}
    else:
        raise ValueError(f"未知算例 {name}")
    extra_keys = ("pit_depth_min", "pit_drift", "wet_fraction", "upper_dry_blocks",
                  "stem_dry_cells", "end_wet_total", "max_depth_run", "south_band_wet",
                  "res_max_finite")
    verdict = evaluate_case(name, e_mass=mass["e_mass"], qout_last=mass["qout_last"],
                            wet_off_max=metrics["wet_off_max"], wet_end=metrics["wet_end"],
                            wet_peak=metrics["wet_peak"],
                            inflow_q=spec.get("inflow_q_m3s", 0.0),
                            **{k: metrics[k] for k in extra_keys if k in metrics})
    supported = not fatal and not run_rec.get("timed_out", False)
    rec = {"case": name, "e_mass": mass["e_mass"], "v_in_m3": round(mass["v_in_m3"], 1),
           "v_out_m3": round(mass["v_out_m3"], 1), "v_end_m3": round(mass["v_end_m3"], 1),
           "qout_last": mass["qout_last"], "wet": wet_rec, "criteria": verdict["criteria"],
           "run": run_rec, "fatal": fatal, "sgc_ran_ok": bool(supported),
           "pass": bool(supported and verdict["pass"])}
    if reused:
        rec["reused"] = True
    if inflow_mode and inflow_mode != "two_series":
        rec["inflow_mode"] = inflow_mode
    return rec


def run_one_case(name: str, run_dir: Path, *, dry_run: bool, timeout_s: float,
                 audit: AuditLog) -> dict:
    """单算例：写输入 →（可选）跑 exe → 解析 res.mass / 湿像元 → 判据。

    s5 先按双 QVAR 序列（两条 N 行各 5 m³/s）试跑；exe 拒绝（run.log 致命特征
    或未产出 res.mass）且首跑无结果时，回退单条全缘 QVAR（10 m³/s）重跑一次。
    """
    guard_no_overwrite(run_dir)  # 终稿 §10：已有 res.mass 的 run 目录禁止覆盖
    audit.step("write_inputs", case=name, run_dir=run_dir.name)
    write_case_dir(run_dir, build_case(name))
    if dry_run:
        return {"case": name, "skipped": "dry-run：只写输入，未跑 exe", "pass": None}
    audit.step("run_exe", case=name)
    run_rec = run_exe(run_dir, timeout_s=timeout_s)
    inflow_mode = None
    if name == "s5":
        mass_path = run_dir / "results" / "res.mass"
        log_path = run_dir / "run.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        if not mass_path.is_file() and (has_fatal(log_text) or run_rec.get("exit") != 0):
            audit.step("s5_fallback_combined_qvar", case=name)
            guard_no_overwrite(run_dir)
            write_case_dir(run_dir, build_case(name, inflow_mode="combined"))
            run_rec = run_exe(run_dir, timeout_s=timeout_s)
            inflow_mode = "combined_fallback"
    return collect_case(name, run_dir, run_rec=run_rec, inflow_mode=inflow_mode)


def main() -> int:
    parser = argparse.ArgumentParser(description="合成算例能力核证（计划任务4 + §4.5 八算例 / ADR-0020 护栏）")
    add_common_args(parser)
    parser.add_argument("--keep", action="store_true",
                        help="保留 run 目录（默认即保留；本脚本从不删除任何目录）")
    parser.add_argument("--timeout", type=float, default=1800.0, help="单 run 超时秒数")
    args = parser.parse_args()
    audit = AuditLog("run_sgc_synthetic_tests", dry_run=args.dry_run)
    audit.param(cases=list(CASES), timeout_s=args.timeout, keep=args.keep,
                sgc={"n": SGC_N, "r": SGC_R, "p": SGC_P, "chan": SGC_CHAN,
                     "bankfull_depth_m": round(bankfull_depth(), 3)},
                grid={"ncols": NCOLS, "nrows": NROWS, "dx_m": DX},
                criteria={"s1": {"pit_depth_min_m": S1_PIT_DEPTH_MIN_M, "drift_max": S1_DRIFT_MAX},
                          "s2": {"wet_thresh_m": S2_WET_THRESH_M, "dry_block_limit": S2_DRY_BLOCK_LIMIT},
                          "s5": {"qout_tol": S5_QOUT_TOL},
                          "s7": {"end_wet_max": S7_END_WET_MAX, "max_depth_m": S7_MAX_DEPTH_M},
                          "s8": {"band_rows": S8_BAND_ROWS, "band_wet_min": S8_BAND_WET_MIN}})
    audit.add_input("lisflood_exe", LISFLOOD, hash_file=False)
    cases_report: dict[str, dict] = {}
    for name in CASES:
        run_dir = SYNTH_ROOT / (f"sgc_{name}" + (f"_{args.run_tag}" if args.run_tag else ""))

        def attempt() -> dict:
            # 已有 res.mass 的 run 目录直接复用解析（报告记 reused=true，不重跑不覆盖）
            if not args.dry_run and (run_dir / "results" / "res.mass").is_file():
                audit.step("reuse_existing", case=name, run_dir=run_dir.name)
                return collect_case(name, run_dir, reused=True,
                                    run_rec={"exit": None, "timed_out": False, "reused": True})
            return run_one_case(name, run_dir, dry_run=args.dry_run,
                                timeout_s=args.timeout, audit=audit)

        if not args.dry_run and not LISFLOOD.is_file():
            cases_report[name] = {"case": name, "pass": False,
                                  "error": f"lisflood.exe 不存在：{LISFLOOD.name}"}
        else:
            try:
                rec = attempt()
            except OverwriteRefused as exc:
                rec = {"case": name, "pass": False, "error": str(exc), "refused_overwrite": True}
            except OSError as exc:
                if getattr(exc, "errno", None) != 22:
                    raise
                print(f"[{name}] 写盘 OSError 22，30 s 后重试一次", file=sys.stderr)
                time.sleep(30.0)
                try:
                    rec = attempt()
                except OverwriteRefused as exc2:
                    rec = {"case": name, "pass": False, "error": str(exc2), "refused_overwrite": True}
                except OSError as exc2:
                    rec = {"case": name, "pass": False, "write_blocked": True, "error": str(exc2)}
            cases_report[name] = rec
            if rec.get("error") or rec.get("fatal") or rec.get("run", {}).get("timed_out"):
                print(f"[{name}] {rec.get('error') or 'run.log 命中致命特征或超时'}", file=sys.stderr)
                if rec.get("log_tail"):
                    print(rec["log_tail"], file=sys.stderr)
            audit.step("case_done", case=name, pass_=rec.get("pass"), e_mass=rec.get("e_mass"))
        if "error" in cases_report[name]:
            audit.error(cases_report[name]["error"])
    runs = [r for r in cases_report.values() if not r.get("skipped")]
    sgc_supported = bool(runs) and all(r.get("sgc_ran_ok") for r in runs)
    overall = bool(sgc_supported and all(r.get("pass") for r in runs))

    def tag(v: object) -> str:
        return "SKIP" if v is None else ("PASS" if v else "FAIL")

    print("Synthetic tests: " + " ".join(f"{n}={tag(cases_report[n].get('pass'))}" for n in CASES)
          + f" overall={tag(overall if not args.dry_run else None)}")
    report = {"generated_utc": utc_now(), "dry_run": args.dry_run,
              "case_count": len(CASES), "cases": cases_report,
              "sgc_supported": None if args.dry_run else sgc_supported,
              "overall_pass": None if args.dry_run else overall,
              "note": ("8 算例：s1 蓄水/s2 坡面/s3 输水/s4 漫溢/s5 汇合/s6 退水/s7 干湿/s8 边界水位；"
                       "已有 results/res.mass 的 run 目录复用解析（reused=true），不重跑不覆盖；"
                       "s5 先双 QVAR 序列（两条 N 行各 5 m³/s）试跑，被拒则回退单条全缘 QVAR"
                       "（10 m³/s，inflow_mode=combined_fallback）；"
                       "写盘 OSError 22 重试一次仍失败记 write_blocked；"
                       "sgc_supported = 各 run 产出 res.mass 且 run.log 无致命错误；"
                       "overall_pass = 8 算例判据全过；E_mass 口径同 g15；"
                       "s8 HVAR 序列值为水面高程（DEM+水深）；"
                       "判据按计划 §4.5 原样评估，如实记录不为过门调参")}
    report_path = (args.out_dir or REPORT_PATH.parent) / REPORT_PATH.name
    try:
        write_json(report_path, report)
    except OSError as exc:
        if getattr(exc, "errno", None) != 22:
            raise
        time.sleep(30.0)
        write_json(report_path, report)
    audit.step("report", path=str(report_path))
    audit.write(args.log_json or AUDIT_PATH)
    return 0 if (args.dry_run or overall) else 1


if __name__ == "__main__":
    raise SystemExit(main())
