# 固定验证抽样索引（入库，只存索引不存数值）

计划 §4 要求本目录存放「固定验证抽样索引（入库）」。目的：**发布保真回归必须用同一批抽样点**，
否则每次构建换一批点，无法判断「产品变了」还是「抽样变了」。

## 文件

- `sampling_index.json`（约 711 KB，14 132 点）：由 `scripts/make_sampling_index.py` 生成。
  只含 `row`/`col` 与类别，**不含任何数值**；数值由测试运行时从源 `.wd` 与发布包数值块分别读取比较。

## 抽样口径（每 run 每帧，确定性随机；种子 = `sha256(seed|case|run|frame)`）

| 类别 | 每帧上限 | 规则 |
|---|---:|---|
| `random` | 24 | 有效像元（非 NaN、非海域）中随机 |
| `boundary` | 40 | 干湿边界：`depth > 0.05 m` 的湿像元且 4 邻域内存在非湿像元（取湿侧） |
| `nodata` | 10 | NaN 邻域里的有效像元（取有效侧），验证 NoData 边界不串值 |
| `max` | 1 | 该帧 `depth` 最大值像元 |

run 级 `max_envelope`：`res.max` 的最大值像元（≤5 个）+ 超过最高阈值（10 m）的像元（≤5 个）。
每帧另记录 `valid_cell_count` 与 `wet_cell_count`（标量，用于交叉核对）。

覆盖：久隆 21 帧（1 165 点）、久隆退水 53 帧（3 245）、宁明 6000 与 4000 各 19 帧（各 1 195）、
贵港 30 m 与 200 m 各 57 帧（各 3 666）。**可选 run 未入包时测试自动跳过**，不冒充已发布。

## 使用与门禁

生成（仅当源数据或抽样口径变化时重跑；重跑会改变回归基线，需在提交说明中注明）：

```powershell
uv run python scripts/make_sampling_index.py            # 默认 seed=20261027
```

校验（测试 `tests/integration/test_fixed_sampling.py`，标记 `slow`）：

```powershell
uv run python -m pytest tests/integration/test_fixed_sampling.py -q -m slow
```

门禁：

- 数值误差 ≤ `max(1e-5 m, 1e-6 × |源值|)`；NaN 位置必须一致；
- 抽样点总数必须 > 2 000（防止索引被意外裁剪后"静默通过"）；
- 包络层同样用固定点校验（`numeric/<run>/max`）。

最近一次实测（2026-10-27，`v2-20261027`）：**116 帧、7 221 固定点、最大绝对误差 0.000e+00 m**；
包络 19 点、误差 0.000e+00 m。

`validate --full` 是**构建期随机抽样**门禁（每次构建自检）；本索引是**跨版本可比**的回归门禁，
两者互补，不互相替代。
