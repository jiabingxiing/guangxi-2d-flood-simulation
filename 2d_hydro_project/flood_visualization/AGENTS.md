# AGENTS.md —— floodviz 子项目协作约定

> 本目录是**只读父项目的发布工程**，不是模型工程。所有 Agent 必须遵守下列约定。

## 1. 目录与职责

```text
flood_visualization/
├── configs/        # 项目/案例/图层/前端版本配置（改这里改行为）
├── src/floodviz/   # 预处理与发布代码（Python 3.12）
├── web/            # 静态前端（无构建链，vendor 固定版本）
├── scripts/        # 一次性脚本：抓 vendor、构建页面、启动、契约示例、schema 自检
├── schemas/        # 发布包 JSON Schema（机器可读契约）
├── tests/          # 单元 / 集成 / e2e
├── data/           # 只读引用、裁剪产物、外部条款（大文件不入库）
├── docs/           # 数据契约、许可、验收、运行手册
└── outputs/        # 运行产物：web_bundle/<bundle_id>（不入库）
```

## 2. 只读源规则（硬约束）

- **只读**父项目 `../outputs/**`（模型帧）与 `../data/**`（DEM/SAR/影像）。
- **禁止**修改、移动、删除父项目的 `pyproject.toml`、`uv.lock`、`.venv`、`models/`、`outputs/`、`configs/`。
- **禁止**重跑模型（`lisflood.exe`）。
- **禁止**把原始大文件（DEM 2.05 GB、RGB 4.7 GB、整景影像）复制进本目录；需要裁剪时只保留裁剪后小图。
- **禁止**在发布包中写入绝对路径、用户名、盘符、`.env`、token。

## 3. 命令

```powershell
$env:PYTHONUTF8 = '1'
uv run python -m floodviz inventory --config configs/default.yaml
uv run python -m floodviz prepare   --config configs/default.yaml --bundle-id v6-20261029
uv run python scripts/build_web.py  --bundle-id v6-20261029
uv run python -m floodviz validate  --bundle outputs/web_bundle/v6-20261029 --full
uv run python scripts/check_schemas.py
uv run ruff check src tests scripts
uv run python -m pytest -m "not slow and not e2e" -q
```

启动（仅回环）：
```powershell
scripts\start_local.cmd            # 服务最新包
scripts\start_local.cmd v6-20261029
```

> 包版本现状（2026-11-02）：交付 `v8-20261102`（3 案例 / 11 run，9/9 校验组通过）；构建级回滚 `v7-20261031`；
> 上一交付 `v6-20261029`；数据级回滚 `v4-20261028`（在 `outputs/_archive_bundles/`，需先移回 `outputs/web_bundle/`）。
> 详见 `docs/runbook.md` §4、§6。

## 4. 事实纪律

- 三地不是同一场洪水：久隆/贵港 2024-09 摩羯，宁明 2026-08 紫檀；**不共用绝对时间轴**。
- 久隆、宁明的 `origin_status=relative_only_candidate`：**不得**显示绝对时间，`timestamp` 必须为 null。
- 影像采集时刻不随模拟滑块变化；灾后影像不得配给灾前时刻。
- 6 h/帧是离散回放；播放速度变化不提高物理时间分辨率；**禁止**插值伪造中间帧。
- 质量门 `PASS` 不等于"预测正确"；`FAIL-EXPLAINED` 不计通过；无记录写 `unrecorded`。
- 未验证的性能指标必须写"未测"，禁止把目标值写成实测值。

## 5. 变更流程

1. 改配置（`configs/**`）→ 跑 `inventory` → 改代码（`src/**`）→ 跑 `pytest`。
2. 契约变更（`schemas/**`、`docs/examples/**`、`docs/data_contract.md`）必须三处同步，并跑 `scripts/check_schemas.py`。
3. 前端 `web/**` 只能用 `web/vendor/**` 的本地资源；**禁止**运行时 CDN。
4. 发布包必须同时保留最近两个 `bundle_id`，回滚时只改启动器目标，不删旧包。
   清理用 `uv run python scripts/prune_bundles.py --keep 2 --dry-run` 先看计划，再 `--apply`；脚本硬下限是 2 个，不允许删到只剩 1 个（否则回滚必须先重建）。
