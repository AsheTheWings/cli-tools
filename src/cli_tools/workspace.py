"""
Workspace preferences shared by CLI commands and agent instructions.

The canonical configuration lives at ``workspace.json`` in the directives
prompts tree (``/root/Desktop/directives/prompts/workspace.json``). That path
is the single source of truth: the prompts sync tooling only manages the
``.codex``, ``.agents`` and ``.config/opencode`` roots, so the file is read
directly from the repository checkout by both this CLI and by agents.

Schema (version 1)::

    {
      "schemaVersion": 1,
      "projects": {
        "<name>": {
          "path": "/absolute/path/to/main/checkout",
          "workflow": "direct" | "worktree-pr"
        }
      }
    }

The top-level ``projects`` key carries per-project user preferences and is
expected to grow additional fields over time; unknown fields are preserved
and ignored. Top-level keys starting with ``_`` (e.g. the ``_example`` block
written by ``tool setup workspace``) are documentation and always ignored.

Workflow semantics:

* ``direct``: agents apply changes directly in the project's main checkout:
  the primary working tree whose ``.git`` entry is a real directory (a linked
  worktree has a ``.git`` *file* instead, pointing back to the main
  checkout).
* ``worktree-pr``: agents must create or reuse a git worktree on a dedicated
  branch, land the change there, push, and open a pull request. Committing in
  the main checkout is rejected by ``tool commit`` before anything is staged.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

#: Default location of the canonical workspace preferences file.
DEFAULT_CONFIG_PATH = Path("/root/Desktop/directives/prompts/workspace.json")

#: Environment variable that overrides the configuration path (used by tests
#: and by anyone keeping the preferences file outside the directives tree).
CONFIG_ENV_VAR = "TOOL_WORKSPACE_CONFIG"

WORKFLOW_DIRECT = "direct"
WORKFLOW_WORKTREE_PR = "worktree-pr"
VALID_WORKFLOWS = (WORKFLOW_DIRECT, WORKFLOW_WORKTREE_PR)

SCHEMA_VERSION = 1


class WorkspaceConfigError(ValueError):
    """The workspace configuration exists but is not usable."""


@dataclass(frozen=True)
class ProjectConfig:
    """Resolved preferences for a single project entry."""

    name: str
    path: Path
    workflow: str
    #: Untouched entry payload, for forward-compatible access to fields this
    #: version of the tool does not understand yet.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def uses_worktree_pr(self) -> bool:
        return self.workflow == WORKFLOW_WORKTREE_PR


def default_config_path() -> Path:
    """Return the active configuration path (env override wins)."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """
    Load the workspace configuration.

    Returns an empty mapping when the file does not exist, so callers can
    treat "no preferences configured" as the safe default. Raises
    :class:`WorkspaceConfigError` when the file exists but is unusable.
    """
    path = path or default_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceConfigError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceConfigError(f"{path}: top level must be a JSON object")
    return data


def parse_projects(config: Mapping[str, Any]) -> dict[str, ProjectConfig]:
    """
    Validate and normalize the ``projects`` map of a loaded configuration.

    Raises :class:`WorkspaceConfigError` for malformed entries so problems
    surface loudly at the point of use rather than disabling policy checks
    silently.
    """
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise WorkspaceConfigError('"projects" must be a JSON object')

    parsed: dict[str, ProjectConfig] = {}
    for name, entry in projects.items():
        if not isinstance(entry, dict):
            raise WorkspaceConfigError(f'project "{name}": entry must be an object')
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspaceConfigError(
                f'project "{name}": missing or invalid "path"'
            )
        workflow = entry.get("workflow", WORKFLOW_DIRECT)
        if workflow not in VALID_WORKFLOWS:
            raise WorkspaceConfigError(
                f'project "{name}": "workflow" must be one of {VALID_WORKFLOWS}, '
                f"got {workflow!r}"
            )
        parsed[name] = ProjectConfig(
            name=name,
            path=Path(raw_path).expanduser().resolve(),
            workflow=workflow,
            raw=entry,
        )
    return parsed


def _git_stdout(args: list[str], cwd: Path) -> Optional[str]:
    """Run a git command; return stripped stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    stdout = result.stdout.strip()
    return stdout or None


def _repo_markers(repo_path: Path) -> tuple[Optional[Path], Optional[Path], bool]:
    """
    Return (toplevel, git_common_dir, is_bare) for the repo containing
    ``repo_path``. Either Path is None when git could not resolve it.
    """
    toplevel = _git_stdout(["rev-parse", "--show-toplevel"], repo_path)
    common = _git_stdout(["rev-parse", "--git-common-dir"], repo_path)
    bare = _git_stdout(["rev-parse", "--is-bare-repository"], repo_path)
    return (
        Path(toplevel).resolve() if toplevel else None,
        (repo_path / common).resolve() if common and not Path(common).is_absolute()
        else (Path(common).resolve() if common else None),
        bare == "true",
    )


def is_main_checkout(repo_path: Path) -> bool:
    """
    True when ``repo_path`` is inside the project's main checkout: the
    directory whose ``.git`` is a real directory rather than the ``gitdir:``
    pointer file found in linked worktrees. Bare repositories are not
    checkouts at all and return False.
    """
    repo_path = repo_path.resolve()
    git_dir = _git_stdout(["rev-parse", "--absolute-git-dir"], repo_path)
    common = _git_stdout(["rev-parse", "--git-common-dir"], repo_path)
    bare = _git_stdout(["rev-parse", "--is-bare-repository"], repo_path)
    if not git_dir or not common or bare == "true":
        return False
    common_path = (
        Path(common) if Path(common).is_absolute() else repo_path / common
    )
    return Path(git_dir).resolve() == common_path.resolve()


def resolve_project(
    repo_path: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[ProjectConfig]:
    """
    Find the configured project that owns ``repo_path``.

    A project matches when its configured main-checkout path equals the
    repository's worktree top level, or when the repository's git common
    directory lives under the configured path. The second clause links worktrees
    back to the project they belong to: a worktree's top level is a different
    directory, but its common dir is still the main checkout's ``.git``.

    Returns None when no project matches or no configuration exists.
    """
    if config is None:
        config = load_config()
    if not config:
        return None

    repo_path = repo_path.resolve()
    toplevel, common, _bare = _repo_markers(repo_path)

    for project in parse_projects(config).values():
        if toplevel is not None and toplevel == project.path:
            return project
        if common is not None and (
            common == project.path or project.path in common.parents
        ):
            return project
    return None
