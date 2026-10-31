# docs/ 索引

执行状态与剩余项的事实 SSOT 是 `execution_status.md`，先读它；其余为阶段报告与决策记录。
质量门计数的事实 SSOT 是 `../data/catalog/mass_recheck_g15.json`；任何文档的计数与它冲突时以它为准。

## 状态与总览

- `execution_status.md` —— 执行状态与剩余项（对照终稿；SSOT）
- `final_report.md` —— 阶段成果报告（降级模式首轮，修订版）
- `stage_summary_3cases.md` —— 三案例阶段汇总（阶段 3~5 首轮）

## 操作手册

- `RUNBOOK.md` —— 二维水动力情景回算操作手册（降级模式，阶段 6 雏形）

## 久隆治理（Phase 0–5，ADR-0010 口径）

- `jiulong_100m_rerun.md` —— 100 m 完整物理重跑（5 天 / 13 天窗 × 四边 FREE / 三边闭合；含可复现性与分辨率对退水异常的影响）
- `jiulong_phase34_candidates.md` —— 降雨损失预注册矩阵与排水/边界/退水审计；D1–D5 授权阻塞项
- `adr/ADR-0010-久隆直接降雨模型治理规范.md` —— 治理口径定版（四层 DEM 契约、分辨率口径、阈值预注册、派生标记、质量门、回滚、禁止表述）

## 阶段报告

- `stage0_report.md` —— 阶段 0：启动/盘点报告与门禁评估
- `stage1_data_baseline.md` —— 阶段 1：久隆数据基线与三案例公共数据
- `stage2_build_report.md` —— 阶段 2：LISFLOOD-FP 8.1 Windows 构建
- `stage2_benchmarks.md` —— 阶段 2：数值基准 B1–B5
- `stage3_ningming_run1.md` —— 阶段 3：宁明 2026-08 紫檀事件首轮情景回算
- `stage3_ningming_ensemble.md` —— 宁明率定敏感性集
- `stage3_ningming_convergence.md` —— 宁明换维度收敛试验
- `stage3_manning.md` —— 宁明空间 Manning 敏感性（Phase B0）
- `stage3_sensitivity.md` —— 网格/时间步敏感性（Phase B1）
- `stage4_karst.md` —— 宁明喀斯特标量入渗有界损失情景（Phase B3）
- `s1_validation.md` —— S1 水体提取验证链（阶段 3/5）

## 决策记录（`adr/`，ADR-0001~0010）

0001 工程结构与 Python 环境 ｜ 0002 三案例 CRS 基线 ｜ 0003 模型获取路径 ｜ 0004 公开数据降级模式与垂直基准 ｜ 0005 变电站整体化范围调整 ｜ 0006 结果完整性更正与质量门口径 ｜ 0007 执行状态与降级边界 ｜ 0008 模型定版与桥涵结构使用限制 ｜ 0009 宁明河槽刻蚀停用与地形分层显示 ｜ 0010 久隆直接降雨模型治理规范

## 相关文档（不在本目录）

- 久隆诊断包：`../diagnostics/jiulong/`（Phase 0：`root_cause_report.md`、`baseline_snapshot.json`、`patch_persistence.csv`、`dem_artifact_map.tif`、`resolution_comparison.json`）
- 质量门全表：`../data/catalog/mass_recheck_g15.json`（权威计数）
- 数据元数据与清单：`../data/catalog/`（CRS 基线、事件候选、可达性、合规、缺口风险、硬件）
- 发布子工程：`../flood_visualization/`（其 `AGENTS.md`、`docs/`、`SPEC.md`）
- 计划基线：`../../plans/广西久隆_宁明_贵港_二维水动力模拟技术路线与工程落地方案_终稿.md`
- 久隆治理计划：`../../plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md`
- 汇报图使用说明：`../outputs/viz/汇报图使用说明.md`
