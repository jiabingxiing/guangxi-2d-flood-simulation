# 验收报告（docs/acceptance_report.md）

> **现状注记（2026-11-25 复核）**：本文件记录的是 2026-10-27 `v2-20261027` 首交付的历史构建/验收实测，正文保持原样。
> 其中提到的 `v1-20261027` 已按用户要求删除；**当前交付包是 `v10-20261122`**，本文件的测试计数（277）与 e2e 条数（29）是当时的快照，现役为 295 项（见 `docs/runbook.md` §4 与 `README.md` §7）。
> 保留数量由 `scripts/prune_bundles.py` 强制（硬下限 2 个），不要手工删除。

> 验收对象：`2d_hydro_project/flood_visualization` 发布工程 + 发布包 `v2-20261027`（含光学图层）。
> 验收日期：2026-10-27（北京时间）。验收机：LENOVO 82A8（Ryzen 7 4800U / 16 GB / NVMe SSD），Windows 10 22H2。
> **性质**：研究性情景成果，不是业务预警系统；不得用于业务告警或资产安全判据。
>
> 本报告严格区分「已验证」与「未验证」。未实测的指标一律写「未测」并说明方法，**不把目标值当实测值**。

## 0. 结论摘要

| 类别 | 结果 |
|---|---|
| 自动化测试 | **277 passed**（单元 + 集成 + e2e + 性能/资源），0 failed |
| 发布门禁 `validate --full` | **ok=True**，9 组门禁 **16 945 项检查 0 失败**，耗时 59.6 s |
| 独立复核 `verify_bundle.py`（Lead 侧） | **全部通过**（三地数值抽样、分级色、parts 无缝、帧语义） |
| 固定抽样回归 `data/splits/sampling_index.json` | **116 帧、7 221 固定点、误差 0.000e+00 m**；包络 19 点误差 0 |
| 发布包体积 | **683.9 MB**（预算 ≤10 GB，占 6.8%） |
| 数值保真 | 构建期抽样 3 364 点 + **固定索引 7 221 点**，最大绝对误差均 **0.0 m**（float32 逐位一致） |
| 统计一致性 | **116 帧全量复算**，最大相对差 **0.0**（门禁 0.1%） |
| 空间定位 | 最差 **0.338 源像元（10.15 m）**，门禁 0.5（贵港 30 m 1×2 切分后） |
| 时间语义 | 久隆/宁明 `timestamp` 全 null（不猜填）；贵港绝对时间完整 |
| 浏览器 e2e | **29 条**（SPEC §10 的 18 条 + 图层矩阵 3 条 + 性能/资源/稳定性 8 条）全部通过 |
| 首屏 | 首帧可交互 **1.58 s**（门禁 ≤5 s）；首批业务图 **1.58 s**（门禁 ≤2 s） |
| 点查询 | 缓存命中 p95 **10.6 ms**（门禁 ≤200 ms） |
| 界面操作 | 切帧 p95 **81.2 ms**（门禁 ≤300 ms） |
| 播放 | 2 帧/s 目标下实测 **1.81 帧/s**（6 s 推进 11 帧） |
| 内存 | 浏览器总内存 **710.5 MB**（目标 ≤1.5 GB）；LRU 缓存 247.5 MB（上限 256 MB）；JS 堆 20.5 MB；100 次切地堆增长 **0.0 MB** |
| 未验证项 | 真机 Edge/Chrome 双浏览器、5 名试用者可读性测试、贵港站区 5 m 模型（父项目推迟） |
| 30 分钟长播 | **已验证（2026-10-30）**：1 802 s、0 错误、缓存 66.7 MB 恒定、内存峰值 748.4 MB、稳态斜率 −2.16 MB/min。见 §8 |

## 1. 环境与对象

| 项 | 值 |
|---|---|
| 发布包 | `outputs/web_bundle/v2-20261027`（另有 `v1-20261027` 作回滚基线） |
| bundle_id / created | `v2-20261027` / `2026-10-27T10:01:22+00:00` |
| 构建墙钟 | `prepare` 92.2 s；`validate --full` 59.6 s |
| 文件数 | 2 912（其中数值块 2 672、显示图 180） |
| 浏览器 | Playwright Chromium，视口 1920×1080，deviceScaleFactor=1 |
| 服务 | `python -m http.server` 绑 127.0.0.1（正式交付端口 8765，e2e 用 8790） |

## 2. 三地覆盖（计划 §1.4「三地覆盖」）

| 案例 | run | 帧数 | 网格 | 分辨率 | 质量门 | 入包 |
|---|---|---:|---|---:|---|---|
| 久隆 | `scen_free_200m_sv6h` | **21/21** | 152×182 | 200 m | PASS (E=4.893e-4) | 是 |
| 宁明 | `scen_q6000_n100_100m` | **19/19** | 224×244 | 100 m | PASS (E=5.80e-5) | 是 |
| 宁明（对照） | `scen_q4000_100m` | **19/19** | 224×244 | 100 m | PASS (E=7.02e-5) | 是 |
| 贵港 | `scen_yagi_q11500_30m_b5` | **57/57** | 1233×2230 | 30 m | PASS (E=5.665e-5) | 是 |
| **合计** | | **116 帧** | | | | |

- 三地 **21/19/57** 帧数与计划 §1.3 表一致；序号连续、帧头一致、末帧未超模拟结束（`inventory` 与 `validate` 双重校验）。
- 可选 run（久隆退水 53 帧、贵港 200 m 对照 57 帧）**未入 v2**，在 catalog 中以配置存在但不在包内；不冒充已发布。

## 3. 数值保真与统计（计划 §1.4「查询数值」「统计一致性」）

| 检查 | 方法 | 实测 | 门禁 | 结论 |
|---|---|---|---|---|
| 点查数值 = 源值 | 从源 `.wd` 与发布包 float32 块同时读取同像元 | **max_abs_err = 0.0 m**（3 364 抽样点） | ≤max(1e-5 m, 1e-6·\|src\|) | **已验证** |
| NaN 保真 | NaN 位置逐帧比对 | 位置完全一致 | 一致 | **已验证** |
| 帧面积 | 用源帧独立重算（不复用构建代码） | **max_rel_diff = 0.0**（116 帧全量） | ≤0.1% | **已验证** |
| 零面积 | 全干帧单独断言 | `areas=0.0` 且 `wetted_cells=0` | 必须为 0 | **已验证** |
| 海域剔除 | 与源 `sea_mask.npy` 比对 | 久隆 27 像元一致；宁明/贵港 0 | 一致 | **已验证** |
| 最大包络单向性 | `res.max` vs 逐帧逐像元取最大 | **`res.max ≥ 逐帧fmax` 恒成立**，最大差 久隆 0.060 / 宁明 0.119 / 贵港 0.615 m | 单向 | **已验证** |

> `res.max` 差异为**物理正确**：模型在每个内部时间步累积包络，6 h 保存间隔之间有 720/1440 子步。
> 因此包络可能大于任一可见帧的最大值；界面必须写「含 6 h 采样间隙的子步瞬时峰值」，不得称「可见帧最大值」。

## 4. 空间定位（计划 §1.4「空间导出」）

单张四角双线性贴图的残差随跨东距离二次增长（UTM 等东坐标线在经纬度空间弯曲）。
按「**最少子图数满足 ≤0.5 源像元**」自适应切分：

| 案例/run | split | 每 part 残差 | 结论 |
|---|---|---:|---|
| 久隆 `scen_free_200m_sv6h` | 1×1 | 0.0486 px（9.71 m） | 达标 |
| 宁明 `scen_q6000_n100_100m` | 1×1 | 0.0428 px（4.28 m） | 达标 |
| 宁明 `scen_q4000_100m` | 1×1 | 0.0428 px（4.28 m） | 达标 |
| 贵港 `scen_yagi_q11500_30m_b5` | **1×2** | **0.3383 px（10.15 m）** | 达标 |

- 1×1 尝试值也记录在 `manifest.validation.spatial[].attempts`：贵港 30 m 1×1 = **1.2066 px**（被拒）。
- 切分只沿整像元边界，**不重采样、不插值**；parts 之间无缝、无重叠、并集 = 整幅（`verify_bundle.py` 断言覆盖位图全 True）。
- 独立复核：把 parts 拼回整幅后与源帧逐像元比较分级色（float32 语义），三地 **2 401 个抽样湿像元 0 不匹配**；干像元 alpha 全为 0。

## 5. 时间语义（计划 §3.2 / §3.4）

| 案例 | origin_status | time_origin | 每帧 timestamp | 界面显示 |
|---|---|---|---|---|
| 久隆 | `relative_only_candidate` | **null** | **全 null** | 仅 `T+xx.x h` + 「起点为候选依据」 |
| 宁明 | `relative_only_candidate` | **null** | **全 null** | 同上 |
| 贵港 | `model_schedule_confirmed` | `2024-09-03T00:00:00+08:00` | 6 h 步进至 T+336 h | `T+` 与绝对时间（北京时间）并列 |

- 门禁：`relative_only*` 时 `timestamp` 必须为 null；`model_schedule_confirmed` 时不得缺 timestamp。
- e2e 断言：久隆 `#time-absolute` 不含 `Invalid`/`NaN`，且包含「不可用」；贵港含 `2024-09-03`。
- 禁止插值：`temporal_interpolation="none"`，六预设/播放/逐帧全部基于真实帧索引。

## 6. 图层状态机（计划 §1.4「图层状态」）

| 案例 | dem | flood | max | sar | optical | boundary | sites |
|---|---|---|---|---|---|---|---|
| 久隆 | ready | ready | ready | **ready** | **partial** | ready | ready |
| 宁明 | ready | ready | ready | **ready** | **ready** | ready | ready |
| 贵港 | ready | ready | ready | **partial**（20.64% 覆盖） | **partial**（3.16% NoData） | ready | ready |

- 只有 `ready` / `partial` 可勾选；`remote_verified` / `local_unchecked` / `missing` / `failed` 一律禁用并显示原因。
- e2e 断言：`optical_only` 预设后可见图层集合恰为 `{optical}`；`all_available` 含 `{dem, flood, sar, optical}`；底图开关独立于预设。
- SAR 语义：久隆「2024-09-17 退水后（事后）」、宁明「GF-3 2026-08-24 近洪峰（±6 h）」、贵港「2024-09-12 洪峰期，仅覆盖模型域 20.64%，必须显示足迹」。
- 影像采集时间固定，不随模拟滑块变化（e2e 在 index 5/12/20 三次校验不变）。

## 7. 浏览器 e2e（SPEC §10 的 18 条 + 扩展）

`uv run python -m pytest tests/e2e -q` → **29 passed**（其中 8 条标记 `slow`）。

| # | 用例 | 结果 | 证据 |
|---|---|---|---|
| 1 | 三地切换、fitBounds 到模型域、默认 run 与 catalog 一致 | 通过 | `evidence/01-case-guigang.png` |
| 2 | 切案例同步刷新质量/来源/图例/时间读数，无残留 | 通过 | — |
| 3 | 六预设齐全且行为正确；底图开关独立 | 通过 | — |
| 4 | 时间轴吸附、播放/暂停、上一帧/下一帧、0.5/1/2/4 帧每秒 | 通过 | — |
| 5 | 贵港绝对时间正确 | 通过 | — |
| 6 | 四图层独立透明度滑杆 | 通过 | — |
| 7 | 影像采集时间不随模拟时间变化 | 通过 | — |
| 8 | 点查返回水深/分辨率/来源 run；域外明确提示不夹边 | 通过 | `evidence/03-query.png` |
| 9 | 干帧提示「当前帧无淹没像元（模型该时刻全干）」 | 通过 | — |
| 10 | 面积—时间曲线绘制 + 阈值切换（0.05/0.15/0.3/0.5/1） | 通过 | `evidence/04-area-chart.png` |
| 11 | 质量/许可/来源面板内容完整 | 通过 | — |
| 12 | 最大包络：标题变化、暂停回放、时间轴禁用、图例切换 | 通过 | `evidence/05-max-envelope.png` |
| 13 | 快速拖动后帧号/时间一致（令牌竞态） | 通过 | — |
| 14 | 缺帧时暂停并提示，不用旧帧冒充 | 通过 | `evidence/06-missing-frame.png` |
| 15 | Esri 底图失败保留本地图层 + 「在线底图不可用」 | 通过 | `evidence/07-basemap-offline.png` |
| 16 | 断网（拦截全部非回环请求）页面仍可用、点查可用 | 通过 | `evidence/08-offline.png` |
| 17 | 中文界面 + 键盘（空格播放/暂停、←/→ 逐帧） | 通过 | — |
| 18 | 无控制台错误、无同源 4xx | 通过 | — |
| 19 | **资源零 404**：280 个引用文件（catalog 图层、parts、数值块跨块列抽样） | 通过 | `outputs/qa/resource_integrity.json` |
| 20 | **点查跨块覆盖**：贵港 9 个块列全命中，返回数值 | 通过 | `outputs/qa/query_block_coverage.json` |
| 21 | **稳定性**：100 次切地 + 2 分钟播放，缓存不溢出、无报错 | 通过 | `outputs/qa/stability_100_switches.json`、`stability_playback_2min.json` |
| 22 | **15 个「地点×预设」矩阵**（3 地 × 5 预设）：可见集合与图层状态机严格一致 | 通过 | `outputs/qa/layer_matrix_15.json` |
| 23 | **缺图层夹具**（v1 基线光学缺失）：开关禁用 + 说明原因，无空白假开关 | 通过 | `outputs/qa/missing_layer_disabled.json`、`evidence/09-missing-layer-disabled.png` |
| 24 | **三地播放稳定性**（每地 30 轮，2 帧/s）：帧号严格递增、不越界、不伪造帧 | 通过 | `outputs/qa/playback_three_cases.json` |
| 25 | **元数据体积预算**：每案例元数据 ≤1 MB | 通过 | `outputs/qa/metadata_size.json` |
| 26 | **浏览器总内存**：Sum(Chromium RSS) ≤1.5 GB | 通过 | `outputs/qa/browser_memory.json` |

## 8. 性能实测（计划 §1.4 指标表）

**测量条件**：`headless=True` + GPU 旗标（`--use-angle=d3d11 --enable-gpu --ignore-gpu-blocklist --enable-gpu-rasterization`）。
这样**不弹窗口**（不触发浏览器下载管理器如 IDM 的拦截），但仍走真实核显 ANGLE/D3D11，
渲染器字符串为 `ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001636) Direct3D11 vs_5_0 ps_5_0, D3D11)`。
SwiftShader 软件渲染同时记录作为下界对比，**不用于门禁判定**。

| 指标 | 门禁 | 实测（真实 GPU） | 对比：SwiftShader | 结论 |
|---|---:|---:|---:|---|
| 冷启动 → 首帧可交互 | ≤5 s | **1.577 s** | 0.935 s | 已验证 |
| 首批业务图加载完成 | ≤2 s | **1.579 s** | 0.941 s | 已验证 |
| 点查 p95（缓存命中） | ≤200 ms | **10.62 ms**（p50 7.86） | 51.43 ms | 已验证 |
| 点查冷查询 p50 / max | 参考 | 8.87 / 65.81 ms | 28.85 / 233.05 ms | 记录 |
| 界面响应 p95（切帧） | ≤300 ms | **81.2 ms**（p50 65.6） | 512.1 ms | 已验证 |
| 播放有效帧率（目标 2 帧/s） | 1–2 帧/s | **1.81 帧/s** | 1.73 帧/s | 已验证 |
| LRU 缓存占用 | ≤256 MB | **247.5 MB**（上限 268.4 MB，无溢出） | 同 | 已验证 |
| JS 堆 | 目标 ≤1.5 GB | **20.5 MB** | 24.5 MB | 已验证（下界） |
| 浏览器总内存（Sum Chromium RSS） | 目标 ≤1.5 GB | **710.5 MB**（贵港 30 m 57 帧全过一遍） | — | **已验证** |
| 元数据体积 | ≤1 MB/案例 | 久隆 **0.047 MB**、宁明 **0.086 MB**、贵港 **0.162 MB** | — | **已验证** |

补充稳定性：
- 100 次三地循环切换：LRU 59.2 MB / 14 项 / 无溢出；**JS 堆增长 0.0 MB**；无 JS 错误。
- 连续播放 120 s（2 帧/s）：推进至第 56 帧；缓存 225.5–236.5 MB，淘汰 81 次，**无溢出、无报错**。

**未验证**：
- 浏览器总内存只统计 OS 进程工作集，**未单独区分 WebGL 纹理与 GPU 显存**；但已覆盖浏览器全部进程，不再是仅 JS 堆的下界估计。
- 真实 Edge/Chrome 双浏览器：仅 Chromium（Playwright 自带，与 Chrome 同内核）实测；Edge 未独立跑。

**补充实测（2026-10-30）：30 分钟连续播放门已执行并通过**

此前本报告只做 2 分钟，30 分钟标为「未测」。现已新增自动化门 `tests/e2e/test_e2e_longplay_30min.py` 并实测通过：

| 指标 | 实测值 | 门 |
|---|---|---|
| 时长 / 采样点 | 1 802 s / 326 | ≥1 800 s |
| 控制台错误 | 0 | 0 |
| LRU 缓存 | 66.7 MB 恒定，0 溢出 / 0 失败 / 0 淘汰 | ≤256 MB |
| 案例覆盖 | 贵港、久隆、宁明 | 三地 |
| 帧号 | 24 个不同帧号（9–56） | ≥4 |
| 内存峰值 | 748.4 MB | ≤1 536 MB |
| 稳态增长 | +3.38% | ≤20% |
| 稳态斜率 | −2.16 MB/min | ≤2.0 MB/min |

证据：`outputs/qa/longplay_30min.json`。

口径说明：增长门基线取**预热 420 s 后**的首个采样点，不用起始值。原因有两条：一是页面加载后 JIT、WebGL 纹理池与瓦片缓存会一次性拉升内存（实测预热期 +45.8 MB/min，稳态仅 +0.36 MB/min）；二是起始值会被残留浏览器进程污染（实测起始 473.0 MB 高于首个采样 366.2 MB）。

须注意：t=661 s 处 RSS 由 727 MB 降至 447 MB，为浏览器内存回收，属正常现象。该下跌使稳态斜率为负。

## 9. 发布门禁与复现（`validate --full`）

```
uv run python -m floodviz validate --bundle outputs/web_bundle/v2-20261027 --full
→ ok=True   62.696 s   16 945 项检查   0 失败
```

| 门禁组 | 检查项 | 失败 |
|---|---:|---:|
| structure（目录/必填字段/路径安全） | 229 | 0 |
| timeline（帧序/时间单调/origin 规则） | 1 811 | 0 |
| statistics（结构/阈值键/包络） | 3 924 | 0 |
| images（parts 几何/尺寸/唯一 id） | 1 981 | 0 |
| completeness（引用文件齐全 + 内容哈希） | 251 | 0 |
| numeric（值域/NaN/回读） | 6 845 | 0 |
| statistics_recompute（面积复算） | 1 893 | 0 |
| spatial（四角残差 ≤0.5 px） | 10 | 0 |
| schemas（JSON Schema） | 0 失败文档 | 0 |

- `manifest.content` 全部 **2 895** 个文件 sha256 逐一复算一致；`timeline.frames[].sha256` 与源 `.wd` 逐帧相等（构建时计算）。
- 三地 `grid.grid_hash`：久隆 `e594f0e5…`、宁明 `fd7874a0…`、贵港 `0d43b543…`。
- `runs[].source_hashes` 含 `code_tree_sha256`（父项目代码树）、`input_manifest_sha256`、`frames_manifest_sha256`、`dem_sha256`、`run_par_sha256` 与逐资产哈希。

## 10. 回滚演练

| 步骤 | 操作 | 结果 |
|---|---|---|
| 1 | `scripts\start_local.cmd v2-20261027` | 页面加载；光学图层可见（久隆 partial / 宁明 ready / 贵港 partial） |
| 2 | 停止服务，`scripts\start_local.cmd v1-20261027` | 页面加载；光学为 `missing` 且开关禁用并显示原因 |
| 3 | 再次 `scripts\start_local.cmd v2-20261027` | 恢复正常 |

两版包并存于 `outputs/web_bundle/`，回滚只改启动器目标，不删包、不改包。**已验证**（操作步骤见 `docs/runbook.md` §5）。

## 11. 零安装 standalone 交付

为降低使用复杂度，已从 `v2-20261027` 复制一个可直接分发的 standalone 文件夹：

`standalone/广西二维洪涝可视化_双击打开/`

| 项 | 结果 |
|---|---|
| 双击入口 | `打开可视化.cmd` |
| 服务程序 | Windows PowerShell 5.1 自带 `HttpListener`，无 Python/uv 依赖 |
| 监听地址 | `127.0.0.1:8765`；端口占用时自动顺延至 8775 |
| 文件数 | 2,916（含启动脚本、说明与 README） |
| 总大小 | 685,881,483 B，约 686 MB |
| HTTP smoke | `index.html`、`catalog.json`、`assets/app.js`、`manifest.json`、PNG、BIN 全部 HTTP 200 |
| 浏览器 smoke | Chromium + D3D11：初始 `jiulong/f0000`；切换贵港后 57 帧；控制台错误 0 |
| 安全 | 只绑定回环；路径穿越返回 404；不注册服务、不写注册表、不需要管理员权限 |

注意：不能直接双击 standalone 内的 `index.html`。浏览器 `file://` 安全策略会阻止读取 `catalog.json`。
必须双击 `打开可视化.cmd`，由本地回环 HTTP 服务提供页面。

## 12. 未验证 / 不在本次范围

| 项 | 状态 | 原因 |
|---|---|---|
| 30 分钟连续播放内存与稳定性 | **已验证（2026-10-30）** | 自动化门 `tests/e2e/test_e2e_longplay_30min.py` 实跑 1 802 s 通过，见 §8 |
| Edge 与 Chrome 双浏览器独立验证 | **未验证** | 仅 Chromium（同内核）实测 |
| 5 名试用者可读性测试（≥4 名 1 分钟内完成选地/选时/开 SAR） | **未验证** | 需人工组织，无自动化替代 |
| 真实 GPU 有头窗口数字 | **未测**（只在早期调试中测过 68 ms） | 有头会触发浏览器下载管理器（IDM）拦截，交付版已改为无头+GPU，结论不受影响 |
| 贵港站区 5 m 局部模型 / 区域—局部单向接口 | **不在范围** | 父项目 ADR-0005 已推迟 |
| DEM 垂直基准（G7）/ 许可外发 | **未闭合** | 父项目开放项；发布包内一律标注「未闭合」，仅内部研究使用 |
| 模型精度/验证结论 | **非本工程范围** | 本工程只做发布，不改求解器、不重跑模型 |

## 13. 复现命令

```powershell
cd 2d_hydro_project\flood_visualization
$env:PYTHONUTF8 = '1'

uv sync --frozen
uv run python scripts/fetch_vendor.py
uv run python -m floodviz inventory --config configs/default.yaml
uv run python -m floodviz prepare   --config configs/default.yaml --bundle-id v2-20261027
uv run python scripts/build_web.py  --bundle-id v2-20261027
uv run python -m floodviz validate  --bundle outputs/web_bundle/v2-20261027 --full
uv run python scripts/verify_bundle.py outputs/web_bundle/v2-20261027 --samples 40
uv run python -m pytest tests/integration/test_fixed_sampling.py -q -m slow   # 固定抽样回归
uv run python scripts/check_schemas.py
uv run ruff check src tests scripts
uv run python -m pytest -q                       # 277 passed
scripts\start_local.cmd v2-20261027              # http://127.0.0.1:8765/index.html
```

## 14. 证据文件索引

| 文件 | 内容 |
|---|---|
| `outputs/qa/validate_v2.json` | `validate --full` 完整报告（9 组、16 945 项） |
| `outputs/qa/perf_e2e.json` | 性能实测（真实 GPU 与 SwiftShader 双条件） |
| `outputs/qa/resource_integrity.json` | 280 个引用文件 HTTP 200 检查 |
| `outputs/qa/query_block_coverage.json` | 贵港 9 个块列点查命中 |
| `outputs/qa/stability_100_switches.json` | 100 次切地缓存与堆增长 |
| `outputs/qa/stability_playback_2min.json` | 120 s 连续播放缓存曲线 |
| `outputs/qa/layer_matrix_15.json` | 15 个「地点×预设」可见集合与禁用图层矩阵 |
| `outputs/qa/missing_layer_disabled.json` | 缺图层夹具：禁用状态与原因文案 |
| `outputs/qa/browser_memory.json` | 浏览器总内存（Sum Chromium RSS）实测 |
| `outputs/qa/metadata_size.json` | 每案例元数据体积预算 |
| `outputs/qa/playback_three_cases.json` | 三地各 30 轮播放稳定性 |
| `outputs/qa/evidence/*.png` | 8 张 e2e 截图（三地切换/点查/面积曲线/包络/缺帧/底图降级/断网/缺图层禁用） |
| `data/splits/sampling_index.json` | 固定验证抽样索引（14 132 点，跨版本可比的回归基线） |
| `docs/build_report.md` | 构建实测（体积/耗时/PNG 分布/空间残差） |
| `docs/optical_assets.md` | 光学/SAR 资产清单、覆盖、许可与署名 |
