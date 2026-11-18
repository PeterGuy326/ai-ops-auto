# 模型工具协议探测

`scripts/model_tool_probe.py` 是手动启用的 Chat Completions 兼容性探测，不运行 Easel
原生 Agent、不发布内容、不执行命令。它补充模型端的工具协议证据，不能替代产品端的恢复和预算测试。

每次最多发送两次非流式请求：第一轮要求模型调用 `lookup_receipt(ticket="probe")`；
验证工具名、调用 ID 和参数后，返回只存在于内存的随机回执；第二轮要求原样返回该回执。
回执不出现在第一轮提示中。猜答案、并行工具调用、非法参数、截断输出和多余调用均不算通过。
没有自动重试、真实平台工具或任意代码执行。

安装项目依赖后，在本机安全设置 `EASEL_PROBE_BASE_URL`（含 API 基础路径，例如 `/v1`）和
`EASEL_PROBE_API_KEY`。凭据不要放进命令行参数、提交、PR 或测试报告。HTTPS 和回环 HTTP
可用；禁止 URL 内嵌凭据、查询参数和重定向，不读取系统代理环境变量。

```bash
python scripts/model_tool_probe.py --live --model YOUR_MODEL
```

未传 `--live` 不发请求。退出码 0 表示本次协议探测通过，1 表示探测失败，2 表示配置错误。
请求使用 `max_tokens`，默认每轮 256，`--max-tokens` 可设为 1–1024；不支持该字段或工具协议的
模型会明确失败，不会暗中切换模型或重试。两轮共用 60 秒时限，统一定义在
`easel/timeouts.py`。客户端超时不能证明远端已经停止计算或计费。

标准输出为聚合 JSON：状态、已开始请求数、完整响应数、执行的内存工具次数、总耗时和 token
用量。不输出地址、Key、提示、回执、原始响应、错误正文或推理内容。只有每次已开始的请求都
返回合法用量时才累加；缺失或异常时为 `null`。用量是服务端报告值，不是独立账单核验。
`cost` 始终为 `null`：请求数和输出长度限制不等于货币预算，也不能证明服务免费。

当前只做单模型、单场景、单样本的协议探测；不提供统计成功率、模型排名、成本比较或跨后端
可靠性结论。报告不包含模型名和版本，比较时需另行记录配置与代码提交。自动化测试只使用
内存 HTTP fixture，不使用真实 Key，不发送付费请求；真实探测不加入 CI。

接口字段依据 [Chat Completions 官方参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，
第三方服务的兼容程度需以实际探测为准。
