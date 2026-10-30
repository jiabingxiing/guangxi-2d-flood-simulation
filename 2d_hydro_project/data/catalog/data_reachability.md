# 外部数据源可达性报告（阶段 0）

- **探测日期**：2026-10-24 ｜ **脚本**：`scripts/stage0/probe_endpoints.py`（机读件 `reachability.json`）
- **方法**：requests 双路径（直连 = 忽略代理环境变量；代理 = 使用 `HTTPS_PROXY=127.0.0.1:7897`）；超时 (10s 连接, 20s 读)，无重试。

## 1. 结论（先行）

1. **境外数据源基本可达**：代理路径全部主要端点 200；多数端点直连亦 200。
2. **HEC-USACE 仅代理可达**（直连 ConnectionError）。
3. **UN-SPIDER 站点可达但终稿所引页面 404**（链接已迁移）——终稿 17.1 参考资料第 9 条需复核。
4. **Open-Meteo 归档 API 功能样例通过**（双路径），久隆锚点 2024-09-06~09 逐时降水已落盘 `probes/`。
5. **本机 curl 误报**：curl（schannel）对 HTTPS 报「吊销服务器已脱机」SSL 错误——是证书吊销检查问题，不是网络不通。**后续脚本统一用 requests/uv**，不用 curl 判定可达性。

## 2. 探测矩阵

| 端点 ID | 用途 | 直连 | 代理 |
|---|---|---|---|
| pypi_aliyun | Python 依赖镜像 | 200 | 200 |
| open_meteo_docs | 历史天气文档 | 200 | 200 |
| open_meteo_api | ERA5 归档 API | 200（取样成功） | 200（取样成功） |
| copernicus_dataspace | COP-DEM / Sentinel-1 | 200 | 200 |
| esa_worldcover | 土地覆盖 | 200 | 200 |
| fabdem | FABDEM 数据页 | 200 | 200 |
| seamlesswave | LISFLOOD-FP 官网 | 200 | 200 |
| hec_usace | HEC-RAS | ConnectionError | 200 |
| unspider | SAR 制图实践 | 404（页面迁移） | 404（页面迁移） |

## 3. 未尽事项

- 大文件下载（GLO-30 瓦片、S1 场景，GB 级）稳定性未测；首次正式下载须验证：断点续传/幂等、HTTP 与内容校验（哈希）、完整日志（终稿 四章）。
- 需账号注册的源（Dataspace、ASF、FABDEM、seamlesswave）由用户人工注册，凭证经 `.env`/环境变量注入，不入代码与仓库。
- D 盘仅剩 42 GB——下载前先解决磁盘预算（风险 R11）。
