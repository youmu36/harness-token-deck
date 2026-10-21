# 参与开发

请先按 [README](README.md#本地开发) 配置依赖。使用虚拟环境和临时测试目录，避免让测试或探针读取真实账户、默认来源日志或正在使用的账本。

## 修改与验证

- 后端业务逻辑位于 `backend/src/htd/domain/` 和 `application/`，文件、数据库及桌面接口位于 `adapters/`；IPC 请求和响应由 `ipc/` 定义。
- 前端功能位于 `frontend/src/features/`，共享组件位于 `ui/`。模拟桥接使用合成数据。
- 为行为修改选择相关的 pytest / Vitest 用例，补足必要的边界回归。TypeScript 修改运行 `npm run typecheck`；Python 修改对受影响文件运行 Ruff 和 Pyright。
- 修改 IPC 后运行 `uv run python scripts/gen_contracts.py`，同步提交 `frontend/src/infra/generated/`。不要手工修改生成文件。
- 金额使用精确整数或十进制字符串。扫描、聚合和后台任务保持有界批次，避免新增无界查询或高频轮询。

可使用 `uv run python scripts/verify.py quick` 做综合诊断。原生桌面与安装器检查需要 Windows；`scripts/probes/` 是按问题选用的诊断工具，可能短暂显示窗口，不是日常检查的固定步骤。

## 提交前

运行 `uv run python scripts/check_publication.py --history` 检查拟公开文件及本地 Git 历史。扫描器只报告文件位置和规则，不输出疑似密钥的值；自动检查不能替代人工检查。

不要提交真实数据库、配置、对话日志、截图中的账户信息、访问令牌、证书或备份。`fixtures/` 中只接受可审阅的合成数据；三份旧库由 `scripts/make_legacy_fixtures.py` 生成。依赖改动需更新锁文件和相关第三方许可。

Pull Request 请说明具体问题、修改后的行为和实际运行的检查。默认保持现有版本号；发布版本由维护者统一调整。提交贡献表示同意按项目 [MIT 许可证](LICENSE) 分发相关贡献。
