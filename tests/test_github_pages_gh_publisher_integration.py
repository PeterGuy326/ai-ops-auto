"""Offline Publisher-level contracts for GitHub Pages delivery proofs."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import ContentType
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers import github_pages as github_pages_module
from ai_ops.publishers.github_pages import GitHubPagesPublisher, _CommandResult
from ai_ops.publishers.github_pages_gh import GhProofResult
from ai_ops.runtime.receipts import read_publish_receipt, receipt_path


PREFLIGHT_SHA = "a" * 40
COMMIT_SHA = "b" * 40
THIRD_PARTY_SHA = "c" * 40
JOB_ID = 41
OPERATION_ID = "d" * 32


def _remote_result(sha: str) -> _CommandResult:
    return _CommandResult(
        started=True,
        returncode=0,
        stdout=f"{sha}\trefs/heads/pages\n",
    )


def _content() -> PublishContent:
    return PublishContent(
        title="Contract proof",
        body="offline body",
        content_type=ContentType.LONG_ARTICLE,
        job_id=JOB_ID,
        operation_id=OPERATION_ID,
    )


@pytest.fixture
def publisher_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    repo = tmp_path / "blog"
    repo.mkdir()
    (repo / ".git").mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    data_dir = tmp_path / "runtime"

    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "github_pages_path", repo)
    monkeypatch.setattr(settings, "github_pages_engine", "hexo")
    monkeypatch.setattr(settings, "github_pages_posts_dir", "source/_posts")
    monkeypatch.setattr(settings, "github_pages_images_dir", "source/img")
    monkeypatch.setattr(settings, "github_pages_asset_root", assets)
    monkeypatch.setattr(settings, "agent_asset_vault_root", assets)
    monkeypatch.setattr(settings, "github_pages_max_image_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "github_pages_max_total_image_bytes", 2 * 1024 * 1024)
    monkeypatch.setattr(settings, "github_pages_build_tool", "pnpm")
    monkeypatch.setattr(settings, "github_pages_build_timeout_seconds", 10)
    monkeypatch.setattr(settings, "github_pages_git_timeout_seconds", 10)
    monkeypatch.setattr(settings, "github_pages_lock_timeout_seconds", 1)
    monkeypatch.setattr(settings, "github_pages_remote", "origin")
    monkeypatch.setattr(settings, "github_pages_branch", "pages")
    monkeypatch.setattr(settings, "github_pages_base_url", "https://owner.github.io/site")
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", False)
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    return repo, data_dir


class ReceiptObservingVerifier:
    def __init__(
        self,
        data_dir: Path,
        *,
        deployment: GhProofResult = GhProofResult(True),
        site: GhProofResult = GhProofResult(True),
        readback: GhProofResult = GhProofResult(True),
    ) -> None:
        self.data_dir = data_dir
        self.deployment = deployment
        self.site = site
        self.readback = readback
        self.accepted_receipt: dict | None = None
        self.deployed_receipt: dict | None = None
        self.deployment_sha: str | None = None
        self.readback_url: str | None = None
        self.readback_marker: str | None = None

    async def preflight(self, *, remote_url: str) -> GhProofResult:
        assert remote_url == "git@github.com:owner/site.git"
        return GhProofResult(True)

    async def wait_for_deployment(self, commit_sha: str) -> GhProofResult:
        self.accepted_receipt = read_publish_receipt(
            JOB_ID,
            OPERATION_ID,
            data_dir=self.data_dir,
        )
        self.deployment_sha = commit_sha
        return self.deployment

    async def confirm_site(self) -> GhProofResult:
        return self.site

    async def wait_for_readback(self, *, article_url: str, marker: str) -> GhProofResult:
        self.deployed_receipt = read_publish_receipt(
            JOB_ID,
            OPERATION_ID,
            data_dir=self.data_dir,
        )
        self.readback_url = article_url
        self.readback_marker = marker
        return self.readback


class OfflinePublisher(GitHubPagesPublisher):
    def __init__(
        self,
        *,
        baseline_sha: str = PREFLIGHT_SHA,
        push_result: _CommandResult | None = None,
        remote_after_push_sha: str = COMMIT_SHA,
        verifier: ReceiptObservingVerifier | None = None,
    ) -> None:
        self.baseline_sha = baseline_sha
        self.push_result = push_result or _CommandResult(started=True, returncode=0)
        self.remote_after_push_sha = remote_after_push_sha
        self.verifier = verifier
        self.commands: list[list[str]] = []
        self.staged_paths: list[str] = []
        self.rev_parse_calls = 0
        self.ls_remote_calls = 0

    async def _prepare_gh_verifier(self, repo, remote, branch, *, remote_url):
        del repo
        assert remote == "origin"
        assert branch == "pages"
        assert remote_url == "git@github.com:owner/site.git"
        assert self.verifier is not None
        return self.verifier, remote_url, None

    async def _committed_artifacts_match(self, **_kwargs) -> bool:
        return True

    async def _run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> _CommandResult:
        del cwd, timeout_seconds
        self.commands.append(list(argv))
        if argv == ["git", "remote"]:
            return _CommandResult(started=True, returncode=0, stdout="origin\n")
        if argv == ["git", "remote", "get-url", "--push", "--all", "origin"]:
            return _CommandResult(
                started=True,
                returncode=0,
                stdout="git@github.com:owner/site.git\n",
            )
        if "ls-remote" in argv and "--get-url" in argv:
            return _CommandResult(
                started=True,
                returncode=0,
                stdout="git@github.com:owner/site.git\n",
            )
        if argv == ["git", "status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResult(started=True, returncode=0)
        if argv == ["git", "rev-parse", "--verify", "HEAD"]:
            self.rev_parse_calls += 1
            sha = PREFLIGHT_SHA if self.rev_parse_calls == 1 else COMMIT_SHA
            return _CommandResult(started=True, returncode=0, stdout=f"{sha}\n")
        if "ls-remote" in argv and "--exit-code" in argv:
            self.ls_remote_calls += 1
            sha = self.baseline_sha if self.ls_remote_calls == 1 else self.remote_after_push_sha
            return _remote_result(sha)
        if argv[:2] == ["pnpm", "hexo"]:
            return _CommandResult(started=True, returncode=0)
        if argv[:3] == ["git", "add", "--"]:
            self.staged_paths = argv[3:]
            return _CommandResult(started=True, returncode=0)
        if "commit" in argv:
            return _CommandResult(started=True, returncode=0)
        if argv[:2] == ["git", "diff-tree"]:
            return _CommandResult(
                started=True,
                returncode=0,
                stdout="\0".join(self.staged_paths) + "\0",
            )
        if argv[:2] == ["git", "rev-list"]:
            return _CommandResult(
                started=True,
                returncode=0,
                stdout=f"{COMMIT_SHA} {PREFLIGHT_SHA}\n",
            )
        if argv and argv[0] == "git" and "push" in argv:
            return self.push_result
        raise AssertionError(f"unexpected offline command: {argv!r}")


def _command_started(publisher: OfflinePublisher, prefix: list[str]) -> bool:
    return any(command[: len(prefix)] == prefix for command in publisher.commands)


@pytest.mark.asyncio
async def test_remote_baseline_mismatch_stops_before_write_or_build(
    publisher_environment: tuple[Path, Path],
) -> None:
    repo, _data_dir = publisher_environment
    publisher = OfflinePublisher(baseline_sha=THIRD_PARTY_SHA)

    result = await publisher.publish(1, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.raw_response == {"stage": "preflight"}
    assert not (repo / "source").exists()
    assert not _command_started(publisher, ["pnpm", "hexo"])
    assert not _command_started(publisher, ["git", "add"])
    assert not any("push" in command for command in publisher.commands)


@pytest.mark.asyncio
async def test_started_push_failure_with_old_remote_is_still_uncertain_due_to_aba(
    publisher_environment: tuple[Path, Path],
) -> None:
    _repo, data_dir = publisher_environment
    publisher = OfflinePublisher(
        push_result=_CommandResult(started=True, returncode=1),
        remote_after_push_sha=PREFLIGHT_SHA,
    )

    result = await publisher.publish(1, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.retryable is False
    assert result.outcome_uncertain is True
    assert result.raw_response == {"stage": "push", "commit_sha": COMMIT_SHA}
    assert read_publish_receipt(JOB_ID, OPERATION_ID, data_dir=data_dir) is None


@pytest.mark.asyncio
async def test_post_push_third_sha_is_outcome_uncertain(
    publisher_environment: tuple[Path, Path],
) -> None:
    _repo, data_dir = publisher_environment
    publisher = OfflinePublisher(
        push_result=_CommandResult(started=True, returncode=1),
        remote_after_push_sha=THIRD_PARTY_SHA,
    )

    result = await publisher.publish(1, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.retryable is False
    assert result.outcome_uncertain is True
    assert result.raw_response == {"stage": "push", "commit_sha": COMMIT_SHA}
    assert read_publish_receipt(JOB_ID, OPERATION_ID, data_dir=data_dir) is None


@pytest.mark.asyncio
async def test_accepted_receipt_exists_before_deployment_wait(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, data_dir = publisher_environment
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", True)
    verifier = ReceiptObservingVerifier(
        data_dir,
        deployment=GhProofResult(False, "deployment pending", outcome_uncertain=True),
    )
    publisher = OfflinePublisher(verifier=verifier)

    result = await publisher.publish(1, {}, _content())

    assert result.success is False
    assert verifier.deployment_sha == COMMIT_SHA
    assert verifier.accepted_receipt is not None
    assert verifier.accepted_receipt["raw_response"]["state"] == "accepted"
    assert verifier.accepted_receipt["platform_post_id"] == COMMIT_SHA
    assert verifier.accepted_receipt["platform_url"] == (
        "https://owner.github.io/site/contract-proof/"
    )


@pytest.mark.asyncio
async def test_accepted_receipt_failure_returns_before_deployment_wait(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, data_dir = publisher_environment
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", True)
    monkeypatch.setattr(github_pages_module, "write_publish_receipt", lambda **_kwargs: None)
    verifier = ReceiptObservingVerifier(data_dir)
    publisher = OfflinePublisher(verifier=verifier)

    result = await publisher.publish(1, {}, _content())

    assert result.success is False
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.outcome_uncertain is False
    assert result.raw_response["state"] == "accepted"
    assert "durable receipt" in (result.error or "")
    assert verifier.deployment_sha is None


@pytest.mark.asyncio
async def test_successful_deployment_and_readback_replace_receipt_with_verified_state(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, data_dir = publisher_environment
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", True)
    verifier = ReceiptObservingVerifier(data_dir)
    publisher = OfflinePublisher(verifier=verifier)

    result = await publisher.publish(1, {}, _content())
    receipt = read_publish_receipt(JOB_ID, OPERATION_ID, data_dir=data_dir)

    assert result.success is True
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.raw_response["state"] == "verified"
    assert result.raw_response["marker_sha256"] == verifier.readback_marker
    assert len(result.raw_response["marker_sha256"]) == 64
    assert verifier.readback_url == result.platform_url
    assert verifier.accepted_receipt is not None
    assert verifier.accepted_receipt["raw_response"]["state"] == "accepted"
    assert verifier.deployed_receipt is not None
    assert verifier.deployed_receipt["raw_response"]["state"] == "deployed"
    assert receipt is not None
    assert receipt["raw_response"]["state"] == "verified"
    assert receipt["platform_post_id"] == COMMIT_SHA
    assert receipt["platform_url"] == result.platform_url
    assert "marker_sha256" not in receipt["raw_response"]
    assert list(receipt_path(JOB_ID, OPERATION_ID, data_dir=data_dir).parent.glob("*.json")) == [
        receipt_path(JOB_ID, OPERATION_ID, data_dir=data_dir)
    ]


@pytest.mark.parametrize(
    ("deployment", "expected_uncertain"),
    [
        (GhProofResult(False, "terminal deployment failure"), False),
        (GhProofResult(False, "deployment status unknown", outcome_uncertain=True), True),
    ],
)
@pytest.mark.asyncio
async def test_deployment_failure_preserves_accepted_identity_without_retry(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    deployment: GhProofResult,
    expected_uncertain: bool,
) -> None:
    _repo, data_dir = publisher_environment
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", True)
    verifier = ReceiptObservingVerifier(data_dir, deployment=deployment)
    publisher = OfflinePublisher(verifier=verifier)

    result = await publisher.publish(1, {}, _content())
    receipt = read_publish_receipt(JOB_ID, OPERATION_ID, data_dir=data_dir)

    assert result.success is False
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.outcome_uncertain is expected_uncertain
    assert result.platform_post_id == COMMIT_SHA
    assert result.raw_response["state"] == "accepted"
    assert receipt is not None
    assert receipt["effect_applied"] is True
    assert receipt["outcome_uncertain"] is expected_uncertain
    assert receipt["raw_response"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_readback_failure_preserves_deployed_identity_without_retry(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, data_dir = publisher_environment
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", True)
    verifier = ReceiptObservingVerifier(
        data_dir,
        readback=GhProofResult(False, "public marker missing", outcome_uncertain=True),
    )
    publisher = OfflinePublisher(verifier=verifier)

    result = await publisher.publish(1, {}, _content())
    receipt = read_publish_receipt(JOB_ID, OPERATION_ID, data_dir=data_dir)

    assert result.success is False
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.outcome_uncertain is True
    assert result.platform_post_id == COMMIT_SHA
    assert result.raw_response["state"] == "deployed"
    assert verifier.deployed_receipt is not None
    assert verifier.deployed_receipt["raw_response"]["state"] == "deployed"
    assert receipt is not None
    assert receipt["effect_applied"] is True
    assert receipt["outcome_uncertain"] is True
    assert receipt["raw_response"]["state"] == "deployed"


@pytest.mark.parametrize(
    ("raised_stage", "expected_state"),
    [
        ("deployment", "accepted"),
        ("site", "deployed"),
        ("readback", "deployed"),
    ],
)
@pytest.mark.asyncio
async def test_verifier_exception_never_downgrades_durable_delivery_identity(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    raised_stage: str,
    expected_state: str,
) -> None:
    _repo, data_dir = publisher_environment
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", True)

    class RaisingVerifier(ReceiptObservingVerifier):
        async def wait_for_deployment(self, commit_sha: str) -> GhProofResult:
            result = await super().wait_for_deployment(commit_sha)
            if raised_stage == "deployment":
                raise RuntimeError("must not escape or be persisted")
            return result

        async def confirm_site(self) -> GhProofResult:
            if raised_stage == "site":
                raise RuntimeError("must not escape or be persisted")
            return await super().confirm_site()

        async def wait_for_readback(self, *, article_url: str, marker: str) -> GhProofResult:
            if raised_stage == "readback":
                raise RuntimeError("must not escape or be persisted")
            return await super().wait_for_readback(article_url=article_url, marker=marker)

    verifier = RaisingVerifier(data_dir)
    publisher = OfflinePublisher(verifier=verifier)

    result = await publisher.publish(1, {}, _content())
    receipt = read_publish_receipt(JOB_ID, OPERATION_ID, data_dir=data_dir)

    assert result.success is False
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.outcome_uncertain is True
    assert result.platform_post_id == COMMIT_SHA
    assert result.platform_url == "https://owner.github.io/site/contract-proof/"
    assert result.raw_response["state"] == expected_state
    assert "must not escape" not in result.model_dump_json()
    assert receipt is not None
    assert receipt["effect_applied"] is True
    assert receipt["outcome_uncertain"] is True
    assert receipt["platform_post_id"] == COMMIT_SHA
    assert receipt["platform_url"] == result.platform_url
    assert receipt["raw_response"]["state"] == expected_state


@pytest.mark.asyncio
async def test_repository_path_configuration_errors_do_not_leak_absolute_paths(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo, _data_dir = publisher_environment
    outside = tmp_path / "private-tenant-root" / "posts"
    monkeypatch.setattr(settings, "github_pages_posts_dir", str(outside))
    publisher = OfflinePublisher()

    result = await publisher.publish(1, {}, _content())
    rendered = result.model_dump_json()

    assert result.success is False
    assert result.effect_applied is False
    assert result.raw_response == {"stage": "preflight"}
    assert str(tmp_path) not in rendered
    assert str(outside) not in rendered
    assert publisher.commands == []


@pytest.mark.asyncio
async def test_missing_repository_error_does_not_leak_resolved_host_path(
    publisher_environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo, _data_dir = publisher_environment
    missing = tmp_path / "private-tenant-root" / "missing-blog"
    monkeypatch.setattr(settings, "github_pages_path", missing)

    result = await OfflinePublisher().publish(1, {}, _content())
    rendered = result.model_dump_json()

    assert result.success is False
    assert result.raw_response == {"stage": "preflight"}
    assert str(tmp_path) not in rendered
    assert str(missing) not in rendered
