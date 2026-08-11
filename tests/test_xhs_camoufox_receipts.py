from ai_ops.core.enums import ContentType
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers import xhs_camoufox
from ai_ops.publishers.xhs_camoufox import XhsCamoufoxPublisher, _parse_public_note_url


def test_xhs_receipt_requires_strict_public_note_identity():
    note_id = "64f01234567890abcdef1234"

    assert _parse_public_note_url(
        f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=redacted"
    ) == (note_id, f"https://www.xiaohongshu.com/explore/{note_id}")
    assert _parse_public_note_url("https://www.xiaohongshu.com/explore") is None
    assert _parse_public_note_url("https://creator.xiaohongshu.com/publish/success") is None
    assert _parse_public_note_url("https://www.xiaohongshu.com/explore/not-a-note") is None
    assert _parse_public_note_url(
        f"https://www.xiaohongshu.com.evil.test/explore/{note_id}"
    ) is None


def test_exact_approval_payload_is_not_humanized(monkeypatch):
    monkeypatch.setattr(xhs_camoufox.settings, "xhs_humanize_enabled", True)
    monkeypatch.setattr(
        "ai_ops.content.humanize.humanize_for_xhs",
        lambda *_args, **_kwargs: "rewritten",
    )
    publisher = XhsCamoufoxPublisher()

    exact = PublishContent(
        title="title",
        body="approved bytes",
        content_type=ContentType.IMAGE_TEXT,
        tags=["bound-tag"],
        exact_approval=True,
    )
    legacy = exact.model_copy(update={"exact_approval": False})

    assert publisher._compose_body(exact) == "approved bytes\n\n#bound-tag"
    assert publisher._compose_body(legacy) == "rewritten\n\n#bound-tag"
