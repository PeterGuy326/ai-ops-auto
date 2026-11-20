"""EaselWechatPublisher + easel_readback 离线契约测试 — 全 mock，不真spawn/不联网。

覆盖与 wechat_mp_api 同一纪律：
  1. 未登录预检失败 → 无写副作用、不消耗重试
  2. 确认草稿 → success=False/effect_applied=True/retryable=False（非公开发布）
  3. 写进程启动但未确认 → outcome_uncertain=True，禁止 fallback/自动重试
  4. 缺封面/视频/空标题 → 确定失败
  5. health/metrics 路由；metrics 不可用不伪造零值
  6. renderer 纯投影不含宿主路径/凭证，digest 稳定
  7. readback 比对：META 注释剔除、代码围栏内 '# 注释' 不被误剥
"""
from __future__ import annotations

import json

import pytest

from ai_ops.core.enums import AccountHealth, ContentType
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers import easel_wechat as ew
from ai_ops.publishers.easel_readback import compare_article


def _content(tmp_path, **over) -> PublishContent:
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0fake")
    base = dict(
        title="测试标题",
        body="正文第一段\n\n```\n# 这是代码注释\n```\n\n<!-- META: 编辑备注 -->\n结尾段",
        content_type=ContentType.IMAGE_TEXT,
        images=[str(cover)],
    )
    base.update(over)
    return PublishContent(**base)


def _patch_run(monkeypatch, whoami=True, publish=None):
    calls = []

    async def fake(args, timeout=120.0):
        calls.append(args[0])
        if args[0] == "whoami":
            return (0, json.dumps({"loggedIn": whoami}), "")
        if args[0] == "publish":
            return publish if publish is not None else (0, json.dumps({"success": True, "media_id": 100000104}), "")
        if args[0] == "stats":
            return (0, json.dumps({"fans": 100, "read_uv": 31}), "")
        return (1, "", "unknown")

    monkeypatch.setattr(ew, "run_easel_script", fake)
    return calls


@pytest.mark.asyncio
async def test_draft_confirmed_not_public(tmp_path, monkeypatch):
    _patch_run(monkeypatch)
    res = await ew.EaselWechatPublisher().publish(1, {}, _content(tmp_path))
    assert res.success is False
    assert res.effect_applied is True
    assert res.retryable is False
    assert res.outcome_uncertain is False
    assert res.platform_post_id == "100000104"


@pytest.mark.asyncio
async def test_not_logged_in_no_write(tmp_path, monkeypatch):
    calls = _patch_run(monkeypatch, whoami=False)
    res = await ew.EaselWechatPublisher().publish(1, {}, _content(tmp_path))
    assert res.success is False and res.effect_applied is False
    assert res.retryable is False and res.outcome_uncertain is False
    assert "publish" not in calls  # 预检拦截，未触发写


@pytest.mark.asyncio
async def test_write_started_unknown(tmp_path, monkeypatch):
    _patch_run(monkeypatch, publish=(1, "", "boom"))
    res = await ew.EaselWechatPublisher().publish(1, {}, _content(tmp_path))
    assert res.outcome_uncertain is True and res.retryable is False


@pytest.mark.asyncio
async def test_missing_cover_deterministic(tmp_path, monkeypatch):
    _patch_run(monkeypatch)
    res = await ew.EaselWechatPublisher().publish(1, {}, _content(tmp_path, images=[]))
    assert res.success is False and res.retryable is False and res.effect_applied is False


@pytest.mark.asyncio
async def test_health_and_metrics(tmp_path, monkeypatch):
    _patch_run(monkeypatch)
    pub = ew.EaselWechatPublisher()
    assert await pub.health_check(1, {}) == AccountHealth.HEALTHY
    # 草稿无公开指标：fail-closed 跳过，不伪造计数
    assert (await pub.collect_metrics("1", None, {}))["skipped"] is True
    _patch_run(monkeypatch, whoami=False)
    assert await pub.health_check(1, {}) == AccountHealth.EXPIRED


def test_renderer_projection_path_free(tmp_path):
    pub = ew.EaselWechatPublisher()
    payload = pub.render_agent_contract_payload(_content(tmp_path))
    assert "cover.jpg" not in json.dumps(payload)  # 不含宿主路径
    d1 = pub.agent_contract_digest_material(_content(tmp_path))
    d2 = pub.agent_contract_digest_material(_content(tmp_path))
    assert d1 == d2
    assert pub.agent_contract_renderer_descriptor.adapter_version == ew.EASEL_UPSTREAM_COMMIT


def test_readback_compare_fence_and_meta():
    src = "标题行\n\n正文第一段\n\n```\n# 代码注释\n```\n\n<!-- META: 备注 -->\n结尾段"
    rb = "标题行 正文第一段 # 代码注释 结尾段"
    rep = compare_article(src, "标题行", "", rb, source_title="标题行")
    assert rep["title_match"] is True
    assert rep["body_all_match"] is True  # 围栏 '# 注释' 保留、META 剔除
    assert rep["body_total"] == rep["body_matched"]


def test_readback_compare_table_and_inline_code():
    src = (
        "标题行\n\n| 结果 | 含义 |\n|---|---|\n| accept | 值得接 |\n\n"
        "跑 `eval` 检查，钉在 `0.6.0`：\n\n结尾段"
    )
    # 渲染后：表格去竖线/分隔行，行内代码去反引号
    rb = "标题行 结果 含义 accept 值得接 跑 eval 检查，钉在 0.6.0： 结尾段"
    rep = compare_article(src, "标题行", "", rb, source_title="标题行")
    assert rep["body_all_match"] is True
    assert rep["body_total"] == rep["body_matched"]