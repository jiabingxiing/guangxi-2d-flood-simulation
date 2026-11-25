# AGENTS.md —— 工作区导航与约束

本工作区是「广西久隆/宁明/贵港二维水动力模拟」项目。当前结果为**公开数据降级模式情景级**（ADR-0004），不是业务预警系统，不得用于业务告警或资产级判据。

## 目录与职责

- `2d_hydro_project/` —— 模型工程（LISFLOOD-FP 8.1.0，uv 管理）。入口见其 `README.md`。
- `2d_hydro_project/flood_visualization/` —— 发布子工程，只读父项目。**进入前先读其 `AGENTS.md`**。
- `plans/` —— 计划文档。基线是 `plans/广西久隆_宁明_贵港_二维水动力模拟技术路线与工程落地方案_终稿.md`；久隆治理基线是 `plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md`；`current-alternative-plans/` 是未采纳的备选版本；`visualization_research/` 存放核验证据（json/py/pdf）。
- 根目录 md/docx —— 交付报告（成果报告、答辩手册）。**答辩手册只由 `build_complete_v2.py` 生成**（产出根目录 `广西二维水动力模拟项目_全问答与技术答辩手册(手机阅读优化版).md/.docx`，已纳入 git）；首版遗留的 6 个分段生成器已于 2026-11-25 删除，不要再找。阶段成果报告的 md 是真源：改完 `2d_hydro_project/docs/final_report.md` 后跑 `python rebuild_final_report_docx.py` 重生成 docx。
- `2d_hydro_project/diagnostics/jiulong/` —— 久隆诊断派生物（Phase 0）：基线快照、斑块持续性 CSV、DEM 异常分类栅格、根因报告。只读证据，不是运行产物。
- `2d_hydro_project/outputs/display_qc/` —— 展示质控派生层（Phase 1）。**不是物理重算**，带 `not_physical_recalculation=true`；不得用于物理结论或资产判据。
- `2d_hydro_project/outputs/pipenet/`、`outputs/synthetic/` —— 管网耦合物理重跑与合成算例（ADR-0025）。带 `res.mass` 但不在质量门白名单扫描内，**不得**并入 `outputs/<case>/`。

## 硬约束

- 已有 `res.mass` 的 run 目录**禁止覆盖**（终稿 §10）。写入前用 `scripts/common/grid_io.guard_no_overwrite` 检查。
- 运行 Python 前设 `PYTHONUTF8=1`（Windows 中文控制台编码）。
- 环境统一用 `uv`，走阿里云镜像，勿配代理。
- `2d_hydro_project/models/` 是上游 LISFLOOD-FP 源码：禁止修改，禁止重跑 `lisflood.exe`（除非用户单独批准重跑）。
- floodviz 只读父项目 `outputs/**` 与 `data/**`；发布包内禁止绝对路径、用户名、盘符、token。
- 三地不是同一场洪水：久隆/贵港 2024-09 摩羯，宁明 2026-08 紫檀；不共用绝对时间轴。
- 质量门 `PASS` ≠ 预测正确；`FAIL-EXPLAINED` 不计通过；未验证指标必须写「未测」（ADR-0006 / ADR-0010 决策项 5）。
- 久隆治理口径以 `docs/adr/ADR-0010` 与 `ADR-0019~0022` 为准：200 m R3 双驱动（GA净雨+钦江来水+SGC亚网格河道）为主情景（floodviz默认）；旧纯雨自由出流为历史对照基线；100 m 仅敏感性；展示层只允许 `display_qc` 命名且必须带派生标记；禁止写死 4/9 像元或 100 m 单元面积。
- 派生层不得污染质量门。扫描范围是 `CASE_ROOTS` 三案例根白名单（`outputs/{ningming,jiulong,guigang}`）：`outputs/display_qc/`、`outputs/pipenet/`、`outputs/synthetic/` 都在白名单外，天然不计。但**落在某个案例根内部的派生层**（如旧 `outputs/jiulong/.../scen_free_100m_dt2p5_sv6h_realdem_cleaned`）必须带 `meta.json` 派生标记（`not_physical_recalculation`/`derived_from`）或 `clean_audit.json`/`display_qc_audit.json`，否则被计为独立 run（ADR-0010 第 8 条）。

## 验证入口

```bash
$env:PYTHONUTF8 = "1"
uv --directory 2d_hydro_project run python -m pytest        # 587 passed + 3 条件 skipped（2026-11-25 实测全量通过；删除 6 个根级遗留生成器后由 593 降至 587，减少项即 test_no_host_paths 按文件参数化的 6 个用例）
uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py   # 质量门全表，权威值 66 PASS / 4 FAIL-EXPLAINED / 0 FAIL（70 run，全量闭合）
uv --directory 2d_hydro_project run python scripts/stage6/validate_guigang_s1.py  # 贵港四情景 S1 POD/FAR/CSI + 站点水位诊断
uv --directory 2d_hydro_project run python scripts/stage6/validate_water_levels.py  # 官方水位核验（ΔH / NSE 相对涨水，ADR-0023）
uv --directory 2d_hydro_project/flood_visualization run python -m pytest -m "not slow and not e2e"   # 263 passed（32 项 slow/e2e 按标记排除）
uv --directory 2d_hydro_project/flood_visualization run python -m floodviz inventory --config configs/default.yaml   # 24 个 run 条目通过（7 产品图层 + 17 模型 run，其中贵港 3 个是 display_qc 派生层）
```

**注意**：`uv --directory 2d_hydro_project run pytest` 会报
`uv trampoline failed to canonicalize script path`；必须用 `run python -m pytest`。

## 换盘 / 换位置后的首步（可移植性）

项目可整体复制到任意盘符与任意上级目录后运行；`.venv` **不要**手工搬，重建即可。

```powershell
# 1. 自检：会报出陈旧的 editable 安装与"导入自别处副本"
uv --directory 2d_hydro_project run python scripts/maintenance/preflight.py

# 2. 修复（父子工程各一次）：重写 .pth 到当前位置
uv --directory 2d_hydro_project sync
uv --directory 2d_hydro_project/flood_visualization sync

# 3. 日常巡检：盘可读性、备份时效、计数自洽
uv --directory 2d_hydro_project run python scripts/maintenance/health_check.py

# 4. 备份与校验（备份必须落在另一块物理盘）
uv --directory 2d_hydro_project run python scripts/maintenance/make_backup.py
uv --directory 2d_hydro_project run python scripts/maintenance/verify_backup.py --root <备份根>
```

- 源 DEM 等真外部资源由 `scripts/common/paths.py` 解析：`GX_DEM_SOURCE` → `configs/paths.local.yaml` → 项目内 `data/raw/dem_source/`（可为目录联接，`mklink /J` 无需管理员）。
- 备份根用 `GX_BACKUP_ROOT` 指定（本机已设为 `D:\Backups\gx-hydro2d`）。
- 静态门 `tests/test_no_host_paths.py` 会阻止把宿主盘符写回代码/配置。
- 2026-11-16 实测（当时计数快照；当前计数见「验证入口」一节）：项目复制到另一盘符且中间目录改名后，父子工程测试、质量门与 inventory 结果全部与源位置一致。

## 状态与文档指针

- 文档总纲 MASTER：`docs/MASTER.md` 与 `2d_hydro_project/docs/MASTER.md`（Diátaxis 架构与阅读路线图）
- 执行状态 SSOT：`2d_hydro_project/docs/execution_status.md`
- 质量门 SSOT：`2d_hydro_project/data/catalog/mass_recheck_g15.json`（权威值 66 PASS / 4 FAIL-EXPLAINED / 0 FAIL，任何文档的计数与此冲突时以该 JSON 为准）
- docs 索引：`2d_hydro_project/docs/README.md`
- 降雨下渗预处理器：`2d_hydro_project/docs/rainfall_runoff_preprocessor.md`（Green-Ampt/SCS-CN/Horton 土壤水文与物理净雨驱动报告）
- 决策记录：`2d_hydro_project/docs/adr/`（ADR-0001~0025 全量索引，共 25 篇；0010 = 久隆直接降雨治理，0011 = 贵港零散伪影治理，0012 = 贵港水力约束河槽重构，0013 = 贵港组合驱动归因，0014 = 贵港排水连通治理，0015 = 学术改进产品口径，0016 = 上游来水汇流与多源，0017 = ATL13 垂直基准探针，0018 = 概率化探索，0019 = 久隆钦江小时级 Q(t) 与分账，0020 = 久隆 SGC 亚网格河道耦合，0021 = 久隆 P01–P08 斑块成因归因，0022 = 久隆 2024 历史工况与边界重建，0023 = 官方水位监测接入与连续水文核验，0024 = PipeNetGen 虚拟排水管网与协同排涝耦合，0025 = 三案例管网耦合物理重跑的隔离算例与对照基线口径）
- 久隆治理与双驱动脚本：`scripts/common/{grid_io,patch_analysis,event_hydro}.py`（底座）、`scripts/stage3/{build_jiulong_catchments,build_jiulong_inflow,prepare_jiulong_channel,prepare_jiulong_dem_layers_v2,prepare_jiulong_rainfall,run_jiulong_dual_forcing,validate_jiulong_hydrograph,audit_jiulong_drainage,jiulong_100m_rerun,run_jiulong_ga_rerun}.py`、`scripts/stage6/{audit_jiulong_coupling,compare_jiulong_scenarios,diagnose_jiulong_patch_attribution,validate_jiulong_inundation,diagnose_jiulong_artifacts,analyze_patch_persistence,clean_jiulong_display}.py`
- 贵港治理脚本：`scripts/stage6/{diagnose_guigang_artifacts,analyze_guigang_patches,clean_guigang_display,audit_guigang_channel_route,compare_guigang_phase4,summarize_guigang_phase4,diagnose_guigang_phase4_paradox,verify_guigang_patch_mechanism,hist_guigang_patches,validate_guigang_s1,attribute_guigang_combo,diagnose_guigang_ponding,review_guigang_g7_s1}.py`、`scripts/stage5/guigang_scenario.py`（`--rain {on,off,loss_medium,loss_high,ga}`；`--channel {off,linear,hydraulic}`，hydraulic 需 `--channel-dem <channel_recon/dXXm[_drA|_drB]>`；`--infiltration <mm/h>` 路径 B 单用只配 `--rain on`，ADR-0012~0014）
- 贵港河槽重构与排水治理（ADR-0012~0014）：`scripts/common/channel_engine.py`（Manning 反演/圆滑梯形/刻蚀核心）、`scripts/common/net_rain.py`（预处理器净雨读取 + 时基移位）、`scripts/stage3/build_guigang_centerline.py`（OSM→S2→直线回退链）、`scripts/stage3/build_guigang_channel_dem.py`（五套重构 DEM + 几何/输水验证）、`scripts/stage3/build_guigang_drainage.py`（DEM 推导排水沟 A 档，主槽种子传播树 + canyon 守卫）；priors 预注册 `data/catalog/guigang_channel_priors.json`，组合矩阵 `guigang_round2_matrix.json`，排水矩阵 `guigang_round3_drainage.json`，先冻结后刻蚀/跑
- 学术成果驱动改进（Phase H，ADR-0015~0018）：零重跑产品 `scripts/stage6/{make_fat_products,make_genesis_attribution,ensemble_probability,copula_thresholds}.py`（FAT 覆盖 61 个 run = 贵港 26 + 久隆 12 + 宁明 23，2026-11-22 生成；之后新增的久隆 R1/R2a/R2/R3/R3rec 5 个 run **尚未**生成 FAT，需重跑该脚本）；驱动链 `scripts/stage3/{build_inflow_hydrology,verify_inflow_vs_glofas,prep_imerg_rain,icesat2_datum_probe}.py`（GR4J 边界/GloFAS 对照/IMERG/ATL13，凭证缺记数据不可得）；授权 run 队列 `scripts/stage3/queue_phase_h_runs.sh`（zdown_a/b 推荐先跑；GR4J run 因 T07 r<0.5 退回三角线，可选）；新 run 目录禁止覆盖，执行前 guard_no_overwrite + run_manifest 已留痕
- 官方水位核验与管网耦合（Phase J/K，ADR-0023~0025）：包 `src/hydro_refine/{core,monitors,pipenet,coupling}/`；脚本 `scripts/stage3/{ingest_official_gauges,fetch_five_province_hydro,extract_osm_urban_inputs,build_synthetic_pipenet,rebuild_pipenet_catalog,run_pipenet_coupled_lisflood}.py`、`scripts/stage6/{validate_water_levels,compare_pipenet_inundation,evaluate_coupled_reruns,verify_five_province_algorithms}.py`；配置 `configs/gauges/`、`configs/pipenet/`；耦合重跑算例固定落在 `outputs/pipenet/<case>/run_{baseline,coupled}_*/`（在 `CASE_ROOTS` 白名单外），不得并入 `outputs/<case>/`
- git：主工程代码/文档 + floodviz 源码/测试/文档/QA 证据 + plans + 根级交付报告已入库；大文件与再生产物按 `.gitignore` 排除（data ~10.9 GB、outputs 运行产物、logs、models、.env、web/.dev/mock_bundle 不进 git）。
