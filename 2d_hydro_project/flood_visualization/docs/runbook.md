# 运行手册（docs/runbook.md）

> 适用：`2d_hydro_project/flood_visualization`（本子项目）。
> 只读父项目模型输出；不重跑模型；服务只绑 `127.0.0.1`。
> 唯一实施契约：`SPEC.md`。数据契约：`docs/data_contract.md`。

## 1. 直接使用已生成网页（推荐给非开发用户）

如果只想打开网页，不需要安装 Python、uv 或 Node.js。

1. 打开目录 `standalone\广西二维洪涝可视化_双击打开\`。
2. 双击 **`打开可视化.cmd`**。
3. 浏览器会自动打开 `http://127.0.0.1:8765/index.html`。
4. 关闭命令窗口即可停止服务。

目录已包含 **v6-20261029** 发布包的全部资源（104.2 MB、714 文件 + 4 启动器）。不要双击 `index.html`。直接使用 `file://` 会被浏览器的安全策略阻止 `fetch catalog.json`，页面会显示加载失败。
启动器使用 Windows 自带 PowerShell `HttpListener`。不需要管理员权限。端口 8765 被占用时，会自动尝试 8766–8775。

> 备用启动：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File server.ps1 -Port 8766`。
> 在线 Esri 底图需要网络。本地洪涝、DEM、SAR、光学图层不需要网络。

### 1.1 同步新发布包到 standalone（必须用脚本）

⛔ **不要用 `robocopy /MIR` 直接镜像发布包到 standalone 目录。**
`/MIR` 会删除目标里源包没有的文件，包括手工维护的 4 个启动器文件。
2026-10-28 已因此误删 `打开可视化.cmd`、`server.ps1`、`使用说明.txt`、`README.md`，双击打开失效。

正确做法：

```powershell
uv run python scripts/sync_standalone.py --bundle-id v6-20261029 --dry-run
uv run python scripts/sync_standalone.py --bundle-id v6-20261029
```

脚本行为：

1. 镜像发布包内容（`assets`、`cases`、`frames`、`licenses`、`numeric`、`catalog.json`、`index.html`、`manifest.json`）。
2. **清理残留**：删除 standalone 中源包已不存在的文件与空目录。
   换 run 时（如 200 m 旧 run → 100 m 真实DEM 新 run），`numeric/<case>/<old_run>/`、`frames/<case>/<old_run>/` 在源包里已不存在，但目标目录仍保留。
   实测 2026-10-30：残留 2 876 个文件、639.8 MB，双击打开会看到与发布包不一致的旧数据。
3. 同步前后都检查 4 个启动器文件存在；缺失即退出码 3，不做任何写入。
4. 带 `--dry-run` 只打印计划，并报告将清理的文件数与体积。
5. 重复执行幂等（第二次复制 0 个文件、清理 0 个文件）。

> 清理后应满足：standalone 文件数 = 发布包文件数 + 4 个启动器文件。
> 以 v6 为例：714 + 4 = **718**。
> 若数字不符，说明同步未完成或源包不完整。

## 2. 开发与重新构建的前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（脚本按 PowerShell 5.1 + cmd 编写；Linux 下命令等价，仅 `.cmd` 启动器不可用） |
| Python | 3.12（`uv` 管理；`.python-version` 已固定） |
| 工具 | `uv`（已装，走国内镜像配置） |
| 磁盘 | **F 盘 ≥15 GB 可用**（发布包单版 104–660 MB，保留两版 + 归档三版 + 临时目录）。2026-10-30 实测 F 盘可用 787.7 GB |
| 网络 | **非必需**。只有页面勾选在线 Esri 底图时才需要；离线可完整使用本地图层 |
| 数据 | 父项目 `outputs/**`、`data/**` 必须存在且完整（`inventory` 会校验） |

## 3. 开发环境首次安装

```powershell
cd 2d_hydro_project\flood_visualization
$env:PYTHONUTF8 = '1'
uv sync --frozen                 # 安装锁定依赖（含 dev：pytest/ruff/playwright）
uv run python scripts/fetch_vendor.py    # 抓固定版本 MapLibre 5.24.0 / Proj4js 2.22.0（带 sha256 校验）
```

> vendor 缓存留在 `data/external/vendor-cache/`；断网重建时用 `--offline`。

## 4. 发布包版本现状（2026-10-30 更新）

**当前交付包**：`v6-20261029`（100 m 真实DEM，104.2 MB、714 文件，16 946 项全量验证 0 失败）。
**构建级回滚包**：`v5-20261028`（100 m 真实DEM，94.5 MB、706 文件，同验证结果）。
**数据级回滚包**：`v4-20261028`（200 m 旧代，已归档）。
**历史归档**：`v2-20261027`、`v3-20261028`、`v4-20261028`。

### 4.1 两个目录的分工

| 目录 | 内容 | 由谁读取 |
|---|---|---|
| `outputs/web_bundle/` | 当前可服务包：`v5-20261028`、`v6-20261029` | `start_local.cmd`、`prune_bundles.py` |
| `outputs/_archive_bundles/` | 历史 200 m 包：`v2-20261027`、`v3-20261028`、`v4-20261028` | 仅人工回滚时使用 |

`prune_bundles.py` **只扫描 `outputs/web_bundle/`**，归档目录不会被它删除。

### 4.2 交付包与回滚包不能混用

| 包 | 数据代 | 用途 | 能恢复什么 |
|---|---|---|---|
| `v6-20261029` | 100 m 真实DEM | 交付 | — |
| `v5-20261028` | 100 m 真实DEM | 构建级回滚 | 页面、资源、构建产物 |
| `v4-20261028` | 200 m 旧代 | 数据级回滚 | 200 m 旧模型数据与结果 |

**关键事实（2026-10-30 实测）**：v5 与 v6 的 `numeric/` 共 506 个数值块**逐位一致，0 个不同**。v6 只比 v5 多 8 张品牌图片。

因此：

- 页面或资源出错 → 退回 `v5`，有效。
- **数据或成果出错 → 退回 `v5` 无效**，看到的仍是同一份模型数据。必须退回 `v4`。

### 4.3 构建发布包（**顺序不可颠倒**）

```powershell
$env:PYTHONUTF8 = '1'

# 1) 扫描源 run：帧数、栅格头、时间一致性。任何 REJECT 都不得进入下一步
uv run python -m floodviz inventory --config configs/default.yaml

# 2) 生成发布包（数值块 + 显示图 + 统计 + catalog + manifest）
uv run python -m floodviz prepare --config configs/default.yaml --bundle-id <NEW_BUNDLE_ID>

# 3) 把静态页面与本地 vendor 复制进包根，并置 manifest.web.ready=true
uv run python scripts/build_web.py --bundle-id <NEW_BUNDLE_ID>

# 4) 全量门禁（数值回读 / 统计复算 / 空间残差 / 结构 / schema），成功后回填 manifest.validation
uv run python -m floodviz validate --bundle outputs/web_bundle/<NEW_BUNDLE_ID> --full
```

> 历史示例：v6 的构建命令与上面一致，仅把 `<NEW_BUNDLE_ID>` 换成 `v6-20261029`。

**为什么顺序不能变**：`validate` 要求 `manifest.web.ready=true`（否则告警"页面未就绪"）；
`build_web` 要求包内已有 `manifest.json`（由 `prepare` 写出）。

**构建期间不要读该 bundle**：`prepare` 先写 `.<id>.building-<pid>/`，全部成功后才改名为正式目录。
构建中读取正式目录会读到中间态（实测踩过：报 `FileNotFoundError` 某 .bin 不存在，实际是尚未完成）。

### 快速冒烟（开发用，不用于交付）

```powershell
uv run python -m floodviz prepare --config configs/default.yaml --bundle-id dev1 --only jiulong --frames-limit 3
uv run python scripts/build_web.py --bundle-id dev1
uv run python -m floodviz validate --bundle outputs/web_bundle/dev1
```

`--only` 为子集构建，`default_case` 会临时改到子集内；**不可作为交付包**。

## 5. 启动与访问

```powershell
scripts\start_local.cmd                 # 服务最新版包（当前为 v6-20261029）
scripts\start_local.cmd v6-20261029     # 服务指定包
scripts\start_local.cmd v5-20261028     # 构建级回滚
$env:PORT=8766; scripts\start_local.cmd # 换回环端口
```

浏览器打开 **<http://127.0.0.1:8765/index.html>**。

启动器行为：
1. 未指定参数时按修改时间取 `outputs/web_bundle/` 下最新的包；
2. 校验 `index.html` 存在（缺则提示先跑 `build_web`）；
3. 打开浏览器并执行 `python -m http.server <port> --bind 127.0.0.1 --directory <bundle>`；
4. **只绑回环**，不对外开放；`Ctrl+C` 停止。

> 服务根只指向发布包目录，不暴露父项目、`.env`、源盘路径或未脱敏资料。

## 6. 回滚（两级回滚 + 归档）

发布包命名 `<id>-<YYYYMMDD>`，历史包**不删除**。

### 6.1 查看现状

```powershell
# 当前可服务包
Get-ChildItem outputs\web_bundle -Directory | Sort-Object Name

# 已归档的历史包
Get-ChildItem outputs\_archive_bundles -Directory | Sort-Object Name
```

### 6.2 构建级回滚（页面/资源问题）

只改启动器目标，不删新包、不改旧包。

```powershell
scripts\start_local.cmd v5-20261028
```

适用场景：v6 页面报错、资源 404、样式错乱、JS 异常。

**不适用**：数据或模型结果错误。因为 v5 与 v6 的数值块逐位相同。

### 6.3 数据级回滚（数据/成果问题）

必须先把归档包移回 `web_bundle/`，因为启动器按 `outputs\web_bundle\` 定位包。

```powershell
# 1) 把归档的 v4 移回可服务目录
Move-Item outputs\_archive_bundles\v4-20261028 outputs\web_bundle\

# 2) 服务 v4
scripts\start_local.cmd v4-20261028
```

适用场景：模型数据或成果判定有误，需要回到 200 m 旧代结果做对照。

**回滚后注意**：

- `v4` 是 200 m 旧代，run 集合与 v6 不同（`scen_free_200m_sv6h`、`scen_yagi_q11500_30m_b5` 等）。
- `v4` 的验证记录只有基础校验，无 `gate_summary`；不得把它当"已通过全量门"引用。
- 完成对照后，把 `v4` 移回归档目录，保持 `web_bundle/` 只有 v5/v6。

### 6.4 归档包清单

| 包 | 数据代 | 文件数 | 体积 | 归档位置 |
|---|---|---:|---:|---|
| `v2-20261027` | 200 m | 2 912 | 654.1 MB | `outputs/_archive_bundles/` |
| `v3-20261028` | 200 m | 2 921 | 656.2 MB | `outputs/_archive_bundles/` |
| `v4-20261028` | 200 m | 2 960 | 660.4 MB | `outputs/_archive_bundles/` |
| `v5-20261028` | 100 m 真实DEM | 706 | 94.5 MB | `outputs/web_bundle/` |
| `v6-20261029` | 100 m 真实DEM | 714 | 104.2 MB | `outputs/web_bundle/` |

归档验证记录（2026-10-30 实测）：

- 三个归档包三件套齐全（`index.html`、`manifest.json`、`catalog.json`）。
- 归档文件数与归档前完全一致。
- 归档 `v4` 用 `python -m http.server` 独立验证：`/index.html`、`/catalog.json`、`/manifest.json`、`numeric/jiulong/scen_free_200m_sv6h/f0000/0/0.bin` 全部 HTTP 200。

### 6.5 ⛔ 清理前必读

`prune_bundles.py` 的硬下限是 `MIN_KEEP = 2`（`scripts/prune_bundles.py:25`）。

- 它只扫描 `outputs/web_bundle/`，**归档目录安全**。
- 当前 `web_bundle/` 只剩 v5、v6，`--dry-run` 显示「保留 2 个、候选删除 0 个」。
- **禁止**在 v2–v4 未归档的情况下执行 `--apply`，否则 200 m 数据永久丢失。

```powershell
# 只允许先 dry-run 查看
uv run python scripts/prune_bundles.py --keep 2 --dry-run
```

### 6.6 回滚演练记录

| 日期 | 步骤 | 结果 |
|---|---|---|
| 2026-10-27 | 服务 `v2-20261027`，`GET /index.html`、`GET /catalog.json` | 均 200；久隆 `optical=partial`、`sar=ready` |
| 2026-10-30 | 归档 v2/v3/v4 后，`prune_bundles.py --keep 2 --dry-run` | 保留 2 个、候选删除 0 个，无删除风险 |
| 2026-10-30 | 归档 `v4` 用 `http.server` 独立服务 | 4 项资源全 200，含 200 m 数值块 |

## 7. 回填与重建的触发条件

出现以下任一情况必须重跑 `prepare → build_web → validate`（**不需要"再训练"**）：

1. 模型输出新增/替换（新 `run_id` 或既有 run 的 `res-*.wd` 变化）；
2. `data/processed/assets/manifest.json` 变化（光学/SAR 资产更新）；
3. `configs/**` 的案例、run 白名单、阈值、色带、底图变化；
4. `web/**` 页面代码变化（只需重跑 `build_web` + `validate`）。

`prepare` 会对源帧逐个算 sha256 并写入 `timeline.frames[].sha256` 与 `manifest.content`，
因此输入变化必然导致产物哈希变化，可据此判断是否需要重建。

## 8. 测试

```powershell
$env:PYTHONUTF8 = '1'

uv run ruff check src tests scripts              # 代码质量
uv run python -m pytest -m "not slow and not e2e" -q   # 单元 + 集成（约 5 s）
uv run python -m pytest tests/e2e -m e2e -q            # 浏览器 e2e（约 60 s，自动起 8790 端口服务）
uv run python -m pytest tests/e2e -m slow -q           # 性能与资源完整性（约 2–3 min）
uv run python scripts/check_schemas.py                 # 契约示例对 schema 自检
uv run python scripts/verify_bundle.py outputs/web_bundle/v6-20261029 --samples 40   # 独立验收（Lead 侧）
```

e2e 端口约定：**8765 交付启动器、8790 e2e、8788 web 组开发**。占用会直接报错。

> 2026-10-30 实测：`pytest -m "not slow and not e2e"` 共 **253 项全部通过**，退出码 0。

### 8.1 30 分钟连续播放门（发布前必跑）

用户已决定把 30 分钟连续播放纳入发布门。未跑或失败则不得发布。

```powershell
# 完整门（默认 1800 s，约 31 min）
uv run python -m pytest tests/e2e/test_e2e_longplay_30min.py -m slow -q

# 调试时可用环境变量缩短；发布门必须用默认值
$env:FLOODVIZ_LONGPLAY_SECONDS = '150'
uv run python -m pytest tests/e2e/test_e2e_longplay_30min.py -m slow -q
Remove-Item Env:\FLOODVIZ_LONGPLAY_SECONDS
```

门禁项：

| 项 | 要求 |
|---|---|
| 时长 | ≥ 1800 s 连续播放 |
| 帧推进 | 至少观察到 4 个不同帧号 |
| 越界 | 按案例分别不超上界（久隆 20、宁明 18、贵港 56） |
| 覆盖 | 三地都播放到 |
| 内存·绝对 | 峰值 ≤ 1.5 GB |
| 内存·稳态增长 | 预热 420 s 后，峰值相对稳态基线增长 ≤ 20% |
| 内存·稳态斜率 | 预热 420 s 后线性斜率 ≤ 2.0 MB/min |
| LRU 缓存 | ≤ 256 MB，`overflow=false`，`failures=0` |
| 错误 | 控制台错误 0 |

证据落在 `outputs/qa/longplay_30min.json`。

> **采样与基线口径（实测得出，勿随意改）**
>
> 1. 采样间隔必须显著短于一场播放时长。贵港 57 帧 @2 fps 约 28 s 播完；
>    若间隔 60 s，每次都采到末帧，无法验证帧推进。当前用 5 s。
> 2. **不要用起始内存作泄漏基线。** 页面加载后 JIT、WebGL 纹理池、字体与瓦片缓存
>    会一次性拉升内存（实测 0–420 s 为 +45.8 MB/min，420 s 后仅 +0.36 MB/min）。
> 3. 起始值还可能被残留进程污染：`tasklist` 汇总全机 Chromium 系进程，
>    实测出现过起始 473.0 MB 高于首个采样 366.2 MB 的情况。
>
> 因此增长门基线取**预热结束后的首个采样点**，起始值仅作诊断记录。
> 2026-10-30 首次按「起始值」口径跑，判出 +44.3% 失败，实为预热，非泄漏。

**最近一次实测（2026-10-30，门通过）**：

| 指标 | 实测 |
|---|---|
| 时长 / 采样点 | 1 802 s / 326 |
| 控制台错误 | 0 |
| LRU 缓存 | 66.7 MB 恒定，0 溢出 / 0 失败 / 0 淘汰 |
| 案例覆盖 | 贵港、久隆、宁明 |
| 帧号 | 24 个不同帧号（9–56） |
| 内存峰值 | 748.4 MB（门 1 536） |
| 稳态增长 | +3.38%（门 20%） |
| 稳态斜率 | −2.16 MB/min（门 2.0） |

> t=661 s 处 RSS 由 727 MB 降至 447 MB，为浏览器内存回收，属正常。
> 报告该结果时必须写明这次下跌，不得只写"斜率达标"。

## 9. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `build_web` 报"web/ 缺少文件" | `web/` 页面文件未齐 | 确认 `index.html/styles.css/app.js/map.js/timeline.js` 均在 `web/` |
| `build_web` 报"缺少 vendor" | 未抓前端资源 | `uv run python scripts/fetch_vendor.py` |
| `validate` 告警"页面未就绪" | 未跑 `build_web` 或顺序颠倒 | 按 §4.3 顺序重跑 |
| `prepare` 报 `PipelineError: 检测到其它构建进程` | 并发构建同一 bundle | 等待或结束另一进程；**同一 bundle 只允许单点构建** |
| 浏览器打开空白页 | 端口未起 / 直接双击 `index.html`（`file://`） | 必须走 `http://127.0.0.1:8765/index.html`；`file://` 下 `fetch` 会被浏览器安全策略拦截 |
| 页面提示"在线底图不可用" | 无网络或 Esri 服务不可达 | 正常降级：本地图层仍可用；可点"重试在线底图" |
| 点查显示"该点位于模型域外" | 点击位置不在 rows×cols 网格内 | 属预期行为，不夹边取值 |
| 当前帧显示"无淹没像元" | 该帧模型结果确为全干（如久隆 T+0–24 h、宁明 T+0） | 正常，不是加载失败 |
| 双击打开后看到旧场景/旧算例 | standalone 残留：换 run 后旧 `numeric/<case>/<old_run>/` 未清理 | 重跑 `sync_standalone.py --bundle-id <新包>`；脚本会清理残留。校验文件数应为「包文件数 + 4」 |
| 中文路径报编码错误 | PowerShell 未设 UTF-8 | `$env:PYTHONUTF8='1'` |
| 移动/复制了包导致启动失败 | 启动器按 `outputs\web_bundle\` 定位包 | 用 `start_local.cmd <bundle_id>` 显式指定；归档包需先移回 `web_bundle/` |
| `prune_bundles.py` 报"低于硬下限 2" | `--keep` 小于 2 | 回滚必须有目标版本，不允许只留 1 个包 |

## 10. 目录与清理

- 当前可服务包：`outputs/web_bundle/<id>/`（不入库，保留最近两版：v5、v6）
- 归档历史包：`outputs/_archive_bundles/<id>/`（不入库，v2、v3、v4；**prune 不扫描此目录**）
- 证据：`outputs/qa/`（e2e 截图、`perf_e2e.json`、`validate_*.json`）
- 临时构建：`outputs/web_bundle/.<id>.building-<pid>/`（失败会残留，可安全删除）
- 脚本缓存：`data/external/vendor-cache/`（vendor tarball，可删，重跑 fetch 会重建）
- standalone 备份：`standalone/_backup_YYYYMMDD/`（人工清理前备份，确认无误后可删）

清理命令（**删除前确认路径**）：

```powershell
# 只清理失败残留的临时构建目录
Get-ChildItem outputs\web_bundle -Directory -Filter '.*.building-*' | Remove-Item -Recurse -Force
```

⛔ **不要**对 `outputs/web_bundle/ v5-*`、`v6-*` 或 `outputs/_archive_bundles/` 下任何目录执行删除。
归档目录是唯一的数据级回滚源。
