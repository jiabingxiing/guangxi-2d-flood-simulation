"""地形水文派生量底座（久隆钦江流域建库，ADR-0019/0020 支撑）。

约束（对齐 ADR-0010 四层 DEM 契约与计划 §4.4）：
- Priority-Flood 填洼结果**只允许**用于水文拓扑派生（DEM_HYDRO 用途），
  严禁直接送入水动力求解器（DEM_HYDRAULIC 用途）。
- 汇流累积 / D8 接收像元是**派生诊断量**，不是观测；下游使用处必须标注派生口径。

实现为纯 numpy + heapq（环境无 scipy/pysheds），与
``scripts/stage6/diagnose_jiulong_artifacts.py`` 中同名算法保持一致口径
（该脚本自持副本，属既有证据冻结，不做回改）。
"""

from __future__ import annotations

import heapq

import numpy as np

NB8 = (
    (-1, 0, 1.0),
    (1, 0, 1.0),
    (0, -1, 1.0),
    (0, 1, 1.0),
    (-1, -1, 2.0**0.5),
    (-1, 1, 2.0**0.5),
    (1, -1, 2.0**0.5),
    (1, 1, 2.0**0.5),
)


def priority_flood(dem: np.ndarray, eps: float = 0.0) -> np.ndarray:
    """Priority-Flood 填洼。边界像元为种子，按高程升序扩散。

    eps>0 时为 Priority-Flood+ε（Barnes 式）：填洼同时加微小梯度，使每个平点
    单调指向其溢出口，平地 D8 路由不再任意。ε 累积上限 = eps × 最长平点链，
    本域（1e-5 × ~1e4）≈ 0.1 m，远小于源 DEM 垂直精度。
    """
    height, width = dem.shape
    filled = dem.astype(np.float64).copy()
    closed = np.zeros((height, width), dtype=bool)
    heap: list[tuple[float, int, int]] = []

    def push(r: int, c: int) -> None:
        closed[r, c] = True
        heapq.heappush(heap, (float(filled[r, c]), r, c))

    for r in range(height):
        push(r, 0)
        push(r, width - 1)
    for c in range(1, width - 1):
        push(0, c)
        push(height - 1, c)

    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc, _ in NB8:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < height and 0 <= nc < width) or closed[nr, nc]:
                continue
            closed[nr, nc] = True
            if filled[nr, nc] < z + eps:
                filled[nr, nc] = z + eps
            heapq.heappush(heap, (float(filled[nr, nc]), nr, nc))
    return filled


def d8_receiver(filled: np.ndarray) -> np.ndarray:
    """D8 最陡下降接收像元（按距离归一化坡度）；无下游返回 -1。"""
    height, width = filled.shape
    pad = np.pad(filled, 1, mode="edge")
    best = np.full((height, width), -np.inf)
    target = np.full((height, width), -1, dtype=np.int64)
    rows = np.arange(height)[:, None]
    cols = np.arange(width)[None, :]
    for dr, dc, dist in NB8:
        neighbour = pad[1 + dr : 1 + dr + height, 1 + dc : 1 + dc + width]
        slope = (filled - neighbour) / dist
        improved = slope > best
        best = np.where(improved, slope, best)
        target = np.where(
            improved & (slope > 0),
            (rows + dr) * width + (cols + dc),
            target,
        )
    # 网格边缘的越界目标钳为 -1（无下游）。
    in_bounds = (target >= 0) & (target < height * width)
    target = np.where(in_bounds, target, -1)
    # 无正坡度下游（洼地底/平点底）→ 无下游。
    target = np.where(best > 0, target, -1)
    return target


def flow_accumulation(filled: np.ndarray, receiver: np.ndarray) -> np.ndarray:
    """汇流累积（Kahn 拓扑序：先算头部像元，向下游累加，平点安全）。"""
    from collections import deque

    n = filled.size
    flat_receiver = receiver.ravel()
    acc = np.ones(n, dtype=np.float64)
    indeg = np.zeros(n, dtype=np.int64)
    valid = flat_receiver >= 0
    np.add.at(indeg, flat_receiver[valid], 1)
    queue = deque(np.flatnonzero(indeg == 0).tolist())
    processed = 0
    while queue:
        idx = queue.popleft()
        processed += 1
        downstream = flat_receiver[idx]
        if downstream >= 0:
            acc[downstream] += acc[idx]
            indeg[downstream] -= 1
            if indeg[downstream] == 0:
                queue.append(int(downstream))
    if processed < n:  # 循环兜底（正常不存在）：把剩余像元按行序并入
        for idx in np.flatnonzero(indeg > 0).tolist():
            downstream = flat_receiver[idx]
            if downstream >= 0:
                acc[downstream] += acc[idx]
    return acc.reshape(filled.shape)


def upstream_mask(receiver: np.ndarray, outlet_rc: tuple[int, int]) -> np.ndarray:
    """汇入指定出流像元的全部上游像元（含自身）。

    Kahn 拓扑序取逆（下游→上游）单遍传播：像元属于集合 iff 其接收像元属于集合。
    """
    from collections import deque

    height, width = receiver.shape
    flat_receiver = receiver.ravel()
    mask = np.zeros(height * width, dtype=bool)
    mask[outlet_rc[0] * width + outlet_rc[1]] = True
    indeg = np.zeros(height * width, dtype=np.int64)
    valid = flat_receiver >= 0
    np.add.at(indeg, flat_receiver[valid], 1)
    queue = deque(np.flatnonzero(indeg == 0).tolist())
    order: list[int] = []
    while queue:
        idx = queue.popleft()
        order.append(idx)
        downstream = flat_receiver[idx]
        if downstream >= 0:
            indeg[downstream] -= 1
            if indeg[downstream] == 0:
                queue.append(int(downstream))
    for idx in reversed(order):
        downstream = flat_receiver[idx]
        if downstream >= 0 and mask[downstream]:
            mask[idx] = True
    return mask.reshape((height, width))


def hand(filled: np.ndarray, drainage_mask: np.ndarray) -> np.ndarray:
    """HAND：各像元相对最近排水通道像元的高程差（8 邻域 BFS 传播最近通道）。

    drainage_mask 为 True 的像元（如汇流累积 ≥ 阈值的河道像元）HAND=0；
    其余像元 HAND = filled - 最近通道像元的 filled。
    """
    height, width = filled.shape
    ref = filled.copy()
    hand = np.zeros((height, width), dtype=np.float64)
    if not drainage_mask.any():
        raise ValueError("drainage_mask 为空，无法计算 HAND")
    seen = drainage_mask.copy()
    # 多轮 8 邻域扩张（BFS 等价，按轮扫描，通道像元数远小于全域）。
    work = drainage_mask.copy()
    while work.any():
        nxt = np.zeros_like(work)
        padded = np.pad(work, 1)
        acc_neighbour = np.zeros((height, width), dtype=bool)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                acc_neighbour |= padded[
                    1 + dr : 1 + dr + height, 1 + dc : 1 + dc + width
                ]
        frontier = acc_neighbour & ~seen
        if not frontier.any():
            break
        # 取邻域中通道像元的最小高程作为参考（最近通道的 filled 值）。
        padded_ref = np.pad(ref, 1, mode="constant", constant_values=np.inf)
        best = np.full((height, width), np.inf)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                best = np.minimum(
                    best,
                    padded_ref[
                        1 + dr : 1 + dr + height, 1 + dc : 1 + dc + width
                    ],
                )
        ref = np.where(frontier, best, ref)
        hand = np.maximum(filled - ref, 0.0)
        seen |= frontier
        work = frontier
    return hand
