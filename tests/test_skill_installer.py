from __future__ import annotations

from pathlib import Path

import pytest

import pqrun.skill_installer as skill_installer


def test_install_skill_downloads_from_github(tmp_path: Path, monkeypatch):
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    file_map = {
        "https://example/skill.md": b"# skill",
        "https://example/openai.yaml": b"interface:\n  display_name: test\n",
    }

    def fake_iter_github_files(*, repo: str, source_path: str, ref: str, root_path=None):
        assert repo == "changhyeon363/pqrun"
        assert source_path == "skills/public/pqrun-usage"
        assert ref == "main"
        yield "SKILL.md", "https://example/skill.md"
        yield "agents/openai.yaml", "https://example/openai.yaml"

    def fake_download_bytes(url: str) -> bytes:
        return file_map[url]

    monkeypatch.setattr(skill_installer, "_iter_github_files", fake_iter_github_files)
    monkeypatch.setattr(skill_installer, "_download_bytes", fake_download_bytes)

    dest = skill_installer.install_skill(target_repo, include_docs=False)
    assert dest == target_repo / "skills" / "public" / "pqrun-usage"
    assert (dest / "SKILL.md").exists()
    assert (dest / "agents" / "openai.yaml").exists()


def test_install_skill_refuses_overwrite_with_force_disabled(tmp_path: Path, monkeypatch):
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    monkeypatch.setattr(
        skill_installer,
        "_iter_github_files",
        lambda **kwargs: iter([("SKILL.md", "https://example/skill.md")]),
    )
    monkeypatch.setattr(skill_installer, "_download_bytes", lambda url: b"# skill")

    skill_installer.install_skill(target_repo, include_docs=False)
    with pytest.raises(FileExistsError):
        skill_installer.install_skill(target_repo, force=False, include_docs=False)


def test_install_skill_overwrites_by_default_on_rerun(tmp_path: Path, monkeypatch):
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    monkeypatch.setattr(
        skill_installer,
        "_iter_github_files",
        lambda **kwargs: iter([("SKILL.md", "https://example/skill.md")]),
    )
    monkeypatch.setattr(skill_installer, "_download_bytes", lambda url: b"# skill")

    dest = skill_installer.install_skill(target_repo, include_docs=False)
    marker = dest / "marker.txt"
    marker.write_text("old", encoding="utf-8")

    skill_installer.install_skill(target_repo, include_docs=False)
    assert not marker.exists()


def test_install_skill_overwrites_with_force(tmp_path: Path, monkeypatch):
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    monkeypatch.setattr(
        skill_installer,
        "_iter_github_files",
        lambda **kwargs: iter([("SKILL.md", "https://example/skill.md")]),
    )
    monkeypatch.setattr(skill_installer, "_download_bytes", lambda url: b"# skill")

    dest = skill_installer.install_skill(target_repo, include_docs=False)
    marker = dest / "marker.txt"
    marker.write_text("old", encoding="utf-8")

    skill_installer.install_skill(target_repo, force=True, include_docs=False)
    assert not marker.exists()


def test_install_skill_downloads_docs_snapshot_by_default(tmp_path: Path, monkeypatch):
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    file_map = {
        "https://example/skill.md": b"# skill",
        "https://example/openai.yaml": b"interface:\n  display_name: test\n",
        "https://example/docs-index.md": b"# docs index",
        "https://example/docs-user-quickstart.md": b"# quickstart",
        "https://example/docs-dev-architecture.md": b"# architecture",
        "https://example/readme.md": b"# pqrun",
    }

    def fake_iter_github_files(*, repo: str, source_path: str, ref: str, root_path=None):
        if source_path == "skills/public/pqrun-usage":
            yield "SKILL.md", "https://example/skill.md"
            yield "agents/openai.yaml", "https://example/openai.yaml"
            return
        if source_path == "docs":
            yield "index.md", "https://example/docs-index.md"
            yield "user/quickstart.md", "https://example/docs-user-quickstart.md"
            yield "developer/architecture.md", "https://example/docs-dev-architecture.md"
            yield "assets/logo.png", "https://example/ignored.png"
            return
        if source_path == "README.md":
            yield "README.md", "https://example/readme.md"
            return
        return iter(())

    monkeypatch.setattr(skill_installer, "_iter_github_files", fake_iter_github_files)
    monkeypatch.setattr(skill_installer, "_download_bytes", lambda url: file_map[url])

    dest = skill_installer.install_skill(target_repo)
    refs = dest / "references" / "pqrun-docs"
    assert (refs / "README.md").exists()
    assert (refs / "index.md").exists()
    assert (refs / "user" / "quickstart.md").exists()
    assert (refs / "developer" / "architecture.md").exists()
    assert not (refs / "assets" / "logo.png").exists()
