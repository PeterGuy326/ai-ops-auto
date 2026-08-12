from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
from PIL import Image

from ai_ops.config import settings
from ai_ops.core.enums import ContentType
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers import github_pages as github_pages_module
from ai_ops.publishers.github_pages import GitHubPagesPublisher, _CommandResult


COMMIT_SHA = "a" * 40


def _content(
    *,
    title: str = "安全发布",
    body: str = "正文",
    images: list[str] | None = None,
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.LONG_ARTICLE,
        images=images or [],
        tags=tags or [],
        extra=extra or {},
    )


@pytest.fixture
def blog_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "blog"
    repo.mkdir()
    (repo / ".git").mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
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
    monkeypatch.setattr(settings, "github_pages_base_url", "https://example.test")
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", False)
    monkeypatch.setattr(settings, "github_pages_dry_run", True)
    return repo


def _write_image(path: Path, *, image_format: str = "JPEG", size: tuple[int, int] = (4, 4)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=(25, 50, 75)).save(path, format=image_format)


class FakeCommandPublisher(GitHubPagesPublisher):
    def __init__(
        self,
        *,
        status: str = "",
        push: _CommandResult | None = None,
        baseline: _CommandResult | None = None,
        verification: _CommandResult | None = None,
        cancel_push: bool = False,
        commit_paths: list[str] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.status = status
        self.push_result = push or _CommandResult(started=True, returncode=0)
        self.baseline_result = baseline or _CommandResult(
            started=True,
            returncode=0,
            stdout=f"{COMMIT_SHA}\trefs/heads/pages\n",
        )
        self.verification_result = verification or _CommandResult(
            started=True,
            returncode=0,
            stdout=f"{COMMIT_SHA}\trefs/heads/pages\n",
        )
        self.cancel_push = cancel_push
        self.commit_paths = commit_paths
        self.staged_paths: list[str] = []
        self.ls_remote_calls = 0

    async def _run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> _CommandResult:
        del cwd, timeout_seconds
        self.commands.append(argv)
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
        if argv[:3] == ["git", "status", "--porcelain=v1"]:
            return _CommandResult(started=True, returncode=0, stdout=self.status)
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return _CommandResult(started=True, returncode=0, stdout=f"{COMMIT_SHA}\n")
        if argv[:3] == ["git", "add", "--"]:
            self.staged_paths = argv[3:]
            return _CommandResult(started=True, returncode=0)
        if argv[:2] == ["git", "diff-tree"]:
            paths = self.commit_paths if self.commit_paths is not None else self.staged_paths
            return _CommandResult(
                started=True,
                returncode=0,
                stdout="\0".join(paths) + "\0",
            )
        if argv[:2] == ["git", "rev-list"]:
            return _CommandResult(
                started=True,
                returncode=0,
                stdout=f"{COMMIT_SHA} {COMMIT_SHA}\n",
            )
        if argv and argv[0] == "git" and "push" in argv:
            if self.cancel_push:
                raise asyncio.CancelledError
            return self.push_result
        if "ls-remote" in argv and "--exit-code" in argv:
            self.ls_remote_calls += 1
            return self.baseline_result if self.ls_remote_calls == 1 else self.verification_result
        return _CommandResult(started=True, returncode=0)

    async def _committed_artifacts_match(self, **_kwargs) -> bool:
        return True


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout


@pytest.fixture
def real_blog_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "real-blog"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "ai-ops@example.test")
    _git(repo, "config", "user.name", "AI Ops Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "baseline")

    assets = tmp_path / "real-assets"
    assets.mkdir()
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
    monkeypatch.setattr(settings, "github_pages_base_url", "https://example.test")
    monkeypatch.setattr(settings, "github_pages_gh_verify_enabled", False)
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    return repo


class LocalGitFailurePublisher(GitHubPagesPublisher):
    """Use real local git for rollback assertions; fake only remote/build/failure."""

    def __init__(self, fail_at: str, on_failure=None) -> None:
        self.fail_at = fail_at
        self.on_failure = on_failure
        self.commands: list[list[str]] = []

    async def _run_argv(self, argv, *, cwd, timeout_seconds):
        self.commands.append(argv)
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
        if "ls-remote" in argv and "--exit-code" in argv:
            head = _git(cwd, "rev-parse", "HEAD").strip()
            return _CommandResult(
                started=True,
                returncode=0,
                stdout=f"{head}\trefs/heads/pages\n",
            )
        if argv[:2] == ["pnpm", "hexo"]:
            action = argv[2]
            if self.fail_at == "build" and action == "generate":
                if self.on_failure is not None:
                    self.on_failure()
                return _CommandResult(started=True, returncode=2)
            return _CommandResult(started=True, returncode=0)
        if argv[:3] == ["git", "add", "--"] and self.fail_at == "add":
            # Model a command that staged some/all exact paths before reporting
            # failure. Rollback must inspect and unstage only those paths.
            await super()._run_argv(argv, cwd=cwd, timeout_seconds=timeout_seconds)
            return _CommandResult(started=True, returncode=3)
        if "commit" in argv and self.fail_at == "commit":
            return _CommandResult(started=True, returncode=4)
        return await super()._run_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )


def _attach_bare_remote(repo: Path, remote: Path) -> str:
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git(repo, "branch", "-M", "pages")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--quiet", "-u", "origin", "pages")
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.asyncio
async def test_live_publish_requires_public_base_url_before_writes(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    monkeypatch.setattr(settings, "github_pages_base_url", "")
    publisher = FakeCommandPublisher()

    result = await publisher.publish(1, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert "GITHUB_PAGES_BASE_URL" in (result.error or "")
    assert publisher.commands == []
    assert not (blog_repo / "source/_posts/安全发布.md").exists()


@pytest.mark.asyncio
async def test_dry_run_has_no_effect_and_does_not_persist_content(blog_repo: Path) -> None:
    content = _content(title="绝密标题", body="绝密正文", extra={"description": "绝密描述"})

    result = await GitHubPagesPublisher().publish(0, {}, content)

    assert result.success is True
    assert result.effect_applied is False
    assert result.retryable is False
    assert not (blog_repo / "source").exists()
    serialized = json.dumps(result.raw_response, ensure_ascii=False)
    assert "绝密正文" not in serialized
    assert "绝密描述" not in serialized
    assert "preview" not in serialized
    assert "log" not in serialized


def test_frontmatter_quotes_untrusted_yaml_scalars() -> None:
    content = _content(
        title="标题\npublished: true # injected",
        tags=["tag\nadmin: true", "a: b"],
        extra={"description": "说明\nlayout: evil", "keywords": ["x:#", "y"]},
    )

    rendered = GitHubPagesPublisher()._render(
        content,
        ["category\npermalink: bad"],
        content.body,
    )
    lines = rendered.splitlines()

    assert json.loads(lines[1].removeprefix("title: ")) == content.title
    assert json.loads(lines[4].removeprefix("  - ")) == content.tags[0]
    assert json.loads(lines[8].removeprefix("description: ")) == content.extra["description"]
    assert "published: true # injected" not in lines
    assert "admin: true" not in lines
    assert "layout: evil" not in lines
    assert "permalink: bad" not in lines


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["../outside", "/tmp/outside", "."])
async def test_rejects_post_directory_outside_repo(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    monkeypatch.setattr(settings, "github_pages_posts_dir", configured)

    result = await GitHubPagesPublisher().publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.retryable is False
    assert result.raw_response == {"stage": "preflight"}


@pytest.mark.asyncio
async def test_rejects_existing_post_and_image_without_overwriting(blog_repo: Path) -> None:
    post = blog_repo / "source/_posts/安全发布.md"
    post.parent.mkdir(parents=True)
    post.write_text("keep", encoding="utf-8")

    result = await GitHubPagesPublisher().publish(0, {}, _content())

    assert result.success is False
    assert "拒绝覆盖" in (result.error or "")
    assert post.read_text(encoding="utf-8") == "keep"

    post.unlink()
    image = settings.github_pages_asset_root / "cover.jpg"
    _write_image(image)
    target = blog_repo / "source/img/安全发布/cover.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep")

    result = await GitHubPagesPublisher().publish(0, {}, _content(images=[str(image)]))

    assert result.success is False
    assert "拒绝覆盖" in (result.error or "")
    assert target.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_rejects_image_outside_controlled_asset_root(blog_repo: Path) -> None:
    outside = blog_repo.parent / "outside.jpg"
    _write_image(outside)

    result = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(outside)]),
    )

    assert result.success is False
    assert result.effect_applied is False
    assert "受控目录" in (result.error or "")


@pytest.mark.asyncio
async def test_exact_approval_reads_images_from_agent_vault(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = blog_repo.parent / "agent-vault"
    vault.mkdir()
    image = vault / f"{'a' * 64}.jpg"
    _write_image(image)
    monkeypatch.setattr(settings, "agent_asset_vault_root", vault)

    content = _content(images=[str(image)]).model_copy(update={"exact_approval": True})
    result = await GitHubPagesPublisher().publish(0, {}, content)

    assert result.success is True
    assert result.effect_applied is False
    assert result.raw_response["images_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden_relative", [".secret.jpg", ".private/cover.jpg"])
async def test_rejects_hidden_image_or_hidden_parent(
    blog_repo: Path,
    hidden_relative: str,
) -> None:
    image = settings.github_pages_asset_root / hidden_relative
    _write_image(image)

    result = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(image)]),
    )

    assert result.success is False
    assert "隐藏" in (result.error or "")


@pytest.mark.asyncio
async def test_rejects_symlink_image(blog_repo: Path) -> None:
    target = settings.github_pages_asset_root / "target.jpg"
    link = settings.github_pages_asset_root / "linked.jpg"
    _write_image(target)
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not support symlinks")

    result = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(link)]),
    )

    assert result.success is False
    assert "符号链接" in (result.error or "")


@pytest.mark.asyncio
async def test_rejects_invalid_or_extension_mismatched_image(blog_repo: Path) -> None:
    invalid = settings.github_pages_asset_root / "invalid.jpg"
    invalid.write_bytes(b"not an image")

    invalid_result = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(invalid)]),
    )
    assert invalid_result.success is False
    assert "实际解码" in (invalid_result.error or "")

    mismatch = settings.github_pages_asset_root / "mismatch.jpg"
    _write_image(mismatch, image_format="PNG")
    mismatch_result = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(mismatch)]),
    )
    assert mismatch_result.success is False
    assert "实际格式" in (mismatch_result.error or "")


@pytest.mark.asyncio
async def test_rejects_per_image_and_total_size_overflow(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = settings.github_pages_asset_root / "first.jpg"
    second = settings.github_pages_asset_root / "second.jpg"
    _write_image(first, size=(32, 32))
    _write_image(second, size=(32, 32))

    monkeypatch.setattr(settings, "github_pages_max_image_bytes", first.stat().st_size - 1)
    per_image = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(first)]),
    )
    assert per_image.success is False
    assert "单张图片" in (per_image.error or "")

    monkeypatch.setattr(settings, "github_pages_max_image_bytes", 1024 * 1024)
    monkeypatch.setattr(
        settings,
        "github_pages_max_total_image_bytes",
        first.stat().st_size + second.stat().st_size - 1,
    )
    total = await GitHubPagesPublisher().publish(
        0,
        {},
        _content(images=[str(first), str(second)]),
    )
    assert total.success is False
    assert "图片总大小" in (total.error or "")


@pytest.mark.asyncio
async def test_dirty_repo_stops_before_any_write_or_build(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    publisher = FakeCommandPublisher(status=" M secrets.env\n")

    result = await publisher.publish(0, {}, _content(body="do not write"))

    assert result.success is False
    assert "不干净" in (result.error or "")
    assert not (blog_repo / "source").exists()
    assert ["pnpm", "hexo", "clean"] not in publisher.commands
    assert ["git", "add", "-A"] not in publisher.commands


@pytest.mark.asyncio
async def test_live_publish_uses_fixed_argv_exact_add_and_verified_commit(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    image = settings.github_pages_asset_root / "cover image.jpg"
    _write_image(image)
    publisher = FakeCommandPublisher()
    secret = "this must not be persisted"

    result = await publisher.publish(
        0,
        {},
        _content(
            title="安全发布",
            body=secret,
            images=[str(image)],
            tags=["a: b"],
        ),
    )

    assert result.success is True
    assert result.effect_applied is True
    assert result.platform_post_id == COMMIT_SHA
    assert ["pnpm", "hexo", "clean"] in publisher.commands
    assert ["pnpm", "hexo", "generate"] in publisher.commands
    assert [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "commit",
        "--only",
        "-m",
        "post: 安全发布 (安全发布)",
        "--",
        "source/_posts/安全发布.md",
        "source/img/安全发布/cover image.jpg",
    ] in publisher.commands
    assert [
        "git",
        "add",
        "--",
        "source/_posts/安全发布.md",
        "source/img/安全发布/cover image.jpg",
    ] in publisher.commands
    assert ["git", "add", "-A"] not in publisher.commands
    assert [
        "git",
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        COMMIT_SHA,
    ] in publisher.commands
    assert [
        "git",
        "rev-list",
        "--parents",
        "-n",
        "1",
        COMMIT_SHA,
    ] in publisher.commands
    push_command = next(command for command in publisher.commands if "push" in command)
    assert f"core.hooksPath={os.devnull}" in push_command
    assert "push.pushOption=" in push_command
    assert "--no-follow-tags" in push_command
    assert "--no-recurse-submodules" in push_command
    assert "--no-signed" in push_command
    assert f"--force-with-lease=refs/heads/pages:{COMMIT_SHA}" in push_command
    assert push_command[-1] == f"{COMMIT_SHA}:refs/heads/pages"
    push_alias = push_command[-2]
    assert re.fullmatch(r"ai-ops-transport-[0-9a-f]{64}://repository", push_alias)
    assert f"url.git@github.com:owner/site.git.pushInsteadOf={push_alias}" in push_command

    remote_checks = [
        command
        for command in publisher.commands
        if "ls-remote" in command and "--exit-code" in command
    ]
    assert len(remote_checks) == 2
    verification = remote_checks[-1]
    verification_alias = verification[-2]
    assert re.fullmatch(
        r"ai-ops-transport-[0-9a-f]{64}://repository",
        verification_alias,
    )
    assert f"url.git@github.com:owner/site.git.insteadOf={verification_alias}" in verification
    assert verification[-1] == "refs/heads/pages"
    persisted = json.dumps(result.raw_response, ensure_ascii=False)
    assert secret not in persisted
    assert "log" not in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["build", "add", "commit"])
async def test_precommit_failures_restore_exact_preflight_state(
    real_blog_repo: Path,
    fail_at: str,
) -> None:
    """Build/add/commit failure must remove new files and clear only their index entries."""
    image = settings.github_pages_asset_root / "cover.jpg"
    _write_image(image)
    publisher = LocalGitFailurePublisher(fail_at)
    baseline_head = _git(real_blog_repo, "rev-parse", "HEAD").strip()

    result = await publisher.publish(
        0,
        {},
        _content(images=[str(image)]),
    )

    post = real_blog_repo / "source/_posts/安全发布.md"
    copied_image = real_blog_repo / "source/img/安全发布/cover.jpg"
    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert not post.exists()
    assert not copied_image.exists()
    assert _git(real_blog_repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(real_blog_repo, "rev-parse", "HEAD").strip() == baseline_head
    assert not any(
        command[:2] in (["git", "reset"], ["git", "checkout"]) for command in publisher.commands
    )
    if fail_at in {"add", "commit"}:
        restore = next(
            command
            for command in publisher.commands
            if command[:3] == ["git", "restore", "--staged"]
        )
        delimiter = restore.index("--")
        assert set(restore[delimiter + 1 :]) == {
            "source/_posts/安全发布.md",
            "source/img/安全发布/cover.jpg",
        }


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_article_write_rejects_parent_symlink_swap_without_outside_write(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    outside = tmp_path / "outside-article"
    outside.mkdir()

    class ParentSwapPublisher(FakeCommandPublisher):
        def _snapshot_rollback_paths(self, repo, paths):
            snapshots = super()._snapshot_rollback_paths(repo, paths)
            (repo / "source").symlink_to(outside, target_is_directory=True)
            return snapshots

    result = await ParentSwapPublisher().publish(0, {}, _content())

    assert result.success is False
    assert result.raw_response == {"stage": "write"}
    assert not (outside / "_posts/安全发布.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_image_write_rejects_parent_symlink_swap_without_outside_write(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    image = settings.github_pages_asset_root / "cover.jpg"
    _write_image(image)
    (blog_repo / "source/img").mkdir(parents=True)
    outside = tmp_path / "outside-image"
    outside.mkdir()

    class ImageParentSwapPublisher(FakeCommandPublisher):
        def _copy_planned_image(self, plan, repository_files, relative):
            plan.destination.parent.symlink_to(outside, target_is_directory=True)
            return super()._copy_planned_image(plan, repository_files, relative)

    result = await ImageParentSwapPublisher().publish(
        0,
        {},
        _content(images=[str(image)]),
    )

    assert result.success is False
    assert result.raw_response == {"stage": "write"}
    assert not (outside / image.name).exists()
    assert not (blog_repo / "source/_posts/安全发布.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_write_cleanup_rejects_ordinary_parent_inode_swap(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    image = settings.github_pages_asset_root / "cover.jpg"
    _write_image(image)
    (blog_repo / "source/img").mkdir(parents=True)
    approved_parent = blog_repo / "source/_posts"
    moved_parent = tmp_path / "moved-approved-posts"
    replacement = approved_parent / "安全发布.md"

    class OrdinaryParentSwapPublisher(FakeCommandPublisher):
        def _copy_planned_image(self, plan, repository_files, relative):
            approved_parent.rename(moved_parent)
            approved_parent.mkdir()
            replacement.write_bytes(b"different file must stay\n")
            raise OSError("injected image failure after ordinary parent swap")

    result = await OrdinaryParentSwapPublisher().publish(
        0,
        {},
        _content(images=[str(image)]),
    )

    assert result.success is False
    assert result.raw_response == {"stage": "write"}
    assert replacement.read_bytes() == b"different file must stay\n"
    assert (moved_parent / "安全发布.md").exists()


@pytest.mark.asyncio
async def test_rollback_restores_existing_file_content_mode_and_index(
    real_blog_repo: Path,
) -> None:
    """事务 helper 对既有文件恢复原 bytes/mode，并取消该路径的暂存。"""
    publisher = GitHubPagesPublisher()
    existing = real_blog_repo / "source/_posts/existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"original\n")
    existing.chmod(0o640)
    _git(real_blog_repo, "add", "source/_posts/existing.md")
    _git(real_blog_repo, "commit", "--quiet", "-m", "existing")
    baseline_head = _git(real_blog_repo, "rev-parse", "HEAD").strip()
    baseline_status = _git(
        real_blog_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    snapshots = publisher._snapshot_rollback_paths(real_blog_repo, [existing])

    existing.write_bytes(b"published replacement\n")
    existing.chmod(0o600)
    publisher._seal_rollback_paths(snapshots, real_blog_repo)
    _git(real_blog_repo, "add", "source/_posts/existing.md")

    rollback = await publisher._rollback_precommit(
        repo=real_blog_repo,
        preflight_head=baseline_head,
        preflight_status=baseline_status,
        paths=snapshots,
        unstage=True,
    )

    assert rollback.success is True
    assert existing.read_bytes() == b"original\n"
    assert existing.stat().st_mode & 0o777 == 0o640
    assert _git(real_blog_repo, "status", "--porcelain=v1", "--untracked-files=all") == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_rollback_parent_symlink_swap_cannot_delete_outside_file(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    parent = real_blog_repo / "source/_posts"
    parked_parent = real_blog_repo / "source/_posts-parked"
    target = parent / "race.md"
    outside_parent = tmp_path / "outside-delete"
    outside_parent.mkdir()
    outside_target = outside_parent / target.name
    outside_target.write_bytes(b"outside must stay\n")

    class DeleteRacePublisher(GitHubPagesPublisher):
        def _unlink_rollback_file(self, repository_files, relative):
            parent.rename(parked_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            return super()._unlink_rollback_file(repository_files, relative)

    publisher = DeleteRacePublisher()
    snapshots = publisher._snapshot_rollback_paths(real_blog_repo, [target])
    parent.mkdir(parents=True)
    target.write_bytes(b"published bytes\n")
    publisher._seal_rollback_paths(snapshots, real_blog_repo)

    rollback = await publisher._rollback_precommit(
        repo=real_blog_repo,
        preflight_head=_git(real_blog_repo, "rev-parse", "HEAD").strip(),
        preflight_status="",
        paths=snapshots,
        unstage=False,
    )

    assert rollback.success is False
    assert outside_target.read_bytes() == b"outside must stay\n"
    assert (parked_parent / target.name).read_bytes() == b"published bytes\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_rollback_parent_symlink_swap_cannot_overwrite_outside_file(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    parent = real_blog_repo / "source/_posts"
    parent.mkdir(parents=True)
    parked_parent = real_blog_repo / "source/_posts-parked"
    target = parent / "existing.md"
    target.write_bytes(b"original bytes\n")
    _git(real_blog_repo, "add", "source/_posts/existing.md")
    _git(real_blog_repo, "commit", "--quiet", "-m", "existing for restore race")
    outside_parent = tmp_path / "outside-restore"
    outside_parent.mkdir()
    outside_target = outside_parent / target.name
    outside_target.write_bytes(b"outside must stay\n")

    class RestoreRacePublisher(GitHubPagesPublisher):
        def _atomic_restore_file(self, repository_files, relative, content, mode):
            parent.rename(parked_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            return super()._atomic_restore_file(
                repository_files,
                relative,
                content,
                mode,
            )

    publisher = RestoreRacePublisher()
    snapshots = publisher._snapshot_rollback_paths(real_blog_repo, [target])
    target.write_bytes(b"published replacement\n")
    publisher._seal_rollback_paths(snapshots, real_blog_repo)

    rollback = await publisher._rollback_precommit(
        repo=real_blog_repo,
        preflight_head=_git(real_blog_repo, "rev-parse", "HEAD").strip(),
        preflight_status="",
        paths=snapshots,
        unstage=False,
    )

    assert rollback.success is False
    assert outside_target.read_bytes() == b"outside must stay\n"
    assert (parked_parent / target.name).read_bytes() == b"published replacement\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_rollback_rejects_ordinary_parent_inode_swap_before_delete(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    parent = real_blog_repo / "source/_posts"
    moved_parent = tmp_path / "moved-delete-parent"
    target = parent / "race.md"

    class OrdinaryDeleteRacePublisher(GitHubPagesPublisher):
        def _unlink_rollback_file(self, repository_files, approved):
            parent.rename(moved_parent)
            parent.mkdir()
            (parent / target.name).write_bytes(b"different file must stay\n")
            return super()._unlink_rollback_file(repository_files, approved)

    publisher = OrdinaryDeleteRacePublisher()
    snapshots = publisher._snapshot_rollback_paths(real_blog_repo, [target])
    parent.mkdir(parents=True)
    target.write_bytes(b"published bytes\n")
    publisher._seal_rollback_paths(snapshots, real_blog_repo)

    rollback = await publisher._rollback_precommit(
        repo=real_blog_repo,
        preflight_head=_git(real_blog_repo, "rev-parse", "HEAD").strip(),
        preflight_status="",
        paths=snapshots,
        unstage=False,
    )

    assert rollback.success is False
    assert (parent / target.name).read_bytes() == b"different file must stay\n"
    assert (moved_parent / target.name).read_bytes() == b"published bytes\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd/no-follow contract")
@pytest.mark.asyncio
async def test_rollback_rejects_ordinary_parent_inode_swap_before_restore(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    parent = real_blog_repo / "source/_posts"
    parent.mkdir(parents=True)
    moved_parent = tmp_path / "moved-restore-parent"
    target = parent / "existing.md"
    target.write_bytes(b"original bytes\n")
    _git(real_blog_repo, "add", "source/_posts/existing.md")
    _git(real_blog_repo, "commit", "--quiet", "-m", "existing for directory race")

    class OrdinaryRestoreRacePublisher(GitHubPagesPublisher):
        def _atomic_restore_file(self, repository_files, approved, content, mode):
            parent.rename(moved_parent)
            parent.mkdir()
            (parent / target.name).write_bytes(b"different file must stay\n")
            return super()._atomic_restore_file(
                repository_files,
                approved,
                content,
                mode,
            )

    publisher = OrdinaryRestoreRacePublisher()
    snapshots = publisher._snapshot_rollback_paths(real_blog_repo, [target])
    target.write_bytes(b"published replacement\n")
    publisher._seal_rollback_paths(snapshots, real_blog_repo)

    rollback = await publisher._rollback_precommit(
        repo=real_blog_repo,
        preflight_head=_git(real_blog_repo, "rev-parse", "HEAD").strip(),
        preflight_status="",
        paths=snapshots,
        unstage=False,
    )

    assert rollback.success is False
    assert (parent / target.name).read_bytes() == b"different file must stay\n"
    assert (moved_parent / target.name).read_bytes() == b"published replacement\n"


@pytest.mark.asyncio
async def test_rollback_preserves_other_user_change_and_reports_manual_action(
    real_blog_repo: Path,
) -> None:
    """失败期间出现的其他路径改动必须保留，并明确要求人工核验。"""
    readme = real_blog_repo / "README.md"

    def user_edit() -> None:
        readme.write_text("user work must survive\n", encoding="utf-8")

    publisher = LocalGitFailurePublisher("build", on_failure=user_edit)

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.retryable is False
    assert result.raw_response["rollback_required"] is True
    assert "git status" in (result.error or "")
    assert readme.read_text(encoding="utf-8") == "user work must survive\n"
    assert not (real_blog_repo / "source/_posts/安全发布.md").exists()
    assert _git(real_blog_repo, "status", "--porcelain=v1") == " M README.md\n"
    assert not any(
        command[:2] in (["git", "reset"], ["git", "checkout"]) for command in publisher.commands
    )


@pytest.mark.asyncio
async def test_rollback_refuses_when_commit_happened_but_sha_read_failed(
    real_blog_repo: Path,
) -> None:
    """commit 已落地但 SHA 查询失败时，HEAD 对账必须阻止任何文件回滚。"""

    class ShaReadFailurePublisher(LocalGitFailurePublisher):
        def __init__(self) -> None:
            super().__init__("")
            self.rev_parse_calls = 0

        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            if argv[:3] == ["git", "rev-parse", "--verify"]:
                self.rev_parse_calls += 1
                if self.rev_parse_calls == 2:
                    self.commands.append(argv)
                    return _CommandResult(
                        started=True,
                        returncode=0,
                        stdout="not-a-sha\n",
                    )
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    baseline_head = _git(real_blog_repo, "rev-parse", "HEAD").strip()
    publisher = ShaReadFailurePublisher()

    result = await publisher.publish(0, {}, _content())

    post_relative = "source/_posts/安全发布.md"
    assert result.success is False
    assert result.raw_response["rollback_required"] is True
    assert "HEAD 已变化" in (result.error or "")
    assert _git(real_blog_repo, "rev-parse", "HEAD").strip() != baseline_head
    assert _git(real_blog_repo, "show", f"HEAD:{post_relative}")
    assert (real_blog_repo / post_relative).exists()
    assert not any(
        command[:2] in (["git", "reset"], ["git", "checkout"]) for command in publisher.commands
    )
    assert not any(command[:3] == ["git", "restore", "--staged"] for command in publisher.commands)


@pytest.mark.asyncio
async def test_commit_with_unexpected_path_is_never_pushed(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    publisher = FakeCommandPublisher(
        commit_paths=["source/_posts/安全发布.md", "secrets.env"],
    )

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert "非本任务路径" in (result.error or "")
    assert not any("push" in command for command in publisher.commands)


@pytest.mark.asyncio
async def test_build_hook_content_tampering_is_detected_before_git_add(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "build-tamper.git"
    baseline_head = _attach_bare_remote(real_blog_repo, remote)

    class BuildTamperingPublisher(GitHubPagesPublisher):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            self.commands.append(list(argv))
            if argv[:2] == ["pnpm", "hexo"]:
                if argv[2] == "generate":
                    post = cwd / "source/_posts/安全发布.md"
                    approved = post.read_text(encoding="utf-8")
                    post.write_text(
                        approved.replace("正文", "TAMPERED-BY-BUILD", 1),
                        encoding="utf-8",
                    )
                return _CommandResult(started=True, returncode=0)
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    publisher = BuildTamperingPublisher()

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert "build 后发生变化" in (result.error or "")
    assert result.raw_response["rollback_required"] is True
    assert not any(command[:2] == ["git", "add"] for command in publisher.commands)
    assert not any("push" in command for command in publisher.commands)
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == baseline_head


@pytest.mark.asyncio
async def test_git_add_race_cannot_push_commit_blob_that_differs_from_approval(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "add-race.git"
    baseline_head = _attach_bare_remote(real_blog_repo, remote)

    class AddRacePublisher(GitHubPagesPublisher):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.mutated = False

        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            self.commands.append(list(argv))
            if argv[:2] == ["pnpm", "hexo"]:
                return _CommandResult(started=True, returncode=0)
            if argv[:3] == ["git", "add", "--"] and not self.mutated:
                self.mutated = True
                post = cwd / "source/_posts/安全发布.md"
                approved = post.read_text(encoding="utf-8")
                post.write_text(
                    approved.replace("正文", "TAMPERED-BEFORE-ADD", 1),
                    encoding="utf-8",
                )
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    publisher = AddRacePublisher()

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert "commit 内容或文件模式" in (result.error or "")
    assert any(command[:2] == ["git", "ls-tree"] for command in publisher.commands)
    assert not any("push" in command for command in publisher.commands)
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == baseline_head


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink contract")
@pytest.mark.asyncio
async def test_symlink_swap_before_git_add_is_rejected_by_commit_tree_mode(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "symlink-race.git"
    baseline_head = _attach_bare_remote(real_blog_repo, remote)

    class SymlinkRacePublisher(GitHubPagesPublisher):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.mutated = False

        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            self.commands.append(list(argv))
            if argv[:2] == ["pnpm", "hexo"]:
                return _CommandResult(started=True, returncode=0)
            if argv[:3] == ["git", "add", "--"] and not self.mutated:
                self.mutated = True
                post = cwd / "source/_posts/安全发布.md"
                post.unlink()
                post.symlink_to("../../README.md")
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    publisher = SymlinkRacePublisher()

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert "commit 内容或文件模式" in (result.error or "")
    assert not any("push" in command for command in publisher.commands)
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == baseline_head


@pytest.mark.asyncio
async def test_captured_push_url_cannot_be_retargeted_during_build(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    original_remote = tmp_path / "original.git"
    baseline_head = _attach_bare_remote(real_blog_repo, original_remote)
    replacement_remote = tmp_path / "replacement.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(replacement_remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git(real_blog_repo, "push", "--quiet", str(replacement_remote), "pages:pages")

    class RetargetingPublisher(GitHubPagesPublisher):
        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            if argv[:2] == ["pnpm", "hexo"]:
                if argv[2] == "generate":
                    _git(cwd, "remote", "set-url", "--push", "origin", str(replacement_remote))
                return _CommandResult(started=True, returncode=0)
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    result = await RetargetingPublisher().publish(0, {}, _content())

    assert result.success is True
    assert result.effect_applied is True
    assert result.platform_post_id != baseline_head
    assert _git(original_remote, "rev-parse", "refs/heads/pages").strip() == result.platform_post_id
    assert _git(replacement_remote, "rev-parse", "refs/heads/pages").strip() == baseline_head


@pytest.mark.asyncio
async def test_transport_url_is_a_fixed_point_against_nested_git_rewrites(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    original_remote = tmp_path / "rewrite-original.git"
    baseline_head = _attach_bare_remote(real_blog_repo, original_remote)
    wrong_remote = tmp_path / "rewrite-wrong.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(wrong_remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git(real_blog_repo, "push", "--quiet", str(wrong_remote), "pages:pages")

    # The first rewrite makes remote inspection look safe. A second fresh Git
    # invocation would normally rewrite that captured path to another target.
    alias = "alias://approved-pages"
    _git(real_blog_repo, "remote", "set-url", "origin", alias)
    _git(
        real_blog_repo,
        "config",
        f"url.{original_remote}.pushInsteadOf",
        alias,
    )
    _git(
        real_blog_repo,
        "config",
        f"url.{wrong_remote}.insteadOf",
        str(original_remote),
    )
    assert _git(real_blog_repo, "remote", "get-url", "--push", "--all", "origin").strip() == str(
        original_remote
    )
    assert _git(
        real_blog_repo, "ls-remote", "--get-url", "--", str(original_remote)
    ).strip() == str(wrong_remote)

    class RewriteSafePublisher(GitHubPagesPublisher):
        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            if argv[:2] == ["pnpm", "hexo"]:
                return _CommandResult(started=True, returncode=0)
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    result = await RewriteSafePublisher().publish(0, {}, _content())

    assert result.success is True
    assert result.platform_post_id != baseline_head
    assert _git(original_remote, "rev-parse", "refs/heads/pages").strip() == result.platform_post_id
    assert _git(wrong_remote, "rev-parse", "refs/heads/pages").strip() == baseline_head


@pytest.mark.asyncio
async def test_exact_force_with_lease_rejects_remote_reset_during_build(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    (real_blog_repo / "README.md").write_text("second baseline\n", encoding="utf-8")
    _git(real_blog_repo, "add", "README.md")
    _git(real_blog_repo, "commit", "--quiet", "-m", "second baseline")
    remote = tmp_path / "lease-reset.git"
    baseline_head = _attach_bare_remote(real_blog_repo, remote)
    ancestor = _git(real_blog_repo, "rev-parse", f"{baseline_head}^").strip()

    class ResettingPublisher(GitHubPagesPublisher):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            self.commands.append(list(argv))
            if argv[:2] == ["pnpm", "hexo"]:
                if argv[2] == "generate":
                    _git(remote, "update-ref", "refs/heads/pages", ancestor)
                return _CommandResult(started=True, returncode=0)
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    publisher = ResettingPublisher()

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is True
    push = next(command for command in publisher.commands if "push" in command)
    assert f"--force-with-lease=refs/heads/pages:{baseline_head}" in push
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == ancestor


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable hook contract")
@pytest.mark.asyncio
async def test_pre_push_hook_is_disabled_for_exact_publication_push(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "hook-disabled.git"
    baseline_head = _attach_bare_remote(real_blog_repo, remote)
    sentinel = tmp_path / "pre-push-ran"
    hook = real_blog_repo / ".git/hooks/pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n", encoding="utf-8")
    hook.chmod(0o700)

    class HookSafePublisher(GitHubPagesPublisher):
        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            if argv[:2] == ["pnpm", "hexo"]:
                return _CommandResult(started=True, returncode=0)
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    result = await HookSafePublisher().publish(0, {}, _content())

    assert result.success is True
    assert result.platform_post_id != baseline_head
    assert not sentinel.exists()
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == result.platform_post_id


@pytest.mark.asyncio
async def test_push_follow_tags_config_cannot_expand_publication_side_effects(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "no-follow-tags.git"
    baseline_head = _attach_bare_remote(real_blog_repo, remote)
    _git(real_blog_repo, "tag", "-a", "unapproved-release", "-m", "must stay local")
    _git(real_blog_repo, "config", "push.followTags", "true")

    class ExactSideEffectPublisher(GitHubPagesPublisher):
        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            if argv[:2] == ["pnpm", "hexo"]:
                return _CommandResult(started=True, returncode=0)
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    result = await ExactSideEffectPublisher().publish(0, {}, _content())
    remote_tag = subprocess.run(
        [
            "git",
            "-C",
            str(remote),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/tags/unapproved-release",
        ],
        check=False,
    )

    assert result.success is True
    assert result.platform_post_id != baseline_head
    assert remote_tag.returncode == 1
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == result.platform_post_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_target",
    [
        "https://user:embedded-secret@github.com/owner/site.git",
        "https://github.com/owner/site.git?token=embedded-secret",
        "ext::sh -c malicious",
        "--upload-pack=malicious",
        " git@github.com:owner/site.git",
        "/tmp/remote=malicious",
    ],
)
async def test_unsafe_push_url_is_rejected_before_transport_without_leaking_value(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_target: str,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)

    class UnsafeRemotePublisher(FakeCommandPublisher):
        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            if argv == ["git", "remote", "get-url", "--push", "--all", "origin"]:
                self.commands.append(list(argv))
                return _CommandResult(started=True, returncode=0, stdout=f"{unsafe_target}\n")
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    publisher = UnsafeRemotePublisher()

    result = await publisher.publish(0, {}, _content())
    rendered = result.model_dump_json()
    flattened_argv = "\n".join("\0".join(command) for command in publisher.commands)

    assert result.success is False
    assert result.effect_applied is False
    assert result.raw_response == {"stage": "preflight"}
    assert not any("ls-remote" in command for command in publisher.commands)
    assert not any("push" in command for command in publisher.commands)
    assert unsafe_target not in flattened_argv
    assert "embedded-secret" not in rendered


@pytest.mark.asyncio
async def test_concurrent_unreviewed_parent_commit_is_never_pushed(
    real_blog_repo: Path,
    tmp_path: Path,
) -> None:
    """A process ignoring our lock cannot smuggle its commit into published history."""

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git(real_blog_repo, "branch", "-M", "pages")
    _git(real_blog_repo, "remote", "add", "origin", str(remote))
    _git(real_blog_repo, "push", "--quiet", "-u", "origin", "pages")
    baseline_head = _git(real_blog_repo, "rev-parse", "HEAD").strip()

    class ConcurrentCommitPublisher(GitHubPagesPublisher):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.injected = False

        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            self.commands.append(list(argv))
            if argv[:2] == ["pnpm", "hexo"]:
                return _CommandResult(started=True, returncode=0)
            if "commit" in argv and not self.injected:
                self.injected = True
                (cwd / "unreviewed.txt").write_text("must stay local\n", encoding="utf-8")
                _git(cwd, "add", "unreviewed.txt")
                _git(cwd, "commit", "--quiet", "--only", "-m", "unreviewed", "--", "unreviewed.txt")
            return await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )

    publisher = ConcurrentCommitPublisher()

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert "父提交不是发布前 HEAD" in (result.error or "")
    assert not any("push" in command for command in publisher.commands)
    assert _git(real_blog_repo, "rev-parse", "HEAD^").strip() != baseline_head
    assert _git(remote, "rev-parse", "refs/heads/pages").strip() == baseline_head


@pytest.mark.asyncio
async def test_repository_lock_serializes_entire_live_command_sequence(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    entered_build = asyncio.Event()
    release_build = asyncio.Event()

    class BlockingPublisher(FakeCommandPublisher):
        async def _run_argv(self, argv, *, cwd, timeout_seconds):
            result = await super()._run_argv(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
            if argv == ["pnpm", "hexo", "clean"]:
                entered_build.set()
                await release_build.wait()
            return result

    first = BlockingPublisher()
    second = FakeCommandPublisher()
    first_task = asyncio.create_task(first.publish(1, {}, _content(title="第一篇")))
    await asyncio.wait_for(entered_build.wait(), timeout=1)
    second_task = asyncio.create_task(second.publish(2, {}, _content(title="第二篇")))
    await asyncio.sleep(0.1)

    assert second.commands == []
    release_build.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result.success is True
    assert second_result.success is True
    assert second.commands[0] == ["git", "remote"]


@pytest.mark.asyncio
async def test_unconfirmed_push_timeout_is_outcome_uncertain(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    publisher = FakeCommandPublisher(
        push=_CommandResult(started=True, timed_out=True),
        verification=_CommandResult(started=True, timed_out=True),
    )

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.effect_applied is False
    assert result.retryable is False
    assert result.outcome_uncertain is True
    assert result.raw_response == {"stage": "push", "commit_sha": COMMIT_SHA}


@pytest.mark.asyncio
async def test_cancelled_push_is_outcome_uncertain(
    blog_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_pages_dry_run", False)
    publisher = FakeCommandPublisher(cancel_push=True)

    result = await publisher.publish(0, {}, _content())

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


@pytest.mark.asyncio
async def test_runner_timeout_terminates_kills_and_reaps_fake_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "pid"
    script = tmp_path / "hang.py"
    script.write_text(
        "import os, pathlib, signal, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(github_pages_module, "_PROCESS_STOP_GRACE_SECONDS", 0.05)

    result = await GitHubPagesPublisher()._run_argv(
        [sys.executable, str(script), str(pid_file)],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.timed_out is True
    await _wait_for_file(pid_file)
    assert not _process_exists(int(pid_file.read_text()))


@pytest.mark.asyncio
async def test_runner_cancellation_reaps_fake_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "pid"
    script = tmp_path / "hang.py"
    script.write_text(
        "import os, pathlib, signal, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(github_pages_module, "_PROCESS_STOP_GRACE_SECONDS", 0.05)
    task = asyncio.create_task(
        GitHubPagesPublisher()._run_argv(
            [sys.executable, str(script), str(pid_file)],
            cwd=tmp_path,
            timeout_seconds=30,
        )
    )
    await _wait_for_file(pid_file)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _process_exists(int(pid_file.read_text()))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
@pytest.mark.asyncio
async def test_runner_kills_grandchild_that_outlives_parent_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_file = tmp_path / "grandchild-pid"
    script = tmp_path / "spawn-grandchild.py"
    script.write_text(
        "import subprocess, sys, time\n"
        'code = ("import os, pathlib, signal, sys, time; "\n'
        '        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "\n'
        '        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)")\n'
        "subprocess.Popen([sys.executable, '-c', code, sys.argv[1]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(github_pages_module, "_PROCESS_STOP_GRACE_SECONDS", 0.05)

    result = await GitHubPagesPublisher()._run_argv(
        [sys.executable, str(script), str(child_pid_file)],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.timed_out is True
    await _wait_for_file(child_pid_file)
    child_pid = int(child_pid_file.read_text())
    for _ in range(100):
        if not _process_exists(child_pid):
            break
        await asyncio.sleep(0.01)
    assert not _process_exists(child_pid)


@pytest.mark.asyncio
async def test_runner_environment_does_not_forward_control_plane_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create(*args, **kwargs):
        del args
        captured_env.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setenv("FERNET_KEY", "must-not-reach-hexo-or-git")
    monkeypatch.setenv("API_KEY", "must-not-reach-hexo-or-git")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@example/db")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-hexo-or-git")
    monkeypatch.setenv("PATH", "/safe/path")
    monkeypatch.setattr(settings, "github_pages_gh_token", "must-reach-only-gh")
    monkeypatch.setattr(
        github_pages_module.asyncio,
        "create_subprocess_exec",
        fake_create,
    )

    result = await GitHubPagesPublisher()._run_argv(
        ["git", "status"],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.ok is True
    assert captured_env["PATH"] == "/safe/path"
    assert captured_env["GIT_TERMINAL_PROMPT"] == "0"
    assert captured_env["GIT_ALLOW_PROTOCOL"] == "file:https:ssh"
    assert captured_env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert captured_env["GIT_GRAFT_FILE"] == os.devnull
    assert "FERNET_KEY" not in captured_env
    assert "API_KEY" not in captured_env
    assert "DATABASE_URL" not in captured_env
    assert "OPENAI_API_KEY" not in captured_env
    assert "GH_TOKEN" not in captured_env


@pytest.mark.asyncio
async def test_git_runner_ignores_replace_refs_and_repository_grafts(
    real_blog_repo: Path,
) -> None:
    (real_blog_repo / "README.md").write_text("middle\n", encoding="utf-8")
    _git(real_blog_repo, "add", "README.md")
    _git(real_blog_repo, "commit", "--quiet", "-m", "middle")
    middle = _git(real_blog_repo, "rev-parse", "HEAD").strip()
    (real_blog_repo / "README.md").write_text("actual\n", encoding="utf-8")
    _git(real_blog_repo, "add", "README.md")
    _git(real_blog_repo, "commit", "--quiet", "-m", "actual")
    actual = _git(real_blog_repo, "rev-parse", "HEAD").strip()
    baseline = _git(real_blog_repo, "rev-parse", f"{middle}^").strip()
    publisher = GitHubPagesPublisher()

    _git(real_blog_repo, "replace", actual, baseline)
    assert _git(real_blog_repo, "show", "-s", "--format=%s", actual).strip() == "baseline"
    replacement_safe = await publisher._run_argv(
        ["git", "show", "-s", "--format=%s", actual],
        cwd=real_blog_repo,
        timeout_seconds=5,
    )
    assert replacement_safe.ok is True
    assert replacement_safe.stdout.strip() == "actual"

    _git(real_blog_repo, "replace", "-d", actual)
    graft_file = real_blog_repo / ".git/info/grafts"
    graft_file.write_text(f"{actual} {baseline}\n", encoding="utf-8")
    assert _git(real_blog_repo, "rev-list", "--parents", "-n", "1", actual).split() == [
        actual,
        baseline,
    ]
    graft_safe = await publisher._run_argv(
        ["git", "rev-list", "--parents", "-n", "1", actual],
        cwd=real_blog_repo,
        timeout_seconds=5,
    )
    assert graft_safe.ok is True
    assert graft_safe.stdout.split() == [actual, middle]


@pytest.mark.asyncio
async def test_gh_version_probe_receives_no_token_and_uses_ephemeral_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}
    config_dir: Path | None = None

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"gh version 2.97.0\n", b""

    async def fake_create(*args, **kwargs):
        nonlocal config_dir
        assert args == ("gh", "--version")
        captured_env.update(kwargs["env"])
        config_dir = Path(kwargs["env"]["GH_CONFIG_DIR"])
        assert config_dir.is_dir()
        return FakeProcess()

    monkeypatch.setenv("GH_TOKEN", "ambient-token-must-not-win")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token-must-not-pass")
    monkeypatch.setenv("OPENAI_API_KEY", "control-plane-secret")
    monkeypatch.setattr(settings, "github_pages_gh_token", "project-pages-read-token")
    monkeypatch.setattr(github_pages_module.asyncio, "create_subprocess_exec", fake_create)

    result = await GitHubPagesPublisher()._run_argv(
        ["gh", "--version"],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.ok is True
    assert "GH_TOKEN" not in captured_env
    assert captured_env["GH_PROMPT_DISABLED"] == "1"
    assert captured_env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert captured_env["GH_NO_EXTENSION_UPDATE_NOTIFIER"] == "1"
    assert captured_env["GH_SPINNER_DISABLED"] == "1"
    assert captured_env["GH_TELEMETRY"] == "false"
    assert captured_env["DO_NOT_TRACK"] == "true"
    assert "GITHUB_TOKEN" not in captured_env
    assert "OPENAI_API_KEY" not in captured_env
    assert config_dir is not None
    assert not config_dir.exists()


@pytest.mark.asyncio
async def test_gh_verifier_executes_a_private_copy_bound_to_the_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = tmp_path / "installed-gh"
    approved_bytes = b"#!/bin/sh\necho 'gh version 2.97.0'\n"
    installed.write_bytes(approved_bytes)
    installed.chmod(0o700)
    monkeypatch.setattr(github_pages_module.shutil, "which", lambda _binary: str(installed))
    monkeypatch.setattr(settings, "github_pages_repository", "owner/site")
    monkeypatch.setattr(settings, "github_pages_base_url", "https://owner.github.io/site")
    monkeypatch.setattr(settings, "github_pages_gh_token", "project-pages-read-token")
    monkeypatch.setattr(
        settings,
        "github_pages_gh_sha256",
        hashlib.sha256(approved_bytes).hexdigest(),
    )

    publisher = GitHubPagesPublisher()
    verifier, remote_url, error = await publisher._prepare_gh_verifier(
        tmp_path,
        "origin",
        "pages",
        remote_url="git@github.com:owner/site.git",
    )

    assert error is None
    assert remote_url == "git@github.com:owner/site.git"
    assert verifier is not None
    staged = Path(verifier.config.binary)
    assert staged.name == "gh"
    assert staged != installed
    assert staged.read_bytes() == approved_bytes
    assert staged.stat().st_mode & 0o222 == 0

    installed.write_bytes(b"#!/bin/sh\necho attacker\n")
    assert staged.read_bytes() == approved_bytes
    publisher._cleanup_gh_runtime()
    assert not staged.exists()


@pytest.mark.asyncio
async def test_only_exact_gh_api_contract_receives_project_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}
    approved_binary = str(tmp_path / "gh")
    api_argv = [
        approved_binary,
        "api",
        "repos/owner/site/pages",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "--include",
        "--jq",
        '{"status":.status,"html_url":.html_url,"build_type":.build_type,'
        '"source":.source,"https_enforced":.https_enforced,"public":.public}',
    ]

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"HTTP/2.0 200 OK\n\n{}", b""

    async def fake_create(*args, **kwargs):
        assert list(args) == api_argv
        captured_env.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(settings, "github_pages_repository", "owner/site")
    monkeypatch.setattr(settings, "github_pages_gh_token", "project-pages-read-token")
    monkeypatch.setenv("HTTPS_PROXY", "https://ambient-proxy.example")
    monkeypatch.setenv("SSL_CERT_FILE", "/ambient/ca.pem")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent.sock")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /ambient/private-key")
    monkeypatch.setattr(github_pages_module.asyncio, "create_subprocess_exec", fake_create)

    publisher = GitHubPagesPublisher()
    publisher._github_pages_gh_binary_path = approved_binary
    approved_path = Path(approved_binary)
    approved_path.write_bytes(b"approved gh test binary")
    approved_path.chmod(0o500)
    publisher._github_pages_gh_binary_digest = hashlib.sha256(
        approved_path.read_bytes()
    ).hexdigest()
    result = await publisher._run_argv(
        api_argv,
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.ok is True
    assert captured_env["GH_TOKEN"] == "project-pages-read-token"
    assert "HTTPS_PROXY" not in captured_env
    assert "SSL_CERT_FILE" not in captured_env
    assert "SSH_AUTH_SOCK" not in captured_env
    assert "GIT_SSH_COMMAND" not in captured_env


@pytest.mark.asyncio
async def test_mutated_staged_gh_binary_never_receives_project_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "gh"
    staged.write_bytes(b"approved gh bytes")
    staged.chmod(0o500)
    started = False

    async def fake_create(*_args, **_kwargs):
        nonlocal started
        started = True
        raise AssertionError("mutated gh binary must not start")

    monkeypatch.setattr(settings, "github_pages_repository", "owner/site")
    monkeypatch.setattr(settings, "github_pages_gh_token", "project-pages-read-token")
    monkeypatch.setattr(github_pages_module.asyncio, "create_subprocess_exec", fake_create)
    publisher = GitHubPagesPublisher()
    publisher._github_pages_gh_binary_path = str(staged)
    publisher._github_pages_gh_binary_digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    staged.chmod(0o700)
    staged.write_bytes(b"attacker replacement")
    staged.chmod(0o500)
    argv = [
        str(staged),
        "api",
        "repos/owner/site/pages",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "--include",
        "--jq",
        '{"status":.status,"html_url":.html_url,"build_type":.build_type,'
        '"source":.source,"https_enforced":.https_enforced,"public":.public}',
    ]

    result = await publisher._run_argv(argv, cwd=tmp_path, timeout_seconds=1)

    assert result.started is False
    assert result.error == "unapproved_gh_binary"
    assert started is False


@pytest.mark.asyncio
async def test_unapproved_gh_command_never_starts_or_receives_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    async def fake_create(*_args, **_kwargs):
        nonlocal started
        started = True
        raise AssertionError("unapproved gh command must not start")

    monkeypatch.setattr(settings, "github_pages_gh_token", "project-pages-read-token")
    monkeypatch.setattr(github_pages_module.asyncio, "create_subprocess_exec", fake_create)

    result = await GitHubPagesPublisher()._run_argv(
        ["gh", "auth", "token"],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.started is False
    assert started is False


@pytest.mark.asyncio
async def test_exact_gh_api_without_approved_binary_identity_never_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    async def fake_create(*_args, **_kwargs):
        nonlocal started
        started = True
        raise AssertionError("unapproved gh API command must not start")

    monkeypatch.setattr(settings, "github_pages_repository", "owner/site")
    monkeypatch.setattr(settings, "github_pages_gh_token", "project-pages-read-token")
    monkeypatch.setattr(github_pages_module.asyncio, "create_subprocess_exec", fake_create)
    argv = [
        "gh",
        "api",
        "repos/owner/site/pages",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "--include",
        "--jq",
        '{"status":.status,"html_url":.html_url,"build_type":.build_type,'
        '"source":.source,"https_enforced":.https_enforced,"public":.public}',
    ]

    result = await GitHubPagesPublisher()._run_argv(
        argv,
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.started is False
    assert result.error == "unapproved_gh_binary"
    assert started is False
