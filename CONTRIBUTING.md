# 参与开发

开发环境与构建方式见下文。使用虚拟环境和临时测试目录，避免让测试或探针读取真实账户、默认来源日志或正在使用的账本。

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

修改 IPC 后运行 `uv run python scripts/gen_contracts.py` 并提交生成结果。

## 构建安装包

安装 [NSIS 3](https://nsis.sourceforge.io/Download)，确保 `makensis` 在 PATH 中，或通过 `HTD_MAKENSIS` 指定可执行文件。随后运行：

```powershell
uv run python scripts/build_release.py
```

脚本运行前端检查、Vite 和 PyInstaller 构建，使用临时空库启动验证，再生成 `release/Harness Token Deck_<VERSION>_x64-setup.exe`。若已完成本次改动的定向检查，可使用 `--reuse-checks`。版本由根目录 `VERSION` 统一管理。

## 一键发布到 GitHub

准备根目录 VERSION 和 CHANGELOG 中对应版本的待发布说明。首次安装 [GitHub CLI](https://cli.github.com/)，执行 `gh auth login` 与 `gh auth setup-git`；令牌由 GitHub CLI 管理，不写入仓库。gh 不在 PATH 时可用 HTD_GH 指定可执行文件。

在仓库默认分支执行：

```powershell
.\scripts\publish_release.ps1
```

也可直接运行 `uv run python -m scripts.publish_release`。脚本采用 origin 指向的 GitHub 仓库，执行公开文件与历史检查、构建和隔离启动验证，将当前非忽略文件提交并推送，等待该提交的 GitHub Checks 通过，再创建版本标签、上传安装包/源码 ZIP/构建清单/SHA256SUMS.txt。草稿附件的大小与 SHA-256 全部核对后才公开 Release 并设为最新，随后归档更新说明并提交。

- `--dry-run` 只预检和显示目标，不构建、提交、推送或创建 Release。
- 本次相关检查已完成时可加 `--reuse-checks`，复用检查后只构建与启动验证一次。后端改动仍需先运行相关用例。
- 网络中断可重跑；脚本可续传未公开草稿及发布后的说明归档，不覆盖已发布版本，不移动已有版本标签，不强制推送。新代码必须提升版本后再发布。
- 远程分支领先、冲突、检查失败或附件核对失败时停止，保留本地工作；先处理错误再重跑。

## 修改与验证

- 后端业务逻辑位于 `backend/src/htd/domain/` 和 `application/`，文件、数据库及桌面接口位于 `adapters/`；IPC 请求和响应由 `ipc/` 定义。
- 前端功能位于 `frontend/src/features/`，共享组件位于 `ui/`。模拟桥接使用合成数据。
- 为行为修改选择相关的 pytest / Vitest 用例，补足必要的边界回归。TypeScript 修改运行 `npm run typecheck`；Python 修改对受影响文件运行 Ruff 和 Pyright。
- 修改 IPC 后运行 `uv run python scripts/gen_contracts.py`，同步提交 `frontend/src/infra/generated/`。不要手工修改生成文件。
- 金额使用精确整数或十进制字符串。扫描、聚合和后台任务保持有界批次，避免新增无界查询或高频轮询。

可使用 `uv run python scripts/verify.py quick` 做综合诊断。原生桌面与安装器检查需要 Windows；`scripts/probes/` 是按问题选用的诊断工具，可能短暂显示窗口，不是日常检查的固定步骤。

## 版本更新说明

根目录 [CHANGELOG.md](CHANGELOG.md) 只维护当前尚未发布或本次待发布的变更，按用户能观察到的功能、修复和必要升级说明组织，不复制内部排错日志或未完成计划。版本号和发布日期以实际发布为准；日常修改默认不调整版本号。

发布时把本期说明归档到 `.github/release-notes/archive/版本号.md`，补充发布日期与 GitHub Release 链接，在[归档索引](.github/release-notes/archive/README.md)增加入口，再重写当前说明。历史正文保持在归档中，避免每轮重复带入上下文。接手 AI 默认按当前需求和现行规格工作，仅在编写发行说明时读当前 CHANGELOG，追查特定版本时才读相应归档。

GitHub 仓库的 About 简介独立于 README，文案维护在 [.github/description.txt](.github/description.txt)，发布时同步至仓库设置。

## 提交前

运行 `uv run python scripts/check_publication.py --history` 检查拟公开文件及本地 Git 历史。扫描器只报告文件位置和规则，不输出疑似密钥的值；自动检查不能替代人工检查。

不要提交真实数据库、配置、对话日志、截图中的账户信息、访问令牌、证书或备份。`fixtures/` 中只接受可审阅的合成数据；三份旧库由 `scripts/make_legacy_fixtures.py` 生成。依赖改动需更新锁文件和相关第三方许可。

Pull Request 请说明具体问题、修改后的行为和实际运行的检查。默认保持现有版本号；发布版本由维护者统一调整。提交贡献表示同意按项目 [MIT 许可证](LICENSE) 分发相关贡献。
