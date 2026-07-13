"""Initialize and verify paired design and requirements documents."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click

from cli_tools.cli.document_utils import (
    DESIGN_DIR,
    REQUIREMENTS_DIR,
    extract_frontmatter_scalar,
    extract_repos,
    extract_yaml_list,
    generate_tree_sha,
    get_next_filename,
    is_git_repo,
    parse_frontmatter,
    quote_yaml,
    resolve_repo_path,
    validate_design_doc_path,
    validate_requirements_doc_path,
)


def fail(message: str) -> None:
    click.echo(f"❌ {message}", err=True)
    raise click.exceptions.Exit(1)


def read_document(path: Path, label: str) -> Tuple[List[str], List[str]]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        fail(f"Failed to read {label} '{path}': {error}")

    frontmatter, body = parse_frontmatter(content)
    if frontmatter is None or body is None:
        fail(f"Invalid {label} '{path}': YAML frontmatter not found")
    return frontmatter, body


def canonical_repo_path(repo_name: str, reference_dir: Path) -> Path:
    resolved = resolve_repo_path(repo_name, reference_dir)
    if not resolved:
        fail(f"Could not resolve repository: {repo_name}")
    if not is_git_repo(resolved):
        fail(f"Repository '{repo_name}' resolved to '{resolved}' is not a Git repository")
    return resolved.resolve()


def resolve_target_repos(
    repos: Tuple[str, ...],
    superseded_frontmatter: Optional[List[str]],
) -> List[Path]:
    requested = list(repos)
    if not requested and superseded_frontmatter:
        requested = list(extract_repos(superseded_frontmatter))
        if not requested:
            requested = extract_yaml_list(superseded_frontmatter, "repos")
    if not requested:
        fail("At least one repository is required with -r/--repo")

    resolved: List[Path] = []
    seen = set()
    for repo_name in requested:
        repo = canonical_repo_path(repo_name, DESIGN_DIR)
        key = str(repo)
        if key not in seen:
            resolved.append(repo)
            seen.add(key)
    return resolved


def capture_repo_shas(repos: List[Path]) -> Dict[str, str]:
    click.echo("🔍 Capturing repository tree SHAs...")
    snapshots: Dict[str, str] = {}
    for repo in repos:
        sha = generate_tree_sha(repo)
        if not sha:
            fail(f"Failed to generate Git tree SHA for {repo}")
        snapshots[str(repo)] = sha
        click.echo(f"  - {repo} => {sha}")
    return snapshots


def append_list(frontmatter: List[str], key: str, values: List[str]) -> None:
    if not values:
        return
    frontmatter.append(f"{key}:\n")
    for value in values:
        frontmatter.append(f"  - {quote_yaml(value)}\n")


def append_repos(frontmatter: List[str], repos: Dict[str, str]) -> None:
    frontmatter.append("repos:\n")
    for repo, sha in repos.items():
        frontmatter.append(f"  {quote_yaml(repo)}: {sha}\n")


def assemble(frontmatter: List[str], body: List[str]) -> str:
    return "---\n" + "".join(frontmatter) + "---\n" + "".join(body)


def referenced_path(frontmatter: List[str], key: str, owner: Path) -> Optional[Path]:
    value = extract_frontmatter_scalar(frontmatter, key)
    if not value:
        return None
    return (owner.parent / value).resolve()


@click.group(name="design-doc")
def design_doc_group() -> None:
    """Commands for paired design and requirements documents."""
    pass


@design_doc_group.command(name="build")
@click.option(
    "-r",
    "--repo",
    "repos",
    multiple=True,
    help="Target repository names or absolute paths",
)
@click.option("-t", "--title", help="Design document title")
@click.option("-f", "--feature", "features", multiple=True, help="Target features")
@click.option("-s", "--scope", "scopes", multiple=True, help="Target scopes")
@click.option("-d", "--description", help="Design document description")
@click.option("-u", "--supersede", help="Design document to supersede")
def build_design_doc_command(
    repos: Tuple[str, ...],
    title: Optional[str],
    features: Tuple[str, ...],
    scopes: Tuple[str, ...],
    description: Optional[str],
    supersede: Optional[str],
) -> None:
    """Initialize a linked design and requirements document pair."""
    design_path = get_next_filename(DESIGN_DIR, "design")
    requirements_path = get_next_filename(REQUIREMENTS_DIR, "requirements")

    superseded_design_path: Optional[Path] = None
    superseded_requirements_path: Optional[Path] = None
    superseded_frontmatter: Optional[List[str]] = None
    if supersede:
        superseded_design_path = validate_design_doc_path(supersede, check_exists=True)
        superseded_frontmatter, _ = read_document(
            superseded_design_path, "superseded design document"
        )
        candidate = referenced_path(
            superseded_frontmatter, "requirements", superseded_design_path
        )
        if candidate and candidate.exists():
            superseded_requirements_path = validate_requirements_doc_path(
                str(candidate), check_exists=True
            )

    target_repos = resolve_target_repos(repos, superseded_frontmatter)
    snapshots = capture_repo_shas(target_repos)

    inherited_features = (
        extract_yaml_list(superseded_frontmatter, "features")
        if superseded_frontmatter
        else []
    )
    inherited_scopes = (
        extract_yaml_list(superseded_frontmatter, "scopes")
        if superseded_frontmatter
        else []
    )
    target_features = list(features) or inherited_features
    target_scopes = list(scopes) or inherited_scopes

    feature_scope = ", ".join(target_features or target_scopes) or "[feature/scope]"
    design_title = title or "[Short design title]"
    design_description = description or f"Design for {feature_scope}."
    requirements_title = f"{design_title} Requirements"
    requirements_description = f"Canonical implementation requirements for {feature_scope}."

    design_frontmatter = [
        f"title: {quote_yaml(design_title)}\n",
        f"description: {quote_yaml(design_description)}\n",
        "status: draft\n",
    ]
    if superseded_design_path:
        superseded_design = os.path.relpath(
            superseded_design_path, design_path.parent
        )
        design_frontmatter.append(
            f"supersedes: {quote_yaml(superseded_design)}\n"
        )
    design_frontmatter.append(
        f"requirements: {quote_yaml(os.path.relpath(requirements_path, design_path.parent))}\n"
    )
    append_repos(design_frontmatter, snapshots)
    append_list(design_frontmatter, "features", target_features)
    append_list(design_frontmatter, "scopes", target_scopes)

    requirements_frontmatter = [
        f"title: {quote_yaml(requirements_title)}\n",
        f"description: {quote_yaml(requirements_description)}\n",
        "status: draft\n",
    ]
    if superseded_requirements_path:
        superseded_requirements = os.path.relpath(
            superseded_requirements_path, requirements_path.parent
        )
        requirements_frontmatter.append(
            f"supersedes: {quote_yaml(superseded_requirements)}\n"
        )
    requirements_frontmatter.append(
        f"design: {quote_yaml(os.path.relpath(design_path, requirements_path.parent))}\n"
    )
    append_repos(requirements_frontmatter, snapshots)
    append_list(requirements_frontmatter, "features", target_features)
    append_list(requirements_frontmatter, "scopes", target_scopes)

    design_body = [
        f"# {design_title}\n\n",
        "## Design\n\n",
        "TBD.\n",
    ]
    requirements_body = [f"# {requirements_title}\n\n"]
    for index, repo in enumerate(target_repos, start=1):
        requirements_body.extend(
            [
                f"## {repo}\n\n",
                f"### R{index}. Requirement\n\n",
                "TBD.\n\n",
            ]
        )

    try:
        design_path.write_text(
            assemble(design_frontmatter, design_body), encoding="utf-8"
        )
        requirements_path.write_text(
            assemble(requirements_frontmatter, requirements_body), encoding="utf-8"
        )
    except OSError as error:
        fail(f"Failed to initialize document pair: {error}")

    click.echo(f"✅ Design document initialized: {design_path}")
    click.echo(f"✅ Requirements document initialized: {requirements_path}")


def verify_repo_snapshots(repos: Dict[str, str], reference_dir: Path) -> None:
    if not repos:
        fail("No repository SHA mapping found in frontmatter")

    click.echo("🔍 Verifying repository tree SHAs...")
    mismatches = []
    for repo_name, recorded_sha in repos.items():
        repo = canonical_repo_path(repo_name, reference_dir)
        current_sha = generate_tree_sha(repo)
        if not current_sha:
            fail(f"Failed to generate Git tree SHA for {repo}")
        if current_sha == recorded_sha:
            click.echo(f"  - {repo}: ✓ MATCH")
        else:
            mismatches.append(str(repo))
            click.echo(
                f"  - {repo}: ✗ MISMATCH\n"
                f"    Current:  {current_sha}\n"
                f"    Recorded: {recorded_sha}"
            )
    if mismatches:
        fail(f"Repository snapshot mismatch: {', '.join(mismatches)}")


def verify_pair(design_path: Path, requirements_path: Path) -> None:
    design_frontmatter, design_body = read_document(design_path, "design document")
    requirements_frontmatter, requirements_body = read_document(
        requirements_path, "requirements document"
    )

    linked_requirements = referenced_path(
        design_frontmatter, "requirements", design_path
    )
    if linked_requirements != requirements_path:
        fail("Design document does not link to the requirements document being verified")

    linked_design = referenced_path(requirements_frontmatter, "design", requirements_path)
    if linked_design != design_path:
        fail("Requirements document does not link back to the design document")

    design_repos = extract_repos(design_frontmatter)
    requirements_repos = extract_repos(requirements_frontmatter)
    if design_repos != requirements_repos:
        fail("Design and requirements documents contain different repository snapshots")

    design_title = extract_frontmatter_scalar(design_frontmatter, "title")
    requirements_title = extract_frontmatter_scalar(requirements_frontmatter, "title")
    if not design_title or f"# {design_title}" not in "".join(design_body):
        fail("Design document H1 does not match its title")
    if not requirements_title or f"# {requirements_title}" not in "".join(
        requirements_body
    ):
        fail("Requirements document H1 does not match its title")

    requirements_text = "".join(requirements_body)
    for repo_name in requirements_repos:
        if f"## {repo_name}" not in requirements_text:
            fail(f"Requirements document is missing repository section '## {repo_name}'")

    verify_repo_snapshots(design_repos, design_path.parent)
    click.echo("✅ Design and requirements document pair verified.")


@design_doc_group.command(name="verify")
@click.argument("path", required=True, type=click.Path())
def design_doc_verify_command(path: str) -> None:
    """Verify a design pair's links, structure, snapshots, and repository SHAs."""
    design_path = validate_design_doc_path(path, check_exists=True)
    design_frontmatter, _ = read_document(design_path, "design document")
    requirements_path = referenced_path(
        design_frontmatter, "requirements", design_path
    )
    if not requirements_path:
        fail("Design document does not reference a requirements document")
    requirements_path = validate_requirements_doc_path(
        str(requirements_path), check_exists=True
    )
    verify_pair(design_path, requirements_path)


@click.group(name="requirements-doc")
def requirements_doc_group() -> None:
    """Commands for requirements documents paired with designs."""
    pass


@requirements_doc_group.command(name="verify")
@click.argument("path", required=True, type=click.Path())
def requirements_doc_verify_command(path: str) -> None:
    """Verify a requirements document and its linked design pair."""
    requirements_path = validate_requirements_doc_path(path, check_exists=True)
    requirements_frontmatter, _ = read_document(
        requirements_path, "requirements document"
    )
    design_path = referenced_path(requirements_frontmatter, "design", requirements_path)
    if not design_path:
        fail("Requirements document does not reference a design document")
    design_path = validate_design_doc_path(str(design_path), check_exists=True)
    verify_pair(design_path, requirements_path)
