# 久隆 100 m 完整物理重跑（Phase E4）

> 授权：用户 2026-10-31 明确批准「100 m 完整物理重跑」，并选择「两个时间窗都跑」+「同时试边界修正」。
> 模式：ADR-0004 公开数据降级、情景级、非业务预警。
> 复算脚本：`scripts/stage3/jiulong_100m_rerun.py`（调度）、`scripts/stage3/jiulong_scenarios.py`（单 run）。
> 结论等级：E1 = 仓库实测/可复现；E2 = 强证据；E3 = 候选假设；E4 = 未测。

## 1. 四个 run 的配置矩阵

全部 100 m 网格（365×304，cellsize 100 m，单元面积 10 000 m²）、`dt=2.5`、`saveint=21600`、`fpfric=0.035`、
`acceleration` 求解器、ERA5 逐时均匀雨、S1 常水约束的 DEM 修复（海面钳 0 + 其余负值 BFS 填充）。

| run | 时间窗 | `sim_time` | 边界 |
|---|---|---|---|
| `scen_free_100m_dt2p5_sv6h_r5d_allfree` | 09-05T00→09-10T00 | 432 000 s | N/W/E/S 四边 FREE |
| `scen_free_100m_dt2p5_sv6h_bdsouthonly_r5d_southonly` | 同上 | 432 000 s | N/W/E 闭合，只留南缘 FREE 0.0005 |
| `scen_free_100m_dt2p5_sv6h_r13d_allfree` | 09-05T00→09-18T00 | 1 123 200 s | 四边 FREE |
| `scen_free_100m_dt2p5_sv6h_bdsouthonly_r13d_southonly` | 同上 | 1 123 200 s | 三边闭合 |

### 边界闭合的实现依据（E1，源码核验）

省略某一边的 `bci` 行 ⇒ 该边 `BC_Ident` 保持 `NONE0` ⇒ 法向通量被置 0（闭合）。
证据：`models/lisflood-fp-8.1/LISFLOOD-FP-trunk/boundary.cpp:37-43`
（`Arrptr->Qy[i] = 0` 置零北/南边界；`if (BCptr->BC_Ident[BCi] != NONE0 ...)` 才处理该边）、
`lisflood.h:178`（`NONE0 = 0`）。

实测 `bci.bci` 内容：
- `allfree`：`N/W/E/S` 四行，前三行 `FREE`，末行 `FREE 0.0005`
- `southonly`：只有 `S -99999999 99999999 FREE 0.0005` 一行

---

## 2. 5 天窗结果（已完成）

| run | `E_mass` | 质量门 | `res.max` 湿面积 | `res.max` 水量 | 末帧出流 | 累计出流 |
|---|---|---|---|---|---|---|
| `r5d_allfree` | 3.089e-4 | **PASS** | **271.07 km²** | 168.43 Mm³ | 16.039 m³/s | 11.106 Mm³ |
| `bdsouthonly_r5d_southonly` | 7.877e-6 | **PASS** | **274.66 km²** | 172.32 Mm³ | 14.034 m³/s | 7.146 Mm³ |

两 run 总雨量相同：175.87 Mm³（`Rain-(Inf+Evap)` 列，par 未设入渗）。

### 2.1 可复现性：`allfree` 与既有 100 m run 逐位一致（E1）

`scen_free_100m_dt2p5_sv6h_r5d_allfree` 与既有 `scen_free_100m_dt2p5_sv6h_realdem` 对比：

| 对比项 | 结果 |
|---|---|
| `res.max` 数组 | **逐位相同**（`np.array_equal` = True，最大绝对差 0.0） |
| 21 个 `res-*.wd` 帧 | **21/21 逐位相同** |
| `res.mass` 全部数值列（121 行 × 12 列） | **完全一致**（逐列最大差 0.0，末帧 `Vol` 均为 164 711 509.6 m³） |
| `res.mass` 文件 sha256 | 不同（`6fc7b11c…` vs `678f74ea…`） |

**哈希差异的唯一原因**（已逐字节定位）：`Qerror`/`Verror` 列的浮点符号位，
`0.0000` 与 `-0.0000`（首个差异字节 5467）。物理量等价，不是数值差异。

**结论**：新增的 `--boundary-mode allfree` 路径**未改变默认行为**，既有结果可复现（E1）。

### 2.2 边界闭合的影响方向与预期相反（E2）

| 指标 | allfree | southonly | 变化 |
|---|---|---|---|
| `res.max` 湿面积 | 271.07 km² | 274.66 km² | **+3.59 km²（+1.32%）** |
| `res.max` 水量 | 168.43 Mm³ | 172.32 Mm³ | **+3.89 Mm³（+2.31%）** |
| 累计出流 | 11.106 Mm³ | 7.146 Mm³ | **−3.96 Mm³（−35.6%）** |
| 末帧蓄量 | 164.71 Mm³ | 168.72 Mm³ | **+4.01 Mm³（+2.43%）** |

**账目闭合**：出流减少 3.96 Mm³，蓄量增加 4.01 Mm³，差额 0.05 Mm³ 与残差同量级。

**湿区增量的空间分布**（`gain = southonly 湿 & ¬allfree 湿`，共 360 格 = 3.60 km²）：

| 位置 | 增量格数 |
|---|---|
| N 边（row < 10） | **141** |
| W 边（col < 10） | **124** |
| E 边（col > 354） | **90** |
| S 边（row > 293） | 16 |
| 内部 | 12 |

**增量集中在被闭合的三边**（N+W+E = 355 格，占 98.6%）。

**解读（E2 强证据，非 E1 结论）**：
原设置的 N/W/E 三边 FREE **确实在排水**。闭合后这部分水留在域内，所以湿面积与蓄量增加。
这印证了 Phase 4 审计提出的「三边 FREE 短路」判断在**方向和量级上成立**。

**但必须同时标注三条限制**：
1. **这不证明闭合是正确的物理设置。** 真实受纳水体（钦江/海湾）与域边界的连通关系未核证（D4 阻塞），
   闭合只是敏感性对照，不是率定结果。
2. **增量方向与「改善观感」相反。** 闭合后湿面积更大、退水更慢，若目标是减少淹没范围，闭合不是解。
3. **这只覆盖 5 天窗。** 13 天窗的退水段影响需单独看（进行中）。

---

## 3. 13 天窗结果

**状态：进行中。** 本节将在两个 13 天 run 完成后补齐。
截至本文档生成时，`r13d_allfree` 已推进到 7/53 帧。

---

## 4. 未做与限制

| 项 | 状态 | 原因 |
|---|---|---|
| 12.5 m 原生分辨率重跑 | **未做** | 用户只批准 100 m；12.5 m 需另行批准 |
| 边界闭合的物理结论 | **未做（E4 未测）** | 缺受纳水体连通资料（D4 阻塞）；闭合仅作敏感性对照 |
| 排水网络 / 断面刻蚀 / 结构表达 | **未做** | D1–D5 全部阻塞，用户明确资料无法提供 |
| 独立验证（CSI/NSE/R²） | **E4 未测** | 久隆无洪峰过境观测；用户已接受 `WEAK/未测` |
| 100 m 升级为生产基线 | **未做** | ADR-0010 决策项 2 仍有效：需重跑通过质量门与分辨率收敛后才可升级 |

## 5. 复现命令

```powershell
$env:PYTHONUTF8 = "1"
# 只看计划，不写任何文件
uv --directory 2d_hydro_project run python scripts/stage3/jiulong_100m_rerun.py --dry-run
# 实际执行（4 个 run；已有 res.mass 的目录自动跳过）
uv --directory 2d_hydro_project run python scripts/stage3/jiulong_100m_rerun.py --run-tag e100m-v1
```

**只读声明**：4 个 run 全部写入新建目录，未覆盖任何既有 `res.mass` 目录；
未修改 `models/**`；未修改 `scripts/common/**`。
