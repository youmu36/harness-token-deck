# Harness Token Deck

Windows 本地 Token 用量、费用与钱包工作台，支持 Claude Code、Codex、Zcode 和 Deepseek Harness（DSH）。当前版本 **0.4.2**，采用 [MIT 许可证](LICENSE)。

从本机 Harness 的用量记录中汇总输入、缓存和输出 Token，按自定义价格组估算费用，并管理供应商钱包。数据保存在本机，适合需要对照多个 Harness 和供应商账目的个人用户。

## 功能

- 按来源、模型和时间查看用量、费用、缓存命中率与趋势。
- 监控近期缓存、多个会话的命中率和模型输出速度；静默十分钟后冻结显示。
- 配置价格组、模型费率及供应商关联，保存价格历史并支持重算。
- 管理本地钱包；可选查询 New API、sub2api 或 DeepSeek 官方的公开余额接口。
- 支持历史数据保留、备份、旧版导入、托盘运行与开机启动。

费用取决于来源记录的完整性与自行配置的价格。TPS 包含思考输出，区分来源计时、Codex 生成估算与分钟粗估。Codex 优先使用思考/回答的流式起点，缺失时回退到包含首字等待的请求耗时；Zcode 使用原生首 Token 计时，DSH 使用流式增量时间。交互归属不明或不足 100 毫秒的计时样本不更新速度，Token 和费用仍正常统计。缺少计时或发生网络缓冲时无法还原真实生成速度。账户余额、API Key 余额和套餐额度并非同一口径，请按实际站点类型配置。

## 安装与使用

桌面应用面向 **Windows 10/11 x64**，需要 Microsoft Edge WebView2 Runtime。安装器在缺少运行时时会引导下载微软官方运行时。当前安装包未做代码签名。

安装包包含应用及其 Python 依赖，日常使用无需安装 Python、Node.js 或开发工具，也无需 GitHub 或远程账户登录。

仓库保存源码，不保存安装包；已发布的安装包请在 [Releases 页面](https://github.com/youmu36/harness-token-deck/releases/latest)获取，也可按下文自行构建。

首次启动后，在“设置 → 采集来源”中检查所用 Harness 的路径，再在价格管理中配置模型费率。使用钱包时先创建供应商并关联价格组；远程余额功能需要自行配置站点类型和凭据。

默认来源位于用户主目录内的 `.claude`、`.codex`、`.zcode` 和 `.dsh`。仅读取支持的用量记录，不向来源写入内容。应用数据默认位于 `%LOCALAPPDATA%\HarnessTokenDeck`；通过 `HTD_DATA_DIR` 可指定独立目录。备份可能包含账户标识、站点和个人用量，应视为私人文件。

## 隐私与联网

应用没有云同步或多用户服务。远程余额仅在用户配置后访问所选站点的公开接口；安装 WebView2 时会访问微软的下载服务。

远程密钥通过 Windows 凭据管理器保存，数据库保存凭据引用及账本状态。来源日志、旧版数据库、备份和诊断材料仍可能包含敏感信息，请勿上传到 GitHub。报告问题前请阅读 [安全与隐私说明](SECURITY.md)。

## 本地开发

需要 Python **3.12**、[uv](https://docs.astral.sh/uv/) 和 Node.js **22.12+**（推荐 24 LTS）。桌面宿主与安装器需在 Windows x64 上运行。前端使用 Vue 3 / TypeScript / Vite，后端使用 Python / SQLite，通过 pywebview 连接。

在项目根目录运行：

```powershell
uv sync --locked
npm ci
npm run dev
```

开发服务器使用合成数据，可在浏览器中预览，不连接个人账户。启动桌面程序前先构建前端：

```powershell
npm run build
uv run python -m htd
```

常用检查：

```powershell
uv run python -m pytest -m "not slow and not desktop"
npm test
npm run typecheck
uv run python scripts/check_publication.py --history
```

修改 IPC 后运行 `uv run python scripts/gen_contracts.py` 并提交生成结果。参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 构建安装包

安装 [NSIS 3](https://nsis.sourceforge.io/Download)，确保 `makensis` 在 PATH 中，或通过 `HTD_MAKENSIS` 指定可执行文件。随后运行：

```powershell
uv run python scripts/build_release.py
```

脚本运行前端检查、Vite 和 PyInstaller 构建，使用临时空库启动验证，再生成 `release/Harness Token Deck_0.4.2_x64-setup.exe`。若已完成本次改动的定向检查，可使用 `--reuse-checks`。版本由根目录 `VERSION` 统一管理。

## 源码与许可证

`backend/` 为后端及测试，`frontend/` 为界面及测试，`fixtures/` 为合成样本，`scripts/` 为构建和验证工具，`packaging/` 为安装器资源与第三方许可。

`docs/` 是维护者的私人档案，不属于公开仓库，构建与使用不依赖它。安装目录、真实数据库、日志、备份、密钥、虚拟环境和构建产物均应保留在 Git 之外。仅三份可重复生成的旧版合成数据库作为测试样本纳入仓库。

项目代码按 [MIT](LICENSE) 分发；第三方组件保留各自许可证，详见 [第三方通知](packaging/THIRD_PARTY_NOTICES.md) 和 [许可证来源](packaging/licenses/README.md)。
