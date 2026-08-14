"""Resolve change-delivery policy from the accepted root ``.project.json``."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Optional


MANIFEST_NAME = ".project.json"
SCHEMA_VERSION = 1
WORKFLOW_DIRECT = "direct"
WORKFLOW_WORKTREE_PR = "worktree-pr"
VALID_WORKFLOWS = {WORKFLOW_DIRECT, WORKFLOW_WORKTREE_PR}


class ProjectManifestError(ValueError):
    """The accepted project manifest or managed checkout is unusable."""


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    repository: str
    path: Path
    primary_branch: str
    primary_checkout: str
    workflow: str

    @property
    def uses_worktree_pr(self) -> bool:
        return self.workflow == WORKFLOW_WORKTREE_PR


def _git_stdout(args: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _repository_identity(remote: str) -> str | None:
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if remote.startswith(prefix):
            value = remote[len(prefix):].removesuffix(".git")
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
                return value
    return None


def _parse_manifest(content: str, *, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise ProjectManifestError(f"{label}: invalid JSON: {error}") from error
    required = {
        "schemaVersion", "project", "repository", "primaryBranch",
        "primaryCheckout", "changeDelivery",
    }
    if not isinstance(raw, dict) or not required <= set(raw):
        raise ProjectManifestError(f"{label}: required project fields are missing")
    if raw["schemaVersion"] != SCHEMA_VERSION:
        raise ProjectManifestError(f"{label}: unsupported schema version")
    if not isinstance(raw["project"], str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", raw["project"]
    ):
        raise ProjectManifestError(f"{label}: malformed project identity")
    if not isinstance(raw["repository"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw["repository"]
    ):
        raise ProjectManifestError(f"{label}: malformed repository identity")
    if not isinstance(raw["primaryBranch"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]*", raw["primaryBranch"]
    ):
        raise ProjectManifestError(f"{label}: malformed primary branch")
    if not isinstance(raw["primaryCheckout"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", raw["primaryCheckout"]
    ):
        raise ProjectManifestError(f"{label}: primary checkout must be one directory name")
    delivery = raw["changeDelivery"]
    if not isinstance(delivery, dict) or set(delivery) != {"mode"}:
        raise ProjectManifestError(f"{label}: malformed changeDelivery")
    if delivery["mode"] not in VALID_WORKFLOWS:
        raise ProjectManifestError(f"{label}: unsupported change-delivery mode")
    return raw


def primary_checkout(repo_path: Path) -> Path:
    common = _git_stdout(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo_path)
    bare = _git_stdout(["rev-parse", "--is-bare-repository"], repo_path)
    if not common or bare == "true":
        raise ProjectManifestError("repository has no managed primary checkout")
    common_path = Path(common).resolve()
    if common_path.name != ".git" or not common_path.is_dir():
        raise ProjectManifestError("Git common directory does not identify a primary checkout")
    return common_path.parent


def resolve_project(repo_path: Path) -> ProjectConfig:
    """Load policy from the commit accepted by the primary checkout's HEAD."""
    repo_path = repo_path.resolve()
    primary = primary_checkout(repo_path)
    content = _git_stdout(["show", f"HEAD:{MANIFEST_NAME}"], primary)
    if content is None:
        raise ProjectManifestError(
            f"accepted primary checkout commit does not contain {MANIFEST_NAME}"
        )
    raw = _parse_manifest(content, label=f"{primary}@HEAD:{MANIFEST_NAME}")
    branch = _git_stdout(["branch", "--show-current"], primary)
    if branch != raw["primaryBranch"]:
        raise ProjectManifestError(
            f"primary checkout is on {branch or 'detached HEAD'}, not {raw['primaryBranch']}"
        )
    if primary.name != raw["primaryCheckout"]:
        raise ProjectManifestError(
            f"primary checkout directory is {primary.name}, not {raw['primaryCheckout']}"
        )
    remote = _git_stdout(["remote", "get-url", "origin"], primary)
    identity = _repository_identity(remote or "")
    if identity != raw["repository"]:
        raise ProjectManifestError("primary checkout origin does not match .project.json")
    return ProjectConfig(
        name=raw["project"],
        repository=raw["repository"],
        path=primary,
        primary_branch=raw["primaryBranch"],
        primary_checkout=raw["primaryCheckout"],
        workflow=raw["changeDelivery"]["mode"],
    )


def is_primary_checkout(repo_path: Path) -> bool:
    repo_path = repo_path.resolve()
    toplevel = _git_stdout(["rev-parse", "--show-toplevel"], repo_path)
    return bool(toplevel and Path(toplevel).resolve() == primary_checkout(repo_path))
