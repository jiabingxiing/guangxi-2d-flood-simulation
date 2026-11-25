# 光学与 SAR 显示资产登记（flood_visualization）

> 生成时间 UTC：2026-10-27T09:24:37+00:00 ｜ 生成命令：`uv run python scripts/prepare_optical.py --layer optical --case jiulong --force`
> 机器可读清单：[data/processed/assets/manifest.json](../data/processed/assets/manifest.json)；契约见 [SPEC.md §9](../SPEC.md)。
> 本文件由 `scripts/prepare_optical.py` 按实测结果生成；数字为本次运行实测值。
> 下文各资产的「来源 / 本地栅格」路径是 **2026-10-27 生成时的登记值**；存储随后迁到 `E:\NWKYYSJZX\2026\` 且上级目录改名，`D:/.../2026年工作/...` 已不存在。现役位置以 `GX_OPTICAL_ROOT` 与父项目 `scripts/common/paths.py` 解析为准，不要照抄这些字符串。

## 1. 资产总表

状态统计：remote_verified 0，local_unchecked 0，ready 3，partial 3，missing 0，failed 0；失败 0 个。只有 `ready/partial` 可启用。

| 案例 | 图层 | asset_id | 状态 | 采集时间（北京） | 输出 PNG | 尺寸 px | 体积 | 有效覆盖 | 许可 |
|---|---|---|---|---|---|---|---|---:|---|
| jiulong | optical | jiulong_rgb_20260403 | **partial** | 2026-04-03（仅日期，精确过境时刻未核实） | `data/processed/optical/jiulong/jiulong_rgb_20260403.png` | 2048×1241 | 3.33 MiB | 模型网格 30.429% | 未核实（内部） |
| jiulong | sar | jiulong_s1_20240917 | **ready** | 2024-09-17T18:50:23+08:00 | `data/processed/assets/sar/jiulong_s1_20240917.png` | 182×152 | 0.7 KiB | 模型网格 100.0% | Sentinel（修改后署名） |
| ningming | optical | ningming_s2_20260903 | **ready** | 2026-09-03T11:32:14+08:00 | `data/processed/optical/ningming/ningming_s2_20260903.png` | 1472×1321 | 3.97 MiB | AOI 有效 100.0% | Sentinel（修改后署名） |
| ningming | sar | ningming_gf3_20260824 | **ready** | 2026-08-24（仅日期；精确过境时刻未核实，父项目登记 time_uncertainty_h=6） | `data/processed/assets/sar/ningming_gf3_20260824.png` | 244×224 | 2.9 KiB | 模型网格 100.0% | 未核实（内部） |
| guigang | optical | guigang_s2_20241003 | **partial** | 2024-10-03T11:21:43+08:00 | `data/processed/optical/guigang/guigang_s2_20241003.png` | 1792×1025 | 3.93 MiB | AOI 有效 96.8428% | Sentinel（修改后署名） |
| guigang | sar | guigang_s1_20240912 | **partial** | 2024-09-12T18:42:22+08:00 | `data/processed/assets/sar/guigang_s1_20240912.png` | 2230×1233 | 21.1 KiB | 模型网格 20.64% | Sentinel（修改后署名） |

## 2. 逐资产说明

### jiulong / optical / `jiulong_rgb_20260403`

- 状态：**partial**（仅覆盖模型域 30.429%（8418/27664 格）→ partial）
- 角色：background；标签：可见光背景（本地 RGB，2026-04-03）
- 摘要：2026 年影像，非 2024 事件同期；本地商业 RGB（SVN3-01，EPSG:4326，4.7 GB），仅部分覆盖模型域
- 备注：2026 年影像，非 2024 事件同期，仅部分覆盖；来源/传感器/许可未核实，仅内部研究展示
- 输出：`data/processed/optical/jiulong/jiulong_rgb_20260403.png`（**2048×1241 px**，**3.33 MiB**，sha256 `a101cf42dbad8872…`）
- 尺寸账目：原生裁剪 **57181×34663 px** → 按长边 2048 px 采样 **2048×1241 px** → 压预算落盘 **2048×1241 px**（重采样 average，二分 0 轮，scale 1.0，预算 ≤4 MiB，达标 True）
- 元数据：同目录 `.json`（`data/processed/optical/jiulong/jiulong_rgb_20260403.json`）
- 足迹：`data/processed/optical/jiulong/jiulong_rgb_20260403_footprint.geojson`
- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）：`[[108.598887, 22.075983], [108.8562015, 22.075983], [108.8562015, 21.92], [108.598887, 21.92]]`
- 来源：本地 `D:/NWKYYSJZX/2026年工作/0731 五省区DEM/淹没分析 7.8/data/500kV久隆站附近/灾前可见光/SVN3-01_20260403_L4D0000293335_02_6042500742470001_RGB.tiff`（4500.55 MiB，EPSG:4326，分辨率 [4.5e-06, 4.5e-06]°，nodata=0.0）
  - 来源说明：本地项目影像（SVN3-01 命名）；卫星/传感器/许可/处理链未核实
- 采集时间（北京）：2026-04-03（仅日期，精确过境时刻未核实）；UTC：None；依据：文件名日期 20260403；精确过境时刻未核实
- 裁剪范围 WGS84 `[w,s,e,n]`：[108.598887, 21.92, 108.8562015, 22.075983]
- 来源影像范围 WGS84：[108.598887, 21.829158, 108.8562015, 22.075983]
- 遮罩：本地影像无 SCL 类云分类；未做云/云影遮罩，透明只表示无数据
- 覆盖统计：覆盖模型网格 30.429%（8418/27664 格，336.72 km²）；显示图透明像元 25.3314%
- 许可：未核实（本地项目影像，来源/授权待确认）
  - 原始数据署名：`本地项目影像（SVN3-01），来源未核实`
  - 修改后署名：`本地项目影像（SVN3-01）裁剪，来源未核实`
  - 修改说明：裁剪到模型域包络、重采样显示尺寸；来源/许可未核实，公开发布前须确认
  - 条款文件：无（授权未核实）
- 生成命令：`uv run python scripts/prepare_optical.py --layer optical --case jiulong`
- 生成时间 UTC：2026-10-27T09:24:37+00:00
- 其他说明：
  - 2026-04 背景，非 2024 事件同期（模型事件为 2024-09）。
  - 只覆盖模型域一部分，状态 partial；不得当整域背景。
  - 本地影像来源/传感器/许可未核实；仅限内部研究展示，公开再分发前须确认。
  - 整景 4.7 GB 未入库，只保留本裁剪小图。

### jiulong / sar / `jiulong_s1_20240917`

- 状态：**ready**（覆盖模型网格 100.0%（27664/27664 格）→ ready）
- 角色：sar_observation；标签：SAR 水体（S1 2024-09-17 退水后，二值掩膜）
- 摘要：S1 20 m 提取的二值水体掩膜（σ⁰_VV 固定阈值 -17 dB，父项目 v2），渲染到模型网格；不是水深
- 备注：SAR 不是水深，不能共用深度图例；采集时刻与模拟事件不同期，仅范围证据
- 输出：`data/processed/assets/sar/jiulong_s1_20240917.png`（**182×152 px**，**0.7 KiB**，sha256 `b1d5cfc861c8fc36…`）
- 元数据：同目录 `.json`（`data/processed/assets/sar/jiulong_s1_20240917.json`）
- 足迹：`data/processed/assets/sar/jiulong_s1_20240917_footprint.geojson`
- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）：`[[108.5452654, 22.1943961], [108.8981159, 22.199335], [108.9021711, 21.9248716], [108.5499998, 21.92]]`
- 来源：Sentinel-1A IW GRDH；`S1A_IW_GRDH_1SDV_20240917T105023_20240917T105048_055704_06CD90_3527`
  - 本地栅格：`D:/NWKYYSJZX/2026年工作/1023 久隆 贵港 宁明 二维水动力模拟/2d_hydro_project/data/processed/s1_water/jiulong_20240917T105023_water20m.tif`（180.8 KiB，EPSG:32649，[20.0, 20.0] m，nodata=255.0，sha256 `3ce3abfb25216ba6…`）
- 采集时间（北京）：2024-09-17T18:50:23+08:00；UTC：2024-09-17T10:50:23Z；依据：文件名 UTC 感测开始时刻（父项目 s1_water_extract.py 同口径），北京时间为 UTC+8
- 观测状态：退水后（事后）过境；父项目验证链判定为退水后证据
- 渲染：模型网格 152×182（EPSG:32649，格 200.0 m）；图例 `binary_water`
- 覆盖统计：有效覆盖模型网格 100.0%（27664/27664 格，1106.56 km²）
- 水体：315 个模型格 ≈ 12.6 km²（二值掩膜显示面积，不是水深/淹没面积）
  - 源像元原始水体面积 7.671 km²；frac≥0.5 聚合 2.52 km²
  - 口径提醒：不同聚合阈值下面积差异明显：源像元原始水体 7.671 km²，模型格 frac>0.2 为 12.6 km²，frac≥0.5 为 2.52 km²。只有原始值代表影像水体面积；模型格面积仅作显示参考，不得当淹没面积。
- 足迹：足迹取有效格网外接矩形；外接矩形填充率 1.0；WGS84 包络 [108.5452654, 21.92, 108.9021711, 22.199335]
- 聚合口径：源 20 m/12.5 m 二值水体像元平均到模型格 → 水体分数 > 0.2 判为水体；有效覆盖（源像元多数落在格内）才显示，否则透明
- 源数据覆盖 WGS84：[108.4482388, 21.6955085, 109.0442494, 22.3293486]
- 许可：Copernicus Sentinel Data Legal Notice（免费、开放、需署名；允许复制/传播/修改/组合）
  - 原始数据署名：`Copernicus Sentinel data 2024`
  - 修改后署名：`Contains modified Copernicus Sentinel data 2024`
  - 修改说明：父项目 S1 链 v2 定标与 -17 dB 阈值提取水体，再重采样到模型网格并显示（σ⁰ 后向散射 → 二值水体掩膜）
  - 条款文件：data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf
- 生成命令：`uv run python scripts/prepare_optical.py --layer sar --case jiulong`
- 生成时间 UTC：2026-10-27T09:19:43+00:00
- 其他说明：
  - SAR 不是水深；不可与 depth_classes 深度图例共用。
  - 只有水体像元不透明，其余透明；观测范围由 footprint 表达。
  - 采集时刻与模拟事件不同期，仅作范围/背景证据。

### ningming / optical / `ningming_s2_20260903`

- 状态：**ready**（AOI 有效覆盖 100.0%，有效像元云占比 0.0% → ready）
- 角色：background；标签：可见光背景（Sentinel-2 L2A TCI，2026-09-03）
- 摘要：Sentinel-2 L2A 真彩色，2026-09-03 过境；晚于 2026-08 洪峰，仅作背景，不是洪峰观测
- 备注：采集时间不随模拟时间滑块变化；SCL 云/云影/NoData 透明
- 输出：`data/processed/optical/ningming/ningming_s2_20260903.png`（**1472×1321 px**，**3.97 MiB**，sha256 `dace4d8e5ab21d68…`）
- 尺寸账目：原生裁剪 **2506×2249 px** → 按长边 2048 px 采样 **2048×1838 px** → 压预算落盘 **1472×1321 px**（重采样 average+nearest_scl，二分 4 轮，scale 0.7188，预算 ≤4 MiB，达标 True）
- 元数据：同目录 `.json`（`data/processed/optical/ningming/ningming_s2_20260903.json`）
- 足迹：`data/processed/optical/ningming/ningming_s2_20260903_footprint.geojson`
- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）：`[[106.9600428, 22.2630731], [107.2031128, 22.2599578], [107.1999528, 22.0569492], [106.9572309, 22.0600332]]`
- 来源场景：`S2A_48QYK_20260903_0_L2A`（AWS Open Data Sentinel-2 L2A COG，匿名 HTTPS）
  - TCI：https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/48/Q/YK/2026/9/S2A_48QYK_20260903_0_L2A/TCI.tif
  - SCL：https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/48/Q/YK/2026/9/S2A_48QYK_20260903_0_L2A/SCL.tif
  - STAC：https://earth-search.aws.element84.com/v1/collections/sentinel-s2-l2a/items/S2A_48QYK_20260903_0_L2A
  - 原始分辨率：TCI 10.0 m，SCL 20.0 m
- 采集时间（北京）：2026-09-03T11:32:14+08:00；UTC：2026-09-03T03:32:14.867Z；依据：STAC 条目的 datetime（UTC），北京时间为 UTC+8
- 裁剪范围 WGS84 `[w,s,e,n]`：[106.9572309, 22.0569492, 107.2031128, 22.2630731]
- 遮罩：SCL 类别 [0, 3, 8, 9, 10] 置透明（NoData/云影/云）；透明像元占显示图 0.0%
- 覆盖统计（20 m SCL 全分辨率、AOI 多边形掩膜）：有效覆盖 100.0%，云 0.0%、云影 0.0%（均占有效像元），NoData 0.0%（占 AOI）
  - 交叉核对 `valid_coverage_percent`：实测 100.0 vs 检索报告 100.0（Δ 0.0，容差 1.0）→ 通过
  - 交叉核对 `cloud_percent_of_valid`：实测 0.0 vs 检索报告 0.0（Δ 0.0，容差 1.0）→ 通过
  - 交叉核对 `cloud_shadow_percent_of_valid`：实测 0.0 vs 检索报告 0.0（Δ 0.0，容差 1.0）→ 通过
- 检索证据：`plans/宁明_贵港_公开可见光影像检索结果.md`、`plans/visualization_research/optical_aoi_quality.json`
- 许可：Copernicus Sentinel Data Legal Notice（免费、开放、需署名；允许复制/传播/修改/组合）
  - 原始数据署名：`Copernicus Sentinel data 2026`
  - 修改后署名：`Contains modified Copernicus Sentinel data 2026`
  - 修改说明：裁剪到模型 AOI、重采样显示尺寸、按 SCL 云/云影/NoData 置透明（RGB 值未做拉伸）
  - 条款文件：data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf
- 生成命令：`uv run python scripts/prepare_optical.py --layer optical --case ningming`
- 生成时间 UTC：2026-10-27T09:20:05+00:00
- 其他说明：
  - 采集时间不随模拟时间滑块变化；本景不是洪峰同期观测。
  - SCL 为自动分类，不等同人工云检测；未对残留薄云做人工剔除。
  - PNG 是裁剪后的显示图，原始整景未入库。

### ningming / sar / `ningming_gf3_20260824`

- 状态：**ready**（覆盖模型网格 100.0%（54656/54656 格）→ ready）
- 角色：sar_observation；标签：SAR 水体（GF-3 2026-08-24 近洪峰，二值掩膜）
- 摘要：GF-3 8/24 近洪峰水体掩膜（含常年水体），栅格化到 12.5 m 后渲染到模型网格；不是水深
- 备注：SAR 不是水深，不能共用深度图例；采集时刻与模拟事件不同期，仅范围证据
- 输出：`data/processed/assets/sar/ningming_gf3_20260824.png`（**244×224 px**，**2.9 KiB**，sha256 `42418dd07253b93e…`）
- 元数据：同目录 `.json`（`data/processed/assets/sar/ningming_gf3_20260824.json`）
- 足迹：`data/processed/assets/sar/ningming_gf3_20260824_footprint.geojson`
- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）：`[[106.9628054, 22.2622514], [107.19947, 22.2592189], [107.1963275, 22.0569981], [106.9600004, 22.0600002]]`
- 来源：GF-3（高分三号）SAR 水体掩膜（本地供方验证数据）；`GF3_8月24日水体_宁明县（本地 shapefile 栅格化）`
  - 本地栅格：`D:/NWKYYSJZX/2026年工作/1023 久隆 贵港 宁明 二维水动力模拟/2d_hydro_project/data/interim/ningming/water_0824.tif`（784.7 KiB，EPSG:32648，[12.50034791578512, 12.50207389259571] m，nodata=None，sha256 `dd46f149a32d8a9a…`）
- 采集时间（北京）：2026-08-24（仅日期；精确过境时刻未核实，父项目登记 time_uncertainty_h=6）；UTC：None；依据：本地数据仅给出日期；父项目 gaps_risks G23 登记时刻不确定 ±6 h
- 观测状态：近洪峰；父项目主口径取 78 h 帧（8/24 08:00 中心 ±6 h）
- 渲染：模型网格 224×244（EPSG:32648，格 100.0 m）；图例 `binary_water`
- 覆盖统计：有效覆盖模型网格 100.0%（54656/54656 格，546.56 km²）
- 水体：6621 个模型格 ≈ 66.21 km²（二值掩膜显示面积，不是水深/淹没面积）
  - 源像元原始水体面积 49.453 km²；frac≥0.5 聚合 50.41 km²
  - 口径提醒：不同聚合阈值下面积差异明显：源像元原始水体 49.453 km²，模型格 frac>0.2 为 66.21 km²，frac≥0.5 为 50.41 km²。只有原始值代表影像水体面积；模型格面积仅作显示参考，不得当淹没面积。
- 足迹：足迹取有效格网外接矩形；外接矩形填充率 1.0；WGS84 包络 [106.9600004, 22.0569981, 107.19947, 22.2622514]
- 聚合口径：源 20 m/12.5 m 二值水体像元平均到模型格 → 水体分数 > 0.2 判为水体；有效覆盖（源像元多数落在格内）才显示，否则透明
- 源数据覆盖 WGS84：[106.8736875, 21.9220559, 107.6289842, 22.478049]
- 许可：未核实（GF-3 本地供方数据；父项目 compliance_checklist 登记授权待确认）
  - 原始数据署名：`GF-3（高分三号）数据：本地供方提供，署名待核实`
  - 修改后署名：`GF-3（高分三号）水体掩膜：本地供方提供并栅格化，署名待核实`
  - 修改说明：provider 水体 shapefile 栅格化到 12.5 m，再重采样到模型网格并显示
  - 条款文件：无（授权未核实）
- 生成命令：`uv run python scripts/prepare_optical.py --layer sar --case ningming`
- 生成时间 UTC：2026-10-27T09:20:05+00:00
- 其他说明：
  - SAR 不是水深；不可与 depth_classes 深度图例共用。
  - 只有水体像元不透明，其余透明；观测范围由 footprint 表达。
  - 采集时刻与模拟事件不同期，仅作范围/背景证据。

### guigang / optical / `guigang_s2_20241003`

- 状态：**partial**（AOI 有效覆盖 96.8428%，有效像元云占比 0.0005% → partial（存在 NoData/云空缺））
- 角色：background；标签：可见光背景（Sentinel-2 L2A TCI，2024-10-03）
- 摘要：Sentinel-2 L2A 真彩色，2024-10-03 过境；洪峰（09-12）后约 3 周，属事件后背景，不是洪峰观测
- 备注：AOI 内约 3.16% NoData 透明空缺；不得宣称整域覆盖
- 输出：`data/processed/optical/guigang/guigang_s2_20241003.png`（**1792×1025 px**，**3.93 MiB**，sha256 `4151858e053b1480…`）
- 尺寸账目：原生裁剪 **6691×3827 px** → 按长边 2048 px 采样 **2048×1171 px** → 压预算落盘 **1792×1025 px**（重采样 average+nearest_scl，二分 4 轮，scale 0.875，预算 ≤4 MiB，达标 True）
- 元数据：同目录 `.json`（`data/processed/optical/guigang/guigang_s2_20241003.json`）
- 足迹：`data/processed/optical/guigang/guigang_s2_20241003_footprint.geojson`
- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）：`[[109.295705, 23.3199533], [109.9499552, 23.3257043], [109.9526458, 22.9800175], [109.3000706, 22.9743614]]`
- 来源场景：`S2B_49QCF_20241003_0_L2A`（AWS Open Data Sentinel-2 L2A COG，匿名 HTTPS）
  - TCI：https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/49/Q/CF/2024/10/S2B_49QCF_20241003_0_L2A/TCI.tif
  - SCL：https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/49/Q/CF/2024/10/S2B_49QCF_20241003_0_L2A/SCL.tif
  - STAC：https://earth-search.aws.element84.com/v1/collections/sentinel-s2-l2a/items/S2B_49QCF_20241003_0_L2A
  - 原始分辨率：TCI 10.0 m，SCL 20.0 m
- 采集时间（北京）：2024-10-03T11:21:43+08:00；UTC：2024-10-03T03:21:43.556Z；依据：STAC 条目的 datetime（UTC），北京时间为 UTC+8
- 裁剪范围 WGS84 `[w,s,e,n]`：[109.295705, 22.9743614, 109.9526458, 23.3257043]
- 遮罩：SCL 类别 [0, 3, 8, 9, 10] 置透明（NoData/云影/云）；透明像元占显示图 3.3835%
- 覆盖统计（20 m SCL 全分辨率、AOI 多边形掩膜）：有效覆盖 96.8428%，云 0.0005%、云影 0.0%（均占有效像元），NoData 3.1572%（占 AOI）
  - 交叉核对 `valid_coverage_percent`：实测 96.8428 vs 检索报告 96.8428（Δ 0.0，容差 1.0）→ 通过
  - 交叉核对 `cloud_percent_of_valid`：实测 0.0005 vs 检索报告 0.0005（Δ 0.0，容差 1.0）→ 通过
  - 交叉核对 `cloud_shadow_percent_of_valid`：实测 0.0 vs 检索报告 0.0（Δ 0.0，容差 1.0）→ 通过
- 检索证据：`plans/宁明_贵港_公开可见光影像检索结果.md`、`plans/visualization_research/optical_aoi_quality.json`
- 许可：Copernicus Sentinel Data Legal Notice（免费、开放、需署名；允许复制/传播/修改/组合）
  - 原始数据署名：`Copernicus Sentinel data 2024`
  - 修改后署名：`Contains modified Copernicus Sentinel data 2024`
  - 修改说明：裁剪到模型 AOI、重采样显示尺寸、按 SCL 云/云影/NoData 置透明（RGB 值未做拉伸）
  - 条款文件：data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf
- 生成命令：`uv run python scripts/prepare_optical.py --layer optical --case guigang`
- 生成时间 UTC：2026-10-27T09:20:29+00:00
- 其他说明：
  - 采集时间不随模拟时间滑块变化；本景不是洪峰同期观测。
  - SCL 为自动分类，不等同人工云检测；未对残留薄云做人工剔除。
  - PNG 是裁剪后的显示图，原始整景未入库。

### guigang / sar / `guigang_s1_20240912`

- 状态：**partial**（覆盖模型网格 20.64%（567518/2749590 格）→ partial）
- 角色：sar_observation；标签：SAR 水体（S1 2024-09-12 洪峰期，二值掩膜）
- 摘要：S1 20 m 二值水体掩膜（σ⁰_VV -17 dB，父项目 v2），仅覆盖模型域东部一部分（约两成面积），必须显示足迹
- 备注：SAR 不是水深，不能共用深度图例；采集时刻与模拟事件不同期，仅范围证据
- 输出：`data/processed/assets/sar/guigang_s1_20240912.png`（**2230×1233 px**，**21.1 KiB**，sha256 `dfa96e7e65783e85…`）
- 元数据：同目录 `.json`（`data/processed/assets/sar/guigang_s1_20240912.json`）
- 足迹：`data/processed/assets/sar/guigang_s1_20240912_footprint.geojson`
- 四角经纬度 `[[lon,lat]×4]`（左上、右上、右下、左下）：`[[109.2957815, 23.3139931], [109.949927, 23.3197419], [109.9525275, 22.9856574], [109.3000005, 22.9800002]]`
- 来源：Sentinel-1A IW GRDH；`S1A_IW_GRDH_1SDV_20240912T104222_20240912T104247_055638_06CAF6_AE7D`
  - 本地栅格：`D:/NWKYYSJZX/2026年工作/1023 久隆 贵港 宁明 二维水动力模拟/2d_hydro_project/data/processed/s1_water/guigang_20240912T104222_water20m.tif`（146.7 KiB，EPSG:32649，[20.0, 20.0] m，nodata=255.0，sha256 `573c32d25267bf07…`）
- 采集时间（北京）：2024-09-12T18:42:22+08:00；UTC：2024-09-12T10:42:22Z；依据：文件名 UTC 感测开始时刻，北京时间为 UTC+8
- 观测状态：洪峰期（略早于站峰 09-12T14）
- 渲染：模型网格 1233×2230（EPSG:32649，格 30.0 m）；图例 `binary_water`
- 覆盖统计：有效覆盖模型网格 20.64%（567518/2749590 格，510.766 km²）
- 水体：14801 个模型格 ≈ 13.321 km²（二值掩膜显示面积，不是水深/淹没面积）
  - 源像元原始水体面积 11.23 km²；frac≥0.5 聚合 11.022 km²
  - 口径提醒：不同聚合阈值下面积差异明显：源像元原始水体 11.23 km²，模型格 frac>0.2 为 13.321 km²，frac≥0.5 为 11.022 km²。只有原始值代表影像水体面积；模型格面积仅作显示参考，不得当淹没面积。
- 足迹：足迹取有效格网外接矩形，内部可能仍有空洞（填充率见 coverage.footprint_fill_ratio_of_bbox）；外接矩形填充率 0.8005；WGS84 包络 [109.781238, 22.9844556, 109.9525275, 23.3197419]
- 聚合口径：源 20 m/12.5 m 二值水体像元平均到模型格 → 水体分数 > 0.2 判为水体；有效覆盖（源像元多数落在格内）才显示，否则透明
- 源数据覆盖 WGS84：[109.741601, 22.7602279, 110.2692467, 23.5547057]
- 许可：Copernicus Sentinel Data Legal Notice（免费、开放、需署名；允许复制/传播/修改/组合）
  - 原始数据署名：`Copernicus Sentinel data 2024`
  - 修改后署名：`Contains modified Copernicus Sentinel data 2024`
  - 修改说明：父项目 S1 链 v2 定标与 -17 dB 阈值提取水体，再重采样到模型网格并显示（σ⁰ 后向散射 → 二值水体掩膜）
  - 条款文件：data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf
- 生成命令：`uv run python scripts/prepare_optical.py --layer sar --case guigang`
- 生成时间 UTC：2026-10-27T09:20:29+00:00
- 其他说明：
  - SAR 不是水深；不可与 depth_classes 深度图例共用。
  - 只有水体像元不透明，其余透明；观测范围由 footprint 表达。
  - 采集时刻与模拟事件不同期，仅作范围/背景证据。
  - 部分覆盖模型域，必须显示足迹，不宣称全覆盖。

## 3. 失败资产（failed）

本次运行无失败资产。

## 4. 未启用 / 禁用资产

| 案例 | asset_id | 状态 | 路径 | 原因 |
|---|---|---|---|---|
| jiulong | `jiulong_local_sar_after_unknown` | local_unchecked | `D:/NWKYYSJZX/2026年工作/0731 五省区DEM/淹没分析 7.8/data/灾后SAR_久隆区域.tif` | 传感器/日期/处理链未核实；不得进入发布包，不得标 ready |

- `灾后SAR_久隆区域.tif`：传感器/日期/处理链未核实 → `local_unchecked`，**禁用**；本子项目不使用，也不复制进发布包。
- 检索报告中的其它候选景（宁明 2026-08-12、贵港 2024-10-31 / 09-13 / 09-03）未导入；其中宁明 08-12 景 AOI 云占比 31.48%，不能按整景低云采用。
- `宁明县8月24日SAR.tif`（原始 GF-3 栅格）未直接使用；本次使用父项目栅格化的二值水体掩膜 `data/interim/ningming/water_0824.tif`。`water_new_0824.tif` 是 8/24 新增淹没层（剔除 8/6 常水），本次未用，仅登记为备选。

## 5. 状态机与判定口径

| 状态 | 含义 | 本次实际用例 |
|---|---|---|
| `remote_verified` | 远端可读、未本地化 | 本次无：远程 S2 场景已裁成本地 PNG |
| `local_unchecked` | 本地有数据但未通过质检 | 无 |
| `ready` | 通过发布质检且覆盖达标 | jiulong/sar/`jiulong_s1_20240917`、ningming/optical/`ningming_s2_20260903`、ningming/sar/`ningming_gf3_20260824` |
| `partial` | 通过质检但覆盖不全 | jiulong/optical/`jiulong_rgb_20260403`、guigang/optical/`guigang_s2_20241003`、guigang/sar/`guigang_s1_20240912` |
| `missing` | 未提供 | 无 |
| `failed` | 处理失败 | 无 |

判定规则（脚本内固定）：

- 光学：全分辨率 20 m SCL 按 AOI 多边形统计，有效覆盖 ≥99.5% 且有效像元云占比 ≤1.0% → `ready`，否则 `partial`。
- 光学（本地影像）：显示图有效像元重投影到模型网格，覆盖 ≥99.5% → `ready`，否则 `partial`。
- SAR：重投影到模型网格后有效观测格网占比 ≥99.5% → `ready`，否则 `partial`（必须显示足迹）。
- 云/云影/NoData：SCL 类别 0/3/8/9/10 在显示图中 alpha=0；不做平滑填充冒充观测。

## 6. 许可与署名

- Sentinel（S2 光学、S1 SAR）：Copernicus Sentinel Data Legal Notice，免费开放，允许复制/传播/修改/组合。
  - 条款 PDF：[Sentinel_Data_Legal_Notice.pdf](../data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf)（sha256 `fa2955ff48a1d82e…`）。
- 原始数据公开传播署名：`Copernicus Sentinel data [Year]`；
- 本子项目产物为裁剪/重采样/遮罩后的修改版，署名：`Contains modified Copernicus Sentinel data [Year]`（宁明 2026，贵港/久隆 2024）。
- GF-3（宁明 SAR）与本地 RGB（久隆光学）：授权未核实，仅限内部研究展示；公开再分发前必须补齐授权确认。

## 7. 复现命令

```powershell
cd 2d_hydro_project/flood_visualization
$env:PYTHONUTF8='1'
uv run python scripts/prepare_optical.py --force              # 全量重建
uv run python scripts/prepare_optical.py --layer optical      # 只做光学
uv run python scripts/prepare_optical.py --layer sar          # 只做 SAR
uv run python scripts/prepare_optical.py --download           # 远程读取失败时整景下载兜底
```

依赖：venv 内 numpy / rasterio / pyproj / pillow（见 `pyproject.toml`）。远程 S2 场景约 186–319 MB/景，脚本只做 COG 窗口读取；**整景不入库**，仅失败兜底时下载到系统临时目录，用完删除。

## 8. 已知限制与风险

1. SCL 是自动分类，未做人工云判读；残留薄云/云影可能仍在图中。
2. 贵港光学约 3.16% NoData 空缺透明；不得宣称整域覆盖，也不得把 2024-10 影像当洪峰观测。
3. 贵港 SAR 只覆盖模型域东部一部分；页面必须显示足迹，不得宣称全市覆盖。
4. 久隆光学为 2026-04 本地影像，非 2024 事件同期，且仅覆盖部分模型域；来源/许可未核实。
5. 宁明 GF-3 SAR 精确过境时刻未核实（父项目登记 ±6 h）；GF-3 授权未核实。
6. SAR 二值水体掩膜不是水深/淹没深度，不能与深度图例共用；水体面积只作范围参考。
7. 远程读取为通过本机代理的匿名 HTTPS（关闭证书校验，原因见脚本文档串）；已用检索报告的 SCL 统计交叉核对，差异见各资产 `qa.cross_check`。
8. 未执行人工目视验收（`manual_visual_review` 未完成）；`ready` 仅表示自动质检与覆盖门禁通过。
