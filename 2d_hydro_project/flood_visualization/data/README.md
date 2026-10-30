# data/ —— 数据说明（大文件不入库）

本目录只在本地保存**引用、裁剪产物与条款**。`.gitignore` 排除 `data/raw/**`、`data/interim/**`、
`data/processed/**`、`data/external/**` 下的二进制，只保留 README 与 `data/splits/` 索引。

## 目录职责

| 目录 | 内容 | 入库 | 说明 |
|---|---|---|---|
| `raw/` | 只读引用或授权小样本 | 否 | 父项目原始数据一律**不复制**，只记录路径与哈希 |
| `interim/` | AOI 裁剪、临时转换 | 否 | 可重建；重建命令写入 `docs/build_report.md` |
| `processed/` | 可发布影像与数值块原料 | 否 | `optical/`、`assets/` 由 `scripts/prepare_optical.py` 生成 |
| `external/` | 第三方条款、许可 PDF、vendor 缓存 | 条款入库 | 不放数据本体 |
| `splits/` | 固定验证抽样索引 | **是** | 5% 有效像元 + 干湿边界/NoData/最大值样本，只存索引不存数据 |

## 事实来源

- 模型帧：父项目 `../outputs/<case>/<event>/<run_id>/results/res-XXXX.wd`
- 资产元数据：`plans/visualization_research/local_assets_metadata.json`（本任务 `plans/` 下）
- 质量门：父项目 `../data/catalog/mass_recheck_g15.json`
- 光学检索证据：`plans/宁明_贵港_公开可见光影像检索结果.md`

## 禁止

- 禁止把 2.05 GB 的广西 DEM、4.7 GB 的久隆 RGB、整景 Sentinel TIFF 复制进本目录。
- 禁止把父项目绝对路径写进发布包；发布包只允许包内相对路径。
- 禁止提交 `.env`、token、凭证。

## 重建

```powershell
$env:PYTHONUTF8='1'
uv run python scripts/prepare_optical.py       # 光学 / SAR 显示资产
uv run python -m floodviz prepare --bundle-id v3-20261028
```
