# flood_visualization —— 广西久隆 / 宁明 / 贵港 二维洪涝可视化发布工程

把父项目（`2d_hydro_project/`）已完成的 LISFLOOD-FP 情景成果发布为**本地网页**：
三地切换、逐帧淹水回放、多源图层叠加、像元数值查询、面积—时间曲线、质量与来源面板。

> **性质**：研究性情景成果（公开数据降级模式），**不是业务预警系统**，不得用于业务告警或资产安全判据。
> **只读父项目**：本子项目不重跑模型、不修改父项目依赖与输出。

---

## 1. 直接打开已生成网页（推荐）

如果只是查看结果，不需要安装 Python、uv 或 Node.js。

1. 打开 `standalone\广西二维洪涝可视化_双击打开\`。
2. 双击 **`打开可视化.cmd`**。
3. 浏览器自动打开本地网页。
4. 关闭命令窗口即可停止服务。

> 不要直接双击 `index.html`。浏览器会阻止 `file://` 页面读取 `catalog.json`。
> 启动器使用 Windows 自带 PowerShell。本地服务只绑定 `127.0.0.1`。
> 端口 8765 被占用时，启动器会自动尝试 8766–8775。

## 2. 从源数据重新构建

> **已有发布包也可用**：`scripts\start_local.cmd` 会服务最新的 `v3-20261028`，
> 浏览器打开 <http://127.0.0.1:8765/index.html> 即可。下面是从零重建的完整流程。

```powershell
cd 2d_hydro_project\flood_visualization
$env:PYTHONUTF8 = '1'

uv sync --frozen                                  # 安装/校验锁定依赖（含 dev）
uv run python scripts/fetch_vendor.py             # 首次：抓固定版本前端资源
uv run python -m floodviz prepare --config configs/default.yaml --bundle-id v3-20261028
uv run python scripts/build_web.py --bundle-id v3-20261028
uv run python scripts/sync_standalone.py --bundle-id v3-20261028   # 同步双击版（保留启动器）
uv run python -m floodviz validate --bundle outputs/web_bundle/v3-20261028 --full
```

然后双击或在终端运行：

```powershell
scripts\start_local.cmd              # 服务最新包（v3-20261028）
scripts\start_local.cmd v2-20261027  # 回滚到上一版
```

浏览器打开 **<http://127.0.0.1:8765/index.html>**（只绑回环，不对局域网开放）。

---

## 3. 常用命令

| 目的 | 命令 |
|---|---|
| 扫描源 run（帧数/栅格头/时间） | `uv run python -m floodviz inventory --config configs/default.yaml` |
| 生成发布包 | `uv run python -m floodviz prepare --config configs/default.yaml --bundle-id <id>` |
| 快速冒烟（单案例、3 帧） | 追加 `--only jiulong --frames-limit 3` |
| 复制页面与 vendor 进包 | `uv run python scripts/build_web.py --bundle-id <id>` |
| 发布门禁（数值/统计/空间） | `uv run python -m floodviz validate --bundle outputs/web_bundle/<id> --full` |
| 独立复核（Lead 侧交叉验证） | `uv run python scripts/verify_bundle.py outputs/web_bundle/<id> --samples 40` |
| 契约示例与 schema 自检 | `uv run python scripts/check_schemas.py` |
| 固定前端资源（离线可复用缓存） | `uv run python scripts/fetch_vendor.py [--offline]` |
| 单元 + 集成测试（约 5 s） | `uv run python -m pytest -m "not slow and not e2e" -q` |
| 浏览器端到端测试（约 60 s） | `uv run python -m pytest tests/e2e -q` |
| 全部测试（含性能/资源，约 5 min） | `uv run python -m pytest -q` |
| 代码质量 | `uv run ruff check src tests scripts` |
| 启动服务 | `scripts\start_local.cmd [bundle_id]` |

---

## 4. 目录

```text
flood_visualization/
├── SPEC.md              # 实施契约（唯一真源：目录/字段/门禁/交互）
├── configs/             # 项目、案例、图层、前端版本配置
├── src/floodviz/        # 预处理与发布（catalog / raster / quality / publishing / pipeline）
├── web/                 # 静态前端（无构建链，vendor 固定版本，禁 CDN）
├── scripts/             # 抓 vendor、构建页面、schema 自检、独立复核、启动器
├── schemas/             # 发布包 JSON Schema
├── tests/               # 单元 210 / 集成 38 / e2e 29（共 277 passed）
├── data/                # 只读引用、裁剪产物、外部条款（二进制不入库）
├── docs/                # 数据契约、许可、实施台账、构建与验收报告、运行手册
└── outputs/web_bundle/  # 发布包（不入库；v1 基线 + v2 交付版并存以支持回滚）
```

---

## 5. 发布包内有什么

```text
index.html  assets/{styles.css,app.js,map.js,timeline.js, vendor/**}
catalog.json                          # 三案例、图层状态、图例、底图、质量
cases/<case>/{boundary,sites}.geojson
cases/<case>/<run>/{timeline,statistics}.json
frames/<case>/<run>/<frame_id>_r<r>c<c>.png     # 逐帧显示图（原生网格 1:1，非 XYZ 金字塔）
frames/<case>/{dem_r…,sar_r…,optical/…}.png     # 地形 / SAR / 可见光
numeric/<case>/<run>/<frame_id>/<by>/<bx>.bin   # float32 保真数值块（点查用）
licenses/LICENSES.md + 第三方许可证
manifest.json                          # 内容哈希、构建参数、质量与门禁摘要
```

**为什么不是 XYZ 瓦片**：用户决定直接复用已有 6 h 间隔的真实推演帧。每帧渲染成原生网格
1:1 的预着色 PNG，用 MapLibre `image` source 按四角经纬度贴图；贵港 30 m 网格跨度大，
单张四角贴图残差 1.26 源像元，故沿列切 1×2（0.332 px），仍满足 ≤0.5 源像元门禁。
详见 `SPEC.md` §3.1、`docs/build_report.md`。

---

## 6. 关键口径（阅读成果前必看）

| 事项 | 口径 |
|---|---|
| 三地不是同一场洪水 | 久隆/贵港 = 2024-09 台风摩羯；宁明 = 2026-08 台风紫檀。**不共用绝对时间轴** |
| 时间起点 | 久隆/宁明旧 meta 无规范 t0 → 只显示 `T+小时`，绝对时间为 `null`，界面注明“起点为候选依据” |
| 时间分辨率 | 6 h/帧离散回放；播放速度不改变物理时间分辨率；**不插值伪造中间帧** |
| 最大包络 | 来自模型 `res.max`，是子步累积包络（含 6 h 采样间隙的瞬时峰值），**不是**可见帧的最大值 |
| 面积阈值 | `depth > 阈值` 严格大于，剔除 NaN 与海域像元；零面积如实显示 0 |
| 质量门 | `PASS` 不等于“预测正确”；`FAIL-EXPLAINED` 不计通过；无记录显示 `unrecorded` |
| 影像时间 | 采集时刻固定，**不随模拟滑块变化**；灾后影像不得配给灾前时刻 |
| 点查数值 | 来自 float32 保真块（与源 `.wd` 逐位一致），**不从 PNG 颜色反算** |

---

## 7. 交付状态（2026-10-27）

| 项 | 结果 |
|---|---|
| 发布包 `v2-20261027` | 683.9 MB，2 912 文件，三地 116 帧（久隆 21 / 宁明 19+19 / 贵港 57） |
| 发布门禁 `validate --full` | **ok=True，16 945 项检查 0 失败** |
| 数值/统计/空间 | 抽样 3 364 点误差 **0.0 m**；116 帧面积复算相对差 **0.0**；贴图残差 ≤**0.338 源像元** |
| 自动化测试 | **277 passed**（单元 210 + 集成 38 + e2e 29） |
| 浏览器 e2e | **29 条**：SPEC §10 的 18 条、图层矩阵 3 条、性能/资源/稳定性 8 条，全通过 |
| 性能（真实核显，无头+GPU） | 首帧可交互 1.58 s ≤5 s；点查 p95 10.6 ms ≤200 ms；切帧 p95 81.2 ms ≤300 ms；播放 1.81 帧/s |
| 回滚 | `v2-20261027` 与 `v3-20261028` 并存，启动器切换已验证；`v1-20261027` 已于 2026-10-28 按用户要求删除 |
| 零安装交付 | `standalone\广西二维洪涝可视化_双击打开\打开可视化.cmd`，2,916 文件，约 686 MB，已验证 HTTP 与 Chromium smoke |
| 未验证 | 30 分钟长播内存、Edge/Chrome 双浏览器、5 名试用者可读性测试（详见验收报告 §12） |

---

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [SPEC.md](SPEC.md) | 实施契约（目录、字段、门禁、交互清单） |
| [AGENTS.md](AGENTS.md) | 路径与只读规则、命令、事实纪律 |
| [docs/data_contract.md](docs/data_contract.md) | 发布包数据契约（人读版） |
| [docs/implementation_log.md](docs/implementation_log.md) | 决策、变更台账与交付状态 |
| [docs/build_report.md](docs/build_report.md) | 构建实测（体积、耗时、空间残差、PNG 分布） |
| [docs/optical_assets.md](docs/optical_assets.md) | 光学/SAR 资产清单、覆盖、许可与署名 |
| [docs/acceptance_report.md](docs/acceptance_report.md) | **验收证据**（数值/统计/空间/性能/e2e/未验证项） |
| [docs/runbook.md](docs/runbook.md) | 构建、启动、回滚、故障排查、零安装 standalone 使用 |
| [docs/licenses.md](docs/licenses.md) | 软件与数据授权状态 |

---

## 9. 许可

本子项目代码为 BSD-3-Clause（见 [LICENSE](LICENSE)）。数据与第三方组件授权**不在**该许可覆盖范围内：
模型结果、DEM、Sentinel-1/2、GF-3、Esri 底图各有条款，详见 [docs/licenses.md](docs/licenses.md)。
**LISFLOOD-FP 8.1.0 许可在父项目登记为 UNRESOLVED，不得外发。**
