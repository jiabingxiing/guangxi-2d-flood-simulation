---
doc_type: reference
status: active
topic: execution-status
canonical: true
scope: project
---

# 执行状态与剩余项（对照终稿）

更新：2026-11-25 ｜ 模式：公开数据降级（ADR-0004/0005/0008/0023/0024/0025）｜ **不是计划全部完成，不是业务预警系统**

## 0. Phase C 增量（2026-10-27，本轮）

| Phase | 内容 | 状态 |
|---|---|---|
| C0 | `run_case.py` 统一入口路径缺陷修复（`parents[2]`→`parents[1]`，入口此前从未真正可用） | **已完成**并用其实跑复现重跑 |
| C1 | 13.4 可复现门补齐：宁明/贵港复现重跑（`--run-tag repro`） | **已完成**：三案例 `res.max` **逐位一致** + `res.mass` 末行一致（`reproducibility_test.json`，`repro_recheck.py`） |
| C2 | B4 收尾：久隆退水期 S1 弱验证（终稿 §3.2 降级口径） | **已完成（WEAK）**：free 延长至 09-18（ERA5 延长段 84 mm）；过境帧对照 POD 0.387 / FAR 0.982 / CSI 0.018（模型 260.8 vs S1 12.2 km²）；**模型 09-10→09-17 面积 +27.7 km² 不降反增**——无排水网退水过慢定量证实；延长 run E_mass 5.3e-4 PASS。见 `jiulong_recession_s1.json` |
| C3 | 贵港网格敏感性去混杂：河槽宽按米参数化（200 m 下与 v2 逐位同）+ 等宽 100 m 对照（wfix） | **已完成**：同宽下湿面积 +0.7%（544.13 vs 540.16 km²）——**此前 +16.3% "网格敏感性"几乎全是宽度伪影（300 vs 600 m）**；最大水深 −19.8%、站址像元 10.46→9.06 m 仍敏感 |
| C4 | Phase B5：贵港 30 m 全域长跑（2.75M 格，河槽宽 ≈630 m 按米一致） | **已完成（2026-10-27）**：2230×1233 格、自适应步长 ~1.7 s、墙钟 43069 s（12.0 h）、57 帧、**E_mass 5.7e-5 PASS**；湿面积 493.36 km²（**−8.7%** vs 200 m 540.16）、最大水深 19.44 m（+1.3%）、站址像元 9.074 m / r200 9.108 m（**−13.2%**）。与等宽 100 m（9.059/9.091）差 0.2% → **站址水深 100 m 起收敛，200 m 偏大 15%**；面积在 490–545 km²（±6%）温和收敛 |
| C5 | ADR-0008 模型定版 | **已完成**：8.1.0 定版（SHA-256 冻结）；禁 bridge/orifice（B8b 未独立验证），结构限 DEM 加高 + weirfile |
| C6 | 汇报可视化修订：make_viz 六处旧口径按 ADR-0006 重制 fig1–6 + 使用说明 | **已完成**（res.max 验证/事后阈值 0.30/+13.08 率定/1.68 m 站址/86%/「质量门全过」全部清除；GIF 几何未变未重制） |

## 0b. Phase D 增量（2026-10-28，地形分层与刻蚀审计）

| Phase | 内容 | 状态 |
|---|---|---|
| D1 | 刻蚀审计脚本 `scripts/stage3/dem_channel_audit.py` | **已完成**：19 个宁明 run 全判 FAIL（距常水>250 m 占 64%、最大下切 244.2 m、176/176 列常值） |
| D2 | 宁明取消刻蚀，`--channel {off,watermask,centerline}`，默认 `off`；刻蚀前强制过审计门 | **已完成** |
| D3 | `nocut-v1` 无刻蚀 run（q6000/n0.10/100 m） | **已完成**：`E_mass=2.93e-4` PASS；湿面积 119.15 km²（0.05 m）、CSI 0.334（旧口径 res.max，非时刻切片） |
| D4 | 东缘边界短路诊断 | **已完成**：注入口在东缘 FREE 边界上，四边 FREE 时 83.0% 注入体积从边界排出；关闭东缘出流后退出比降为 53.4%、蓄量由 3.27e8 升至 8.28e8 m³ |
| D5 | 可视化地形分层：`terrain_source` / `terrain_edit` / 改名 `dem` | **已完成**：v3-20261028 包 `validate --full` 通过 |

**必须随成果标注**：
- 网页「DEM 高程」旧图层实际是模型输入地形，含 810 个刻蚀像元（8.10 km²，最大下切 244.19 m），不是源 DEM。
- 宁明既有 CSI 0.357 / 面积偏比 1.94 同时受「人造河槽」与「注入口/出流边界重合」两项污染，不得作为模型物理性能结论。
- `nocut-v1` 的 `E_mass` 达标不能证明真实地形能出流（0.83 的注入量直接从东缘边界排出）。

## 0c. Phase E 增量（2026-10-31，久隆治理）

计划基线：`plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md` v3.0。治理口径以 ADR-0010 定版。
本节状态为 **2026-10-31 21:28 快照**。Phase 0/1/2/3-4 由并行工作流执行；凡标「部分完成/未完成」的项，以落盘文件是否存在与实跑结果为准复核，不得据本节推断已完成的物理工作。

### 0c-1 分阶段状态

| Phase | 内容 | 状态 |
|---|---|---|
| E0 | 分辨率决策（计划 §1.4）：定方案 A，200 m 为生产治理基线、100 m 仅敏感性对照 | **部分完成**：ADR-0010 决策项 2 已按方案 A 定版为默认工作口径；但 Phase 0 产出 `resolution_comparison.json` 的 `decision_input.status` 仍记「未冻结：方案 A/B 需用户确认后才能冻结展示阈值」。**用户确认属未确认项**；方案 B 需用户单独批准 |
| E0 | 基线事实冻结：200 m 生产基线 `scen_free_200m_sv6h`（21 帧、`sim_time=432000`、湿面积 263.24 km²、`E_mass=4.893e-4` PASS） | **已验证**（本次实测复核 `res.max` 与栅格头） |
| E0 | 退水延长基线 `scen_free_200m_sv6h_recession`（53 帧、`sim_time=1123200`、`E_mass=5.3e-4` PASS） | **已验证** |
| E0 | 100 m 敏感性基线 `scen_free_100m_dt2p5_sv6h_realdem`（21 帧、湿面积 271.07 km²、`E_mass=3.089e-4` PASS） | **已验证** |
| E0 | Phase 0 诊断包 `diagnostics/jiulong/`（基线快照、斑块持续性 CSV、DEM 异常分类栅格、根因报告） | **已完成**：`diagnostics/jiulong/phase0-v1/` 已落 `baseline_snapshot.json`、`resolution_comparison.json`、`dem_artifact_map.tif`、`patch_persistence.csv`、`patch_frame_summary.csv`、`patch_tracks.csv`、`patch_summary.json`、`root_cause_report.md` 八件，`data/catalog/jiulong_improvement_manifest.json` 已落盘（含 `evidence_levels`、`inputs`、`code`、`artifacts`、`open_blockers`、`known_doc_discrepancies`） |
| E1 | `display_qc` 只读展示质控层（A/B/C 三档预注册、逐帧审计、派生标记） | **已完成（产出）**：`outputs/display_qc/scen_free_200m_sv6h_recession/{A,B,C}/` 三档已产出（含 `meta.json`+`results/`+`audit/`），`data/catalog/jiulong_display_qc_audit.json` 已落盘；raw 前后哈希 `unchanged=true`，三档 `blocked_for_default_publish` 均为 `false`，派生目录内 `res.mass` 计数为 0，派生标记字段齐全。`tests/test_display_qc.py` 已落盘（52 项） |
| E1 | 旧展示层 `scen_free_100m_dt2p5_sv6h_realdem_cleaned` 退役（写死 4/9 像元、写死 100 m 面积、声称「ARR 2019 规定」） | **已完成（口径）**：ADR-0010 决策项 8 判定不合规并明令不得作默认生产层；**该目录物理保留未删**，前端口径切换见 E5 |
| E2 | 四层 DEM 契约（`DEM_RAW`/`DEM_REPAIRED`/`DEM_HYDRO`/`DEM_HYDRAULIC`） | **已完成（口径）**：ADR-0010 决策项 1 定版 |
| E2 | 四层 DEM 实际生成与候选填洼 | **已完成（产出）**：`data/interim/jiulong/dem_layers/` 四层栅格 + `meta.json` + 差异栅格已落盘（`dem_raw` 改 0 像元、`dem_repaired` 改 154、`dem_hydro` 改 441、`dem_hydraulic` 改 0），`data/catalog/jiulong_dem_layers.json` 已落盘（`contract_version=jiulong_dem_layers_v1`，`substitution_rules.enforced=true`，`hydro_differs_from_hydraulic_cells=441`）。**但层契约里的 `not_done`/`blocked` 仍开放**：结构修复缺授权资料、垂直基准未闭合（G7）、全域最小坡度与固定刻蚀按计划禁止故未实现。`tests/test_dem_layers.py` 已存在 |
| E3 | 降雨损失预注册敏感性矩阵 | **部分完成**：`data/catalog/jiulong_rainfall_loss_matrix.json` 已落盘，三档（A 零入渗 / B 标量入渗 / C 初损+持续损失）在跑任何算例前冻结并带 `preregistration_sha256`，`preregistered_before_any_run=true`；闭合域冒烟 `PASS`（蓄量=净雨逐位成立）。**真算例未跑**：`real_run.executed=false`，C 档在本构建无法实现（无初损参数），故为雨量序列预处理候选输入而非已跑模型 |
| E4 | 排水/边界/退水审计 | **已完成（审计层）**：`data/catalog/jiulong_drainage_audit.json` 与 `scripts/stage3/audit_jiulong_drainage.py` 已落盘（`read_only=true`），`docs/jiulong_phase34_candidates.md` 已落盘。**物理治理未做**：排水网络只到线索级，D1–D5 五项授权资料全部缺失（阻塞），退水异常未消除 |
| E5 | ADR-0010 定版 | **已完成**：`docs/adr/ADR-0010-久隆直接降雨模型治理规范.md`（8 条决策 + 后果 + 替代方案） |
| E5 | `docs/README.md` ADR 索引更新至 0010 | **已完成** |
| E5 | `execution_status.md` 本段更新 | **已完成** |
| E5 | floodviz 接入（默认层与退役标注） | **已完成（默认层回退 raw）**：`flood_visualization/configs/cases/jiulong.yaml` 已按 ADR-0010 第 2/8 条改为 `default_run_id: scen_free_200m_sv6h`（200 m 生产基线 raw），`_cleaned` 已从 `runs` 移除并加退役注释（含实测改动量 −36.56%/−53.16%），100 m 标 `role: legacy` 并注明「不得升级为生产基线」，`quality.warnings` 已写「7 PASS / 3 FAIL-EXPLAINED，FAIL-EXPLAINED 不计通过」。**`role=derived` 的 `display_qc` 条目仍未写入**——按计划 §9.3 第 3 条，`display_qc` 是否作默认层需用户确认；配置内已留注释说明 |
| E5 | 契约测试 `tests/test_governance_contract.py` | **已验证**：2026-10-31 21:34 全量实跑 **`152 passed`，0 failed，0 skipped**。此前 21:26 的 96 passed 为 Phase 1 独立测试落盘前的口径。两处测试缺陷已修（均属测试自身数据设置错误，非实现缺陷）：① `test_classify_cells_distinguishes_categories` 把海面标记混在数值行内，导致 `wet_cells` 期望值错算；② `test_classify_track_filter_requires_all_conditions` 未越过 A 档 `shallow_max_area_km2=0.16` 与 `deep_depth_m=0.30` 就断言「不再小/不再浅」，实际仍命中 `FILTER_SHALLOW` / `FILTER_SUBRESOLUTION` |

### 0c-2 久隆逐 run 质量门状态（`data/catalog/mass_recheck_g15.json`，2026-10-31 21:15 重算）

| run | 边界 | `E_mass` | 质量门状态 |
|---|---|---|---|
| `scen_free_200m` | FREE | 4.893e-4 | **PASS** |
| `scen_free_200m_sv6h`（200 m 生产基线） | FREE | 4.893e-4 | **PASS** |
| `scen_free_200m_dt2p5_b1` | FREE | 3.960e-5 | **PASS** |
| `scen_free_200m_sv6h_recession`（退水延长） | FREE | 5.300e-4 | **PASS** |
| `scen_free_100m_b1` | FREE | 2.242e-4 | **PASS** |
| `scen_free_100m_dt2p5_sv6h_realdem`（100 m 敏感性） | FREE | 3.089e-4 | **PASS** |
| `scen_z0_200m` | HFIX | 1.839e-4 | **PASS** |
| `scen_z05_200m` | HFIX | 1.223e-2 | **FAIL-EXPLAINED**（G13 常湿激波/HFIX 有界质量增益；不计通过） |
| `scen_z10_200m` | HFIX | 3.705e-2 | **FAIL-EXPLAINED**（同上；不计通过） |
| `scen_ztide_200m` | HVAR | 4.060e-2 | **FAIL-EXPLAINED**（同上；不计通过） |

久隆合计：**14 个有效 run → 11 PASS / 3 FAIL-EXPLAINED / 0 FAIL**。全表（三案例）：**43 PASS / 3 FAIL-EXPLAINED / 0 FAIL**（共 46 run）。
（2026-10-31 新增 4 个 100 m 重跑 run 后由 39 PASS / 42 run 更新；权威值以 `data/catalog/mass_recheck_g15.json` 为准。）
**不得表述为「质量门全量通过」**；`PASS` 只证明水量账目闭合，不证明预测正确。

### 0c-3 本轮必须随成果标注的限制

- 200 m 与 100 m 同一情景湿面积差 **+7.83 km²（+2.97%）**（271.07 vs 263.24 km²），站址邻域水深对分辨率敏感；200 m 能力上限未解除。
- 旧展示层 `scen_free_100m_dt2p5_sv6h_realdem_cleaned` 在 `res.max` 上把湿面积从 271.07 压到 **171.97 km²（−36.56%）**、水量从 168.43 压到 **78.90 Mm³（−53.16%）**，且未登记体积变化率——属「滤波误删真实积水」风险的实际发生，已由 ADR-0010 决策项 8 退役。
- 该目录含 `results/res.mass`（SHA-256 前 16 位 `6fc7b11c61aa6acc`，与源 run 逐位相同）且含 `run.log`，会被 `g15_mass_recheck.py` 的扫描条件计为独立 run。**已按 ADR-0010 第 8 条修复**：`g15_mass_recheck.py` 增加派生层判定（`meta.json` 的 `not_physical_recalculation`/`derived_from`，或存在 `clean_audit.json`/`display_qc_audit.json`），并把该目录登记进 `excluded_variants`（理由「展示派生层（非独立模型 run）；含复制自源 run 的 res.mass，按 ADR-0010 第 8 条不参与质量门计数」）。当前重算为 **43 PASS / 3 FAIL-EXPLAINED / 0 FAIL（46 run）**，久隆 14 个有效 run，计数不再需要人工排除。
- 退水异常未消除：模型 09-10→09-17 湿面积 **+27.68 km² 不降反增**，S1 09-17 水体约 12.2 km²，方向相反（`jiulong_recession_s1.json` 判 `WEAK`）。无排水网络与 FREE 缓排是主因，不得以滤波掩盖。
- `display_qc` 三档实测改动量（`jiulong_display_qc_audit.json`，源 run `scen_free_200m_sv6h_recession`）：`res.max` 面积/水量变化 A 档 −0.37%/−0.03%、B 档 −0.48%/−0.05%、C 档 −0.56%/−0.06%；单帧最大变化 A 1.43%/1.02%、B 2.49%/1.81%、C 3.06%/2.28%。三档均未触发预注册 10% 告警（`blocked_for_default_publish=false`）。**这组数字只说明展示层改动量可控，不证明模型正确，也不能当精度证据。**
- 旧 4/9 像元规则在 200 m 网格上的实测后果（`legacy_pixel_rule_diagnostic`）：峰值帧删除 764 个斑块 / 1348 像元 / 53.92 km²（占该帧 20.17%），其中 205 个斑块在 90% 以上帧次持续存在——属模型长期维持的浅水像元，不是瞬时渲染噪点。
- 久隆独立验证状态仍为 **`WEAK/未测`**：CSI、POD、FAR、NSE、R²、水深误差**未测**，不构成硬门。
- 本节 §2 门禁矩阵「13.2 质量守恒」行已同步为当前口径（43 PASS / 46 run）；**权威值以 `data/catalog/mass_recheck_g15.json` 为准**，本节任何数字与之冲突时以该 JSON 为正确。
- 本节 §2 门禁矩阵「十四章测试」行已同步为当前口径；2026-10-31 实测为 **`152 passed`，0 failed，0 skipped**，**全量测试通过**。
- 截至 2026-10-31 21:34，Phase 0/1/2/3-4/5 的脚本与产物均已落盘，无「应落盘而未落盘」的交付物。本段所有状态判定基于该时点的文件存在性与实跑结果，不基于任何口头进度。

### 0c-4 本轮明确未完成项（不得当作已完成）

> 2026-10-31 用户已就计划 §9.3 的 5 项作出决定，下表已按决定更新。

| 项 | 状态 / 阻塞原因 | 需要 |
|---|---|---|
| 方案 A/B 分辨率决策的用户确认 | **已决定：选择方案 A**。`resolution_comparison.json` 的 `decision_input.status` 已改为「已冻结：用户于 2026-10-31 明确选择方案 A」；Phase 1 的 A/B/C 阈值据此按 200 m 口径冻结 | ~~无~~ |
| Phase 0 输入 manifest | **已补齐**（`data/catalog/jiulong_improvement_manifest.json` 已落盘） | ~~无~~ |
| Phase 1 单元测试 `tests/test_display_qc.py` | **已补齐**（52 项，覆盖 raw 哈希不变、派生标记、cellsize 换算、体积账目、`res.mass` 拒写） | ~~无~~ |
| Phase 2 物理候选（新 run tag 的候选地形算例） | **仍未做**：`jiulong_dem_layers.json` 的 `blocked` 记「DEM_HYDRAULIC 结构修复缺授权资料」「负值来源闭合缺 G7」。四层契约与候选栅格已产出，但未用于任何新 run | 结构资料 + G7 垂直基准；在此之前只作候选输入 |
| Phase 3 降雨损失真算例 | **仍未做**：`real_run.executed=false`。C 档（初损+持续损失）在本构建无初损参数，只能作雨量序列预处理候选 | 需单独授权跑 A/B 档；C 档不得写成「已跑初损模型」 |
| Phase 4 排水网络与边界治理 | **仍阻塞**：用户 2026-10-31 明确表示**无法提供**站址坐标、排水网络、潮位/水位资料。D1–D5 五项全部保持阻塞；退水异常（+27.68 km²）在现有资料下不可消除，只登记原因，不以滤波掩盖 | 业务部门资料到位前不实施 |
| 100 m 完整物理重跑 | **已获批准并执行**：4 个 run（5 天窗 / 13 天窗 × 四边 FREE / 三边闭合），结果见 `docs/jiulong_100m_rerun.md`。`allfree` 与既有 100 m run 逐位一致（可复现 E1）；边界闭合使湿面积 +1.32%、出流 −35.6%（E2） | ~~无~~；100 m 升级为生产基线仍需质量门 + 分辨率收敛，ADR-0010 决策项 2 保持有效 |
| 前端默认层切换到 `display_qc` | **用户未表态**：默认层维持 200 m raw（`scen_free_200m_sv6h`）。`display_qc` A/B/C 三档**已作为 `role: derived` 可选层接入** floodviz，质量门显示 `unrecorded`（不得显示 PASS），raw 可一键切回 | 用户确认是否允许 `display_qc` 作默认展示层 |
| 久隆独立验证（CSI/POD/FAR/NSE/R²/水深误差） | **用户已接受 `WEAK/未测`**：不设 NSE、R²、水深误差硬门；相关指标一律记「未测」。`PASS` 仅表示水量账目闭合 | ~~无~~ |

## 0d. Phase F 增量（2026-11-02，贵港治理）

计划基线：`plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md`。治理口径以 ADR-0011 定版。
本节状态为 **2026-11-02 12:40 快照**。Phase 0/1/2+4/3/5 由并行工作流执行；凡标「部分完成/未达成/阻塞」的项，以落盘文件是否存在与实跑结果为准复核，不得据本节推断已完成的物理工作。

### 0d-1 分阶段状态

| Phase | 内容 | 状态 |
|---|---|---|
| F0 | 基线诊断包 `diagnostics/guigang/`（基线快照、斑块持续性 CSV、跨帧轨迹、假洼地分析、根因报告） | **已完成**：七件已落盘（`baseline_snapshot.json`、`patch_persistence.csv` 244,685 行、`patch_frame_summary.csv` 57 行、`patch_tracks.csv` 10,138 行、`patch_summary.json`、`guigang_sink_analysis.json`、`root_cause_report.md`）。源 run 前后 sha256 两次一致，目录内无 `res.mass` |
| F0 | 基线事实冻结（`res.max`，`depth > 0.05 m`，8 邻域） | **已验证**：湿像元 85,256 / 852.56 km² / 4,422 斑块 / ≤4 格 3,546（80.19%）/ ≤9 格 4,089（92.469%）/ 最大斑块 614.96 km²（72.131%）/ `E_mass=6.241e-05` PASS |
| F0 | 降雨账目 | **已验证（E1）**：`V_rain = 3.35403150e8 m³`（与 `res.mass` 逐位一致，确证零损失）；`V_upstream = 5.3784e9 m³`；雨水占总入流 **5.87%**；≤9 格小坑截留雨水 **24.91%**。**主水体占湿面积 72.13% 但占湿体积 91.95%** |
| F0 | 假洼地相关性核验 | **已完成，计划预估不成立**：最大像元级 \|Phi\| **0.263**、最大 1 km 分块 Pearson **0.573**、最大斑块命中率 **74.9%**，三项全 <0.85。但 **lift=3.56×**，属「必要但不充分」的中等相关 |
| F1 | `display_qc` 只读展示质控层（A/B/C 三档预注册、逐帧审计、派生标记） | **已完成（产出）**：`outputs/display_qc/guigang/2024-09-yagi/scen_yagi_q11500_100m_realdem_display_qc/{A,B,C}/` 三档已产出；`data/catalog/guigang_display_qc_audit.json` 已落盘；`tests/test_guigang_display_qc.py` 已落盘。raw 前后哈希 `unchanged=true`，派生目录内 `res.mass` 计数为 0 |
| F1 | 三档去除率是否达计划 §4 的 ≥90% | **未达成（结构性上界 25.49%）**：A 22.14% / B 22.41% / C 25.49%，对应计划预测 80.2% / 92.5% / 96.8%，偏差 −58.1 / −70.1 / −71.3 pp。**计划 §3 第 2 条把 `max_depth ≥ 0.50 m` 全部豁免，该规则下可去除斑块理论上界仅 1,127 个 = 25.49%；C 档已达上界。≥90% 目标与计划 §3 规则结构直接矛盾** |
| F1 | 三档能否默认发布 | **全部 `blocked_for_default_publish=true`**：逐帧湿面积变化率 A 13.76% / B 23.95% / C 27.48%，均超 5.0% 预注册上限（最差帧 seq 14，t=84 h 涨水段）。三档只能作诊断层 |
| F2 | 净雨预处理（Initial Loss / Continuing Loss） | **已完成（E3 候选参数）**：`dualloss_v1`（Ia=30 mm, fc=2.0 mm/h → 净雨 45.40 mm，33.51%）；`rainloss_high_v1`（Ia=50 mm, fc=3.0 mm/h → 净雨 36.10 mm，26.64%）。`res.mass` 反算净雨与 meta 登记**逐位一致**。`run.par` 不写 `infiltration`（`infevap.cpp:14-46` 只对 H>1e-3 m 湿单元生效，无法表达初损；且会与净雨预处理重复扣 fc） |
| F4 | 三个新物理情景实跑 | **已完成**：`flow_only_v1` / `dualloss_v1` / `rainloss_high_v1`，均 100 m、`q_peak=11500`、`dt=2.5`、`channel=off`、`sim_time=1209600`、57 帧；`rain=on` 逐字节向后兼容（5 个输入文件 sha256 与基线相同）。墙钟 57.0 / 71.6 / 71.3 min，exit 0 |
| F4 | 计划核心假设「离散水点由无损失降雨造成」 | **成立（强支持）**：关闭降雨 → 斑块 4,422→**20（−99.5%）**，外围小斑块面积 237.60→**0.29 km²（−99.9%）**，单像元孤岛 1,922→**14**。逐帧：t=108 h 基线 327.93 km²/7,348 斑块 → flow_only 128.80 km²/**3** 斑块；t=336 h 基线 672.94/4,724 → flow_only 485.94/**126** |
| F4 | 「施加净雨损失即可消除离散斑块」 | **被否证**：`dualloss_v1` 斑块 **6,041（+36.6%）**、`rainloss_high_v1` **6,167（+39.5%）**，与计划预期（去除 80~97%）**方向相反**。三个降雨情景的湿像元全部是基线的真子集（`only_self=0`），**无新增水**；斑块数上升来自主水体被切碎（614.96→543.97 km²，−70.99 km²；单像元斑块 1,922→3,210；≥1 km² 大斑块 14→7；外围斑块水深中位数 0.344→0.222/0.211 m）。**「减少水量」与「减少斑块数」在本域是两个相反方向的目标** |
| F3 | 河道调理（S1 常水骨架 + 刻蚀） | **阻塞，未实施**：`verdict=BLOCKED_ALTERNATIVE_REQUIRED`，`phase3_implemented=false`。定量证据：S1 常水掩膜足印占模型域 **26.19%**，北半域（行 0-184）覆盖率 **0%**，域内常水像元 **1,338（13.38 km²）**碎成 **213 块**，最大块 **4.33 km²**（20 m）/ **3.38 km²**（100 m），而郁江主水体 **614.96 km²**（尺寸比 **141:1**）；上游 Q 点源在 col0 西边界，注入行带覆盖率 **0.0**；**无任何常水块同时触及西、东边界**（端点退化）。**三个新情景全部沿用 `channel=off`，未做河道调理** |
| F5 | S1 09-12 洪峰景验证 | **已完成**：`data/catalog/guigang_s1_validation.json`（逐行复用 `verify()` 口径；基线自校验 `all_match=true`）。见 0d-3 |
| F5 | 站点水位诊断 | **已完成（仅诊断）**：贵港站 09-12T14 洪峰 **43.09 m** vs 模型锚点 7×7 包络 WSE **28.30 m**，apparent offset **14.79 m**；09-14T05 offset 15.85 m。**G7 垂直基准未闭合，两时点 offset 不一致，不得当作 `datum_offset_m`、率定值或同峰对齐依据**。未参与任何验收判定 |
| F5 | 质量门全表实测确认 | **已完成（实跑）**：**46 PASS / 3 FAIL-EXPLAINED / 0 FAIL（49 run）**。较基线 43 PASS / 46 run，新增 3 个 run 全部 PASS。`display_qc` 零条目被计入 |
| F5 | ADR-0011 定版 | **已完成**：`docs/adr/ADR-0011-贵港水动力推演零散伪影治理与驱动重构.md`（9 条决策 + 后果 + 替代方案 + 计划三处错误更正登记） |
| F5 | `docs/README.md` ADR 索引更新至 0011 | **已完成** |
| F5 | `execution_status.md` 本段更新 | **已完成** |

### 0d-2 四情景质量门状态（`data/catalog/mass_recheck_g15.json`，2026-11-02T04:35:21Z 重算）

| run | 边界 | `E_mass` | 质量门状态 |
|---|---|---|---|
| `scen_yagi_q11500_100m_dt2p5_choff_realdem`（基线） | FREE | 6.241e-05 | **PASS** |
| `scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1` | FREE | 5.758e-05 | **PASS** |
| `scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1` | FREE | 7.290e-05 | **PASS** |
| `scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1` | FREE | 6.241e-05 | **PASS** |

全表（三案例）：**46 PASS / 3 FAIL-EXPLAINED / 0 FAIL（共 49 run）**。三案例分布：久隆 14 run、宁明 23 run、贵港 12 run。
`FAIL-EXPLAINED` 三项仍为久隆 `scen_z05_200m`（1.223e-2）、`scen_z10_200m`（3.705e-2）、`scen_ztide_200m`（4.06e-2），**不计通过**。
**不得表述为「质量门全量通过」**；`PASS` 只证明水量账目闭合，不证明预测正确。

### 0d-3 S1 09-12 验证结果（`data/catalog/guigang_s1_validation.json`，E1）

口径：`t_model_h=228.0`（`res-0038.wd`）；湿格 `wd > 0.05`；**只在 S1 足迹内比对**（覆盖模型域 20.6%，足迹外不计假警报）；**面积比较用 `area_sim_in_footprint_km2`**；S1 固定阈值 −17 dB（G17 降级）。

| 情景 | 降雨模式 | CSI | POD | FAR | `area_sim_in_footprint_km2` | ΔCSI | FAR 压降 |
|---|---|---:|---:|---:|---:|---:|---:|
| `baseline_realdem` | on（毛雨 135.50 mm） | 0.076 | 0.652 | 0.921 | 92.63 | — | — |
| `flow_only_v1` | off | **0.164** | 0.616 | **0.817** | 38.02 | +0.088 | 11.3% |
| `dualloss_v1` | loss_medium | 0.093 | 0.633 | 0.901 | 72.37 | +0.017 | 2.2% |
| `rainloss_high_v1` | loss_high | 0.095 | 0.631 | 0.899 | 70.75 | +0.019 | 2.4% |

观测基准（四情景共用）：`area_obs_km2_in_footprint = 11.29`、`valid_footprint_km2 = 510.95`。

- **计划 §4「FAR 压降 > 20%」未达成**（三情景全部：11.3% / 2.2% / 2.4%）。
- **「CSI 有效提升」达成**（按本项目自定的 `delta_csi > 0` 判定；计划未给数值门槛，该阈值非计划原文）。
- `flow_only_v1` 的 CSI 提升伴随**湿面积 −37.9%**（852.56 → 529.52 km²），是「模拟水体向观测水体收缩」造成的重叠度改善，**须与面积一并读**。
- **CSI/POD/FAR 是重叠度指标，不是水深或水位精度指标，不得据此声称模型预测正确。**

### 0d-4 本轮必须随成果标注的限制

- **Phase 1 的 ≥90% 去除率未达成，结构性上界 25.49%（1,127 个斑块）。** 计划 §4 的「预期去除斑块比例」是**纯面积谱累计占比**（A 80.19% vs 计划 80.2%、B 92.469% vs 92.5%、C 97.671% vs 96.8%，逐位吻合），**未计入计划自己写的深水豁免规则**，故不可作为验收基线。任何声称已达成 ≥90% 的表述都是错误的。
- **loss 情景斑块数反升（+36.6% / +39.5%）。** 机制是净雨削减使浅滩水深跌破 0.05 m 判据，把连片滩地切成孤岛（「大切小」）。**斑块数不是衡量本治理目标的好指标。**
- **离散斑块不是浅水。** ≤9 格斑块 `max_depth` 中位 **1.034 m**、p90 **2.335 m**、最大 **12.06 m**；只有 996 个（24.4%）低于 0.30 m。计划 §1.2 的「浅水坑洼 / 5 cm 地表糙度」定性不成立，已按 ADR-0011 决策项 6（C1）更正。**任何基于水深阈值的过滤对它们基本无效**——这是去除率只有 20~26% 的根本原因。
- **计划 §3 Phase 0 任务 3 的上游注入「≈5.5×10⁸ m³」实测为 5.3784e9 m³，差一个数量级**（ADR-0011 决策项 6，C2）。
- **计划 §3 Phase 0 任务 2 的假洼地相关系数「>0.85」在任何口径下不成立**（最大 \|Phi\| 0.263、分块 Pearson 0.573、斑块命中率 74.9%），已登记（ADR-0011 决策项 6，C3）。
- **`display_qc` 三档全部 `blocked_for_default_publish=true`**（逐帧面积变化率 13.76% / 23.95% / 27.48% 超 5.0% 上限）。三档只能作诊断层，不得默认发布；`blocked` 表示档位不能默认发布，**不表示源 run 有质量问题**。
- **`display_qc` 不改变物理结果。** 带 `not_physical_recalculation=true`，只能作展示层；不得用于物理结论、验证指标或资产级判据。三档 `res.max`（**派生包络口径**）面积变化 A 5.419% / B 5.526% / C 5.790%，水量变化 A 0.2633% / B 0.2682% / C 0.3116%；**斑块级判定口径**的去除率仍为 A 22.14% / B 22.41% / C 25.49%。两套口径的成因与适用场景见 ADR-0011 §5.3，**不得混用于同一句结论**。**这组数字只说明展示层改动量可控，不证明模型正确，也不能当精度证据。**
- **郁江主水体 614.96 km² 完整保护已达成**：`largest_patch` `removed_cells=0`、`kept_area_km2=614.96`，三档全部成立。主槽白名单改用「候选主汇流区」定义（`main_body_area_fraction=0.5`），**未使用 S1 常水缓冲白名单**（足印仅 26.19%，北半域 0）。
- **Phase 3 未实施，是输入数据固有缺陷，不是执行缺陷。** 「干流传导受阻」限制未被消除；三个新情景全部 `channel=off`。**情景之间的差值与相对变化可用于判断降雨损失假设的方向性；绝对淹没面积、干流洪峰传播时间、河道内水位绝对值不得作为物理结论或资产判据。**
- **降雨损失参数标 E3 候选假设，未经本地率定。** Ia=30/50 mm、fc=2.0/3.0 mm/h 是项目候选参数，**不归因于 ARR 2019、UK EA 或任何国际规范**。换一组参数会得到不同的净雨与斑块数；不得把本次三档结果当作贵港下渗特性的结论。
- **贵港独立验证仍不足以构成硬门。** 仅单景 S1（足迹 20.6%、固定阈值 −17 dB）；**NSE、R²、水深误差未测**；水位诊断受 G7 阻塞。CSI/POD/FAR 只代表足迹内重叠度。
- **时钟基准**：`rain.rain` 时刻列以模型 **t=0 = 2024-09-03T00 Asia/Shanghai** 为基准（`T0_DAY=3`）。误用 09-04T00 或 09-05T00 会使全部累计雨量整体偏移 24 h 或 48 h。本节数值统一按 09-03T00 对齐。
- 本节 §2 门禁矩阵「13.2 质量守恒」行已同步为当前口径（46 PASS / 49 run）；**权威值以 `data/catalog/mass_recheck_g15.json` 为准**，本节任何数字与之冲突时以该 JSON 为正确。

### 0d-5 本轮明确未完成项（不得当作已完成）

| 项 | 状态 / 阻塞原因 | 需要 |
|---|---|---|
| Phase 1 的 ≥90% 斑块去除率 | **未达成**：A 22.14% / B 22.41% / C 25.49%，结构性上界 25.49%（1,127 个斑块）。计划 §3 第 2 条深水豁免构成硬上界 | 只能修改**规则结构**（如把深水豁免从「绝对」改为「按档位」），须作新预注册参数集并由用户批准；**不得只调阈值** |
| 微小斑块占比 ≤25% | **未达成**：A 92.71% / B 92.78% / C 92.50%（基线 92.47%） | 同上 |
| 物理重构后斑块数 ≤200 | **未达成**：`flow_only_v1` 20（达成）；`dualloss_v1` 6,041、`rainloss_high_v1` 6,167（均反升） | 需重新定义治理指标（斑块数不是好指标）；或修改湿格判据并联动重算 |
| S1 观测 FAR 压降 >20% | **未达成**：11.3% / 2.2% / 2.4% | 需更完整的 S1 时序观测与更接近真实的降雨损失参数；当前单景 + G17 降级不足以支撑 |
| Phase 3 河道调理 | **阻塞未实施**：`BLOCKED_ALTERNATIVE_REQUIRED`。S1 常水掩膜不可用（足印 26.19%、北半域 0%、213 块、最大 4.33 km²、端点退化） | 独立河道资料（实测断面、航道图或高精度 DEM）；在此之前保持 `channel=off` |
| 干流传导受阻 | **未消除**：无河槽，上游来水在宽平底平原扩散 | 同 Phase 3 |
| 贵港 `display_qc` 默认层切换 | **未表态**：三档均 `blocked_for_default_publish=true`；且默认层切换需用户确认 | 先解决逐帧面积变化超预注册上限问题，再请用户确认 |
| 贵港 NSE / R² / 水深误差 | **未测**：无连续水位/流量过程；仅单景 S1 与受 G7 阻塞的站点水位 | 授权观测资料（G7 垂直基准 + 连续站水位） |
| G7 垂直基准 | **未闭合**：站点 43.09 m vs 模型 28.30 m，两时点 offset 不一致 | DEM 来源/控制点（业务） |


已完成的是"三案例降级模式首轮情景 + 结果完整性修正（Phase A）+ Phase B0–B5 敏感性/结构/喀斯特补课 + Phase C 复现/退水验证/去混杂/定版 ADR/30 m 全域"，不是终稿全部阶段/门禁完成。原阶段 0 门禁未满足；阶段 6 未执行；贵港站区局部 5 m、区域—局部单向接口、宁明 25/12.5 m、久隆 12.5 m 未执行（用户决策推迟）。全部结论为情景级。

## 0e. Phase G 增量（2026-11-07，土壤物理下渗与物理求解器重跑）

计划基线：`plans/地表产流损失、土壤水文属性与初损网格化算法（针对产流水量虚高）计划.txt`。

### 0e-1 算法改进与模块落地
- **算法底座**：在主工程新增 `rainfall_runoff_preprocessor/` 独立模块，构建了结合 SoilGrids/FAO 华南红壤导水率与非饱和蓄水容量的物理下渗模型体系（Green-Ampt 动力下渗、SCS-CN 初损与径流曲线数、Horton 经验入渗）。
- **空间与时序预处理**：实现 ERA5 逐时雨量细分与 1 分钟子时间步积分（含积水判据与零雨跳步优化），输出符合 LISFLOOD-FP 输入规范的逐时净雨时序 `.rain` 与空间参数包（`soil_ksat.tif`, `soil_theta_s.tif`, `soil_theta_fc.tif`, `soil_theta_i.tif`, `soil_hsg.tif`, `landcover_impervious.tif`）。
- **质量门自检**：八情景质量守恒审计全量通过（相对误差 $\le 10^{-20} \ll 10^{-5}$）。

### 0e-2 久隆物理求解器重跑事实（`scen_free_200m_dt2p5_sv6h_recession_ga_v1`）
针对久隆 2024-09 摩羯事件 13 天退水段（09-05T00 至 09-18T00，时长 1,123,200 s），采用 Green-Ampt GA-Mid 净雨序列驱动 LISFLOOD-FP 8.1.0 物理求解器实跑：
- **运行配置**：200 m 生产治理基线网格（178×203 像元），`initial_tstep=2.5s`，`saveint=21600s`（6h），`acceleration` 求解器。耗时 609.2 s（10.2 min），内存峰值 9.1 MB。
- **质量门 G15**：水量账目绝对残差 −58,766 m³，相对残差 $E_{mass} = 4.843 \times 10^{-4} \le 1.0 \times 10^{-3}$，**PASS**（较基线 5.300e-4 进一步改善）。
- **入流削减与虚高水量消除**：总净雨量由 242.50 mm 降为 109.70 mm（削减 54.8%），入流净水量由 268.34 Mm³ 压降至 121.35 Mm³，消除虚高产流水量 **1.47 亿 m³**；全周期残水由 234.41 Mm³ 降至 110.01 Mm³（压降 53.1%）。
- **峰值与退水期淹没压降**：
  - 全周期最大淹没面积由 294.36 km² 压至 217.60 km²（**净压降 76.76 km²，−26.1%**）。
  - S1 卫星过境帧（09-17）模拟水体由 260.76 km² 降至 199.64 km²（**净压降 61.12 km²，−23.4%**）。
  - S1 假警报像元由 6,401 格降至 4,896 格（**减少 1,505 格，假警报压降 23.5%**）。
  - POD 由 0.387 微降至 0.311，FAR 由 0.982 维持在 0.981，CSI 由 0.018 保持在 0.018。
- **退水反常机制与已知限制**：
  - 09-10T00 模拟湿面积由基线的 233.08 km² 大幅收缩至 149.92 km²（主暴雨退水明显）；
  - 但在 09-10 至 09-17 期间，退水段 84 mm 降雨即使经下渗削减后仍残留 20.85 mm 净雨。由于 200 m 网格未刻蚀亚像元河槽且域内无人工排涝渠系，地表平坦微洼地再次蓄水超 5 cm 阈值，导致 09-17 湿面积回弹至 199.64 km²。
  - **工程结论**：产流下渗损失治理显著消除了 1.47 亿立方米的虚高入流，并削减了近 1/4 的假警报像元与峰值淹没；但要彻底消除平原地表退水假象，**必须配合地形水力刻槽（DEM_HYDRAULIC）与排水连通性治理**。

## 0f. Phase H 增量（2026-11-07，贵港水力约束河槽重构，ADR-0012）

计划基线：`plans/无实测水下地形条件下的河槽水力刻蚀与几何重构算法（针对贵港干流漫滩）.txt`。口径以 ADR-0012 定版。用户决策（2026-11-07）：①批准重跑 5 个深度情景 run；②中心线用 OSM Overpass（带回退链）；③首轮滩地糙率维持基线 0.035（单变量控制）。

### 0f-1 分阶段状态

| Phase | 内容 | 状态 |
|---|---|---|
| H0 | 预注册 priors + ADR-0012 | **已完成（先于一切刻蚀/run）**：`data/catalog/guigang_channel_priors.json`（matrix_id `guigang-channel-priors-v1`，W=300 m / S=0.116‰ / n_main=0.030 / z=2 / A–E 深度 10/12/14/16/19 m 及冻结 α=0.3835/0.5063/0.6375/0.7754/0.9913）；`docs/adr/ADR-0012-贵港水力约束河槽重构.md` 定版。构建时复算 α 超差即拒绝（实测 Δ≤1e-6） |
| H1 | 河道中心线（OSM Overpass → S2 → 直线回退链） | **已完成（OSM 主路线实跑）**：`scripts/stage3/build_guigang_centerline.py`；郁江 name=郁江 way 端点匹配成链（4 段主链），裁到走廊后 77.57 km、790 节点；**S 缘入（109.6046,22.9830）、E 缘出（109.9508,23.2052）**——预注册锚点 (109.60,23.11) 偏离真河 2,513 m（仅诊断，不作硬门）；S1 常水距离 QA：走廊内 33,723 像元到中心线中位 **168.9 m**、60.6% ≤300 m（河道半宽量级），>500 m 的 12,988 像元为坑塘水体（与 ADR-0011 碎块结论一致）。实测 https 直连 Overpass 被重置（SSL exit 35），**明文 http 可用**（HTTP 200），已登记 |
| H2 | 刻蚀引擎 + 五套重构 DEM + 验证 | **已完成（全部 PASS）**：`scripts/common/channel_engine.py`（Manning 二分反演，.txt §2.4 四锚点偏差 ≤4 mm；圆滑梯形 smoothstep；床面顺流单调；4×4 亚采样，Z_new=min(Z_orig, Z_ideal)，走廊外零修改）；`scripts/stage3/build_guigang_channel_dem.py` 产出 `data/interim/guigang/dem_layers/`（raw/hydro 零修改/hydraulic）与 `channel_recon/d{10,12,14,16,19}m/`（各刻蚀 3,047 格，max 下切 13.3–19.0 m）；`guigang_channel_carve_validation.json`：几何 6 项 + 输水核验（q_ratio 中位 0.908–0.922，门 ±15%/面积 ±10%，量化容差已登记）**五情景全 PASS** |
| H2b | 单元测试 | **已验证**：`tests/test_channel_engine.py` 25 项；全量 **249 passed，0 failed**（2026-11-07 实测，含既有 211+Phase G 新增）。off 模式回归：prep 产物（dem.asc/bci.bci/run.par/yujiang.bdy/rain.rain）与基线 run **逐位一致** |
| H3 | hydraulic 模式接入 + 5 run 实跑 | **已完成（exit 0，E_mass 全 PASS）**：`guigang_scenario.py` 新增 `--channel hydraulic --channel-dem <dir> --n-channel`；dem 整层替换（validation 门禁）、bci 点源=中心线注入点（走廊南缘入域 500 m，**与基线西缘注入不同——几何一致性变更已登记 meta**）、run.par 增 `manningfile manning.map`（主槽掩膜 3,047 格 n=0.030，其余 nodata 回退 fpfric=0.035；pars.cpp:234 + input.cpp:909-940 实证）。5 个 run `scen_yagi_q11500_100m_dt2p5_chhyd_d{10,12,14,16,19}` 全部完成（求解墙钟 2,536–2,778 s，峰值内存 44.3 MB）；off 模式逐字节回归通过。**S1 验证核心预期未达成**，见 0f-3 |
| H3c | 质量门全表刷新 | **已完成（实跑）**：`g15_mass_recheck.py` 重算 → **52 PASS / 3 FAIL-EXPLAINED / 0 FAIL（55 run）**（较 49 run 新增 5 个 chhyd + 1 个 Phase G 的 ga_v1，全部 PASS）。`validate_guigang_s1.py` 已扩展 9 情景（缺失 run 记 available=false），基线自校验 all_match=true |
| H4 | 文档 | **已完成**：`docs/README.md` ADR 索引 +0012、工作区 `AGENTS.md` 脚本清单、本节 |

### 0f-3 五情景实测结果（t=228 h，S1 09-12 洪峰景，足迹内口径）

| run | D (m) | α（冻结） | CSI | POD | FAR | 足迹内模拟面积 km² | ΔFAR | E_mass | 门 |
|---|---|---|---|---|---|---|---|---|---|
| `choff_realdem`（基线） | — | — | 0.076 | 0.652 | 0.921 | 92.63 | 0 | 6.24e-05 | PASS |
| `chhyd_d10` | 10 | 0.3835 | 0.074 | 0.475 | 0.919 | 66.57 | −0.2% | 1.512e-04 | PASS |
| `chhyd_d12` | 12 | 0.5063 | 0.072 | 0.460 | 0.921 | 66.10 | 0.0% | 1.573e-04 | PASS |
| `chhyd_d14` | 14 | 0.6375 | 0.068 | 0.437 | 0.925 | 65.68 | +0.4% | 1.593e-04 | PASS |
| `chhyd_d16` | 16 | 0.7754 | 0.066 | 0.420 | 0.927 | 65.36 | +0.7% | 1.725e-04 | PASS |
| `chhyd_d19` | 19 | 0.9913 | 0.063 | 0.403 | 0.930 | 65.05 | +1.0% | 1.707e-04 | PASS |

**首轮结论（如实登记，S1 指标是足迹内重叠度不是精度）：**

1. **计划 §7.4 的 S1 改善预期未达成**：五个深度情景 FAR 0.919~0.930（基线 0.921），FAR 压降 >20% 目标**全部未达成**；CSI 全部低于基线，「CSI 有效提升」同样未达成。
2. **河槽输水本身有效**：重构河槽把足迹内模拟水从 92.63 压到 65~67 km²（−28%），「干流传导受阻」在物理上得到缓解（v_out 5.3404e9 → 5.3470e9 m³）；但被排走的水同时落在命中与虚警区，POD 由 0.652 单调降至 0.403，FAR 基本不动——**深度越大 POD 损失越多**，与 .txt §7.4「不能仅选 CSI 最高方案」的告诫一致，本轮无任何深度在 S1 口径下可取。
3. **假警报主导因素不在主槽几何**：对照 `flow_only_v1`（无降雨输入，FAR 0.817，ΔFAR −11.3%），降雨滩地积水的贡献远大于任何河槽几何情景（|ΔFAR| ≤1%）。河槽重构与降雨损失（Phase F/0e 路线）作用在不同水源上，**组合情景（hydraulic + 净雨）是下一轮的自然候选**，未跑，不作结论。
4. 新 run `E_mass`（1.5~1.7e-4）比基线高 2.4~2.8 倍但仍远低于 1e-3 门——水量账目闭合，不证明预测正确。
5. 墙钟：5 run 求解 2,536~2,778 s/run（比重构前基线略快——河槽排走部分地表水）。

### 0f-2 本轮必须随成果标注的限制

- 重构 DEM 是**水力一致性河槽先验地形，不是实测 bathymetry**；α（主槽分流比）是 Manning 反演的情景假设，不是观测值。
- 100 m 网格主槽仅 2~3 格宽（W=300 m）；梯形岸坡过渡带（zD=27~38 m）为亚格宽，栅格断面输水较设计存在中位 −8~−9% 的量化偏差（门 ±15% 已登记）；本构建无亚网格河道（模型横幅 Rectangular channels only）。
- 中心线来自 OSM（ODbL 公开矢量），精度未经独立断面核验，以 S1 常水距离（中位 169 m）作代理证据。
- 点源位置由基线西缘改为走廊南缘真实河道入口（S 缘内 500 m）——与基线对比时该差异属几何一致性变更，不是受控变量。
- d19 情景重构层床面最低 −1.2 m（低于高程基准的 datum 伪影；LISFLOOD 只用高程差；G16 负值断言只适用于 raw 走廊）。
- 重构层与基线 dem.asc 头沿用同一约定（yllcorner = AOI 角点最小值 b[1]，与 WarpedVRT 实际底边差 18.5 m——基线 12 个 run 既有口径，tif 用真实 transform 无此差）。
- **PASS ≠ 预测正确**；新增 run 的质量门与 S1 指标以刷新后的 `mass_recheck_g15.json` / `guigang_s1_validation.json` 为权威。




## 0g. Phase H 第二轮（2026-11-07，贵港组合驱动归因实验，ADR-0013）

计划基线：ADR-0012 首轮否定性结论 + 用户决策（3 个组合 run，GA-Mid 为主 + 标量对照）。矩阵预注册 `data/catalog/guigang_round2_matrix.json`（`guigang-round2-combo-v1`）。

### 0g-1 分阶段状态

| Phase | 内容 | 状态 |
|---|---|---|
| G1 | 预跑对账 | **已完成（PASS，实测）**：`rainfall_gross.rain`（313 行含 t=0）与 ERA5 probe 逐值一致，全序列和 137.1 mm；模型窗（t≤336 h）裁掉 09-17T01..23 的 1.6 mm → E1 口径 135.50 mm。summary 的 137.1 = 全序列口径，**非数据缺陷**，预处理器产物无需重生成。GA 净雨窗内 38.87 mm（尾裁 0.0606 mm） |
| G2 | 净雨接线代码 | **已完成（全量 259 passed）**：`scripts/common/net_rain.py`（`read_preprocessor_net_rain`：hours/seconds 解析、**+48 h 显式时基移位**（由 probe 首键与 T0_DAY 推导）、窗口裁剪、端点补零、单调/非负校验；逻辑镜像 `run_jiulong_ga_rerun.py:88-108`，久隆脚本不回改）；`guigang_scenario.py` 新增 `--rain ga`（rain_meta 登记 GA 源 sha/参数快照/移位/尾裁；`baseline_comparison` 双变量清单）；`scripts/stage6/attribute_guigang_combo.py`（只读 2×2 因子归因）；`tests/test_guigang_combo.py` 10 项。off 模式逐字节回归维持（0f 已证） |
| G2 | 已修缺陷登记 | ①轮 1 的 hydraulic meta 块无条件覆盖 `baseline_comparison`（轮 1 RAIN 恒 on 未触发；组合下冲突）→ 改为仅 RAIN=on 时设置；②ga 分支补 `gross_total_mm` 与独立打印分支 |
| G3 | 3 个组合 run | **已完成（exit 0，E_mass 全 PASS）**：`chhyd_d14_r2_lossmed`（求解 2,125 s）、`chhyd_d14_r2_ga`、`chhyd_d10_r2_norain`。质量门全表刷新 **55 PASS / 3 FAIL-EXPLAINED / 0 FAIL（58 run）**；`validate_guigang_s1.py` 12 情景出表（基线自校验 all_match=true）；`attribute_guigang_combo.py` 归因落盘。结果见 0g-3 |

### 0g-3 组合实验实测结果（t=228 h，S1 足迹内口径）

| run | 河槽 | 降雨 | FAR | CSI | POD | 足迹内 km² | E_mass |
|---|---|---|---|---|---|---|---|
| baseline_realdem | off | on（毛雨） | 0.921 | 0.076 | 0.652 | 92.63 | 6.2e-5 PASS |
| flow_only_v1 | off | off | 0.817 | 0.164 | 0.616 | 38.02 | 5.8e-5 PASS |
| dualloss_v1 | off | loss_medium | 0.901 | 0.093 | 0.633 | 72.37 | 7.3e-5 PASS |
| chhyd_d14 | hyd d14 | on（毛雨） | 0.925 | 0.068 | 0.437 | 65.68 | 1.6e-4 PASS |
| **chhyd_d14_r2_lossmed** | hyd d14 | loss_medium | **0.897** | 0.090 | 0.408 | 44.75 | 1.7e-4 PASS |
| **chhyd_d14_r2_ga** | hyd d14 | **GA-Mid** | **0.895** | 0.091 | 0.406 | 43.50 | PASS |
| **chhyd_d10_r2_norain** | hyd d10 | **off** | **0.386** | **0.335** | 0.425 | **7.82** | PASS |

### 0g-4 归因结论（`guigang_combo_attribution.json`，2×2 因子差分）

1. **预注册方向性预期两项全部达成**：组合 FAR 0.895/0.897 < 0.901；`chhyd_d10_r2_norain` FAR 0.386 << 0.817。
2. **「干流传导受阻」的定量分解（channel_x_norain 块）**：FAR 的河槽效应在无雨档 = **−0.431**（0.817→0.386），在有雨档仅 −0.002；交互项 **−0.429**。即：模型足迹内假警报的主体是**上游来水经未刻槽走廊向平原漫溢**，重构河槽把这部分压掉约 80%（足迹内模拟水 38.02→7.82 km²），降雨只是叠加在其上的第二来源（B@A1 = −0.533：有河槽时关雨的 FAR 增益最大）。
3. **净雨机制量级有限**：有河槽时净雨效应（B@A1）FAR 仅 −0.028（loss_medium）/−0.030（GA）——净雨预处理只裁掉毛雨的 1/3，剩余 ~90 mm 净雨仍主导滩地积水；GA 略优于标量（0.895 vs 0.897，差 0.002，不构成结论）。
4. **现实运行点**：真实洪水有降雨，组合情景的 FAR 仍在 ~0.90——「河槽已修好上游输水，残余假警报来自降雨滩地积水 + DEM 基准/堤防/S1 口径」，与 0e-2「须配合排水连通治理」结论互相印证。
5. **不得过度解读**：`r2_norain` 的 CSI 0.335 是**诊断角点**（无降雨情景），不是可发布的情景；PASS 仅证明水量账目闭合。

### 0g-2 方向性预期（预注册，无硬门）

- 组合情景 FAR < min(单机制 FAR) = 0.901（dualloss_v1）；
- `chhyd_d10_r2_norain` FAR < `flow_only_v1` 的 0.817（河槽减少上游来水漫溢）；
- 若未达：如实登记，下一误差源候选 = DEM 高程基准（G7）/ 堤防缺失 / S1 阈值与足迹口径（G17）。**CSI/POD/FAR 是足迹内重叠度，不是精度。**

## 0h. Phase H 第三轮（2026-11-07，贵港排水连通治理 · 诊断探针，ADR-0014）

计划基线：ADR-0013 归因结论（残余 FAR = 净雨滩地积水）+ 用户约束（只有 DEM，无排水网资料）。矩阵预注册 `data/catalog/guigang_round3_drainage.json`（`guigang-round3-drainage-v1`）。**定位：诊断探针**——DEM 推导沟与求解器入渗均为情景假设，用于量化「多强排水能力能解释残余 FAR」，不是真实排水网（用户 2026-11-07 决策）。

### 0h-1 分阶段状态

| Phase | 内容 | 状态 |
|---|---|---|
| G0 | 预注册 + ADR-0014 | **已完成（先于任何 run）**：A 档沟参数（宽 100 m=1 格、深 2.0 m、汇流阈值 2 km²、最小沟长 2 km、间距 ≥1 km、总长上限 120 km）+ 入渗 2.0 mm/h（红壤 fc 先验界内）+ 探针定位声明 |
| G2 | 排水沟构建 | **已完成（实跑）**：`scripts/stage3/build_guigang_drainage.py`——priority-flood **主槽为种子**的传播树（平坦区路径确定，不受 D8 平坦失效影响）+ 源点分支提取 + canyon 守卫（单沟 max 下切 > 深度+3 m 整体弃用，实测拦下山体切穿 59 m 的沟）+ 平原约束（DEM ≤45 m）。产出 **5 条 / 158.1 km / 刻蚀 1,425 格 / max 下切 4.6 m**（`channel_recon/d14m_drA/`）。已修缺陷：沟源头在山地导致床面峡谷化（山段排除修）；总长上限单位错（格数→km 修） |
| G2b | 垂直排水接入 + 预诊断 + 测试 | **已完成**：`guigang_scenario.py` 新增 `--infiltration`（路径 B 单用：毛雨 + 求解器入渗，强制 `--rain on`，混用拒绝）；`diagnose_guigang_ponding.py`（只读）：r2_ga 积水 2.07e8 m³、水深 p50 0.25 m、**80% 湿格距主槽 >2 km**（预诊断预示稀疏沟覆盖有限）。全量 pytest **267 passed**（新增 8 项） |
| G3 | 3 个治理 run | **已完成（exit 0，E_mass 全 PASS）**：质量门 **58 PASS / 3 FAIL-EXPLAINED / 0 FAIL（61 run）**；S1 十五情景出表（基线自校验 all_match=true）。结果见 0h-3 |

### 0h-3 排水治理实测结果（t=228 h，S1 足迹内口径）

| run | 排水机制 | FAR | CSI | POD | 足迹内 km² | E_mass |
|---|---|---|---|---|---|---|
| chhyd_d14（锚点） | — | 0.925 | 0.068 | 0.437 | 65.68 | 1.6e-4 PASS |
| **chhyd_d14_r3_infil2** | 求解器入渗 2.0 mm/h | **0.863** | **0.114** | 0.403 | **33.31** | 1.8e-4 PASS |
| **chhyd_d14_r3_drA** | DEM 沟 A 档（5 条/158 km） | 0.925 | 0.068 | 0.437 | 65.68 | 1.6e-4 PASS |
| **chhyd_d14_r3_combo** | 沟 + 入渗 | **0.863** | **0.114** | 0.403 | 33.30 | 1.7e-4 PASS |

### 0h-4 归因结论（如实登记）

1. **方向性预期部分达成**：r3_infil2 与 r3_combo FAR 0.863 < 0.925 ✓；r3_drA **零效果**（与锚点逐位相同）——两条预期中垂直轨达成、水平轨未达成。
2. **垂直排水（求解器入渗）显著有效**：FAR −6.2 pp、CSI +0.046（0.068→0.114，有雨情景历史最高）、足迹内模拟水 −49%（65.68→33.31 km²）。机理：入渗对每个湿格持续扣水，直接排掉「净雨预处理排不掉的已积水」——路径 A（改降雨输入）与路径 B（改积水汇）的互补性首次得到实测分离。
3. **水平排水（A 档沟）在本分辨率/密度下无测量效果**：已核验沟层确实进模（1,425 格、228 h 水深差 max 2.49 m、1,198 格湿态翻转），但足迹内 H/FA/M 不变。解释：158 km 的 1 格宽沟只覆盖 2 km² 级汇流走廊，而积水 80% 距主槽 >2 km（预诊断），A 档稀疏度不足以连通主要积水区。**这是探针的量化结论：需要 B 档（更密/更长）或 km 级联通性才可能起效**，B 档留待决策。
4. r3_combo 与 r3_infil2 逐位相同——沟的零效果在叠加中同样成立。
5. 残余结构更新：有雨 + 入渗后 FAR 0.863，仍高于 flow_only 的 0.817（无雨下限）——差值即剩余净雨滩地积水；进一步压降的候选 = 入渗率敏感性（3.0 mm/h 上限）/ B 档沟 / DEM 高程基准（G7）/ S1 口径（G17）。
6. **探针声明（随所有 r3 成果强制出现）**：入渗率与沟参数均为情景假设；入渗恒定无土壤库（后期可能过排）；DEM 推导沟 ≠ 真实排水网；不得作资产判据。



## 0i. Phase H 第四轮（2026-11-08，排水天花板 / 密度敏感性 / G7-S1 口径复核）

计划基线：ADR-0014 延续 + 用户批准三项后续。矩阵预注册 `data/catalog/guigang_round4_drainage.json`（`guigang-round4-sensitivity-v1`）。定位仍为**诊断探针**。

### 0i-1 分阶段状态

| Phase | 内容 | 状态 |
|---|---|---|
| G0 | 预注册 round4 | **已完成（先于任何 run）**：入渗 3.0 mm/h（fc 先验上限）+ B 档沟（间距 500 m、总长上限 400 km，其余同 A）+ G7/S1 复核任务单 |
| G2 | B 档沟构建 | **已完成（实跑）**：`build_guigang_drainage.py --tier B`（档位来源表化：A→round3 / B→round4）→ **27 条 / 402.0 km / 刻蚀 3,856 格 / max 下切 4.88 m**（`channel_recon/d14m_drB/`）；A 档重构回归通过 |
| G3 | 3 个 run | **已完成（exit 0，E_mass 全 PASS）**：质量门 **61 PASS / 3 FAIL-EXPLAINED / 0 FAIL（64 run）**；S1 十八情景出表（自校验 all_match=true）。结果见 0i-3 |
| G3b | G7/S1 口径复核 | **已完成（只读）**：`scripts/stage6/review_guigang_g7_s1.py` → `guigang_g7_s1_review.json`。三项发现见 0i-4 |

### 0i-3 第四轮实测结果（t=228 h，S1 足迹内口径）

| run | 机制 | FAR | CSI | POD | 足迹内 km² | E_mass |
|---|---|---|---|---|---|---|
| chhyd_d14（锚点） | — | 0.925 | 0.068 | 0.437 | 65.68 | PASS |
| chhyd_d14_r3_infil2 | 入渗 2.0 | 0.863 | 0.114 | 0.403 | 33.31 | PASS |
| **chhyd_d14_r4_infil3** | **入渗 3.0** | **0.720** | **0.196** | 0.394 | **15.90** | PASS |
| **chhyd_d14_r4_drB** | **沟 B 档（27 条/402 km）** | 0.925 | 0.069 | 0.437 | 65.48 | PASS |
| **chhyd_d14_r4_drB_infil3** | B 档 + 入渗 3.0 | **0.720** | **0.196** | 0.394 | 15.92 | PASS |

### 0i-4 归因结论（如实登记）

1. **垂直排水天花板量化**：入渗 3.0 mm/h（fc 先验上限）→ FAR 0.925→**0.720**（−20.5 pp）、CSI 0.068→**0.196**（有雨情景历史最高，超过无雨 flow_only 的 0.164）、足迹内模拟水 15.90 km²（接近观测 11.29）。求解器入渗排掉 ~94% 雨水（res.mass 反算 Inf≈3.15e8 m³），FAR 甚至跌破「无雨下限 0.817」——入渗连上游漫溢积水也排。**3.0 是先验上限，不再外推**；恒定率无土壤库的过排风险在此档最大（已登记）。
2. **水平轨结论升级**：B 档（402 km，密度 2.7×A）仍与锚点逐位一致（FAR 0.925）→ 按预注册口径，水平轨结论升级为「**本分辨率与口径下无效**」而非「密度不足」；`r4_drB_infil3` 与 `r4_infil3` 逐位一致。100 m 格元平均的 1 格宽沟（3,856 格=38.6 km²，max 4.9 m）在 t=228 h 足迹内无可测效果。
3. **G7 复核（证据增强，基准仍不闭合）**：19 个 run 的 apparent offset 范围 14.79~23.24 m（spread 8.45 m）——非确认常数，维持「不得当 datum_offset_m」结论。
4. **S1 口径敏感性量化（G17）**：-17 dB 与原始掩膜一致率 0.9998（校验通过）；阈值 ±2 dB 使观测水面积在 6.68~15.88 km² 间摆 2.4 倍——**口径不确定性与情景间差异同量级**，任何 CSI/FAR 结论都须在「-17 dB 固定阈值」限定下表述。
5. **常水第二时点交叉验证（决定性物理证据）**：09-05 常水观测 vs 模型充槽期（t=60 h）足迹内对比——无河槽 run（基线族）CSI 仅 **0.01**（模型漫灌平原 105 km²，S1 常水仅 ~2 km² 级）；**全部 chhyd run CSI 0.276~0.305、FAR ~0.31-0.34、模拟 13~16 km²**——重构河槽在纯基流充槽下复现 S1 常水水体。**这是河槽位置正确性的独立验证**（不依赖洪水时刻、不依赖降雨/入渗口径），且对降雨/净雨/排水机制不敏感（各 chhyd 变体一致）。

## 1j. Phase H：学术成果驱动改进（2026-11-22，ADR-0015~0018；计划即授权）

计划：`plans/广西二维水动力模拟_学术成果驱动改进实现计划.md`（用户 2026-11-22 批准，§5.3 五 run 授权）。

**已完成（零重跑部分）**：
1. **M0**：ADR-0015 学术映射冻结 + FAT/归因/概率产品口径预注册。
2. **M1（T02–T04）**：FAT/超阈时间产品 **61/61 PASS run 全覆盖**（贵港 26 + 久隆 12 + 宁明 23，S1 达成）——首湿/超阈（0.3/0.5/1.0 m 情景阈值）/历时 GeoTIFF + 三级窗分布 + 站址暴露三元组表（`outputs/products/<site>/fat/`，审计 `data/catalog/fat_products_audit.json`）。**贵港成因归因产品**（S2 达成）：规则先冻结（`guigang_genesis_rules.json`）后计算——pluvial 185.44 km² / river 20.37 km² / disagreement 2.66 km²（定量证实滩地降雨积水主导；d14/d10 河槽差异为已注册混淆项）。
3. **M2（T05–T09）**：`guigang_inflow_priors.json`（郁江 87,780 km² 径流深反推 + 明江 6,379 km² 全流域上限，**待用户复核**）；**GR4J 型集总产汇流**（ADR-0016，纯 numpy，守恒残差 0；IHC 暖启动 0.726 vs GA 映射 0.702 一致）→ 贵港/宁明 inflow bdy + 3 个授权 run 目录已组装（`scen_gr4j_q_100m`/`scen_gr4j_q6000_100m`/`scen_zdown_a_100m`/`scen_zdown_b_100m`）；**GloFAS 先验对照（T07/S3）**：贵港事件窗 **r<0.5 → 按预注册退回三角情景线**（全窗 r=0.538）、宁明事件窗 r=0.255（仅 5 日，记未验证）；GR4J 峰值早 1–2 天、量级偏大（集总模型无传播时滞，已登记）；**IMERG（T08）与 ATL13（T09）无凭证** → 按计划 §6.4 记「数据不可得」，S4/S5 **未验证**，脚本+合成单测落地。
4. **M3（T12–T14，可先行部分）**：ADR-0018；**宁明 9 run 集合超越概率图**（S6 前半达成；成员全部质量门 PASS 强制清单，thr0.05 面 126.27 km² / thr0.3 面 110.35 km²，审计 `ningming_probability.json`）；**Copula 联合阈值探索**（48 年 AM3D×P30，三族 AIC：t-copula 最优 ν=2 ρ=0.344，AIC −21.5；Bootstrap CI [0.09, 0.62]——探索性，禁业务告警）；MMF 差异表骨架（`mmf_rainfall_evaluation.json`，S4 未验证待 IMERG）。
5. **测试**：新增 6 个测试文件 46 项（FAT 11 + 归因 7 + GR4J/入流 11 + IMERG 5 + 概率 5 + 基准探针 7）；全量回归见 §2 门禁矩阵更新。

**待执行（机时，run 已组装待启动）**：5.3 授权清单 5 run（贵港 4 + 宁明 1，各 ≤24 h）。注意：`scen_gr4j_q_*` 建议降级为敏感性执行（T07 r<0.5 预注册结论：三角情景线仍为生产线）；`scen_zdown_a/b`（HVAR 顶托）与 `scen_imerg_ga`（待 IMERG 凭证）不受影响。run 落 FAIL 即停线（zdown 系列 FAIL-EXPLAINED 已预注册）。

**新增文件**：`scripts/stage6/{make_fat_products,make_genesis_attribution,ensemble_probability,copula_thresholds}.py`、`scripts/stage3/{build_inflow_hydrology,verify_inflow_vs_glofas,prep_imerg_rain,icesat2_datum_probe}.py`、审计件 `data/catalog/{fat_products_audit,guigang_genesis_rules,guigang_genesis_products,guigang_inflow_priors,inflow_audit,inflow_glofas_check,imerg_prep_audit,gx_datum_probe,ningming_probability,copula_thresholds_report,mmf_rainfall_evaluation}.json`。产品根：`outputs/products/{guigang,jiulong,ningming}/`（fat/genesis/probability）。

## 1k. Phase I：久隆钦江双驱动重构与离散水点全链条治理（2026-11-22，ADR-0019~0022；计划即授权）

计划：`plans/code_artifact.md`（用户 2026-11-22 批准执行五项前置任务 + 任务 4/5 lisflood.exe 运行）。

**任务 1（斑块归因）——已完成**：`diagnose_jiulong_patch_attribution.py` 对 R0 基线 1,107 个湿斑块完成 P01–P08 全分类（P04=882 / P02=123 / P01=60 / P08=18 / P07=12 / P06=10 / P03=2 / P05=0），15 项指标含 HAND（PF+ε）/距河距离（OSM 干流 BFS）/轨迹匹配（1085/1107）；产物 `diagnostics/jiulong/patch_attribution/`，20 项单测过。
2. **任务 2（水系建库）——已完成**：A1=1,775 km²（GLO-30 派生，与陆屋 1,400 km² 先验自洽；`jiulong_catchments.json`）；钦江干流 = OSM 本地 PBF（用户当日提供 china-261121.osm.pbf，12 段）+ HydroRIVERS NEXT_DOWN 链双源，S1 常水核验 0.31；入域断面 (0,156)（N 缔）/出海 (151,26)，8 邻域 0 断裂；SGC 输入 sgc_width/sgc_bank 落盘；拓扑审计：纵向局部倒坡（200 m 格固有，max 8.4 m）如实登记；支流 CATCH_SKM≥20 无相交命中（v1 限制）。
3. **任务 3（小时级 Q(t)）——已完成**：GA+SCS-UH 形态 + GR4J 日尺度体积先验缩放（限幅 [0.4,2.5]，实际 0.63）；定版峰值 **1,398 m³/s @ t0+69 h**、体积 97 Mm³（系数 0.44=GR4J 先验）；`validate_jiulong_hydrograph.py` 全项 PASS；A2 净雨 50.7 mm（暖启动+饱和后 drainage=Ks 口径）。防重复计量：QVAR/`.rain` 分账 + par 禁 infiltration 断言。
4. **任务 4（SGC 核验 + §4.5 八项合成电池）——已通过（7/8）**：SGC 三项小算例（输水/漫溢/退水）全 PASS；§4.5 八项电池 s2/s3/s4/s5/s6/s7/s8 PASS（含同边双 QVAR 实测可行、HVAR 缓升 G13 门通过、干湿转换排空），**s1 蓄水按判据如实 FAIL**（给定雨量下示例洼地物理上不可达 1.0 m，薄层汇入约 131 m³/总雨 2400 m³，1 h 内蓄水仍在进行）——overall_pass=false 如实登记，未调参掩盖；单测 24 项过；`diagnostics/synthetic/sgc_report.json`。
5. **任务 5（R0–R4 对照）——已完成 5 run**（R0=只读基线；r1_v3/r2a_v4/r2/r3 PASS，r4 FAIL-EXPLAINED 感潮口径）：R1 净雨单变量湿 187.5 km²；R2a 来水摊散 58.8 km²；R2 SGC 河道 55.0 km²；**R3 双驱动主情景 217.5 km²**（∫Qin 97.3 Mm³ 与 bdy 理论值闭合 9e-6）；R4 感潮 260.1 km²（E_mass 7.8e-2，HVAR 政策 FAIL-EXPLAINED，G13 同病）。
6. **因子差分（`compare_jiulong_scenarios.v2`）**：来水效应（R3−R1）= +30.0 km² 且碎斑 −74（沿河集中）；净雨效应（R3−R2）= +162.6 km²（面状积水）；河道 SGC 效应（R2−R2a）= −3.8 km²（集中于槽）；合计（R3−R0）= −45.7 km²（GA 损失压掉粗雨高估）。
7. **S1 验证（`validate_jiulong_inundation.v1`）**：R3 CSI 0.043 / POD 0.373 / FAR 0.954（基线 0.018/0.387/0.982）；5 d 窗内 R3 退水方向检验不适用；**R3 13 d 退水窗（r3rec，09-05..09-17）已跑**：E_mass 7.6e-6 PASS、res.max 233.8 km²、峰值帧 204.3 km²@294 h；退水方向检验 **未达成**（09-10→09-17 模型 +17.5 km² vs S1 −5.84 km²），较旧基线同期 +27.7 km² 改善 37%；剩余主因 = 无显式排水网 + 退水期净雨 25.7 mm 持续补给。v1 因 13 d 净雨切片 bug 作废留痕（`*_v1invalid`）。
8. **质量门 SSOT 刷新**：**PASS 66 / FAIL-EXPLAINED 4 / FAIL 0（70 run）**（+4 PASS：R1/R2a/R2/R3；+1 PASS：R3 13d 退水窗；+1 FE：R4）；g15 公式与政策未改。
9. **工程**：新增脚本 12 个（stage3×8 + stage6×4）、共用底座 2 个、测试 6 文件 76 项、ADR-0019~0022、`configs/cases/jiulong_hydro.yaml`；全量回归 512+ 过（见门禁矩阵）。作废 run 留痕：`*_v1invalid/_v1invalid_v2/_v1invalid_v3`（rain 首行缺换行 / QVAR 三格段口径 / 中途换强迫），均被 g15 EXCLUDE_MARKERS 排除。
10a. **DEM_REPAIRED v2（§4.4/M1 后续项已落地，2026-11-23）**：`prepare_jiulong_dem_layers_v2.py` —— 负值区 7,838 格（313.52 km²）弃 BFS 平推、改 GLO-30 基准对齐融合（offset −22.766 m，G7 未闭合的量化体现）；LAND/SEA/NODATA 三掩膜独立落盘；融合体积 −3,011 m³（洪峰蓄量 2.36 亿 m³ 的 1.3e-5，3% 上限内）；7,498 格近岸负值按 GLO-30 证据保留不钳零（reason=3）；契约检查（S1 保护/HYDRAULIC=REPAIRED/融合仅限未解析负值）全过；单测 3 项 + v1 契约 43 项全过；catalog `jiulong_dem_layers_v2.json`。本层为候选输入，未用于任何 run 重算。
10b. **发布包 v10**：floodviz `v10-20261122` 已构建并通过 validate --full（3 案例 / 17 run + 6 产品图层）；新增 5 个久隆 run 入包（R1/R2a/R2/R3 + R3 13d 退水窗），R4 感潮 FAIL-EXPLAINED 不入包；`configs/cases/jiulong.yaml` 同步 5 条目；`_select_runs` 口径：`optional: true` 的 run 不进包（recession/100 m 维持该设计）。成因分层视图已入 v10：`patch_class` 新 kind（8 类离散色带+类名图例，修复产品 run 误用深度色带的既有缺陷）、4 项新单测（floodviz 263 passed）、审计件 `flood_visualization/data/catalog/jiulong_patch_attribution_layer.json`（逐类像元数 P01=196/P02=3542/P03=32/P04=2698/P05=0/P06=15/P07=74/P08=24，与父项目 class_summary 吻合）。
10. **环境记录（2026-11-22）**：E 盘出现会话级大写限流（>64 KB 逐级收紧至 <8 KB 后部分恢复），R2/R3/R4 改在 D 盘临时工作根执行后按 ≤24 KB 分块回拷；r4 需单一 bdyfile（qinjiang+qztide 并入 inflow_tide.bdy）。
11. **待办**：IMERG 凭证已配（.netrc+GES DISC 应用已批准），2026-11-22 当晚网络 TLS 间歇未通，通后按 ADR-0019 换 run tag 升级 R1/R3 降雨；陆屋站实测资料获取（方案 A）；display_qc 双门控修订（ADR-0021 §5）。

## 1l. Phase J：官方水位监测核验、PipeNetGen 虚拟管网与南方五省区扩展（2026-11-25，ADR-0023~0024）

计划：`plans/基于官方水位监测核验与PipeNetGen虚拟管网的水动力模型改进实现计划.md`（用户批准，严检 `lint_plan.py --strict` 0 errors）。

1. **官方水位接入与连续水文核验（Track 1，ADR-0023）**：
   - 新增 `src/hydro_refine/monitors/{datum_converter,hydro_evaluator,gauge_ingestor,gauge_extractor,boundary_assimilation}.py` 与 `scripts/stage3/ingest_official_gauges.py`、`scripts/stage6/validate_water_levels.py`，落盘 `data/catalog/gauges/{guigang,ningming,jiulong}_gauges_normalized.json` 与 `data/catalog/gauge_validation_results.json`；
   - **宁明 4 站 SHP 实测核验**：闭合东缘短路边界（`scen_q6000_n100_100m_choff_eclosed` vs `scen_q6000_n100_100m`）使宁明县城站基准无关相对涨水 `nse_relative_rise` 从 `0.810` 提升至 **`0.926`**、洪峰滞后从 `+10.0 h` 压降至 **`+4.0 h`**、派连河板祝支流站从 `-0.706` 升至 **`0.514`**；
   - **贵港双锚点虚拟水尺**：OSM 郁江河道吸附（`snapped_channel`，偏移 `2,512.7 m`）在 `chhyd_d14` 检出主槽模拟涨水 `5.814 m`（实测 `7.50 m`），同时定位三角入流洪峰提前 23–41 h 的同化修正靶标；久隆 K88+000 站强制标注 `qc_flag=9` 与 2024 工况隔离；
   - 公开门户反混淆与 fail-closed：水利部 `xxfb.mwr.cn`（`#FontTag`）与珠江委 `zwswj.com`/`pearlwater.gov.cn`（HTTP 412 JS WAF）严格登记阻塞不绕过；广西开放平台 `gxsjkf.dsjfzj.gxzf.gov.cn:8183` 登记需用户授权与 `--extra-csv` 导入口径。
2. **PipeNetGen 虚拟管网与 1D-2D 协同排涝（Track 2，ADR-0024）**：
   - 零 GPLv3 开源合规栈（`networkx` + `swmmio` + `pyswmm`，排除 GPLv3 的 `hydroeval`）构建 `src/hydro_refine/{pipenet,coupling}/`；
   - 基于真实 `china-261121.osm.pbf` 与 DEM 生成三案例 0 逆坡 DAG 管网：贵港（10,258 节点 / 10,205 管段 / 468.62 km）、久隆（1,559 节点 / 1,463 管段 / 67.465 km）、宁明（3,002 节点 / 2,916 管段 / 125.855 km）；
   - PySWMM 动态波（Windows 纯 ASCII 暂存转接）与城区减涝对比：贵港 1D 6 h 排河总量 **`3,967,523.6 m³`**（峰流 `379.5206 m³/s`，等效排涝深 `0.0984 m`），诊断投影使城区湿斑块 **312 → 275（-11.86%）**、湿面积 **-25.75%**、积水体积 **-13.87%**；
   - 微型城市格实跑 LISFLOOD-FP 8.1.0 真实 1D-2D `QVAR` 点汇/排口双算例均达 **`E_mass <= 1e-3` PASS**，隔离存放于 `outputs/pipenet/` 与 `outputs/synthetic/`，零污染既有质量门 SSOT。
3. **南方五省区（粤、桂、黔、滇、琼）同步水情采集与跨省算法核验**：
   - `five_provinces_harvester.py` + `fetch_five_province_hydro.py` 探测 16 项五省及流域公开接口（`9/16` HTTP 200 可读）：海南 `swt.hainan.gov.cn` 明文采集 10 水库+3 江河实时水位及 `sq_map.asp` 331 站阈值档案；广西 `slt.gxzf.gov.cn` 提取 2026-08 洪水响应时间窗；开放平台与 WAF/字体混淆均如实登记状态；
   - 对接 Open-Meteo GloFAS v4 API 拉取五省 9 个控制断面连续流量，验证 3×3 `0.05°` 干流像元吸附对广东北江石角（量级比从 `0.001` 恢复至 **`1.081`**、`lag=-1 d`）、韩江三河坝（`0.764`、`lag=-1 d`）及广西梧州（`1.151`）的修正效能；
   - 单遍抽取五省 5 代表城区（广州/贵港/贵阳/昆明/海口）真实 OSM 路网水系并接入 Copernicus GLO-30 30m 真实地形完成 PipeNetGen + PySWMM 动态波泛化验证：`5/5` 全部满足 `is_dag=True`、`negative_slope_count=0` 且全部求解闭合（连续性误差 `0.001%~0.04%`），结果见 `data/catalog/{five_provinces_hydro_snapshot,five_provinces_algorithm_verification}.json`。

## 1m. Phase K：三案例管网耦合**物理重跑**与隔离对照基线（2026-11-25，ADR-0025）

计划：`plans/pipenet_3cases_coupled_rerun_plan.md`（用户批准；宁明基线口径按用户选择改为「自产基线 + 耦合」双算例）。

1. **耦合内核分层（ADR-0024 补记）**：`drainage_kernel.py` 区分两条路径——`build_lisflood_drainage_bdy_and_bci`（粗聚合，簇内雨水压到单格抽取，会触发 `H<0` 钳零，仅诊断用）与新增 `build_fullgrid_coupled_forcing`（全网格，逐格点汇恒 ≤ `0.85 × 该格瞬时净雨`，数学保证零负水深）。物理重跑只用后者。
2. **全网格耦合边界实测**（`build_fullgrid_coupled_forcing`，逐时刻质量闭合相对误差约 `1e-7`）：
   - 贵港：`4,032` 点汇 / `10` 能力档 / `39` 注入像元（`53` 排口节点按拓扑分账后合并），城区 `39.91 km²`；
   - 久隆：`394` 点汇（`395` 原始格 −`7` 个 SGC 河道像元，能力**迁移**至邻格而非丢弃，得 `394`；`unique_series_per_cell=True`）/ `28` 注入像元（`76` 排口节点）；
   - 宁明：`1,206` 点汇 / `30` 注入像元（`86` 排口节点）。
   - 三案例排口落格与城区点汇格 **100% 重叠**，处置为注入点 BFS 移位（不删格、不丢排涝能力）。
3. **隔离目录实跑**：`outputs/pipenet/<case>/run_{baseline,coupled}_*/`，基线为同批自产算例（ADR-0025 决策 1/2），唯一变量为管网点源项。
4. **质量门零污染机制**：`g15_mass_recheck.py` 用显式三案例根白名单（`CASE_ROOTS`），`outputs/pipenet/` 天然在扫描外。回归测试改为锁定**机制**（`pipenet` 不在三案例根内、`CASE_ROOTS` 恰为三项白名单），不再断言「pipenet 下无 `res.mass`」——该旧断言会被本阶段正确产物触发。
5. **执行前核实并修正的计划假设**（源码级）：
   - SGC 的 OpenMP 竞争不存在：`iterateq.cpp:118-141` 的 SGC 分支是死代码；真实路径为 `lisflood.cpp:766` → `Fast_MainStart` → `lisflood2/sgm_fast.cpp:3696`，点源插值在 `#pragma omp single` 串行区。
   - SGC 模式下**任何非零 `QVAR` 均引入与流量同量级的体积残差**（单变量实测：负源 +1103.9 m³ / 正源 +126.1 m³ / 成对 +1100 m³ / QVAR 恒零 0.0 / 纯雨无点源 0.0）。该限制已写入 ADR-0025 与冒烟测试。
   - `SGCbank` 文件语义是**河道床底高程**（`input.cpp::LoadSGC` 第 2217-2228 行读入 `SGCz`），不是水深。
   - `.rain` 首行会被 `LoadTimeSeries(skipFirstLine=ON)` **无条件跳过**，必须为注释行。
   - `capacity_cap_mmh=80` 在三案例**均未生效**（`0.85 × 雨率峰值` 仅 11.98 / 9.01 / 3.87 mm/h），结果对上限取值不敏感；真正约束为 `safety_factor=0.85`（数值安全系数，非水力标定）。
6. **宁明基线口径验证**：自产 `run_baseline_c100m` 的 E_mass = `1.032e-4`，与原 `choff_eclosed` 的 `0.0001032` **完全一致**，证明装配忠实复现东缘关闭边界，未引入偏差，同时不触犯该 run 的 `diagnostic_only` 禁令。贵港自产基线 `1.593e-4` 亦与既有 `chhyd_d14` 的 `0.0001593` 一致。
7. **三案例物理重跑结果**（`data/catalog/pipenet_coupled_rerun.json`，全部 PASS）：

| 案例 | 基线 E_mass | 耦合 E_mass | 壁钟(基/耦) |
|---|---:|---:|---|
| 久隆 200 m | 7.639e-06 | 7.746e-06 | 90.9 / 84.2 s |
| 宁明 100 m | 1.032e-04 | 1.057e-04 | 1141.6 / 870.3 s |
| 贵港 100 m | 1.593e-04 | 1.577e-04 | 2431.5 / 2590.6 s |

   耦合 E_mass 与基线同量级，说明逐时刻守恒的点源项未引入额外误差（不是「跑出 PASS」，而是「没有变差」）。

8. **四维评估结果**（`data/catalog/pipenet_coupled_rerun_evaluation.json`，`physical_recalculation=true`）：

| 案例 | 城区湿面积削减 | 斑块削减 | 体积削减 | 机制判读 | 全域体积变化 |
|---|---:|---:|---:|---|---:|
| 久隆 | 9.00% | 15.79% | 10.51% | 本地降雨主导（等效水深 0.067 m） | +0.07% |
| 宁明 | 0.19% | 1.47% | 0.05% | **外来顶托主导**（等效水深 2.988 m） | −0.01% |
| 贵港 | 12.62% | 6.85% | 19.49% | 本地降雨主导（等效水深 0.163 m） | −0.81% |

   - **宁明削减近零是物理机制，不是耦合失效**：城区峰时体积换算均匀水深 2.988 m，积水来自明江上游来水顶托；管网只按本地瞬时净雨限幅抽排，对 18 m 深的外来顶托水无效。该判读由 `dim1_interpretation.regime` 自动判定（阈值 1.0 m），不依赖人工注释。
   - **全域体积变化均 ≤1%**，证明回注未造成异常抬水或边界滞留。
   - S1：久隆 CSI 0.0428→0.0432（Δ+0.0004）、FAR 0.9537→0.9533；贵港 CSI 0.0665→0.0665（Δ0）；宁明 S1 **如实不可用**（事件 2026-08，现有水体层 2024-09，不同期）。
   - 站点 ΔH：贵港 2 个探针可算（原锚点主槽洪峰 24.773→24.175 m，Δ−0.598 m）；宁明 2 个（县城 Δ−0.001 m）；久隆如实无可用观测（`qc_flag=9` 按既有设计排除 2026 工况）。
9. **执行中修掉的实现缺陷**（均已在代码留注释）：
   - 分档原用分位数，因 80 mm/h 上限处有大原子（贵港 2808/4032）而压成 2–3 档 → 改容量值域等分（10 档）。
   - 排口重叠格原被删除，会丢失该城区排涝能力 → 改注入点 BFS 移位，重叠格保留为点汇。
   - SGC 河道像元原被直接删除 → 改能力**迁移**到邻格（`_remap_after_exclusion`），排水总量守恒。
   - 排口分账原按 `rc in cap_m3s` 过滤，SGC 迁移后久隆全部受影响雨水口被误判无归属、回注为 0 → 改按节点归属定权。
   - 多排口经 BFS 落到同一像元 → 按唯一注入像元合并曲线（正负同格会互抵）。
   - 编排脚本 catalog 整体覆盖，`--case ningming` 会抹掉 `jiulong` 记录 → 改合并写入，并新增 `rebuild_pipenet_catalog.py` 从磁盘补齐。
   - 评估脚本臆造 `evaluate_station_fit` 键名（`wse_peak_m`/`delta_h_sim_m`），导致三案例站点全被判「无有效观测」→ 改用真实键名（`sim_peak_wse_m`/`sim_rise_m` 等），修复后贵港/宁明 ΔH 才可算出。
   - `capacity_cap_effective` 原只有单一布尔，无法区分「上限截断了输入分布」与「上限改变了抽取量」→ 拆为 `cap_truncated_cells` / `cap_binding_cells` / `capacity_cap_effective` 三字段。

| 终稿要求 | 当前证据 | 状态 |
|---|---|---|
| 阶段 0 授权数据/地形/独立观测/RACI | G7/G12/许可/RACI 未闭合；ADR-0004 降级 | **未满足（降级接受）** |
| 阶段 1 久隆基线（结构/河槽/边界账目） | DEM/ERA5/糙率先验；无堤防/桥涵/站门资料；实际 200 m | **部分** |
| 阶段 2 七项基准 + 结构定量 | B1 PASS；B2 质量 FAIL；B3 部分；B4 PASS；B5 性能；B6/B7 诊断；B8 PASS_PARTIAL（B8a PASS / B8b 不一致→ADR-0008 定版约束） | **未过门（B2 明示）** |
| 阶段 3 久隆率定/独立验证 | 5 情景 + ztide；无近洪峰观测；**退水期 S1 弱验证已做（WEAK，方向相反证据已登记）**；网格/时间步敏感性已补 | **部分/未过门** |
| 阶段 4 宁明河道—漫滩/喀斯特/验证 | 12 独立配置；时刻切片 CSI 0.357；喀斯特有界情景（B3 强杠杆）；空间 Manning（B0 弱杠杆）；单事件 | **部分/未过门** |
| 阶段 5 贵港区域 30 m + 局部 5 m + 单向接口 | 200 m 走廊基线 + **30 m 全域已完成（493.36 km²）** + 等宽 100 m 对照；站区局部 5 m/接口未执行 | **部分（区域 30 m 已达成，局部降级）** |
| 阶段 6 业务化 | run_case/RUNBOOK 雏形（入口缺陷已修）；无在线源/滚动/容错/SLA | **未执行** |
| 13.1 数据门禁 | input_manifest 已扩至贵港/S1/GF-3/配置/ERA5 延长段；许可/G7/G12 仍开放 | **部分** |
| 13.2 质量守恒 | **66 PASS / 4 FAIL-EXPLAINED（z05/z10/ztide/r4）/ 0 FAIL**（70 个 run，2026-11-24 权威 SSOT `data/catalog/mass_recheck_g15.json`；含久隆双驱动 R0–R3、SGC 亚网格河道、贵港水力河槽与排水探针、学术 Phase H 产品 run） | **部分（有明示不合格）** |
| 13.3 水文/空间验证 | 无连续水位/流量过程；宁明时刻切片 CSI 0.357；贵港 S1 足迹 CSI 0.043；久隆退水 S1 WEAK（基线 0.018 vs GA 重跑 0.018 vs R3 双驱动 0.043，S1 假警报压降 23.5%）；**贵港 09-12 单景 S1 四情景 CSI 0.076~0.164、FAR 0.817~0.921（仅足迹内，非硬门）** | **未满足** |
| 13.4 性能/复现/站区安全 | **三案例冻结重跑逐位一致（2026-10-27）**；run_manifest 模块全接入；无站区业务阈值 | **部分（复现项已闭合）** |
| 十三章测试 | **593 passed + 3 skipped，0 failed**（2026-11-25 全量 `pytest` 实跑通过；含 Phase K 新增 4 项耦合测试与 4 项 g15 白名单回归） | **达标** |
| 十六章 ADR | **ADR-0001~0025 全量落盘（新增 0023 官方水位核验、0024 PipeNetGen 虚拟管网耦合、0025 三案例耦合物理重跑隔离算例口径）**，SSOT 与 README/MASTER 索引全同步 | **达标** |

## 3. 前轮已完成（Phase A/B 摘要，详见 git 历史/前版）

- Phase A（2026-10-26）：贵港湿像元 WSE（25.06/21.92/20.00，offset 非常数不作基准）、久隆站址±2格主口径（0.547 m）、宁明 78h 时刻切片（CSI 0.357）、质量门三分、产品口径修正、追溯（run_manifest/code_manifest）、覆盖保护（RUN_TAG）。
- Phase B（2026-10-26/27）：B0 空间 Manning（<2% 弱杠杆）、B2 结构 B8（B8a PASS/B8b 差异）、B3 喀斯特标量入渗（强杠杆 1.94→0.92×）、B1 网格/时间步 9 run 全表、B4 6h 输出（到达 54.0 h）、B5 100 m 敏感性。

## 4. 仍未完成/阻塞

| 项 | 需要 |
|---|---|
| Phase B5 收尾 | ~~已完成 2026-10-27~~：30 m 全域 493.36 km²、E_mass 5.7e-5 PASS、已并表 |
| 贵港 5 m 局部模型 + 区域—局部单向接口 | 站区测绘/竣工资料 + ADR-0005 范围决策 |
| G7 垂直基准 | DEM 来源/控制点（业务） |
| G12 宁明站坐标 | 供方核证 |
| 授权 Q(t)/Z(t)/雨量/潮位/断面 | 业务部门 |
| 在线预报/滚动/容错（R10） | 实时数据 + SLA |
| RACI/独立审查/许可外发 | 用户/业务侧 |
| 模型升级时 bridge/orifice 再验证 | 按 ADR-0008 第 3 条（T016c 双构建复验） |
| git 提交 | 2026-10-30~31 用户授权分批入库：主工程 README/docs/configs/data-catalog/src/scripts/tests/uv 元数据 + floodviz 源码/测试/文档/QA 证据 + plans + 根级交付报告已进 git；大文件按 `.gitignore` 排除（data ~10.9 GB、outputs 运行产物、logs、models、.env、web/.dev 再生产物不入库，仅本机留存） |

## 5. 结论

当前可交付："降级模式情景级成果 + Phase A/B/C/D 完整性与敏感性补课 + Phase E 久隆治理 + Phase F/G 贵港水力河槽与排水治理 + Phase H 学术成果驱动改进（FAT/成因归因/集合概率） + Phase I 久隆钦江双驱动重构与耦合治理（ADR-0019~0022） + 明示的门禁状态"。

**当前最新证据链（2026-11-24 快照）**：
1. **久隆钦江双驱动重构与河道耦合**：实现小时级 $Q(t)$（洪峰 1,398 m³/s、注入 97 Mm³）与 GA 净雨（50.7 mm）A1/A2 分账防重复计量；SGC 亚网格河道（宽 140 m、满槽深 4.5 m）免除人工刻蚀，四道护栏通过；主情景 R3 湿面积 217.52 km²，较粗雨基线净压降 45.7 km²；S1 CSI 提升至 0.043；P01–P08 对 1,107 个斑块实现 100% 物理成因归因。
2. **贵港水力约束河槽与排水连通**：基于水力几何 Manning 反演构建五套重构 DEM，输水核验全过；实施入渗 3.0 mm/h 探针，S1 CSI 提升至 0.196；明确外洪（20.4 km²）与降雨积水（185.4 km²）归因分界。
3. **学术驱动产品体系**：FAT 淹没历时产品覆盖全量 66 PASS run；建立贵港成因空间归因图与宁明集合超越概率图；Copula 联合阈值与 GR4J/ATL13 探针已完成基线分析。
4. **质量门与自动化测试**：质量门全表权威 SSOT 为 **66 PASS / 4 FAIL-EXPLAINED / 0 FAIL（70 run，全量闭合）**，派生层 `display_qc` 零污染；全量自动化测试 **593 passed + 3 条件 skipped，0 failed**（实跑验证）。在业务授权数据到位前，全部结果严格维持降级模式情景级定位，不得用于确定性业务预警或资产级安全判据。
5. **三案例管网耦合物理重跑（Phase K，ADR-0025）**：贵港/久隆/宁明在 `outputs/pipenet/<case>/run_{baseline,coupled}_*/` 下真实调用 `lisflood.exe` 完成 1D-2D 管网耦合重跑，六个算例全部通过 `E_mass ≤ 1e-3`（耦合值与基线同量级）。四维评估显示贵港城区湿面积 −12.62%、久隆 −9.00%、宁明 −0.19%（宁明为外来顶托主导，属物理机制而非失效）。**`outputs/pipenet` 下 6 个 `res.mass` 未污染质量门 SSOT**（`g15` 白名单机制实测确认）。

### 三案例管网耦合重跑的关键限度（必须随结论披露）

1. **削减幅度不是管网真实潜力**：抽取量为 `min(单格管网能力, 0.85 × 瞬时净雨)`，`safety_factor=0.85` 是**保证零负水深的数值安全系数**，不是管径水力标定值。所有削减百分比应读作「85% 净雨可排假设下的排涝上限效应」。
2. **`capacity_cap_mmh=80` 在三个案例均未生效**：`0.85 × 雨率峰值` 仅 11.98 / 9.01 / 3.87 mm/h，全部低于 80。结果对上限取值不敏感；真正约束为 `safety_factor`。
3. **SGC 模式下非零 `QVAR` 会引入与流量同量级的体积残差**（单变量实测：负源 +1103.9 m³ / 正源 +126.1 m³ / 成对 +1100 m³ / QVAR 恒零 0.0 / 纯雨无点源 0.0）。久隆生产算例 E_mass=7.7e-6 通过，是因为其管网点源通量相对全域降雨/来水极小；**若提高久隆抽取强度，残差会按此比例放大**。
4. **宁明无洪中 S1 观测**：现有 S1 水体层仅 2024-09 两景，与 2026-08 紫檀事件不同期，S1 维度如实记为不可用，不做跨期对照。
5. **久隆无可用站点**：唯一站点 `jiulong_k88_2026_excluded` 被 `qc_flag=9` 按既有设计排除（2024/2026 工况隔离），ΔH 如实记为不可算。
