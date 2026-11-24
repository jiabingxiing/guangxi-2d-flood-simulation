---
doc_type: reference
status: active
topic: master-index-pointer
canonical: false
scope: project
---

# 项目文档总纲导航（docs/MASTER.md）

本项目二维水动力模拟模型工程与主文档均位于 `2d_hydro_project/docs/` 目录下。

完整详细的 Diátaxis 文档全景导航请见：
👉 **[2d_hydro_project/docs/MASTER.md](../2d_hydro_project/docs/MASTER.md)**

---

## 核心单一事实来源（SSOT）快速跳转

1. **执行状态与剩余项（SSOT）**：
   - [2d_hydro_project/docs/execution_status.md](../2d_hydro_project/docs/execution_status.md)
2. **质量门全量验证（SSOT）**：
   - 权威文件：`2d_hydro_project/data/catalog/mass_recheck_g15.json`（**66 PASS / 4 FAIL-EXPLAINED / 0 FAIL，共 70 个 run**）
   - 检验脚本：`uv --directory 2d_hydro_project run python scripts/stage3/g15_mass_recheck.py`
3. **架构决策链（ADR-0001 至 ADR-0022）**：
   - [2d_hydro_project/docs/adr/](../2d_hydro_project/docs/adr/)
4. **工作区硬约束与导航**：
   - [AGENTS.md](../AGENTS.md)
