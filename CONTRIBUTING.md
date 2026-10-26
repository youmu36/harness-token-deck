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

## 隔离的本地 CI

日常修改先按[修改与验证](#修改与验证)选择相关检查，通过后停止扩展检查；需要完整检查时才使用本节入口。纯文档、需求讨论、架构规划及仓库约定调整只做相关文档检查，无需生成安装包。仅在完成本轮 Feedback 对应的功能开发、修复或新的开发内容并通过相关检查后，于开发交付结尾按[构建安装包](#构建安装包)生成与当前源码匹配的新安装包，供本地实机测试；中间讨论和文档修改不逐次打包，用户明确要求测试包时按其要求执行。已有本次相关检查结果时使用 `--reuse-checks`。后续正式发布可复用匹配的已验证安装包，直接走[一键发布](#一键发布到-github)，不再串联测试和构建。

普通分支、标签和更新说明的 `push` 不触发 GitHub Actions。远端仅在 Pull Request 或 Actions 页手动运行 Checks 时检查，发布脚本不等待远端测试。

需要与远端相同的一组检查时，在 Windows x64、Node.js 24 环境执行：

```powershell
.\scripts\local_ci.ps1
```

脚本把当前拟提交源码复制到 `.verify/local-ci/` 下的临时 Git 克隆，包含已暂存、未暂存和未跟踪的非忽略文件，并保留删除；不复制忽略的私人文档、运行数据、依赖和产物。副本使用独立的 Python 3.12 虚拟环境、`uv sync --locked` 和 `npm ci`，在隔离数据目录中依次运行公开文件/历史扫描、非 slow/desktop 后端测试、前端类型检查、前端测试和 Vite 构建。检查清单与 GitHub 共用 `scripts/verify.py ci`；临时目录退出时清理，结果保存在 `.verify/local-ci/last.json`。

相同源码指纹及本机 Python/Node 环境的成功结果自动复用；`--force` 强制重跑。失败或源码在验证期间变动不会产生可复用的通过结果。此方式模拟 Windows CI 的依赖安装和检查步骤，使用本机 Windows，不是 GitHub 托管镜像或虚拟机。

## 构建安装包

安装 [NSIS 3](https://nsis.sourceforge.io/Download)，确保 `makensis` 在 PATH 中，或通过 `HTD_MAKENSIS` 指定可执行文件。随后运行：

```powershell
uv run python scripts/build_release.py
```

脚本运行前端检查、Vite 和 PyInstaller 构建，使用临时空库启动验证，再生成 `release/Harness Token Deck_<VERSION>_x64-setup.exe`。若已完成本次改动的定向检查，可使用 `--reuse-checks`。Git 工作区的构建清单记录源码指纹和安装包 SHA-256，供发布时复用；构建期间源码变动则停止。解压的源码 ZIP 仍可打包，但没有 Git 指纹的产物不自动复用。版本由根目录 `VERSION` 统一管理。

## 一键发布到 GitHub

准备根目录 VERSION 和 CHANGELOG 中对应版本的待发布说明。首次安装 [GitHub CLI](https://cli.github.com/)，执行 `gh auth login` 与 `gh auth setup-git`；令牌由 GitHub CLI 管理，不写入仓库。gh 不在 PATH 时可用 HTD_GH 指定可执行文件。

在仓库默认分支执行：

```powershell
.\scripts\publish_release.ps1
```

也可双击 [publish_release.cmd](scripts/publish_release.cmd)，在菜单中选择“仅检查”或“正式发布”。窗口显示版本、origin 与网络方式，运行日志保存在 `.verify/publish/`，完成或失败后保留窗口。自动网络模式先探测本机 `127.0.0.1:7890`；端口无监听时直连。命令行可明确选择网络：

```powershell
.\scripts\publish_release.cmd -Action Preflight -Proxy 7890
.\scripts\publish_release.cmd -Action Publish -Proxy direct
```

`-NoPause` 适用于自动调用；原有 PowerShell/Python 入口可用 `--proxy 7890`、`--proxy http://127.0.0.1:7890` 或 `--proxy direct`。网络设置只作用于本次进程及子进程，不修改系统代理和全局 Git 设置。保留已有 GitHub CLI 登录；找不到 gh 时，按报错安装 GitHub CLI，或用 `HTD_GH` 指定路径。

也可直接运行 `uv run python -m scripts.publish_release`。脚本采用 origin 指向的 GitHub 仓库，执行公开文件与历史检查；若当前源码指纹、安装包大小与 SHA-256、检查状态和启动结果均匹配现有构建清单，直接复用安装包，不重复测试或构建。否则先运行隔离本地 CI（相同源码已通过时自动复用结果），再构建与隔离启动验证一次。之后将当前非忽略文件提交并推送，创建版本标签、上传安装包/源码 ZIP/构建清单/SHA256SUMS.txt。草稿附件的大小与 SHA-256 全部核对后才公开 Release 并设为最新，随后归档更新说明并提交。

- `--dry-run` 只预检和显示目标，不构建、提交、推送或创建 Release。
- 本次相关检查已完成时可加 `--reuse-checks`，由调用者确认已验证当前改动，跳过本地 CI；没有匹配安装包时只构建与启动验证一次。后端改动仍需先运行相关用例。
- 是否复用根据当前文件内容判断；工作区干净不等于已验证，未提交改动也可先验证后提交。旧构建清单缺少源码指纹或安装包摘要时，需要先重新构建，之后可自动复用。
- 只修改发布入口、说明或测试，需保持已经验收的原安装包时，可显式加 `--build-source-archive <原构建公开源码.zip>`。先重算 ZIP 全量指纹并与原构建清单核对，再逐文件比较当前源码；任何应用、依赖、打包输入或未列入发布文件白名单的变化都会停止，不能借此跳过实际代码改动的构建。清单保留原 `source_fingerprint`，另记 `publication_source_fingerprint`、原源码 ZIP 摘要及仅发布文件差异。`--dry-run` 同样执行这组核对；源码 ZIP 自带当前发布工具。
- 网络中断可重跑；脚本可续传未公开草稿及发布后的说明归档，不覆盖已发布版本，不移动已有版本标签，不强制推送。新代码必须提升版本后再发布。
- 远程分支领先、冲突、本地检查失败、构建后源码变动或附件核对失败时停止，保留本地工作；先处理错误再重跑。仓库若另行配置了必须通过远端检查才能推送的分支规则，仍需由维护者调整该规则或通过 PR 合入。

## 修改与验证

- 后端业务逻辑位于 `backend/src/htd/domain/` 和 `application/`，文件、数据库及桌面接口位于 `adapters/`；IPC 请求和响应由 `ipc/` 定义。
- 前端功能位于 `frontend/src/features/`，共享组件位于 `ui/`。模拟桥接使用合成数据。
- 为行为修改选择相关的 pytest / Vitest 用例，补足必要的边界回归。TypeScript 修改运行 `npm run typecheck`；Python 修改对受影响文件运行 Ruff 和 Pyright。
- 修改 IPC 后运行 `uv run python scripts/gen_contracts.py`，同步提交 `frontend/src/infra/generated/`。不要手工修改生成文件。
- 金额使用精确整数或十进制字符串。扫描、聚合和后台任务保持有界批次，避免新增无界查询或高频轮询。

可使用 `uv run python scripts/verify.py quick` 做综合诊断。原生桌面与安装器检查需要 Windows；`scripts/probes/` 是按问题选用的诊断工具，可能短暂显示窗口，不是日常检查的固定步骤。

## 版本更新说明

根目录 [CHANGELOG.md](CHANGELOG.md) 只维护当前尚未发布或本次待发布的变更，按用户能观察到的功能、修复和必要升级说明组织，不复制内部排错日志或未完成计划。版本号和发布日期以实际发布为准；日常修改默认不调整版本号。

`CHANGELOG.md`、GitHub Release 正文及归档的读者都是软件用户。只写用户可感知的功能变化、问题修复、兼容性与升级事项；不要写一键发布脚本、本地测试或 CI、开发环境、构建工具、仓库维护等仅服务本机开发或项目维护的内容。这些内容归入本文件相应开发章节，不作为版本特性发布。

逐条确认更新是否影响用户的使用、安装、数据或升级；仅有开发流程调整时，不为凑条目新增用户更新说明。工程调整若确实改变用户体验，只描述最终行为与必要操作，例如安装耗时减少或升级兼容性改善，不展开内部测试、脚本和发布步骤。

发布时把本期说明归档到 `.github/release-notes/archive/版本号.md`，补充发布日期与 GitHub Release 链接，在[归档索引](.github/release-notes/archive/README.md)增加入口，再重写当前说明。历史正文保持在归档中，避免每轮重复带入上下文。接手 AI 默认按当前需求和现行规格工作，仅在编写发行说明时读当前 CHANGELOG，追查特定版本时才读相应归档。

GitHub 仓库的 About 简介独立于 README，文案维护在 [.github/description.txt](.github/description.txt)，发布时同步至仓库设置。

## 提交前

运行 `uv run python scripts/check_publication.py --history` 检查拟公开文件及本地 Git 历史。扫描器只报告文件位置和规则，不输出疑似密钥的值；自动检查不能替代人工检查。

不要提交真实数据库、配置、对话日志、截图中的账户信息、访问令牌、证书或备份。`fixtures/` 中只接受可审阅的合成数据；三份旧库由 `scripts/make_legacy_fixtures.py` 生成。依赖改动需更新锁文件和相关第三方许可。

Pull Request 请说明具体问题、修改后的行为和实际运行的检查。默认保持现有版本号；发布版本由维护者统一调整。提交贡献表示同意按项目 [MIT 许可证](LICENSE) 分发相关贡献。
