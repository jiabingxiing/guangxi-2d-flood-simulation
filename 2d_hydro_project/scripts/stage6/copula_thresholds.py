"""Copula 联合阈值探索（计划 T13 / S6；ADR-0018 §2；**探索性，禁业务告警**）。

- 变量：区域年最大 3 日雨量 AM3D（ERA5 1979–2025，Open-Meteo 采样点均值）
  × 事件前 30 日雨量 P30（湿度代理，E3）；
- 边缘：经验 PIT + 高斯核平滑（信息扩散思想，小样本稳健）；
- Copula 三族 AIC 选优（纯 numpy，sdv-dev Copulas 许可证未复核不引入）：
  Gaussian（正态得分）、Clayton（Kendall τ→θ）、t（df 网格极大似然）；
- 联合超越概率 P(U>u, V>v)：Gaussian 数值积分、Clayton 闭式、t 抽样近似；
- Bootstrap（年样本重采样 ×100）给参数与联合概率区间；
- 拟合失败如实记「未验证」（ADR-0018 样本量红线，n=47）。

产出：`data/catalog/copula_thresholds_report.json`。
用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/copula_thresholds.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/copula_thresholds.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

CATALOG = PROJ / "data" / "catalog"
INTERIM = PROJ / "data" / "interim" / "copula"
SAMPLE_POINTS = [
    {"name": "guigang", "lat": 23.11, "lon": 109.60},
    {"name": "ningming", "lat": 22.14, "lon": 107.07},
    {"name": "nanning", "lat": 22.84, "lon": 108.30},
]
YEARS = (1979, 2026)
U_GRID = (0.5, 0.8, 0.9, 0.95, 0.98, 0.99)
EVENTS = {"贵港 2024-09 摩羯": ("2024-09-08", "2024-09-14"),
          "宁明 2026-08 紫檀": ("2026-08-21", "2026-08-27")}
EVIDENCE_LEVEL = "探索性（n=47 年样本；统计阈值禁业务告警，ADR-0004/0018）"


def normal_cdf(z: float) -> float:
    from statistics import NormalDist

    return NormalDist().cdf(z)


def normal_ppf(p: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(p)


def fetch_daily_rain(start_year: int, end_year: int, cache_path: Path,
                     *, use_cache: bool = True) -> tuple[list[str], np.ndarray]:
    """采样点均值日雨（mm）；缓存键 = 窗口 + 点集。end 自动钳到今天（API 拒绝未来日期）。"""
    start = f"{start_year}-01-01"
    end = f"{end_year}-12-31"
    from datetime import date as _date

    today = _date.today() - timedelta(days=6)
    if _date.fromisoformat(end) > today:
        end = today.isoformat()
    if cache_path.is_file() and use_cache:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("window") == [start, end]:
            return cached["time"], np.asarray(cached["precip_mm"], dtype=np.float64)
    import requests
    import time as _time

    acc: np.ndarray | None = None
    dates: list[str] = []
    for k, pt in enumerate(SAMPLE_POINTS):
        url = ("https://archive-api.open-meteo.com/v1/archive"
               f"?latitude={pt['lat']}&longitude={pt['lon']}"
               f"&start_date={start}&end_date={end}"
               "&daily=precipitation_sum&timezone=Asia%2FShanghai")
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"ERA5 日雨获取失败（{pt['name']}）：{exc}") from exc
        t = data["daily"]["time"]
        p = np.asarray(data["daily"]["precipitation_sum"], dtype=np.float64)
        p = np.nan_to_num(p, nan=0.0)
        if acc is None:
            dates = t
            acc = np.zeros(len(t))
        elif t != dates:
            raise RuntimeError("采样点日期轴不一致")
        acc += p
        print(f"  fetch rain {k + 1}/{len(SAMPLE_POINTS)} ({pt['name']})", flush=True)
        _time.sleep(0.2)
    assert acc is not None
    mean_mm = acc / len(SAMPLE_POINTS)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache_path, {"window": [start, end], "time": dates,
                            "precip_mm": mean_mm.tolist(),
                            "points": SAMPLE_POINTS})
    return dates, mean_mm


def annual_am3d_p30(dates: list[str], p_mm: np.ndarray) -> list[dict[str, float]]:
    """按水文年（1–12 月）取年最大 3 日雨量及其前 30 日雨量。"""
    d0 = date.fromisoformat(dates[0])
    rolling3 = np.convolve(p_mm, np.ones(3), mode="valid")  # rolling3[i] 覆盖 d0+i..+2
    rolling30 = np.convolve(p_mm, np.ones(30), mode="valid")  # 覆盖 d0+i..+29
    by_year: dict[int, tuple[float, int]] = {}
    for i in range(rolling3.size):
        mid = d0 + timedelta(days=int(i))
        yr = mid.year
        if mid.month < 12 or rolling3[i] > by_year.get(yr, (0.0, 0))[0]:
            if yr not in by_year or rolling3[i] > by_year[yr][0]:
                by_year[yr] = (float(rolling3[i]), i)
    out = []
    for yr in sorted(by_year):
        am3, i = by_year[yr]
        p30 = float(rolling30[max(0, i - 30)]) if i - 30 >= 0 else 0.0
        out.append({"year": yr, "am3d_mm": round(am3, 2), "p30_mm": round(p30, 2)})
    return out


def kernel_pit(x: np.ndarray, *, bw_scale: float = 0.9) -> np.ndarray:
    """高斯核平滑的 PIT（信息扩散思想）：P_i = Σ K(x_i−x_j)/(n·h 归一)。"""
    n = x.size
    sd = float(np.std(x, ddof=1)) if n > 1 else 1.0
    h = max(bw_scale * sd * n ** (-1.0 / 5.0), 1e-9)
    diff = x[:, None] - x[None, :]
    k = np.exp(-0.5 * (diff / h) ** 2)
    # 避免自身 1/1 边界：减半自身核 + 最小值截断
    pit = (k.sum(axis=1) - 0.5 * 1.0) / n
    return np.clip(pit, 1e-3, 1.0 - 1e-3)


def kendall_tau(x: np.ndarray, y: np.ndarray) -> float:
    n = x.size
    conc = disc = 0
    for i in range(n - 1):
        dx = x[i + 1:] - x[i]
        dy = y[i + 1:] - y[i]
        conc += int(np.sum(dx * dy > 0))
        disc += int(np.sum(dx * dy < 0))
    return (conc - disc) / (n * (n - 1) / 2)


def fit_gaussian(u: np.ndarray, v: np.ndarray) -> dict[str, float]:
    z1 = np.asarray([normal_ppf(p) for p in u])
    z2 = np.asarray([normal_ppf(p) for p in v])
    rho = float(np.corrcoef(z1, z2)[0, 1])
    rho = min(max(rho, -0.98), 0.98)
    c2 = 1.0 / (1 - rho * rho)
    ll = 0.0
    for a, b in zip(z1, z2):
        log_biv = (-0.5 * (a * a - 2 * rho * a * b + b * b) * c2
                   - math.log(2 * math.pi) - 0.5 * math.log(1 - rho * rho))
        log_margin = (-0.5 * a * a - 0.5 * math.log(2 * math.pi)
                      - 0.5 * b * b - 0.5 * math.log(2 * math.pi))
        ll += log_biv - log_margin
    return {"family": "gaussian", "rho": round(rho, 4),
            "loglik": round(ll, 3), "aic": round(-2 * ll + 2 * 1, 3)}


def fit_clayton(u: np.ndarray, v: np.ndarray) -> dict[str, float]:
    tau = kendall_tau(u, v)
    if tau <= 0:
        return {"family": "clayton", "theta": None, "loglik": None,
                "aic": None, "note": "τ≤0：Clayton 域外（只刻画正相依）"}
    theta = 2 * tau / (1 - tau)
    ll = 0.0
    for a, b in zip(u, v):
        s = a ** (-theta) + b ** (-theta) - 1.0
        dens = (1 + theta) * (a * b) ** (-1 - theta) * s ** (-2 - 1.0 / theta)
        ll += math.log(max(dens, 1e-300))
    return {"family": "clayton", "theta": round(theta, 4),
            "loglik": round(ll, 3), "aic": round(-2 * ll + 2 * 1, 3)}


def _t_loglik(u: np.ndarray, v: np.ndarray, rho: float, nu: float) -> float:
    """二元 t copula 对数似然（Γ 用 lgamma；一元 t 密度解析）。"""
    c = 1.0 / math.sqrt(1 - rho * rho)
    lg = math.lgamma((nu + 2) / 2) - math.lgamma(nu / 2)
    ll = 0.0
    from statistics import NormalDist

    for a, b in zip(u, v):
        z1 = NormalDist().inv_cdf(float(a))
        z2 = NormalDist().inv_cdf(float(b))
        # 单元 t 分位数：z 仍用正态得分近似（df 修正有限，登记局限）
        quad = (z1 * z1 - 2 * rho * z1 * z2 + z2 * z2) * c * c / nu
        log_biv = (lg - math.log(nu * math.pi) - 0.5 * math.log(1 - rho * rho)
                   - (nu + 2) / 2 * math.log(1 + quad))
        log_m1 = math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) \
            - 0.5 * math.log(nu * math.pi) - (nu + 1) / 2 * math.log(1 + z1 * z1 / nu)
        log_m2 = math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) \
            - 0.5 * math.log(nu * math.pi) - (nu + 1) / 2 * math.log(1 + z2 * z2 / nu)
        ll += log_biv - log_m1 - log_m2
    return ll


def fit_t(u: np.ndarray, v: np.ndarray) -> dict[str, float]:
    tau = kendall_tau(u, v)
    rho = math.sin(tau * math.pi / 2)
    rho = min(max(rho, -0.98), 0.98)
    best = None
    for nu in range(2, 11):
        ll = _t_loglik(u, v, rho, float(nu))
        if best is None or ll > best[1]:
            best = (float(nu), ll)
    nu, ll = best
    return {"family": "t", "rho": round(rho, 4), "nu": nu,
            "loglik": round(ll, 3), "aic": round(-2 * ll + 2 * 2, 3)}


def clayton_c(u: np.ndarray, v: np.ndarray, theta: float) -> np.ndarray:
    return (u ** (-theta) + v ** (-theta) - 1.0) ** (-1.0 / theta)


def gaussian_joint_exceed(u_thr: float, v_thr: float, rho: float) -> float:
    """P(U>u, V>v) = 1 − u − v + C(u,v)；C(u,v) 经数值积分（梯形，800 点）。"""
    z1 = normal_ppf(u_thr)
    z2 = normal_ppf(v_thr)
    c = math.sqrt(1 - rho * rho)
    n_steps = 800
    ts = np.linspace(-8.0, z1, n_steps)
    phi_t = np.exp(-0.5 * ts * ts) / math.sqrt(2 * math.pi)
    cond_t = np.asarray([normal_cdf((z2 - rho * t) / c) for t in ts])
    c_uv = float(np.trapz(phi_t * cond_t, ts))
    return 1.0 - u_thr - v_thr + c_uv


def t_joint_exceed(u_thr: float, v_thr: float, rho: float, nu: float,
                   n_mc: int = 200_000) -> float:
    """t-copula 联合超越：MC 抽样近似（探索性，登记 MC 误差）。"""
    rng = np.random.default_rng(47)
    chol = np.linalg.cholesky(np.array([[1.0, rho], [rho, 1.0]]))
    z = rng.standard_normal((n_mc, 2)) @ chol.T
    w = rng.chisquare(nu, n_mc) / nu
    tz = z / np.sqrt(w)[:, None]
    from statistics import NormalDist

    nd = NormalDist()
    u1 = np.asarray([nd.cdf(float(x)) for x in tz[:, 0]])
    u2 = np.asarray([nd.cdf(float(x)) for x in tz[:, 1]])
    return float(np.mean((u1 > u_thr) & (u2 > v_thr)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--bootstrap", type=int, default=100)
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    log = AuditLog("copula_thresholds.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(points=SAMPLE_POINTS, years=YEARS, u_grid=U_GRID)

    cache = INTERIM / "era5_daily_1979_2025.json"
    dates, p_mm = fetch_daily_rain(*YEARS, cache, use_cache=not args.no_fetch)
    log.add_input("era5_daily_cache", cache)
    samples = annual_am3d_p30(dates, p_mm)
    am3 = np.asarray([s["am3d_mm"] for s in samples])
    p30 = np.asarray([s["p30_mm"] for s in samples])
    log.step("samples", n=len(samples),
             am3d_max=float(am3.max()), p30_max=float(p30.max()))

    u = kernel_pit(am3)
    v = kernel_pit(p30)
    fits = [fit_gaussian(u, v), fit_clayton(u, v), fit_t(u, v)]
    valid = [f for f in fits if f.get("aic") is not None]
    best = min(valid, key=lambda f: f["aic"]) if valid else None

    surface = []
    event_pos = {}
    if best is not None:
        for uu in U_GRID:
            row = []
            for vv in U_GRID:
                if best["family"] == "gaussian":
                    p = gaussian_joint_exceed(uu, vv, best["rho"])
                elif best["family"] == "clayton":
                    c_uv = float(clayton_c(np.asarray([uu]), np.asarray([vv]), best["theta"])[0])
                    p = 1.0 - uu - vv + c_uv
                else:
                    p = t_joint_exceed(uu, vv, best["rho"], best["nu"])
                row.append(round(max(min(p, 1.0), 0.0), 4))
            surface.append({"u_am3d": uu, "probs": dict(zip(map(str, U_GRID), row))})
        # 事件落位
        for ev, (s, e) in EVENTS.items():
            i0 = dates.index(next(d for d in dates if d >= s))
            i1 = dates.index(next(d for d in dates if d >= e))
            win3 = float(np.convolve(p_mm, np.ones(3), mode="valid")[max(0, i0 - 2):i1 + 1].max())
            p30_ev = float(p_mm[max(0, i0 - 30):i0].sum())
            from numpy import searchsorted

            u_hat = float((am3 < win3).mean() + 0.5 * (am3 == win3).mean())
            v_hat = float((p30 < p30_ev).mean() + 0.5 * (p30 == p30_ev).mean())
            event_pos[ev] = {"am3d_mm": round(win3, 1), "p30_mm": round(p30_ev, 1),
                             "u_hat": round(u_hat, 3), "v_hat": round(v_hat, 3)}
        # Bootstrap CI（重采样年份）
        rng = np.random.default_rng(1979)
        boot_params = []
        n = u.size
        for _ in range(args.bootstrap):
            idx = rng.integers(0, n, n)
            try:
                if best["family"] == "gaussian":
                    boot_params.append(fit_gaussian(u[idx], v[idx])["rho"])
                elif best["family"] == "clayton":
                    th = fit_clayton(u[idx], v[idx]).get("theta")
                    if th is not None:
                        boot_params.append(th)
                else:
                    boot_params.append(fit_t(u[idx], v[idx])["rho"])
            except (ValueError, ZeroDivisionError, OverflowError):
                continue
        boot_ci = None
        if boot_params:
            bp = np.asarray(boot_params, dtype=np.float64)
            boot_ci = {"n_ok": int(bp.size),
                       "lo": round(float(np.percentile(bp, 2.5)), 4),
                       "hi": round(float(np.percentile(bp, 97.5)), 4)}
        best = {**best, "param_bootstrap_ci": boot_ci}

    payload = {
        "script": "copula_thresholds.py",
        "generated_utc": utc_now(),
        "adr": "ADR-0018",
        "evidence_level": EVIDENCE_LEVEL,
        "n_years": len(samples),
        "samples_preview": samples[-6:],
        "fits": fits,
        "best": best,
        "u_v_grid": list(U_GRID),
        "joint_exceedance_surface": surface,
        "event_positions": event_pos,
        "caveats": [
            "n=47 年 → 探索性；拟合选族不确定度大（Bootstrap CI 随附）",
            "P30 为湿度代理（无长序列土壤湿度），E3",
            "联合超越概率 = 同年两变量同时超阈的统计近似；非模型概率",
            "统计阈值仅作情景设计参考，禁业务告警（ADR-0004）",
        ],
        "verdict": "已产出（探索性）" if best is not None else "未验证（拟合失败）",
        "dry_run": bool(args.dry_run),
    }
    out_json = args.log_json or (CATALOG / "copula_thresholds_report.json")
    log.payload["copula"] = payload
    log.write(out_json)
    print(json.dumps({k: payload[k] for k in ("n_years", "fits", "best", "verdict")},
                     ensure_ascii=False, indent=1)[:900])
    print("->", out_json)


if __name__ == "__main__":
    main()
