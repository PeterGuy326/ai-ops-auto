"""Task B · 通知模块单测。

覆盖：
  - dedup 滑窗：首条放行 / 第 2 次丢 / 第 3 次聚合放行 / >3 次静默丢
  - dedup 窗口过期后重置
  - webhook 容错：未配置 URL → False 不抛 / 连接拒绝 → False 不抛
  - 4 个事件 API：dict/ORM-like 对象都支持
  - A↔B 签名兼容：notify.report_ready vs notifier_stub.report_ready
  - 模板内容包含规划字段（article_id / account_id / platform / title / url）
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from ai_ops.notify import (
    account_expired,
    content_taint,
    dedup,
    fanout_done,
    publish_failed,
    publish_success,
    report_ready,
    webhook,
)
from ai_ops.notify.adapters import DingTalkAdapter, WechatWorkAdapter
from ai_ops.notify.dedup import Deduper


# ============================================================================
# dedup 滑窗
# ============================================================================
class TestDeduper:
    def test_first_pass(self):
        d = Deduper(window_seconds=300, threshold=3)
        ok, hint = d.should_send("publish_success", "job:1")
        assert ok is True
        assert hint is None

    def test_second_silenced(self):
        d = Deduper(window_seconds=300, threshold=3)
        d.should_send("publish_success", "job:1")
        ok, hint = d.should_send("publish_success", "job:1")
        assert ok is False
        assert hint is None

    def test_third_aggregated(self):
        d = Deduper(window_seconds=300, threshold=3)
        d.should_send("publish_success", "job:1")
        d.should_send("publish_success", "job:1")
        ok, hint = d.should_send("publish_success", "job:1")
        assert ok is True
        assert hint is not None
        assert "第 3 次" in hint

    def test_fourth_and_fifth_silenced(self):
        d = Deduper(window_seconds=300, threshold=3)
        # 灌满前 3 次（1 放 / 2 丢 / 3 聚合放）
        for _ in range(3):
            d.should_send("publish_success", "job:1")
        # 第 4 / 5 次必须静默
        ok4, _ = d.should_send("publish_success", "job:1")
        ok5, _ = d.should_send("publish_success", "job:1")
        assert ok4 is False
        assert ok5 is False

    def test_different_target_isolated(self):
        """不同 target_id 桶相互独立。"""
        d = Deduper(window_seconds=300, threshold=3)
        d.should_send("publish_success", "job:1")
        ok, _ = d.should_send("publish_success", "job:2")
        assert ok is True

    def test_different_event_isolated(self):
        """不同 event_type 桶相互独立。"""
        d = Deduper(window_seconds=300, threshold=3)
        d.should_send("publish_success", "job:1")
        ok, _ = d.should_send("publish_failed", "job:1")
        assert ok is True

    def test_window_expiry(self, monkeypatch):
        """超出 window 后，旧时间戳被清，新一次又算"首条"。"""
        import ai_ops.notify.dedup as dedup_mod
        # 用 1 秒窗口 + 模拟时间
        fake_time = [1000.0]

        def fake_monotonic():
            return fake_time[0]

        monkeypatch.setattr(dedup_mod.time, "monotonic", fake_monotonic)
        d = Deduper(window_seconds=10, threshold=3)
        ok1, _ = d.should_send("e", "1")
        assert ok1 is True
        # 推进 11 秒，旧时间戳 cutoff 之外
        fake_time[0] += 11
        ok2, _ = d.should_send("e", "1")
        assert ok2 is True  # 又是"首条"

    def test_reset(self):
        d = Deduper(window_seconds=300, threshold=3)
        d.should_send("e", "1")
        d.reset()
        ok, _ = d.should_send("e", "1")
        assert ok is True


# ============================================================================
# webhook 容错
# ============================================================================
class TestWebhook:
    def test_no_url_skip_silently(self):
        # 不传 URL 且 settings 默认空 → 返回 False、不抛
        assert webhook.send("hello", webhook_url="") is False

    def test_connect_refused_swallowed(self):
        # 端口 1 几乎肯定被拒；超时 5s 内必然失败
        result = webhook.send("hello", webhook_url="http://127.0.0.1:1/")
        assert result is False  # 失败但不抛

    def test_success_with_mock(self):
        """httpx 层 mock 一个 200 响应。"""
        import httpx

        class _MockResp:
            status_code = 200
            text = '{"code":0,"msg":"ok"}'

            def json(self):
                return {"code": 0, "msg": "ok"}

        class _MockClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _MockResp()

        with patch.object(httpx, "Client", _MockClient):
            ok = webhook.send("hi", webhook_url="http://fake/")
        assert ok is True


# ============================================================================
# 事件 API
# ============================================================================
@pytest.fixture(autouse=True)
def _reset_dedup():
    """每个 test 前后清理 dedup，避免互相污染。"""
    dedup.reset_for_test()
    yield
    dedup.reset_for_test()


@pytest.fixture
def captured():
    """捕获 webhook.send 的所有调用。"""
    calls: list[str] = []

    def _fake_send(text, **_):
        calls.append(text)
        return True

    with patch("ai_ops.notify.webhook.send", side_effect=_fake_send):
        yield calls


def test_publish_success_template(captured):
    job = {
        "id": 42,
        "account_id": 7,
        "platform": "xiaohongshu",
        "platform_url": "https://xhs.com/p/abc",
        "title": "LLM 集成实战",
    }
    publish_success(job)
    assert len(captured) == 1
    msg = captured[0]
    assert "account_id=7" in msg
    assert "xiaohongshu" in msg
    assert "《LLM 集成实战》" in msg
    assert "https://xhs.com/p/abc" in msg


def test_publish_failed_template(captured):
    job = {"id": 99, "account_id": 3, "error": "风控触发"}
    publish_failed(job)
    assert len(captured) == 1
    msg = captured[0]
    assert "job_id=99" in msg
    assert "风控触发" in msg


def test_account_expired_template(captured):
    from ai_ops.core.enums import AccountHealth

    account = {
        "id": 5,
        "nickname": "dws_xhs_01",
        "platform": "xiaohongshu",
        "health": AccountHealth.EXPIRED,
    }
    account_expired(account)
    assert len(captured) == 1
    msg = captured[0]
    assert "account_id=5" in msg
    assert "dws_xhs_01" in msg
    assert "expired" in msg
    assert "/accounts/5/login" in msg


def test_report_ready_template(captured):
    report_ready("daily", "/tmp/report.md")
    assert len(captured) == 1
    msg = captured[0]
    assert "日报" in msg
    assert "/tmp/report.md" in msg


def test_report_ready_weekly_label(captured):
    report_ready("weekly", "/tmp/w.md")
    assert "周报" in captured[0]


def test_event_supports_orm_like_object(captured):
    """既能传 dict，也能传 ORM-like 对象（getattr 兜底）。"""

    class FakeJob:
        id = 1
        account_id = 2
        platform = "zhihu"
        platform_url = "https://zhihu.com/p/1"
        title = "标题"

    publish_success(FakeJob())
    assert len(captured) == 1
    assert "zhihu" in captured[0]


def test_dedup_silenced_event_not_sent(captured):
    """同 job 连发 2 次，只发出 1 条。"""
    job = {"id": 1, "account_id": 1, "platform": "xhs", "platform_url": "", "title": "x"}
    publish_success(job)
    publish_success(job)  # 第 2 次必须被去重
    assert len(captured) == 1


def test_dedup_threshold_aggregated_event(captured):
    """同 job 连发 3 次，发出 2 条（首条 + 聚合）。"""
    job = {"id": 1, "account_id": 1, "platform": "xhs", "platform_url": "", "title": "x"}
    for _ in range(3):
        publish_success(job)
    assert len(captured) == 2
    assert "第 3 次" in captured[1]


def test_content_taint_template(captured):
    content_taint(123, "TODO")
    assert len(captured) == 1
    assert "article_id=123" in captured[0]
    assert "TODO" in captured[0]


def test_fanout_done_template(captured):
    fanout_done(123, 5, 2)
    assert len(captured) == 1
    assert "成功 5" in captured[0]
    assert "失败 2" in captured[0]


# ============================================================================
# A↔B 接口契约：signature 必须兼容
# ============================================================================
def test_signature_compatible_with_a_stub():
    from ai_ops.reports.notifier_stub import report_ready as stub
    from ai_ops.notify import report_ready as real
    stub_params = set(inspect.signature(stub).parameters.keys())
    real_params = set(inspect.signature(real).parameters.keys())
    assert stub_params == real_params, f"A↔B signature mismatch: stub={stub_params} real={real_params}"


# ============================================================================
# adapters 空壳
# ============================================================================
def test_dingtalk_adapter_not_implemented():
    with pytest.raises(NotImplementedError, match="out of scope"):
        DingTalkAdapter().send("hi")


def test_wechatwork_adapter_not_implemented():
    with pytest.raises(NotImplementedError, match="out of scope"):
        WechatWorkAdapter().send("hi")


# ============================================================================
# 事件函数不会向上抛异常（webhook.send 内部已吞，事件层再加一层防御自检）
# ============================================================================
def test_event_with_broken_webhook_does_not_raise():
    """webhook.send 抛异常时，事件函数也不能向上抛。"""
    def _explode(text, **_):
        raise RuntimeError("network down")

    with patch("ai_ops.notify.webhook.send", side_effect=_explode):
        # publish_success 内部直接调 webhook.send；若不吞会抛 RuntimeError
        # 验证：业务调用方拿到的就是 None，不爆炸
        try:
            publish_success({"id": 1, "account_id": 1, "platform": "xhs"})
        except Exception as e:
            pytest.fail(f"event function leaked exception: {e}")
