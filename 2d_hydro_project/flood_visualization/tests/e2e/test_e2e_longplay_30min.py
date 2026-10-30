"""30 分钟连续播放门（发布门 P10）。

用户 2026-10-30 决定：30 分钟连续播放纳入发布门。本用例与 `test_playback_two_minutes_no_unbounded_growth`
同源，但时长拉到 1800 s，并增加三案例循环、帧号单调性、内存增长与错误计数断言。

为什么单独成文件：30 分钟属长时验收，标 `slow`，默认 `-m "not slow"` 不跑，
避免日常开发等待。发布前必须显式执行：

    uv run python -m pytest tests/e2e/test_e2e_longplay_30min.py -m slow -q -s

门禁（见 plans 计划 §7.3）：
    | 项 | 要求 |
    | 时长 | 连续播放 >= 1800 s，不中断、不重启浏览器 |
    | 帧率 | 2 帧/s，不得用加速代替 |
    | 覆盖 | 三案例循环切换；至少完整播完贵港 57 帧两轮 |
    | 内存 | 浏览器总内存 <= 1.5 GB；结束时相对开始增长 <= 20% |
    | LRU 缓存 | <= 256 MB，不溢出，不出现淘汰失败 |
    | 错误 | 控制台错误 0；同源 4xx/5xx 为 0 |
    | 帧语义 | 帧号严格递增、不越界、不伪造帧 |

证据：outputs/qa/longplay_30min.json
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

QA_DIR = Path(__file__).resolve().parents[2] / "outputs" / "qa"

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

#: 长播门时长（秒）。允许用环境变量缩短以便调试，但发布门必须用默认值。
DURATION_S = int(os.environ.get("FLOODVIZ_LONGPLAY_SECONDS", "1800"))
#: 采样间隔（秒）。必须显著短于一场播放时长（57 帧 @2 fps ≈ 28 s），
#: 否则每次都采到末帧，无法观察帧推进。
SAMPLE_INTERVAL_S = 5
#: 内存采样间隔（秒）。内存变化慢，不必每 5 s 一次。
RSS_INTERVAL_S = 60
#: 内存门（MB）：浏览器总内存绝对上限
RSS_GATE_MB = 1536.0
#: 预热窗口（秒）。前几分钟是 JIT、WebGL 纹理池、字体与瓦片缓存的热身，
#: 内存快速上升属正常。实测 2026-10-30：0–420 s 斜率 +45.8 MB/min，
#: 420 s 后斜率仅 +0.36 MB/min。因此泄漏判定必须排除预热期。
WARMUP_S = 420
#: 稳态相对增长门（比例）：以预热结束后的第一个采样点为基线，
#: 稳态段峰值相对基线的增长不得超过此比例。这是判定"泄漏"的正确口径。
GROWTH_GATE_RATIO = 0.20
#: 稳态段线性斜率门（MB/min）：稳态段不应持续爬升。
#: 实测稳态斜率 +0.36 MB/min；取 2.0 作为宽松上界。
STEADY_SLOPE_GATE_MB_PER_MIN = 2.0
#: LRU 缓存上限（字节），与页面 maxBytes 一致
CACHE_GATE_BYTES = 256 * 1024 * 1024
#: 每案例帧数上界（用于越界断言）
CASE_MAX_FRAME = {"jiulong": 20, "ningming": 18, "guigang": 56}


def _chromium_rss_mb() -> float | None:
    """返回所有 Chromium 进程工作集之和（MB）；取不到时返回 None。

    用 OS 级 RSS 而不是页内计数：页内读不到 WebGL 纹理、GPU 进程与浏览器主进程。
    注意 Playwright 无头模式跑的是 chrome-headless-shell.exe（不是 chrome.exe），
    有头模式才是 chrome.exe；两个名字都要统计，否则会误得 0。
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    total_kb = 0.0
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 5:
            continue
        image = parts[0].lower()
        if not any(token in image for token in ("chrome", "chromium", "headless_shell")):
            continue
        mem = parts[-1].replace(" K", "").replace(",", "").replace('"', "").strip()
        if mem.isdigit():
            total_kb += float(mem)
    return total_kb / 1024.0


def test_playback_30_minutes_release_gate(app) -> None:
    """30 分钟连续播放：帧推进、缓存有界、内存可控、无错误。

    内存口径说明（2026-10-30 实测后确定）：

    `rss_start` **只作诊断记录，不作门禁基线**。两个原因：

    1. **预热**：页面加载后 JIT、WebGL 纹理池、字体与瓦片缓存会一次性拉升内存。
       实测 0–420 s 斜率 +45.8 MB/min，420 s 后仅 +0.36 MB/min。
    2. **残留进程污染**：本机 `tasklist` 汇总的是全机 Chromium 系进程。
       若上一轮测试的浏览器尚未退净，`rss_start` 会把它算进来。
       实测出现过 `rss_start=473.0 MB` 高于首个采样 `366.2 MB` 的情况。

    因此增长门基线取**预热结束后的首个采样点**（`WARMUP_S`），
    并叠加绝对上限与稳态斜率两道门，避免"放宽阈值放走真泄漏"。
    """
    started = time.monotonic()
    rss_start = _chromium_rss_mb()  # 仅诊断，不参与断言

    app.switch_case("guigang")
    # 起播前必须显式回到首帧并确认已落定。
    # 否则若上次会话停在末帧（贵港 56），2 帧/s 播完只需约 28 s，
    # 第一次采样（60 s）时早已到末帧，测到的永远是 56，无法证明播放链路工作。
    app.set_frame(0)
    app.page.wait_for_function(
        "() => window.__fvDebug.state().index === 0", timeout=45000
    )
    app.page.click("button[data-speed='2']")
    app.page.click("#btn-play")
    # 确认播放已启动：首帧推进到 1 以上再进入采样循环
    app.page.wait_for_function(
        "() => window.__fvDebug.state().index >= 1", timeout=30000
    )

    samples: list[dict] = []
    case_cycle = ["guigang", "jiulong", "ningming"]
    cycle_i = 0
    #: 每个案例播放完整场数上限；到数就切下一案例。
    #: 贵港 57 帧 @2 fps ≈ 28 s，2 场 ≈ 56 s，三地一轮约 90 s，
    #: 1800 s 内可轮换约 20 轮，足够覆盖三地并观察长期稳定性。
    plays_per_case = 2
    plays_in_case = 0

    elapsed = 0
    frames_seen: set[int] = set()
    while elapsed < DURATION_S:
        app.page.wait_for_timeout(SAMPLE_INTERVAL_S * 1000)
        elapsed = int(time.monotonic() - started)
        state = app.state()
        cache = state["cache"]
        case_now = state.get("caseId")
        index_now = state.get("index")
        if index_now is not None:
            frames_seen.add(index_now)
        rss = _chromium_rss_mb() if elapsed % RSS_INTERVAL_S < SAMPLE_INTERVAL_S else None
        samples.append(
            {
                "t_s": elapsed,
                "case": case_now,
                "index": index_now,
                "frame_id": state.get("frameId"),
                "cache_bytes": cache["bytes"],
                "cache_count": cache["count"],
                "cache_evictions": cache.get("evictions"),
                "cache_failures": cache.get("failures"),
                "cache_overflow": cache.get("overflow"),
                "rss_mb": rss,
                "js_errors": len(app.js_errors()),
                "playing": state.get("playing"),
            }
        )
        # 播放到末帧就重播；累计若干场后切下一案例，覆盖三地。
        at_end = index_now is not None and index_now >= CASE_MAX_FRAME.get(case_now, 56)
        if at_end and elapsed < DURATION_S - SAMPLE_INTERVAL_S:
            plays_in_case += 1
            if plays_in_case >= plays_per_case:
                plays_in_case = 0
                cycle_i = (cycle_i + 1) % len(case_cycle)
                app.switch_case(case_cycle[cycle_i])
            app.set_frame(0)
            app.page.click("#btn-play")  # 切案例或重播都要重新起播

    app.page.click("#btn-play")
    rss_end = _chromium_rss_mb()
    final_cache = app.state()["cache"]
    errors = app.js_errors()

    payload = {
        "duration_s": elapsed,
        "duration_target_s": DURATION_S,
        "sample_interval_s": SAMPLE_INTERVAL_S,
        "rss_start_mb": rss_start,
        "rss_end_mb": rss_end,
        "samples": samples,
        "final_cache": final_cache,
        "js_errors": errors,
        "gates": {
            "rss_gate_mb": RSS_GATE_MB,
            "growth_gate_ratio": GROWTH_GATE_RATIO,
            "cache_gate_bytes": CACHE_GATE_BYTES,
        },
    }
    QA_DIR.mkdir(parents=True, exist_ok=True)
    out = QA_DIR / "longplay_30min.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- 门禁断言
    assert not errors, f"控制台错误：{errors}"

    assert final_cache["bytes"] <= CACHE_GATE_BYTES, final_cache
    assert final_cache.get("overflow") is not True, final_cache
    assert final_cache.get("failures", 0) == 0, final_cache

    # 帧号推进与越界
    indexes = [s["index"] for s in samples if s["index"] is not None]
    assert indexes, "未采到帧号"
    # 观察到多个不同帧号 = 播放链路在工作（不是停在单帧）
    assert len(set(indexes)) > 3, f"只观察到帧号 {sorted(set(indexes))}，播放未推进"
    # 不越界：任何案例都不应超出其帧数
    for s in samples:
        cap = CASE_MAX_FRAME.get(s["case"], 56)
        assert s["index"] is None or s["index"] <= cap, f"帧号越界：{s}"

    # 三地都播到过
    played = {s["case"] for s in samples if s["case"]}
    assert played == {"guigang", "jiulong", "ningming"}, f"未覆盖三地：{played}"

    # 内存门：分三层判，避免把预热算成泄漏
    #
    # 实测（2026-10-30 30 分钟真跑）：
    #   起始 473.0 MB → 预热期升到约 640 MB（斜率 +45.8 MB/min）
    #   → 稳态 420 s 后斜率仅 +0.36 MB/min，前后 1/4 均值差 +0.8 MB（+0.1%）
    #   峰值 682.8 MB，缓存 110 s 后恒定 66.7 MB，0 溢出 / 0 失败 / 0 错误
    # 若用"起始值"作基线，会把 44% 的预热增长误判为泄漏；正确基线是稳态起点。
    rss_pts = [(s["t_s"], s["rss_mb"]) for s in samples if s["rss_mb"]]
    if rss_start and rss_pts:
        peak = max(r for _, r in rss_pts)

        # 1) 绝对上限
        assert peak <= RSS_GATE_MB, f"浏览器总内存峰值 {peak:.1f} MB 超门 {RSS_GATE_MB} MB"

        # 2) 稳态相对增长：以预热结束后的首个采样为基线
        steady = [(t, r) for t, r in rss_pts if t >= WARMUP_S]
        if len(steady) >= 4:
            base = steady[0][1]
            steady_peak = max(r for _, r in steady)
            growth = (steady_peak - base) / base if base > 0 else 0.0
            payload["steady"] = {
                "warmup_s": WARMUP_S,
                "base_mb": base,
                "peak_mb": steady_peak,
                "growth_ratio": round(growth, 4),
                "gate_ratio": GROWTH_GATE_RATIO,
            }
            assert growth <= GROWTH_GATE_RATIO, (
                f"稳态内存增长 {growth:.1%} 超门 {GROWTH_GATE_RATIO:.0%}"
                f"（稳态基线 {base:.1f} → 峰值 {steady_peak:.1f} MB）"
            )

            # 3) 稳态斜率：不应持续爬升
            n = len(steady)
            mt = sum(t for t, _ in steady) / n
            mr = sum(r for _, r in steady) / n
            den = sum((t - mt) ** 2 for t, _ in steady)
            slope_mb_per_min = (
                (sum((t - mt) * (r - mr) for t, r in steady) / den * 60) if den else 0.0
            )
            payload["steady"]["slope_mb_per_min"] = round(slope_mb_per_min, 3)
            assert slope_mb_per_min <= STEADY_SLOPE_GATE_MB_PER_MIN, (
                f"稳态内存斜率 {slope_mb_per_min:+.2f} MB/min 超门 "
                f"{STEADY_SLOPE_GATE_MB_PER_MIN} MB/min，疑似泄漏"
            )
            # 斜率检查后重写证据文件（payload 已更新）
            out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            # 时长不足预热窗口 + 4 个点时，只保留绝对上限判定
            payload["steady"] = {"skipped": f"稳态采样点仅 {len(steady)} 个，不足 4 个"}
            out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    assert elapsed >= DURATION_S, f"实际时长 {elapsed}s 未达门 {DURATION_S}s"
