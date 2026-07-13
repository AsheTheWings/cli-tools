"""Shared filesystem, frontmatter, and Git helpers for document commands."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click


WORKSPACE_ROOT = Path("/root/Desktop")
DESIGN_DIR = WORKSPACE_ROOT / "design"
REQUIREMENTS_DIR = WORKSPACE_ROOT / "requirements"


def resolve_repo_path(repo_name: str, reference_dir: Path) -> Optional[Path]:
    """Resolve a repository name or path to a directory."""
    candidates = [repo_name]
    if "/" in repo_name and not repo_name.startswith("/"):
        candidates.append(repo_name.rsplit("/", 1)[0])

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() and path.is_dir():
            return path.resolve()
        for base in (reference_dir, WORKSPACE_ROOT, Path.cwd()):
            path = (base / candidate).resolve()
            if path.is_dir():
                return path
    return None


def generate_tree_sha(repo_path: Path) -> Optional[str]:
    """Generate a working-tree SHA with a temporary Git index."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        click.echo("❌ Error: 'git' command not found.", err=True)
        return None

    descriptor, temporary_index = tempfile.mkstemp()
    os.close(descriptor)
    os.remove(temporary_index)
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = temporary_index

    try:
        for arguments in (
            ["read-tree", "--empty"],
            ["add", "-A", "--", "."],
            ["write-tree"],
        ):
            result = subprocess.run(
                ["git", "-C", str(repo_path), *arguments],
                capture_output=True,
                env=environment,
                text=True,
            )
            if result.returncode != 0:
                return None
        return result.stdout.strip()
    except FileNotFoundError:
        click.echo("❌ Error: 'git' command not found.", err=True)
        return None
    finally:
        if os.path.exists(temporary_index):
            os.remove(temporary_index)


def parse_frontmatter(
    content: str,
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """Split Markdown into frontmatter and body lines."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], lines[index + 1 :]
    return None, None


def extract_repos(frontmatter_lines: List[str]) -> Dict[str, str]:
    """Extract repository names and SHAs from a YAML mapping."""
    in_repos = False
    repos: Dict[str, str] = {}
    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("repos:"):
            in_repos = True
            continue
        if not in_repos:
            continue
        if not line.startswith((" ", "\t")):
            break
        if ":" not in stripped:
            continue
        repo_name, repo_sha = stripped.split(":", 1)
        repo_name = repo_name.split("#", 1)[0].strip().strip('"').strip("'")
        repo_sha = repo_sha.split("#", 1)[0].strip().strip('"').strip("'")
        repos[repo_name] = repo_sha
    return repos


def extract_yaml_list(frontmatter_lines: List[str], key: str) -> List[str]:
    """Extract a top-level YAML list."""
    in_section = False
    items: List[str] = []
    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(f"{key}:"):
            in_section = True
            continue
        if not in_section:
            continue
        if not line.startswith((" ", "\t")):
            break
        item = stripped.lstrip("-").strip()
        item = item.split("#", 1)[0].strip().strip('"').strip("'")
        if item:
            items.append(item)
    return items


def extract_frontmatter_scalar(frontmatter_lines: List[str], key: str) -> Optional[str]:
    """Extract a top-level scalar value from frontmatter."""
    for line in frontmatter_lines:
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped.startswith(f"{key}:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, str) and decoded else None
            except json.JSONDecodeError:
                pass
        return value.strip('"').strip("'") or None
    return None


def quote_yaml(value: str) -> str:
    """Quote a YAML scalar using JSON's YAML-compatible syntax."""
    return json.dumps(value, ensure_ascii=False)


def get_next_filename(parent_dir: Path, prefix: str) -> Path:
    """Return the next prefix-YYYYMMDD-N.md path."""
    parent_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    maximum = 0
    for path in parent_dir.glob(f"{prefix}-{today}-*.md"):
        try:
            maximum = max(maximum, int(path.stem.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return parent_dir / f"{prefix}-{today}-{maximum + 1}.md"


def is_git_repo(repo_path: Path) -> bool:
    """Return whether a directory is a Git repository or linked worktree."""
    return repo_path.is_dir() and (repo_path / ".git").exists()


def validate_document_path(
    path_str: str,
    directory: Path,
    prefix: str,
    label: str,
    check_exists: bool,
) -> Path:
    """Validate a generated document's directory and filename."""
    path = Path(path_str)
    if not path.is_absolute():
        candidate = (directory / path).resolve()
        path = candidate if candidate.exists() else path.resolve()
    else:
        path = path.resolve()

    expected_dir = directory.resolve()
    if path.parent != expected_dir:
        click.echo(f"❌ {label} '{path_str}' must be located in '{expected_dir}'", err=True)
        sys.exit(1)
    if not re.match(rf"^{prefix}-\d{{8}}-\d+\.md$", path.name):
        click.echo(
            f"❌ {label} file name '{path.name}' must follow "
            f"'{prefix}-YYYYMMDD-N.md'",
            err=True,
        )
        sys.exit(1)
    if check_exists and not path.exists():
        click.echo(f"❌ {label} not found: {path}", err=True)
        sys.exit(1)
    return path


def validate_design_doc_path(path_str: str, check_exists: bool = True) -> Path:
    return validate_document_path(
        path_str, DESIGN_DIR, "design", "Design document", check_exists
    )


def validate_requirements_doc_path(path_str: str, check_exists: bool = True) -> Path:
    return validate_document_path(
        path_str,
        REQUIREMENTS_DIR,
        "requirements",
        "Requirements document",
        check_exists,
    )
