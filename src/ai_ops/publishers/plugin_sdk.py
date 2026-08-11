"""Versioned, deny-by-default SDK for third-party Publisher adapters.

Discovery is intentionally split from validation:

* inventory only reads installed distribution metadata and never imports plugin code;
* validation imports only exact ``distribution:entry-point`` selectors that an
  operator placed in the allowlist;
* factories are required to be side-effect free.  External I/O belongs in the
  async Publisher methods, never module import or construction.

Third-party plugins execute in the control-plane process and therefore share its
permissions.  The SDK is a compatibility and policy boundary, not a sandbox.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
import inspect
import re
from typing import Any

from ..core.enums import Platform, PublisherKind
from .base import PublisherBase


PUBLISHER_PLUGIN_API_VERSION = 1
PUBLISHER_PLUGIN_ENTRY_POINT_GROUP = "ai_ops.publishers.v1"

_PLUGIN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_PUBLISHER_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_UPSTREAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$")
_EXCEPTION_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESOLUTION_CODE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,127}$")
_PLUGIN_SELECTOR_ATTRIBUTE = "_ai_ops_publisher_plugin_selector"


def safe_plugin_exception_type(exc: BaseException) -> str:
    """Return a bounded ASCII exception identity for plugin-facing output."""

    exception_type = type(exc).__name__
    if not isinstance(exception_type, str) or not _EXCEPTION_TYPE_RE.fullmatch(exception_type):
        return "Exception"
    return exception_type


class PublisherPluginCapability(StrEnum):
    """Capabilities declared by a Publisher plugin compatibility manifest."""

    LOGIN = "login"
    PUBLISH = "publish"
    HEALTH_CHECK = "health_check"
    METRICS = "metrics"
    AGENT_CONTRACT_RENDERER = "agent_contract_renderer"


_REQUIRED_CAPABILITIES = frozenset(
    {
        PublisherPluginCapability.LOGIN,
        PublisherPluginCapability.PUBLISH,
        PublisherPluginCapability.HEALTH_CHECK,
    }
)


def normalize_distribution_name(value: str) -> str:
    """Return the PEP 503 comparison form without importing ``packaging``."""

    normalized = re.sub(r"[-_.]+", "-", value).lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        return ""
    return normalized


def publisher_kind_value(value: PublisherKind | str) -> str:
    """Normalize built-in and third-party Publisher kind identities."""

    return value.value if isinstance(value, PublisherKind) else str(value)


def is_publisher_plugin_instance(publisher: object) -> bool:
    """Return whether an instance came through the validated plugin loader."""

    return isinstance(getattr(publisher, _PLUGIN_SELECTOR_ATTRIBUTE, None), str)


def plugin_selector(distribution_name: str, plugin_id: str) -> str:
    """Build the exact allowlist selector for one installed entry point."""

    normalized_distribution = normalize_distribution_name(distribution_name)
    if not normalized_distribution or not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("distribution or plugin id has an invalid format")
    return f"{normalized_distribution}:{plugin_id}"


def parse_plugin_selector(value: str) -> tuple[str, str]:
    """Parse a deny-by-default ``distribution:entry-point`` selector."""

    if value == "*" or value.count(":") != 1:
        raise ValueError("plugin selector must be distribution:entry-point and cannot be '*'")
    distribution_name, plugin_id = value.split(":", 1)
    normalized_distribution = normalize_distribution_name(distribution_name)
    if not normalized_distribution or not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("plugin selector contains an invalid distribution or entry-point name")
    return normalized_distribution, plugin_id


def _normalize_enabled_selectors(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(plugin_selector(*parse_plugin_selector(value)) for value in values))
    if len(set(normalized)) != len(normalized):
        raise ValueError("plugin selectors must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class PublisherPluginManifest:
    """Credential-free compatibility manifest for one Publisher implementation."""

    plugin_id: str
    plugin_version: str
    api_version: int
    platform: Platform
    publisher_kind: PublisherKind | str
    adapter_version: str
    capabilities: tuple[PublisherPluginCapability | str, ...]
    priority: int = 100
    renderer_id: str | None = None
    contract_version: str | None = None
    upstream_name: str | None = None
    upstream_version: str | None = None

    def __post_init__(self) -> None:
        if not _PLUGIN_ID_RE.fullmatch(self.plugin_id):
            raise ValueError("plugin_id has an invalid format")
        if not isinstance(self.api_version, int) or isinstance(self.api_version, bool):
            raise ValueError("api_version must be an integer")
        if self.api_version < 1:
            raise ValueError("api_version must be positive")
        try:
            normalized_platform = Platform(self.platform)
        except (TypeError, ValueError) as exc:
            raise ValueError("platform is not supported by this control-plane version") from exc
        object.__setattr__(self, "platform", normalized_platform)

        kind = publisher_kind_value(self.publisher_kind)
        if not _PUBLISHER_KIND_RE.fullmatch(kind):
            raise ValueError("publisher_kind must be a stable lowercase identifier")
        if kind in {item.value for item in PublisherKind}:
            raise ValueError("third-party plugins must use a distinct publisher_kind")
        object.__setattr__(self, "publisher_kind", kind)

        for label, value in (
            ("plugin_version", self.plugin_version),
            ("adapter_version", self.adapter_version),
        ):
            if not _VERSION_RE.fullmatch(value):
                raise ValueError(f"{label} has an invalid format")
        if not 1 <= self.priority <= 1000:
            raise ValueError("priority must be between 1 and 1000")

        try:
            normalized_capabilities = tuple(
                PublisherPluginCapability(capability) for capability in self.capabilities
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("capabilities contain an unsupported value") from exc
        if len(set(normalized_capabilities)) != len(normalized_capabilities):
            raise ValueError("capabilities must be unique")
        canonical_capabilities = tuple(sorted(normalized_capabilities, key=lambda item: item.value))
        if normalized_capabilities != canonical_capabilities:
            raise ValueError("capabilities must be sorted by their stable value")
        if not _REQUIRED_CAPABILITIES.issubset(normalized_capabilities):
            raise ValueError("login, publish, and health_check capabilities are required")
        object.__setattr__(self, "capabilities", normalized_capabilities)

        has_renderer = PublisherPluginCapability.AGENT_CONTRACT_RENDERER in normalized_capabilities
        renderer_fields_present = bool(self.renderer_id) and bool(self.contract_version)
        if has_renderer != renderer_fields_present:
            raise ValueError(
                "renderer_id and contract_version are required exactly when the "
                "agent_contract_renderer capability is declared"
            )
        if self.renderer_id is not None and not _PLUGIN_ID_RE.fullmatch(self.renderer_id):
            raise ValueError("renderer_id has an invalid format")
        if self.contract_version is not None and not _VERSION_RE.fullmatch(self.contract_version):
            raise ValueError("contract_version has an invalid format")
        has_upstream_name = self.upstream_name is not None
        has_upstream_version = self.upstream_version is not None
        if has_upstream_name != has_upstream_version:
            raise ValueError("upstream_name and upstream_version must be declared together")
        if self.upstream_name is not None and not _UPSTREAM_NAME_RE.fullmatch(self.upstream_name):
            raise ValueError("upstream_name has an invalid format")
        if self.upstream_version is not None and not _VERSION_RE.fullmatch(self.upstream_version):
            raise ValueError("upstream_version has an invalid format")

    def to_dict(self) -> dict[str, object]:
        """Return the stable, secret-free manifest projection."""

        return {
            "adapter_version": self.adapter_version,
            "api_version": self.api_version,
            "capabilities": [capability.value for capability in self.capabilities],
            "contract_version": self.contract_version,
            "platform": self.platform.value,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "priority": self.priority,
            "publisher_kind": str(self.publisher_kind),
            "renderer_id": self.renderer_id,
            "upstream_name": self.upstream_name,
            "upstream_version": self.upstream_version,
        }


@dataclass(frozen=True, slots=True)
class PublisherPlugin:
    """Object returned by a Publisher entry-point provider."""

    manifest: PublisherPluginManifest
    factory: Callable[[], PublisherBase]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PublisherPluginManifest):
            raise ValueError("manifest must be a PublisherPluginManifest")
        if not callable(self.factory):
            raise ValueError("factory must be callable")


class PublisherPluginErrorCode(StrEnum):
    MISSING = "missing_enabled_plugin"
    DUPLICATE = "duplicate_enabled_plugin"
    METADATA_INVALID = "distribution_metadata_invalid"
    LOAD_FAILED = "entry_point_load_failed"
    PROVIDER_INVALID = "provider_invalid"
    PROVIDER_FAILED = "provider_failed"
    MANIFEST_MISMATCH = "manifest_mismatch"
    API_INCOMPATIBLE = "api_incompatible"
    FACTORY_FAILED = "factory_failed"
    PUBLISHER_INVALID = "publisher_invalid"
    IDENTITY_MISMATCH = "publisher_identity_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    KIND_CONFLICT = "publisher_kind_conflict"


@dataclass(frozen=True, slots=True)
class PublisherPluginError:
    """Stable, redacted validation error safe for logs and JSON output."""

    selector: str
    code: PublisherPluginErrorCode
    exception_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "exception_type": self.exception_type,
            "selector": self.selector,
        }


class PublisherPluginResolutionError(RuntimeError):
    """Raised when configured plugin state makes Publisher routing unsafe."""

    def __init__(self, code: str = "publisher_plugin_configuration_invalid") -> None:
        if not isinstance(code, str) or not _RESOLUTION_CODE_RE.fullmatch(code):
            code = "publisher_plugin_configuration_invalid"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PublisherPluginEntryPoint:
    """Static installed-entry-point metadata; obtaining it does not load code."""

    selector: str
    plugin_id: str
    distribution_name: str
    distribution_version: str | None
    enabled: bool
    duplicate: bool

    def to_dict(self) -> dict[str, object]:
        status = "disabled"
        if self.duplicate:
            status = "duplicate"
        elif self.enabled and self.distribution_version is None:
            status = "invalid_metadata"
        elif self.enabled:
            status = "enabled"
        return {
            "distribution": self.distribution_name,
            "distribution_version": self.distribution_version,
            "duplicate": self.duplicate,
            "enabled": self.enabled,
            "plugin_id": self.plugin_id,
            "selector": self.selector,
            "status": status,
        }


@dataclass(frozen=True, slots=True)
class PublisherPluginInventory:
    """Side-effect-free inventory of installed and selected Publisher plugins."""

    entries: tuple[PublisherPluginEntryPoint, ...]
    enabled_selectors: tuple[str, ...]
    missing_enabled: tuple[str, ...]
    duplicate_enabled: tuple[str, ...]
    invalid_enabled: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_enabled and not self.duplicate_enabled and not self.invalid_enabled

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, object]:
        return {
            "code_loaded": False,
            "enabled_selectors": list(self.enabled_selectors),
            "entry_point_group": PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
            "exit_code": self.exit_code,
            "missing_enabled": list(self.missing_enabled),
            "duplicate_enabled": list(self.duplicate_enabled),
            "invalid_enabled": list(self.invalid_enabled),
            "ok": self.ok,
            "plugins": [entry.to_dict() for entry in self.entries],
            "schema_version": 1,
            "summary": {
                "enabled": len(self.enabled_selectors),
                "installed": len(self.entries),
                "invalid_enabled": (
                    len(self.missing_enabled)
                    + len(self.duplicate_enabled)
                    + len(self.invalid_enabled)
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class LoadedPublisherPlugin:
    """One enabled plugin whose provider, manifest, and factory were validated."""

    selector: str
    distribution_name: str
    distribution_version: str | None
    plugin: PublisherPlugin

    def to_dict(self) -> dict[str, object]:
        return {
            "distribution": self.distribution_name,
            "distribution_version": self.distribution_version,
            "manifest": self.plugin.manifest.to_dict(),
            "selector": self.selector,
            "status": "valid",
        }


@dataclass(frozen=True, slots=True)
class PublisherPluginValidationReport:
    """Result of loading and validating only explicitly enabled plugins."""

    enabled_selectors: tuple[str, ...]
    loaded: tuple[LoadedPublisherPlugin, ...]
    errors: tuple[PublisherPluginError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    @property
    def code_loaded(self) -> bool:
        """Whether at least one selected entry point was actually loaded."""

        pre_load_errors = {
            PublisherPluginErrorCode.MISSING,
            PublisherPluginErrorCode.DUPLICATE,
            PublisherPluginErrorCode.METADATA_INVALID,
        }
        return bool(self.loaded) or any(error.code not in pre_load_errors for error in self.errors)

    def to_dict(self) -> dict[str, object]:
        return {
            "code_loaded": self.code_loaded,
            "enabled_selectors": list(self.enabled_selectors),
            "entry_point_group": PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
            "errors": [error.to_dict() for error in self.errors],
            "exit_code": self.exit_code,
            "ok": self.ok,
            "plugins": [plugin.to_dict() for plugin in self.loaded],
            "schema_version": 1,
            "summary": {
                "enabled": len(self.enabled_selectors),
                "invalid": len(self.errors),
                "valid": len(self.loaded),
            },
        }


def _installed_entry_points(entry_points: Sequence[Any] | None = None) -> tuple[Any, ...]:
    if entry_points is not None:
        candidates: Iterable[Any] = entry_points
    else:
        discovered = metadata.entry_points()
        if hasattr(discovered, "select"):
            candidates = discovered.select(group=PUBLISHER_PLUGIN_ENTRY_POINT_GROUP)
        else:  # pragma: no cover - compatibility for old importlib.metadata backports
            candidates = discovered.get(PUBLISHER_PLUGIN_ENTRY_POINT_GROUP, ())
    return tuple(
        entry_point
        for entry_point in candidates
        if getattr(entry_point, "group", PUBLISHER_PLUGIN_ENTRY_POINT_GROUP)
        == PUBLISHER_PLUGIN_ENTRY_POINT_GROUP
    )


def _distribution_details(entry_point: Any) -> tuple[str, str | None]:
    distribution = getattr(entry_point, "dist", None)
    raw_name: str | None = None
    raw_version: str | None = None
    if distribution is not None:
        try:
            raw_name = distribution.metadata.get("Name")
        except Exception:
            raw_name = None
        try:
            candidate_version = distribution.version
            raw_version = candidate_version if isinstance(candidate_version, str) else None
        except Exception:
            raw_version = None
    # Real EntryPoint objects expose ``dist``. A missing/invalid name cannot be
    # safely selected because the allowlist binds both package and entry point.
    safe_version = raw_version if raw_version and _VERSION_RE.fullmatch(raw_version) else None
    return normalize_distribution_name(raw_name or ""), safe_version


def inspect_publisher_plugins(
    enabled_selectors: Sequence[str] = (),
    *,
    entry_points: Sequence[Any] | None = None,
) -> PublisherPluginInventory:
    """Inspect entry-point metadata without calling ``EntryPoint.load``."""

    normalized_enabled = _normalize_enabled_selectors(enabled_selectors)
    candidates = _installed_entry_points(entry_points)
    candidate_rows: list[tuple[Any, str, str, str | None]] = []
    for entry_point in candidates:
        distribution_name, distribution_version = _distribution_details(entry_point)
        plugin_id = str(getattr(entry_point, "name", ""))
        if not distribution_name or not _PLUGIN_ID_RE.fullmatch(plugin_id):
            continue
        candidate_rows.append(
            (
                entry_point,
                plugin_selector(distribution_name, plugin_id),
                distribution_name,
                distribution_version,
            )
        )
    counts = Counter(selector for _, selector, _, _ in candidate_rows)
    installed_selectors = set(counts)
    entries = tuple(
        PublisherPluginEntryPoint(
            selector=selector,
            plugin_id=str(entry_point.name),
            distribution_name=distribution_name,
            distribution_version=distribution_version,
            enabled=selector in normalized_enabled,
            duplicate=counts[selector] > 1,
        )
        for entry_point, selector, distribution_name, distribution_version in sorted(
            candidate_rows,
            key=lambda item: (item[1], item[3] or ""),
        )
    )
    missing = tuple(sorted(set(normalized_enabled) - installed_selectors))
    duplicate_enabled = tuple(
        sorted(selector for selector in normalized_enabled if counts[selector] > 1)
    )
    invalid_enabled = tuple(
        sorted(
            {
                selector
                for _, selector, _, distribution_version in candidate_rows
                if selector in normalized_enabled and distribution_version is None
            }
        )
    )
    return PublisherPluginInventory(
        entries=entries,
        enabled_selectors=normalized_enabled,
        missing_enabled=missing,
        duplicate_enabled=duplicate_enabled,
        invalid_enabled=invalid_enabled,
    )


def _error(
    selector: str,
    code: PublisherPluginErrorCode,
    exc: BaseException | None = None,
) -> PublisherPluginError:
    return PublisherPluginError(
        selector=selector,
        code=code,
        exception_type=safe_plugin_exception_type(exc) if exc is not None else None,
    )


def _is_async_callable(value: object) -> bool:
    """Return whether the runtime call contract produces an awaitable method."""

    if not callable(value):
        return False
    if inspect.iscoroutinefunction(value):
        return True
    return inspect.iscoroutinefunction(getattr(value, "__call__", None))


def _publisher_instance_error(
    selector: str,
    plugin: PublisherPlugin,
    publisher: object,
) -> PublisherPluginError | None:
    if not isinstance(publisher, PublisherBase):
        return _error(selector, PublisherPluginErrorCode.PUBLISHER_INVALID)

    manifest = plugin.manifest
    if publisher.platform != manifest.platform or publisher_kind_value(publisher.kind) != str(
        manifest.publisher_kind
    ):
        return _error(selector, PublisherPluginErrorCode.IDENTITY_MISMATCH)

    capabilities = set(manifest.capabilities)
    metrics_declared = PublisherPluginCapability.METRICS in capabilities
    renderer_declared = PublisherPluginCapability.AGENT_CONTRACT_RENDERER in capabilities
    if any(
        not _is_async_callable(getattr(publisher, method_name, None))
        for method_name in ("login", "publish", "health_check")
    ):
        return _error(selector, PublisherPluginErrorCode.CAPABILITY_MISMATCH)
    if bool(getattr(publisher, "supports_metrics", False)) != metrics_declared:
        return _error(selector, PublisherPluginErrorCode.CAPABILITY_MISMATCH)
    descriptor = publisher.agent_contract_renderer_descriptor
    if (descriptor is not None) != renderer_declared:
        return _error(selector, PublisherPluginErrorCode.CAPABILITY_MISMATCH)

    if metrics_declared and (
        type(publisher).collect_metrics is PublisherBase.collect_metrics
        or not _is_async_callable(getattr(publisher, "collect_metrics", None))
    ):
        return _error(selector, PublisherPluginErrorCode.CAPABILITY_MISMATCH)

    if renderer_declared:
        assert descriptor is not None
        if (
            type(publisher).render_agent_contract_payload
            is PublisherBase.render_agent_contract_payload
            or not callable(getattr(publisher, "render_agent_contract_payload", None))
        ):
            return _error(selector, PublisherPluginErrorCode.CAPABILITY_MISMATCH)
        descriptor_kind = publisher_kind_value(descriptor.publisher_kind)
        if (
            descriptor.platform != manifest.platform
            or descriptor_kind != str(manifest.publisher_kind)
            or descriptor.renderer_id != manifest.renderer_id
            or descriptor.contract_version != manifest.contract_version
            or descriptor.adapter_version != manifest.adapter_version
        ):
            return _error(selector, PublisherPluginErrorCode.MANIFEST_MISMATCH)
        try:
            # Import lazily so the public Publisher SDK remains independent of
            # the Agent service during ordinary plugin author imports.
            from ..agent_contract.schemas import validate_renderer_contract

            descriptor_material = descriptor.digest_material()
            renderer = validate_renderer_contract(
                descriptor_material,
                expected_platform=manifest.platform,
                expected_publisher_kind=str(manifest.publisher_kind),
            )
            if renderer.model_dump(mode="json") != descriptor_material:
                raise ValueError("renderer descriptor is not canonical")
        except (Exception, SystemExit):
            return _error(selector, PublisherPluginErrorCode.CAPABILITY_MISMATCH)
    return None


def _validate_publisher_factory(
    selector: str,
    plugin: PublisherPlugin,
) -> PublisherPluginError | None:
    try:
        publisher = plugin.factory()
    except (Exception, SystemExit) as exc:
        # SystemExit from third-party constructors must not terminate doctor or
        # control-plane import. Operator interrupts still propagate.
        return _error(selector, PublisherPluginErrorCode.FACTORY_FAILED, exc)
    return _publisher_instance_error(selector, plugin, publisher)


def instantiate_validated_publisher(
    selector: str,
    plugin: PublisherPlugin,
) -> PublisherBase:
    """Construct and revalidate a plugin Publisher on every registry resolve."""

    try:
        publisher = plugin.factory()
    except (Exception, SystemExit):
        raise PublisherPluginResolutionError("publisher_factory_failed") from None
    error = _publisher_instance_error(selector, plugin, publisher)
    if error is not None:
        raise PublisherPluginResolutionError(error.code.value)
    assert isinstance(publisher, PublisherBase)
    object.__setattr__(publisher, _PLUGIN_SELECTOR_ATTRIBUTE, selector)
    return publisher


def validate_enabled_publisher_plugins(
    enabled_selectors: Sequence[str] = (),
    *,
    entry_points: Sequence[Any] | None = None,
) -> PublisherPluginValidationReport:
    """Load and validate only exact selectors explicitly enabled by an operator."""

    normalized_enabled = _normalize_enabled_selectors(enabled_selectors)
    if not normalized_enabled:
        return PublisherPluginValidationReport((), (), ())

    candidates = _installed_entry_points(entry_points)
    by_selector: dict[str, list[Any]] = {}
    for entry_point in candidates:
        distribution_name, _ = _distribution_details(entry_point)
        plugin_id = str(getattr(entry_point, "name", ""))
        if not distribution_name or not _PLUGIN_ID_RE.fullmatch(plugin_id):
            continue
        selector = plugin_selector(distribution_name, plugin_id)
        by_selector.setdefault(selector, []).append(entry_point)

    loaded: list[LoadedPublisherPlugin] = []
    errors: list[PublisherPluginError] = []
    for selector in normalized_enabled:
        matches = by_selector.get(selector, [])
        if not matches:
            errors.append(_error(selector, PublisherPluginErrorCode.MISSING))
            continue
        if len(matches) != 1:
            errors.append(_error(selector, PublisherPluginErrorCode.DUPLICATE))
            continue
        entry_point = matches[0]
        distribution_name, distribution_version = _distribution_details(entry_point)
        if distribution_version is None:
            errors.append(_error(selector, PublisherPluginErrorCode.METADATA_INVALID))
            continue
        try:
            provider = entry_point.load()
        except (Exception, SystemExit) as exc:
            errors.append(_error(selector, PublisherPluginErrorCode.LOAD_FAILED, exc))
            continue
        if not callable(provider):
            errors.append(_error(selector, PublisherPluginErrorCode.PROVIDER_INVALID))
            continue
        try:
            plugin = provider()
        except (Exception, SystemExit) as exc:
            errors.append(_error(selector, PublisherPluginErrorCode.PROVIDER_FAILED, exc))
            continue
        if not isinstance(plugin, PublisherPlugin):
            errors.append(_error(selector, PublisherPluginErrorCode.PROVIDER_INVALID))
            continue

        expected_plugin_id = parse_plugin_selector(selector)[1]
        if plugin.manifest.plugin_id != expected_plugin_id:
            errors.append(_error(selector, PublisherPluginErrorCode.MANIFEST_MISMATCH))
            continue
        if plugin.manifest.api_version != PUBLISHER_PLUGIN_API_VERSION:
            errors.append(_error(selector, PublisherPluginErrorCode.API_INCOMPATIBLE))
            continue

        if plugin.manifest.plugin_version != distribution_version:
            errors.append(_error(selector, PublisherPluginErrorCode.MANIFEST_MISMATCH))
            continue

        instance_error = _validate_publisher_factory(selector, plugin)
        if instance_error is not None:
            errors.append(instance_error)
            continue
        loaded.append(
            LoadedPublisherPlugin(
                selector=selector,
                distribution_name=distribution_name,
                distribution_version=distribution_version,
                plugin=plugin,
            )
        )

    plugin_id_counts = Counter(item.plugin.manifest.plugin_id for item in loaded)
    duplicate_id_selectors = {
        item.selector for item in loaded if plugin_id_counts[item.plugin.manifest.plugin_id] > 1
    }
    if duplicate_id_selectors:
        errors.extend(
            _error(selector, PublisherPluginErrorCode.MANIFEST_MISMATCH)
            for selector in sorted(duplicate_id_selectors)
        )
        loaded = [item for item in loaded if item.selector not in duplicate_id_selectors]

    identity_counts = Counter(
        (item.plugin.manifest.platform, str(item.plugin.manifest.publisher_kind)) for item in loaded
    )
    conflicting_selectors = {
        item.selector
        for item in loaded
        if identity_counts[
            (item.plugin.manifest.platform, str(item.plugin.manifest.publisher_kind))
        ]
        > 1
    }
    if conflicting_selectors:
        errors.extend(
            _error(selector, PublisherPluginErrorCode.KIND_CONFLICT)
            for selector in sorted(conflicting_selectors)
        )
        loaded = [item for item in loaded if item.selector not in conflicting_selectors]

    return PublisherPluginValidationReport(
        enabled_selectors=normalized_enabled,
        loaded=tuple(loaded),
        errors=tuple(errors),
    )


__all__ = [
    "LoadedPublisherPlugin",
    "PUBLISHER_PLUGIN_API_VERSION",
    "PUBLISHER_PLUGIN_ENTRY_POINT_GROUP",
    "PublisherPlugin",
    "PublisherPluginCapability",
    "PublisherPluginEntryPoint",
    "PublisherPluginError",
    "PublisherPluginErrorCode",
    "PublisherPluginInventory",
    "PublisherPluginManifest",
    "PublisherPluginResolutionError",
    "PublisherPluginValidationReport",
    "inspect_publisher_plugins",
    "instantiate_validated_publisher",
    "is_publisher_plugin_instance",
    "normalize_distribution_name",
    "parse_plugin_selector",
    "plugin_selector",
    "publisher_kind_value",
    "safe_plugin_exception_type",
    "validate_enabled_publisher_plugins",
]
