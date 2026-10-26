# 可选诊断探针

这些脚本仅在对应问题需要时使用，不是默认交付流程。历史阶段名称或旧报告不能证明当前版本通过，运行前核对脚本的场景和断言；日常开发与验证见根目录 [CONTRIBUTING.md](../../CONTRIBUTING.md)。

原生脚本通常需要先加载 `scripts/env.ps1` 并运行 `npm run build`，会短暂显示窗口/托盘。使用隔离的 HTD_DATA_DIR、来源和实例命名空间，结束后退出宿主。报告通常位于 `.verify/`；部分旧脚本输出截图到 docs/design/screens，截图仅为当次证据，不作为界面规格。

| 问题 | 入口（项目根运行） |
| --- | --- |
| WebView 桥、托盘、单实例、挂起或冻结框架 | `uv run python scripts/probes/probe_<webview\|tray\|single_instance\|suspend\|freeze>.py`，每次只选对应文件 |
| 隐藏/恢复、工作集与页面连续性 | `uv run python -m scripts.probes.accept_tray_memory` |
| 冷启动与托盘差异 | `uv run python -m scripts.probes.startup_memory --label <名称>`；隔离的 180 天合成输入，冷启动/静置/托盘/恢复，分记实际宿主与所属 WebView2 的 CPU、私有提交、工作集和进程角色，检查页面标记连续性，不强制 GC |
| 两小时趋势、午夜及托盘恢复 | `uv run python -m scripts.probes.accept_tray_trend` |
| 管理与统计组合流程 | `uv run python -m scripts.probes.accept_internal_refactor` |
| 用量/管理页面原生交互 | `uv run python -m scripts.probes.accept_usage_pages`，管理流程加 `--management` |
| 模型、设置与原生选择器 | `uv run python -m scripts.probes.accept_models_settings` |
| 历史扫描与重算 | `uv run python -m scripts.probes.accept_history_recalc` |
| 远程协议与凭据（合成站点） | `uv run python -m scripts.probes.accept_remote` |
| 来源只读观察 | `scripts/probes/observe_feedback.py`，时长由显式参数指定 |
| 同机等量宿主/WebView2资源样本 | `uv run python -m scripts.probes.data_refactor_runtime --label <名称> --seconds 3 --requests 20` |
| 普通统计/分析/钱包切页与小时明细内存 | `uv run python -m scripts.probes.pages_memory --label <名称> --seconds 6 --requests 80`；180 天合成历史、四来源新输入，分记宿主/WebView2、CPU、私有提交、工作集、DOM 和 IPC 次数 |
| 宿主多会话写入与大型分析缓存 | `uv run python -m scripts.probes.host_modules --label <名称> --requests 800 --seconds 6`；180 天/1024 模型、单会话与八会话各等量输入、分析首读与分页命中、钱包读取及托盘恢复，分记实际宿主/WebView2 资源和最终入库数；省略参数默认各 160 条、每场景 6 秒，显式时长最少 2 秒 |
| 拆解事务与缓存的 CPU 成本 | `uv run python -m scripts.probes.cpu_costs --label <名称>`；单/八会话各三次 12×128 请求及计费、监控 20,000 次构建/命中、分析 2,000 次构建/分页命中；只测进程 CPU 与缓存编码大小，不能替代原生宿主/WebView2 的资源对照 |
| 数据目录原生迁移与自动重启 | `uv run python -m scripts.probes.directory_restart`；合成待计费输入，独立定位/实例 |
| 存储查询有限样本 | `scripts/probes/measure_storage_update.py` |

真实账户探针需显式选择 real 模式及站点，所需资料由本地测试配置读取，不把密钥写入命令或报告。旧 auto 探测方式已废弃，不能据旧探针参数恢复产品自动识别。技术可行性探针不替代受影响的产品行为验证；资源结果只描述当次样本，不承诺固定启动速度、内存降幅或长期稳定性。

资源报告的 `processMetrics.host/webview2` 分别记录实际宿主与 WebView2；Windows 虚拟环境可能有轻量 Python 启动器，必须一并统计其运行宿主子进程。旧版仅统计根 PID 和 WebView2 的报告可能漏计真实后端，不能用于本体内存或整体进程总量结论；重新建立同版本同工作量基线后比较。

`cpuPeakOneCorePercent1s` 为约 100 ms 采样形成的一秒滚动窗口 CPU 峰值，按单核计；`cpuPeakMachinePercent1s` 再除以本机逻辑 CPU 数，不是任意瞬间峰值。累计 CPU、窗口峰值、稳态/峰值私有提交及工作集分别比较。资源采样单独运行，避免和测试、构建或另一个性能探针并行；前后重复并交错顺序，小幅变化不能直接解释为稳定收益。

需要排除运行时波动时，可显式以 `HTD_PROBE_BACKEND_ROOT` 和 `HTD_PROBE_WEB_ROOT` 指定隔离的来源代码/生产前端快照，默认仍使用工作区；快照、输入与结果分别记录。UiProbe 的 `host-memory` 动作可只读查看宿主私有提交、Python 对象块及 .NET 堆/回收次数；`collect=true` 仅用于隔离诊断，不进入产品生命周期，也不用于制造正式对照降幅。

`cpu_costs` 直接导入后端，对照旧源码时用 `PYTHONPATH=<后端快照根>;<仓库根>`，不能只设上述原生探针变量。`--without-monitor-triggers` 仅用于隔离旧专用触发器的成本，报告须保留该标记；当前实现已退休这些触发器，不据此选项声称新增收益。

`HTD_PROBE_HISTORY_MODELS` 可为历史夹具指定 0..5000 个轮转模型；默认 0 保留原单模型输入。前后对照须使用相同值，不能将模型数量变化当成实现改进。

`host_modules --history-models 128 --requests 32 --batch-size 4 --seconds 3` 可对照小批次调度：180 天历史保持 5,760 条，按指定模型数轮转；单/八会话各 32 条，每四条调用同一手动扫描入口。省略 `--batch-size` 仍一次扫描全部输入，默认历史模型数仍为 1,024。该参数只控制合成探针的输入方式，不延迟产品采集；不同批次/模型参数不可直接比较。统计输出包含最终 identity 数和 inbox 清空断言。
