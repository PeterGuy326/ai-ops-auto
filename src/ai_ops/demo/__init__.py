"""Offline, credential-free product demonstration helpers.

The demo package is intentionally separate from the production publisher
registry.  Importing it never enables a platform adapter or changes runtime
configuration.
"""

from .backends import FakeMetricsBackend, FakePublisher
from .runner import (
    DemoRunSummary,
    build_dry_run_plan,
    run_offline_demo,
)

__all__ = [
    "DemoRunSummary",
    "FakeMetricsBackend",
    "FakePublisher",
    "build_dry_run_plan",
    "run_offline_demo",
]
