"""招聘平台账号管理 —— 复用 accounts/store.py 的 Fernet 加密。

只管 JobAccount 的增查 + 凭证加解密。store 可注入（单测传临时 key，无需配 FERNET_KEY env）。
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.store import CredentialStore, get_store
from .enums import JobBoard
from .models import JobAccount

# Cookie-Editor 的 sameSite 取值 → Playwright 接受的取值
_SAMESITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
    "": "Lax",
}


def normalize_cookies(raw: Sequence[dict], default_domain: str = ".zhipin.com") -> list[dict]:
    """把浏览器扩展（Cookie-Editor 等）导出的 cookie 归一化成 Playwright add_cookies 要的格式。

    处理差异：
      - expirationDate(float) → expires(int)；session cookie 无该字段则不带 expires
      - sameSite: no_restriction/unspecified/... → None/Lax/Strict
      - 补 domain/path 默认值；只保留 Playwright 认的字段
    丢弃无 name 的脏项。
    """
    out: list[dict] = []
    for c in raw:
        name = c.get("name")
        if not name:
            continue
        cookie = {
            "name": name,
            "value": c.get("value", ""),
            "domain": c.get("domain") or default_domain,
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": _SAMESITE_MAP.get(str(c.get("sameSite", "")).lower(), "Lax"),
        }
        exp = c.get("expirationDate", c.get("expires"))
        if isinstance(exp, (int, float)) and exp > 0:
            cookie["expires"] = int(exp)
        out.append(cookie)
    return out


def create_account(
    session: Session,
    board: JobBoard,
    nickname: str,
    cookies: Sequence[dict],
    *,
    profile: Optional[dict] = None,
    daily_quota: int = 30,
    store: Optional[CredentialStore] = None,
) -> JobAccount:
    """新建招聘账号，cookie 经 Fernet 加密落库。

    cookies: 形如 [{"name","value","domain","path"}, ...]（浏览器导出）。
    """
    st = store or get_store()
    acc = JobAccount(
        board=board,
        nickname=nickname,
        profile=profile or {},
        encrypted_credential=st.encrypt({"cookies": normalize_cookies(list(cookies))}),
        daily_quota=daily_quota,
    )
    session.add(acc)
    session.flush()
    return acc


def create_cdp_account(
    session: Session,
    board: JobBoard,
    nickname: str,
    *,
    profile: Optional[dict] = None,
    daily_quota: int = 30,
) -> JobAccount:
    """新建 CDP 模式账号——不存 cookie（登录态来自用户真 Chrome），只用于配额/绑定记账。"""
    acc = JobAccount(
        board=board,
        nickname=nickname,
        profile={**(profile or {}), "mode": "cdp"},
        encrypted_credential=b"",
        daily_quota=daily_quota,
    )
    session.add(acc)
    session.flush()
    return acc


def get_credential(
    session: Session, account_id: int, *, store: Optional[CredentialStore] = None
) -> dict:
    """解密取回账号凭证 {"cookies": [...]}。账号不存在或无凭证则抛错。"""
    acc = session.get(JobAccount, account_id)
    if acc is None:
        raise ValueError(f"招聘账号 {account_id} 不存在")
    if not acc.encrypted_credential:
        raise ValueError(f"招聘账号 {account_id} 未存凭证（先 add-account 导入 cookie）")
    st = store or get_store()
    return st.decrypt(acc.encrypted_credential)


def list_accounts(
    session: Session, board: Optional[JobBoard] = None
) -> list[JobAccount]:
    q = select(JobAccount).order_by(JobAccount.id)
    if board is not None:
        q = q.where(JobAccount.board == board)
    return list(session.scalars(q).all())
