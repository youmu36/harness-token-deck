# New API 合成响应

按 [远程余额规格](../../../docs/spec/remote-balance.md) 的公开协议字段构造，全部为合成数据，未访问真实站点，也未录制测试账户。参考 [New API 文档](https://docs.newapi.pro/) 的 API 令牌用量、用户信息、系统状态与 OpenAI 兼容账单接口。

`token.json` 与 `user-self.json` 的剩余配额均为 987655；`status.json` 给出每 USD 500000 配额单位，换算后为 1,975,310,000,000 pico。`unlimited.json` 无金额；`subscription.json` 减 `usage.json` 的百分之一为 8.8905 个站点展示货币单位，兼容账单模式始终标记币种不确定。

样本只保留余额相关字段，无令牌、认证头或完整真实响应。失败、重定向、超大流和网络异常由 respx 测试动态构造。

`token-boolean.json` / `unlimited-boolean.json` 为布尔 `code=true` 兼容分支的合成样例，与真实站点观察到的状态类型一致；金额、配额和其他值仍为合成数据。不限额的零配额不等于账户余额为零，兼容账单中的大额占位值也不作为实际账户余额。

`sub2api-usage.json` 为 `/v1/usage` 的合成样例：明确 USD 的 balance 换算为 1,234,567,890,123 pico，向零截断；故意令 remaining 与 balance 不同，验证账户余额只取 balance。未录制真实账户数据。
