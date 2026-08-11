"""Security and compatibility contract for the Publisher Plugin SDK."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_ops.agent_contract.schemas import RendererContract
from ai_ops.core.enums import AccountHealth, Platform
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers.base import AgentContractRendererDescriptor, PublisherBase
from ai_ops.publishers.plugin_sdk import (
    PUBLISHER_PLUGIN_API_VERSION,
    PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginErrorCode,
    PublisherPluginManifest,
    PublisherPluginResolutionError,
    inspect_publisher_plugins,
    validate_enabled_publisher_plugins,
)
from ai_ops.publishers.registry import PublisherRegistry, build_default_registry


CORE_CAPABILITIES = (
    PublisherPluginCapability.HEALTH_CHECK,
    PublisherPluginCapability.LOGIN,
    PublisherPluginCapability.PUBLISH,
)


class _PluginPublisher(PublisherBase):
    platform = Platform.ZHIHU
    kind = "acme_zhihu"

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        return PublishResult(success=True, platform_post_id="plugin-post")

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


class _OtherPluginPublisher(_PluginPublisher):
    kind = "beta_zhihu"


class _MetricsPluginPublisher(_PluginPublisher):
    kind = "metrics_zhihu"
    supports_metrics = True

    async def collect_metrics(self, post_id, post_url, credential):
        return {"views": 1}


class _WrongIdentityPublisher(_PluginPublisher):
    kind = "wrong_identity"


class _ExactPluginPublisher(_PluginPublisher):
    kind = "acme_zhihu_exact"
    agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
        renderer_id="acme.zhihu.exact",
        contract_version="1",
        adapter_version="adapter-7",
        platform=Platform.ZHIHU,
        publisher_kind=kind,
        requires_external_account_id=True,
    )

    def render_agent_contract_payload(self, content: PublishContent) -> dict[str, object]:
        return {"body": content.body, "title": content.title}


class _FakeEntryPoint:
    group = PUBLISHER_PLUGIN_ENTRY_POINT_GROUP

    def __init__(
        self,
        *,
        distribution: str,
        name: str,
        provider,
        version: str = "1.2.3",
    ) -> None:
        self.name = name
        self.dist = SimpleNamespace(metadata={"Name": distribution}, version=version)
        self._provider = provider
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if isinstance(self._provider, BaseException):
            raise self._provider
        return self._provider


def _manifest(
    *,
    plugin_id: str = "acme.zhihu",
    kind: str = "acme_zhihu",
    api_version: int = PUBLISHER_PLUGIN_API_VERSION,
    priority: int = 5,
    capabilities=CORE_CAPABILITIES,
    adapter_version: str = "adapter-1",
    renderer_id: str | None = None,
    contract_version: str | None = None,
) -> PublisherPluginManifest:
    return PublisherPluginManifest(
        plugin_id=plugin_id,
        plugin_version="1.2.3",
        api_version=api_version,
        platform=Platform.ZHIHU,
        publisher_kind=kind,
        adapter_version=adapter_version,
        capabilities=capabilities,
        priority=priority,
        renderer_id=renderer_id,
        contract_version=contract_version,
    )


def _entry_point(
    *,
    distribution: str = "acme-ai-ops",
    plugin_id: str = "acme.zhihu",
    kind: str = "acme_zhihu",
    factory=_PluginPublisher,
    priority: int = 5,
    api_version: int = PUBLISHER_PLUGIN_API_VERSION,
    capabilities=CORE_CAPABILITIES,
    adapter_version: str = "adapter-1",
    renderer_id: str | None = None,
    contract_version: str | None = None,
):
    def provider():
        return PublisherPlugin(
            manifest=_manifest(
                plugin_id=plugin_id,
                kind=kind,
                api_version=api_version,
                priority=priority,
                capabilities=capabilities,
                adapter_version=adapter_version,
                renderer_id=renderer_id,
                contract_version=contract_version,
            ),
            factory=factory,
        )

    return _FakeEntryPoint(distribution=distribution, name=plugin_id, provider=provider)


def _registry_config(*, allowlist=()):
    return SimpleNamespace(
        baijiahao_publisher_enabled=False,
        browser_engine="playwright_chrome_channel",
        publisher_plugin_allowlist=tuple(allowlist),
        sohuhao_publisher_enabled=False,
        youtube_uploader_enabled=False,
        zhihu_cli_enabled=False,
    )


def test_empty_allowlist_never_loads_installed_plugin_code():
    entry_point = _entry_point()

    inventory = inspect_publisher_plugins((), entry_points=[entry_point])
    validation = validate_enabled_publisher_plugins((), entry_points=[entry_point])

    assert inventory.ok is True
    assert inventory.entries[0].enabled is False
    assert validation.to_dict()["code_loaded"] is False
    assert entry_point.load_calls == 0


def test_inventory_reports_missing_and_duplicate_selection_without_loading():
    first = _entry_point()
    duplicate = _entry_point()
    missing_selector = "missing-dist:missing.plugin"

    inventory = inspect_publisher_plugins(
        ("acme-ai-ops:acme.zhihu", missing_selector),
        entry_points=[duplicate, first],
    )

    assert inventory.ok is False
    assert inventory.missing_enabled == (missing_selector,)
    assert inventory.duplicate_enabled == ("acme-ai-ops:acme.zhihu",)
    assert first.load_calls == duplicate.load_calls == 0


def test_validation_loads_only_the_exact_distribution_and_entry_point():
    selected = _entry_point()
    disabled = _entry_point(
        distribution="other-ai-ops",
        plugin_id="other.zhihu",
        kind="other_zhihu",
        factory=_OtherPluginPublisher,
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[disabled, selected],
    )

    assert report.ok is True
    assert [plugin.selector for plugin in report.loaded] == ["acme-ai-ops:acme.zhihu"]
    assert selected.load_calls == 1
    assert disabled.load_calls == 0


def test_api_mismatch_never_calls_the_publisher_factory():
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return _PluginPublisher()

    entry_point = _entry_point(api_version=2, factory=factory)

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert report.errors[0].code is PublisherPluginErrorCode.API_INCOMPATIBLE
    assert factory_calls == 0


def test_load_and_factory_errors_are_redacted():
    secret = "token=must-not-leak"
    load_error = _FakeEntryPoint(
        distribution="acme-ai-ops",
        name="acme.zhihu",
        provider=RuntimeError(secret),
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[load_error],
    )
    rendered = str(report.to_dict())

    assert report.errors[0].code is PublisherPluginErrorCode.LOAD_FAILED
    assert report.errors[0].exception_type == "RuntimeError"
    assert secret not in rendered


@pytest.mark.parametrize("boundary", ["entry_point_load", "publisher_factory"])
def test_operator_keyboard_interrupt_propagates_across_plugin_boundaries(boundary):
    """KeyboardInterrupt is operator cancellation, not an invalid-plugin report."""

    def interrupt_factory():
        raise KeyboardInterrupt

    if boundary == "entry_point_load":
        entry_point = _FakeEntryPoint(
            distribution="acme-ai-ops",
            name="acme.zhihu",
            provider=KeyboardInterrupt(),
        )
    else:
        entry_point = _entry_point(factory=interrupt_factory)

    with pytest.raises(KeyboardInterrupt):
        validate_enabled_publisher_plugins(
            ("acme-ai-ops:acme.zhihu",),
            entry_points=[entry_point],
        )


def test_same_platform_different_kinds_are_deterministic_and_allowed():
    beta = _entry_point(
        distribution="beta-ai-ops",
        plugin_id="beta.zhihu",
        kind="beta_zhihu",
        factory=_OtherPluginPublisher,
        priority=5,
    )
    acme = _entry_point(priority=5)
    selectors = ("beta-ai-ops:beta.zhihu", "acme-ai-ops:acme.zhihu")

    registry = build_default_registry(
        config=_registry_config(allowlist=selectors),
        plugin_entry_points=[beta, acme],
    )
    publishers = registry.resolve(Platform.ZHIHU)

    assert [publisher.kind for publisher in publishers[:2]] == ["acme_zhihu", "beta_zhihu"]
    assert registry.plugin_validation_report.ok is True


def test_duplicate_platform_and_kind_rejects_every_ambiguous_plugin():
    first = _entry_point()
    second = _entry_point(
        distribution="copy-ai-ops",
        plugin_id="copy.zhihu",
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu", "copy-ai-ops:copy.zhihu"),
        entry_points=[first, second],
    )

    assert report.loaded == ()
    assert {error.code for error in report.errors} == {PublisherPluginErrorCode.KIND_CONFLICT}


def test_same_plugin_id_from_two_selected_distributions_rejects_both():
    first = _entry_point()
    second = _entry_point(
        distribution="copy-ai-ops",
        kind="beta_zhihu",
        factory=_OtherPluginPublisher,
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu", "copy-ai-ops:acme.zhihu"),
        entry_points=[first, second],
    )

    assert report.loaded == ()
    assert len(report.errors) == 2
    assert {error.code for error in report.errors} == {PublisherPluginErrorCode.MANIFEST_MISMATCH}


def test_registry_revalidates_every_factory_result_and_never_falls_back():
    calls = 0

    def changing_factory():
        nonlocal calls
        calls += 1
        return _PluginPublisher() if calls == 1 else _WrongIdentityPublisher()

    entry_point = _entry_point(factory=changing_factory)
    registry = build_default_registry(
        config=_registry_config(allowlist=("acme-ai-ops:acme.zhihu",)),
        plugin_entry_points=[entry_point],
    )

    assert calls == 1
    with pytest.raises(PublisherPluginResolutionError) as raised:
        registry.resolve(Platform.ZHIHU)
    assert raised.value.code == PublisherPluginErrorCode.IDENTITY_MISMATCH.value


def test_invalid_enabled_plugin_keeps_registry_importable_but_blocks_routing():
    entry_point = _entry_point(factory=lambda: object())
    registry = build_default_registry(
        config=_registry_config(allowlist=("acme-ai-ops:acme.zhihu",)),
        plugin_entry_points=[entry_point],
    )

    assert registry.plugin_validation_report.errors[0].code is (
        PublisherPluginErrorCode.PUBLISHER_INVALID
    )
    with pytest.raises(PublisherPluginResolutionError):
        registry.resolve(Platform.ZHIHU)


def test_namespaced_plugin_kind_supports_agent_exact_renderer_contract():
    capabilities = (
        PublisherPluginCapability.AGENT_CONTRACT_RENDERER,
        PublisherPluginCapability.HEALTH_CHECK,
        PublisherPluginCapability.LOGIN,
        PublisherPluginCapability.PUBLISH,
    )
    entry_point = _entry_point(
        kind="acme_zhihu_exact",
        factory=_ExactPluginPublisher,
        capabilities=capabilities,
        adapter_version="adapter-7",
        renderer_id="acme.zhihu.exact",
        contract_version="1",
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )
    descriptor = report.loaded[0].plugin.factory().agent_contract_renderer_descriptor
    contract = RendererContract.model_validate(descriptor.digest_material())

    assert report.ok is True
    assert contract.publisher_kind == "acme_zhihu_exact"
    assert contract.adapter_version == "adapter-7"


def test_plugin_doctor_rejects_renderer_that_planning_would_reject():
    class _UnsafeZhihuExactPublisher(_PluginPublisher):
        kind = "unsafe_zhihu_exact"
        agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
            renderer_id="unsafe.zhihu.exact",
            contract_version="1",
            adapter_version="adapter-7",
            platform=Platform.ZHIHU,
            publisher_kind=kind,
            requires_external_account_id=False,
        )

        def render_agent_contract_payload(self, content: PublishContent) -> dict[str, object]:
            return {"body": content.body, "title": content.title}

    capabilities = (
        PublisherPluginCapability.AGENT_CONTRACT_RENDERER,
        PublisherPluginCapability.HEALTH_CHECK,
        PublisherPluginCapability.LOGIN,
        PublisherPluginCapability.PUBLISH,
    )
    entry_point = _entry_point(
        kind="unsafe_zhihu_exact",
        factory=_UnsafeZhihuExactPublisher,
        capabilities=capabilities,
        adapter_version="adapter-7",
        renderer_id="unsafe.zhihu.exact",
        contract_version="1",
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert report.loaded == ()
    assert report.errors[0].code is PublisherPluginErrorCode.CAPABILITY_MISMATCH


def test_renderer_capability_uses_descriptor_as_the_runtime_fact():
    class _HiddenRendererPublisher(_ExactPluginPublisher):
        kind = "hidden_renderer"
        agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
            renderer_id="hidden.renderer",
            contract_version="1",
            adapter_version="adapter-1",
            platform=Platform.ZHIHU,
            publisher_kind=kind,
            requires_external_account_id=True,
        )

        @property
        def supports_agent_contract_renderer(self) -> bool:
            return False

    entry_point = _entry_point(
        kind="hidden_renderer",
        factory=_HiddenRendererPublisher,
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert report.loaded == ()
    assert report.errors[0].code is PublisherPluginErrorCode.CAPABILITY_MISMATCH


@pytest.mark.parametrize("method_name", ["login", "publish", "health_check"])
@pytest.mark.parametrize("implementation", [None, lambda *_args, **_kwargs: True])
def test_required_publisher_methods_must_be_callable_and_async(
    method_name,
    implementation,
):
    publisher_type = type(
        "InvalidCorePublisher",
        (_PluginPublisher,),
        {
            "kind": "invalid_core",
            method_name: implementation,
        },
    )
    entry_point = _entry_point(
        kind="invalid_core",
        factory=publisher_type,
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert report.loaded == ()
    assert report.errors[0].code is PublisherPluginErrorCode.CAPABILITY_MISMATCH


def test_renderer_projection_must_remain_callable():
    class _InvalidRendererPublisher(_ExactPluginPublisher):
        render_agent_contract_payload = None

    capabilities = (
        PublisherPluginCapability.AGENT_CONTRACT_RENDERER,
        PublisherPluginCapability.HEALTH_CHECK,
        PublisherPluginCapability.LOGIN,
        PublisherPluginCapability.PUBLISH,
    )
    entry_point = _entry_point(
        kind="acme_zhihu_exact",
        factory=_InvalidRendererPublisher,
        capabilities=capabilities,
        adapter_version="adapter-7",
        renderer_id="acme.zhihu.exact",
        contract_version="1",
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert report.loaded == ()
    assert report.errors[0].code is PublisherPluginErrorCode.CAPABILITY_MISMATCH


def test_namespaced_plugin_kind_routes_the_matching_metrics_collector():
    capabilities = (
        PublisherPluginCapability.HEALTH_CHECK,
        PublisherPluginCapability.LOGIN,
        PublisherPluginCapability.METRICS,
        PublisherPluginCapability.PUBLISH,
    )
    entry_point = _entry_point(
        kind="metrics_zhihu",
        factory=_MetricsPluginPublisher,
        capabilities=capabilities,
    )
    registry = build_default_registry(
        config=_registry_config(allowlist=("acme-ai-ops:acme.zhihu",)),
        plugin_entry_points=[entry_point],
    )

    collector = registry.resolve_collector(Platform.ZHIHU, "metrics_zhihu")

    assert isinstance(collector, _MetricsPluginPublisher)


def test_metrics_capability_requires_a_real_async_collector_override():
    class _InheritedCollectorPublisher(_PluginPublisher):
        kind = "inherited_metrics"
        supports_metrics = True

    class _SyncCollectorPublisher(_PluginPublisher):
        kind = "sync_metrics"
        supports_metrics = True

        def collect_metrics(self, post_id, post_url, credential):
            return {"views": 1}

    capabilities = (
        PublisherPluginCapability.HEALTH_CHECK,
        PublisherPluginCapability.LOGIN,
        PublisherPluginCapability.METRICS,
        PublisherPluginCapability.PUBLISH,
    )

    for kind, factory in (
        ("inherited_metrics", _InheritedCollectorPublisher),
        ("sync_metrics", _SyncCollectorPublisher),
    ):
        report = validate_enabled_publisher_plugins(
            ("acme-ai-ops:acme.zhihu",),
            entry_points=[
                _entry_point(
                    kind=kind,
                    factory=factory,
                    capabilities=capabilities,
                )
            ],
        )

        assert report.loaded == ()
        assert report.errors[0].code is PublisherPluginErrorCode.CAPABILITY_MISMATCH


@pytest.mark.parametrize(
    "selector",
    ["*", "bare-name", "bad path:plugin", "dist:", "dist:UPPER"],
)
def test_inventory_rejects_unsafe_or_ambiguous_selectors(selector):
    with pytest.raises(ValueError):
        inspect_publisher_plugins((selector,), entry_points=[])


@pytest.mark.parametrize("distribution_version", [None, "", "1.2.3\nFORGED\x1b[2J"])
def test_enabled_plugin_requires_safe_distribution_version_before_factory(
    distribution_version,
):
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return _PluginPublisher()

    entry_point = _entry_point(factory=factory)
    entry_point.dist.version = distribution_version

    inventory = inspect_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )
    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert inventory.ok is False
    assert inventory.invalid_enabled == ("acme-ai-ops:acme.zhihu",)
    assert inventory.exit_code == 1
    assert report.loaded == ()
    assert report.errors[0].code is PublisherPluginErrorCode.METADATA_INVALID
    assert report.code_loaded is False
    assert entry_point.load_calls == 0
    assert factory_calls == 0
    assert "FORGED" not in str(report.to_dict())


def test_plugin_error_type_is_bounded_ascii_and_never_reflects_dynamic_name():
    secret = "token=must-not-leak"
    injected_error = type(f"RuntimeError\n{secret}\x1b[2J", (Exception,), {})
    entry_point = _FakeEntryPoint(
        distribution="acme-ai-ops",
        name="acme.zhihu",
        provider=injected_error("message is also private"),
    )

    report = validate_enabled_publisher_plugins(
        ("acme-ai-ops:acme.zhihu",),
        entry_points=[entry_point],
    )

    assert report.errors[0].exception_type == "Exception"
    assert secret not in str(report.to_dict())


def test_registry_factory_error_type_is_sanitized(caplog):
    secret = "token=must-not-leak"
    injected_error = type(f"RuntimeError\n{secret}\x1b[2J", (Exception,), {})

    def factory():
        raise injected_error("message is also private")

    registry = PublisherRegistry()
    registry.register(
        Platform.ZHIHU,
        factory,
        registration_id="test:injected-factory",
    )

    with pytest.raises(PublisherPluginResolutionError) as raised:
        registry.resolve(Platform.ZHIHU)

    assert raised.value.code == "publisher_factory_failed"
    assert caplog.records[-1].exception_type == "Exception"
    assert secret not in caplog.text
    assert secret not in str(raised.value)


def test_registry_build_error_type_is_sanitized(monkeypatch):
    secret = "token=must-not-leak"
    injected_error = type(f"RuntimeError\n{secret}\x1b[2J", (Exception,), {})

    def fail_validation(*args, **kwargs):
        raise injected_error("message is also private")

    monkeypatch.setattr(
        "ai_ops.publishers.registry.validate_enabled_publisher_plugins",
        fail_validation,
    )

    registry = build_default_registry(
        config=_registry_config(allowlist=("acme-ai-ops:acme.zhihu",)),
        plugin_entry_points=[],
    )
    report = registry.plugin_validation_report

    assert report.ok is False
    assert report.errors[0].exception_type == "Exception"
    assert secret not in str(report.to_dict())
