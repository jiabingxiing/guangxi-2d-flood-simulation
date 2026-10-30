#!/bin/bash
# 队列2：等宁明全线段版结束（lisflood 进程消失）后，跑修复版久隆四情景
P="D:/NWKYYSJZX/2026年工作/1023 久隆 贵港 宁明 二维水动力模拟/2d_hydro_project"
export PYTHONUTF8=1
echo "[watcher2] 等待宁明全线段版（lisflood）结束..."
sleep 120
while tasklist 2>/dev/null | grep -qi "lisflood"; do sleep 60; done
echo "[watcher2] 启动修复版久隆四情景（海面仅钳边连负值）"
uv --directory "$P" run python scripts/stage3/jiulong_scenarios.py all
echo "[watcher2] 久隆修复版完成"
