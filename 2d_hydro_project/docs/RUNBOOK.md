---
doc_type: how-to
status: active
topic: runbook
canonical: true
scope: project
---

# RUNBOOK：二维水动力情景回算操作手册（降级模式，阶段 6 雏形）

更新：2026-11-24 ｜ 适用：`2d_hydro_project`（LISFLOOD-FP 8.1 MinGW 构建，公开数据降级模式 ADR-0004；模型/结构使用边界见 ADR-0008）

## 0. 前置条件

- 模型可执行：`models/lisflood-fp-8.1/LISFLOOD-FP-trunk/lisflood.exe`（w64devkit 构建，含 _WIN32 对齐补丁）。
- 地形：`D:\NWKYYSJZX\2026年工作\0731 五省区DEM\广西\广西.tif`（公开类数据；**沿海平原负值=垃圾值 G16**，宁明/贵港走廊实测零负值）。
- 降雨：Open-Meteo ERA5（≤8 天分段；缓存于 `data/catalog/probes/`）。
- 验证数据：宁明 GF-3 水体（`data/interim/ningming/water_*.tif`）+ 4 站月水位；贵港站公开洪峰；S1 六景（`E:\S1_gxhydro\`）。
- 凭证：Copernicus Dataspace 在 `2d_hydro_project/.env`（gitignored，勿入库）。

## 1. 标准命令（统一入口）

```bash
cd 2d_hydro_project
uv run python scripts/run_case.py ningming                                  # 宁明 2026-08 紫檀（默认基线 q4000/n0.035）
uv run python scripts/run_case.py ningming --qpeak 6000 --fric 0.10        # 事后参考组合（非验证）
uv run python scripts/run_case.py jiulong ztide                            # 久隆 2024-09 摩羯（天文潮情景）
uv run python scripts/run_case.py guigang --qpeak 11500                    # 贵港 2024-09 郁江（公开峰锚定）
```

单步操作（prep / run / verify）用各脚本子命令；敏感性批跑用 `ningming_ensemble.py` / `ningming_convergence.py`。
重跑必须用新目录：`uv run python scripts/run_case.py ... --run-tag <tag>`（注入 `RUN_TAG`）；已有 `res.mass` 的目录会被拒绝，禁止覆盖。

### 1.1 Phase B 敏感性工具（2026-10-26 新增）

```bash
# B0 空间 Manning（机制冒烟 + 均匀 vs 空间对比；主试验需先跑 --run）
uv run python scripts/stage3/manning_sensitivity.py            # 冒烟 + 对比已有 run
uv run python scripts/stage3/manning_sensitivity.py --run     # 追加跑空间 n 主试验（约 20 min）

# B2 结构连通性 B8（合成堤防四组 + T016c 双构建对照，约 3 min）
uv run python scripts/stage2/structure_benchmark.py

# B3 喀斯特标量入渗（账目冒烟 + 三档汇总；主试验需先跑 --run）
uv run python scripts/stage3/karst_loss.py                     # 冒烟 + 汇总已有 run
uv run python scripts/stage3/karst_loss.py --run              # 追加跑 1e-6/5e-6 两档（各约 20 min）

# B1 网格/时间步敏感性（统一口径指标；每档一次模型运行）
uv run python scripts/stage3/grid_sensitivity.py --case jiulong --res 100     # 久隆 100 m
uv run python scripts/stage3/grid_sensitivity.py --case jiulong --dt 2.5      # 久隆 dt 2.5
uv run python scripts/stage3/grid_sensitivity.py --case ningming --res 50    # 宁明 50 m
uv run python scripts/stage3/grid_sensitivity.py --case guigang --res 100    # 贵港 100 m
uv run python scripts/stage3/grid_sensitivity.py --report                   # 只重算汇总表
```

### 1.2 Phase C 可复现与退水验证工具（2026-10-27 新增）

```bash
# 13.4 可复现门：三案例基线 vs _repro 重跑比对（重跑用 run_case.py --run-tag repro）
uv run python scripts/stage3/repro_recheck.py

# B4 退水期 S1 弱验证（需先跑延长模拟）：
RUN_TAG=recession uv run python scripts/stage3/jiulong_scenarios.py prep free --saveint 21600 --sim-days 13
RUN_TAG=recession uv run python scripts/stage3/jiulong_scenarios.py run free --saveint 21600 --sim-days 13
uv run python scripts/stage3/jiulong_recession_s1.py

# 敏感性跑可指定目录批次后缀（默认 b1；30 m 长跑用 b5）：
uv run python scripts/stage3/grid_sensitivity.py --case guigang --res 30 --tag b5
# 贵港河槽宽已按米参数化（--channel-width，默认 600；200 m 下与旧 3 格口径逐位一致）
```

注意：`grid_sensitivity.py` 自动注入 `RUN_TAG`（目录后缀 `_<tag>`），不触碰首轮基线；被中断的目录须改名 `_interrupted` 隔离后才能重跑（`prep()` 拒绝覆盖）。

### 1.3 久隆双驱动与学术成果驱动工具（Phase H/I 新增）

```bash
# 久隆双驱动驱动链与实跑（ADR-0019/0020）：
uv run python scripts/stage3/build_jiulong_catchments.py         # A1 汇水区与面雨量
uv run python scripts/stage3/build_jiulong_inflow.py             # 小时级 Q(t) 与 A1/A2 分账
uv run python scripts/stage3/prepare_jiulong_channel.py          # SGC 亚网格河槽与拓扑核验
uv run python scripts/stage3/run_jiulong_dual_forcing.py --case r3 # R3 主情景实跑
uv run python scripts/stage6/audit_jiulong_coupling.py           # 耦合守恒与退水审计
uv run python scripts/stage6/diagnose_jiulong_patch_attribution.py # P01–P08 斑块成因归因

# 学术成果驱动产品生成（Phase H，ADR-0015~0018）：
uv run python scripts/stage6/make_fat_products.py                # 全量 66 PASS run FAT/历时 GeoTIFF
uv run python scripts/stage6/make_genesis_attribution.py         # 贵港成因空间归因图
uv run python scripts/stage6/ensemble_probability.py             # 宁明集合超越概率
uv run python scripts/stage6/copula_thresholds.py               # Copula 联合阈值与 Bootstrap CI
```

## 2. 质量门（每个 run 必查）

- **河槽模式（2026-10-28 起）**：宁明 `--channel {off,watermask,centerline}` 默认 `off`；非 off 模式在 prep 末尾过 `dem_channel_audit` 门（距常水>250 m >20% 或最大下切>30 m 即 FAIL）。既有 19 个 run 全判 FAIL，见 `data/catalog/dem_channel_audit.json` 与 ADR-0009。
- **E_mass（终稿 13.2）**= |Ve−V0−Vrain−(∫Qin−∫Qout)dt| / max(V0+Vrain+∫Qin,1) ≤ 1e-3；
  工具 `scripts/stage3/g15_mass_recheck.py`（自动扫描全部有效 run）。
- HFIX/HVAR 海（潮）边界情景超 1e-3 记 **FAIL-EXPLAINED**（G13 常湿激波质量增益），**不计入通过数**；不再使用 N/A-HFIX。
- 当前全表：**66 PASS、4 FAIL-EXPLAINED（久隆 z05/z10/ztide/r4）、0 FAIL**（70 个 run）。权威值以 `data/catalog/mass_recheck_g15.json` 为准。
- 运行秒退（exit −1）优先查 run.log：多为时刻序列非递增（雨/潮写入已加断言防护）。

## 3. 已固化的三个坑（勿回退）

1. **bci 边界区间 = 地图坐标（米）**，非单元序号；全边段用 `±99999999`（钳位=全覆盖），FREE 必须带显式坡度。
2. **时序元组序**：写入一律 `for t, v in pts`（列 = 值\t时刻）；任何 pts 变换后保持 (t, v)。
3. **点源标度律**：.bdy 文件值 = 实际 m³/s ÷ 网格边长；par 必须含 `bdyfile` 键。

## 4. 成果判读口径（必须随成果标注）

- 一切结论为**情景级**（上游过程为公开证据锚定的情景线；无授权数据）。
- 宁明：**时刻切片**（8/24 主帧 78h，主阈值 0.15 m）；q6000/n0.10 参考 CSI 0.357、POD 0.773、FAR 0.602；12 个独立配置/36 个独立阈值点，不是 108 点全因子。站址像元 0.018 m、±2 格 10.32 m；11.88 m 是约 100 m 邻域值。
- 久隆：站址像元 0.008 m、±2 格 0.547 m；1.684 m 在西南 1.28 km，不是站址。z05/z10/ztide 质量门 FAIL-EXPLAINED，不计通过。**到达 54.0 h（Phase B4：6h 输出重跑 `scen_free_200m_sv6h`，与基线逐位一致）**。
- 贵港：事件湿像元峰 25.06 m；09-12T14 21.92 m vs 43.09（apparent offset +21.17），09-14T05 20.00 vs 44.00（+24.00）；offset 非常数，**不得当作基准率定**。面积对 Q 不敏感（±30% 仅 +7%）。
- S1 水体提取为 G17 降级口径（无地形校正、固定 −17 dB、足迹/风浪漏提）；贵港 S1 仅足迹内弱下限参照（模拟 73.0 vs 观测 11.4 km²，CSI 0.043）。

## 5. 产物索引

| 内容 | 位置 |
|---|---|
| 三案例汇总 | `docs/stage_summary_3cases.md` |
| 终稿报告（Word） | `docs/final_report.md` / `final_report.docx` |
| 情景产品包（水深/首次到达/峰值时刻/历时） | `outputs/products/<site>/*.tif` + `products_summary.json`（`scripts/stage6/make_products.py`；到达=首个 `wd>0.05` 快照，历时=`res.totaltm`） |
| 冻结输入哈希清单 | `data/catalog/input_manifest.json`（`scripts/stage0/make_input_manifest.py`；输入变更后重算并评估重跑范围） |
| 可复现测试 | `data/catalog/reproducibility_test.json`（三案例冻结重跑逐位一致，`scripts/stage3/repro_recheck.py`） |
| 久隆退水期 S1 弱验证 | `data/catalog/jiulong_recession_s1.json` + `outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h_recession/` |
| 自动化测试 | `tests/`（42 项，`uv run pytest tests/ -q`；2026-10-26 实测） |
| 宁明敏感性/收敛 | `docs/stage3_ningming_ensemble.md` / `stage3_ningming_convergence.md` + `data/catalog/ningming_*.json` |
| Phase B0 空间糙率 | `docs/stage3_manning.md` + `data/catalog/manning_sensitivity.json` |
| Phase B2 结构连通性 | `docs/stage2_benchmarks.md` §6.1 + `outputs/bench/B8_levee/summary.json` |
| Phase B3 喀斯特入渗 | `docs/stage4_karst.md` + `data/catalog/karst_scenarios.json` |
| Phase B1 网格/时间步 | `docs/stage3_sensitivity.md` + `data/catalog/sensitivity.json` |
| 久隆情景（含 ztide） | `data/catalog/jiulong_scenarios_summary.json` |
| 贵港情景（3 Q 档） | `outputs/guigang/2024-09-yagi/*/verify.json` |
| S1 链 | `docs/s1_validation.md` + `data/processed/s1_water/` |
| 质量复核 | `data/catalog/mass_recheck_g15.json` |
| 风险登记 | `data/catalog/gaps_risks.md`（G1~G19） |
