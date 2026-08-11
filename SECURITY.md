# Security Policy

`ai-ops-auto` 会处理平台 cookie/token，并能执行真实内容发布。请把鉴权绕过、凭证暴露、重复/误发布、
CSRF、命令注入、SSRF 与不安全默认视为安全问题。

## Supported versions

项目尚处于 `0.x` Alpha 阶段，目前只对默认分支最新代码提供安全修复。尚未发布稳定版支持周期。

## Private reporting

请使用 GitHub 的
[private vulnerability report](https://github.com/PeterGuy326/ai-ops-auto/security/advisories/new)。

不要在公开 Issue、Discussion、PR、截图或日志片段中提交：

- API key、cookie、session、webhook URL、Fernet key 或数据库凭证。
- 真实账号昵称、私信、二维码、简历/个人数据或内部网络信息。
- 能直接触发真实发布的可复用攻击脚本。

报告建议包含：受影响 commit/版本、影响、前置条件、已脱敏的复现步骤与建议缓解方案。

维护者将尽快确认收到报告，并在完成影响评估后协调修复与公开时间。项目当前不承诺固定 SLA。

## If a credential was exposed

凭证一旦进入公开 Git 历史、Issue 或 CI 日志，就应视为已泄露。只删除文本不足够：

1. 立即在原服务中吊销或轮换凭证。
2. 终止相关 session，检查访问/发布/通知日志。
3. 通过私密渠道通知维护者，说明泄露时间窗和受影响权限。
4. 如果泄露 `FERNET_KEY`，同时假设数据库内所有加密平台凭证可被解密，逐一重置平台登录态。

## Deployment baseline

- 保持 `AUTO_PUBLISH_ENABLED=false`，直到测试账号完成单平台验证。
- 对外暴露前设置强随机 `API_KEY`，通过 TLS 反向代理访问。
- 把 `.env` 限制为服务账号可读，使用密钥管理系统而不是镜像层或命令行参数。
- 使用专用运营账号和最小权限；不使用个人主账号测试实验性适配器。
- 不记录完整 `DATABASE_URL`、Authorization header、cookie 或 Publisher 原始命令中的敏感值。

详细拓扑见 [docs/deployment.md](docs/deployment.md)。
