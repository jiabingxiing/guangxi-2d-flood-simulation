---
doc_type: reference
status: active
topic: execution-status
canonical: true
scope: project
---

# 执行状态与剩余项（对照终稿）

更新：2026-10-31 ｜ 模式：公开数据降级（ADR-0004/0005/0008）｜ **不是计划全部完成，不是业务预警系统**

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

## 1. 总判定

已完成的是"三案例降级模式首轮情景 + 结果完整性修正（Phase A）+ Phase B0–B5 敏感性/结构/喀斯特补课 + Phase C 复现/退水验证/去混杂/定版 ADR/30 m 全域"，不是终稿全部阶段/门禁完成。原阶段 0 门禁未满足；阶段 6 未执行；贵港站区局部 5 m、区域—局部单向接口、宁明 25/12.5 m、久隆 12.5 m 未执行（用户决策推迟）。全部结论为情景级。

## 2. 门禁矩阵

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
| 13.2 质量守恒 | **43 PASS / 3 FAIL-EXPLAINED（z05/z10/ztide）/ 0 FAIL**（46 run，含复现/延长/等宽/30 m/100 m 重跑 run） | **部分（有明示不合格）** |
| 13.3 水文/空间验证 | 无连续水位/流量过程；宁明时刻切片 CSI 0.357；贵港 S1 足迹 CSI 0.043；久隆退水 S1 WEAK | **未满足** |
| 13.4 性能/复现/站区安全 | **三案例冻结重跑逐位一致（2026-10-27）**；run_manifest 模块部分接入；无站区业务阈值 | **部分（复现项已闭合）** |
| 十四章测试 | **42 passed**（2026-10-27 复测；产品重生成后原 2 skip 已实跑） | **部分（覆盖仍有限）** |
| 十六章 ADR | ADR-0001~0010（**0008 模型定版/桥涵限制、0009 宁明刻蚀停用/地形分层已补、0010 久隆直接降雨模型治理规范已补**）；DEM 校正/验证指标 ADR 仍可细化 | **部分** |

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

当前可交付："降级模式情景级首轮成果 + Phase A/B/C 完整性与敏感性补课 + 明示的门禁状态"。本轮新增证据链：①三案例可复现（逐位一致）；②久隆退水验证定量暴露无排水网限制（模型面积与实测退水方向相反）；③贵港面积对网格温和收敛（490–545 km²，±6%），旧"最敏感 +16.3%"系河槽宽度伪影，站址水深 100 m 起收敛（200 m 偏大 15%）；④模型/结构使用边界以 ADR-0008 定版；⑤贵港 30 m 全域完成（终稿阶段 5 区域 30 m 要求达成，站区 5 m 仍为 ADR-0005 推迟）。在授权数据补齐前，不得把结果用于业务告警、确定性防洪结论或资产级安全判据。
