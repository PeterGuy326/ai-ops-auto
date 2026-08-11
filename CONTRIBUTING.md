# Contributing

感谢你帮助 `ai-ops-auto` 变成一个可验证、可维护的 Creator Ops 控制面。

## Before opening an issue

- 安全漏洞按 [SECURITY.md](SECURITY.md) 私密报告。
- 平台失效请说明平台、内容类型、OS、浏览器/适配器版本、上游 commit 和最后成功时间。
- 删除 cookie、token、账号 ID、二维码、个人内容、绝对路径和内网端点后再粘贴日志。

## Development setup

```bash
git clone https://github.com/PeterGuy326/ai-ops-auto.git
cd ai-ops-auto
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

生成 Fernet key，并手工填入本地 `.env`：

```bash
ai-ops gen-fernet-key
```

保持 `AUTO_PUBLISH_ENABLED=false`。绝大多数开发与测试不需要真实平台凭证。

React 前端：

```bash
cd frontend
npm ci
npm run dev
```

## Change workflow

1. 从最新 `main` 创建单一用途的分支。
2. 在最小充分范围内修改，保留工作区中与任务无关的现有改动。
3. 为用户可见行为增加测试，同步更新文档和 `CHANGELOG.md` 的 `Unreleased`。
4. 运行与改动最接近的测试，完成前运行全部检查。
5. 通过 Pull Request 合入 `main`；不把一个巨大的“顺手重构”夹在功能修复里。

## Required checks

```bash
python -m ruff check .
python -m pytest -q

cd frontend
npm ci
npm run lint
npm run build
```

也可在仓库根目录运行 `bash scripts/verify.sh`。只报告你实际运行过的结果；
无真账号/外部工具时把平台 E2E 标为 `NOT VERIFIED`。

## Platform adapter contributions

适配器代码与真实平台证据是两件事。新增或修复 Publisher 时：

- 集中管理 selector，对平台 UI 变化给出明确失败。
- 测试登录、发布参数翻译、成功判定、失败判定和 metrics 解析。
- 不要仅因为“没抛异常”就返回成功；需要 post id/url 或等价服务端证据。
- 不将真凭证放进 fixture、录像、trace、截图或 CI secret 依赖的公开测试。
- 按 [平台能力矩阵](docs/platform-capabilities.md) 的证据规则更新成熟度和 `Last verified`。

## Pull request evidence

PR 请包含一张验证表：

| ID | 验收标准（可观察行为） | 命令/手动步骤 | 环境 | 期望 | 实测 | 状态 |
|---|---|---|---|---|---|---|
| V1 | `<behavior>` | `<reproducible step>` | `<OS/tool/env>` | `<expected>` | `<observed>` | PASS / FAIL / NOT VERIFIED |

同时说明风险、迁移/回滚方案和未验证平台。

## License

提交贡献表示你同意按 [Apache License 2.0](LICENSE) 许可该贡献。引入外部代码或资产前必须确认来源、
与 Apache-2.0 的兼容性，并保留必要的归属声明。
