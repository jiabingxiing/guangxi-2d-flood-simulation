#!/usr/bin/env python
"""Convenience root wrapper for 2D hydrodynamic rainfall-runoff preprocessor.

广西华南湿润红壤区二维地表水动力模拟：土壤下渗数据源与净雨转换算法（针对产流水量虚高）
Plan reference: plans/地表产流损失、土壤水文属性与初损网格化算法（针对产流水量虚高）计划.txt

Usage:
  uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case jiulong
  uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case guigang
"""
import sys
from pathlib import Path

# Add 2d_hydro_project to Python path
PROJ_DIR = Path(__file__).resolve().parent / "2d_hydro_project"
if str(PROJ_DIR) not in sys.path:
    sys.path.insert(0, str(PROJ_DIR))

from rainfall_runoff_preprocessor.run_preprocessor import main

if __name__ == "__main__":
    main()
