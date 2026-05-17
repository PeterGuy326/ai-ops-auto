"""凭证加密存储 — Fernet 对称加密。

不裸存 cookie/token。FERNET_KEY 通过环境变量注入，泄露 = 全军覆没。
"""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


class CredentialStore:
    def __init__(self, key: str | None = None):
        k = key or settings.fernet_key
        if not k:
            raise RuntimeError("FERNET_KEY 未配置：cookie 加密无法工作")
        self._fernet = Fernet(k.encode() if isinstance(k, str) else k)

    def encrypt(self, payload: dict) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(raw)

    def decrypt(self, blob: bytes) -> dict:
        try:
            raw = self._fernet.decrypt(blob)
        except InvalidToken as e:
            raise RuntimeError("凭证解密失败：FERNET_KEY 不匹配或数据损坏") from e
        return json.loads(raw.decode("utf-8"))


def _store_singleton() -> CredentialStore | None:
    """惰性单例——避免 import 阶段触发 FERNET_KEY 检查。"""
    if not settings.fernet_key:
        return None
    return CredentialStore()


_store: CredentialStore | None = None


def get_store() -> CredentialStore:
    global _store
    if _store is None:
        _store = CredentialStore()
    return _store
