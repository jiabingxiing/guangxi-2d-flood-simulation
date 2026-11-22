#!/bin/bash
# Phase H 授权 run 队列（ADR-0016；计划 5.3；用户 2026-11-22 批准）。
# 串行执行（16 GB 内存单进程纪律）；每个 run 结束后检查 run.log 是否含
# "SIMULATION COMPLETED"，未完成即停线（落 FAIL/中断不带病续跑）。
#
# 推荐队列（zdown 顶托两档；T07 先验对照 r<0.5 已按预注册把 GR4J 线退回
# 三角情景线，如需追加 GR4J run 请取消注释对应行）：
#
# 用法（Git Bash，或 WSL；机时每 run ≤24 h）：
#   bash scripts/stage3/queue_phase_h_runs.sh
set -uo pipefail
P="$(cd "$(dirname "$0")/../.." && pwd)"
LISFLOOD="$P/models/lisflood-fp-8.1/LISFLOOD-FP-trunk/lisflood.exe"
export PYTHONUTF8=1

RUNS=(
  "$P/outputs/guigang/2024-09-yagi/scen_zdown_b_100m"
  "$P/outputs/guigang/2024-09-yagi/scen_zdown_a_100m"
  # "$P/outputs/guigang/2024-09-yagi/scen_gr4j_q_100m"      # 可选：见 T07 结论
  # "$P/outputs/ningming/2026-08-zitan/scen_gr4j_q6000_100m" # 可选：见 T07 结论
)

for d in "${RUNS[@]}"; do
  echo "[queue] $(date '+%F %T') 启动 $d"
  (cd "$d" && "$LISFLOOD" run.par > run.log 2>&1)
  if grep -q "SIMULATION COMPLETED" "$d/run.log"; then
    echo "[queue] 完成：$d"
  else
    echo "[queue][STOP] 未正常完成：$d（run.log 末行如下）"
    tail -5 "$d/run.log"
    exit 1
  fi
done
echo "[queue] 全部完成；下一步：质量门复算 g15_mass_recheck.py → S1 验证回填 → FAT 产品重跑增量"
