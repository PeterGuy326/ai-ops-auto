"""jobhunt P2 地基验收 —— 招聘账号 JobAccount + Fernet 凭证加解密。

离线、确定性：用临时生成的 Fernet key 注入 CredentialStore，无需配 FERNET_KEY env。
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.accounts.store import CredentialStore
from ai_ops.core.enums import AccountHealth
from ai_ops.core.models import Base
from ai_ops.jobhunt import models as jh_models  # noqa: F401  注册表
from ai_ops.jobhunt.accounts import (
    create_account,
    get_credential,
    list_accounts,
    normalize_cookies,
)
from ai_ops.jobhunt.enums import JobBoard
from ai_ops.jobhunt.models import JobAccount


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def store():
    return CredentialStore(key=Fernet.generate_key().decode())


_COOKIES = [
    {"name": "bst", "value": "secret-token-123", "domain": ".zhipin.com", "path": "/"},
    {"name": "geek_zp_token", "value": "abc", "domain": ".zhipin.com", "path": "/"},
]


def test_create_account_encrypts_credential(session, store):
    acc = create_account(session, JobBoard.BOSS, "我的Boss号", _COOKIES, store=store)
    assert acc.id is not None
    assert acc.board == JobBoard.BOSS
    assert acc.daily_quota == 30
    assert acc.health == AccountHealth.UNKNOWN
    # 凭证是密文，明文 token 不出现在落库字节里
    assert acc.encrypted_credential
    assert b"secret-token-123" not in acc.encrypted_credential


def test_credential_roundtrip(session, store):
    acc = create_account(session, JobBoard.BOSS, "号", _COOKIES, store=store)
    cred = get_credential(session, acc.id, store=store)
    # 归一化后 name/value/domain 保留，且补齐 Playwright 需要的字段
    got = {c["name"]: c for c in cred["cookies"]}
    assert got["bst"]["value"] == "secret-token-123"
    assert got["bst"]["domain"] == ".zhipin.com"
    assert got["bst"]["sameSite"] in ("Lax", "Strict", "None")
    assert "httpOnly" in got["bst"] and "secure" in got["bst"]


def test_normalize_cookies_from_cookie_editor():
    """Cookie-Editor 导出格式 → Playwright add_cookies 格式。"""
    raw = [
        {
            "name": "bst", "value": "tok", "domain": ".zhipin.com", "path": "/",
            "expirationDate": 1799999999.5, "sameSite": "no_restriction",
            "httpOnly": True, "secure": True, "hostOnly": False,  # hostOnly 应被丢弃
        },
        {"name": "sess", "value": "s", "sameSite": "unspecified"},  # 无 domain/expire=session
        {"value": "无名脏项"},  # 应被丢弃
    ]
    out = normalize_cookies(raw)
    assert len(out) == 2
    bst = out[0]
    assert bst["expires"] == 1799999999       # float→int
    assert bst["sameSite"] == "None"          # no_restriction→None
    assert "hostOnly" not in bst              # 非 Playwright 字段被剔除
    assert bst["httpOnly"] is True
    sess = out[1]
    assert sess["domain"] == ".zhipin.com"    # 补默认域
    assert sess["sameSite"] == "Lax"          # unspecified→Lax
    assert "expires" not in sess              # session cookie 不带 expires


def test_wrong_key_cannot_decrypt(session, store):
    acc = create_account(session, JobBoard.BOSS, "号", _COOKIES, store=store)
    other = CredentialStore(key=Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="解密失败"):
        get_credential(session, acc.id, store=other)


def test_get_credential_missing_account_raises(session, store):
    with pytest.raises(ValueError, match="不存在"):
        get_credential(session, 999, store=store)


def test_get_credential_no_credential_raises(session, store):
    acc = JobAccount(board=JobBoard.BOSS, nickname="空号")  # 不存凭证
    session.add(acc)
    session.flush()
    with pytest.raises(ValueError, match="未存凭证"):
        get_credential(session, acc.id, store=store)


def test_list_accounts_filter_by_board(session, store):
    create_account(session, JobBoard.BOSS, "boss1", _COOKIES, store=store)
    create_account(session, JobBoard.ZHILIAN, "zl1", _COOKIES, store=store)
    assert len(list_accounts(session)) == 2
    boss_only = list_accounts(session, JobBoard.BOSS)
    assert len(boss_only) == 1 and boss_only[0].nickname == "boss1"
