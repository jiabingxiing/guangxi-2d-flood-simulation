# flood_visualization 实施台账（Lead 维护）

> **现状注记（2026-11-25 复核）**：本文件记录的是 2026-10-27 首交付的历史构建/验收实测，正文保持原样。
> 其中提到的 `v1-20261027` 已按用户要求删除；**当前交付包是 `v10-20261122`**（见 `docs/runbook.md` §4），本文件里的 `v2-20261027`/`v3-20261028` 只是历史版本，不是现役。
> 保留数量由 `scripts/prune_bundles.py` 强制（硬下限 2 个），不要手工删除。

> 目标：完全实现 `plans/广西久隆_宁明_贵港_二维洪涝可视化实现计划.md` 的 M0–M3。
> 本文件记录**决策、变更与验证证据的索引**，细节见 `docs/data_contract.md`、
> `docs/build_report.md`（数据管道实测）、`docs/acceptance_report.md`（验收）、`docs/runbook.md`（操作）。

## 1. 关键决策（按时间）

| 日期 | 决策 | 依据 |
|---|---|---|
| 2026-10-27 | **不逐帧生成 XYZ 瓦片金字塔**；每帧渲染原生网格 1:1 预着色 PNG，用 MapLibre `image` source 按四角经纬度贴图 | 用户指示："不要逐帧生成，直接复用本项目已有的间隔 6 小时的推演结果"；贵港 30 m 57 帧合计约 940 MB ASCII，逐帧逐层级会放大到数十 GB，违背 ≤10 GB 预算 |
| 2026-10-27 | **子图（parts）自适应切分**，门禁保持"像元中心→经纬度→四角反贴图残差 ≤0.5 源像元"不放宽 | 单张四角双线性 warp 无法表达 UTM 等东坐标线弯曲；实测 贵港 30 m 1×1=1.261 px 超限 → 1×2=0.332 px 达标（久隆/宁明 1×1 约 0.05 px，贵港 200 m 1×1=0.180 px） |
| 2026-10-27 | `res.max` 是**子步累积包络**（不是逐帧取最大），只断言 `res.max ≥ 逐帧fmax` 单向性 | 实测差值单向为正：久隆 +0.060 m、宁明 +0.119 m、贵港 30 m +0.615 m；saveint=6 h 之间有 720/1440 子步，物理正确 |
| 2026-10-27 | 阈值统一 `depth > 阈值` **严格大于**，并剔除海域像元 | 与父项目统计口径一致；海域仅从统计剔除，数值块与点查保留真实值（模型已钳 0） |
| 2026-10-27 | 久隆/宁明 `origin_status=relative_only_candidate`：`time_origin` 与每帧 `timestamp` 一律为 null，UI 只显示 T+ 并标注起点依据候选 | 旧 meta 无规范 t0；禁止猜填绝对时间 |
| 2026-10-27 | 光学/背景层显示图长边 ≤2048 px、≤4 MB；SAR 二值图原始网格 1:1 | 背景层体积与解码时延；SAR 二值图本身很小（≤22 KB） |

## 2. 交付物清单

| 交付物 | 路径 | 状态 |
|---|---|---|
| 实施契约 | `SPEC.md` | **已完成**（含 parts 契约与实测切分） |
| 子项目配置 | `configs/{default,layers,basemaps,vendor}.yaml`、`configs/cases/*.yaml` | **已完成** |
| Python 预处理与发布 | `src/floodviz/**`（catalog/raster/quality/publishing/pipeline/cli） | **已完成**；`ruff check` 全绿 |
| 前端静态页面 | `web/index.html`、`styles.css`、`app.js`、`map.js`、`timeline.js`（vendor 固定版本） | **已完成**；29 条 e2e 通过 |
| 发布契约 | `schemas/*.json`、`docs/examples/*.example.json`、`docs/data_contract.md` | **已完成**并通过 schema 自检 |
| 前端资源固定 | `configs/vendor.yaml` + `scripts/fetch_vendor.py` + `web/vendor/VENDOR_MANIFEST.json` | **已完成**（MapLibre 5.24.0 / Proj4js 2.22.0，sha256 校验） |
| 构建与启动 | `scripts/build_web.py`、`scripts/start_local.cmd`、`standalone/广西二维洪涝可视化_双击打开/` | **已完成**；含回滚切换与无 Python standalone 启动 |
| 多源资产 | `data/processed/{optical,assets}` + `docs/optical_assets.md` | **已完成**：3 光学 + 3 SAR，`manifest.counts.total=6` |
| 测试与验收 | `tests/**`（277 passed）、`.github/workflows/ci.yml`、`docs/acceptance_report.md`、`docs/runbook.md` | **已完成**；含 standalone HTTP/Chromium smoke |
| 发布包 | `outputs/web_bundle/v1-20261027/`（基线）、`v2-20261027/`（交付版） | **已完成**，两版并存 |

## 3. 复现命令

```powershell
$env:PYTHONUTF8='1'
uv sync --frozen
uv run python scripts/fetch_vendor.py
uv run python -m floodviz inventory --config configs/default.yaml
uv run python -m floodviz prepare   --config configs/default.yaml --bundle-id v2-20261027
uv run python scripts/build_web.py  --bundle-id v2-20261027
uv run python -m floodviz validate  --bundle outputs/web_bundle/v2-20261027 --full
uv run python scripts/verify_bundle.py outputs/web_bundle/v2-20261027 --samples 40
uv run python scripts/check_schemas.py
uv run ruff check src tests scripts
uv run python -m pytest -q                                  # 277 passed
```

启动：`scripts\start_local.cmd v2-20261027` → `http://127.0.0.1:8765/index.html`
回滚：`scripts\start_local.cmd v1-20261027`

## 4. 交付状态（2026-10-27 收尾）

| 项 | 计划归属 | 状态 |
|---|---|---|
| 三地逐帧真实成果发布（21/19/57 帧，含对照 run 宁明 q4000） | M1 | **已完成**；`validate --full` 16 945 项 0 失败 |
| 光学三景导入（宁明 S2 2026-09-03 / 贵港 S2 2024-10-03 / 久隆本地 RGB 2026-04-03） | T07 / M2 | **已完成**；状态 `ready`/`partial`，均注明「背景角色、非洪峰观测」 |
| SAR 三地（久隆 S1 2024-09-17 / 宁明 GF-3 2026-08-24 / 贵港 S1 2024-09-12） | T07 / M2 | **已完成**；贵港 `partial`（仅 20.64% 覆盖，带足迹） |
| 浏览器 e2e（SPEC §10 的 18 条 + 图层矩阵 3 条 + 性能/资源/稳定性 8 条） | T09 / M3 | **已完成**，29 条全通过，8 张截图证据 |
| 性能实测（首屏/点查/界面响应/播放/内存） | T09 / M3 | **已完成**，见 `docs/acceptance_report.md` §8；30 分钟长播**未测** |
| 回滚演练（两版包并存） | T10 / M3 | **已完成**：`v1-20261027` 与 `v2-20261027` 并存，启动器切换验证通过 |
| 自动化测试 | 计划 §7.1 | **已完成**：277 passed（单元 210 + 集成 38 + e2e 29） |
| 5 名试用者可读性测试 | T11 / M3 | **未验证**（需人工组织，无自动化替代） |
| 30 分钟连续播放 + 浏览器总内存 ≤1.5 GB | T09 / M3 | **未测**（只做 2 分钟；GPU 显存需人工读浏览器任务管理器） |
| Edge 与 Chrome 双浏览器独立验证 | T09 / M3 | **未验证**（仅 Chromium 同内核实测） |
| M4 输出加密（1 h / 15–30 min） | M4（可选） | **未执行**；需用户认可机时成本；不声称已提高物理精度 |
| 贵港站区 5 m 局部模型 / 区域—局部接口 | 父项目 ADR-0005 | 不在本子项目范围（父项目已推迟） |
| DEM 垂直基准 / 许可外发 | 父项目 G7 / 许可 | 未闭合；发布包内一律标注「未闭合」，内部研究使用 |

> 结论：**计划 §1.1 的六项必须实现操作、§1.5 的非目标约束、M0–M3 的自动可验证部分已全部实现并留证**；
> 三项人工验收（试用者测试、30 分钟长播、双浏览器）如实标注为未验证，不冒充达标。

## 5. 发布包版本

| bundle_id | 内容 | 用途 |
|---|---|---|
| `v1-20261027` | 水深/DEM/SAR/包络，光学 `missing` | 回滚基线（已删除） |
| `v2-20261027` | v1 + 三地光学（`partial`/`ready`/`partial`），683.9 MB | 历史首交付（当前交付为 `v10-20261122`） |

两版并存，回滚只改启动器目标（见 `docs/runbook.md` §5）。
