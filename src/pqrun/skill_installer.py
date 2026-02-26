"""Install pqrun Codex skill templates into another repository from GitHub."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterator
from urllib.request import Request, urlopen


def _github_json(url: str) -> dict | list:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pqrun-skill-installer",
        },
    )
    with urlopen(request) as response:
        return json.load(response)


def _download_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "pqrun-skill-installer"})
    with urlopen(request) as response:
        return response.read()


def _iter_github_files(
    *, repo: str, source_path: str, ref: str, root_path: str | None = None
) -> Iterator[tuple[str, str]]:
    if root_path is None:
        root_path = source_path
    api_url = f"https://api.github.com/repos/{repo}/contents/{source_path}?ref={ref}"
    payload = _github_json(api_url)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return

    for item in payload:
        item_type = item.get("type")
        item_path = item.get("path")
        if not isinstance(item_path, str):
            continue
        if item_type == "file":
            prefix = f"{root_path}/"
            rel_path = item_path[len(prefix) :] if item_path.startswith(prefix) else Path(item_path).name
            download_url = item.get("download_url")
            if isinstance(download_url, str):
                yield rel_path, download_url
        elif item_type == "dir":
            yield from _iter_github_files(repo=repo, source_path=item_path, ref=ref, root_path=root_path)


def install_skill(
    target_repo: str | Path,
    *,
    skill_name: str = "pqrun-usage",
    force: bool = True,
    include_docs: bool = True,
    github_repo: str = "changhyeon363/pqrun",
    github_ref: str = "main",
) -> Path:
    """
    Install a skill template from GitHub into ``<target_repo>/skills/public/<skill_name>``.

    Args:
        target_repo: Target repository root path.
        skill_name: Skill directory name under ``skills/public`` in the GitHub repo.
        force: If True, overwrite existing destination (default: True).
        include_docs: If True, also install a pqrun docs snapshot under
            ``<skill>/references/pqrun-docs``.
        github_repo: GitHub repository slug (``owner/repo``).
        github_ref: Git ref (branch, tag, or commit).

    Returns:
        Destination path where the skill was installed.
    """
    source_path = f"skills/public/{skill_name}"
    destination = Path(target_repo).expanduser().resolve() / "skills" / "public" / skill_name

    if destination.exists():
        if not force:
            raise FileExistsError(f"Skill already exists: {destination}")
        shutil.rmtree(destination)

    files = list(_iter_github_files(repo=github_repo, source_path=source_path, ref=github_ref))
    if not files:
        raise FileNotFoundError(f"Skill not found on GitHub: {github_repo}@{github_ref}:{source_path}")

    destination.mkdir(parents=True, exist_ok=True)
    for rel_path, download_url in files:
        out_path = destination / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_download_bytes(download_url))

    if include_docs:
        _install_docs_snapshot(
            destination=destination,
            github_repo=github_repo,
            github_ref=github_ref,
        )

    return destination


def _install_docs_snapshot(*, destination: Path, github_repo: str, github_ref: str) -> None:
    docs_destination = destination / "references" / "pqrun-docs"
    docs_destination.mkdir(parents=True, exist_ok=True)

    docs_files = list(_iter_github_files(repo=github_repo, source_path="docs", ref=github_ref))
    for rel_path, download_url in docs_files:
        if rel_path != "index.md" and not rel_path.startswith("user/") and not rel_path.startswith("developer/"):
            continue
        out_path = docs_destination / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_download_bytes(download_url))

    readme_files = list(_iter_github_files(repo=github_repo, source_path="README.md", ref=github_ref))
    for rel_path, download_url in readme_files:
        if rel_path != "README.md":
            continue
        out_path = docs_destination / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_download_bytes(download_url))
