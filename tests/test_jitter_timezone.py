from datetime import datetime
from types import SimpleNamespace

from ai_ops.scheduler import jitter


def test_naive_utc_night_is_pushed_using_business_timezone(monkeypatch):
    monkeypatch.setattr(
        jitter,
        "settings",
        SimpleNamespace(
            scheduler_timezone="Asia/Shanghai",
            publish_jitter_seconds=0,
        ),
    )
    monkeypatch.setattr(jitter.random, "randint", lambda start, end: 0)

    # 18:00 UTC is 02:00 the next day in Shanghai and must move to 07:00 local.
    planned = datetime(2026, 8, 9, 18, 0)
    assert jitter.jitter_publish_time(planned) == datetime(2026, 8, 9, 23, 0)
    assert jitter.is_safe_publish_window(planned) is False


def test_naive_utc_daytime_is_not_misclassified_as_night(monkeypatch):
    monkeypatch.setattr(
        jitter,
        "settings",
        SimpleNamespace(
            scheduler_timezone="Asia/Shanghai",
            publish_jitter_seconds=0,
        ),
    )

    # 02:00 UTC is 10:00 in Shanghai, so it stays unchanged.
    planned = datetime(2026, 8, 10, 2, 0)
    assert jitter.jitter_publish_time(planned) == planned
    assert jitter.is_safe_publish_window(planned) is True
