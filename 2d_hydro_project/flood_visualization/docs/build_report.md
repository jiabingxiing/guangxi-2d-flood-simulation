# 构建实测报告（docs/build_report.md）

> **现状注记（2026-10-28）**：本文件记录的是历史构建/验收实测，内容保持原样不改。
> 其中提到的 `v1-20261027` 发布包已于 2026-10-28 按用户要求删除；当前保留 `v2-20261027`（回滚）与 `v3-20261028`（交付）。
> 保留数量由 `scripts/prune_bundles.py` 强制（硬下限 2 个），不要手工删除。

> 本文件是**数据管道组的构建实测记录**，与 QA 的 `docs/acceptance_report.md` 分工不同：
> 这里只写"我实际跑了什么、量到什么数"，不写未执行的性能目标。
> 生成时间：2026-10-27（UTC+08:00）｜构建者：pipeline（task-1）｜
> 唯一实施契约：`SPEC.md`（用户 2026-10-27 决定：**不建 XYZ 瓦片金字塔**，
> 每帧只输出原生网格 1:1 预着色 PNG，必要时按整数像元边界切 part）。

## 0. 结论摘要（先看这里）

| 项 | 实测值 | 预算/门禁 | 结论 |
|---|---|---|---|
| 发布包 `v1-20261027` 总体积 | **642.8 MB**（2906 文件） | ≤10 GB | **通过**（占 6.4%） |
| 其中数值保真层 `numeric/` | 619.0 MB（2672 个 `.bin`） | — | 体积主体 |
| 其中显示层 `frames/` | 21.7 MB（178 个 PNG + 4 DEM PNG） | 单图 ≤2 MB | **通过**（最大 355 KB） |
| 帧总数 | **116 帧**（21 + 19 + 19 + 57） | 21/19/57 | **通过** |
| `prepare` 墙钟（全量三地 4 run） | **114.1 s** | — | 实测 |
| `validate --full` 墙钟 | **71.6 s**（16,926 项检查） | — | 实测 |
| 数值回读误差 | **0.0 m**（3364 抽样点） | ≤max(1e-5, 1e-6·\|src\|) | **通过** |
| 统计复算相对差 | **0.0**（116 帧全量复算） | ≤0.1% | **通过** |
| 空间贴图残差 | 最大 **0.338 px = 10.15 m**（贵港 1×2） | ≤0.5 源像元 | **通过** |
| `res.max ≥ 逐帧fmax` 单向性 | 三地全部成立 | 必须单向 | **通过** |
| schema 校验 | 0 失败（catalog/timeline/statistics/images/numeric_manifest） | 0 失败 | **通过** |

## 1. 构建命令与产物

```powershell
$env:PYTHONUTF8='1'
cd 2d_hydro_project/flood_visualization

# 1) 构建全量包（三地 4 个非 optional run）
uv run python -m floodviz prepare --config configs/default.yaml --bundle-id v1-20261027
#   → outputs/web_bundle/v1-20261027   114.1 s

# 2) 复制静态页面与本地 vendor（Lead 执行）
uv run python scripts/build_web.py --bundle-id v1-20261027

# 3) 全量门禁 + 回填 manifest.validation
uv run python -m floodviz validate --bundle outputs/web_bundle/v1-20261027 --full
#   → ok=True   71.6 s   16,926 项检查 0 失败
```

- **原子发布**：构建先落 `outputs/web_bundle/.v1-20261027.building-<pid>/`，
  全部成功后才 rename 为正式目录；失败自动清理，不会留下半个包。
- **并发保护**：临时目录名带 PID；检测到**其它**同 bundle_id 的 `.building-*` 直接
  抛 `PipelineError`（同一 bundle 只允许单点构建）。这一点由一次真实撞车事故验证：
  Lead 与我同时构建同一 bundle 时报 `FileNotFoundError`，加保护后不再发生。
- **流式常量内存**：逐帧 `读 ASCII → 写 256 块 → 渲染 PNG → 统计 → 丢弃`，
  峰值只保留单帧 float32（贵港 11.0 MB）+ 块写缓冲；**不缓存全部帧**。

## 2. 每 run 实测（帧数 / split / PNG / 体积 / 耗时）

| case / run | 帧数 | 网格 | split | PNG 数 | PNG 合计 | PNG min / 中位 / max | numeric | 构建耗时 |
|---|---:|---|---|---:|---:|---|---:|---:|
| jiulong / `scen_free_200m_sv6h` | 21 | 182×152 @200 m | **1×1** | 22 | 0.108 MB | 187 B / 7.4 KB / 9.1 KB | 2.3 MB | 0.58 s |
| ningming / `scen_q6000_n100_100m` | 19 | 244×224 @100 m | **1×1** | 20 | 0.247 MB | 292 B / 15.0 KB / 17.3 KB | 4.2 MB | 0.66 s |
| ningming / `scen_q4000_100m`（对照） | 19 | 244×224 @100 m | **1×1** | 20 | 0.231 MB | 292 B / 14.2 KB / 15.6 KB | 4.2 MB | 0.46 s |
| guigang / `scen_yagi_q11500_30m_b5` | 57 | 2230×1233 @30 m | **1×2** | 116 | **18.94 MB** | 5.4 KB / **209 KB** / **355 KB** | 608.4 MB | 50.4 s |
| **合计** | **116** | | | **178** | **20.48 MB** | | **619.0 MB** | **114.1 s** |

- 分 case 墙钟：jiulong **0.61 s** / ningming **1.17 s** / guigang **51.6 s**（其余为内容哈希与 catalog 组装）。
- `PNG 数` = 帧数 + 1（`max` 包络），贵港 30 m 因 1×2 每帧 2 张：`57×2 + 2 = 116`。
- **贵港 30 m 洪峰帧实测 355 KB**（先前估计 538 KB，实测更小）；单图远低于 2 MB 门限。
  保留 RGBA 不做调色板量化，以保证 PNG 颜色与固定色带逐像素一致（便于验收比对）。

## 3. 空间门禁：四角贴图残差（关键实测）

MapLibre `image` source 按四角经纬度做双线性 warp。UTM 等东坐标线在经纬度空间是弯的，
单张图的 warp 残差随**跨东距离二次增长**，超过 0.5 源像元时必须按整数像元边界切 part
（Lead 2026-10-27 裁决：不放大容差，改为自适应切分）。

实测方法：源像元中心 `(col+0.5,row+0.5)` → 原生 UTM → `proj` 到 `EPSG:4326` →
Web Mercator（MapLibre 渲染空间）→ part 四角双线性反算 → 回原生连续像元坐标 → 与真值比较。

| case / run | 1×1 残差（被拒） | 采用 split | 每 part 残差 | 米 | 门禁 |
|---|---:|---|---:|---:|---|
| jiulong / 200 m | 0.0478 px ✓ | 1×1 | r0c0 **0.0478 px** | 9.71 m | ≤0.5 ✓ |
| ningming 6000 / 100 m | 0.0421 px ✓ | 1×1 | r0c0 **0.0428 px** | 4.28 m | ≤0.5 ✓ |
| ningming 4000 / 100 m | 0.0421 px ✓ | 1×1 | r0c0 **0.0428 px** | 4.28 m | ≤0.5 ✓ |
| guigang 30 m | **1.2066 px ✗** | **1×2** | r0c0 **0.3383 px** / r0c1 **0.3382 px** | 10.15 / 10.15 m | ≤0.5 ✓ |
| guigang 200 m（未入 v1） | 0.1804 px ✓ | 1×1 | — | — | — |

- 贵港 30 m 误差**几乎全在行方向**（max_col 0.077 px vs max_row 1.260 px），
  物理解释是 UTM 等东坐标线在经纬度空间随跨东距离弯曲；沿列对半切后弯曲降到约 1/4，
  实测 0.332 px，与理论一致。**沿行切无用，故只用 1×2。**
- 候选切分的实测序列（`manifest.validation.spatial.*.attempts`）已留档，可复核为何选 1×2。
- 子图**只沿整像素边界切**：无重采样、无插值、无重叠，覆盖严格等于整幅
  （`paint.check_partition` 断言每像元归属恰为 1 个 part）。

## 4. 数值保真层（numeric/**）

- 布局：`numeric/<case>/<run>/<frame_id>/<block_y>/<block_x>.bin`，块 256×256、边缘裁剪、
  块内 **C 序**、`float32` **小端**、`NaN` = 无效。块数：jiulong 1×1、ningming 1×1、
  贵港 30 m **5 行 × 9 列 = 45 块/持有者**。
- 文件齐全性（独立脚本复算，不依赖包内 import）：

| run | 帧 + max 持有者 | 块数/持有者 | 期望文件 | 实测 | 缺失/尺寸错 |
|---|---:|---:|---:|---:|---:|
| jiulong | 22 | 1 | 22 | 22 | 0 |
| ningming 6000 | 20 | 1 | 20 | 20 | 0 |
| ningming 4000 | 20 | 1 | 20 | 20 | 0 |
| guigang 30 m | 58 | 45 | 2610 | 2610 | 0 |
| **合计** | | | **2672** | **2672** | **0** |

- **数值回读门禁**（`validate --full`，让逐帧抽 8 帧 × 每帧 24 随机点 + 四角 + 中心）：

| run | 抽样帧 | 抽样点 | 最大绝对误差 | 门禁 | 结论 |
|---|---:|---:|---:|---|---|
| jiulong | 21 | 609 | **0.0 m** | max(1e-5, 1e-6·\|src\|) | 通过 |
| ningming 6000 | 19 | 551 | **0.0 m** | 同上 | 通过 |
| ningming 4000 | 19 | 551 | **0.0 m** | 同上 | 通过 |
| guigang 30 m | 57 | 1653 | **0.0 m** | 同上 | 通过 |
| **合计** | **116** | **3364** | **0.0 m** | | **通过** |

  误差恰为 0 是因为源 ASCII 值经 `float32` 化后写入、再 `float32` 读回，**逐位一致**。
  越界坐标抽样断言必须抛错（返回"域外"，不夹到边缘像元），已逐个验证。
- `mask.png`：1-bit PNG，整幅 1:1，白=有效。尺寸已逐一核对等于网格
  （jiulong 182×152、ningming 244×224、guigang 2230×1233）。
  **口径**：`mask = 非 NaN`。海域像元（久隆 `sea_mask.npy` 27 个）**不进数值块掩膜**，
  点查仍返回真实值（模型已把海面钳 0）；海域只从**统计**中剔除。
- `max` 包络块写 `numeric/<case>/<run>/max/<by>/<bx>.bin`，取自模型自带的 `results/res.max`。

## 5. 统计（statistics.json）

口径（不可变，写入每个 statistics.json）：原生网格**有效像元**（非 NaN 且非海域）
中 `depth > 阈值` 的面积；**严格大于**（不是 ≥）；不从 PNG 颜色反算。

- 阈值：`0.05 / 0.15 / 0.3 / 0.5 / 1.0 / 2.0 / 5.0 / 10.0 m`，键用字符串（`"0.05"`…）。
- 三地默认 run 结论：

| case | 峰值帧 | 峰时 | 包络面积 @0.05 m | 包络像元 | 包络最大水深 |
|---|---|---|---:|---:|---:|
| jiulong | f0019 | T+114.0 h | 263.24 km² | 6581 | 6.027 m |
| ningming 6000 | f0016 | T+96.0 h | 126.22 km² | 12622 | 18.552 m |
| guigang 30 m | f0047 | T+282.0 h | 493.36 km² | 548182 | 19.436 m |

- **统计复算门禁**（`validate --full`，用源 `.wd` 独立重算，116 帧**全量**不抽样）：

| run | 复算帧数 | 最大相对差 | 门禁 | 结论 |
|---|---:|---:|---|---|
| jiulong | 21 | **0.0** | ≤0.1% | 通过 |
| ningming 6000 | 19 | **0.0** | ≤0.1% | 通过 |
| ningming 4000 | 19 | **0.0** | ≤0.1% | 通过 |
| guigang 30 m | 57 | **0.0** | ≤0.1% | 通过 |
| **合计** | **116** | **0.0** | | **通过** |

## 6. `res.max` 与逐帧采样差异（**物理正确，不是缺陷**）

模型 `saveint=21600 s`，但内部时间步远密（久隆 dt=5 s、宁明 1.5 s、贵港 5 s）。
`res.max` 是**每个内部子步**累积的包络，逐帧只是 6 h 采样，因此 `res.max ≥ 逐帧fmax`
**单向成立**，差异代表采样间隙里的瞬时峰值。

| case / run | 逐像元 `res.max − 逐帧fmax` 最大差 | 反向（逐帧 > res.max） | 比较像元 |
|---|---:|---:|---:|
| jiulong / 200 m | **+0.060 m** | **0（无反向）** | 27 664 |
| ningming 6000 / 100 m | **+0.119 m** | **0** | 54 656 |
| ningming 4000 / 100 m | **+0.067 m** | **0** | 54 656 |
| guigang 30 m | **+0.615 m** | **0** | 2 749 590 |

- 门禁只断言**单向性**（不要求相等），实测三地全部成立（`min_signed_diff_m = 0.0`）。
- 记录位置：`manifest.validation.max_crosscheck.by_run`。
- **UI 文案要求**：开启最大包络时必须写"全程最大水深包络（非某一时刻；含 6 h 采样间隙的瞬时峰值）"，
  避免用户误以为包络 = max(可见帧)。

## 7. 门禁总表（`validate --full` 实测）

`uv run python -m floodviz validate --bundle outputs/web_bundle/v1-20261027 --full`
→ **ok=True**，**16,926 项检查，0 失败**，**71.6 s**。

| 组 | 检查项 | 失败 | 说明 |
|---|---:|---:|---|
| structure | 220 | 0 | catalog/timeline 必填字段、状态机、路径安全（禁绝对路径/盘符/源数据路径） |
| timeline | 1811 | 0 | `sim_seconds = seq×saveint`、严格递增、末帧不超时长、origin 未确认→timestamp=null |
| statistics | 3924 | 0 | 阈值键一致、面积与像元数×格面积自洽、包络 ≥ 任一帧、与 timeline 对齐 |
| images | 1981 | 0 | part 顺序/数量=row-major split、id 全文件唯一、坐标 4 角、≤4096 px、文件存在 |
| completeness | 251 | 0 | 2672 块齐全、mask 尺寸=网格、2889 个 content 哈希复算一致 |
| numeric | 6845 | 0 | 3364 抽样点回读、NaN 语义、越界必须报错 |
| statistics_recompute | 1893 | 0 | 116 帧全量用源 `.wd` 重算，相对差 ≤0.1% |
| spatial | 10 | 0 | 每 part 反贴图残差 ≤0.5 px、与构建期记录一致性 |
| schemas | — | 0 | 复用 Lead 的 `publishing.schemas.validate_bundle_documents`（0 失败文档） |

回填结果（包根 `manifest.json`）：
- `validation.numeric = {samples: 3364, max_abs_err_m: 0.0, max_rel_err: 0.0, gate, per_run, passed: true}`
- `validation.statistics = {frames_checked: 116, max_rel_diff: 0.0, gate: 0.001, per_run, passed: true}`
- `validation.spatial.*` = 各 run 的 `split/parts/max_px/max_m/samples/gate_px/passed`
- `validation.max_crosscheck` = 单向性结论 + 三地 `max_diff_m_by_case`
- `validation.gate_summary.ok = true`
- **`web.ready` 保持 `true`**（回填只写 `validation`，不动 `web`）

## 8. 显示层（frames/**）实现要点

- 每帧 **1 张 PNG（split=1×1）或 2 张（贵港 30 m，1×2）**，尺寸 = 该 part 覆盖的
  模型像元数（像素=面积，pixel-is-area），**行 0 = 最北行**。
- 固定色带（`catalog.display.depth_classes`）：`depth ≤ 0.05 m` 或 `NaN → alpha=0`；
  其余按分级着色。**不插值、不伪造中间时刻、无多层级金字塔。**
- 文件名约定：`<frame_id>_r<row>c<col>.png`；max 为 `max_r<row>c<col>.png`；
  DEM 为 `frames/<case>/dem_r<row>c<col>.png`。
- `images.json`（run 级）：`grid`（run 级几何）、`color_scale`、`split`、
  `frames[].parts[]`（含 `id/row/col/path/width/height/coordinates`）、`max.parts[]`、
  `spatial_validation`（本报告第 3 节的实测残差）。
- 干帧（无水深）如实写 `max_depth_m=0 / wet_pixels=0`，PNG 全透明（久隆 f0000–f0004 即如此，
  首湿为 f0005=0.26 m）。**不是缺陷，不得跳过。**
- DEM：连续色带、NaN 透明、**最近邻原值**（不做双线性"造高"），与水深层同 split。

## 9. 已知缺口与风险

| 项 | 状态 | 说明 / 下一步 |
|---|---|---|
| 光学图层（三地） | **missing** | 资产清单 `data/processed/assets/manifest.json` 当前仅 3 条 SAR 记录，无光学；v1 中三地 `optical` 状态为 `missing` 并带人类可读原因。optical 组补齐（原图缩到长边 ≤2048 px / ≤4 MB）后构建 **v2** 增量。计划明确 M1 不依赖光学，**不阻塞 v1 发布**。 |
| SAR 图层 | **ready / ready / partial** | 已吸入：久隆 100% 覆盖（事后过境）、宁明 100%（近洪峰）、贵港 **20.64%**（partial，必须画足迹）。三地均带 `image` + `footprint` + 实测 `note`（采集时刻/覆盖/署名/角色）。 |
| optional run | 未入 v1 | `jiulong/…_recession`（53 帧）、`guigang/…_200m`（57 帧）为 `optional: true` 对照，未纳入默认包。需要时用 `--only` 或改配置另行构建。 |
| `origin_status` | 久隆/宁明 `relative_only_candidate` | 按契约 `timestamp = null`，UI 只显示 T+ 小时；贵港 `model_schedule_confirmed` 有绝对时间。已由 timeline 门禁逐帧断言。 |
| 垂直基准 | 未闭合（G7） | 三地 `vertical_datum_status` 均写明；不得直接与站水位相减率定。 |
| 面积口径 | 已固定 | `depth > 阈值` 严格大于 + 剔除 NaN 与海域 27 像元（仅久隆）。**不是** `≥`，不含海域。 |
| 性能（浏览器） | **未测** | 本报告不含浏览器帧率/内存/首屏指标（属 QA/Web 组）。这里只给构建期 CPU 与包体积实测。 |
| v2 前置条件 | 待 optical | 构建 v2 前应确认 `data/processed/assets/manifest.json` 的 `counts.total = 6`（3 SAR + 3 光学）。 |

## 10. 复现方式与验证脚本

```powershell
$env:PYTHONUTF8='1'
cd 2d_hydro_project/flood_visualization

# 单案例快速迭代（开发用）
uv run python -m floodviz prepare --config configs/default.yaml --bundle-id dev1 --only jiulong --frames-limit 3

# 全量（正式发布）
uv run python -m floodviz prepare --config configs/default.yaml --bundle-id v1-20261027
uv run python scripts/build_web.py --bundle-id v1-20261027
uv run python -m floodviz validate --bundle outputs/web_bundle/v1-20261027 --full

# 只做结构/齐全性（不读源帧，快）
uv run python -m floodviz validate --bundle outputs/web_bundle/v1-20261027
```

- **顺序不可颠倒**：`prepare → build_web → validate --full`。
  `validate` 在 `manifest.web.ready=false` 时给 warning（不阻断，但正式发布应为 true）。
- **构建期间不要读该 bundle**：原子 rename 瞬间读端可能看到中间态（已实际发生过一次误报）。
- 相关模块：`src/floodviz/pipeline.py`（构建/校验入口）、`raster/blocks.py`（数值块）、
  `raster/paint.py`（着色/切分/残差）、`quality/stats.py`（统计）、`quality/validate.py`（门禁）。
