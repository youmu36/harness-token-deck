# 合成旧库

由 `scripts/make_legacy_fixtures.py` 和 `scripts/legacy_fixtures/` 中的 SQL 与配置构造，全部为合成数据。固定快照时刻 `2026-09-10T00:00:00.000Z`，不读取真实旧库或旧源码。发布检查会核对文件哈希，测试会在临时目录重新生成并比较。

加载 `scripts/env.ps1` 后运行 `uv run python scripts/make_legacy_fixtures.py`。可用 `--output-dir <临时目录>` 独立生成；重复运行三份库及 `expected.json` 的字节相同。每份库采用 DELETE journal，交付时没有 WAL/SHM 文件；迁移在线快照的 WAL 行为由后续阶段单独测试。

| 文件 | 场景 |
| --- | --- |
| `v022.sqlite3` | user_version=6，无元数据/扩展/DSH 选择；缺省偏好、模型池、版本 label 与激活；9 条账本、2 个来源检查点 |
| `v023.sqlite3` | 撤回的 user_version=7；versionVendors 同组不同版本关联不同供应商，主 price_selections 混入 DSH；10 条账本、3 个检查点 |
| `v030.sqlite3` | user_version=6、storage_generation=6；groupVendors 优先、倍率、价格修订/激活、峰谷与阶梯规则；多次充值/校准、负余额、缺 createdAt；11 条账本、1 个重算批次及明细、保留清理身份、3 条 inbox、3 个检查点 |

`expected.json` 按数据库文件名分别列出 sha256、各表行数、账本总额、来源/UTC 日期总额和供应商旧余额。金额均为 pico 整数字符串；余额由生成器的独立旧算法计算：最后校准时刻排除等号，创建时刻包含等号，关联取当前组归属。

价格 JSON 是迁移的配置来源，派生表只作核对。样本补齐稳定主键及所需审计列；合成 SQL 不作为真实旧库未记载列的证据，真实数据兼容性需要单独验证。

来源路径固定在 `C:\HTU-Fixtures\`，仅用于模拟旧路径，不要求本机存在。Codex checkpoint 的 offset/anchor 对应 `fixtures/codex/desktop.jsonl` 的原始字节；Zcode checkpoint 含 pending row，DSH 使用旧 context 字段。后续首次扫描测试可在临时目录物化来源并映射路径。

Zcode checkpoint 使用 `row_id=4, tail_id="4", pending=[4]`，pending 不超过游标。迁移测试在临时来源库物化 id="4" 的尾行，验证 pending 保留且首次扫描没有 source_rewritten。
