"""2D 等效地下排水源汇场（Drainage Sink Kernel）与 LISFLOOD-FP 耦合接口构建器。

物理口径：
1. 根据 PipeNetGen 合成管网中每个检查井/雨水口的设计汇水面积、下游管道满流输水能力（Q_full）
   以及投影后落在二维网格的像元位置，构建城市单元等效排水容量栅格（mm/h）；
2. 在 LISFLOOD-FP 8.1.0 求解器内，利用原生支持的点源/点汇接口 `P x y QVAR <name>`
   （`iterateq.cpp::UpdateH` 的 QVAR 分支 / `lisflood2/sgm_fast.cpp::SGC2_PointSources_Vol_row`），
   实现「城区雨水口吸纳负汇 + 河道排放口正源入流」的水量 100% 守恒闭合传输
   （不修改 models/ 下任何 C++ 源码）。

两条构建路径（本模块明确区分，不得混用）：

- `build_lisflood_drainage_bdy_and_bci`：**粗聚合**版本，把城区像元聚成 ≤`max_sinks` 个簇，
  每簇取一个代表像元。簇内多格雨水被压到单格抽取，会超出该格瞬时净雨并触发求解器
  `H<0` 钳零（`iterateq.cpp:541`），因此只能用于诊断量级评估，**不可用于物理重跑**。
- `build_fullgrid_coupled_forcing`：**全网格**版本，逐城区像元布设点汇、逐排口像元布设点源，
  单格抽取恒 ≤ `safety_factor × 该格瞬时净雨`，数学上保证不产生负水深。
  物理重跑（`outputs/pipenet/<case>/run_coupled_*`）必须使用本函数。

`QVAR` 数值约定：`.bdy` 内的值 = 实际流量 Q(m³/s) / dx(m)，
求解器侧按 `Qpnt = qtmp * dx` 还原（`iterateq.cpp:509`、`sgm_fast.cpp:2345`）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer

from hydro_refine.core.paths import PROJ
from scripts.common.grid_io import (
    AsciiGrid,
    read_ascii_grid,
    read_ascii_header,
    write_ascii_grid,
)
from scripts.common.patch_analysis import connected_components


def _safe_rel_path(p: Path) -> str:
    try:
        return p.resolve().relative_to(PROJ.resolve()).as_posix()
    except ValueError:
        return str(p)


def build_drainage_sink_grid(
    pipenet_geojson: Path,
    grid_template_asc: Path,
    *,
    epsg: int,
    out_sink_asc: Path | None = None,
    max_cell_drain_mmh: float = 80.0,
) -> dict[str, Any]:
    """将虚拟管网节点排水能力投影至 2D 模型栅格，生成等效排水容量场（mm/h）。"""
    tmpl = read_ascii_grid(grid_template_asc, nodata_to_nan=True)
    hdr = tmpl.header
    nrows, ncols = tmpl.shape
    cell = tmpl.cell_size_m
    cell_area_m2 = tmpl.cell_area_m2
    xll = float(hdr["xllcorner"])
    yll = float(hdr["yllcorner"])

    gj = json.loads(pipenet_geojson.read_text(encoding="utf-8"))
    tf = Transformer.from_crs(4326, epsg, always_xy=True)

    # 先聚合每条管段的满流能力与节点映射
    node_cap_m3s: dict[str, float] = {}
    outfalls_rc: dict[str, tuple[int, int, float, float]] = {}
    junctions_rc: list[tuple[str, int, int, float, float]] = []

    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        fkind = props.get("feature_kind")
        if fkind == "conduit":
            u = str(props["from_node"])
            q_full = float(props.get("q_full_m3s") or 0.05)
            node_cap_m3s[u] = max(node_cap_m3s.get(u, 0.0), q_full)

    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        fkind = props.get("feature_kind")
        if fkind not in ("junction", "outfall"):
            continue
        lon, lat = feat["geometry"]["coordinates"]
        x, y = tf.transform(float(lon), float(lat))
        col = int((x - xll) / cell)
        row = int((yll + nrows * cell - y) / cell)
        if not (0 <= row < nrows and 0 <= col < ncols):
            continue
        nid = str(props["node_id"])
        if fkind == "outfall":
            outfalls_rc[nid] = (row, col, x, y)
        else:
            # 单个雨水口局部进水按其设计能力或局部管段能力限幅
            q_local = min(float(props.get("q_design_m3s") or 0.02), node_cap_m3s.get(nid, 0.05))
            q_local = max(q_local, 0.015)
            junctions_rc.append((nid, row, col, x, y))

    cap_m3s_grid = np.zeros((nrows, ncols), dtype=np.float64)
    inlet_count_grid = np.zeros((nrows, ncols), dtype=np.int32)
    for nid, row, col, _x, _y in junctions_rc:
        q_local = min(float(node_cap_m3s.get(nid, 0.05)), 0.15)
        cap_m3s_grid[row, col] += q_local
        inlet_count_grid[row, col] += 1

    # 将 m3/s 折算为 mm/h: (Q / A_cell) * 3600 * 1000
    cap_mmh_grid = (cap_m3s_grid / cell_area_m2) * 3600.0 * 1000.0
    cap_mmh_grid = np.clip(cap_mmh_grid, 0.0, max_cell_drain_mmh).astype(np.float32)

    if out_sink_asc is not None:
        write_ascii_grid(out_sink_asc, AsciiGrid(cap_mmh_grid, dict(hdr)), fmt="%.3f")

    active_mask = cap_mmh_grid > 0.0
    return {
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "grid_shape": [nrows, ncols],
        "cellsize_m": cell,
        "active_urban_cells": int(active_mask.sum()),
        "urban_drained_area_km2": round(float(active_mask.sum()) * cell_area_m2 / 1e6, 3),
        "total_inlets_mapped": len(junctions_rc),
        "outfalls_mapped": len(outfalls_rc),
        "mean_active_capacity_mmh": (
            round(float(cap_mmh_grid[active_mask].mean()), 2) if active_mask.any() else 0.0
        ),
        "max_active_capacity_mmh": round(float(cap_mmh_grid.max()), 2),
        "cap_mmh_grid": cap_mmh_grid,
        "outfalls_rc": outfalls_rc,
        "header": dict(hdr),
    }


def build_lisflood_drainage_bdy_and_bci(
    sink_info: dict[str, Any],
    *,
    times_s: list[float],
    urban_rain_mmh: list[float],
    out_dir: Path,
    safety_factor: float = 0.85,
    max_sinks: int = 24,
) -> dict[str, Any]:
    """为 LISFLOOD-FP 8.1.0 构建点汇/点源水量守恒耦合边界（bci 片段 + .bdy 序列）。

    .. deprecated:: 物理重跑请改用 `build_fullgrid_coupled_forcing`
       本函数把城区像元聚成少量簇后从**代表像元单格**抽取簇内全部雨水，
       抽取量可超出该格瞬时净雨，导致求解器把 H 钳零并破坏 E_mass。
       保留它只为兼容既有诊断脚本与单元测试。

    核心守恒契约：
    - 每个聚合城区排水块的点汇吸纳速率 |Q_sink(t)| <= safety_factor * Q_rain_in(t)
      （确保不吸干单元格导致 negative H 水量误差）；
    - 所有城区点汇的总吸纳流量在每一时刻等量分配注入最近受纳河道排放口（Q_outfall >= 0），
      保证 ΣQ_sink(t) + ΣQ_outfall(t) == 0。
    """
    cap_grid: np.ndarray = sink_info["cap_mmh_grid"]
    hdr: dict[str, float] = sink_info["header"]
    outfalls_rc: dict[str, tuple[int, int, float, float]] = sink_info["outfalls_rc"]
    cell = float(hdr["cellsize"])
    xll = float(hdr["xllcorner"])
    yll = float(hdr["yllcorner"])
    nrows, ncols = cap_grid.shape

    active_rc = np.argwhere(cap_grid > 0.0)
    if len(active_rc) == 0 or not outfalls_rc:
        raise ValueError("有效城区排水像元或河道排放口为空，无法构建点汇耦合边界")

    # 按粗空间网格分块聚类，控制 LISFLOOD-FP 点源数量在高效区间（<= max_sinks）
    block = max(1, int(np.ceil(np.sqrt(len(active_rc) / max_sinks))))
    clusters: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r, c in active_rc:
        key = (int(r) // block, int(c) // block)
        clusters.setdefault(key, []).append((int(r), int(c)))

    # 若聚簇仍超过 max_sinks，按排水总能力取前 max_sinks 个核心区块
    sorted_clusters = sorted(
        clusters.items(),
        key=lambda kv: sum(float(cap_grid[r, c]) for r, c in kv[1]),
        reverse=True,
    )[:max_sinks]

    t_arr = np.asarray(times_s, dtype=np.float64)
    rain_arr = np.asarray(urban_rain_mmh, dtype=np.float64)
    total_sink_q_series = np.zeros_like(t_arr)

    bci_lines: list[str] = []
    bdy_lines: list[str] = ["# PipeNetGen urban drainage sink & river outfall QVAR (mass balanced)"]

    for idx, (_blk, cells) in enumerate(sorted_clusters):
        # 取簇中心最接近质心的有排水能力像元作为点汇落点
        mean_r = sum(r for r, _c in cells) / len(cells)
        mean_c = sum(c for _r, c in cells) / len(cells)
        rep_r, rep_c = min(cells, key=lambda rc: (rc[0] - mean_r) ** 2 + (rc[1] - mean_c) ** 2)
        px = xll + (rep_c + 0.5) * cell
        py = yll + (nrows - rep_r - 0.5) * cell

        cluster_cap_mmh = float(np.mean([cap_grid[r, c] for r, c in cells]))
        # 单点像元点汇安全上限：仅从代表像元自身单格雨水/邻域汇入量中抽取，防止抽干 H<0 触发质量门偏差
        cell_area_m2 = cell ** 2
        q_sink_series = np.zeros_like(t_arr)
        for ti, rain_i in enumerate(rain_arr):
            eff_mmh = min(cluster_cap_mmh, max(0.0, rain_i) * safety_factor)
            q_m3s = (eff_mmh / 1000.0 / 3600.0) * cell_area_m2
            q_sink_series[ti] = -q_m3s
        total_sink_q_series += (-q_sink_series)

        sname = f"dr_sink_{idx:02d}"
        bci_lines.append(f"P\t{px:.1f}\t{py:.1f}\tQVAR\t{sname}")
        bdy_lines.append(sname)
        bdy_lines.append(f"{len(t_arr)} seconds")
        for t_val, q_val in zip(t_arr, q_sink_series):
            bdy_lines.append(f"{q_val / cell:.6f}\t{t_val:.0f}")

    # 将全部截留雨水等量分配注入代表性河道排放口（取空间分布前 4 个主排口）
    out_list = list(outfalls_rc.items())[:4]
    share = 1.0 / len(out_list)
    for k, (_oid, (orow, ocol, ox, oy)) in enumerate(out_list):
        px = xll + (ocol + 0.5) * cell
        py = yll + (nrows - orow - 0.5) * cell
        oname = f"dr_out_{k:02d}"
        bci_lines.append(f"P\t{px:.1f}\t{py:.1f}\tQVAR\t{oname}")
        bdy_lines.append(oname)
        bdy_lines.append(f"{len(t_arr)} seconds")
        for t_val, q_tot in zip(t_arr, total_sink_q_series):
            bdy_lines.append(f"{(q_tot * share) / cell:.6f}\t{t_val:.0f}")

    out_dir.mkdir(parents=True, exist_ok=True)
    bci_snip = out_dir / "drainage_points.bci"
    bdy_file = out_dir / "drainage_points.bdy"
    bci_snip.write_text("\n".join(bci_lines) + "\n", encoding="ascii", newline="\n")
    bdy_file.write_text("\n".join(bdy_lines) + "\n", encoding="ascii", newline="\n")

    return {
        "n_sink_clusters": len(sorted_clusters),
        "n_outfall_sources": len(out_list),
        "total_drained_volume_m3": round(
            float(np.trapezoid(total_sink_q_series, t_arr) if hasattr(np, "trapezoid") else np.trapz(total_sink_q_series, t_arr)),
            1,
        ),
        "bci_snippet": _safe_rel_path(bci_snip),
        "bdy_file": _safe_rel_path(bdy_file),
    }


# ---------------------------------------------------------------------------
# 全网格 1D-2D 管网耦合（物理重跑专用）
# ---------------------------------------------------------------------------

def _map_nodes_to_cells(
    pipenet_geojson: Path,
    header: dict[str, float],
    *,
    epsg: int,
) -> dict[str, Any]:
    """把管网 junction / outfall 节点投影到二维网格像元，并解析拓扑归属。

    返回 dict：
      - `junctions`: {node_id: (row, col)}
      - `outfalls`:  {node_id: (row, col)}
      - `node_cap_m3s`: {node_id: 下游管段满流能力}
      - `owner_outfall`: {junction_id: outfall_id | None}（沿 conduit 有向边追溯）
    """
    hdr = header
    nrows = int(hdr["nrows"])
    ncols = int(hdr["ncols"])
    cell = float(hdr["cellsize"])
    xll = float(hdr["xllcorner"])
    yll = float(hdr["yllcorner"])

    gj = json.loads(Path(pipenet_geojson).read_text(encoding="utf-8"))
    tf = Transformer.from_crs(4326, epsg, always_xy=True)

    successors: dict[str, list[str]] = {}
    node_cap: dict[str, float] = {}
    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        if props.get("feature_kind") != "conduit":
            continue
        src = str(props["from_node"])
        successors.setdefault(src, []).append(str(props["to_node"]))
        node_cap[src] = max(node_cap.get(src, 0.0), float(props.get("q_full_m3s") or 0.05))

    junctions: dict[str, tuple[int, int]] = {}
    outfalls: dict[str, tuple[int, int]] = {}
    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        kind = props.get("feature_kind")
        if kind not in ("junction", "outfall"):
            continue
        lon, lat = feat["geometry"]["coordinates"]
        x, y = tf.transform(float(lon), float(lat))
        col = int((x - xll) / cell)
        row = int((yll + nrows * cell - y) / cell)
        if not (0 <= row < nrows and 0 <= col < ncols):
            continue
        nid = str(props["node_id"])
        if kind == "outfall":
            outfalls[nid] = (row, col)
        else:
            junctions[nid] = (row, col)
            node_cap[nid] = max(node_cap.get(nid, 0.0), float(props.get("q_full_m3s") or 0.0))
            node_cap[f"__qdesign__{nid}"] = float(props.get("q_design_m3s") or 0.02)

    def _trace(nid: str, guard: int = 1000) -> str | None:
        """沿第一条下游 conduit 追溯至 outfall；遇环或断头返回 None。"""
        cur = nid
        seen: set[str] = set()
        for _ in range(guard):
            if cur in outfalls:
                return cur
            nxt = successors.get(cur) or []
            if not nxt:
                return None
            cur = nxt[0]
            if cur in seen:
                return None
            seen.add(cur)
        return None

    owner = {nid: _trace(nid) for nid in junctions}
    return {
        "junctions": junctions,
        "outfalls": outfalls,
        "node_cap_m3s": node_cap,
        "owner_outfall": owner,
    }


def _remap_after_exclusion(
    cap_m3s: dict[tuple[int, int], float],
    ex_mask: np.ndarray,
    inlets_per_cell: dict[tuple[int, int], int],
    *,
    max_radius: int = 10,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """把落在排除掩膜内的点汇搬迁到最近的非掩膜邻格，**不丢水量**。

    原地删除会丢掉该格排涝能力（久隆 7 格 SGC 河道像元约 0.16 km²）。
    物理上这些雨水口确实存在，只是不能让点汇抽走河道水量；把能力迁到
    紧邻的陆域像元，既保住总排涝能力，又避免抽干河槽。

    返回搬迁记录 [(源格, 目标格), ...]，供审计。
    """
    nrows, ncols = ex_mask.shape
    moves: list[tuple[tuple[int, int], tuple[int, int]]] = []
    offsets = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    for rc in [k for k in cap_m3s if ex_mask[k]]:
        q = cap_m3s[rc]
        cnt = inlets_per_cell.get(rc, 1)
        target: tuple[int, int] | None = None
        seen = {rc}
        frontier = [rc]
        for _dist in range(1, max_radius + 1):
            nxt: list[tuple[int, int]] = []
            for r, c in frontier:
                for dr, dc in offsets:
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < nrows and 0 <= nc < ncols) or (nr, nc) in seen:
                        continue
                    seen.add((nr, nc))
                    nxt.append((nr, nc))
            pick = sorted(p for p in nxt if not ex_mask[p])
            if pick:
                target = pick[0]
                break
            frontier = nxt
        del cap_m3s[rc]
        inlets_per_cell.pop(rc, None)
        if target is None:
            continue
        cap_m3s[target] = cap_m3s.get(target, 0.0) + q
        inlets_per_cell[target] = inlets_per_cell.get(target, 0) + cnt
        moves.append((rc, target))
    return moves


def _pick_injection_cell(
    start: tuple[int, int],
    *,
    blocked: np.ndarray,
    weight: np.ndarray,
    max_radius: int = 25,
) -> tuple[tuple[int, int] | None, int | None]:
    """从 `start` 向外 BFS，找最近且 `weight` 最小的可注入像元（回到源格则距离 0）。

    `blocked` 为不可注入像元（城区点汇格、nodata）。返回 (格号, BFS 距离)。
    """
    nrows, ncols = weight.shape
    sr, sc = start
    if not blocked[sr, sc] and weight[sr, sc] >= 0:
        return (sr, sc), 0
    seen = {start}
    frontier = [start]
    offsets = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    for dist in range(1, max_radius + 1):
        nxt: list[tuple[int, int]] = []
        for r, c in frontier:
            for dr, dc in offsets:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < nrows and 0 <= nc < ncols) or (nr, nc) in seen:
                    continue
                seen.add((nr, nc))
                nxt.append((nr, nc))
        cand = [(float(weight[p]), p) for p in nxt if not blocked[p] and weight[p] >= 0]
        if cand:
            cand.sort(key=lambda item: (item[0], item[1]))
            return cand[0][1], dist
        frontier = nxt
    return None, None


def build_fullgrid_coupled_forcing(
    pipenet_geojson: Path,
    base_run_dir: Path,
    out_dir: Path,
    *,
    epsg: int,
    time_window_s: float,
    safety_factor: float = 0.85,
    n_capacity_bins: int = 10,
    unique_series_per_cell: bool = False,
    exclude_mask_asc: Path | None = None,
    capacity_cap_mmh: float = 80.0,
    max_series_per_bin: int = 0,
) -> dict[str, Any]:
    """为物理重跑构建**全网格**点汇/点源耦合边界（水量严格闭合）。

    设计要点（均经 `models/lisflood-fp-8.1` 源码核实）：

    1. **逐格点汇**：每个含雨水口的城区像元一个 `QVAR` 点汇。单格抽水量恒 ≤
       `safety_factor × 该格瞬时净雨率 × 单格面积`，因此该步内单元体积不减为负，
       不会触发求解器 `H<0` 钳零（`iterateq.cpp:541`、`sgm_fast.cpp` 卷体积钳位）。
    2. **SGC / 排口冲突清洗**：`exclude_mask_asc`（久隆 `sgc_width.asc > 0`）内的像元
       不布设点汇（否则会抽走河道水量）；与排口落格重合的像元也不布设点汇。
    3. **拓扑分账排口**：沿 conduit 有向边把每个雨水口归属到唯一 `OUT_*` 排口，
       按各排口的汇水能力占比 `w_k`（Σw_k=1）回注，而非硬编码前 4 个排口均分。
    4. **分档复用曲线**：非 SGC 走 `iterateq.cpp` 串行循环，可按能力分档共享
       `.bdy` 曲线（`input.cpp:2050` 支持多个点源指向同名曲线）；SGC 走
       `lisflood2/sgm_fast.cpp:3696` 的 `#pragma omp single` 串行插值区，
       共享曲线亦安全，但 `unique_series_per_cell=True` 可进一步规避同格多源。

    返回 dict 含 `bci_path`、`bdy_path`、`n_sinks`、`n_outfalls`、闭合校验等。
    """
    hdr = read_ascii_header(Path(base_run_dir) / "dem.asc")
    nrows = int(hdr["nrows"])
    ncols = int(hdr["ncols"])
    cell = float(hdr["cellsize"])
    cell_area = cell * cell
    xll = float(hdr["xllcorner"])
    yll = float(hdr["yllcorner"])

    mapped = _map_nodes_to_cells(pipenet_geojson, hdr, epsg=epsg)
    junctions = mapped["junctions"]
    outfalls = mapped["outfalls"]
    node_cap = mapped["node_cap_m3s"]
    owner = mapped["owner_outfall"]
    if not junctions:
        raise ValueError(f"{pipenet_geojson} 无落入网格的雨水口节点")
    if not outfalls:
        raise ValueError(f"{pipenet_geojson} 无落入网格的排放口节点")

    # --- 逐格聚合单格排水能力（m³/s），与 build_drainage_sink_grid 同口径 ---
    cap_m3s: dict[tuple[int, int], float] = {}
    inlets_per_cell: dict[tuple[int, int], int] = {}
    for nid, rc in junctions.items():
        q_local = min(float(node_cap.get(f"__qdesign__{nid}", 0.02)), node_cap.get(nid, 0.05))
        q_local = max(q_local, 0.015)
        q_local = min(q_local, 0.15)
        cap_m3s[rc] = cap_m3s.get(rc, 0.0) + q_local
        inlets_per_cell[rc] = inlets_per_cell.get(rc, 0) + 1

    outfall_cells = set(outfalls.values())
    excluded: dict[str, int] = {"sgc_channel": 0}
    outfall_overlap_cells = 0

    # 注意：排口落格**仍可作点汇**——求解器允许 sink 与 source 是不同 `P` 行、
    # 落在不同像元即可。这里只记录重叠数，注入落点会在下面 BFS 移位到非点汇格。
    # 若粗暴删除这些格，会丢掉对应城区的排涝能力（久隆 41 格 / 贵港 41 格）。
    for rc in cap_m3s:
        if rc in outfall_cells:
            outfall_overlap_cells += 1

    # --- 冲突清洗：SGC 河道像元不得作点汇（久隆 7 格）---
    if exclude_mask_asc is not None:
        ex_grid = read_ascii_grid(exclude_mask_asc)
        if ex_grid.shape != (nrows, ncols):
            raise ValueError(
                f"{exclude_mask_asc} 形状 {ex_grid.shape} 与 DEM {(nrows, ncols)} 不符"
            )
        ex_mask = np.asarray(ex_grid.data, dtype=np.float64) > 0.0
        moves = _remap_after_exclusion(cap_m3s, ex_mask, inlets_per_cell)
        excluded["sgc_channel"] = len(moves)
        excluded["sgc_remap_moves"] = [[list(a), list(b)] for a, b in moves]
    excluded["outfall_overlap"] = outfall_overlap_cells

    if not cap_m3s:
        raise ValueError("清洗后无可用城区点汇像元")

    # --- 排口能力分账：按归属雨水口能力之和定权 ---
    # 注意：不按 `rc in cap_m3s` 过滤。SGC 清洗是**迁移**像元（保能力）而非删除，
    # 若按格号过滤，久隆全部 7 个受影响雨水口会被误判为"无归属"，回注直接为 0。
    outfall_weight: dict[str, float] = {}
    for nid, _rc in junctions.items():
        of = owner.get(nid)
        if of is None or of not in outfalls:
            continue
        q_local = min(float(node_cap.get(f"__qdesign__{nid}", 0.02)), node_cap.get(nid, 0.05))
        q_local = max(q_local, 0.015)
        q_local = min(q_local, 0.15)
        outfall_weight[of] = outfall_weight.get(of, 0.0) + q_local
    if not outfall_weight:
        raise ValueError("无任何排放口获得拓扑归属，无法回注")
    wsum = sum(outfall_weight.values())
    outfall_share = {oid: w / wsum for oid, w in outfall_weight.items()}

    # --- 排口注入落点：避开点汇格与 SGC 河道像元，优先最低 DEM ---
    dem_grid = read_ascii_grid(Path(base_run_dir) / "dem.asc", nodata_to_nan=True)
    dem = np.asarray(dem_grid.data, dtype=np.float64)
    nodata = ~np.isfinite(dem)
    blocked = np.zeros((nrows, ncols), dtype=bool)
    for rc in cap_m3s:
        blocked[rc] = True
    blocked |= nodata
    # 关键：SGC 模式下把正源注入**河道像元**会破坏质量守恒（实测 E_mass 3.6e-3 vs
    # 注入洪泛平原 0.0）。求解器在 SGC 河道单元用 `SGC2_PointSources_H_row` 反算 H，
    # 正源与河道体积更新之间存在记账偏差。因此注入点必须落在非 SGC 像元。
    sgc_blocked = 0
    if exclude_mask_asc is not None:
        sgc_mask = np.asarray(read_ascii_grid(exclude_mask_asc).data, dtype=np.float64) > 0.0
        if sgc_mask.shape == blocked.shape:
            sgc_blocked = int((~blocked & sgc_mask).sum())
            blocked |= sgc_mask
    weight_grid = np.where(nodata, -1.0, dem)

    injection: dict[str, tuple[int, int]] = {}
    injection_dist: dict[str, int] = {}
    for oid, rc in outfalls.items():
        if oid not in outfall_share:
            continue
        spot, dist = _pick_injection_cell(rc, blocked=blocked, weight=weight_grid)
        if spot is None:
            raise ValueError(f"排放口 {oid} 在 {rc} 周围找不到可注入像元")
        injection[oid] = spot
        injection_dist[oid] = int(dist)

    # --- 逐格点汇容量折算为 mm/h，并按档位分组 ---
    # 同时保留**未截断**能力，用于判定 80 mm/h 上限是否真的截断了输入分布。
    caps_mmh_raw: dict[tuple[int, int], float] = {}
    caps_mmh: dict[tuple[int, int], float] = {}
    for rc, q in cap_m3s.items():
        mmh = (q / cell_area) * 3600.0 * 1000.0
        caps_mmh_raw[rc] = float(mmh)
        caps_mmh[rc] = float(min(mmh, capacity_cap_mmh))

    caps_sorted = sorted(caps_mmh.values())
    n_bins = max(1, int(min(n_capacity_bins, len(caps_sorted))))
    # 按**容量值域等分**切档，不用分位数：本项目的逐格能力在 80 mm/h 上限处有大原子
    # （贵港 2808/4032、宁明 792/1206 达上限），分位数会把它们压成 2–3 档，
    # 同档内容量相差数十 mm/h。值域等分保证档内跨度 ≤ (max-min)/n_bins。
    if n_bins > 1 and caps_sorted[-1] > caps_sorted[0]:
        edges = np.linspace(caps_sorted[0], caps_sorted[-1], n_bins + 1)
        edges = np.unique(edges)
    else:
        edges = np.array([caps_sorted[0], caps_sorted[-1]], dtype=np.float64)
        edges = np.unique(edges)
        if edges.size == 1:
            edges = np.array([edges[0], edges[0] + 1e-6], dtype=np.float64)
    bin_of: dict[tuple[int, int], int] = {}
    for rc, mmh in caps_mmh.items():
        idx = int(np.searchsorted(edges, mmh, side="right") - 1)
        bin_of[rc] = int(np.clip(idx, 0, len(edges) - 2))

    bin_cells: dict[int, list[tuple[int, int]]] = {}
    for rc, b in bin_of.items():
        bin_cells.setdefault(b, []).append(rc)

    # --- 读基准 run 的降雨序列（mm/h @ 时间戳），供容量折算与诊断使用 ---
    rain_path = Path(base_run_dir) / "rain.rain"
    rain_times, rain_rates = _read_timeseries_points(rain_path)

    # --- 上限是否真的生效（决定结论适用范围，必须随结果一起报告）---
    # 抽取量 = min(单格能力, safety_factor × 瞬时雨率)。三个层次要分清：
    #  1) `cap_truncates_input`：有多少格的原生能力 > 上限而被截断（分布事实）；
    #  2) `capacity_cap_effective`：上限是否可能改变某一刻的抽取量，
    #     即 safety_factor × 雨率峰值 > 上限；
    #  3) `cap_binding_cells`：实际抽取量被**能力**（而非雨率）限制的格数。
    # 只有 (3) > 0 时，逐格能力才真正影响结果；若 (2) 为 False，改上限不改变任何结果。
    rain_peak_mmh = float(rain_rates.max()) if len(rain_rates) else 0.0
    drain_ceiling_mmh = safety_factor * rain_peak_mmh
    cap_truncated_cells = int(
        sum(1 for m in caps_mmh_raw.values() if m > capacity_cap_mmh + 1e-9)
    )
    cap_binding_cells = int(
        sum(1 for m in caps_mmh.values() if m < drain_ceiling_mmh)
    )
    cap_effective = bool(drain_ceiling_mmh > capacity_cap_mmh)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bdy_lines: list[str] = [
        "# PipeNetGen full-grid urban drainage coupling "
        "(mass-balanced sinks -> outfall sources; SYNTHETIC_DATA_NOT_REAL_ASSET)",
    ]
    bci_lines: list[str] = []
    sink_names: dict[Any, str] = {}

    def _emit_series(name: str, q_of_t: np.ndarray, t_arr: np.ndarray) -> None:
        bdy_lines.append(name)
        bdy_lines.append(f"{len(t_arr)} seconds")
        for t_val, q_val in zip(t_arr, q_of_t):
            # 精度 1e-10 保闭合：Q/dx 量级 1e-2，%.10f 的截断误差远小于 1e-9 相对门
            bdy_lines.append(f"{q_val / cell:.10f}\t{t_val:.0f}")

    # 档位共享曲线（非 SGC 或 unique_series_per_cell=False）
    # 重要：同档内所有像元指向**同一条**曲线，故曲线值必须是「单格」抽水流量；
    # 档内总抽取量 = 单格值 × 档内像元数，在汇总时才乘格数（否则会重复抽取 N 倍）。
    bin_curve_q: dict[int, np.ndarray] = {}
    for b, cells in sorted(bin_cells.items()):
        rep_mmh = float(np.mean([caps_mmh[rc] for rc in cells]))
        q_series = np.zeros_like(rain_rates)
        for ti, rate in enumerate(rain_rates):
            eff = min(rep_mmh, max(0.0, rate) * safety_factor)
            q_series[ti] = -(eff / 1000.0 / 3600.0) * cell_area
        bin_curve_q[b] = q_series
        _emit_series(f"dr_bin_{b:02d}", q_series, rain_times)

    # 逐格独立曲线
    cell_curve_q: dict[tuple[int, int], np.ndarray] = {}
    if unique_series_per_cell:
        for rc, mmh in sorted(caps_mmh.items()):
            q_series = np.zeros_like(rain_rates)
            for ti, rate in enumerate(rain_rates):
                eff = min(mmh, max(0.0, rate) * safety_factor)
                q_series[ti] = -(eff / 1000.0 / 3600.0) * cell_area
            cell_curve_q[rc] = q_series
            _emit_series(f"dr_jl_{rc[0]}_{rc[1]}", q_series, rain_times)

    for rc in sorted(caps_mmh):
        px = xll + (rc[1] + 0.5) * cell
        py = yll + (nrows - rc[0] - 0.5) * cell
        if unique_series_per_cell:
            name = f"dr_jl_{rc[0]}_{rc[1]}"
        else:
            name = f"dr_bin_{bin_of[rc]:02d}"
        sink_names[rc] = name
        bci_lines.append(f"P\t{px:.1f}\t{py:.1f}\tQVAR\t{name}")

    # --- 汇总每时刻总吸纳量，按 outfall_share 注入 ---
    total_sink_by_t = np.zeros_like(rain_rates)
    if unique_series_per_cell:
        for q_series in cell_curve_q.values():
            total_sink_by_t += -q_series
    else:
        for b, cells in bin_cells.items():
            total_sink_by_t += -(bin_curve_q[b]) * len(cells)

    # --- 排口注入：按**唯一注入像元**归并，避免同格多个 P 行 ---
    # 多个排口经 BFS 可能落到同一像元。求解器允许同格多源（体积累加，
    # `sgm_fast.cpp::SGC2_PointSources_Vol_row` 用 `volume_row[ps_x] += dV`），
    # 但同格正源+负汇会互相抵消，且同格多源对 `Qerror` 记账不友好。
    # 这里按像元合并成一条曲线，注入总量不变、闭合关系保持。
    inj_by_cell: dict[tuple[int, int], float] = {}
    for oid, spot in injection.items():
        inj_by_cell[spot] = inj_by_cell.get(spot, 0.0) + outfall_share[oid]

    # 若某注入像元同时是点汇像元，正负同格会抵消，必须排除
    inj_by_cell = {rc: s for rc, s in inj_by_cell.items() if rc not in caps_mmh}
    share_sum = sum(inj_by_cell.values())
    if share_sum <= 0:
        raise ValueError("全部注入像元与点汇像元冲突，无法回注")
    inj_by_cell = {rc: s / share_sum for rc, s in inj_by_cell.items()}

    for rc in sorted(inj_by_cell):
        px = xll + (rc[1] + 0.5) * cell
        py = yll + (nrows - rc[0] - 0.5) * cell
        oname = f"dr_out_{rc[0]}_{rc[1]}"
        bci_lines.append(f"P\t{px:.1f}\t{py:.1f}\tQVAR\t{oname}")
        out_series = total_sink_by_t * inj_by_cell[rc]
        _emit_series(oname, out_series, rain_times)

    bci_path = out_dir / "drainage_points.bci"
    bdy_path = out_dir / "drainage_points.bdy"
    bci_path.write_text("\n".join(bci_lines) + "\n", encoding="ascii", newline="\n")
    bdy_path.write_text("\n".join(bdy_lines) + "\n", encoding="ascii", newline="\n")

    # --- 水量闭合校验：ΣQ_sink + ΣQ_outfall == 0（逐时刻）---
    drain_vol = float(
        np.trapezoid(total_sink_by_t, rain_times)
        if hasattr(np, "trapezoid")
        else np.trapz(total_sink_by_t, rain_times)
    )
    out_vol = sum(
        float(
            np.trapezoid(total_sink_by_t * inj_by_cell[rc], rain_times)
            if hasattr(np, "trapezoid")
            else np.trapz(total_sink_by_t * inj_by_cell[rc], rain_times)
        )
        for rc in inj_by_cell
    )
    closure_err = abs(drain_vol - out_vol) / max(abs(drain_vol), 1.0)

    return {
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "physical_recalculation": True,
        "grid_shape": [nrows, ncols],
        "cellsize_m": cell,
        "n_sinks": len(caps_mmh),
        "n_outfall_sources": len(inj_by_cell),
        "n_outfall_nodes_routed": len(outfall_share),
        "n_capacity_bins": len(bin_cells),
        "unique_series_per_cell": bool(unique_series_per_cell),
        "safety_factor": safety_factor,
        "capacity_cap_mmh": capacity_cap_mmh,
        # 结论适用范围：80 mm/h 上限是否真的约束了抽取量。
        # 抽取量 = min(单格能力, safety_factor×雨率)，故仅当
        # safety_factor×雨率峰值 > 80 时上限才生效。三案例实测均为 false，
        # 即结果对上限取值不敏感；真正的约束是 safety_factor（数值安全系数，非水力标定）。
        "capacity_cap_effective": cap_effective,
        "cap_truncated_cells": cap_truncated_cells,
        "cap_truncated_frac": round(cap_truncated_cells / max(len(caps_mmh), 1), 4),
        "rain_peak_mmh": round(rain_peak_mmh, 3),
        "drain_ceiling_mmh": round(drain_ceiling_mmh, 3),
        "cap_binding_cells": cap_binding_cells,
        "cap_binding_frac": round(cap_binding_cells / max(len(caps_mmh), 1), 4),
        "capacity_mmh_stats_raw": {
            "min": round(float(min(caps_mmh_raw.values())), 2),
            "mean": round(float(np.mean(list(caps_mmh_raw.values()))), 2),
            "max": round(float(max(caps_mmh_raw.values())), 2),
        },
        "constraint_note": (
            "抽取量 = min(单格管网能力, safety_factor×瞬时净雨)。"
            "cap_binding_cells 才是真正被能力限制的格数；"
            "capacity_cap_effective=False 表示改上限不改变任何结果。"
            "safety_factor 为保证零负水深的数值安全系数，不是管径水力标定值，"
            "削减百分比应读作该假设下的排涝上限效应。"
        ),
        "excluded_cells": excluded,
        "urban_area_km2": round(len(caps_mmh) * cell_area / 1e6, 4),
        "capacity_mmh_stats": {
            "min": round(float(min(caps_mmh.values())), 2),
            "mean": round(float(np.mean(list(caps_mmh.values()))), 2),
            "max": round(float(max(caps_mmh.values())), 2),
        },
        "outfall_shares": {o: round(w, 6) for o, w in sorted(outfall_share.items())},
        "outfall_injection_bfs_dist": injection_dist,
        "outfall_injection_sgc_blocked_cells": sgc_blocked,
        "total_drained_volume_m3": round(drain_vol, 1),
        "total_reinjected_volume_m3": round(out_vol, 1),
        "mass_closure_rel_error": float(f"{closure_err:.3e}"),
        "mass_closure_ok": bool(closure_err <= 1e-9),
        "bci_path": _safe_rel_path(bci_path),
        "bdy_path": _safe_rel_path(bdy_path),
        "bci_lines": len(bci_lines),
        "bdy_lines": len(bdy_lines),
        "series_names": sorted(
            {str(n) for n in sink_names.values()}
            | {f"dr_out_{o}" for o in outfall_share}
        ),
    }


def _read_timeseries_points(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """.rain 文件 → (时间数组, 速率数组)。格式：`N units` 头 + 逐行 `rate<tab>t`。

    与 `Rainfall_uniform.h::update_rainfall` 的读取顺序一致（第一列为值、第二列为时间）。
    """
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    n_declared: int | None = None
    unit = "seconds"
    rows: list[tuple[float, float]] = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if n_declared is None:
            parts = s.split()
            n_declared = int(parts[0])
            unit = parts[1].lower() if len(parts) > 1 else "seconds"
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        rows.append((float(parts[0]), float(parts[1])))
    if n_declared is None:
        raise ValueError(f"{path} 缺 `N units` 头")
    rows = rows[:n_declared]
    scale = {"seconds": 1.0, "second": 1.0, "minutes": 60.0, "minute": 60.0,
             "hours": 3600.0, "hour": 3600.0, "days": 86400.0, "day": 86400.0}.get(unit, 1.0)
    rates = np.array([r for r, _t in rows], dtype=np.float64)
    times = np.array([t * scale for _r, t in rows], dtype=np.float64)
    return times, rates


def evaluate_urban_patch_drainage(
    res_max_asc: Path,
    sink_info: dict[str, Any],
    *,
    swmm_drained_depth_m: float,
    wet_threshold_m: float = 0.05,
) -> dict[str, Any]:
    """基于物理管网截留等效水深，在既有城区二维积水场评估斑块消除与水深压降效果。

    注明：本函数用于定量预估虚拟管网排水对既有多斑块积水的削减幅度（diagnostic_projection=true），
    不篡改源 run 任何文件。
    """
    rmax = read_ascii_grid(res_max_asc, nodata_to_nan=True)
    wd = np.where(np.isfinite(rmax.data), rmax.data, 0.0).astype(np.float64)
    cap_grid: np.ndarray = sink_info["cap_mmh_grid"]
    if wd.shape != cap_grid.shape:
        raise ValueError(f"水深栅格形状 {wd.shape} 与排水容量场 {cap_grid.shape} 不符")

    urban_mask = cap_grid > 0.0
    if not urban_mask.any():
        return {"available": False, "reason": "no_urban_mask"}

    cell_area = rmax.cell_area_m2
    wet_before = urban_mask & (wd > wet_threshold_m)
    labels_b, n_b = connected_components(wet_before, connectivity=8)

    # 按局部排水能力相对均值进行空间加权折算，并严格非负限幅
    mean_cap = float(cap_grid[urban_mask].mean())
    rel_cap = np.where(urban_mask, cap_grid / max(mean_cap, 1e-6), 0.0)
    local_drain_m = swmm_drained_depth_m * np.clip(rel_cap, 0.25, 2.0)
    wd_after = np.where(urban_mask, np.maximum(wd - local_drain_m, 0.0), wd)
    wet_after = urban_mask & (wd_after > wet_threshold_m)
    labels_a, n_a = connected_components(wet_after, connectivity=8)

    vol_before = float(wd[wet_before].sum()) * cell_area
    vol_after = float(wd_after[wet_after].sum()) * cell_area
    mean_d_before = float(wd[wet_before].mean()) if wet_before.any() else 0.0
    mean_d_after = float(wd_after[wet_after].mean()) if wet_after.any() else 0.0

    return {
        "diagnostic_drainage_projection": True,
        "not_physical_recalculation": True,
        "urban_cells": int(urban_mask.sum()),
        "urban_area_km2": round(float(urban_mask.sum()) * cell_area / 1e6, 3),
        "swmm_equivalent_drained_depth_m": round(float(swmm_drained_depth_m), 4),
        "before": {
            "wet_cells": int(wet_before.sum()),
            "wet_area_km2": round(float(wet_before.sum()) * cell_area / 1e6, 3),
            "n_patches": int(n_b),
            "mean_depth_m": round(mean_d_before, 4),
            "volume_m3": round(vol_before, 1),
        },
        "after": {
            "wet_cells": int(wet_after.sum()),
            "wet_area_km2": round(float(wet_after.sum()) * cell_area / 1e6, 3),
            "n_patches": int(n_a),
            "mean_depth_m": round(mean_d_after, 4),
            "volume_m3": round(vol_after, 1),
        },
        "patch_reduction_pct": (
            round((n_b - n_a) / n_b * 100.0, 2) if n_b > 0 else 0.0
        ),
        "wet_area_reduction_pct": (
            round((int(wet_before.sum()) - int(wet_after.sum())) / int(wet_before.sum()) * 100.0, 2)
            if wet_before.any()
            else 0.0
        ),
        "volume_reduction_pct": (
            round((vol_before - vol_after) / vol_before * 100.0, 2) if vol_before > 0 else 0.0
        ),
    }
