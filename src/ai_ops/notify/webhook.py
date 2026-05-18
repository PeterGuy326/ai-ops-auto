"""通知 webhook 适配层 — 当前主力 = 飞书 custom robot。

底层逻辑：发布层事件流到 IM 群是"运行管理端"5 端闭环的最后一公里。本 sprint 用户主用飞书，
所以只实现飞书；钉钉/企微留 adapters.py 抽象空壳，下个 sprint 接。

失败容错原则：webhook 是辅助通道，不能让它拖死主业务。任何网络错误 / 4xx / 5xx
都吞掉 + logger.warning，不抛给调用方。这是通知模块的红线——通知挂了发布不能停。

依赖说明：pyproject 已有 httpx>=0.27，直接用同步 client；超时 5s 防止 event loop
长时间阻塞（worker 是 async，同步 post 走 to_thread 也是 follow-up 优化）。
"""
from __future__ import annotations

from typing import Optional

import httpx

from ..config import settings
from ..observability import get_logger

logger = get_logger(__name__)

# 飞书 custom robot 最大 5s 超时；webhook 响应慢就当它没响应——不能拖死主业务
_HTTP_TIMEOUT = 5.0


def send(text: str, *, webhook_url: Optional[str] = None) -> bool:
    """发送一条纯文本到飞书 webhook。

    Args:
        text: 消息正文，已渲染好的字符串（事件层负责拼模板）
        webhook_url: 可选覆盖；不传则用 settings.feishu_webhook_url

    Returns:
        True = 发送成功（HTTP 200 且 code=0）；False = 失败但已吞异常
    """
    url = webhook_url if webhook_url is not None else settings.feishu_webhook_url
    if not url:
        # 未配置 webhook URL → 静默跳过（开发环境常态，不刷 warning）
        logger.debug("notify.webhook: skipped (no FEISHU_WEBHOOK_URL configured)")
        return False

    payload = {"msg_type": "text", "content": {"text": text}}

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "notify.webhook: non-200",
                extra={
                    "event": "webhook_non_200",
                    "status_code": resp.status_code,
                    "body": resp.text[:200],
                },
            )
            return False
        # 飞书 robot 返回 {"StatusCode": 0, ...} 表示成功（也可能是 code/msg 字段）
        try:
            data = resp.json()
            # 兼容字段大小写差异（飞书 v1 返回 StatusCode/StatusMessage，v2 返回 code/msg）
            code = data.get("code", data.get("StatusCode", 0))
            if code != 0:
                logger.warning(
                    "notify.webhook: business-fail",
                    extra={
                        "event": "webhook_business_fail",
                        "code": code,
                        "body": resp.text[:200],
                    },
                )
                return False
        except Exception:
            # 响应非 JSON，但 HTTP 200，姑且认为成功（mock server 经常裸回 200 文本）
            pass
        return True
    except (httpx.RequestError, httpx.HTTPError) as e:
        # ConnectError / TimeoutException / 其他网络层失败 → 吞掉
        logger.warning(
            "notify.webhook: request failed",
            extra={"event": "webhook_request_failed", "error": str(e)},
        )
        return False
    except Exception as e:
        # 兜底防御：任何意外都不能炸到调用方
        logger.warning(
            "notify.webhook: unexpected error",
            extra={"event": "webhook_unexpected_error", "error": str(e)},
            exc_info=True,
        )
        return False
