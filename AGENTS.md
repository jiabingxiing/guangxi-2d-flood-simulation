# AGENTS.md —— 工作区导航与约束

本工作区是「广西久隆/宁明/贵港二维水动力模拟」项目。当前结果为**公开数据降级模式情景级**（ADR-0004），不是业务预警系统，不得用于业务告警或资产级判据。

## 目录与职责

- `2d_hydro_project/` —— 模型工程（LISFLOOD-FP 8.1.0，uv 管理）。入口见其 `README.md`。
- `2d_hydro_project/flood_visualization/` —— 发布子工程，只读父项目。**进入前先读其 `AGENTS.md`**。
- `plans/` —— 计划文档。基线是 `plans/广西久隆_宁明_贵港_二维水动力模拟技术路线与工程落地方案_终稿.md`；久隆治理基线是 `plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md`；`current-alternative-plans/` 是未采纳的备选版本；`visualization_research/` 存放核验证据（json/py/pdf）。
- 根目录 md/docx —— 交付报告（成果报告、答辩手册）。答辩手册由 `build_complete_v2.py` / `generate_handbook_v2.py` / `update_handbook_latest.py` 生成；手工改报告前先确认对应生成器，防止下次生成时被覆盖。
- `2d_hydro_project/diagnostics/jiulong/` —— 久隆诊断派生物（Phase 0）：基线快照、斑块持续性 CSV、DEM 异常分类栅格、根因报告。只读证据，不是运行产物。
- `2d_hydro_project/outputs/display_qc/` —— 展示质控派生层（Phase 1）。**不是物理重算**，带 `not_physical_recalculation=true`；不得用于物理结论或资产判据。

## 硬约束

- 已有 `res.mass` 的 run 目录**禁止覆盖**（终稿 §10）。写入前用 `scripts/common/grid_io.guard_no_overwrite` 检查。
- 运行 Python 前设 `PYTHONUTF8=1`（Windows 中文控制台编码）。
- 环境统一用 `uv`，走阿里云镜像，勿配代理。
- `2d_hydro_project/models/` 是上游 LISFLOOD-FP 源码：禁止修改，禁止重跑 `lisflood.exe`（除非用户单独批准重跑）。
- floodviz 只读父项目 `outputs/**` 与 `data/**`；发布包内禁止绝对路径、用户名、盘符、token。
- 三地不是同一场洪水：久隆/贵港 2024-09 摩羯，宁明 2026-08 紫檀；不共用绝对时间轴。
- 质量门 `PASS` ≠ 预测正确；`FAIL-EXPLAINED` 不计通过；未验证指标必须写「未测」（ADR-0006 / ADR-0010 决策项 5）。
- 久隆治理口径以 `docs/adr/ADR-0010` 为准：200 m 生产基线、100 m 仅敏感性；展示层只允许 `display_qc` 命名且必须带派生标记；禁止写死 4/9 像元或 100 m 单元面积。
- 派生层目录**不得含 `res.mass`**：会被 `g15_mass_recheck.py` 误计为独立 run。

## 验证入口

```bash
$env:PYTHONUTF8 = "1"
uv --directory 2d_hydro_project run python -m pytest        # 152 passed（基线）
uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py   # 质量门全表，权威值 43 PASS / 3 FAIL-EXPLAINED / 0 FAIL（46 run）
uv --directory 2d_hydro_project/flood_visualization run python -m pytest -m "not slow and not e2e"
uv --directory 2d_hydro_project/flood_visualization run python -m floodviz inventory --config configs/default.yaml
```

**注意**：`uv --directory 2d_hydro_project run pytest` 会报
`uv trampoline failed to canonicalize script path`；必须用 `run python -m pytest`。

## 状态与文档指针

- 执行状态 SSOT：`2d_hydro_project/docs/execution_status.md`
- 质量门 SSOT：`2d_hydro_project/data/catalog/mass_recheck_g15.json`（任何文档的计数与此冲突时以该 JSON 为准）
- docs 索引：`2d_hydro_project/docs/README.md`
- 决策记录：`2d_hydro_project/docs/adr/`（ADR-0001~0010；0010 = 久隆直接降雨模型治理规范）
- 久隆治理脚本：`scripts/common/{grid_io,patch_analysis}.py`（共用底座）、`scripts/stage6/{diagnose_jiulong_artifacts,analyze_patch_persistence,clean_jiulong_display}.py`、`scripts/stage3/{prepare_jiulong_dem_layers,run_rainfall_loss_sensitivity,audit_jiulong_drainage,jiulong_100m_rerun}.py`
- git：主工程代码/文档 + floodviz 源码/测试/文档/QA 证据 + plans + 根级交付报告已入库；大文件与再生产物按 `.gitignore` 排除（data ~10.9 GB、outputs 运行产物、logs、models、.env、web/.dev/mock_bundle 不进 git）。
