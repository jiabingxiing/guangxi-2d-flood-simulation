# 前端样式规范（frontend_style_guide.md）

> 适用对象：`flood_visualization/web/` 发布前端。
> 本文所有路径均相对 `flood_visualization/` 目录；不写绝对路径、盘符、用户名、token。
> 本文是样式的镜像说明，不是样式真源；真源见 §2。

## 1. 适用范围与硬约束

本前端是原生 HTML/CSS/JS 静态页面：无构建链、禁止运行时 CDN、禁止引入 UI 框架，父项目数据只读。因此 shadcn/ui、Tailwind CSS、DaisyUI、antd、React、Next.js、v0.dev 等依赖 bundler 或 CDN 的方案均不适用。

任何 UI 改动只能改唯一真源 `web/`，再经生成链 `web/` → `scripts/build_web.py --bundle-id <id>` → `outputs/web_bundle/<id>/assets/` → `scripts/sync_standalone.py --bundle-id <id>` → `standalone/广西二维洪涝可视化_双击打开/` 生效。`standalone/` 是生成物、未入 git，禁止手改；发布包内禁止绝对路径、用户名、盘符、token。`web/vendor/` 是本地固定版 MapLibre 5.24.0 与 Proj4js 2.22.0，不得替换为在线引用。事实纪律见 `AGENTS.md` §4，变更流程见 `AGENTS.md` §5。

## 2. 唯一真源声明

CSS 变量的唯一真源是 `web/styles.css` 的 `:root` 声明块；本文只是该真源的镜像说明，供评审与查阅。若本文表格与 `web/styles.css` 的 `:root` 在名称、值或用途上出现冲突，一律以 `web/styles.css` 为准，并回头修正本文；不得反过来改 `web/styles.css` 去迁就本文。新增、删改 token 时，先改 `web/styles.css` 的 `:root`，再同步本文 §3 表格。

## 3. 设计 Token 表

下表逐项列出已定稿 token，按 8 组组织。名称、值、用途三列；值以 `web/styles.css` 的 `:root` 为准。

### 3.1 主色（统一原 `#1677ff` 与 `#0284c7`/`#0369a1` 双主色）

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--accent` | `#0369a1` | 主色：选中态底色、主按钮、强调控件 |
| `--accent-strong` | `#075985` | 主色深阶：hover、按下态、强调标题 |
| `--accent-soft` | `#e0f2fe` | 主色浅底：信息区、浅底徽标、选中衬底 |
| `--on-accent` | `#ffffff` | 主色之上的文字与图标色 |

### 3.2 中性

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--bg` | `#f8fafc` | 页面底色 |
| `--surface` | `#ffffff` | 卡片、面板主表面 |
| `--surface-alt` | `#f1f5f9` | 次级表面、条纹区、表头 |
| `--border` | `#e2e8f0` | 常规描边、分隔线 |
| `--border-strong` | `#cbd5e1` | 强调描边、输入框边 |
| `--text` | `#0f172a` | 主文字 |
| `--text-2` | `#334155` | 次级文字 |
| `--text-3` | `#64748b` | 弱化、辅助、占位文字 |

### 3.3 语义三元组（背景／边框／文字统一，供 badge、notice、tag 共用）

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--ok` | `#059669` | 达标／成功状态文字与前景 |
| `--ok-bg` | `#ecfdf5` | 达标／成功状态背景 |
| `--ok-bd` | `#a7f3d0` | 达标／成功状态边框 |
| `--warn` | `#b45309` | 警示状态文字与前景 |
| `--warn-bg` | `#fffbeb` | 警示状态背景 |
| `--warn-bd` | `#fcd34d` | 警示状态边框 |
| `--err` | `#b91c1c` | 异常／失败状态文字与前景 |
| `--err-bg` | `#fef2f2` | 异常／失败状态背景 |
| `--err-bd` | `#fecaca` | 异常／失败状态边框 |
| `--info` | `#0369a1` | 提示／信息状态文字与前景 |
| `--info-bg` | `#f0f9ff` | 提示／信息状态背景 |
| `--info-bd` | `#7dd3fc` | 提示／信息状态边框 |

### 3.4 圆角（单一圆角档，替换原 5/6/10/12/14/20/9999px 七档）

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--r-sm` | `4px` | 小控件、输入、徽标 |
| `--r-md` | `6px` | 按钮、卡片内块 |
| `--r-lg` | `8px` | 卡片、面板容器 |
| `--r-pill` | `999px` | 胶囊 tab、标签 |

### 3.5 阴影（两层阴影，替换原 4 套加玻璃 blur）

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--shadow-1` | `0 1px 2px rgba(15,23,42,.06)` | 近地一层：卡片、按钮 |
| `--shadow-2` | `0 2px 8px rgba(15,23,42,.08)` | 浮起二层：浮层、HUD、下拉 |

### 3.6 间距

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--sp-1` | `4px` | 紧凑内距、图标间隙 |
| `--sp-2` | `8px` | 控件内距、元素间隙 |
| `--sp-3` | `12px` | 分组间距 |
| `--sp-4` | `16px` | 区块间距 |

### 3.7 字号

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--fs-xs` | `10.5px` | 角标、刻度、辅助注记 |
| `--fs-sm` | `11.5px` | 标签、表格、次要信息 |
| `--fs-md` | `12.5px` | 正文、按钮、默认字号 |
| `--fs-lg` | `14px` | 小标题、强调数值 |

### 3.8 其它

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `--mono` | `"Cascadia Mono","Consolas","SFMono-Regular","Menlo",monospace` | 等宽字体：数值、坐标、帧号、bundle-id 等机器文本 |

## 4. 允许 / 禁止清单

### 4.1 允许

| 项 | 说明 |
| --- | --- |
| CSS 变量 | 颜色、圆角、阴影、间距、字号一律走 `var(--*)`，取值来自 §3 |
| 原生组件类 | `.btn` / `.card` / `.badge` 及 §5 列出的按钮派生类，不引入组件库 |
| 数据色带的 `linear-gradient` | 仅 `.legend-gradient`、`.dem-grad-sub` 两处，用于表达连续数值色带，属数据编码而非装饰 |
| 语义三元组状态色 | `--ok*` / `--warn*` / `--err*` / `--info*` 的「文字／背景／边框」三件套，供 badge、notice、tag 共用 |
| 中性几何字符 | `◀` / `▶` / `\|` 用于折叠、播放方向、分隔；保持无彩色、无 emoji |

### 4.2 禁止

| 项 | 说明 |
| --- | --- |
| 硬编码颜色／圆角／阴影值 | 禁止把 `#xxxxxx`、`rgba(...)`、`px` 字面量直接写进组件规则；一律 `var(--*)`。`<canvas>` 与 MapLibre `paint` 不能读 CSS 变量，允许在 JS 内镜像同名色值并集中定义（见 `web/app.js` 的 `C` 调色板、`web/map.js` 的 paint 常量） |
| 装饰性渐变 | 除数据色带外，禁止用渐变做背景、按钮、标题装饰 |
| `backdrop-filter` 玻璃拟态 | 禁止毛玻璃与 `blur()` 效果（旧玻璃变量已废弃） |
| 彩色 emoji 图标 | 禁止用 emoji 当图标；改用中性几何字符或 CSS 绘制 |
| 新增框架或依赖 | 禁止引入任何需要 bundler、npm 或编译的库；`web/vendor/` 版本固定 |
| 运行时 CDN | 禁止外链脚本与样式、在线字体、在线图标等一切运行时网络依赖 |
| 任何插值伪造中间帧 | 6 h/帧为离散回放；播放速度不改物理分辨率，禁止补帧、插值生成中间帧 |

## 5. 组件使用规则

- 按钮统一用 `.btn` 系：`.btn-icon`、`.btn-primary`、`.btn-toggle`、`.btn-ghost`、`.speed-btn`、`.preset-btn`、`.case-tab`。新增按钮先复用已有类，不新建平行按钮体系。
- 容器统一用 `.card`：折叠面板、HUD、notice bar、图例、质量面板的外框都归入 `.card`。
- 状态色统一取语义三元组（`--ok` / `--warn` / `--err` / `--info` 各自的文字、背景、边框），供 badge、notice、tag 共用，不各自配色。
- 选中态统一 `background:var(--accent);color:var(--on-accent)`；hover 用 `--accent-strong`，浅底用 `--accent-soft`。
- 圆角只允许 4 档：`--r-sm`、`--r-md`、`--r-lg`、`--r-pill`；阴影只允许 2 层：`--shadow-1`、`--shadow-2`。禁止退回旧的七档圆角与「4 套阴影加玻璃 blur」。
- focus 态统一 2px 外环（如 `outline: 2px solid var(--accent); outline-offset: 2px` 或等价环），不得省略，不得各写各的宽度。

## 6. 组件清单

| 组件 | 说明 |
| --- | --- |
| 顶栏 | 项目标题与全局状态，含 `#project-title` |
| 案例 tab | 三案例切换（`.case-tab`） |
| 情景下拉 | 情景／run 选择 |
| header badges | 顶栏 badge 组，配色取语义三元组 |
| 左右折叠面板 | `.card` 容器 + 中性几何字符折叠指示 |
| 图层预设／透明度 | `.preset-btn` 与透明度滑块 |
| 在线底图 | 基于本地 vendor 渲染的在线瓦片开关 |
| 地图 HUD | 地图内浮层信息 |
| notice bar | 提示条，状态色取语义三元组 |
| 时间轴 | 离散帧回放（`.speed-btn`），6 h/帧 |
| 面积曲线 | 曲线与 `#chart-note` |
| 图例 | `.legend-gradient` / `.dem-grad-sub` 数据色带 |
| 质量面板 | `#quality-body` 质量守恒门 |
| 状态栏 | 许可与发布包文案 |

> 全屏门户 overlay 已移除，禁止重新引入。

## 7. 事实纪律指针

| 锚点／项 | 不可删改的文案与语义 |
| --- | --- |
| `#project-title` | 项目标题文案，不可删改 |
| `#origin-note` | 「候选依据」口径：`origin_status=relative_only_candidate`，不显示绝对时间、`timestamp` 为 null |
| `#temporal-note` | 「6 h/帧」离散回放；播放速度不改变物理时间分辨率；禁止插值伪造中间帧 |
| `#chart-note` | 「不是资产安全等级」 |
| `#quality-body` | 质量守恒门、`PASS`、垂直基准、情景级口径 |
| `#map-title` | 「当前帧模拟水深」与「全程最大水深包络（非某一时刻）」两种标题语义 |
| 状态栏 | 许可文案与发布包文案 |
| 三地时间轴 | 久隆／贵港 2024-09 摩羯、宁明 2026-08 紫檀，不共用绝对时间轴 |
| 质量门语义 | `PASS` ≠ 预测正确；`FAIL-EXPLAINED` 不计通过 |
| 未验证指标 | 必须写「未测」，禁止把目标值写成实测值 |

## 8. 变更与验收流程

1. 只改唯一真源 `web/`（`web/styles.css` 的 `:root` 与组件规则），不改 `standalone/`。
2. 构建：`uv run python scripts/build_web.py --bundle-id <id>`，产物落在 `outputs/web_bundle/<id>/assets/`。
3. 校验：执行 `floodviz validate --full`；完整命令为 `uv run python -m floodviz validate --bundle outputs/web_bundle/<id> --full`。
4. 同步前核对清单：`uv run python scripts/sync_standalone.py --bundle-id <id> --dry-run`，逐项确认将复制与将清理的文件。
5. 正式同步：`uv run python scripts/sync_standalone.py --bundle-id <id>`。
6. 对 `web/` 与 `standalone/assets/`（即 `standalone/广西二维洪涝可视化_双击打开/assets/`）逐文件做 SHA256 比对，确认一致。
7. 回归检查：`uv run python -m pytest -m "not slow and not e2e"`、`uv run python scripts/check_schemas.py`、`uv run ruff check src tests scripts`。
8. 用户手动双击 `standalone/广西二维洪涝可视化_双击打开/打开可视化.cmd` 验收；自动化不能代替人工打开确认。

> e2e 需设 `FLOODVIZ_E2E_BUNDLE=<id>` 才会服务新包；未设置时 e2e 仍指向旧包，不能用其通过与否判断新包。
