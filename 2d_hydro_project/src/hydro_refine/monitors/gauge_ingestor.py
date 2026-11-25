"""官方水文监测站数据接入、清洗、质量标注与插值补齐模块。

支持三种合法数据源通道（绝不绕过反爬/字体混淆）：
1. 项目配置与本地官方公报归档（YAML / CSV / JSON）；
2. 本地水文站点 Shapefile/DBF 档案（如宁明县水位站点信息.shp）；
3. 公开无加密政务网页/JSON API（若检测到 FontTag 字体混淆或 JS WAF 质询，立即 fail-closed 返回 protected_or_obfuscated）。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
import shapefile
import yaml

from hydro_refine.core.paths import PROJ, rel_posix, resolve_legacy
from hydro_refine.monitors.datum_converter import (
    DatumAlignmentResult,
    align_water_levels,
    load_atl13_probe_bounds,
)


@dataclass
class GaugeObservation:
    """单条水情监测记录（qc_flag: 0=实测有效, 1=平滑, 2=插值填补, 9=异常或跨工况排除）。"""

    timestamp: str
    t_h: float
    water_level_m: float
    discharge_m3s: float | None = None
    qc_flag: int = 0
    source: str = "official_archive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "t_h": round(float(self.t_h), 3),
            "water_level_m": round(float(self.water_level_m), 4),
            "discharge_m3s": (
                None if self.discharge_m3s is None else round(float(self.discharge_m3s), 3)
            ),
            "qc_flag": int(self.qc_flag),
            "source": self.source,
        }


@dataclass
class StationSeries:
    """单站标准化水情序列与元数据。"""

    station_id: str
    name: str
    river: str
    lon: float
    lat: float
    in_model_domain: bool = True
    station_datum: str = "station_local"
    warning_level_m: float | None = None
    baseflow_level_est_m: float | None = None
    snap_centerline: str | None = None
    max_snap_m: float = 5000.0
    exclude_from_validation: bool = False
    regime: str = "historical_matched"
    observations: list[GaugeObservation] = field(default_factory=list)
    alignment: DatumAlignmentResult | None = None

    def valid_observations(self, *, max_qc_flag: int = 1) -> list[GaugeObservation]:
        return [o for o in self.observations if o.qc_flag <= max_qc_flag]

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "name": self.name,
            "river": self.river,
            "lon": self.lon,
            "lat": self.lat,
            "in_model_domain": self.in_model_domain,
            "station_datum": self.station_datum,
            "warning_level_m": self.warning_level_m,
            "baseflow_level_est_m": self.baseflow_level_est_m,
            "snap_centerline": self.snap_centerline,
            "max_snap_m": self.max_snap_m,
            "exclude_from_validation": self.exclude_from_validation,
            "regime": self.regime,
            "alignment": self.alignment.to_dict() if self.alignment else None,
            "observations": [o.to_dict() for o in self.observations],
        }


@dataclass
class IngestionResult:
    """流域站点接入汇总。"""

    case: str
    event_id: str
    t0_local: str
    crs_epsg: int
    stations: list[StationSeries]
    source_files: list[str] = field(default_factory=list)
    http_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "event_id": self.event_id,
            "t0_local": self.t0_local,
            "crs_epsg": self.crs_epsg,
            "source_files": self.source_files,
            "http_status": self.http_status,
            "stations": [s.to_dict() for s in self.stations],
        }


def _hours_since(ts_str: str, t0_str: str) -> float:
    dt = datetime.fromisoformat(ts_str)
    t0 = datetime.fromisoformat(t0_str)
    return (dt - t0).total_seconds() / 3600.0


def fetch_public_http_table(url: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    """尝试获取公开无加密水情网页/JSON 接口。

    检测到 FontTag 字体混淆或 JS 防护质询时立即 fail-closed，绝不尝试破解或绕过。
    """
    try:
        resp = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": "gx-hydro2d-research/0.1"},
        )
    except requests.RequestException as exc:
        return {"status": "network_unreachable", "url": url, "error": str(exc), "rows": []}

    body = resp.text or ""
    if "#FontTag" in body or "otltag" in body or "<script r='m'>" in body:
        return {
            "status": "protected_or_obfuscated",
            "url": url,
            "http_code": resp.status_code,
            "note": "目标网页启用了字体加密/JS 防护，按合规策略拒绝绕过，请改用官方导出归档 CSV/Shapefile。",
            "rows": [],
        }

    if resp.status_code != 200:
        return {
            "status": f"http_{resp.status_code}",
            "url": url,
            "http_code": resp.status_code,
            "rows": [],
        }

    try:
        payload = resp.json()
        rows = payload.get("result") or payload.get("data") or (payload if isinstance(payload, list) else [])
        return {"status": "ok", "url": url, "rows": rows}
    except ValueError:
        return {"status": "non_json_response", "url": url, "rows": []}


def load_csv_observations(
    csv_path: Path, t0_local: str
) -> dict[str, list[GaugeObservation]]:
    """读取标准化水情 CSV：station_id,timestamp,water_level_m[,discharge_m3s,qc_flag,source]。"""
    by_station: dict[str, list[GaugeObservation]] = {}
    with csv_path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sid = row["station_id"].strip()
            ts = row["timestamp"].strip()
            wl = float(row["water_level_m"])
            q_raw = row.get("discharge_m3s", "").strip()
            q = float(q_raw) if q_raw else None
            qc = int(row["qc_flag"]) if row.get("qc_flag", "").strip() else 0
            src = row.get("source", csv_path.name).strip()
            obs = GaugeObservation(
                timestamp=ts,
                t_h=_hours_since(ts, t0_local),
                water_level_m=wl,
                discharge_m3s=q,
                qc_flag=qc,
                source=src,
            )
            by_station.setdefault(sid, []).append(obs)
    return by_station


def _load_shapefile_archive(archive: dict[str, Any], t0_local: str) -> list[StationSeries]:
    """从官方水位站点 Shapefile 加载站点基期与峰值记录（如宁明 4 站档案）。"""
    shp_path = resolve_legacy(archive["path"])
    fmap = archive["field_map"]
    base_ts = archive["base_timestamp"]
    peak_ts = archive["peak_timestamp"]

    sf = shapefile.Reader(str(shp_path))
    out: list[StationSeries] = []
    for idx, sr in enumerate(sf.shapeRecords()):
        rec = sr.record.as_dict()
        name = str(rec[fmap["name"]]).strip()
        river = str(rec[fmap["river"]]).strip()
        lon = float(rec.get(fmap["lon"]) or sr.shape.points[0][0])
        lat = float(rec.get(fmap["lat"]) or sr.shape.points[0][1])
        base_lvl = float(rec[fmap["base_level"]])
        peak_lvl = float(rec[fmap["peak_level"]])
        obs = [
            GaugeObservation(
                timestamp=base_ts,
                t_h=_hours_since(base_ts, t0_local),
                water_level_m=base_lvl,
                qc_flag=0,
                source=f"{shp_path.name}（基期）",
            ),
            GaugeObservation(
                timestamp=peak_ts,
                t_h=_hours_since(peak_ts, t0_local),
                water_level_m=peak_lvl,
                qc_flag=0,
                source=f"{shp_path.name}（峰值）",
            ),
        ]
        out.append(
            StationSeries(
                station_id=f"shp_{idx + 1}_{name}",
                name=name,
                river=river,
                lon=lon,
                lat=lat,
                in_model_domain=True,
                station_datum="station_local",
                baseflow_level_est_m=base_lvl,
                observations=obs,
            )
        )
    return out


def interpolate_hourly_series(
    observations: list[GaugeObservation],
    *,
    max_gap_h: float = 6.0,
) -> list[GaugeObservation]:
    """对存在短暂缺测的水位序列执行整小时线性/保形插值（仅填补 <= max_gap_h 的间隔，插值点标 qc_flag=2）。"""
    valid = sorted((o for o in observations if o.qc_flag <= 1), key=lambda x: x.t_h)
    if len(valid) < 2:
        return list(observations)

    out = list(observations)
    known_t = {round(o.t_h, 3) for o in observations}
    for a, b in zip(valid, valid[1:]):
        gap = b.t_h - a.t_h
        if 1.0 < gap <= max_gap_h:
            t0_dt = datetime.fromisoformat(a.timestamp)
            n_steps = int(round(gap))
            for step in range(1, n_steps):
                th = a.t_h + step
                if round(th, 3) in known_t or th >= b.t_h - 1e-6:
                    continue
                frac = step / gap
                val = a.water_level_m + frac * (b.water_level_m - a.water_level_m)
                ts_iso = (
                    t0_dt.fromtimestamp(t0_dt.timestamp() + step * 3600.0, tz=t0_dt.tzinfo)
                    .isoformat(timespec="seconds")
                )
                out.append(
                    GaugeObservation(
                        timestamp=ts_iso,
                        t_h=th,
                        water_level_m=val,
                        qc_flag=2,
                        source=f"interpolated({a.source})",
                    )
                )
    out.sort(key=lambda x: x.t_h)
    return out


def ingest_case_gauges(
    config_path: Path,
    *,
    extra_csv: Path | None = None,
    atl13_probe_json: Path | None = None,
    probe_url: str | None = None,
) -> IngestionResult:
    """根据站点 YAML 配置接入、融合并对齐指定案例的官方水位监测记录。"""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    case = str(cfg["case"])
    event_id = str(cfg["event_id"])
    t0_local = str(cfg["t0_local"])
    crs_epsg = int(cfg["crs_epsg"])
    source_files = [rel_posix(config_path)]

    atl13_bounds = load_atl13_probe_bounds(atl13_probe_json) if atl13_probe_json else None

    stations_map: dict[str, StationSeries] = {}
    for st in cfg.get("stations", []):
        sid = str(st["station_id"])
        obs_list = [
            GaugeObservation(
                timestamp=str(o["timestamp"]),
                t_h=_hours_since(str(o["timestamp"]), t0_local),
                water_level_m=float(o["water_level_m"]),
                discharge_m3s=(
                    None if o.get("discharge_m3s") is None else float(o["discharge_m3s"])
                ),
                qc_flag=int(o.get("qc_flag", 0)),
                source=str(o.get("source", "config")),
            )
            for o in st.get("observations", [])
        ]
        stations_map[sid] = StationSeries(
            station_id=sid,
            name=str(st["name"]),
            river=str(st.get("river", "")),
            lon=float(st["lon"]),
            lat=float(st["lat"]),
            in_model_domain=bool(st.get("in_model_domain", True)),
            station_datum=str(st.get("station_datum", "station_local")),
            warning_level_m=(
                None if st.get("warning_level_m") is None else float(st["warning_level_m"])
            ),
            baseflow_level_est_m=(
                None
                if st.get("baseflow_level_est_m") is None
                else float(st["baseflow_level_est_m"])
            ),
            snap_centerline=st.get("snap_centerline"),
            max_snap_m=float(st.get("max_snap_m", 5000.0)),
            exclude_from_validation=bool(
                st.get("exclude_from_validation", False)
                or st.get("exclude_from_2024_validation", False)
            ),
            regime=str(st.get("regime", "historical_matched")),
            observations=obs_list,
        )

    # 合并档案 Shapefile（按站点名匹配，不重复创建同名站）
    for arc in cfg.get("archives", []):
        if arc.get("type") == "shapefile":
            shp_resolved = resolve_legacy(arc["path"])
            source_files.append(rel_posix(shp_resolved))
            for s in _load_shapefile_archive(arc, t0_local):
                matched = next((ex for ex in stations_map.values() if ex.name == s.name), None)
                if matched is not None:
                    seen_ts = {o.timestamp for o in matched.observations}
                    for o in s.observations:
                        if o.timestamp not in seen_ts:
                            matched.observations.append(o)
                else:
                    stations_map[s.station_id] = s

    # 合并外部 CSV 补充逐时观测
    if extra_csv is not None and extra_csv.is_file():
        source_files.append(rel_posix(extra_csv) if extra_csv.is_relative_to(PROJ) else str(extra_csv.name))
        csv_obs = load_csv_observations(extra_csv, t0_local)
        for sid, obs_list in csv_obs.items():
            if sid in stations_map:
                seen_ts = {o.timestamp for o in stations_map[sid].observations}
                for o in obs_list:
                    if o.timestamp not in seen_ts:
                        stations_map[sid].observations.append(o)

    # 排序并执行垂直基准对齐登记
    for st in stations_map.values():
        st.observations.sort(key=lambda o: o.t_h)
        valid_obs = st.valid_observations()
        levels = [o.water_level_m for o in valid_obs]
        st.alignment = align_water_levels(
            levels,
            source_datum=st.station_datum,
            target_datum="dem_local",
            atl13_bounds=atl13_bounds,
        )

    http_status: dict[str, Any] = {}
    if probe_url:
        http_status = fetch_public_http_table(probe_url)

    return IngestionResult(
        case=case,
        event_id=event_id,
        t0_local=t0_local,
        crs_epsg=crs_epsg,
        stations=list(stations_map.values()),
        source_files=source_files,
        http_status=http_status,
    )
