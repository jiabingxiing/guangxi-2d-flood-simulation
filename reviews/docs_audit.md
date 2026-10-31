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

## 6. 修复执行结果（用户批准 P0–P3 后）

| 优先级 | 动作 | 状态 | 结果 |
|---|---|---|---|
| P0 | 统一质量门计数为 43/3/0（46 run） | **已完成** | `execution_status.md`（:73/:80/:121 三处）、`ADR-0010`（:13/:157）、`SPEC.md`（:56）、`RUNBOOK.md`（:72）全部改为当前值并指向权威 JSON |
| P0 | 历史快照加注不改数字 | **已完成** | `final_report.md`、`stage_summary_3cases.md` 保留 36 PASS，加注「2026-10-27 阶段快照，当前见 mass_recheck_g15.json」 |
| P1 | 根级 `AGENTS.md` 补新增内容 | **已完成** | 补 ADR-0010、`diagnostics/jiulong/`、`outputs/display_qc/`、久隆治理基线计划、6 个治理脚本、3 个测试、质量门 SSOT 指针、`python -m pytest` 实测坑 |
| P1 | `docs/README.md` 补索引 | **已完成** | 新增「久隆治理」分节（2 篇）与 `diagnostics/jiulong/` 指针；补质量门 SSOT 与久隆治理计划链接 |
| P2 | 清理 `execution_status.md` 内部矛盾 | **已完成** | 删除「不同源」注记，改为正式约定「以 mass_recheck_g15.json 为准」 |
| P3 | 27 篇文档补 frontmatter | **已完成** | 实际补 28 篇（含 `docs/README.md`）；`missing-frontmatter` warning 由 27 → **0** |

**未执行（有意）**：
- 未修改 `optical_assets.md` 的两个入库外链接（lint 误报，文件真实存在）。
- 未删改任何 ADR 引用（`duplicate-id` 误报）。
- `placeholder` 1 个（`standalone/_launcher/README.md`）与 `missing-agent-context` 1 个（根级 AGENTS.md 实际存在）保留，均为误报。

### 6.1 修复后 lint 复核

```text
改动前：documents=67, errors=11, warnings=29
改动后：documents=67, errors=11, warnings=2
  error  codes: broken-link 2（误报）, duplicate-id 9（误报）
  warning codes: placeholder 1（误报）, missing-agent-context 1（误报）
  frontmatter present: 28/67（docs/ 全部 28 篇已补；其余 39 篇为 data/catalog、flood_visualization 等）
```

### 6.2 回归验证

| 检查 | 结果 |
|---|---|
| `uv --directory 2d_hydro_project run python -m pytest -q` | **152 passed** |
| `floodviz pytest -m "not slow and not e2e"` | **253 passed** |
| `floodviz inventory --config configs/default.yaml` | **总体：通过** |

**结论**：文档改动未影响任何测试或发布流程。

---

## 7. 交付门声明

**改动文件与目的**：

| 文件 | 目的 |
|---|---|
| `AGENTS.md` | 补 ADR-0010、新增目录与脚本指针、质量门 SSOT、`python -m pytest` 实测坑 |
| `2d_hydro_project/docs/execution_status.md` | 质量门计数 39→43；久隆 run 10→14；清理内部矛盾 |
| `2d_hydro_project/docs/adr/ADR-0010-*.md` | 两处质量门计数 39→43 |
| `2d_hydro_project/docs/final_report.md` | 加「历史快照」注记，数字不变 |
| `2d_hydro_project/docs/stage_summary_3cases.md` | 加「历史快照」注记，数字不变 |
| `2d_hydro_project/docs/RUNBOOK.md` | 质量门计数 36→43 |
| `2d_hydro_project/docs/README.md` | 补 3 篇索引、新增「久隆治理」分节、质量门 SSOT 指针 |
| `2d_hydro_project/flood_visualization/SPEC.md` | 质量门计数 36→43 |
| `2d_hydro_project/docs/**/*.md`（28 篇） | 补 YAML frontmatter |
| `reviews/add_frontmatter.py` | 幂等补 frontmatter 脚本（支持 --dry-run/--apply） |
| `reviews/docs_audit.md` | 本审计报告 |

**检查命令与结果**：

```text
lint_docs.py 改动前 -> errors=11, warnings=29
lint_docs.py 改动后 -> errors=11, warnings=2
  （11 个 error 全部为已核实的工具误报；27 个 missing-frontmatter 已消除）

uv --directory 2d_hydro_project run python -m pytest -q          -> 152 passed
floodviz pytest -m "not slow and not e2e"                        -> 253 passed
floodviz inventory --config configs/default.yaml                 -> 总体：通过
```

**覆盖范围**：文档结构、链接可达性、ID 唯一性、frontmatter 完整性、索引完整性、质量门计数一致性、AGENTS.md 时效性。

**未执行的检查与未确认事实**：
- 未做 `flood_visualization/docs/` 7 篇的逐篇事实核对（只做了结构与链接检查）。
- 未确认 `final_report.md` 除质量门外的其他数字是否仍与代码一致。
- 未确认 `optical_assets.md` 入库外链接是否需要改写（取决于文档是否对外发布）。

**未做的高风险变更及原因**：
- 未修改 `optical_assets.md` 的 2 个链接（lint 误报，文件真实存在；改动无收益）。
- 未删改任何 ADR 引用（`duplicate-id` 误报）。
- 未改动历史报告的数字（保留历史快照是正确做法，只需加注）。

**不得把「Markdown 格式正确」写成「文档事实正确」。** 本次修复的是可核验的事实漂移（质量门计数）；文档陈述与代码的一致性只覆盖质量门一项。
