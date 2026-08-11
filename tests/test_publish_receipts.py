from __future__ import annotations

import json
from pathlib import Path

from ai_ops.core.schemas import PublishResult
from ai_ops.runtime import receipts


def test_receipt_spool_is_atomic_redacted_and_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts.settings, "data_dir", tmp_path)
    operation_id = "a" * 32
    result = PublishResult(
        success=True,
        platform_post_id="post-123",
        platform_url="https://example.test/posts/123",
        raw_response={
            "adapter": "fake-cli",
            "adapter_version": "1.2.3",
            "outcome": "confirmed",
            "API_KEY": "must-not-leak",
            "stdout": "body and cookie material must not be journaled",
        },
    )

    path = receipts.write_publish_receipt(
        job_id=7,
        operation_id=operation_id,
        publisher_kind="fake_cli",
        result=result,
    )

    assert path is not None and path.is_file() and not path.is_symlink()
    assert path.stat().st_mode & 0o077 == 0
    serialized = path.read_text(encoding="utf-8")
    assert "must-not-leak" not in serialized
    assert "body and cookie" not in serialized
    payload = receipts.read_publish_receipt(7, operation_id)
    assert payload is not None
    assert payload["platform_post_id"] == "post-123"
    assert payload["publisher_kind"] == "fake_cli"
    assert payload["raw_response"] == {
        "adapter": "fake-cli",
        "adapter_version": "1.2.3",
        "outcome": "confirmed",
    }

    # A symlink is never accepted as durable evidence.
    path.unlink()
    target = tmp_path / "attacker.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    path.symlink_to(target)
    assert receipts.read_publish_receipt(7, operation_id) is None


def test_remove_publish_receipt_is_scoped_to_one_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts.settings, "data_dir", tmp_path)
    first = "1" * 32
    second = "2" * 32
    result = PublishResult(success=False, outcome_uncertain=True)
    for operation_id in (first, second):
        assert receipts.write_publish_receipt(
            job_id=9,
            operation_id=operation_id,
            publisher_kind="fake_cli",
            result=result,
        )

    receipts.remove_publish_receipt(9, first)

    assert receipts.read_publish_receipt(9, first) is None
    assert receipts.read_publish_receipt(9, second) is not None
    assert Path(receipts.receipt_path(9, second)).exists()
