# 广西久隆 / 贵港 / 宁明 二维水动力模拟

基于 **LISFLOOD-FP 8.1.0** 的三地二维洪涝淹没模拟与治理工程：模型工程（`2d_hydro_project/`）+ 发布可视化子工程（`2d_hydro_project/flood_visualization/`）。

> ⚠️ **使用范围**：当前全部结果为**公开数据降级模式情景级**（ADR-0004），不是业务预警系统，不得用于业务告警或资产级判据；质量门 `PASS` ≠ 预测正确。

## 三地情景

| 地点 | 场次洪水 | 说明 |
| --- | --- | --- |
| 久隆 | 2024-09 摩羯台风 | 200 m 生产基线；100 m 仅敏感性（ADR-0010） |
| 贵港 | 2024-09 摩羯台风 | 伪影治理 / 河槽重构 / 组合驱动归因（ADR-0011~0014） |
| 宁明 | 2026-08 紫檀台风 | 独立时间轴，不与久隆/贵港共用 |

三地**不是同一场洪水**，不共用绝对时间轴。

## 目录结构

```
├── AGENTS.md                  # 工作区导航与硬约束（给人与 AI 协作用）
├── 2d_hydro_project/          # 模型工程主目录
│   ├── scripts/               # stage3~stage6 治理与验证脚本
│   ├── configs/               # 情景配置；paths.example.yaml 为外部资源模板
│   ├── data/catalog/          # 质量门 SSOT（mass_recheck_g15.json 等）
│   ├── diagnostics/           # 久隆/贵港 诊断派生物（只读证据）
│   ├── docs/                  # 文档索引、执行状态、ADR-0001~0014
│   ├── outputs/display_qc/    # 展示质控派生层（非物理重算）
│   └── flood_visualization/   # 发布子工程 floodviz（只读父项目）
├── plans/                     # 技术路线与实施计划文档
└── reviews/                   # 评审材料
```

## 质量门现状

权威值以 `2d_hydro_project/data/catalog/mass_recheck_g15.json` 为准：

**61 PASS / 3 FAIL-EXPLAINED / 0 FAIL**（64 个 run）

## 验证入口

```bash
# Python 运行前设 PYTHONUTF8=1（Windows 中文控制台）
uv --directory 2d_hydro_project run python -m pytest                    # 主工程全量测试
uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py   # 质量门全表
uv --directory 2d_hydro_project run python scripts/stage6/validate_guigang_s1.py  # 贵港 S1 校验
uv --directory 2d_hydro_project/flood_visualization run python -m pytest -m "not slow and not e2e"  # floodviz 测试
```

> 注意：`uv --directory 2d_hydro_project run pytest` 会因 trampoline 报错，必须用 `run python -m pytest`。

## 数据与可移植性

- **大体积数据不随仓库分发**：源 DEM 等外部资源按 `2d_hydro_project/configs/paths.example.yaml` 的说明放置（环境变量 `GX_DEM_SOURCE` 或 `configs/paths.local.yaml`）。
- 项目可整体复制到任意盘符运行：先跑 `scripts/maintenance/preflight.py` 自检，再 `uv sync` 重建虚拟环境。
- 备份根目录由环境变量 `GX_BACKUP_ROOT` 指定，备份须落在另一块物理盘。

## 文档索引

- 执行状态：`2d_hydro_project/docs/execution_status.md`
- docs 总索引：`2d_hydro_project/docs/README.md`
- 决策记录 ADR-0001~0014：`2d_hydro_project/docs/adr/`（0010 = 久隆直接降雨治理规范；0011~0014 = 贵港伪影/河槽/组合驱动/排水连通治理）
- 降雨下渗预处理器：`2d_hydro_project/docs/rainfall_runoff_preprocessor.md`
