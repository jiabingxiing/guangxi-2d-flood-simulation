---
doc_type: overview
status: active
topic: project-readme
canonical: true
scope: project
---

# 广西久隆 / 贵港 / 宁明 二维水动力模拟

基于 **LISFLOOD-FP 8.1.0** 的三地二维洪涝淹没模拟与治理工程：模型工程（`2d_hydro_project/`）+ 发布可视化子工程（`2d_hydro_project/flood_visualization/`）。

> ⚠️ **使用范围**：当前全部结果为**公开数据降级模式情景级**（ADR-0004），不是业务预警系统，不得用于业务告警或资产级判据；质量门 `PASS` ≠ 预测正确。

## 导航索引（Diátaxis 架构）

项目详细文档全景导航详见 **[2d_hydro_project/docs/MASTER.md](file:///e:/NWKYYSJZX/2026/1023%20%E4%B9%85%E9%9A%86%20%E8%B4%B5%E6%B8%AF%20%E5%AE%81%E6%98%8E%20%E4%BA%8C%E7%BB%B4%E6%B0%B4%E5%8A%A8%E5%8A%9B%E6%A8%A1%E6%8B%9F/2d_hydro_project/docs/MASTER.md)** 与 **[docs/MASTER.md](file:///e:/NWKYYSJZX/2026/1023%20%E4%B9%85%E9%9A%86%20%E8%B4%B5%E6%B8%AF%20%E5%AE%81%E6%98%8E%20%E4%BA%8C%E7%BB%B4%E6%B0%B4%E5%8A%A8%E5%8A%9B%E6%A8%A1%E6%8B%9F/docs/MASTER.md)**。

## 三地情景

| 地点 | 场次洪水 | 说明 |
| --- | --- | --- |
| 久隆 | 2024-09 摩羯台风 | R3 净雨+钦江来水双驱动主情景（SGC 亚网格河槽，ADR-0019~0022）；200 m 生产基线 |
| 贵港 | 2024-09 摩羯台风 | 伪影治理 / 水力约束河槽重构 / 组合驱动归因（ADR-0011~0014） |
| 宁明 | 2026-08 紫檀台风 | 独立时间轴，不与久隆/贵港共用；停用 244m 人工刻蚀（ADR-0009） |

三地**不是同一场洪水**，不共用绝对时间轴。

## 目录结构

```
├── AGENTS.md                  # 工作区导航与硬约束（给人与 AI 协作用）
├── docs/                      # 全局文档总纲（docs/MASTER.md）
├── 2d_hydro_project/          # 模型工程主目录
│   ├── scripts/               # stage3~stage6 治理与验证脚本
│   ├── configs/               # 情景配置；paths.example.yaml 为外部资源模板
│   ├── data/catalog/          # 质量门 SSOT（mass_recheck_g15.json 等）
│   ├── diagnostics/           # 久隆/贵港 诊断派生物（只读证据）
│   ├── docs/                  # 文档总纲 MASTER.md、执行状态、ADR-0001~0022
│   ├── outputs/display_qc/    # 展示质控派生层（非物理重算）
│   └── flood_visualization/   # 发布子工程 floodviz（只读父项目）
├── plans/                     # 技术路线与实施计划文档
└── reviews/                   # 评审材料与审计报告
```

## 质量门现状

权威值以 `2d_hydro_project/data/catalog/mass_recheck_g15.json` 为准：

**66 PASS / 4 FAIL-EXPLAINED / 0 FAIL**（70 个 run，全量闭合）

## 验证入口

```bash
# Python 运行前设 PYTHONUTF8=1（Windows 中文控制台）
uv --directory 2d_hydro_project run python -m pytest                    # 主工程全量测试（527 passed + 3 skipped）
uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py   # 质量门全表（66 PASS / 4 FAIL-EXPLAINED）
uv --directory 2d_hydro_project run python scripts/stage6/validate_guigang_s1.py  # 贵港 S1 校验
uv --directory 2d_hydro_project/flood_visualization run python -m pytest -m "not slow and not e2e"  # floodviz 测试（259 passed）
```

> 注意：`uv --directory 2d_hydro_project run pytest` 会因 trampoline 报错，必须用 `run python -m pytest`。

## 数据与可移植性

- **大体积数据不随仓库分发**：源 DEM 等外部资源按 `2d_hydro_project/configs/paths.example.yaml` 的说明放置（环境变量 `GX_DEM_SOURCE` 或 `configs/paths.local.yaml`）。
- 项目可整体复制到任意盘符运行：先跑 `scripts/maintenance/preflight.py` 自检，再 `uv sync` 重建虚拟环境。
- 备份根目录由环境变量 `GX_BACKUP_ROOT` 指定，备份须落在另一块物理盘。

## 文档索引

- 执行状态：`2d_hydro_project/docs/execution_status.md`
- docs 总索引：`2d_hydro_project/docs/README.md`
- 决策记录 ADR-0001~0018：`2d_hydro_project/docs/adr/`（0010 = 久隆直接降雨治理规范；0011~0014 = 贵港伪影/河槽/组合驱动/排水连通治理；0015~0018 = 学术借鉴口径、上游来水与降雨多源、垂直基准核证、概率化与联合阈值）
- 降雨下渗预处理器：`2d_hydro_project/docs/rainfall_runoff_preprocessor.md`
