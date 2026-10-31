---
doc_type: explanation
status: active
topic: adr-0001
canonical: true
scope: project
---

# ADR-0001：工程结构与 Python 环境管理

- **状态**：已接受（Accepted）
- **日期**：2026-10-24
- **决策人**：软件/运行负责人（代行，待 RACI 确认后追认）

## 背景

终稿第十章规定了建议项目结构；本机环境（Windows 10、uv 0.11.15、阿里云镜像、pypi.org 不可达）要求统一的包管理方式。

## 决策

1. 工程落位 `2d_hydro_project/`，目录结构按终稿第十章执行：`configs/`、`data/{raw,interim,processed,catalog}`、`src/{acquisition,preprocessing,model,postprocessing,reporting}`、`tests/`、`scripts/`、`outputs/`、`logs/`、`docs/adr/`。
2. Python 环境统一用 **uv** 管理：`pyproject.toml` + `uv.lock` + `.python-version=3.12`（不用系统 PATH 的 3.13，避免漂移）。
3. 大文件目录（`data/raw|interim|processed`、`outputs`、`logs`）内容不入 git，目录以 `.gitkeep` 保留；凭证类文件（`.env`、token）一律不入库。
4. 阶段脚本放 `scripts/stage<N>/`，命名自解释；每脚本落盘机读结果（JSON/MD）到 `data/catalog/`。
5. 中文控制台运行 Python 前设 `PYTHONUTF8=1`（避免 cp936 编码错误）。

## 依据

- 终稿第十章（工程结构与复现目录）、第三章（合规）。
- 本机实测：uv 直连阿里云镜像可用；curl 对 HTTPS 报吊销检查错误，不用于可达性判定。

## 影响

- 后续所有代码在此结构内开发；结构变更须经新 ADR。
- `uv.lock` 已生成并随库保存，保证可复现安装。
