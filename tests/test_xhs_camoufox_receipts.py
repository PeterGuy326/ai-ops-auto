from ai_ops.publishers.xhs_camoufox import _parse_public_note_url


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
