from types import SimpleNamespace

import pytest

from ai_ops.agent_contract.bindings import account_binding_digest
from ai_ops.core.enums import Platform


def _account(**changes):
    values = {
        "id": 7,
        "platform": Platform.ZHIHU,
        "nickname": "approved-destination",
        "topic_id": 3,
        "profile": {"subject_id": "zhihu-user-7"},
        "encrypted_credential": b"ciphertext-generation-one",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_account_binding_is_stable_and_credential_free():
    first = account_binding_digest(_account())
    second = account_binding_digest(_account())

    assert first == second
    assert len(first) == 64
    assert "ciphertext" not in first


@pytest.mark.parametrize(
    "changes",
    [
        {"platform": Platform.XIAOHONGSHU},
        {"nickname": "replacement"},
        {"topic_id": 4},
        {"profile": {"subject_id": "other"}},
        {"encrypted_credential": b"ciphertext-generation-two"},
    ],
)
def test_account_binding_changes_with_destination_identity(changes):
    assert account_binding_digest(_account(**changes)) != account_binding_digest(
        _account()
    )


def test_account_binding_rejects_unknown_credential_storage_type():
    with pytest.raises(ValueError, match="invalid storage type"):
        account_binding_digest(_account(encrypted_credential="plaintext-secret"))
