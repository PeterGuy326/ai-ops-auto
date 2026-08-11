# Publisher Plugin SDK v1

Publisher Plugin SDK 把平台执行层从 core 中解耦，但不会把第三方 Python 代码变成沙箱。插件与
`ai-ops` 同进程运行，能读取进程环境；调用 `login` / `publish` / `health_check` /
`collect_metrics` 时还会收到相应账号数据。因此，`PUBLISHER_PLUGIN_ALLOWLIST` 是明确的本地
代码执行授权，不能用来试跑不可信包。

## 安全装配流程

```text
installed metadata
      |
      | ai-ops plugins list       (never imports plugin code)
      v
distribution:entry-point allowlist
      |
      | ai-ops plugins doctor     (loads selected trusted code)
      v
API/manifest/factory/identity validation
      |
      v
deterministic Publisher registry
```

- 默认 allowlist 为空，第三方 entry point 的 `load()` 调用次数为零。
- selector 必须精确写成规范化的 `distribution-name:entry-point-name`；裸名称与 `*` 被拒绝。
- entry-point group 固定为 `ai_ops.publishers.v1`，manifest 的 `api_version` 必须等于 `1`。
- 同一平台可以有多个实现；每个第三方 `(platform, publisher_kind)` 必须唯一，且插件必须使用
  不与 core `PublisherKind` 重名的自有 kind。
- 相同优先级按稳定 registration id 排序，安装时返回的 entry-point 顺序不会改变真实写路径。
- 已启用插件缺失、重名或校验失败时，控制面仍可启动并运行只读诊断，但 Publisher routing
  会 fail closed，不会静默换一条写路径。
- registry 每次构造 Publisher 都重新核验 platform、kind、metrics 能力及 exact renderer
  descriptor，防止状态型 factory 绕过首次检查。
- runtime 在调用前冻结插件身份；插件随后修改实例属性，或从异步方法抛出 `SystemExit`，都只会
  形成当前调用的脱敏失败，不会让它逃逸并终止 API/worker。
- metrics 只接受 core 定义的四个规范化计数器；插件返回的 `raw`、自定义字段和异常文本不会进入
  持久化快照、任务回执或公开诊断。

顶层 `ai-ops doctor` 仍只读取包元数据，不执行第三方 import。在下面两条诊断命令中，
`plugins list` 只读 metadata，只有 `plugins doctor` 会加载 allowlist 中的代码：

```bash
ai-ops plugins list --json
ai-ops plugins doctor --json
```

API/worker 启动并装配 Publisher registry 时也会加载和重新校验已启用插件；上述“只有”
限定于诊断命令之间的对比，不代表服务进程不会执行已授权代码。

## 插件包最小结构

插件自己的 `pyproject.toml`：

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "acme-ai-ops"
version = "1.2.3"
requires-python = ">=3.11"
dependencies = ["ai-ops-auto>=0.1,<0.2"]

[project.entry-points."ai_ops.publishers.v1"]
"acme.zhihu" = "acme_ai_ops:publisher_plugin"
```

`plugin_version` 必须与安装发行包的 metadata version 完全一致；`adapter_version` 表示本插件的
payload/回执适配契约版本。包装外部 CLI/API 时，可再填写成对的 `upstream_name` 与
`upstream_version`。

```python
from ai_ops.publishers import (
    AccountHealth,
    Platform,
    PublishResult,
    PublisherBase,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginManifest,
)


class AcmeZhihuPublisher(PublisherBase):
    platform = Platform.ZHIHU
    kind = "acme_zhihu"

    async def login(self, account_id, credential):
        ...

    async def publish(self, account_id, credential, content):
        return PublishResult(
            success=True,
            platform_post_id="123",
            platform_url="https://www.zhihu.com/p/123",
        )

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


def publisher_plugin():
    return PublisherPlugin(
        manifest=PublisherPluginManifest(
            plugin_id="acme.zhihu",
            plugin_version="1.2.3",
            api_version=1,
            platform=Platform.ZHIHU,
            publisher_kind="acme_zhihu",
            adapter_version="2",
            capabilities=(
                PublisherPluginCapability.HEALTH_CHECK,
                PublisherPluginCapability.LOGIN,
                PublisherPluginCapability.PUBLISH,
            ),
            upstream_name="pyzhihu-cli",
            upstream_version="0.2.4",
        ),
        factory=AcmeZhihuPublisher,
    )
```

能力列表必须按稳定字符串排序，并始终包含 `health_check`、`login`、`publish`。只有确实实现
`collect_metrics` 且设置 `supports_metrics=True` 时才能增加 `metrics`。声明
`agent_contract_renderer` 时，还必须提供 `renderer_id`、`contract_version`，并让 descriptor 的
platform、namespaced kind、adapter/contract version 与 manifest 完全一致。

Provider 与 factory/Publisher 构造器必须满足：

- 不访问网络、数据库、账号凭证或浏览器 profile；
- 不写文件、不启动子进程、不执行登录或平台探活；
- factory 每次返回一个新的 Publisher 实例；
- 外部 I/O 只能发生在异步 Publisher 方法中；
- 异常信息不能把 cookie、token、正文或本机路径拼入公开回执。

SDK 会验证类型和自描述一致性，但无法证明第三方代码真的遵守这些约束。

## 安装与启用

生产部署应锁定包版本和 hash/SBOM；项目不提供在线安装或修改 `.env` 的命令：

```bash
uv pip install 'acme-ai-ops==1.2.3'
ai-ops plugins list --json
```

人工审阅来源、许可证、依赖树和代码后，在 `.env` 中使用 JSON 数组授权：

```dotenv
PUBLISHER_PLUGIN_ALLOWLIST=["acme-ai-ops:acme.zhihu"]
```

然后执行：

```bash
ai-ops plugins doctor --json
ai-ops doctor
```

顶层 doctor 只做 metadata 检查；启用插件时它会给出提示运行专用 doctor 的 WARN，
因此这里不使用会把该预期提示当作失败的 `--strict`。

插件通过兼容性检查不等于平台 canary 通过，也不等于 Stable。发布回执、readback、账号权限和
连续 canary 证据仍按[平台能力矩阵](platform-capabilities.md)治理。

## 不可信适配器的未来边界

社区 CLI/MCP 若不能作为受信任依赖，不应直接包装成 v1 同进程插件。后续应使用独立
subprocess/RPC host、固定 argv/环境、资源限额和按能力发放凭证的 broker；在这条隔离边界落地前，
allowlist 不提供任何权限隔离保证。
