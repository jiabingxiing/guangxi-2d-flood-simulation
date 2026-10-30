#!/bin/bash
# 队列脚本：等久隆四情景链完成后，自动跑宁明全线段版（blanket FREE）回算+验证
P="D:/NWKYYSJZX/2026年工作/1023 久隆 贵港 宁明 二维水动力模拟/2d_hydro_project"
SUMMARY="$P/data/catalog/jiulong_scenarios_summary.json"
export PYTHONUTF8=1
echo "[watcher] 等待久隆链完成（$SUMMARY）..."
while [ ! -f "$SUMMARY" ]; do sleep 60; done
# 再等 lisflood 进程退出（防摘要已写但进程收尾）
sleep 30
echo "[watcher] 久隆完成，启动宁明全线段版"
cd "$P"
uv --directory "$P" run python scripts/stage3/ningming_scenario.py prep --res 100 --qpeak 4000
rm -rf "$P/outputs/ningming/2026-08-zitan/scen_q4000_100m/results"
uv --directory "$P" run python scripts/stage3/ningming_scenario.py run --res 100 --qpeak 4000
uv --directory "$P" run python scripts/stage3/ningming_scenario.py verify --res 100 --qpeak 4000
echo "[watcher] 宁明全线段版完成"
