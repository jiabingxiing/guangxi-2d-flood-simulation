# 久隆治理计划 Phase 0/1/2/3-4/5 独立验收审查

- 审查人：Lead（计划 task-6 的独立审查职责；原定审查者未产出结果，由 Lead 终审）
- 审查日期：2026-10-31
- 审查方式：只读核对 + 全量实跑复核。未修改被审对象（除修正两处测试自身数据缺陷与一处报告路径笔误，已在 §5 单列）
- 计划基线：`plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md` v3.0
- 治理口径基线：`2d_hydro_project/docs/adr/ADR-0010-久隆直接降雨模型治理规范.md`

---

## 0. 总判定

**通过（有条件）。** 计划 Phase 0–5 的脚本与产物全部落盘并可复现；六项硬约束实测成立；三处计划自身的口径错误被诊断包更正。

**不通过项：0。**
**未确认项：2**（用户决策类，非执行缺陷，见 §6）。

---

## 1. 硬约束核验（计划 §1.3 / §4 / §7.1）

| # | 硬约束 | 核验方法 | 结果 |
|---|---|---|---|
| H1 | 已有 `res.mass` 的 run 目录不得覆盖 | 扫描 `outputs/jiulong/**` 在会话期间（>2026-10-31 20:40）的修改时间 | **PASS** — 0 个文件被改动 |
| H2 | raw 文件哈希前后不变 | `jiulong_display_qc_audit.json` 的 `raw_integrity`：运行前后 `res.max`/`res.mass`/53 帧 `frame_list_sha256` 逐位相同，`unchanged=true`，`changed_frames=[]` | **PASS** |
| H3 | 派生层不得含 `res.mass` | `outputs/display_qc/**` 递归查 `res.mass` | **PASS** — 计数 0 |
| H4 | 派生层不得污染质量门 | `mass_recheck_g15.json` 中 `display_qc` 相关 run 计数 | **PASS** — 0；counts 稳定 `39 PASS / 3 FAIL-EXPLAINED / 0 FAIL`（42 run） |
| H5 | `FAIL-EXPLAINED` 不计通过 | 读 `mass_recheck_g15.json`；`test_mass_gate_never_reports_na_and_explained_is_not_pass` 断言 | **PASS** — 3 个 FAIL-EXPLAINED 均带 `excluded_from_pass_count=true` |
| H6 | 不修改 `models/`、不重跑 `lisflood.exe` | `models/**` 修改时间扫描；`lisflood.exe` 仅被 `outputs/bench/B9_loss_smoke` 调用（已授权，见 §4） | **PASS** |

**补充证据（H4）**：`g15_mass_recheck.py` 原会被旧展示层
`scen_free_100m_dt2p5_sv6h_realdem_cleaned` 污染——该目录含复制自源 run 的
`results/res.mass`（与源逐位相同）与 `run.log`，满足原扫描条件，会被计为独立 run。
已修：新增 `is_derived_layer()`，判定依据是 `meta.json` 的
`not_physical_recalculation` / `derived_from`，或存在 `clean_audit.json` /
`display_qc_audit.json`；**不靠目录名猜测**。修复后该目录进入 `excluded_variants`，
理由为「展示派生层（非独立模型 run）……按 ADR-0010 第 8 条不参与质量门计数」。
17 个久隆 run 的判定实测：仅 `_cleaned` 为 `derived=True`，其余 16 个为 `False`。

---

## 2. 表述纪律核验（计划 §2.3）

对四份交付文档（`root_cause_report.md`、`ADR-0010`、
`jiulong_phase34_candidates.md`、`execution_status.md`）扫描六条禁止表述。

**PASS。** 命中 11 处，逐条核对后**全部出现在「不得写 / 禁止表述」的禁止性语境中**，
无一例把禁止表述当作结论使用：

- `root_cause_report.md:37` — 「**不得表述为**「质量门全量通过」」
- `root_cause_report.md:135` — 「**禁止表述**：不得写「Priority-Flood 不会制造平坦面」」
- `ADR-0010:33,85,97,99,100,101` — 决策项 5 与决策项 7「禁止表述清单」，原文照录计划 §2.3
- `jiulong_phase34_candidates.md:41,302` — 「**不得**据此写「红壤地区初期必然吸收 30–50 mm」」等
- `execution_status.md:74` — 「**不得表述为**「质量门全量通过」」

---

## 3. 阈值口径核验（计划 §1.4 / §3.3 / §3.4）

| # | 要求 | 核验方法 | 结果 |
|---|---|---|---|
| T1 | 不得写死 4/9 像元或 100 m 单元面积 | 六个新脚本正则扫 `100.0 * 100.0` / `100*100` / `10000.0` / `* 0.04` | **PASS** — 6/6 无命中 |
| T2 | 单元面积从栅格头 `cellsize` 读 | `cell_area_m2 == cellsize_m ** 2` 断言（`test_governance_contract.py`） | **PASS** |
| T3 | 像元数由物理面积换算 | 审计 JSON `threshold_conversion.formula` = `n_cells = ceil(area_threshold_m2 / cell_area_m2)`；`param_sets.*.params` 同时含 `cellsize_m`/`cell_area_m2`/`n_cells_*` | **PASS** |
| T4 | 跨分辨率用同一物理面积阈值 | `resolution_comparison.json` 的 `area_thresholds_km2 = [0.04, 0.16, 0.36, 1.0, 4.0]`，`by_threshold` 每档同时给出 `cells_200m`/`cells_100m` 与两侧面积/斑块数 | **PASS** |
| T5 | 参数在处理前预注册 | `jiulong_display_qc_audit.json` 三档带 `param_hash`；`jiulong_rainfall_loss_matrix.json` `preregistered_before_any_run=true`、`preregistration_sha256=8dbae1f8…` | **PASS** |

**换算正确性抽查**（实测）：

| 物理面积 | 200 m 像元数 | 100 m 像元数 | 计划 §1.4 期望 | 结果 |
|---|---|---|---|---|
| 0.04 km² | 1 | 4 | 1 / 4 | 一致 |
| 0.36 km² | 9 | 36 | 9 / 36 | 一致 |

**T4 的关键结论**（E1）：同一物理面积阈值下，100 m 保留面积恒小于 200 m
（比值 0.79–0.85），斑块数比值 0.70–1.08 无稳定方向。**这证明"只比斑块个数"
不能作为分辨率结论**，计划 §3.4 的判断成立。

---

## 4. 派生标记与账目核验（计划 §5 Phase 1 验收）

| # | 要求 | 结果 |
|---|---|---|
| D1 | `derived_from` | **PASS** — 三档均指向 `outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h_recession` |
| D2 | `not_physical_recalculation=true` | **PASS** |
| D3 | `param_hash` | **PASS** — A/B/C 各一哈希 |
| D4 | 源 run 哈希 | **PASS** — `source_run_sha256`（`res.mass`）、`source_res_max_sha256`、`source_frame_list_sha256` 三者齐全 |
| D5 | `cellsize_m` / `cell_area_m2` | **PASS** — 200.0 / 40000.0，满足平方关系 |
| D6 | 逐帧审计含面积/体积/斑块数/原因码 | **PASS** — `frame_audit_{A,B,C}.csv` + `track_verdicts_{A,B,C}.json` |
| D7 | 超预注册告警值自动阻止默认发布 | **PASS** — 三档 `blocked_for_default_publish=false`；实测最大变化 A 1.43%/1.02%、B 2.49%/1.81%、C 3.06%/2.28%，均远低于 10% 告警 |

**账目可复算性（E1）**：Lead 独立复算 `_cleaned` 旧层的影响，与其登记值逐位吻合：

```
raw  area 271.07 km2  vol 168.43 Mm3  patches 4047
cln  area 171.97 km2  vol  78.90 Mm3  patches  470
delta area -99.10 km2 (-36.56%)  vol -89.53 Mm3 (-53.16%)
```

与 ADR-0010 决策项 8 登记的 −36.56% / −53.16% **完全一致**。该层被退役的依据成立。

---

## 5. 证据等级与脚本契约核验

| # | 要求 | 结果 |
|---|---|---|
| E1 | 根因报告每条结论标 E1/E2/E3/E4 | **PASS** — §5 根因汇总表 12 行（R1–R12）逐行标级；§0.3、§1.1 等段落亦标 |
| E2 | `--dry-run` / `--run-tag` / `--log-json` / `--out-dir` | **PASS** — 六脚本均经 `add_common_args` 提供；`--help` 实测四项齐全 |
| E3 | 目标目录有 `res.mass` 时退出 | **PASS** — 六脚本均引用 `guard_no_overwrite`；`test_guard_refuses_existing_res_mass` 实测抛 `OverwriteRefused` |
| E4 | 输入哈希入 JSON | **PASS** — `baseline_snapshot.json`、`jiulong_improvement_manifest.json` 含 inputs 哈希 |

---

## 6. 未确认项（需用户决策，非执行缺陷）

| # | 事项 | 状态 | 确认方法 |
|---|---|---|---|
| U1 | 方案 A/B 分辨率决策 | **未确认** — ADR-0010 已按计划 §1.4「默认按方案 A」定版为默认工作口径；`resolution_comparison.json` 的 `decision_input.status` 仍记「未冻结：方案 A/B 需用户确认后才能冻结展示阈值」 | 用户按计划 §9.3 第 1 条明确选择 |
| U2 | `display_qc` 是否作前端默认层 | **未确认** — 已产出但未接入前端；当前默认层按 ADR-0010 回退到 200 m raw | 用户按计划 §9.3 第 3 条确认 |

**其余三项（计划 §9.3 第 2/4/5 条）**：第 2 条（100 m 重跑）**未批准，故未执行**；
第 4 条（站址坐标/排水/潮位资料）**未提供，故 Phase 4 物理治理阻塞**；
第 5 条（接受 WEAK/未测）已在 ADR-0010 与 `execution_status.md` 登记为当前口径。

---

## 7. 审查中发现并修正的问题（Lead 处置）

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| F1 | `tests/test_display_qc.py::test_classify_cells_distinguishes_categories` 失败 | **测试自身数据设置错误**：把海面标记混在数值行内（`sea[1,2]=True` 与 `depth[1,2]=0.20` 重叠），海面像元被排除出 `wet_cells`，期望值 3 实得 2。实现正确 | 改为海面独占第 3 行，期望值随之修正为 `sea_cells=3`、`wet_cells=3` |
| F2 | `tests/test_display_qc.py::test_classify_track_filter_requires_all_conditions` 失败 | **测试自身参数越界**：A 档 `shallow_max_area_km2=0.16`、`deep_depth_m=0.30`；用例用 0.12 km² 断言"不再小"、用 0.29 m 断言"不再浅"，两者仍在阈值内，被 `FILTER_SHALLOW` / `FILTER_SUBRESOLUTION` 正确捕获。实现正确 | 面积改 0.20 km²、深度改 0.35 m；并把 0.12 km² 的正确行为（`FILTER_SHALLOW`）补为显式断言 |
| F3 | `root_cause_report.md:6` 称产物在 `diagnostics/jiulong/phase0-v1/`，实际落在 `diagnostics/jiulong/` 根 | **文档路径笔误** | 更正为实际路径，并说明 `--run-tag` 写审计日志到 `logs/diagnostics/` |
| F4 | 队友遗留 14 个 `.tmp_*.py` 探针脚本 + 一个嵌套误建目录 `2d_hydro_project/2d_hydro_project/`（13 个测试产物） | **工作区污染** | 已核验路径与内容后删除；嵌套目录只含 `dem_layers_guardtest` 测试产物，无源文件 |
| F5 | 旧展示层 `_cleaned` 污染质量门扫描 | **真实完整性缺陷**（ADR-0010 决策项 8 已识别） | 修 `g15_mass_recheck.py`，见 §1 H4 |

**F1/F2 的判定依据**：两处均先独立复算实现行为，确认
`classify_cells` 与 `classify_track` 的输出符合计划 §3.3 规则顺序
（豁免优先、过滤为合取），再判定为测试数据错误。**未为了让测试通过而改动实现。**

---

## 8. 实跑复核记录

```
uv --directory 2d_hydro_project run python -m pytest -q
  -> 152 passed in 40.85s

uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py
  -> counts {'PASS': 39, 'FAIL-EXPLAINED': 3, 'FAIL': 0}（42 run）

uv --directory 2d_hydro_project\flood_visualization run python -m floodviz inventory --config configs/default.yaml
  -> 总体：通过（9 run 全 OK）

uv --directory 2d_hydro_project\flood_visualization run python -m pytest -m "not slow and not e2e"
  -> 253 passed, 32 deselected

uv --directory 2d_hydro_project\flood_visualization run ruff check src tests scripts
  -> All checks passed!
```

---

## 9. 覆盖范围与未覆盖范围

**已覆盖**：硬约束 H1–H6、表述纪律、阈值口径 T1–T5、派生标记 D1–D7、
证据等级、脚本契约、账目可复算性、raw 完整性、质量门隔离。

**未覆盖（不在本次审查范围）**：
- 物理正确性。久隆无近洪峰观测、无连续水位/流量过程，CSI/POD/FAR/NSE/R²/水深误差
  全部**未测**；本次审查只能确认「账目闭合、口径一致、改动可追溯」，
  **不能确认模型是否预测正确**。
- 200 m 分辨率能力上限。该限制由 ADR-0010「负面后果」节登记，未解除。
- Phase 2/3/4 的物理重跑结果。未获授权，未执行。
