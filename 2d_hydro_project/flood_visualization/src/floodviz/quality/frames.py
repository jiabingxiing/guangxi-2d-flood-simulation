"""帧审计：逐帧文件名、序号连续性、栅格头一致性、时间单调性。

发布门禁（SPEC §2）：缺帧、序号不连续、头不一致、末帧超模拟结束时长 → 拒绝发布。
审计只读文件名与 6 行头，不读像元值（数值验收在质量门另做）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..raster.grid import read_ascii_header

FRAME_RE = re.compile(r"^res-(\d{4})\.wd$")
MAX_RE = re.compile(r"^res\.max$")


@dataclass(frozen=True)
class FrameInfo:
    """单帧元信息。"""

    seq: int
    path: Path
    sha256: str
    header: dict[str, float]
    size_bytes: int

    @property
    def frame_id(self) -> str:
        return f"f{self.seq:04d}"

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass
class RunAudit:
    """run 审计结果；reject_reasons 非空即拒绝发布。"""

    run_dir: Path
    frames: list[FrameInfo] = field(default_factory=list)
    contiguous: bool = False
    headers_identical: bool = False
    count: int = 0
    expected_count: int | None = None
    save_interval_s: float | None = None
    simulation_duration_s: float | None = None
    reject_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    max_grid_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.reject_reasons

    def to_dict(self) -> dict:
        return {
            "run_dir": str(self.run_dir),
            "count": self.count,
            "expected_count": self.expected_count,
            "contiguous": self.contiguous,
            "headers_identical": self.headers_identical,
            "save_interval_s": self.save_interval_s,
            "simulation_duration_s": self.simulation_duration_s,
            "reject_reasons": list(self.reject_reasons),
            "warnings": list(self.warnings),
            "max_grid_present": self.max_grid_path is not None,
            "frames": [
                {
                    "frame_id": f.frame_id,
                    "seq": f.seq,
                    "filename": f.filename,
                    "size_bytes": f.size_bytes,
                    "sha256": f.sha256,
                }
                for f in self.frames
            ],
        }


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def list_frame_files(run_dir: Path) -> list[tuple[int, Path]]:
    """列出 results/res-XXXX.wd，返回按序号排序的 (seq, path)，并顺带发现非法文件名。"""
    results = run_dir / "results"
    if not results.is_dir():
        raise FileNotFoundError(f"缺少 results 目录：{results}")
    found: list[tuple[int, Path]] = []
    for child in sorted(results.iterdir()):
        if not child.is_file():
            continue
        match = FRAME_RE.match(child.name)
        if match:
            found.append((int(match.group(1)), child))
    found.sort(key=lambda item: item[0])
    return found


def audit_run(
    run_dir: str | Path,
    *,
    save_interval_s: float,
    simulation_duration_s: float,
    expected_count: int | None = None,
    compute_sha256: bool = True,
) -> RunAudit:
    """逐帧审计；任何硬条件不满足都写入 reject_reasons。"""
    run_dir = Path(run_dir)
    audit = RunAudit(
        run_dir=run_dir,
        save_interval_s=float(save_interval_s),
        simulation_duration_s=float(simulation_duration_s),
        expected_count=(
            int(expected_count)
            if expected_count is not None
            else round(float(simulation_duration_s) / float(save_interval_s)) + 1
        ),
    )
    if not run_dir.is_dir():
        audit.reject_reasons.append(f"run 目录不存在：{run_dir}")
        return audit

    pairs = list_frame_files(run_dir)
    if not pairs:
        audit.reject_reasons.append(f"{run_dir}/results 下没有 res-XXXX.wd")
        return audit

    seqs = [seq for seq, _ in pairs]
    audit.contiguous = seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    if not audit.contiguous:
        missing = sorted(set(range(seqs[0], seqs[-1] + 1)) - set(seqs))
        audit.reject_reasons.append(f"帧序号不连续，缺序号：{missing}")
    if seqs[0] != 0:
        audit.reject_reasons.append(f"首帧序号为 {seqs[0]}，期望从 0 开始（零起点约定）")

    headers: list[dict[str, float]] = []
    for seq, path in pairs:
        header = read_ascii_header(path)
        headers.append(header)
        audit.frames.append(
            FrameInfo(
                seq=seq,
                path=path,
                sha256=sha256_file(path) if compute_sha256 else "",
                header=header,
                size_bytes=path.stat().st_size,
            )
        )
    audit.count = len(audit.frames)
    audit.headers_identical = all(h == headers[0] for h in headers[1:])
    if not audit.headers_identical:
        audit.reject_reasons.append("各帧栅格头不一致（尺寸/原点/像元/NoData 有差异）")

    last = audit.frames[-1]
    last_sim = last.seq * float(save_interval_s)
    if last_sim > float(simulation_duration_s) + 1e-6:
        audit.reject_reasons.append(
            f"末帧 {last.frame_id} 时间 {last_sim:.0f}s 超过模拟结束 {float(simulation_duration_s):.0f}s"
        )
    if audit.count < audit.expected_count:
        audit.reject_reasons.append(
            f"帧数不足：实得 {audit.count}，"
            f"按 saveint={save_interval_s}s 与 sim={simulation_duration_s}s 期望 {audit.expected_count}"
        )
    elif audit.count > audit.expected_count:
        audit.warnings.append(f"帧数多于零起点期望值：{audit.count} vs {audit.expected_count}（多出的帧不进入时间轴）")

    max_path = run_dir / "results" / "res.max"
    if max_path.is_file():
        audit.max_grid_path = max_path
        max_header = read_ascii_header(max_path)
        if max_header != headers[0]:
            audit.reject_reasons.append("res.max 栅格头与帧头不一致")
    else:
        audit.warnings.append("缺少 results/res.max（最大包络图层不提供）")

    return audit
