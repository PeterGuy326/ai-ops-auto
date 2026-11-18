# OpenClaw 循环保护诊断

运行 `python -m easel doctor`，在 OpenClaw 对话模式下查看保存的
`tools.loopDetection.enabled` 及 Agent 覆盖开关计数。
读取路径与 Easel 其他配置检查相同，支持 `EASEL_OPENCLAW_STATE_DIR`。
诊断只读，不启用保护、不修改阈值，也不改变环境检查退出码。

开关分为显式开启、显式关闭、未设置、格式异常。字符串 `"false"`、数字和 `null`
不是布尔开关。未设置的值需要原生版本解释继承和默认值，不能直接视为关闭。
旧布局 `agents.list` 和新布局 `agents.entries` 分开计数；同时存在会提示核对，
不会猜测运行中的网关选了哪个布局。输出不包含 Agent 名称、凭证或原始错误内容。

这不是原生 schema 校验或运行时能力探测。保存为开启不证明当前版本支持、网关已加载，
也不证明每个检测器都启用。请结合所装 OpenClaw 的配置校验和网关日志确认实际行为。
无法解析的配置（包括此读取器不支持的 JSON5）明确显示未知。
API 直连和其他原生后端不显示此项；它们不受 OpenClaw 配置控制。

循环检测也不是严格的每任务调用预算。预算需要实际工具执行前的拦截、任务作用域和并发计数，
不能拿 HTTP 请求次数代替工具调用数。

版本参考：[2026.6.11 配置布局](https://github.com/openclaw/openclaw/blob/v2026.6.11/docs/tools/loop-detection.md)
和[当前原生文档](https://docs.openclaw.ai/tools/loop-detection)。
