# flood_visualization 实施契约（Lead 维护，协作唯一依据）

> 目标：把 `plans/广西久隆_宁明_贵港_二维洪涝可视化实现计划.md` 的 M0–M3 完全落地：
> 本地 HTML 入口 + 静态资源包 + 回环 HTTP 服务，读取真实逐帧水深与时间元数据，
> 三地切换、多源叠加、时间轴回放、像元数值查询、面积曲线、质量与来源面板。
>
> 只读父项目 `2d_hydro_project/` 的模型输出与数据；不重跑模型；不改 DSH 服务；
> 不启动除交付用 `127.0.0.1:8765` 以外的新服务（开发期可用其它回环端口）。

## 0. 团队写作用域（避免冲突）

| owner | 写作用域（相对 `2d_hydro_project/flood_visualization/`） |
|---|---|
| lead | 其余全部：根目录元文件、`configs/`、`schemas/`、`docs/`、`scripts/{fetch_vendor,build_web,start_local}`、`src/floodviz/{__init__,__main__,cli,config}.py`、`src/floodviz/catalog/`、`src/floodviz/raster/grid.py`、`src/floodviz/quality/frames.py`、`src/floodviz/publishing/`、`web/vendor/`、`data/README.md` |
| pipeline | `src/floodviz/raster/{__init__,blocks,tiles,warp}.py`、`src/floodviz/quality/{__init__,stats,validate}.py`、`src/floodviz/pipeline.py`、`data/interim/**`、`data/processed/**`（`data/processed/optical/**` 与 `data/processed/assets/**` 除外）、`outputs/**` |
| web | `web/**`（`web/vendor/**` 除外） |
| optical | `scripts/prepare_optical.py`、`data/processed/optical/**`、`data/processed/assets/**`、`data/external/optical/**`、`docs/optical_assets.md` |
| qa | `tests/**`、`.github/workflows/ci.yml`、`docs/acceptance_report.md`、`docs/runbook.md` |

不要编辑别人作用域内的文件；发现契约问题在汇报里提出，由 lead 改。

## 1. 运行方式

统一用子项目隔离环境（venv 由 lead 用 uv 建好）：

```powershell
$env:PYTHONUTF8='1'
uv --directory './2d_hydro_project/flood_visualization' run python -m floodviz --help
uv --directory './2d_hydro_project/flood_visualization' run python -m pytest -q
```

父项目 Python 环境只读参考：`2d_hydro_project/.venv/Scripts/python.exe`（numpy 2.5.4 / rasterio 1.5.2 / pyproj 3.8.0 / pyyaml 6.0.3），不得修改父项目 `pyproject.toml`/`uv.lock`。

## 2. 数据来源（只读）

| 案例 | run（默认） | 帧数 | 源目录（相对任务根，父项目下） |
|---|---|---:|---|
| jiulong | `scen_free_200m_sv6h` | 21 | `outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h` |
| jiulong 可选 | `scen_free_200m_sv6h_recession` | 53 | `outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h_recession` |
| ningming | `scen_q6000_n100_100m` | 19 | `outputs/ningming/2026-08-zitan/scen_q6000_n100_100m` |
| ningming 对照 | `scen_q4000_100m` | 19 | `outputs/ningming/2026-08-zitan/scen_q4000_100m` |
| guigang | `scen_yagi_q11500_30m_b5` | 57 | `outputs/guigang/2024-09-yagi/scen_yagi_q11500_30m_b5` |
| guigang 对照 | `scen_yagi_q11500_200m` | 57 | `outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m` |

每 run 目录含 `meta.json`、`run.par`、`results/res-XXXX.wd`（ESRI ASCII 水深网格，6 行头 + 行优先数值，首行=北）。
缺 `res-XXXX.wd`、序号不连续、头不一致、末帧超模拟结束时长 → **拒绝发布**（不是警告）。

时间起点（北京时间候选，来源依据见父项目脚本/docs）：
- jiulong：`2024-09-05T00:00:00+08:00`；旧 meta 无规范 t0 → `origin_status: "relative_only_candidate"`，UI 同时显示 T+小时与日期但标注“起点依据候选”；
- ningming：`2026-08-21T00:00:00+08:00`（上游 Q 三角线起点）→ `origin_status: "relative_only_candidate"`；
- guigang：`2024-09-03T00:00:00+08:00` → `origin_status: "model_schedule_confirmed"`（meta 有 t0）。

模型时间：`sim_seconds = source_sequence * save_interval_s`，但实现必须逐帧写显式 `sim_seconds`；不得用文件时间/目录名。
每帧面积：原生网格有效像元（非 NaN、非海域 mask）`depth > threshold` 的像元数 × 格面积。

质量门：读父项目 `data/catalog/mass_recheck_g15.json`（GBK/UTF-8 容错），按 run 路径映射 `gate_1e-3`、`e_mass_132`；无记录 → `unrecorded`，UI 不得显示 PASS。总体现状 **66 PASS / 4 FAIL-EXPLAINED / 0 FAIL**（70 run，以该 JSON 为权威），但页面只展示所选 run 的状态。

## 3. 发布包目录契约（`outputs/web_bundle/<bundle_id>/`）

```text
index.html                    # 复制自 web/index.html（构建器写入 <base href> 不需要，全部相对路径）
assets/styles.css
assets/app.js  assets/map.js  assets/timeline.js
assets/vendor/...             # 本地 MapLibre / Proj4js + LICENSE，禁止运行时 CDN
catalog.json                  # 唯一入口元数据（≤1 MB/案例目标）
cases/<case_id>/<run_id>/timeline.json
cases/<case_id>/<run_id>/statistics.json
cases/<case_id>/boundary.geojson        # 模型域外框（WGS84 Polygon）
cases/<case_id>/sites.geojson           # 站点/参考点（WGS84 Point，properties.name/label）
cases/<case_id>/sar_footprint.geojson   # 若有 SAR：实际数据覆盖足迹
cases/<case_id>/optical_footprint_<asset_id>.geojson
frames/<case_id>/<run_id>/<frame_id>.png                 # 水深（预着色 RGBA，原生网格 1:1）
frames/<case_id>/<run_id>/max.png                        # 全程最大包络
frames/<case_id>/dem.png                                 # 地形（预着色，透明度由 UI 控）
frames/<case_id>/sar.png                                 # SAR 层（水体掩膜或后向散射）
frames/<case_id>/optical/<asset_id>.png                  # 可见光（遮云/无数据透明）
frames/<case_id>/<run_id>/images.json                    # 每图：尺寸 + 四角经纬度 + 网格 transform + bbox
numeric/<case_id>/<run_id>/manifest.json
numeric/<case_id>/<run_id>/mask.png                      # 1-bit PNG；白=有效像元
numeric/<case_id>/<run_id>/<frame_id>/<block_y>/<block_x>.bin   # float32 LE，块内 C 序
numeric/<case_id>/<run_id>/max/<block_y>/<block_x>.bin
licenses/LICENSES.md  licenses/*.txt
manifest.json                 # 内容哈希、构建参数、质量摘要、created_utc
```

ZIP 内所有 URL 一律为相对路径；`https://` 只允许 Esri 底图（可在离线时失败降级）。

### 3.1 显示图（**用户 2026-10-27 决定：不逐帧生成 XYZ 瓦片金字塔**）

> 变更依据：贵港 30 m 有 57 帧、全 run ASCII 合计约 940 MB（**单帧约 16.5 MB，940 MB 是 57 帧总和**），
> 逐帧逐缩放层级建 XYZ 会放大到数十 GB 且耗时不可控，违背 ≤10 GB 预算。
> 改为：**直接复用已有 6 h 间隔的真实推演帧**，每帧渲染为 1～N 张原生网格 1:1 的预着色 PNG（子图），
> 用 MapLibre `image` source 按四角经纬度贴图。不插值、不伪造中间时刻、不做多层级倍增。
>
> **子图切分实测（pipeline 2026-10-27）**：久隆 1×1 0.048 px、宁明 1×1 0.042 px、
> 贵港 200 m 1×1 0.180 px、**贵港 30 m 1×2（沿列对半）0.332 px**；门禁 0.5 源像元，不放宽。
> DEM 与 max 包络同样按该 run 的 split 切分（`dem_r<r>c<c>.png`、`max_r<r>c<c>.png`、`dem_images.json`）。

- 每帧渲染为 **1～N 张 RGBA PNG 子图（parts）**，每个 part 的行 0 = 最北行，pixel-is-area。
  切割只许沿整像元边界，不重采样、不插值；part 之间无缝、无重叠、并集严格等于整幅网格。
- **子图数量自适应（硬门禁，不可放宽）**：从 1×1 起，若“像元中心 → 经纬度 → 四角双线性反贴图”
  的抽样残差 > **0.5 源像元**，则按列对半（1×2、1×4…）与按行对半（2×2…）继续拆直到**每个 part** 都达标；
  采用满足门禁的**最少子图数**。实测写入 `manifest.json.validation.spatial` 与 `docs/build_report.md`。
  背景：贵港 30 m（2230×1233）单张 1.261 px 超限，2×2 后 0.313 px 达标；久隆/宁明 1×1 约 0.05 px。
- 贴图坐标：每个 part 的四个外角（左上、右上、右下、左下）用 pyproj 从模型 CRS 变换到 WGS84，
  写入 `images.json` 的 `coordinates: [[lon,lat]×4]`（pixel-is-area 网格外角）。
- 水深层固定：`depth <= 0.05 m` 或 NaN → 透明；其余按 `color_scale.classes` 预着色（固定色带）。
- DEM 层：连续色带，NaN 透明；采样用最近邻（重采样到模型网格），不做双线性“造高”。
- SAR/光学层：无观测或遮云像元 alpha=0；光学不叠滤镜。
- 光学等非模型网格图层可独立尺寸，但最大边 ≤4096 px（web 纹理上限），四角坐标按实际覆盖计算。
- 图像不得由“最大水深 + 帧号”伪造；每帧来自对应 `res-XXXX.wd`。
- `max` 取自 run 目录 `res.max`（模型自带的全程最大包络），同样按 parts 切割；
  启用时暂停时间回放并显示不同标题。
- 体积预算：单个 part ≤2 MB；全包 ≤10 GB（实测写入 `docs/build_report.md`）。
- `images.json` 契约：

```json
{"schema_version":"1.0","case_id":"guigang","run_id":"scen_yagi_q11500_30m_b5","kind":"depth_frames",
 "grid":{"crs":"EPSG:32649","rows":1233,"cols":2230,"transform":[30,0,325737.9,0,-30,2579305.3],
         "cell_size_m":30.0,"cell_area_m2":900.0},
 "color_scale":{"transparent_below_m":0.05,"classes":[{"min_m":0.05,"max_m":0.15,"color":"#b7e4c7","label":"0.05–0.15 m"}]},
 "split":{"rows":2,"cols":2},
 "frames":[{"frame_id":"f0000","max_depth_m":0.0,"wet_pixels":0,
   "parts":[{"id":"f0000_r0c0","row":0,"col":0,
             "path":"frames/guigang/scen_yagi_q11500_30m_b5/f0000_r0c0.png",
             "width":1115,"height":617,"coordinates":[[108.70,23.20],[108.98,23.20],[108.98,23.05],[108.70,23.05]],
             "max_depth_m":0.0,"wet_pixels":0}]}],
 "max":{"max_depth_m":19.44,"wet_pixels":338000,
   "parts":[{"id":"max_r0c0","row":0,"col":0,"path":"frames/guigang/scen_yagi_q11500_30m_b5/max_r0c0.png",
             "width":1115,"height":617,"coordinates":[[108.70,23.20],[108.98,23.20],[108.98,23.05],[108.70,23.05]]}]}}
```

- 文件名约定：帧 `<frame_id>_r<row>c<col>.png`，包络 `max_r<row>c<col>.png`；即使 `split` 为 1×1
  也保留 `_r0c0` 后缀（命名统一，便于反查）。`parts[].id` 全文件唯一。
- `timeline.frames[].depth_image` 只是**逻辑 id**（`frames/<case>/<run>/<frame_id>.png`），
  真实文件以 `images.json` 的 `parts[].path` 为准；validate 只校验 parts 文件齐全。

### 3.2 数值块（保真层）

- `manifest.json` 的 `grid` 为 `crs`、`rows`、`cols`、`transform`（rasterio/Affine 顺序 `[a,b,c,d,e,f]`，北向上：`d<0`，`c/f` 为左上角坐标）。
- 像元中心：`x=a*(col+0.5)+b*(row+0.5)+c`，`y=d*(col+0.5)+e*(row+0.5)+f`；行 0 = 最北行。
- 块大小 256；边缘块按实际裁剪；块内 C 序；`float32` 小端；NaN = 无效/NoData；干地 = 0。
- 客户端查询：lon/lat → proj4 到 `crs` → 逆仿射 → `row=floor, col=floor`；越界返回“域外”，不夹边；读块 `numeric/<case>/<run>/<frame_id>/<block_y>/<block_x>.bin`，`value=(row-by*256)*bw+(col-bx*256)`；NaN 显示“无有效值”。
- 查询值必须与源 `.wd` 逐位一致（float32 化后）；`validate` 抽样断言 ≤ max(1e-5, 1e-6*|src|)。

## 4. `catalog.json`（结构见 `docs/examples/catalog.example.json` 与 `schemas/catalog.schema.json`）

关键字段（必须全有）：

```json
{
  "schema_version": "1.0",
  "bundle_id": "v3-20261028",
  "created_utc": "2026-10-27T10:00:00+00:00",
  "source_manifest_hash": "sha256:<hex>",
  "license_notice": "研究性情景成果，非业务预警；各图层授权见 licenses/LICENSES.md",
  "default_case": "jiulong",
  "display": {
    "project_title": "广西久隆·宁明·贵港 二维洪涝可视化",
    "subtitle": "研究性情景成果（降级模式），非业务预警",
    "default_threshold_m": 0.05,
    "thresholds_m": [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0],
    "depth_classes": [{"min_m":0.05,"max_m":0.15,"color":"#b7e4c7","label":"0.05–0.15 m"}, "...", {"min_m":10.0,"max_m":null,"color":"#3f0071","label":"≥10 m"}],
    "area_threshold_note": "面积为原生网格单帧有效像元 depth>阈值 的面积；不是资产安全等级",
    "temporal_note": "6 h/帧离散回放；播放速度不改变物理时间分辨率"
  },
  "basemaps": [
    {"id":"esri_world_imagery","label":"Esri World Imagery（在线）","tiles":["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],"tile_size":256,"attribution":"Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community","requires_network":true},
    {"id":"esri_world_street","label":"Esri World Street Map（在线）","tiles":["https://services.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"],"tile_size":256,"attribution":"...","requires_network":true}
  ],
  "cases": [{
    "case_id":"jiulong","name":"久隆","event_id":"2024-09-yagi",
    "event_name":"台风摩羯残余环流（2024-09）","scenario_role":"研究性情景（非独立验证）",
    "subtitle":"200 m 降级情景 · 21 帧 · 6 h/帧",
    "model_bbox":[108.66,21.94,108.99,22.22],
    "grid":{"crs":"EPSG:32649","rows":152,"cols":182,"transform":[200.0,0.0,246911.3,0.0,-200.0,2456393.0],"cell_size_m":200.0,"cell_area_m2":40000.0},
    "time":{"time_origin":"2024-09-05T00:00:00+08:00","time_zone":"Asia/Shanghai","origin_status":"relative_only_candidate","origin_note":"旧 meta 无规范 t0，按父项目脚本候选值，仅显示用"},
    "default_run_id":"scen_free_200m_sv6h",
    "runs":[{"run_id":"scen_free_200m_sv6h","label":"默认情景（T+0–120 h）","role":"default","frame_count":21,"save_interval_s":21600,"simulation_duration_s":432000,"timeline":"cases/jiulong/scen_free_200m_sv6h/timeline.json","statistics":"cases/jiulong/scen_free_200m_sv6h/statistics.json","numeric_manifest":"numeric/jiulong/scen_free_200m_sv6h/manifest.json","quality":{"mass_gate":"PASS","e_mass":0.0004893,"validation_status":"降级模式；无独立水文验证","vertical_datum_status":"未闭合","warnings":["..."]}}],
    "layers":{
      "flood":{"status":"ready","label":"模型水深（逐帧）","kind":"frame","images":"frames/jiulong/scen_free_200m_sv6h/images.json"},
      "max":{"status":"ready","label":"全程最大水深包络","kind":"max_envelope","image":"frames/jiulong/scen_free_200m_sv6h/max.png","geometry":{"rows":152,"cols":182,"transform":[200.0,0.0,246911.3,0.0,-200.0,2456393.0],"crs":"EPSG:32649","coordinates":[[108.6572,22.2054],[108.9996,22.1806],[108.9482,21.9433],[108.6283,21.9677]]}},
      "dem":{"status":"ready","label":"DEM 高程","kind":"raster","image":"frames/jiulong/dem.png","value_min_m":0.0,"value_max_m":300.0,"legend":"terrain","note":"高程为模型输入 DEM；垂直基准未闭合"},
      "sar":{"status":"partial|ready|missing|local_unchecked|remote_verified|failed","label":"SAR 水体","asset_id":"...","acquired_at":"...","summary":"...","footprint":"cases/jiulong/sar_footprint.geojson","image":"frames/jiulong/sar.png","legend":"binary_water","note":"SAR 不是水深，不能共用深度图例"},
      "optical":{"status":"...","label":"可见光背景","asset_id":"...","acquired_at":"...","summary":"...","footprint":"...","image":"frames/<case>/optical/<asset_id>.png","role":"background|observation","note":"采集时间不随模拟时间滑块变化"},
      "boundary":{"status":"ready","geojson":"cases/jiulong/boundary.geojson"},
      "sites":{"status":"ready","geojson":"cases/jiulong/sites.geojson"}
    },
    "quality":{"mass_gate":"PASS","e_mass":0.0004893,"validation_status":"...","vertical_datum_status":"未闭合","warnings":["情景级成果；无业务预警资质","..."]},
    "sources":{"model":"LISFLOOD-FP 8.1.0","run_id":"...","run_path_hint":"outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h","license":"见 licenses/LICENSES.md","evidence":["plans/visualization_research/frame_audit.json"]}
  }]
}
```

`layers.flood` 不必带 tiles 模板：由 timeline 每帧给出 `depth_tiles`。`max` 为可选图层，独立入口（不是时间帧）；`max` 必须能从 `runs[].numeric_manifest` 之外单独取到。
状态枚举只允许：`remote_verified`、`local_unchecked`、`ready`、`partial`、`missing`、`failed`；仅 `ready/partial` 可勾选；其不可用时必须显示原因（`summary`/`note`）。

## 5. `timeline.json`（每 run 一份，结构见 `docs/examples/timeline.example.json`）

```json
{"schema_version":"1.0","case_id":"guigang","event_id":"2024-09-yagi","run_id":"scen_yagi_q11500_30m_b5",
 "time_origin":"2024-09-03T00:00:00+08:00","time_zone":"Asia/Shanghai","origin_status":"model_schedule_confirmed",
 "save_interval_s":21600,"simulation_duration_s":1209600,"frame_count":57,"temporal_interpolation":"none",
 "statistics":"cases/guigang/scen_yagi_q11500_30m_b5/statistics.json",
 "frames":[{"frame_id":"f0000","source_sequence":0,"sim_seconds":0,"timestamp":"2024-09-03T00:00:00+08:00",
   "depth_image":"frames/guigang/scen_yagi_q11500_30m_b5/f0000.png",
   "query_blocks":"numeric/guigang/scen_yagi_q11500_30m_b5/f0000/{block_y}/{block_x}.bin",
   "sha256":"<res-0000.wd sha256>"}]}
```

`timestamp` 仅当 `time_origin` 有依据时填 ISO8601(+08:00)，否则 `null`；`sim_seconds` 永远为真值。帧严格递增；`frame_count` 与数组长度一致。

## 6. `statistics.json`

```json
{"schema_version":"1.0","case_id":"...","run_id":"...","unit_area":"km2",
 "thresholds_m":[0.05,0.15,...],
 "frames":[{"frame_id":"f0000","sim_seconds":0,"timestamp":null,
            "areas_km2":{"0.05":1.234,"0.15":0.5},"wetted_cells":{"0.05":30}}],
 "max_envelope":{"areas_km2":{"0.05":...},"peak_frame_id":"f0021","peak_time_s":126000},
 "grid_cell_area_m2":40000.0,"valid_cell_count":26500,"sea_cell_count":27}
```

面积计算前剔除 NaN 与海域 mask（run 目录 `sea_mask.npy` 若存在）；阈值键用字符串（如 `"0.05"`）。

## 7. `numeric/<case>/<run>/manifest.json`

```json
{"schema_version":"1.0","case_id":"...","run_id":"...",
 "grid":{"crs":"EPSG:32649","rows":1233,"cols":2230,"transform":[30.0,0.0,325737.9,0.0,-30.0,2579305.3],"cell_size_m":30.0,"cell_area_m2":900.0},
 "block_size":256,"blocks":{"rows":5,"cols":9},
 "dtype":"<f4","byte_order":"little","nodata":"nan",
 "layout":"block rows [by*256, min(rows,(by+1)*256)) x cols [bx*256, min(cols,(bx+1)*256)); C order; index=(r-r0)*w+(c-c0)",
 "mask_png":"numeric/<case>/<run>/mask.png",
 "mask_encoding":"1-bit PNG, full grid 1:1, row 0 north, pixel value 255=valid",
 "frames":[{"frame_id":"f0000","path_template":"numeric/<case>/<run>/f0000/{block_y}/{block_x}.bin"}],
 "max":{"path_template":"numeric/<case>/<run>/max/{block_y}/{block_x}.bin"}}
```

## 8. 命令行与模块 API

```text
python -m floodviz inventory  --config configs/default.yaml [--build-id local]   # 帧/头/时间校验 + run 清单
python -m floodviz prepare    --config configs/default.yaml --bundle-id v3-20261028   # 生成完整发布包
python -m floodviz validate   --bundle outputs/web_bundle/v3-20261028 [--full]       # 包完整性/数值/统计/空间门禁
python scripts/build_web.py   --bundle-id v3-20261028                                # 复制 web/ + vendor 到包根
python scripts/fetch_vendor.py --config configs/default.yaml                         # 下载固定前端资源
python scripts/start_local.cmd                                                       # 仅 127.0.0.1:8765 服务最近包
```

Lead 提供的模块（pipeline 直接调用，签名固定）：

```python
# src/floodviz/config.py
load_config(config_path: Path) -> Config            # 已解析三案例、路径、阈值、瓦片参数
Config.case(case_id) -> CaseConfig

# src/floodviz/raster/grid.py
read_ascii_header(path) -> dict                     # ncols/nrows/xllcorner/yllcorner/cellsize/nodata
read_ascii_grid(path, *, dtype=np.float32, nodata_to_nan=False) -> np.ndarray
header_to_grid(header, crs) -> Grid                 # Grid: rows, cols, transform(6), crs, cell_size_m, cell_area_m2, bounds_native
read_native_frame(path) -> np.ndarray               # Res 文件 → float32, -9999→NaN

# src/floodviz/quality/frames.py
audit_run(run_dir: Path, *, save_interval_s: float, sim_seconds: float) -> RunAudit
# RunAudit: frames(list[FrameInfo: seq/path/sha256/header]), contiguous, headers_identical, count, reject_reasons

# src/floodviz/catalog/timeline.py
build_timeline(case, run, frames, paths_prefix) -> dict
compute_sim_seconds(seq, save_interval_s) -> float
format_timestamp(time_origin: str|None, sim_seconds: float, tz: str) -> str|None

# src/floodviz/catalog/quality.py
load_mass_gate(parent_root: Path, run_path_rel: str) -> dict   # {mass_gate, e_mass, cause}

# pipeline 需实现（pipeline.py）：
prepare_bundle(config: Config, bundle_id: str) -> Path
validate_bundle(bundle_dir: Path, config: Config|None, full: bool) -> ValidationReport
```

`pipeline.py` 内部职责：校验 → 原生块与统计 → 瓦片（水深/max/DEM/SAR/光学）→ GeoJSON → catalog → manifest → licenses；提供 `--only`、`--frames-limit` 便于开发（但最终发布必须全量且校验通过）。生成的 bundle 必须能被 `validate` 以 `--full` 通过。

## 9. 图层可用性矩阵（配置 `configs/layers.yaml` + 资产清单）

只有 `ready/partial` 可开。当前事实基线（由 optical/SAR 处理结果修正，不得虚报）：
- jiulong：DEM ready；水深 ready；max ready；SAR（`2d_hydro_project/data/processed/s1_water/jiulong_20240917T105023_sigma20m.tif`，2024-09-17 后向散射）→ ready，角色“事后 SAR 后向散射（非水深）”；光学（本地 RGB 2026-04-03）→ partial（仅覆盖部分模型域，2026 背景 ≠ 2024 事件）或 `local_unchecked`（未完成质检时）；`灾后SAR_久隆区域.tif` 因传感器/日期/处理链未核实 → `local_unchecked`，禁用。
- ningming：DEM ready；水深 ready；max ready；SAR（GF-3 2026-08-24 水体掩膜）→ ready（二进制水体，非水深）；光学（S2 2026-09-03）→ ready 或 partial（按实际有效覆盖）；2026-08-12 景仅作为候选，不默认启用。
- guigang：DEM ready；水深 ready；max ready；SAR（S1 2024-09-12 后向散射，覆盖模型域东部一部分）→ partial（必须画足迹，不宣称全市覆盖）；光学（S2 2024-10-03，有效覆盖≈96.8%，约3.16%透明空缺）→ partial（跨“事件后”背景，不能当洪峰观测）。

若某资产下载/质检失败 → 状态 `failed`，原因写入 catalog 与 UI；不允许开关亮起。

## 10. Web 交互验收（web 组按此实现）

必做：
1. 三地切换；`map.fitBounds` 到 `model_bbox`；不显示行政区充数。
2. 预设：仅可见光 / 仅 SAR / 仅 DEM / 仅淹没 / 全部可用 / 自定义；底图独立开关。
3. 时间轴：拖动吸附真实帧；播放/暂停；上一帧/下一帧；速度 0.5/1/2/4 帧每秒；显示 T+小时与绝对时间（若 timestamp 为 null 只显示 T+）。
4. 每图层透明度滑杆；图例；显示模拟时间与影像真实采集时间（影像时间不随滑块变化）。
5. 点击地图：显示当前帧水深、模型分辨率、来源 run；NaN/域外明确提示；面积—时间曲线（阈值可切换 0.05/0.15/0.3/0.5/1 m）。
6. 情景/质量门/数据缺口/来源/许可面板；切案例同步刷新。
7. 最大包络：独立入口/开关，开启时暂停时间回放并显示“全程最大水深包络（非某一时刻）”标题；关闭后恢复。
8. 双缓冲：新帧瓦片与统计准备好后统一提交；请求令牌（case/run/frame）丢弃过期响应；预取前后 1 帧；LRU ≤256 MB（简单以瓦片字节数计）。
9. 缺帧/瓦片 404：暂停播放并提示“帧缺失”，不得用旧帧新日期或 0 值冒充。
10. 在线底图：Esri 失败时保留本地图层并显示“在线底图不可用”，不自动换未审核底图；Esri 路径 `{z}/{y}/{x}`；署名显示。
11. 离线：不依赖任何 CDN/字体/网络；vendor 本地。
12. 中文界面；1920×1080；无控制台错误；键盘空格播放/暂停、左右键逐帧。

## 11. 测试与验收（qa 组）

- 单元：ASCII 头/解析、帧审计（缺帧/不连续/头不一致/超时长拒绝）、时间格式（null/时区/严格递增）、阈值面积、图层状态机（只有 ready/partial 可开）、投影正反算（已知点）、瓦片编号（已知经纬度→XYZ）。
- 集成：三地小样本（可用 `--only case`/`--frames-limit` 生成到 `outputs/build/test`）→ 数值块读回 vs 源 `.wd` 抽样、统计与重算相对差 ≤0.1%、PNG 定位（源像元→瓦片像素颜色类匹配）、catalog schema 校验。
- e2e（Playwright/Chromium，本地 http.server 起 bundle）：三地切换、六预设、透明度、最大包络、SAR 日期文案、播放/暂停/逐帧、点查数值、面积曲线、断网（route abort 外部请求）仍可用、token 竞态（快速拖动后帧号/日期一致）。
- 性能：冷启动首个业务瓦片 ≤2 s、首屏可交互 ≤5 s（不计在线底图）、播放 1–2 帧/s 稳定、点查 p95 ≤200 ms、界面响应 p95 ≤300 ms、贵港 30 m 播放 30 分钟内存无持续增长且 ≤1.5 GB。测不到就写“未达标/未测”，不得虚构。
- 故障：临时改名一个瓦片/帧 → UI 明确提示；校验器报错。

## 12. 交付文档（qa + lead）

`README.md`（安装/构建/启动/回滚）、`AGENTS.md`（路径与只读规则）、`docs/data_contract.md`、`docs/runbook.md`、`docs/licenses.md` + `licenses/`、`docs/acceptance_report.md`（实测证据表：命令、结果、时延、误差、截图/trace 路径）、`docs/optical_assets.md`（optical 组）。`acceptance_report.md` 必须区分“已验证/未验证”，不得把未测写达标。

## 13. 汇报格式（团队内部）

完成后给 lead 发消息：状态（已完成/部分完成/阻塞）、改动文件清单、执行的命令与结果、未完成项与风险、下一步。不要只写“做完了”。
