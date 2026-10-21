# 合成测试数据

本目录所有样例均为按规格构造的合成数据（手写来源样例或测试录制响应），不包含真实会话、提示词、凭据或用户账本。禁止将真实会话内容加入版本控制。

- `codex/desktop.jsonl`：C-01～C-03 的基础请求，模型 gpt-6-astra，请求时间 2026-09-01T00:00:01.000Z。半行追加、副本与坏行由后续测试在临时目录构造（C-02/C-03/C-05）。
- `codex/repeated-snapshots.jsonl`：C-04，同一请求的重复累计快照。
- `codex/source-cases.json`：来源接受/拒绝矩阵。
- `codex/fork-subagent.jsonl`：C-14，子会话首条身份与父会话历史 meta。
- `zcode/usage.json`、`terminal-cases.json`：基础 token 与 C-10 六行矩阵。retry 表示第二次 attempt，其数据库 status 仍为 completed。
- `zcode/schema.sql`：基础列；`schema-native.sql`：带可选原生 token 列。测试在临时库执行其中一版。
- `dsh/session.jsonl`：C-12 两条请求。C-13 的第二条未缓存输入 200 与多帧压缩由对应测试派生。
- `format/money.json`：跨语言金额显示向量，包含 FB-04 截断、半单位舍入、负零、千分位与超过 JS 安全整数的 pico 值；金额输入以字符串存储。
- `ipc/responses.json`：由 `backend/tests/contracts/test_recorded_contracts.py` 在合成临时账本上录制已登记命令的请求/成功响应、校验/冲突错误及缓冲事件；运行相关 UUID、路径和指纹归一化，供前端 Ajv 契约测试使用。测试环境隔离本机数据与来源，凭据为合成字符串并在录制结果中脱敏。
- `legacy/`：三份合成旧库及按库索引的 `expected.json`；用 `scripts/make_legacy_fixtures.py` 重建，固定时间/ID，精确旧余额和版本差异见 [说明](legacy/README.md)。

JSON/JSONL 可解析性与 SQLite 两版 schema 由 `backend/tests/test_fixtures.py` 检查；来源解析和计费行为由相关单元、集成测试验证。
