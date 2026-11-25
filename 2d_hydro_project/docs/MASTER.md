---
doc_type: reference
status: active
topic: master-index
canonical: true
scope: project
---

# 项目全景导航与文档总纲（MASTER.md）

本文档是广西久隆/宁明/贵港二维水动力模拟项目的**统一文档总纲与阅读导航（SSOT Index）**，遵循 **Diátaxis 架构规范**，将项目全量文档划分为教程（Tutorial）、操作指南（How-to）、参考手册（Reference）和原理解释（Explanation）四大类型。

> [!IMPORTANT]
> **两大单一权威事实来源（SSOT）**：
> 1. **执行状态与剩余项 SSOT**：[execution_status.md](execution_status.md)。任何文档记录的阶段完成状态与此冲突时，以此为准。
> 2. **质量门计数权威 SSOT**：`2d_hydro_project/data/catalog/mass_recheck_g15.json`（当前权威值：**66 PASS / 4 FAIL-EXPLAINED / 0 FAIL，共 70 个 run**）。任何文档提及的质量门总数与此冲突时，以此 JSON 为准。

---

## 1. 教程（Tutorials）—— 快速入门与环境巡检

用于指导新开发者、评审专家或迁移环境后快速上手与验证系统可用性。

- **工作区导航与约束**：[AGENTS.md](../../AGENTS.md)
  - 核心硬约束：严禁覆盖已有 `res.mass`、`lisflood.exe` 源码禁止重跑、环境统一使用 `uv`、设置 `PYTHONUTF8=1`。
- **环境自检与可移植性维护**：
  - `uv --directory 2d_hydro_project run python scripts/maintenance/preflight.py`（环境检查）
  - `uv --directory 2d_hydro_project run python scripts/maintenance/health_check.py`（日常健康巡检）
- **发布子工程入门**：[flood_visualization/AGENTS.md](../flood_visualization/AGENTS.md)
  - 只读访问父项目数据，零绝对路径，支持单机离线查看三案例情景。

---

## 2. 操作指南（How-to Guides）—— 任务操作手册与脚本流

面向工程运维与复现计算的实用指导。

- **情景回算操作手册**：[RUNBOOK.md](RUNBOOK.md)
  - 覆盖宁明、久隆、贵港三地的情景组装、边界条件（BCI）、雨量驱动与运行调用。
- **降雨产流与土壤下渗预处理**：[rainfall_runoff_preprocessor.md](rainfall_runoff_preprocessor.md)
  - 运行 `uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case <jiulong|guigang|ningming>` 生成物理净雨序列（模块在工程根 `rainfall_runoff_preprocessor/`，不在 `scripts/` 下）。
- **展示质控层生产与审计（Phase 1）**：
  - 久隆展示质控：`scripts/stage6/clean_jiulong_display.py`
  - 贵港展示质控：`scripts/stage6/clean_guigang_display.py`
  - 硬约束：带 `not_physical_recalculation=true`，派生目录禁止写入 `res.mass`。
- **质量门全量验证**：
  - `uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py`
- **全量自动化测试**：
  - `uv --directory 2d_hydro_project run python -m pytest`（实测 587 passed + 3 条件 skipped，2026-11-25）
- **官方水位核验与管网耦合（Phase J/K）**：
  - `scripts/stage6/{validate_water_levels,compare_pipenet_inundation,evaluate_coupled_reruns,verify_five_province_algorithms}.py`
  - 耦合算例落在 `outputs/pipenet/<case>/run_{baseline,coupled}_*/`，在质量门白名单外，不并入三案例 outputs（ADR-0025）

---

## 3. 参考手册（Reference）—— 权威事实、数据契约与决策链

提供精确的技术参数、规范契约与历史决策记录。

- **文档中心总索引**：[README.md](README.md)
- **数据目录与 CRS 规格**：
  - [aoi_crs_baseline.md](../data/catalog/aoi_crs_baseline.md)（三案例坐标系：EPSG:32649）
  - [compliance_checklist.md](../data/catalog/compliance_checklist.md)（合规清单）
  - [gaps_risks.md](../data/catalog/gaps_risks.md)（公开数据降级缺口与风险）
- **架构决策链（ADR-0001 至 ADR-0025）**：
  - **基础架构与环境**：[ADR-0001](adr/ADR-0001-工程结构与Python环境.md)、[ADR-0002](adr/ADR-0002-三案例CRS基线.md)、[ADR-0003](adr/ADR-0003-模型获取路径.md)、[ADR-0004](adr/ADR-0004-公开数据降级模式与垂直基准策略.md)、[ADR-0005](adr/ADR-0005-变电站整体化范围调整.md)、[ADR-0006](adr/ADR-0006-结果完整性更正与质量门口径.md)、[ADR-0007](adr/ADR-0007-执行状态与降级边界.md)、[ADR-0008](adr/ADR-0008-模型定版与桥涵结构使用限制.md)。
  - **宁明与早期治理**：[ADR-0009](adr/ADR-0009-宁明河槽刻蚀停用与地形分层显示.md)。
  - **久隆治理体系**：[ADR-0010](adr/ADR-0010-久隆直接降雨模型治理规范.md)、[ADR-0019](adr/ADR-0019-久隆钦江上游来水与水文驱动.md)、[ADR-0020](adr/ADR-0020-久隆河道亚网格与洪泛区耦合.md)、[ADR-0021](adr/ADR-0021-久隆斑块物理归因与展示质控.md)、[ADR-0022](adr/ADR-0022-久隆历史工况与边界重建.md)。
  - **贵港治理体系**：[ADR-0011](adr/ADR-0011-贵港水动力推演零散伪影治理与驱动重构.md)、[ADR-0012](adr/ADR-0012-贵港水力约束河槽重构.md)、[ADR-0013](adr/ADR-0013-贵港组合驱动归因实验.md)、[ADR-0014](adr/ADR-0014-贵港排水连通治理.md)。
  - **学术驱动与概率产品**：[ADR-0015](adr/ADR-0015-学术借鉴与改进产品口径.md)、[ADR-0016](adr/ADR-0016-上游来水产汇流与降雨多源评估.md)、[ADR-0017](adr/ADR-0017-垂直基准独立核证.md)、[ADR-0018](adr/ADR-0018-概率化与联合阈值探索.md)。
  - **监测核验与城市管网**：[ADR-0023](adr/ADR-0023-官方水位监测接入与连续水文核验.md)、[ADR-0024](adr/ADR-0024-PipeNetGen虚拟排水管网与协同排涝耦合.md)、[ADR-0025](adr/ADR-0025-三案例管网耦合物理重跑的隔离算例与对照基线口径.md)。

---

## 4. 原理解释（Explanation）—— 方案设计、根因分析与技术推演

深入阐述算法机理、为什么这样决策以及实验物理背景。

- **基线方案总规划**：
  - [终稿方案](../../plans/广西久隆_宁明_贵港_二维水动力模拟技术路线与工程落地方案_终稿.md) —— 项目顶层技术路线基线。
- **专项治理实施计划**：
  - [久隆伪影诊断到全链条治理计划](../../plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md)
  - [久隆双驱动重构与离散水点治理详细计划（code_artifact.md）](../../plans/code_artifact.md) —— 从“纯雨驱动演示”到“水文水动力耦合”的核心架构设计。
  - [贵港零散水点治理与双驱动重构计划](../../plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md)
  - [学术成果驱动改进实现计划](../../plans/广西二维水动力模拟_学术成果驱动改进实现计划.md)
- **根因诊断分析报告**：
  - 久隆根因诊断报告：`2d_hydro_project/diagnostics/jiulong/root_cause_report.md`（高程阶梯伪影、海面未屏蔽、四边假排水等五大机理根因）
  - 贵港根因诊断报告：`2d_hydro_project/diagnostics/guigang/root_cause_report.md`（低洼汇聚、常水与外洪混淆、入流时基偏差）
- **阶段成果与验证汇总**：
  - [final_report.md](final_report.md)（阶段成果报告）
  - [stage_summary_3cases.md](stage_summary_3cases.md)（三案例阶段汇总）
  - [s1_validation.md](s1_validation.md)（Sentinel-1 遥感水体提取与足迹验证）
