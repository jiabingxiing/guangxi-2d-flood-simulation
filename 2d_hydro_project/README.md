# 广西久隆 / 宁明 / 贵港 二维水动力洪涝模拟项目

> 计划基线：`../plans/广西久隆_宁明_贵港_二维水动力模拟技术路线与工程落地方案_终稿.md`（下称"终稿"）。
> 当前状态：**公开数据降级模式首轮情景（stage 3–5 degraded）**。原阶段 0 门禁未满足（ADR-0004），全部结果为情景级，不是业务预警系统；阶段 6 在线业务化未执行。结果完整性修正见 `docs/execution_status.md`。

## 阶段状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 启动/盘点 | AOI/事件、数据授权、基准、硬件盘点 | 两批完成；**原门禁未满足，降级门禁有条件通过**（ADR-0004，`docs/stage0_report.md` §6） |
| 1 久隆数据基线 | 数据 QA、结构/河槽、边界、站区基线 | **部分**：三案例 DEM 裁切+QA、ERA5 驱动、宁明 2026-08 验证数据登记；结构/站区资料缺 |
| 2 模型集成/基准 | 安装模型、官方案例、数值基准 | **部分**：LISFLOOD-FP 8.1.0 构建 + T016c 冒烟 + B1–B7 登记；B2 质量门 FAIL |
| 3 久隆历史事件 | 基线回算、率定/验证、风险映射 | **降级首轮**：5 情景；无近洪峰独立观测；站址值为 ±2 格面积级 |
| 4 宁明验证 | 河道—漫滩、喀斯特适用性 | **降级首轮**：时刻切片指标；喀斯特未建模；单事件 |
| 5 贵港区域+局部 | 区域模型 + 站区独立局部模型 | **降级首轮**：200 m 走廊；局部 5 m/30 m 未执行；WSE 仅诊断 |
| 6 业务化评估 | 时效、容错、交付 | **未执行**（在线预报 R10 排除） |

## 当前实现（降级模式）

| 案例 | 计划分辨率 | 实际分辨率 | 主要 run |
|---|---:|---:|---|
| 久隆 | 12.5 m | 200 m | `scen_free_200m`、`scen_ztide_200m` 等 5 个 |
| 宁明 | 25/12.5 m | 100 m | `scen_q4000_100m`、`scen_q6000_n100_100m` 等 |
| 贵港 | 30 m 区域 + 5 m 局部 | 200 m 走廊 | `scen_yagi_q8000/11500/15000_200m` |

实际配置写入 `configs/cases/*.yaml` 的 `implementation` 段；计划值保留在 `grid` 段，二者不得混用。

## 目录结构（终稿 §十）

```text
configs/        base.yaml + cases/{jiulong,ningming,guigang}.yaml + data_sources.yaml + model_manifest.yaml
data/
  raw/          只读原始资料（不入库）
  interim/      对齐、重投影、QA 中间件（不入库）
  processed/    冻结的模型输入（不入库）
  catalog/      元数据、许可、哈希、质量报告（入库）
src/
  acquisition/      数据目录、下载、导入
  preprocessing/    网格、DEM、结构、雨量、边界、糙率
  model/            成熟模型运行器、配置校验、基准测试
  postprocessing/   水深产品、水量账目、SAR 验证、风险映射
  reporting/        运行报告、地图与清单
tests/          数据契约、基准算例、回归测试
scripts/        盘点、预处理、基准、回算、验证、风险产品脚本
outputs/        <case>/<event>/<run_id>/（不入库）
logs/           （不入库）
docs/           阶段报告、ADR 决策记录
```

## 快速使用

```bash
# 建立环境（国内直连阿里云镜像；勿走失效代理）
uv --directory . sync

# 质量/追溯
uv --directory . run python scripts/stage0/make_code_manifest.py   # 代码/配置哈希
uv --directory . run python scripts/stage0/make_input_manifest.py  # 输入哈希
uv --directory . run python scripts/stage3/g15_mass_recheck.py     # 全 run 质量门（61 PASS / 3 FAIL-EXPLAINED / 0 FAIL，64 run）
uv --directory . run python -m pytest                              # 全量自动化测试（383 passed）

# 换盘/换位置后首步（详见根 AGENTS.md「可移植性」节）
uv --directory . run python scripts/maintenance/preflight.py        # 自检：陈旧 editable 安装 / 导入越界
uv --directory . run python scripts/maintenance/health_check.py     # 巡检：盘可读 / 备份时效 / 计数自洽
uv --directory . run python scripts/maintenance/make_backup.py      # 备份（代码包 + 不可再生中间件 + 证据）
uv --directory . run python scripts/maintenance/verify_backup.py --root <备份根>   # 备份哈希校验

# 三案例（降级模式；宁明默认基线 q4000/n0.035）
uv --directory . run python scripts/run_case.py ningming
uv --directory . run python scripts/run_case.py jiulong free
uv --directory . run python scripts/run_case.py guigang --qpeak 11500
```

> 运行前设 `PYTHONUTF8=1`；Windows 中文控制台注意编码。已有 `res.mass` 的目录不得覆盖（终稿 §10）。

> Windows 中文控制台注意：运行 Python 前设 `PYTHONUTF8=1`，避免 cp936 编码错误。

## 关键文档

- 阶段 0 报告与门禁评估：`docs/stage0_report.md`
- 执行状态与剩余项：`docs/execution_status.md`
- CRS/垂直基准基线：`data/catalog/aoi_crs_baseline.md`、`data/catalog/crs_check.json`
- 历史事件候选：`data/catalog/events_candidates.md`
- 数据源目录与可达性：`configs/data_sources.yaml`、`data/catalog/data_reachability.md`
- 合规清单：`data/catalog/compliance_checklist.md`
- 缺口与风险登记：`data/catalog/gaps_risks.md`
- 质量门全表：`data/catalog/mass_recheck_g15.json`
- 代码/输入清单：`data/catalog/code_manifest.json`、`data/catalog/input_manifest.json`
- 硬件盘点：`data/catalog/hardware_inventory.md`
- 决策记录：`docs/adr/`
