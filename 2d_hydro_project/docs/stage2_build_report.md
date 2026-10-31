---
doc_type: reference
status: active
topic: stage2-build
canonical: true
scope: project
---

# 阶段 2 报告：LISFLOOD-FP 8.1 Windows 构建（2026-10-24）

## 结论

**已完成**：LISFLOOD-FP 8.1.0 在本机以 MinGW-w64 GCC 构建成功，官方算例 T016c 冒烟通过（65 s，输出完整含 res.mass 水量账目）。模型清单已更新。**未完成**：终稿十一章 7 项数值基准（下一步）。

## 构建链路（含失败记录与规避）

| 步骤 | 结果 |
|---|---|
| WSL Ubuntu | **不可用**：虚拟机启动失败（HCS ERROR_NO_SYSTEM_RESOURCES），放弃该路线 |
| MSYS2（清华镜像） | **失败**：清华对 msys2 全路径 403；官方 repo.msys2.org（Cloudflare）直连与经代理均多次断流（≤10MB 即断） |
| w64devkit v2.10.1（GitHub Releases 经代理） | **成功**：67MB 下载约 4 秒；落位 `D:\w64devkit`，GCC 16.2.0 + GNU make |
| 源码补丁 | 见下节（1 处，最小化） |
| 构建 | `make lisflood CONFIG=config/local-mingw CXX=g++ -j8` → `lisflood.exe`（1.34MB，0 error） |
| 版本验证 | `lisflood.exe -v` → **LISFLOOD-FP version 8.1.0 (double)** |
| 冒烟测试 | `testing/T016c_Bridge_orifice_test` 复制至 `outputs/smoke/T016c_run1/` 运行 MT_SGC_chan.par：exit 0，65s，`results_SGC_chan/` 输出完整（res-*.wd、res.max、res.mass、res.dem 等，376KB） |

构建配置 `config/local-mingw`：`-std=c++14 -O3 -fopenmp -static -D_NUMERIC_MODE=1 -D_NETCDF=0`，LDLIBS 仅 `-lgomp`（静态链接，无 DLL 依赖）。

## 源码补丁登记（可追溯，终稿 5.1 要求）

**文件**：`models/lisflood-fp-8.1/LISFLOOD-FP-trunk/utility.cpp`（3 处编辑）
1. 头部新增 `#ifdef _WIN32 #include <malloc.h>`；
2. `memory_allocate`：新增 `#elif defined(_WIN32)` 分支 → `_aligned_malloc(size, 64)`；
3. `memory_free(void**)`：新增 `#elif defined(_WIN32)` 分支 → `_aligned_free(*memory)`。

**原因**：MinGW 无 `posix_memalign`；且**发现上游 bug**——gcc 分支的 `memory_free` 将释放误写为 `posix_memalign`（写穿栈变量、Linux 下静默泄漏对齐内存）。本平台分支采用 `_aligned_malloc/_aligned_free` 正确配对，规避该 bug；Linux 分支未改动（上游问题，已在此登记）。
**另发现未修复的上游问题**（仅登记）：`memory_free(void**, size)` 中 `total_allocated += size` 疑为符号反了（应为 `-=`），影响内存账目统计显示，不影响模拟。

## 已知限制

1. **`make test` 单元测试目标编译失败**（GCC 16 下 `test/test_dg2.cpp` 与 `swe/dg2/fields.h` 接口漂移，上游问题）；以官方算例实跑作为功能验证替代，单元测试修复列入待办（低优先）。
2. **`_NETCDF=0`**：当前仅 ASCII（Esri grid/.mass）输出；windep 内 netcdf 为 MSVC 构建不可用于 MinGW。如需 NetCDF 输出/动态降雨，后续装 MSYS2 mingw 版 netcdf（网络恢复后）再开启。
3. 许可：Zenodo 元数据 gpl-2.0 与官网 GPLv3 表述不一致，待查源码文件头回填（未改任何许可文件）。

## 复现命令

```bash
export PATH=/d/w64devkit/bin:$PATH
cd 2d_hydro_project/models/lisflood-fp-8.1/LISFLOOD-FP-trunk
make lisflood CONFIG=config/local-mingw CXX=g++ -j8
./lisflood.exe -v
```

## 下一步（终稿 十一章基准）

包内预处理工具可直接生成基准输入：`createLakeAtRestCones/Blocks`（静水）、`create1DDamBreak/createRadialDamBreak`（溃坝）、`checkDynamicRain`（雨量账目）——阶段 2 续：按 13.2 门禁（基准 E_mass ≤ 1e-4）逐项跑并出基准报告。
