---
doc_type: decision-record
status: active
topic: adr-0024
canonical: true
scope: project
---

# ADR-0024：PipeNetGen 虚拟地下雨水管网合成与 1D-2D 协同排涝耦合

- **状态**：已接受（Accepted）。
- **日期**：2026-11-25
- **触发**：高密度城镇区（贵港城区、宁明县城、久隆集镇）缺乏市政地下管网实测资料，纯地表模型在城区低洼微地形形成大量滞留斑块伪影；前期 ADR-0014 仅依赖 DEM 推导明渠与全局标量入渗，缺乏城市管网重力流汇水与定向排河机理。
- **关联**：ADR-0008（不修改 `models/` 求解器源码）、ADR-0011/0014（贵港伪影与排水治理解耦）、ADR-0020（SGC 河道耦合）。

## 决策

1. **开源借鉴与合规许可组合**：
   - 借鉴 `SWMManywhere`（BSD-3-Clause）与 `pysewer` 的「OSM 道路骨架 + DEM 重力有向无环图（DAG）」思想，依托 `networkx`（BSD-3-Clause）、`swmmio`（MIT）与 `pyswmm`（BSD-2-Clause）研发本土化 PipeNetGen 模块 `hydro_refine.pipenet`；避免引入 GPLv3 传递依赖。
2. **物理合规生成规则（GB 50014）**：
   - 过滤桥涵/隧道/步行道，按道路等级等距采样雨水井并空间哈希合并交叉口；
   - 采用多源逆向 Dijkstra 最小成本汇水树将全部节点连通至受纳水体排放口，并以唯一 `1-to-1` 终端 `OUT_*` 节点满足 EPA-SWMM5 出口约束；
   - 沿拓扑序单调下推管底标高（最小坡度 $0.002$，数学保证 0 条逆坡管段），采用精确圆管满流 Manning 常数 $C=4^{5/3}/\pi \approx 3.20836$ 反算国标 DN300~DN2400 管径并支持多联干管（barrels）。
3. **1D-2D 双轨耦合设计（不改 `models/` 源码）**：
   - **生产推荐（方案 B）**：利用 LISFLOOD-FP 8.1.0 原生支持的点汇/点源接口 `P x y QVAR <name>`
     （`iterateq.cpp::UpdateH` 的 QVAR 分支；SGC 路径为 `lisflood2/sgm_fast.cpp::SGC2_PointSources_Vol_row`），
     构建「城区雨水井负汇吸纳（限幅防止负水深失真）+ 受纳河道正源回注」水力闭合边界，
     确保全局水量 100% 守恒（$E_{\text{mass}} \le 10^{-3}$）；
   - **科研对比（方案 A）**：利用 `pyswmm` 运行 EPA-SWMM5 动态波 1D 仿真提取排河流量过程线与溢流节点，交叉校核等效排水容量场。
   - **实现分层（2026-11-25 补记，ADR-0025）**：`drainage_kernel.py` 提供两条构建路径——
     `build_lisflood_drainage_bdy_and_bci` 为粗聚合诊断版（簇内雨水压到单格抽取，
     会触发 `H<0` 钳零，**不可用于物理重跑**）；`build_fullgrid_coupled_forcing` 为全网格
     物理版（逐格点汇恒 ≤ `0.85 × 该格瞬时净雨`，数学保证零负水深）。物理重跑必须用后者。
4. **虚拟资产标识与隔离**：
   - 全部输出 `.inp`、`GeoJSON` 与统计清单强制携带 `SYNTHETIC_DATA_NOT_REAL_ASSET=true`；实验性耦合算例与合成基准放置于 `outputs/pipenet/` 与 `outputs/synthetic/`，严禁污染 `outputs/{guigang,jiulong,ningming}/` 与质量门 66-4-0 SSOT 计数。

## 后果

- 实现了城市地表-地下排水通道从"无资料空白/启发式开沟"向"有拓扑、有水力定径、可运行 SWMM 与 LISFLOOD-FP 闭合耦合"的技术跃迁。
- 在虚拟合成城区基准（LISFLOOD-FP 8.1.0 实跑）与贵港实际路网+DEM 合成管网评估中，显著降低了低洼滞水体积与孤立积水斑块数量。
- **2026-11-25 升级为真实物理重跑**：三案例（贵港/久隆/宁明）在隔离目录
  `outputs/pipenet/<case>/run_{baseline,coupled}_*/` 下真正调用 `lisflood.exe` 重跑，
  基线由同批自产算例承担（见 ADR-0025）。全部结果带 `physical_recalculation=true`，
  与此前 `not_physical_recalculation=true` 的诊断投影明确区分。
- **已知限制（SGC 路径）**：SGC 模式下任何非零 `QVAR` 都会引入与流量同量级的体积残差
  （单变量实测：负源 +1103.9 m³ / 正源 +126.1 m³ / 成对 +1100 m³，QVAR 恒零时为 0.0）。
  久隆生产算例 E_mass=7.7e-6 通过，是因为其管网点源通量相对全域降雨/来水极小；
  若提高久隆抽取强度，残差会按此比例放大。详见 ADR-0025 事实 3。
