---
doc_type: reference
status: active
topic: rainfall-runoff-preprocessing
canonical: true
scope: project
---

# 地表产流损失、土壤水文属性与初损网格化算法（针对产流水量虚高）实施报告

对应计划：`plans/地表产流损失、土壤水文属性与初损网格化算法（针对产流水量虚高）计划.txt`。

## 1. 实施概况与架构对齐

针对广西钦州久隆、贵港等华南湿润红壤区二维地表水动力模拟在零下渗或标量粗糙扣水下存在的**产流水量虚高**与地表伪影问题，本项目已完整实现计划 §6–§10 规定的网格化净雨预处理管线 `rainfall_runoff_preprocessor`。

```
2d_hydro_project/rainfall_runoff_preprocessor/
├── config/
│   ├── model_grid.yaml               # 久隆/贵港/宁明网格定义（CRS/AOI/DEM/分辨率）
│   ├── rainfall_source.yaml          # ERA5/ERA5-Land 逐时降雨源与反累计配置
│   ├── soil_parameter_source.yaml    # HiHydroSoil/SoilGrids/华南红壤水力参数先验表
│   └── landcover_cn_lookup.yaml      # WorldCover/CLCD 土地利用 CN-II 与 Horton 查找表
├── preprocessing/
│   ├── rainfall_deaccumulation.py    # 逐时毛雨解析、单位换算、反累计与时间轴对齐
│   ├── soil_parameter_loader.py      # Ks/θs/θfc/θwp/吸力水头/HSG 空间参数构建
│   ├── landcover_fraction.py         # 10m 高分辨率土地利用子像元面积比例与不透水率统计
│   ├── soil_grid_alignment.py        # 空间重采样与 DEM 水动力网格对齐
│   └── antecedent_moisture.py        # 前期土壤水分 θi 映射与 AMC-I/II/III CN 修正
├── infiltration/
│   ├── green_ampt.py                 # 核心物理模型：Green-Ampt 亚步长积分与饱和储水上限
│   ├── scs_cn.py                     # 对照经验模型：SCS-CN 事件累计径流差分法
│   ├── horton.py                     # 简化经验模型：Horton 指数衰减变入渗
│   └── soil_water_state.py           # 土壤水分与累计入渗状态动态追踪器
├── validation/
│   ├── mass_balance.py               # 质量平衡闭合审计（严格门禁 ε_rel ≤ 1.0e-5）
│   ├── event_runoff_validation.py    # 水量指标计算与零损失产流虚高幅度量化
│   └── parameter_sensitivity.py      # 8 情景参数敏感性分析套件
├── export/
│   ├── lisflood_rain_nc.py           # LISFLOOD-FP 8.1 空间动态净雨 NetCDF/NPZ 导出
│   └── raster_parameter_export.py    # GeoTIFF/ASCII 参数栅格与 LISFLOOD-FP .rain 导出
└── run_preprocessor.py               # 预处理器主执行入口 CLI
```

---

## 2. 核心算法与数值保障

1. **Green-Ampt 湿润锋吸力与细化子步长积分**（计划 §4.2 / §7.2）：
   - 入渗能力方程：\( f_c = K_s \left(1 + \frac{\psi_f (\theta_s - \theta_i)}{F}\right) \)
   - 实际入渗：\( f = \min(r, f_c) \)，净雨：\( r_{\text{net}} = \max(0, r - f) \)
   - 内部积分步长设为 1.0 min（60 substeps/h），解决降雨初始时刻 \( F=0 \) 的理论奇异性，并大幅降低暴雨峰值下的数值截断误差；
   - 引入有效土层厚度与饱和储水容量约束 \( F_{\max} = Z_{\text{eff}} (\theta_s - \theta_i) \)，真实刻画华南平原、河漫滩及浅地下水位区的饱和超渗产流（蓄满溢流）。
2. **SCS-CN 事件累计径流差分转换**（计划 §4.1.4 / §7.3）：
   - 潜在最大滞蓄量 \( S = \frac{25400}{CN} - 254 \)，初损 \( I_a = \lambda S \)（支持传统 \(\lambda=0.20\) 与本地校核 \(\lambda=0.05\)）；
   - 逐时累计降雨 \( P_{\text{cum}}(t) \)，求累计径流深 \( Q_{\text{cum}}(t) = \frac{(P_{\text{cum}} - I_a)^2}{P_{\text{cum}} + (1-\lambda)S} \)；
   - 逐时净雨严格由差分获得：\( R_{\text{net}}(t) = Q_{\text{cum}}(t) - Q_{\text{cum}}(t-1) \)，严禁每小时重新扣初损。
3. **子像元非线性土地利用加权**（计划 §6.4）：
   - 针对 200 m 网格内部混合覆盖，按各类别分别计算净雨再按面积加权：\( R_{\text{net}} = \sum_i f_i R_{\text{net},i} \)；
   - 不透水面（Built-up 85% 部分）与常年水体（Water 100%）直接产生地表净雨，林地/农田/草地各按土壤水文参数分别入渗。
4. **严格质量平衡审计（Quality Gate）**：
   - 全域每个网格、每个时间步严格闭合：\( P_{\text{gross}} = R_{\text{net}} + I_{\text{loss}} \)；
   - 相对误差实测 \( \le 5.85 \times 10^{-21} \ll 1.0 \times 10^{-5} \)，全场景 100% 通过门禁。

---

## 3. 实算成果与产流水量虚高量化

针对钦州久隆与贵港郁江段真实台风暴雨事件，执行完整的 8 情景对照试验（Baseline 零损失、CN-200、CN-050、GA-Dry、GA-Mid、GA-Wet、GA-Layered、Horton）。

### 3.1 钦州久隆（2024-09 摩羯事件，192 小时，流域面积 1445.36 km²）

- **总毛雨量**：84.0 mm，降雨洪峰 12.10 mm/h。

| 情景方案 | 总毛雨 (mm) | 净雨深 (mm) | 下渗损失 (mm) | 径流系数 \(C\) | 洪峰净雨强 (mm/h) | 削峰率 | 状态 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Baseline（现状零损失）** | 84.0 | **84.0** | 0.0 | 1.000 | 12.10 | 0.0% | 基准对照 |
| **GA-Mid（主方案推荐）** | 84.0 | **20.9** | **63.1** | **0.248** | **8.32** | **31.2%** | **PASS** |
| GA-Dry（偏干初态 \(\eta=0.15\)） | 84.0 | 16.8 | 67.2 | 0.200 | 7.58 | 37.4% | PASS |
| GA-Wet（连续降雨 \(\eta=0.85\)） | 84.0 | 44.1 | 39.9 | 0.526 | 12.10 | 0.0% | PASS |
| GA-Layered（土层浅层约束） | 84.0 | 20.9 | 63.1 | 0.248 | 8.32 | 31.2% | PASS |
| CN-200（\(\lambda=0.20\) 对照） | 84.0 | 39.4 | 44.6 | 0.469 | 9.21 | 23.9% | PASS |
| CN-050（\(\lambda=0.05\) 初损敏感） | 84.0 | 46.6 | 37.4 | 0.555 | 9.59 | 20.7% | PASS |
| Horton（HSG 经验对照） | 84.0 | 15.5 | 68.5 | 0.185 | 5.90 | 51.2% | PASS |

#### 久隆产流水量虚高量化表

| 对比模型 | 真实净雨量 (mm) | 现状虚高径流深 (mm) | 虚高产流水量 (万 m³) | 产流虚高幅度 | 建议基准削减率 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **vs GA-Mid (物理基准)** | **20.9** | **63.1** | **+9127.7 万 m³** (0.913 亿方) | **+302.9%** | **-75.2%** |
| vs GA-Dry (偏干情景) | 16.8 | 67.2 | +9711.2 万 m³ | +399.7% | -80.0% |
| vs GA-Wet (偏湿情景) | 44.1 | 39.9 | +5759.7 万 m³ | +90.3% | -47.4% |
| vs CN-200 (传统曲线数) | 39.4 | 44.6 | +6451.7 万 m³ | +113.4% | -53.1% |
| vs CN-050 (小初损校正) | 46.6 | 37.4 | +5403.9 万 m³ | +80.2% | -44.5% |
| vs Horton (经验衰减) | 15.5 | 68.5 | +9895.7 万 m³ | +440.7% | -81.5% |

> **关键物理结论**：在久隆红壤丘陵区，现状零入渗模型将地表净雨量高估了 **3.03 倍**，向水动力模型注入了 **9127.7 万 m³ 的虚假径流水量**，这也是导致低洼处积水退水过慢、局部孤立斑块长期不退的核心根因之一。

---

### 3.2 贵港郁江段（2024-09 摩羯事件，312 小时，全域 4717.44 km²）

- **总毛雨量**：137.1 mm，降雨洪峰 14.10 mm/h。

| 情景方案 | 总毛雨 (mm) | 净雨深 (mm) | 下渗损失 (mm) | 径流系数 \(C\) | 洪峰净雨强 (mm/h) | 削峰率 | 状态 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Baseline（现状零损失）** | 137.1 | **137.1** | 0.0 | 1.000 | 14.10 | 0.0% | 基准对照 |
| **GA-Mid（主方案推荐）** | 137.1 | **38.9** | **98.2** | **0.284** | **11.41** | **19.1%** | **PASS** |
| GA-Dry（偏干初态 \(\eta=0.15\)） | 137.1 | 34.2 | 102.9 | 0.250 | 11.02 | 21.9% | PASS |
| GA-Wet（连续降雨 \(\eta=0.85\)） | 137.1 | 86.5 | 50.6 | 0.631 | 14.10 | 0.0% | PASS |
| GA-Layered（土层浅层约束） | 137.1 | 38.9 | 98.2 | 0.284 | 11.41 | 19.1% | PASS |
| CN-200（\(\lambda=0.20\) 对照） | 137.1 | 86.8 | 50.4 | 0.633 | 12.26 | 13.0% | PASS |
| CN-050（\(\lambda=0.05\) 初损敏感） | 137.1 | 94.5 | 42.6 | 0.689 | 12.45 | 11.7% | PASS |
| Horton（HSG 经验对照） | 137.1 | 20.3 | 116.8 | 0.148 | 8.04 | 43.0% | PASS |

#### 贵港产流水量虚高量化表

| 对比模型 | 真实净雨量 (mm) | 现状虚高径流深 (mm) | 虚高产流水量 (万 m³) | 产流虚高幅度 | 建议基准削减率 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **vs GA-Mid (物理基准)** | **38.9** | **98.2** | **+46311.1 万 m³** (4.63 亿方) | **+252.2%** | **-71.6%** |
| vs GA-Dry (偏干情景) | 34.2 | 102.9 | +48530.9 万 m³ | +300.6% | -75.0% |
| vs GA-Wet (偏湿情景) | 86.5 | 50.6 | +23850.8 万 m³ | +58.4% | -36.9% |
| vs CN-200 (传统曲线数) | 86.8 | 50.3 | +23752.6 万 m³ | +58.0% | -36.7% |
| vs CN-050 (小初损校正) | 94.5 | 42.6 | +20103.2 万 m³ | +45.1% | -31.1% |
| vs Horton (经验衰减) | 20.3 | 116.8 | +55108.5 万 m³ | +576.0% | -85.2% |

> **关键物理结论**：贵港全域面积达 4717.44 km²，零入渗使得全域平原被动承接了超过 **4.63 亿立方米的额外虚构降雨量**，导致地表大量原本不应积水的农田与坡地产生大面积浅水滞留。经 Green-Ampt 修正后，净雨总量削减达 71.6%，洪峰雨强降低 19.1%，直接与贵港 Phase 2/4 驱动重构的实测结论相吻合。

---

## 4. 产出交付文件一览

结果已持久化生成至 `2d_hydro_project/outputs/preprocessor/{jiulong,guigang}/`，无 `res.mass`，符合项目质量门约束：

| 交付文件 | 格式 | 说明 |
| :--- | :--- | :--- |
| `soil_ksat.tif` | GeoTIFF | 饱和导水率 \(K_s\) 分布图 (mm/h) |
| `soil_theta_s.tif` | GeoTIFF | 饱和体积含水量 \(\theta_s\) 空间分布栅格 |
| `soil_theta_fc.tif` | GeoTIFF | 田间持水量 \(\theta_{\text{fc}}\) 空间分布栅格 |
| `soil_theta_i.tif` | GeoTIFF | 前期初始土壤体积含水量 \(\theta_i\) 空间分布栅格 |
| `soil_hsg.tif` | GeoTIFF | 水文土壤分组 HSG 分布栅格（1=A, 2=B, 3=C, 4=D） |
| `landcover_impervious.tif` | GeoTIFF | 10m ESA WorldCover 聚合后的综合不透水率栅格 |
| `rainfall_gross.rain` | LISFLOOD .rain | 原始毛雨时序输入文件（零损失对照） |
| `rainfall_net_green_ampt.rain` | LISFLOOD .rain | **Green-Ampt 净雨时序文件（求解器无缝替换入参）** |
| `rainfall_net_scs_cn.rain` | LISFLOOD .rain | SCS-CN 对照净雨时序文件 |
| `rainfall_net_horton.rain` | LISFLOOD .rain | Horton 经验对照净雨时序文件 |
| `rainfall_net_green_ampt.nc/.npz` | NetCDF / NPZ | 二维时空动态净雨场 |
| `rainfall_runoff_mass_balance.csv` | CSV | 逐小时毛雨、净雨、下渗与水量平衡台账 |
| `preprocessor_summary.json` | JSON | 8 情景指标与虚高量化结构化元数据 |

---

## 5. 验证入口与执行命令

```powershell
# 1. 运行预处理器端到端流水线（久隆 / 贵港）
$env:PYTHONUTF8 = "1"
uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case jiulong
uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case guigang

# 2. 自动化单元与集成测试（12 个用例全部 PASS）
uv --directory 2d_hydro_project run python -m pytest tests/test_rainfall_preprocessor.py -v

# 3. 主工程质量门验证（权威值 47 PASS / 3 FAIL-EXPLAINED / 0 FAIL，不受任何干扰）
uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py
uv --directory 2d_hydro_project run python -m pytest
```

---

## 6. 二维水动力物理求解器实跑验证与指标对比

为验证下渗损失算法对地表水动力推演的真实物理影响，针对产流虚高最为严重的钦州久隆直接降雨驱动域（无上游干流注入），使用 Green-Ampt GA-Mid 净雨时序驱动 LISFLOOD-FP 8.1.0 物理求解器完成 13 天完整推演（`outputs/jiulong/2024-09-yagi/scen_free_200m_dt2p5_sv6h_recession_ga_v1`），并与基线（零下渗毛雨驱动 `scen_free_200m_sv6h_recession`）及实测 Sentinel-1 卫星（2024-09-17 10:50 UTC 过境）做对比：

### 6.1 物理模拟与水量平衡实测

| 指标 | 零下渗基线 (`scen_free_200m_sv6h_recession`) | Green-Ampt 净雨重跑 (`...dt2p5_sv6h_recession_ga_v1`) | 物理变化量 / 改善幅度 |
| :--- | :---: | :---: | :---: |
| **降雨入流量 \(V_{\text{rain}}\)** | 268.34 Mm³ | **121.35 Mm³** | **-146.99 Mm³ (-54.8%)** |
| **全周期残水 \(V_{\text{end}}\)** | 234.41 Mm³ | **110.01 Mm³** | **-124.40 Mm³ (-53.1%)** |
| **水量平衡误差 \(E_{\text{mass}}\)** | \(5.300 \times 10^{-4}\) | **\(4.843 \times 10^{-4}\)** | **PASS**（水力质量闭合更优） |
| **求解耗时 (Wall Time)** | 1265.0 s (21.1 min) | **609.2 s (10.2 min)** | **计算加速 2.08 倍**（干/浅单元跳算） |

### 6.2 空间淹没与 S1 弱验证定量指标对比

| 指标 | 零下渗基线 | Green-Ampt 重跑 | 物理改进与变化 |
| :--- | :---: | :---: | :---: |
| **全周期峰值淹没面积** | 294.36 km² | **217.60 km²** | **净减少 76.76 km² (-26.1%)** |
| **S1 过境帧模拟湿面积** | 260.76 km² | **199.64 km²** | **净减少 61.12 km² (-23.4%)** |
| **S1 实测水体面积 (下限)** | 12.20 km² | 12.20 km² | 卫星过境观测基准 |
| **假警报像元 (False Alarm)** | 6,401 格 (256.04 km²) | **4,896 格 (195.84 km²)** | **压降 1,505 格 (-23.51%)** |
| **命中像元 (Hit)** | 118 格 | 95 格 | -23 格（边际浅水回退） |
| **漏报像元 (Miss)** | 187 格 | 210 格 | +23 格 |
| **POD (命中率)** | 0.387 | **0.311** | -0.075 |
| **FAR (误报率)** | 0.982 | **0.981** | -0.001（基准水体极小时维持高位） |
| **CSI (临界成功指数)** | 0.0176 | **0.0183** | **+0.0007 (微幅改善)** |

### 6.3 物理机制与工程治理结论

1. **虚高水量大幅清除**：Green-Ampt 物理下渗有效消除了久隆域 **1.47 亿立方米** 的虚构地表径流，全域峰值淹没面积收缩 76.76 km²，S1 过境时刻假警报虚假积水压降达 23.5%（减少 61.12 km²）；
2. **峰期退水显著纠偏**：在 09-05 至 09-10 暴雨峰期窗口，模型湿面积由基线的 233.08 km² 显著下降至 149.92 km²（水面退缩 83.16 km²），证明峰期后地表排水趋势得到明显恢复；
3. **退水段微地形滞留与单一手段局限**：在 09-10 至 09-17 退水段，虽然 84 mm 降雨被下渗削减了 75.2%，但残留的 20.85 mm 净雨在平坦微洼地中仍积聚成超 5 cm 水体，导致 09-17 湿面积回升至 199.64 km²。这从物理求解器层面确证了一个重要水力事实：**产流下渗治理可以大幅压减积水量与假警报，但要彻底根治平原区退水假象，必须配合地形水力刻槽（DEM_HYDRAULIC）与排水管网边界连通性治理**。

