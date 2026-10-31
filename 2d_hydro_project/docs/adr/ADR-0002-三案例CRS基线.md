---
doc_type: explanation
status: active
topic: adr-0002
canonical: true
scope: project
---

# ADR-0002：三案例水平 CRS 基线

- **状态**：有条件接受（AOI 为暂定框；业务确认精确 AOI 后复跑核验脚本方可转正）
- **日期**：2026-10-24

## 背景

终稿 0.3 将三地 CRS 列为争议项：贵港约 109°E 应优先核验 UTM 49N，不得照抄固定 EPSG。

## 决策

| 案例 | 暂定 AOI（WGS84） | 核验 EPSG |
|---|---|---|
| 久隆 | 108.55–108.90°E, 21.92–22.20°N | **32649（UTM 49N）** |
| 宁明 | 106.90–107.60°E, 21.95–22.45°N | **32648（UTM 48N）** |
| 贵港 | 109.20–110.00°E, 22.95–23.40°N | **32649（UTM 49N）** |

核验方法：`scripts/stage0/check_crs.py` —— pyproj `query_utm_crs_info` 按 AOI 几何查询 + 经度分带公式交叉验证；三 AOI 均单带覆盖。贵港项为实测确认（109°E < 114°E，落 49 带），非照抄。

## 约束

1. AOI 任何调整后必须复跑核验脚本；若越出带界（108°E/114°E），重新决策。
2. 全部模型栅格最终统一到案例 CRS，米制；像元对齐由契约测试守护（`tests/test_config_contract.py`）。
3. 垂直基准**独立于本决策**，仍为阻塞项（`datum_offset_m: null`，见 aoi_crs_baseline.md 第 2 节）。

## 影响

- 配置 `configs/base.yaml` 与三案例 YAML 已写入对应 EPSG；契约测试锁定，防止回退为经纬度网格。
