---
doc_type: reference
status: active
topic: docs-index
canonical: true
scope: project
---

# docs/ 索引

执行状态与剩余项的事实 SSOT 是 `execution_status.md`，先读它；其余为阶段报告与决策记录。
质量门计数的事实 SSOT 是 `../data/catalog/mass_recheck_g15.json`；任何文档的计数与它冲突时以它为准。

## 状态与总览

- `execution_status.md` —— 执行状态与剩余项（对照终稿；SSOT）
- `final_report.md` —— 阶段成果报告（降级模式首轮，修订版）
- `stage_summary_3cases.md` —— 三案例阶段汇总（阶段 3~5 首轮）
- `rainfall_runoff_preprocessor.md` —— 降雨产流损失、土壤水文属性与初损网格化预处理器实施报告（针对产流水量虚高）

## 操作手册

- `RUNBOOK.md` —— 二维水动力情景回算操作手册（降级模式，阶段 6 雏形）

## 久隆治理（Phase 0–5，ADR-0010 口径）

- `jiulong_100m_rerun.md` —— 100 m 完整物理重跑（5 天 / 13 天窗 × 四边 FREE / 三边闭合；含可复现性与分辨率对退水异常的影响）
- `jiulong_phase34_candidates.md` —— 降雨损失预注册矩阵与排水/边界/退水审计；D1–D5 授权阻塞项
- `adr/ADR-0010-久隆直接降雨模型治理规范.md` —— 治理口径定版（四层 DEM 契约、分辨率口径、阈值预注册、派生标记、质量门、回滚、禁止表述）

## 贵港治理（Phase 0–5，ADR-0011 口径）

- `stage5_guigang_phase4_scenarios.md` —— Phase 2+4 物理双驱动重构实跑（`flow_only_v1` / `dualloss_v1` / `rainloss_high_v1`）；核心假设被数据否证，如实报告，未调阈值
- `adr/ADR-0011-贵港水动力推演零散伪影治理与驱动重构.md` —— 治理口径定版（展示质控命名与派生标记、降雨损失 E3 候选参数、S1 常水路线不可用证据与 Phase 3 阻塞、四情景质量门、验收目标达成/未达成逐条登记、计划三处错误更正）

## 贵港水力约束河槽重构（Phase H，ADR-0012/0013 口径）

- `adr/ADR-0012-贵港水力约束河槽重构.md` —— 水力几何先验 + 圆滑梯形刻蚀定版（三层 DEM 契约、预注册 priors、A–E 深度矩阵、滩地糙率首轮单变量口径、三层验证、禁止表述清单）
- `adr/ADR-0013-贵港组合驱动归因实验.md` —— 第二轮定版（首轮否定性结论登记、2×2 因子归因设计、GA-Mid 物理净雨路径 A 纪律、+48h 时基口径、方向性预期与禁止表述）
- `adr/ADR-0014-贵港排水连通治理.md` —— 第三/四轮定版（诊断探针定位、仅 DEM 约束下的垂直/水平双轨、priority-flood 主槽种子传播树 + canyon 守卫、路径 B 单用纪律；第四轮：入渗天花板 3.0 mm/h FAR 0.720、B 档密度否证、常水第二时点河槽独立验证 CSI ~0.3）
- 证据：`data/catalog/guigang_channel_priors.json`（预注册）、`guigang_centerline_audit.json`（OSM 中心线 + S1 距离 QA）、`guigang_channel_carve_validation.json`（几何 6 项 + 输水核验）、`guigang_dem_layers.json`（三层契约）、`guigang_round2_matrix.json` / `guigang_round3_drainage.json` / `guigang_round4_drainage.json`（二/三/四轮矩阵冻结）、`guigang_ponding_diagnosis.json`（积水诊断）、`guigang_drainage_audit.json` / `guigang_drainage_audit_B.json`（沟网审计）、`guigang_g7_s1_review.json`（G7/S1 口径复核 + 常水第二时点）；重构层在 `data/interim/guigang/channel_recon/`，物理净雨在 `outputs/preprocessor/guigang/`

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

## 学术成果驱动改进（Phase H，2026-11-22；ADR-0015~0018，计划 `plans/广西二维水动力模拟_学术成果驱动改进实现计划.md`）

- `adr/ADR-0015-学术借鉴与改进产品口径.md` —— 学术映射矩阵冻结 + FAT/成因归因/概率产品口径预注册（产品落盘目录决议、FAT 三档情景阈值、归因四分支规则、集合概率成员清单强制项、数据不可得降级纪律）
- `adr/ADR-0016-上游来水产汇流与降雨多源评估.md` —— GR4J 型集总产汇流预注册（文献中值参数 + OAT 敏感性 + IHC 暖启动/GA 映射双口径、bdy 格式源码实证、GloFAS 先验对照通道、MMF 协议、复合矩阵与授权 run 纪律、HVAR 顶托 Z_a/Z_b 定值规则）
- `adr/ADR-0017-垂直基准独立核证.md` —— ATL13/GSW 探针口径（offset 有界估计中位数±IQR、不写死 datum_offset、覆盖门 ≥10 轨、留出复核集）
- `adr/ADR-0018-概率化与联合阈值探索.md` —— 集合超越概率（质量门强制过滤成员）+ Copula 联合阈值（三族 AIC、信息扩散边缘、Bootstrap CI；探索性红线、禁业务告警）
- 新脚本：`stage6/{make_fat_products,make_genesis_attribution,ensemble_probability,copula_thresholds}.py`、`stage3/{build_inflow_hydrology,verify_inflow_vs_glofas,prep_imerg_rain,icesat2_datum_probe}.py`、run 队列 `stage3/queue_phase_h_runs.sh`
- 产品：`outputs/products/<site>/fat/`（61 个 run 的 FAT/历时/站址暴露三元组，2026-11-22 生成；久隆 R1/R2a/R2/R3/R3rec 5 个 run 尚未覆盖）、`outputs/products/guigang/genesis/`（origin_class.tif）、`outputs/products/ningming/probability/`（集合超越概率）
- 证据：`data/catalog/{fat_products_audit,guigang_genesis_rules,guigang_genesis_products,guigang_inflow_priors,inflow_audit,inflow_glofas_check,imerg_prep_audit,gx_datum_probe,ningming_probability,copula_thresholds_report,mmf_rainfall_evaluation}.json`

## 久隆双驱动重构与耦合治理（Phase I，2026-11-22~23；ADR-0019~0022，计划 `plans/code_artifact.md`）

- `adr/ADR-0019-久隆钦江上游来水与水文驱动.md` —— 小时级 Q(t) 与防重复计量分账（A1 外集水区入 QVAR，A2 域内 GA 净雨入 .rain，杜绝双重计算）
- `adr/ADR-0020-久隆河道亚网格与洪泛区耦合.md` —— SGC 亚网格河道表达（矩形全格/亚格，水力几何先验，四道护栏，免除人工刻蚀破坏地形）
- `adr/ADR-0021-久隆斑块物理归因与展示质控.md` —— P01–P08 八类成因逐斑块物理判定与连通性门控（打破一刀切滤波天花板）
- `adr/ADR-0022-久隆历史工况与边界重建.md` —— 2024 历史时空一致性、海陆/潮位边界闭合与 GLO-30 负值区融合
- 新脚本：`stage3/{build_jiulong_catchments,build_jiulong_inflow,prepare_jiulong_channel,prepare_jiulong_dem_layers_v2,prepare_jiulong_rainfall,run_jiulong_dual_forcing,validate_jiulong_hydrograph}.py`、`stage6/{audit_jiulong_coupling,compare_jiulong_scenarios,diagnose_jiulong_patch_attribution,validate_jiulong_inundation}.py`
- 产品与主情景：`outputs/jiulong/2024-09-yagi/scen_r3_dual_sgc_200m_sv6h/`（生产主情景，floodviz 默认）；证据 `data/catalog/{jiulong_inflow_audit,jiulong_river_geometry_audit,jiulong_dem_layers_v2,jiulong_dual_forcing_summary}.json`、`diagnostics/jiulong/coupling/v1/coupling_audit.json`、`diagnostics/jiulong/patch_attribution/class_summary.json`（逐类像元数，另有 `patch_attribution.csv` 与 `patch_class_map.tif`）

## 官方水位核验与 PipeNetGen 虚拟管网（Phase J/K，2026-11-25；ADR-0023~0025）

- `adr/ADR-0023-官方水位监测接入与连续水文核验.md` —— 三通道合法水位接入（YAML/CSV、SHP 档案、无加密 HTTP JSON；遇到 `#FontTag`/WAF 严格 fail-closed）、G7 基准保护与基准无关相对涨水 $\Delta H$ 评估、raw/snapped 双锚点虚拟水尺、2024/2026 工况隔离、`HVAR`/`QVAR` 边界同化骨架
- `adr/ADR-0024-PipeNetGen虚拟排水管网与协同排涝耦合.md` —— 零 GPLv3 开源合规栈（`networkx` + `swmmio` + `pyswmm`）、OSM 道路+DEM 逆向 Dijkstra 汇水树与 GB 50014 零逆坡管径定径、EPA-SWMM5 动态波与 LISFLOOD-FP `QVAR` 点汇/排口闭合耦合、合成资产强制隔离（`outputs/pipenet/` 与 `outputs/synthetic/`）
- `adr/ADR-0025-三案例管网耦合物理重跑的隔离算例与对照基线口径.md` —— 三案例真实 `lisflood.exe` 耦合重跑口径：全网格耦合内核分层（粗聚合诊断版 vs 全网格物理版）、自产基线承担对照（宁明原参照带 `diagnostic_only`）、质量门靠 `CASE_ROOTS` 白名单隔离、SGC 模式非零 `QVAR` 体积残差限制
- 新包与脚本：`src/hydro_refine/{core,monitors,pipenet,coupling}/`、`stage3/{ingest_official_gauges,fetch_five_province_hydro,extract_osm_urban_inputs,build_synthetic_pipenet,rebuild_pipenet_catalog,run_pipenet_coupled_lisflood}.py`、`stage6/{validate_water_levels,compare_pipenet_inundation,evaluate_coupled_reruns,verify_five_province_algorithms}.py`
- 证据与清单：`data/catalog/gauges/{guigang,ningming,jiulong}_gauges_normalized.json`、`data/catalog/{gauge_validation_results,synthetic_pipe_catalogs,pipenet_coupling_evaluation,pipenet_coupled_rerun,pipenet_coupled_rerun_evaluation,five_provinces_hydro_snapshot,five_provinces_algorithm_verification}.json`；耦合算例在 `outputs/pipenet/<case>/run_{baseline,coupled}_*/`（质量门白名单外）

## 决策记录（`adr/`，ADR-0001~0025 全量索引）

| ADR | 标题 | 核心决策要点 |
|---|---|---|
| [ADR-0001](adr/ADR-0001-工程结构与Python环境.md) | 工程结构与 Python 环境 | uv 包管理、阿里云镜像、平级 docs/scripts/tests 目录 |
| [ADR-0002](adr/ADR-0002-三案例CRS基线.md) | 三案例 CRS 基线 | 宁明/贵港 EPSG:32649、久隆 EPSG:32649（统一 UTM Zone 49N） |
| [ADR-0003](adr/ADR-0003-模型获取路径.md) | 模型获取路径 | LISFLOOD-FP 8.1.0 官方源码 Windows 本地构建 |
| [ADR-0004](adr/ADR-0004-公开数据降级模式与垂直基准策略.md) | 公开数据降级模式与垂直基准 | 情景级模拟定位，禁作资产级告警，垂直基准未闭合 |
| [ADR-0005](adr/ADR-0005-变电站整体化范围调整.md) | 变电站整体化范围调整 | AOI 包含站址与周边汇水拓扑，禁局部小切块 |
| [ADR-0006](adr/ADR-0006-结果完整性更正与质量门口径.md) | 结果完整性更正与质量门口径 | 质量门 PASS 仅代表水量闭合，FAIL-EXPLAINED 明示 |
| [ADR-0007](adr/ADR-0007-执行状态与降级边界.md) | 执行状态与降级边界 | 统一执行状态 SSOT，标注降级限制 |
| [ADR-0008](adr/ADR-0008-模型定版与桥涵结构使用限制.md) | 模型定版与桥涵结构限制 | LISFLOOD-FP 8.1.0 SHA-256 冻结，禁未独立验证的桥涵 |
| [ADR-0009](adr/ADR-0009-宁明河槽刻蚀停用与地形分层显示.md) | 宁明河槽刻蚀停用与地形分层 | 停用 244 m 下切人工刻蚀，真实 DEM 输入 |
| [ADR-0010](adr/ADR-0010-久隆直接降雨模型治理规范.md) | 久隆直接降雨模型治理规范 | 200 m 生产基线、四层 DEM 契约、display_qc 派生标记 |
| [ADR-0011](adr/ADR-0011-贵港水动力推演零散伪影治理与驱动重构.md) | 贵港零散伪影治理与驱动重构 | 识别离散积水根因，明确常水与外洪界限 |
| [ADR-0012](adr/ADR-0012-贵港水力约束河槽重构.md) | 贵港水力约束河槽重构 | 水力几何先验 + 圆滑梯形刻蚀，三层验证 |
| [ADR-0013](adr/ADR-0013-贵港组合驱动归因实验.md) | 贵港组合驱动归因实验 | 2×2 因子归因设计、GA 物理净雨 +48 h 时基口径 |
| [ADR-0014](adr/ADR-0014-贵港排水连通治理.md) | 贵港排水连通治理 | DEM 约束下垂直/水平双轨诊断探针、priority-flood 种子传播树 |
| [ADR-0015](adr/ADR-0015-学术借鉴与改进产品口径.md) | 学术借鉴与改进产品口径 | FAT 三档情景阈值、成因归因四分支规则、集合概率成员过滤 |
| [ADR-0016](adr/ADR-0016-上游来水产汇流与降雨多源评估.md) | 上游来水产汇流与降雨评估 | GR4J 产汇流预注册、GloFAS 对照通道、MMF 协议 |
| [ADR-0017](adr/ADR-0017-垂直基准独立核证.md) | 垂直基准独立核证 | ICESat-2 ATL13 / GSW 水面高程探针与有界偏差评估 |
| [ADR-0018](adr/ADR-0018-概率化与联合阈值探索.md) | 概率化与联合阈值探索 | 集合超越概率、Copula 联合阈值、学术探索红线 |
| [ADR-0019](adr/ADR-0019-久隆钦江上游来水与水文驱动.md) | 久隆钦江上游来水与驱动 | A1/A2 防重复计量分账、GA+SCS-UH 事件水文模型、小时级 Q(t) |
| [ADR-0020](adr/ADR-0020-久隆河道亚网格与洪泛区耦合.md) | 久隆河道亚网格与洪泛区耦合 | SGC 亚网格河槽表达、四道护栏、常水基流体检、免除人工刻蚀 |
| [ADR-0021](adr/ADR-0021-久隆斑块物理归因与展示质控.md) | 久隆斑块物理归因与展示质控 | P01–P08 八类成因逐斑块物理归因、连通性/深坑双门控 |
| [ADR-0022](adr/ADR-0022-久隆历史工况与边界重建.md) | 久隆历史工况与边界重建 | 2024 历史时空一致性、E/W 分水岭闭合、GLO-30 负值区基准融合 |
| [ADR-0023](adr/ADR-0023-官方水位监测接入与连续水文核验.md) | 官方水位监测接入与连续水文核验 | 三通道合法接入、反加密 fail-closed、相对涨水 $\Delta H$、双锚点虚拟水尺 |
| [ADR-0024](adr/ADR-0024-PipeNetGen虚拟排水管网与协同排涝耦合.md) | PipeNetGen 虚拟排水管网与协同排涝耦合 | OSM+DEM 重力 DAG、GB 50014 定径、PySWMM 1D 与 LISFLOOD-FP `QVAR` 耦合 |
| [ADR-0025](adr/ADR-0025-三案例管网耦合物理重跑的隔离算例与对照基线口径.md) | 三案例管网耦合物理重跑的隔离算例与对照基线口径 | 全网格耦合内核、自产基线、白名单隔离、SGC `QVAR` 残差限制 |

## 相关文档（不在本目录）

- 水位核验与管网耦合清单：`../data/catalog/{gauge_validation_results,synthetic_pipe_catalogs,pipenet_coupling_evaluation}.json`、`../configs/{gauges,pipenet}/`

- 久隆诊断包：`../diagnostics/jiulong/`（Phase 0：`root_cause_report.md`、`baseline_snapshot.json`、`patch_persistence.csv`、`dem_artifact_map.tif`、`resolution_comparison.json`）
- 贵港诊断包：`../diagnostics/guigang/`（Phase 0：`root_cause_report.md`、`baseline_snapshot.json`、`patch_persistence.csv`、`patch_tracks.csv`、`patch_summary.json`、`guigang_sink_analysis.json`）
- 质量门全表：`../data/catalog/mass_recheck_g15.json`（权威计数）
- 贵港 S1 验证：`../data/catalog/guigang_s1_validation.json`（四情景 POD/FAR/CSI、足迹口径、站点水位诊断）
- 久隆 GA 净雨重跑 S1 验证：`../data/catalog/jiulong_ga_v1_dt2p5_s1_validation.json`（物理求解器重跑 POD/FAR/CSI 与退水段动态）
- 贵港展示质控审计：`../data/catalog/guigang_display_qc_audit.json`（A/B/C 预注册参数与逐帧审计）
- 贵港河道路线审计：`../data/catalog/guigang_channel_route_audit.json`（Phase 3 阻塞证据）
- 数据元数据与清单：`../data/catalog/`（CRS 基线、事件候选、可达性、合规、缺口风险、硬件）
- 发布子工程：`../flood_visualization/`（其 `AGENTS.md`、`docs/`、`SPEC.md`）
- 计划基线：`../../plans/广西久隆_宁明_贵港_二维水动力模拟技术路线与工程落地方案_终稿.md`
- 久隆治理计划：`../../plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md`
- 贵港治理计划：`../../plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md`
- 汇报图使用说明：`../outputs/viz/汇报图使用说明.md`
