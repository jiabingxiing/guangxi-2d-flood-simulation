"""时间轴构造：把逐帧源文件映射为可发布的时间契约（SPEC §5）。

硬规则：
- 相对时间一律由 `source_sequence * save_interval_s` 计算，并逐帧写显式 sim_seconds；
  不使用文件修改时间、目录名、GIF 帧号或 generated_utc。
- `time_origin` 缺失或未核验（origin_status 以 relative_only 开头）时 timestamp 必须为 null，
  UI 只能显示 T+ 小时；不得猜填绝对时间。
- 帧严格递增；帧数与数组长度一致。
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..config import CaseConfig, RunConfig
    from ..quality.frames import FrameInfo

SCHEMA_VERSION = "1.0"


def compute_sim_seconds(seq: int, save_interval_s: float) -> float:
    """零起点等间隔 run 的相对模拟秒。非等间隔/断点续算 run 必须直接用逐帧时间表。"""
    if seq < 0:
        raise ValueError(f"帧序号不能为负：{seq}")
    if save_interval_s <= 0:
        raise ValueError(f"save_interval_s 必须为正：{save_interval_s}")
    return float(seq) * float(save_interval_s)


def origin_has_absolute_time(origin_status: str) -> bool:
    """只有非 relative_only* 的状态才允许写绝对时间戳。"""
    return not str(origin_status).startswith("relative_only")


def format_timestamp(
    time_origin: str | None,
    sim_seconds: float,
    *,
    origin_status: str = "model_schedule_confirmed",
) -> str | None:
    """按起点 + 相对秒生成 ISO8601（保留起点时区偏移）；不可用时返回 None。"""
    if time_origin is None or not origin_has_absolute_time(origin_status):
        return None
    base = dt.datetime.fromisoformat(str(time_origin).replace("Z", "+00:00"))
    if base.tzinfo is None:
        raise ValueError(f"time_origin 必须带时区偏移：{time_origin!r}")
    moment = base + dt.timedelta(seconds=float(sim_seconds))
    return moment.isoformat()


def build_timeline(
    case: CaseConfig,
    run: RunConfig,
    frames: list[FrameInfo],
    *,
    images_prefix: str,
    blocks_prefix: str,
    statistics_path: str,
    numeric_manifest_path: str,
) -> dict[str, Any]:
    """构造 timeline.json 文档。

    images_prefix 例：`frames/guigang/scen_yagi_q11500_30m_b5`
    blocks_prefix 例：`numeric/guigang/scen_yagi_q11500_30m_b5`
    """
    items: list[dict[str, Any]] = []
    previous = -1.0
    for frame in frames:
        sim_seconds = compute_sim_seconds(frame.seq, run.save_interval_s)
        if sim_seconds <= previous:
            raise ValueError(f"帧时间不严格递增：{frame.frame_id} -> {sim_seconds}")
        previous = sim_seconds
        if sim_seconds > run.simulation_duration_s + 1e-6:
            raise ValueError(f"帧 {frame.frame_id} 时间 {sim_seconds}s 超过模拟结束 {run.simulation_duration_s}s")
        items.append(
            {
                "frame_id": frame.frame_id,
                "source_sequence": frame.seq,
                "sim_seconds": sim_seconds,
                "timestamp": format_timestamp(run.time_origin, sim_seconds, origin_status=run.origin_status),
                "depth_image": f"{images_prefix}/{frame.frame_id}.png",
                "query_blocks": f"{blocks_prefix}/{frame.frame_id}/{{block_y}}/{{block_x}}.bin",
                "sha256": frame.sha256,
            }
        )
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case.case_id,
        "event_id": case.event_id,
        "run_id": run.run_id,
        "time_origin": run.time_origin if origin_has_absolute_time(run.origin_status) else None,
        "time_zone": case.time_zone,
        "origin_status": run.origin_status,
        "origin_note": run.origin_note,
        "save_interval_s": float(run.save_interval_s),
        "simulation_duration_s": float(run.simulation_duration_s),
        "frame_count": len(items),
        "temporal_interpolation": "none",
        "statistics": statistics_path,
        "numeric_manifest": numeric_manifest_path,
        "frames": items,
    }
    validate_timeline(doc)
    return doc


def validate_timeline(doc: dict[str, Any]) -> None:
    """契约自检；不合法直接抛错（阻止发布）。"""
    frames = doc.get("frames") or []
    if not frames:
        raise ValueError("timeline.frames 为空")
    if doc.get("frame_count") != len(frames):
        raise ValueError(f"frame_count={doc.get('frame_count')} 与 frames 长度 {len(frames)} 不一致")
    if doc.get("temporal_interpolation") != "none":
        raise ValueError("temporal_interpolation 必须为 'none'（禁止伪造中间时刻）")
    origin_ok = origin_has_absolute_time(doc.get("origin_status", ""))
    seqs: list[int] = []
    times: list[float] = []
    for idx, frame in enumerate(frames):
        for key in ("frame_id", "source_sequence", "sim_seconds", "depth_image", "query_blocks", "sha256"):
            if key not in frame:
                raise ValueError(f"frames[{idx}] 缺少字段 {key!r}")
        seq = int(frame["source_sequence"])
        sim = float(frame["sim_seconds"])
        seqs.append(seq)
        times.append(sim)
        if origin_ok:
            if not frame["timestamp"]:
                raise ValueError(f"frames[{idx}] origin 已确认但 timestamp 为空")
        elif frame["timestamp"] is not None:
            raise ValueError(f"frames[{idx}] origin 未确认却写了绝对 timestamp（禁止猜填）")
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        raise ValueError("source_sequence 必须严格递增且不重复")
    if any(b <= a for a, b in itertools.pairwise(times)):
        raise ValueError("sim_seconds 必须严格递增")
    if not str(frames[0]["depth_image"]).startswith("frames/"):
        raise ValueError("depth_image 必须是发布包内相对路径（frames/…）")
    if "{block_y}" not in frames[0]["query_blocks"]:
        raise ValueError("query_blocks 模板必须包含 {block_y}/{block_x}")
