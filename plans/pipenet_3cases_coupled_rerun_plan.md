# 三案例全域二维模型（贵港/久隆/宁明）隔离目录 1D-2D 管网耦合真实重跑实施计划

## Goal Description

在不覆盖任何既有 `res.mass` 历史运行目录、不修改 `models/` 上游求解器源码、且不污染质量门 SSOT（`66 PASS / 4 FAIL-EXPLAINED / 0 FAIL`）的前提下，于隔离目录 **`outputs/pipenet/<case>/run_coupled_*/`** 下，针对 **贵港、久隆、宁明** 三个代表性流域全域二维模型，**真正调用 `lisflood.exe`（LISFLOOD-FP 8.1.0）重跑 1D-2D PipeNetGen 管网排涝耦合情景**，并完成质量守恒、城区滞水斑块消退、Sentinel-1 遥感足迹与官方站点水位过程线四维物理对比。

### 已核实的基准算例与求解器物理约束

| 案例 | 参考基准目录 | 分辨率 / 网格 | 模拟时长 | 原实测耗时 | 城区管网有效格数 (`max_cells`) | 与 SGC 河道重叠 | 城区降雨总量 / 体积 | 建议隔离输出目录 |
|---|---|---|---|---:|---:|---:|---:|---|
| **贵港** | `outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14` | `100 m` / `370×669` | `1,209,600 s` (14 d) | `2,718 s` (~45 min) | `4,032` (`40.32 km²`) | `0`（无 SGC） | `135.50 mm` / `5.463 Mm³` | `outputs/pipenet/guigang/run_coupled_100m` |
| **久隆** | `outputs/jiulong/2024-09-yagi/scen_r3_dual_sgc_200m_sv6h` | `200 m` / `152×182` | `432,000 s` (5 d) | `58 s` (~1 min) | `395`（剔除 SGC 重叠 7 格后 **`388`**） | **`7` 格** | `50.70 mm` (GA净雨) / `0.801 Mm³` | `outputs/pipenet/jiulong/run_coupled_200m` |
| **宁明** | `outputs/ningming/2026-08-zitan/scen_q6000_n100_100m_choff_eclosed` | `100 m` / `224×244` | `388,800 s` (4.5 d) | `593 s` (~10 min) | `1,206` (`12.06 km²`) | `0`（无 SGC） | `281.87 mm` / `3.399 Mm³` | `outputs/pipenet/ningming/run_coupled_100m` |

> [!IMPORTANT]
> **源码级硬核发现（直接来自 `models/lisflood-fp-8.1/LISFLOOD-FP-trunk/`）**：
> 1. **点源上限（`input.cpp:1533`）**：`maxpi = 20000`（并可自动重分配）。三案例全量城区像元（最大贵港 `4,032` 格 + `53` 排口 = `4,085`）远低于 20,000 上限。
> 2. **单 `.bdy` 文件契约（`input.cpp:1828–2078`）**：LISFLOOD-FP 8.1.0 的 `.par` 只读取**单一 `bdyfile`**，必须将原有上游来水（`yujiang` / `qinjiang` / `mingjiang`）与新增管网点汇/排口 `QVAR` 过程线合并写入隔离目录的新 `.bdy` 文件。
> 3. **性能加速关键（`input.cpp:2050` & `boundary.cpp:1424`）**：`boundary.cpp` 内置了 `if (t == timeSeries->prev_time) return timeSeries->prev_value;` 缓存，且 `input.cpp` 支持多个 `.bci` `P x y QVAR <bin_name>` 共享同一条 `.bdy` 曲线；在**非 SGC 串行循环**（贵港、宁明，`iterateq.cpp:496`）中可将 `4,032` 格量化为 8–10 个排水能力分档曲线，避免每步重复插值数千次导致运行时间暴涨。
> 4. **久隆 SGC OpenMP 线程安全约束（`sgc.cpp:1044`）**：`SGC` 模式下 `ps_index` 循环带 `#pragma omp parallel for`，明确注释 *“should not be more than one point source per cell”*，且 `prev_time` 缓存在多线程共享同一 `TimeSeries` 时存在竞争风险。因此对 **久隆（仅 388 格）** 采取：（a）严格按 `(row, col)` 去重且**剔除全部 `sgc_width > 0` 河道像元与重叠排口**；（b）每个格分配独立命名的 `TimeSeries`（388 × 121 行，文件极小），彻底规避 OpenMP 竞态！
> 5. **严防负水深破坏质量守恒（`iterateq.cpp:541`、`sgc.cpp:1098`）**：当负点汇吸水量超过单元瞬时净雨与存水时，求解器将 $H < 0$ 钳零，直接破坏 `E_mass`。本计划默认采用 `safety_factor = 0.85` 降雨期实雨限幅捕获 + 等量受纳河道排口回注。

---

## User Review Required

> [!WARNING]
> **重大动作与耗时审批**：
> 1. 根据工作区 `AGENTS.md` 硬约束，重跑 `lisflood.exe` 须获您明确批准。三案例完整跑完预计墙钟合计 **约 56 分钟**（久隆 ~1 min、宁明 ~10 min、贵港 ~45 min；若加跑贵港 `r4_infil3` 叠加情景需再加 ~45 min）。
> 2. 批准后将严格把全部新 `run` 写入 `outputs/pipenet/<case>/run_coupled_*/`，原有 `outputs/{guigang,jiulong,ningming}/` 保持只读零改动。

---

## Open Questions

请在批准前确认以下两项关键口径决策（其余已设推荐默认值）：

1. **贵港参考基线选取**：
   - **选项 A（推荐）**：以 `scen_yagi_q11500_100m_dt2p5_chhyd_d14`（双驱动水力河槽、无全局人工入渗）作为主对照基线，干净分离「地下管网排涝效应」与「全流域土壤入渗效应」；
   - **选项 B**：同步加跑一组 `scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_infil3` + PipeNetGen（城区扣除 `3.0 mm/h` 土壤下渗后剩余净雨进管网），评估与现行 floodviz 默认主情景的叠加增量（总机时约 +45 分钟）。
2. **退水期滞水提取模式**：
   - **模式 1（默认推荐，`storm_capture_only`）**：仅在降雨时段 $R(t) > 0$ 按局部管网能力抽取 $\le 0.85 \cdot R(t)$ 径流排入下游河道，**单遍耦合推演即数学保证零负水深、$E_{\text{mass}} \le 10^{-3}$**。
   - **模式 2（可选对照，`storm_plus_recession`）**：利用既有一阶基线逐帧水深在已知深积水洼地（$H_{\text{base}}(t) > 0.25\text{ m}$）追加停雨后有界缓慢排空流量（上限受控于现有存水量，待实跑 `E_mass` 校验）。

---

## Proposed Changes

### 耦合内核扩展（全像元分档与拓扑排口映射）

#### [MODIFY] [drainage_kernel.py](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/src/hydro_refine/coupling/drainage_kernel.py)
- 扩展 `build_lisflood_drainage_bdy_and_bci(...)` 支持全量城区像元耦合边界构建（保留向后兼容默认签名）：
  1. **SGC / 排口冲突清洗**：支持传入可选 `exclude_mask_asc`（久隆 `sgc_width.asc > 0`）与 `(row, col)` 排口占位集合，保证同一个格网像元至多出现 1 个点源/点汇，且绝不把负汇落在河槽/排放口像元上。
  2. **拓扑对应排口分账（替换原硬编码前 4 个排口均分）**：利用 PipeNetGen `*_pipes.geojson` 中节点到所属 `outfall` 的连通映射（或空间最近排放口归属），计算每个下游排放口对应的实际汇水能力占比 $w_k$（$\sum_k w_k = 1$），并可选注入 PySWMM 1D 演算求得的单位脉冲滞后核（默认或直排严格满足 $\sum Q_{\text{sink}}(t) + \sum Q_{\text{outfall}}(t) = 0$）。
  3. **分模式 `.bdy` 生成器**：
     - 非 SGC 大规模网格（贵港 `4,032` 格、宁明 `1,206` 格）：按排水能力将格网离散分档为 $B \le 12$ 个能力档 `dr_bin_XX`（如每 10 mm/h 一档），`.bci` 全覆盖每个非冲突城区像元坐标并指向对应档位名称；
     - SGC 并行网格（久隆 `388` 格）：为每个城区像元写入唯一独立名称 `dr_jl_{r}_{c}`，避免 OpenMP 下共享 `TimeSeries->prev_time` 写冲突。

```python
# 关键水量闭合映射与 SGC 安全构建骨架（拟增函数）
def build_fullgrid_coupled_forcing(
    pipenet_geojson: Path,
    base_run_dir: Path,
    out_run_dir: Path,
    *,
    epsg: int,
    safety_factor: float = 0.85,
    n_capacity_bins: int = 10,
    unique_series_per_cell: bool = False,  # SGC 模式（久隆）强制为 True
    lag_hours: float = 0.0,                # 可选基于 PySWMM 峰值滞时的排口移位
) -> dict[str, Any]:
    ...
```

---

### 隔离重跑编排与质量/物理四维评估脚本

#### [NEW] [run_pipenet_coupled_lisflood.py](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/scripts/stage3/run_pipenet_coupled_lisflood.py)
- 负责在隔离目录 `outputs/pipenet/<case>/run_coupled_*/` 下安全组装并执行 `lisflood.exe`：
  1. **防覆盖门卫**：逐案调用 `guard_no_overwrite(out_run_dir)`；若目标已含 `results/res.mass` 立即阻断，要求切换 `--run-tag`；
  2. **零宿主绝对路径输入装配**：复制/硬链接或按相对目录生成 `dem.asc`、`manning.map`、`sgc_width.asc`、`sgc_bank.asc`（久隆），合并基准 `.bdy` 与管网 `.bdy` 为 `coupled.bdy`，写入含 `SYNTHETIC_DATA_NOT_REAL_ASSET=true` 与 `isolated_from_g15_ssot=true` 的 `meta.json` 和 `run_manifest.json`；
  3. **运行与守恒门控**：调用 `lisflood.exe -v run.par`（记录标准输出至 `run.log`），计算与 `g15_mass_recheck.py` 同口径的 `E_mass`，若超过 `1e-3` 即停机诊断（如微调 `safety_factor`）。
  4. 支持 `--dry-run`（仅生成耦合输入不启求解器，供自动化集成测试）与 `--case {guigang,jiulong,ningming,all}`。

#### [NEW] [evaluate_coupled_reruns.py](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/scripts/stage6/evaluate_coupled_reruns.py)
- 对三案例物理重跑结果 `run_coupled_*/results/res.max` 与逐帧 `res.XXXX` 进行全面对比（不依赖任何 `not_physical_recalculation` 假设）：
  1. **真实物理重算内涝对比**：对比耦合前后**城区内涝掩膜内**与**全域范围内**的湿面积（km²）、湿格数、最大/平均水深、总积水体积与 8 邻域孤立斑块数（重点核查久隆 13d 退水或 5d 末帧滞水斑块变化、宁明县城滞水削减、贵港城区内涝消退）；
  2. **河道受纳顶托检验**：检验将城区排涝水注入受纳河道排口后，主槽洪峰水位与河道淹没是否引发显著下游/河岸抬水；
  3. **既有核验流水线联动**：
     - 复跑 `validate_water_levels.py --case <c> --run-dir <coupled_dir>` 核查站址/河道 $\Delta H$ 与 $\Delta T_{\text{peak}}$ 变化；
     - 复跑贵港/久隆/宁明 Sentinel-1 足迹核验，统计耦合后 `POD / FAR / CSI` 变化；
  4. 产出权威评估清单 `data/catalog/pipenet_coupled_rerun_evaluation.json`（与此前零重跑诊断 `pipenet_coupling_evaluation.json` 明确对比）。

---

### 回归隔离保障与文档更新

#### [MODIFY] [test_coupling_pipeline.py](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/tests/integration/test_coupling_pipeline.py)
- 增加两条集成测试：
  1. `dry-run` 测试 `run_pipenet_coupled_lisflood.py` 对贵港（多格分档）与久隆（`unique_series_per_cell` + SGC 掩膜剔除 `overlap_sgc=7`）的输入合并与水量闭合 $\sum Q_{\text{sink}} + \sum Q_{\text{outfall}} = 0$ 断言；
  2. 带小型 SGC 网格的 `lisflood.exe` 冒烟测试，验证 `SGC + P x y QVAR` 并行安全且 $E_{\text{mass}} \le 10^{-3}$。

#### [MODIFY] [test_mass_recheck_g15.py](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/tests/regression/test_mass_recheck_g15.py)
- 确保新增 `outputs/pipenet/*/run_coupled_*/results/res.mass` 后，主项目 `g15_mass_recheck.py` 仍严格保持 **`66 PASS / 4 FAIL-EXPLAINED / 0 FAIL`**，零污染既有 SSOT。

#### [MODIFY] [ADR-0024-PipeNetGen虚拟排水管网与协同排涝耦合.md](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/docs/adr/ADR-0024-PipeNetGen虚拟排水管网与协同排涝耦合.md) / [execution_status.md](file:///e:/NWKYYSJZX/2026/1023%20久隆%20贵港%20宁明%20二维水动力模拟/2d_hydro_project/docs/execution_status.md)
- 待实跑完成后登记三案例物理耦合重跑结论与前后对比表（继续保持非真实管网资产声明）。

---

## Verification Plan

### Automated Tests
批准实施且代码与重跑完成后，执行单次全门禁链条验证：
```powershell
$env:PYTHONUTF8 = "1"
uv --directory 2d_hydro_project run python scripts/maintenance/preflight.py
uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py   # 断言保持 66 PASS / 4 FAIL-EXPLAINED / 0 FAIL
uv --directory 2d_hydro_project run python -m pytest -q                           # 全量通过（含新增 SGC+QVAR 与隔离测试）
uv --directory 2d_hydro_project/flood_visualization run python -m pytest -m "not slow and not e2e" -q
```

### Manual Verification
1. 检查 `data/catalog/pipenet_coupled_rerun_evaluation.json` 中三案例各自耦合前后的物理重算 `E_mass`、`urban_volume_reduction_pct`、`patch_reduction_pct`、S1 `CSI/FAR` 及站点 $\Delta H$ 对照变化；
2. 人工复核是否需要将验证通过的三案例耦合情景以 `role: experimental_synthetic_pipenet` 显式接入 `flood_visualization` 展示配置（本计划默认暂不自动修改 `floodviz` 主配置）。
