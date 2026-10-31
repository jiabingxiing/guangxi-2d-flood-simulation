# 文档审计报告（documentation-standard · audit 模式）

- 审计对象：`2d_hydro_project/`（含 `flood_visualization/` 子工程）
- 审计日期：2026-11-01
- 审计模式：**只读审计**。未修改任何被审文档。
- 工具：`inventory_docs.py`、`lint_docs.py`（skill 脚本，仅标准库）

---

## 0. 总判定

**结构 lint 未通过（11 error / 29 warning），但其中 11 个 error 全部为工具误报。**
**真实的文档缺陷是 4 类事实漂移，lint 未检出。**

| 类别 | 数量 | 判定 |
|---|---|---|
| lint 报出的 error | 11 | **全部误报**（2 断链 + 9 重复 ID），见 §2 |
| lint 报出的 warning | 29 | 27 个为「无 frontmatter」（项目既有约定，见 §3）；1 个 placeholder；1 个误报 |
| **真实事实漂移** | **4 类** | **需修复**，见 §4 |

**结论：不能因为 lint 报 error 就判定文档不合格，也不能因为修完 lint 就判定文档正确。**
真实问题在 §4。

---

## 1. 文档盘点

| 项 | 数量 |
|---|---|
| 被审 Markdown 文档（排除 `models/`、`outputs/`、`.venv/`） | 67 |
| ADR 决策记录 | 10（ADR-0001 ~ ADR-0010） |
| `docs/` 阶段报告与手册 | 22 |
| `data/catalog/` 说明文档 | 6 |
| 诊断/候选/重跑文档 | 3 |
| AGENTS.md | 2（根级 `AGENTS.md`、`flood_visualization/AGENTS.md`） |
| 文档索引（`docs/README.md`） | 1 |

**权威文档分层现状**：

- 执行状态 SSOT：`docs/execution_status.md` —— 符合项目既有约定。
- 质量门 SSOT：`data/catalog/mass_recheck_g15.json` —— 机器可读，正确。
- 文档导航：`docs/README.md` —— 存在，但索引不全（见 §4-D3）。
- 根级 `AGENTS.md` 2392 字节 —— 符合 skill「保持短」的要求。

---

## 2. lint error 逐条核实：11 个全部为误报

### 2.1 broken-link（2 个）—— 目标文件真实存在，被 .gitignore 排除

| # | 文档:行 | 链接目标 | 文件是否存在 | gitignore |
|---|---|---|---|---|
| 1 | `flood_visualization/docs/optical_assets.md:4` | `data/processed/assets/manifest.json` | **存在** | `flood_visualization/.gitignore:38` `data/processed/*` |
| 2 | `flood_visualization/docs/optical_assets.md:79` | `data/external/optical/licenses/Sentinel_Data_Legal_Notice.pdf` | **存在** | `flood_visualization/.gitignore:41` `data/external/*` |

**判定：误报。** lint 按 git 跟踪范围解析链接，而这两类目录被项目刻意排除（可再生 / 大文件）。
链接在文件系统上有效。**不需要修改文档。**

**但记录一个真实风险**：这两个链接指向**可再生、不入库**的文件。他人克隆仓库后点击会 404。
若要让文档对外可用，应改为指向生成命令（如 `uv run python scripts/prepare_optical.py`），
或明写「本文件不入库，需本地生成」。这属**改进项，非缺陷**。

### 2.2 duplicate-id（9 个）—— 其他文档只是「引用」ADR 编号，不是「定义」

lint 把任意位置出现的 `ADR-000X` 字样都当作 ID 定义。实测：

- 10 个 ADR 的**定义**（一级标题）唯一，无重复：
  `# ADR-0001：…` 到 `# ADR-0010：…`，各一。
- 被判重复的其他文档，全部是**引用**。例：
  - `docs/execution_status.md:3`「模式：公开数据降级（ADR-0004/0005/0008）」
  - `docs/execution_status.md:113`「ADR-0004 降级」

**判定：误报。** 引用 ADR 编号是正确的文档实践，不是重复定义。**不需要修改文档。**

---

## 3. lint warning 分类

| code | 数量 | 判定 |
|---|---|---|
| `missing-frontmatter` | 27 | **项目既有约定**。全项目 67 篇文档均无 YAML frontmatter，包括 ADR-0001~0009（本会话之前既有）。skill 说「新文档**建议**使用」，非强制。**不建议为此批量改动** —— 会让 27 篇文档产生无价值 diff，且破坏 ADR 既有体例 |
| `placeholder` | 1 | `flood_visualization/standalone/_launcher/README.md` —— 该目录按 `.gitignore` 规则「只保留启动器真源」，是**有意保留的模板占位**。误报 |
| `missing-agent-context` | 1 | 报「缺 AGENTS.md」。实测根级 `AGENTS.md` **存在**（2392 字节）。**误报** |

---

## 4. 真实缺陷：4 类事实漂移（lint 未检出）

### D1【高】质量门计数三处不一致，且全部过时

**权威值**（`data/catalog/mass_recheck_g15.json`，`computed_utc` 2026-10-31T15:04:25Z）：

```
counts = 43 PASS / 3 FAIL-EXPLAINED / 0 FAIL
总 run = 46；久隆 14 个 run（11 PASS / 3 FAIL-EXPLAINED）
```

**文档中的值**：

| 文档 | 位置 | 记载值 | 与实际差 |
|---|---|---|---|
| `docs/execution_status.md` | :73 | 39 PASS（42 run） | **−4 run** |
| `docs/execution_status.md` | :80 | 39 PASS（42 run） | **−4 run** |
| `docs/execution_status.md` | :121（门禁矩阵 13.2 行） | 36 PASS | **−7** |
| `docs/adr/ADR-0010` | :13 | 39 PASS（42 run） | **−4 run** |
| `docs/adr/ADR-0010` | :157 | 39 PASS | **−4** |
| `docs/jiulong_100m_rerun.md` | :151 | 由 39 → 43（已写对） | 一致 |
| `docs/final_report.md` | :138 | 36 PASS | **−7** |
| `docs/RUNBOOK.md` | :72 | 36 PASS（2026-10-27） | **−7** |
| `docs/stage_summary_3cases.md` | :10 | 36 PASS（2026-10-27） | **−7** |
| `flood_visualization/SPEC.md` | :56 | 36 PASS | **−7** |

**成因**：本会话跑了 4 个新 100 m run，g15 从 42 → 46 run。`jiulong_100m_rerun.md`
记录了这个变化，但**其他 6 篇文档未同步**。这正是 SSOT 失效：同一事实有 3 个值。

**影响**：违反 skill 核心规则 2「每个事实只保留一个权威文档」。用户或 Agent 读
`execution_status.md` 会得到错误的通过数。

### D2【高】根级 `AGENTS.md` 未反映本会话新增内容

`AGENTS.md` 是 Agent 的项目上下文入口，skill 核心规则 3 要求它写「命令、目录、约束、
验证入口和文档指针」。实测：

- 未提 `ADR-0010`（治理口径的定版文档）
- 未提 `diagnostics/jiulong/`（Phase 0 诊断包目录）
- 未提 `outputs/display_qc/`（展示派生层目录）
- 未提新增的 6 个治理脚本与 3 个测试文件
- 未提 `uv run pytest` 会失败、须用 `python -m pytest`（本会话实测的坑）

**影响**：新 Agent 接手时无法从 `AGENTS.md` 得知本会话建立的治理约束与目录。

### D3【中】`docs/README.md` 索引不全

`docs/README.md` 是文档导航（skill 核心规则 4）。实测缺失 2 篇本会话新增文档：

- `docs/jiulong_100m_rerun.md`（100 m 重跑结果）
- `docs/jiulong_phase34_candidates.md`（Phase 3-4 候选与阻塞项）

另缺失 `diagnostics/jiulong/root_cause_report.md`（根因诊断，Phase 0 主交付）。

**影响**：读者按索引找不到这些文档。

### D4【中】`execution_status.md` 内部自相矛盾未清理

同一文档内三处计数不同（:73 的 39、:121 的 36）。文档已有注释说明「不同源，
以 mass_recheck_g15.json 为准」，但**保留两个过时值**本身仍是噪声。
skill 核心规则 2 要求「其他位置只写链接或摘要」。

---

## 5. 未确认项

| # | 事项 | 状态 | 确认方法 |
|---|---|---|---|
| U1 | `flood_visualization/docs/` 下 7 篇文档是否仍与代码一致 | **未确认** | 需逐篇对照 `src/floodviz/`；本次审计只做了结构与链接检查，未做事实核对 |
| U2 | `docs/final_report.md` 的 36 PASS 是否应改为当前值，还是应标注「历史快照」 | **未确认** | 取决于该文档定位（阶段成果报告 vs 滚动状态）。需用户或项目负责人判定 |
| U3 | 是否要为 27 篇文档补 frontmatter | **未确认** | 需用户决定；skill 只说「建议」 |
| U4 | `optical_assets.md` 的两个入库外链接是否应改写 | **未确认** | 取决于文档是否对外发布 |

---

## 6. 建议的修复计划（待批准后执行）

| 优先级 | 动作 | 涉及文件 | 风险 |
|---|---|---|---|
| P0 | 统一质量门计数为 43/3/0（46 run），并明写权威来源为 `mass_recheck_g15.json` | `execution_status.md`、`ADR-0010`、`SPEC.md` | 低（纯事实更正） |
| P0 | 历史快照类文档（`final_report.md`、`stage_summary_3cases.md`、`RUNBOOK.md`）的 36 PASS **加注「2026-10-27 历史口径，当前见 mass_recheck_g15.json」**，不改数字 | 同上 3 篇 | 低（保留历史，只加注） |
| P1 | 根级 `AGENTS.md` 补 ADR-0010、`diagnostics/jiulong/`、`outputs/display_qc/`、治理脚本入口、`python -m pytest` 命令要点 | `AGENTS.md` | 低 |
| P1 | `docs/README.md` 补 3 篇缺失文档索引 | `docs/README.md` | 低 |
| P2 | 清理 `execution_status.md` 内部过时计数，改为链接到权威 JSON | `execution_status.md` | 低 |
| P3 | （可选）27 篇文档补 frontmatter | 27 篇 | **中**（大量 diff，破坏既有体例） |
| P3 | （可选）`optical_assets.md` 改写入库外链接 | 1 篇 | 低 |

**不建议做的**：
- 不为消除 lint 误报而修改 `optical_assets.md` 链接或删改 ADR 引用。
- 不批量补 frontmatter，除非用户明确要求。

---

## 7. 交付门声明

**改动文件**：无（本次为只读审计）。

**检查命令与结果**：

```text
uv run --no-project --python 3.12 python <skill>/scripts/inventory_docs.py 2d_hydro_project
  -> 67 文档盘点完成

uv run --no-project --python 3.12 python <skill>/scripts/lint_docs.py 2d_hydro_project \
    --ignore models/ --ignore outputs/ --ignore .venv/
  -> status=fail, documents=67, errors=11, warnings=29
  -> error 分类：broken-link 2（误报）、duplicate-id 9（误报）
  -> warning 分类：missing-frontmatter 27（既有约定）、placeholder 1（误报）、missing-agent-context 1（误报）
```

**覆盖范围**：文档结构、链接可达性、ID 唯一性、frontmatter、索引完整性、质量门计数一致性、AGENTS.md 时效性。

**未覆盖**：文档陈述与代码/数据的事实正确性（除质量门计数外）；`flood_visualization/docs/` 7 篇的逐篇事实核对。

**未做的高风险变更及原因**：未批量补 frontmatter（会产生 27 篇无价值 diff 且破坏 ADR 既有体例）；未改动历史报告的数字（保留历史快照是正确做法，只需加注）。
