---
doc_type: reference
status: active
topic: docs-audit-20261124
canonical: true
scope: project
---

# 项目文档只读审计与健康度检查报告（2026-11-24）

- **审计对象**：项目根目录、`plans/`、`2d_hydro_project/docs/`（含 `adr/`）、`2d_hydro_project/data/catalog/`、`2d_hydro_project/flood_visualization/docs/`、`reviews/`
- **审计基准**：`documentation-standard` skill、Diátaxis 架构、SSOT 原则、质量门（G15）
- **审计日期**：2026-11-24
- **审计模式**：**只读审计（Read-only Audit）**

---

## 一、审计结论概览

| 检查维度 | 状态 / 数量 | 判定与影响说明 |
|---|---|---|
| **被审 Markdown 总量** | 85 篇 | 覆盖根目录、方案库、决策链、阶段报告与子工程说明 |
| **SSOT 事实漂移（D1）** | **3 处过时** | 质量门全表权威计数已升级为 **66 PASS / 4 FAIL-EXPLAINED / 0 FAIL（共 70 run）**，部分文档仍记录 61-3-0（64 run）或早期值 |
| **ADR 决策覆盖度（D2）** | **4 篇未索引** | 实际存在 ADR-0001 至 **ADR-0022**（22 篇），但 `docs/README.md` 与根级 `AGENTS.md` 仅索引至 ADR-0018 |
| **模型基线与情景升级（D3）**| **已升级** | 久隆双驱动重构主情景（R3 净雨+钦江来水+SGC）已落地并成为发布默认层，早期“纯雨自由出流”口径需明确定位为历史对照 |
| **Frontmatter 元数据覆盖** | 43 篇规范 / 42 篇缺失 | 核心文档已规范化，`plans/` 方案库及早期文档缺少 YAML frontmatter |
| **全工程统一导航（MASTER）**| **缺失** | 当前导航依赖 `2d_hydro_project/docs/README.md`，缺少全局根级 `docs/MASTER.md` |
| **链接有效性（Broken Links）**| 0 处核心断链 / 2 处外部路径 | `2d_hydro_project/docs/` 内相对链接 **100% 有效**；`plans/` 存在 3 处硬编码宿主绝对路径 |
| **模板占位符（TODO/TBD）** | 0 处 | 生产与决策文档中未发现遗留的未闭合占位符或未处理 TODO |

---

## 二、真实事实漂移（必须修复项）

### 2.1 D1【高】质量门全表计数漂移
- **权威真源（SSOT）**：`2d_hydro_project/data/catalog/mass_recheck_g15.json`（时间戳 `2026-11-24T03:40:19Z`）
  - **PASS**：66
  - **FAIL-EXPLAINED**：4（HFIX/HVAR 合理超限明示且不计通过）
  - **FAIL**：0
  - **总 run 数**：70
- **漂移点分析**：
  - 根目录 `AGENTS.md`（行 58、80）：记载为 `61 PASS / 3 FAIL-EXPLAINED / 0 FAIL（64 run）`，落后 6 个 run（含新纳入的 Phase H 学术 run 与久隆双驱动 R 系列 run）。
  - `2d_hydro_project/docs/execution_status.md`：最新章节停留在 2026-11-02（Phase E），久隆逐 run 质量门仍记录 2026-10-31 快照，未更新至 70 run 全量表。
  - `2d_hydro_project/docs/README.md`（行 64）：记载 `61 PASS run`，需更新为 66 PASS。

### 2.2 D2【高】ADR 架构决策记录缺失索引
- **落盘现状**：`2d_hydro_project/docs/adr/` 目录下实际完整存在 22 篇 ADR：
  - `ADR-0019-久隆钦江上游来水与水文驱动.md`
  - `ADR-0020-久隆河道亚网格与洪泛区耦合.md`
  - `ADR-0021-久隆斑块物理归因与展示质控.md`
  - `ADR-0022-久隆历史工况与边界重建.md`
- **漂移点分析**：
  - `2d_hydro_project/docs/README.md` 第 67 行标题写为 `## 决策记录（adr/，ADR-0001~0018）`，清单只到 0018。
  - 根目录 `AGENTS.md` 决策记录清单停留在 `ADR-0001~0018`。

### 2.3 D3【中】久隆双驱动主情景升级与基线状态同步
- **代码与配置现状**：
  - 依据用户批准的 `plans/code_artifact.md`，久隆已实现 R3 双驱动（GA 净雨 50.7 mm + 钦江来水 97 Mm³ + SGC 亚网格河道），湿面积 217.52 km²，斑块显著减少。
  - `2d_hydro_project/flood_visualization/configs/cases/jiulong.yaml` 中，`default_run_id` 已更新为 `scen_r3_dual_sgc_200m_sv6h`（生产主情景），而旧 `scen_free_200m_sv6h` 改标为 `role: baseline`（历史纯雨驱动对照）。
  - `2d_hydro_project/scripts/stage6/make_viz.py` 已补充生成 `fig7_jiulong_dual_forcing.png` 双驱动对照图。
- **漂移点分析**：
  - `AGENTS.md`、`execution_status.md` 及部分说明仍将 `scen_free_200m_sv6h` 称为“默认生产基线”，需更新口径以保持文档与代码/配置事实一致。

---

## 三、文档结构、元数据与 Diátaxis 分类审计

### 3.1 元数据 Frontmatter 覆盖现状
- 43 篇文档已声明符合 `documentation-standard` 的 YAML Frontmatter：
  - 包含 `doc_type`（tutorial, how-to, reference, explanation）、`status`、`topic`、`canonical`、`scope`。
- 42 篇文档缺少 Frontmatter：
  - 主要集中在 `plans/`（17 篇技术方案与调研）、`reviews/`（3 篇审计与评审）、以及早期的阶段实验报告。
  - **建议**：为技术方案与主工程核心文档按需增补 Frontmatter，不强行批量破坏历史纯文本归档。

### 3.2 导航索引规范化
- 当前工程缺少顶层的统一入口导航 `docs/MASTER.md`。
- 当前最权威的文档入口是 `2d_hydro_project/docs/README.md`。
- **建议**：在 `2d_hydro_project/docs/` 建立或规范化 `MASTER.md`（或强化现有的 `README.md`），明确按 Diátaxis 四元分类法为人类开发者与 AI Agent 建立结构化的阅读路径。

---

## 四、链接与路径审计

1. **相对链接完整性**：
   - 经自动化脚本遍历检测，`2d_hydro_project/docs/` 下的所有 Markdown 文档间相对链接、ADR 引用链接 **100% 有效**，无 404 断链。
2. **外部/宿主绝对路径发现**：
   - `plans/广西久隆_宁明_贵港_二维洪涝可视化实现计划.md` 中发现了 3 处包含 `D:/NWKYYSJZX/...` 的历史硬编码宿主绝对路径。按可移植性规范（静态门 `test_no_host_paths.py`），应予以提示或在规范化阶段替换为相对路径或环境变量说明。
3. **gitignored 资产链接**：
   - `flood_visualization/docs/optical_assets.md` 指向 `data/processed/assets/` 与 `data/external/`，属于有意排除入库的可再生大文件（已有注释说明，属设计范畴）。

---

## 五、后续动作计划（Normalize 与 Maintain）

1. **Step 2: normalize（规范化治理与索引补全）**：
   - 创建 `2d_hydro_project/docs/MASTER.md` 统一 SSOT 导航树，按 Diátaxis（教程、操作指南、参考手册、原理解释）清晰组织。
   - 更新 `2d_hydro_project/docs/README.md`，将 ADR-0019 至 ADR-0022 完整纳入索引。
   - 为缺失元数据的关键核心文档补全标准化 Frontmatter。
   - 修复 `plans/` 内历史路径与语法链接问题。
2. **Step 3: maintain（代码与方案变更同步）**：
   - 同步更新根级 `AGENTS.md`：修正 G15 质量门权威计数为 66-4-0（70 run），补充 ADR-0019~0022 索引，同步久隆双驱动生产主情景。
   - 扩充 `2d_hydro_project/docs/execution_status.md`：增补 Phase G（贵港水力河槽/排水治理）、Phase H（学术驱动 FAT/归因/概率）、久隆双驱动耦合（ADR-0019~0022）的正式执行状态章节。
   - 检查并同步 `RUNBOOK.md`，增加久隆双驱动与学术改进脚本操作指引。
