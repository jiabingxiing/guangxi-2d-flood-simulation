# floodviz 数据契约

本文件是发布包（`outputs/web_bundle/<bundle_id>/`）的**人读版**契约。机器可读版本见
`schemas/*.json`，示例见 `docs/examples/*.example.json`。实现依据 `SPEC.md`。

约定：所有 URL 为**发布包内相对路径**（POSIX 风格 `/`）；唯一允许的外链是在线底图。

## 1. 目录

```text
index.html
assets/{styles.css,app.js,map.js,timeline.js,web_manifest.json}
assets/vendor/{maplibre,proj4}/...
catalog.json
cases/<case_id>/<run_id>/{timeline.json,statistics.json}
cases/<case_id>/{boundary.geojson,sites.geojson,sar_footprint.geojson,optical_footprint_<asset_id>.geojson}
frames/<case_id>/<run_id>/<frame_id>_r<row>c<col>.png
frames/<case_id>/<run_id>/max_r<row>c<col>.png
frames/<case_id>/<run_id>/images.json
frames/<case_id>/{dem.png,sar_<asset_id>.png,optical_<asset_id>.png}
numeric/<case_id>/<run_id>/manifest.json
numeric/<case_id>/<run_id>/mask.png
numeric/<case_id>/<run_id>/<frame_id>/<block_y>/<block_x>.bin
numeric/<case_id>/<run_id>/max/<block_y>/<block_x>.bin
licenses/{LICENSES.md,maplibre-LICENSE.txt,proj4-LICENSE.txt}
manifest.json
```

## 2. 坐标与像元约定

- 原生网格：模型 UTM（久隆/贵港 EPSG:32649，宁明 EPSG:32648），ESRI ASCII，行 0 = 最北行，pixel-is-area。
- `transform` 为 rasterio/Affine 顺序 `[a,b,c,d,e,f]`，北向上时 `d<0`，`c/f` = 左上角角点坐标。
- 像元中心：`x=a*(col+0.5)+b*(row+0.5)+c`，`y=d*(col+0.5)+e*(row+0.5)+f`。
- 点查：经 proj4 从 WGS84 转到模型 CRS → 逆仿射 → `row=floor, col=floor`；越界返回"域外"，**不夹到边缘像元**。
- 显示图：`coordinates` 顺序固定 **左上、右上、右下、左下**（WGS84，网格外角，pixel-is-area）。
- 空间门禁：像元中心 → WGS84 → 四角双线性反算的偏差必须 ≤ **0.5 源像元**；超过则增加子图（`split`）。

## 3. 数值块（保真层）

- 块大小 256（`block_size`），块内 C 序，`dtype="<f4"`（float32 小端），`nodata="nan"`。
- 块文件命名 `numeric/<case>/<run>/<frame_id>/<by>/<bx>.bin`；边缘块按实际行列裁剪。
- 取值：`index = (row - by*256) * block_width + (col - bx*256)`。
- `mask.png`：整幅 1:1 位图，**白=非 NaN**（可用于渲染）；海域像元**不**从数值块剔除，
  只从统计面积中剔除（海域源值已被模型钳 0，点查返回 0 而非 NaN）。

## 4. 时间

- `sim_seconds = source_sequence * save_interval_s`（本项目三地均 6 h 等间隔、无 restart）。
- `origin_status`：
  - `model_schedule_confirmed`（贵港）→ 每帧 `timestamp` 为 ISO8601 带 +08:00；
  - `relative_only_candidate`（久隆、宁明）→ `time_origin` 与 `timestamp` **均为 null**，
    只能显示 `T+xx.x h`，并注明"起点为候选依据（旧 meta 无规范 t0）"。
- 禁止：文件修改时间、目录名、GIF 帧号、`generated_utc` 冒充模拟时刻；禁止线性插值伪造中间帧。
- `temporal_interpolation` 恒为 `"none"`。

## 5. 统计

- 面积为原生网格单帧 `depth > threshold` 的**有效像元**面积之和；有效 = 非 NaN 且非海域。
- `areas_km2` / `wetted_cells` 的键为**字符串阈值**（`"0.05"`、`"0.15"`…）。
- `max_envelope` 为 `res.max`（模型自带全程最大包络）统计，附 `peak_frame_id` / `peak_time_s`。
- PNG 颜色**不能**用作定量来源；定量值只来自数值块。

## 6. 质量语义

- `runs[].quality.mass_gate` 取值 `PASS` / `FAIL-EXPLAINED` / `FAIL` / `unrecorded`。
  **PASS 不推导"预测正确"**；`FAIL-EXPLAINED` 不计入通过；无记录一律 `unrecorded`。
- `validation_status`、`vertical_datum_status`、`warnings[]` 逐案例展示，不写死总数。
- 页面展示的是"所选 run"的质量状态，不是全项目汇总。

## 7. 图层状态机

`remote_verified`（远端可读、未导入）｜`local_unchecked`（本地未验）｜`ready`｜`partial`｜`missing`｜`failed`

**只有 `ready` / `partial` 可勾选**；其余必须显示原因（`summary` / `note` / `missing_reason`），不得出现"开关亮着但没有图层"。
