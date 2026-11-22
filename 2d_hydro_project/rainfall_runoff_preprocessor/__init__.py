"""Rainfall-Runoff Preprocessor for 2D Hydrodynamic Modeling.

广西华南湿润红壤区二维地表水动力模拟：土壤下渗数据源与净雨转换算法（解决产流水量虚高）。
Provides Green-Ampt, SCS-CN, and Horton infiltration preprocessors,
along with grid-based soil hydraulic parameters, landcover sub-grid fraction weighting,
antecedent moisture tracking, and mass balance validation.
"""

__version__ = "0.1.0"
