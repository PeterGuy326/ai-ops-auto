from __future__ import annotations

import asyncio

import pytest

from ai_ops.runtime.account_lease import (
    AccountOperationLease,
    AccountOperationLeaseTimeout,
)
from ai_ops.runtime import account_lease as lease_mod


def test_account_lease_serializes_separate_async_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_mod.settings, "data_dir", tmp_path)

    async def exercise():
        async with AccountOperationLease(12, timeout_seconds=0):
            with pytest.raises(AccountOperationLeaseTimeout):
                async with AccountOperationLease(12, timeout_seconds=0):
                    pytest.fail("the same account lease must be exclusive")
            # A different account is independent.
            async with AccountOperationLease(13, timeout_seconds=0):
                pass

    asyncio.run(exercise())


def test_account_lease_rejects_symlink_lock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_mod.settings, "data_dir", tmp_path)
    lock = AccountOperationLease(9, timeout_seconds=0)
    lock.path.parent.mkdir(parents=True)
    target = tmp_path / "outside-lock"
    target.write_text("do not lock through symlink", encoding="utf-8")
    lock.path.symlink_to(target)

    async def exercise():
        with pytest.raises(OSError, match="symlink"):
            async with lock:
                pass

    asyncio.run(exercise())
