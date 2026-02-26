#!/usr/bin/env python3
"""
Update docs tree snapshot inside skills/public/pqrun-usage/SKILL.md.

python -m scripts.update_skill_docs_tree
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
SKILL_MD = ROOT / "skills" / "public" / "pqrun-usage" / "SKILL.md"
PATTERNS_FILE = ROOT / "scripts" / "config" / "pqrun-usage-docs-tree.toml"

START_MARKER = "<!-- DOCS_TREE_START -->"
END_MARKER = "<!-- DOCS_TREE_END -->"

# Fallback file groups if PATTERNS_FILE does not exist.
DEFAULT_IMPORTANT_GLOBS = [
    "docs/index.md",
    "docs/user/*.md",
    "docs/developer/*.md",
]


def load_patterns() -> list[str]:
    if not PATTERNS_FILE.exists():
        return DEFAULT_IMPORTANT_GLOBS

    payload = tomllib.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    patterns = payload.get("patterns", [])
    if not isinstance(patterns, list):
        return DEFAULT_IMPORTANT_GLOBS
    cleaned = [str(item).strip() for item in patterns if str(item).strip()]
    return cleaned or DEFAULT_IMPORTANT_GLOBS


def collect_important_paths() -> tuple[set[Path], set[Path]]:
    important_files: set[Path] = set()
    for pattern in load_patterns():
        important_files.update(path for path in ROOT.glob(pattern) if path.is_file())

    important_dirs: set[Path] = {DOCS_DIR}
    for file_path in important_files:
        current = file_path.parent
        while current != ROOT and current.is_relative_to(DOCS_DIR):
            important_dirs.add(current)
            if current == DOCS_DIR:
                break
            current = current.parent
    return important_files, important_dirs


def _render_dir(
    directory: Path,
    *,
    important_files: set[Path],
    important_dirs: set[Path],
    prefix: str = "",
) -> list[str]:
    dirs = sorted([child for child in directory.iterdir() if child.is_dir()], key=lambda path: path.name)
    files = sorted([child for child in directory.iterdir() if child.is_file()], key=lambda path: path.name)

    entries: list[tuple[str, Path | None]] = []
    hidden_files = 0

    for child in dirs:
        if child in important_dirs:
            entries.append((f"{child.name}/", child))
        else:
            entries.append((f"{child.name}/ ...", None))

    for child in files:
        if child in important_files:
            entries.append((child.name, None))
        else:
            hidden_files += 1

    if hidden_files > 0:
        entries.append((f"... ({hidden_files} file(s) hidden)", None))

    lines: list[str] = []
    for index, (label, nested_dir) in enumerate(entries):
        is_last = index == len(entries) - 1
        branch = "└── " if is_last else "├── "
        lines.append(f"{prefix}{branch}{label}")
        if nested_dir is not None:
            child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
            lines.extend(
                _render_dir(
                    nested_dir,
                    important_files=important_files,
                    important_dirs=important_dirs,
                    prefix=child_prefix,
                )
            )
    return lines


def build_tree_block() -> str:
    important_files, important_dirs = collect_important_paths()
    lines = ["```text", "pqrun-docs/"]
    lines.extend(
        _render_dir(
            DOCS_DIR,
            important_files=important_files,
            important_dirs=important_dirs,
        )
    )
    lines.append("```")
    return "\n".join(lines)


def update_skill_md() -> None:
    content = SKILL_MD.read_text(encoding="utf-8")
    start = content.find(START_MARKER)
    end = content.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Docs tree markers not found in SKILL.md")

    insertion_start = start + len(START_MARKER)
    before = content[:insertion_start]
    after = content[end:]
    tree_block = "\n" + build_tree_block() + "\n"
    SKILL_MD.write_text(before + tree_block + after, encoding="utf-8")


if __name__ == "__main__":
    update_skill_md()
    print(f"Updated docs tree block in {SKILL_MD}")
