# 可选诊断探针

这些脚本仅在对应问题需要时使用，不是默认交付流程。历史阶段名称或旧报告不能证明当前版本通过，运行前核对脚本的场景和断言；验证范围见[验证与交付](../../docs/design/verification.md)。

原生脚本通常需要先加载 `scripts/env.ps1` 并运行 `npm run build`，会短暂显示窗口/托盘。使用隔离的 HTU_DATA_DIR、来源和实例命名空间，结束后退出宿主。报告通常位于 `.verify/`；部分旧脚本输出截图到 docs/design/screens，截图仅为当次证据，不作为界面规格。

| 问题 | 入口（项目根运行） |
| --- | --- |
| WebView 桥、托盘、单实例、挂起或冻结框架 | `uv run python scripts/probes/probe_<webview\|tray\|single_instance\|suspend\|freeze>.py`，每次只选对应文件 |
| 隐藏/恢复、工作集与页面连续性 | `uv run python -m scripts.probes.accept_tray_memory` |
| 两小时趋势、午夜及托盘恢复 | `uv run python -m scripts.probes.accept_tray_trend` |
| 管理与统计组合流程 | `uv run python -m scripts.probes.accept_internal_refactor` |
| 用量/管理页面原生交互 | `uv run python -m scripts.probes.accept_usage_pages`，管理流程加 `--management` |
| 模型、设置与原生选择器 | `uv run python -m scripts.probes.accept_models_settings` |
| 历史扫描与重算 | `uv run python -m scripts.probes.accept_history_recalc` |
| 远程协议与凭据（合成站点） | `uv run python -m scripts.probes.accept_remote` |
| 来源只读观察 | `scripts/probes/observe_feedback.py`，时长由显式参数指定 |
| 存储查询有限样本 | `scripts/probes/measure_storage_update.py` |

真实账户探针需显式选择 real 模式及站点，所需资料由本地测试配置读取，不把密钥写入命令或报告。旧 auto 探测方式已废弃，不能据旧探针参数恢复产品自动识别。技术可行性探针不替代受影响的产品行为验证；资源结果只描述当次样本，不承诺固定启动速度、内存降幅或长期稳定性。
