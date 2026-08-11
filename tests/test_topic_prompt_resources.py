from pathlib import Path

from ai_ops.content import generator


def test_topic_prompt_falls_back_to_wheel_share_directory(tmp_path, monkeypatch):
    source_root = tmp_path / "missing-source-prompts"
    prefix = tmp_path / "installed-prefix"
    installed = prefix / "share" / "ai-ops-auto" / "prompts" / "topics"
    installed.mkdir(parents=True)
    (installed / "demo.md").write_text("wheel prompt", encoding="utf-8")

    monkeypatch.setattr(generator, "_TOPICS_PROMPT_DIR", source_root)
    monkeypatch.setattr(generator.sys, "prefix", str(prefix))

    assert generator._load_topic_prompt("demo") == "wheel prompt"


def test_topic_prompt_slug_cannot_escape_prompt_root(tmp_path, monkeypatch):
    prompt_root = tmp_path / "prompts"
    prompt_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("must not load", encoding="utf-8")

    monkeypatch.setattr(generator, "_TOPICS_PROMPT_DIR", prompt_root)

    assert generator._load_topic_prompt("../outside") == ""
    assert generator._load_topic_prompt(str(Path("nested") / "outside")) == ""
