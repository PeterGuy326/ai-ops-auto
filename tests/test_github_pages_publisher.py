from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
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
        verification: _CommandResult | None = None,
        cancel_push: bool = False,
        commit_paths: list[str] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.status = status
        self.push_result = push or _CommandResult(started=True, returncode=0)
        self.verification_result = verification or _CommandResult(
            started=True,
            returncode=0,
            stdout=f"{COMMIT_SHA}\trefs/heads/pages\n",
        )
        self.cancel_push = cancel_push
        self.commit_paths = commit_paths
        self.staged_paths: list[str] = []

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
        if argv[:2] == ["git", "push"]:
            if self.cancel_push:
                raise asyncio.CancelledError
            return self.push_result
        if argv[:3] == ["git", "ls-remote", "--exit-code"]:
            return self.verification_result
        return _CommandResult(started=True, returncode=0)


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
        "push",
        "--porcelain",
        "origin",
        f"{COMMIT_SHA}:refs/heads/pages",
    ] in publisher.commands
    assert [
        "git",
        "ls-remote",
        "--exit-code",
        "origin",
        "refs/heads/pages",
    ] in publisher.commands
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
    assert not any(command[:2] == ["git", "push"] for command in publisher.commands)


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
    assert "FERNET_KEY" not in captured_env
    assert "API_KEY" not in captured_env
    assert "DATABASE_URL" not in captured_env
    assert "OPENAI_API_KEY" not in captured_env
