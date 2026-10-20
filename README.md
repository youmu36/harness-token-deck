# Harness Token Usage

Windows 本地 token 用量与钱包账本，支持 Claude Code、Codex、Zcode、Deepseek Harness。当前版本 **0.4.2**。

使用见[功能手册](docs/manual.md)，维护从[文档入口](docs/README.md)按任务定位，不需通读文档库。

## 开发

在项目根目录加载环境并启动预览：

```powershell
. ./scripts/env.ps1
uv sync --locked
npm ci
npm run dev
```

Git Bash 使用 `source scripts/env.sh`。Python 3.12 环境位于 `D:\Tools for agent\htu-venv`；浏览器预览使用隔离的模拟数据。

`npm run build` 生成桌面静态页面，随后 `uv run python -m htu` 启动桌面软件。测试按改动选择，规则见[验证与交付](docs/design/verification.md)。

## 安装包与维护工具

- 发布：相关检查通过后运行 `uv run python scripts/build_release.py --reuse-checks`，复用检查并只构建一次；流程见[打包设计](docs/design/packaging.md)。版本以 `VERSION` 为准，仅按本次要求修改。
- 安装包：[Windows x64 0.4.2](<release/Harness Token Usage_0.4.2_x64-setup.exe>)，构建信息：[release-0.4.2.json](release/release-0.4.2.json)。当前未签名，项目许可证待定。
- 桌面命令、数据目录与调试选项见[桌面宿主](docs/spec/desktop.md)；无窗口入口与契约生成见[IPC](docs/design/ipc.md)。
- Codex 来源核对见[只读审计](docs/spec/source-codex.md#只读审计)。

项目使用本地 Git，不自动提交或推送。反馈与进度的维护约定见 [AGENTS.md](AGENTS.md)。
