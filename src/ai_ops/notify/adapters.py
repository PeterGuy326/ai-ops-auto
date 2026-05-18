"""通知适配器抽象空壳 — 钉钉 / 企业微信。

当前 sprint out of scope：用户主用飞书，钉钉/企微下个 sprint 真接入。
留这两个空类是为了固化接口约定，避免下次接入时和 webhook.send 漂移。

接口契约：所有 adapter 都暴露 `send(text: str) -> bool`，
失败必须吞异常返回 False，不抛给调用方（与 webhook.send 一致）。
"""
from __future__ import annotations


class DingTalkAdapter:
    """钉钉自定义机器人（占位，out of scope, follow-up）。"""

    def send(self, text: str) -> bool:  # noqa: ARG002
        raise NotImplementedError(
            "DingTalkAdapter not implemented (out of scope, follow-up: 下个 sprint)"
        )


class WechatWorkAdapter:
    """企业微信群机器人（占位，out of scope, follow-up）。"""

    def send(self, text: str) -> bool:  # noqa: ARG002
        raise NotImplementedError(
            "WechatWorkAdapter not implemented (out of scope, follow-up: 下个 sprint)"
        )
