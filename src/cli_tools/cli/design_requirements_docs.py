"""Initialize and verify paired design and requirements documents."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, NoReturn, Optional, Tuple

import click

from cli_tools.cli.document_utils import (
    DESIGN_DIR,
    REQUIREMENTS_DIR,
    RepoSnapshot,
    extract_frontmatter_scalar,
    extract_repo_snapshots,
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


def fail(message: str) -> NoReturn:
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
    related_frontmatter: Optional[List[str]],
) -> List[Path]:
    requested = list(repos)
    if not requested and related_frontmatter:
        requested = list(extract_repos(related_frontmatter))
        if not requested:
            requested = extract_yaml_list(related_frontmatter, "repos")
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


def append_repos(
    frontmatter: List[str],
    repos: Dict[str, str],
    implementations: Optional[Dict[str, str]] = None,
) -> None:
    frontmatter.append("repos:\n")
    for repo, sha in repos.items():
        frontmatter.extend(
            [
                f"  {quote_yaml(repo)}:\n",
                f"    design: {sha}\n",
                f"    implementation: {(implementations or {}).get(repo, 'null')}\n",
            ]
        )


def replace_repos(
    frontmatter: List[str],
    repos: Dict[str, RepoSnapshot],
) -> List[str]:
    start = next(
        (index for index, line in enumerate(frontmatter) if line.strip() == "repos:"),
        None,
    )
    if start is None:
        fail("No repository snapshots found in frontmatter")
    end = start + 1
    while end < len(frontmatter) and frontmatter[end].startswith((" ", "\t")):
        end += 1
    replacement: List[str] = []
    append_repos(
        replacement,
        {repo: snapshot["design"] for repo, snapshot in repos.items()},
        {
            repo: implementation
            for repo, snapshot in repos.items()
            if (implementation := snapshot["implementation"])
        },
    )
    return frontmatter[:start] + replacement + frontmatter[end:]


def replace_scalar(frontmatter: List[str], key: str, value: str) -> List[str]:
    updated = list(frontmatter)
    for index, line in enumerate(updated):
        if not line.startswith((" ", "\t")) and line.strip().startswith(f"{key}:"):
            updated[index] = f"{key}: {value}\n"
            return updated
    fail(f"Frontmatter is missing '{key}'")


def assemble(frontmatter: List[str], body: List[str]) -> str:
    return "---\n" + "".join(frontmatter) + "---\n" + "".join(body)


def referenced_path(frontmatter: List[str], key: str, owner: Path) -> Optional[Path]:
    value = extract_frontmatter_scalar(frontmatter, key)
    if not value:
        return None
    return (owner.parent / value).resolve()


def require_supersede_target(frontmatter: List[str]) -> None:
    status = extract_frontmatter_scalar(frontmatter, "status")
    snapshots = extract_repo_snapshots(frontmatter)
    if status != "active" or any(
        snapshot["implementation"] for snapshot in snapshots.values()
    ):
        fail("Can only supersede an active, unimplemented design")


def require_extend_target(frontmatter: List[str]) -> None:
    if extract_frontmatter_scalar(frontmatter, "status") != "implemented":
        fail("Can only extend an implemented design")


@click.group(name="design")
def design_group() -> None:
    """Commands for paired design and requirements documents."""
    pass


@design_group.command(name="build")
@click.option(
    "-r",
    "--repo",
    "repos",
    multiple=True,
    help="Target repository names or absolute paths",
)
@click.option("-t", "--title", help="Design document title")
@click.option(
    "-D",
    "--domain",
    "domains",
    multiple=True,
    help="Stable implementation domains and commit scopes",
)
@click.option("-f", "--feature", "features", multiple=True, help="Target features")
@click.option("-d", "--description", help="Design document description")
@click.option("-u", "--supersede", help="Design document to supersede")
@click.option("-e", "--extend", help="Design document to extend")
def build_design_doc_command(
    repos: Tuple[str, ...],
    title: Optional[str],
    features: Tuple[str, ...],
    domains: Tuple[str, ...],
    description: Optional[str],
    supersede: Optional[str],
    extend: Optional[str],
) -> None:
    """Initialize a linked design and requirements document pair."""
    if supersede and extend:
        fail("--supersede and --extend are mutually exclusive")

    design_path = get_next_filename(DESIGN_DIR, "design")
    requirements_path = get_next_filename(REQUIREMENTS_DIR, "requirements")

    relation = "supersedes" if supersede else "extends" if extend else None
    related_design_path: Optional[Path] = None
    related_requirements_path: Optional[Path] = None
    related_frontmatter: Optional[List[str]] = None
    related = supersede or extend
    if related:
        related_design_path = validate_design_doc_path(related, check_exists=True)
        related_frontmatter, _ = read_document(
            related_design_path, f"{relation} design document"
        )
        if relation == "supersedes":
            require_supersede_target(related_frontmatter)
        else:
            require_extend_target(related_frontmatter)
        candidate = referenced_path(
            related_frontmatter, "requirements", related_design_path
        )
        if candidate and candidate.exists():
            related_requirements_path = validate_requirements_doc_path(
                str(candidate), check_exists=True
            )
        else:
            fail(
                f"{relation.title()} design does not reference an existing "
                "requirements document"
            )

    target_repos = resolve_target_repos(repos, related_frontmatter)
    snapshots = capture_repo_shas(target_repos)

    inherited_features = (
        extract_yaml_list(related_frontmatter, "features")
        if related_frontmatter
        else []
    )
    inherited_domains = []
    if related_frontmatter:
        inherited_domains = extract_yaml_list(related_frontmatter, "domains")
        if not inherited_domains:
            inherited_domains = extract_yaml_list(related_frontmatter, "scopes")
    target_features = list(features) or inherited_features
    target_domains = list(domains) or inherited_domains

    subject = ", ".join(target_features or target_domains) or "[feature/domain]"
    design_title = title or "[Short design title]"
    design_description = description or f"Design for {subject}."
    requirements_title = f"{design_title} Requirements"
    requirements_description = f"Canonical implementation requirements for {subject}."

    design_frontmatter = [
        f"title: {quote_yaml(design_title)}\n",
        f"description: {quote_yaml(design_description)}\n",
        "status: active\n",
    ]
    if relation and related_design_path:
        related_design = os.path.relpath(related_design_path, design_path.parent)
        design_frontmatter.append(f"{relation}: {quote_yaml(related_design)}\n")
    design_frontmatter.append(
        f"requirements: {quote_yaml(os.path.relpath(requirements_path, design_path.parent))}\n"
    )
    append_repos(design_frontmatter, snapshots)
    append_list(design_frontmatter, "domains", target_domains)
    append_list(design_frontmatter, "features", target_features)

    requirements_frontmatter = [
        f"title: {quote_yaml(requirements_title)}\n",
        f"description: {quote_yaml(requirements_description)}\n",
        "status: active\n",
    ]
    if relation and related_requirements_path:
        related_requirements = os.path.relpath(
            related_requirements_path, requirements_path.parent
        )
        requirements_frontmatter.append(
            f"{relation}: {quote_yaml(related_requirements)}\n"
        )
    requirements_frontmatter.append(
        f"design: {quote_yaml(os.path.relpath(design_path, requirements_path.parent))}\n"
    )
    append_repos(requirements_frontmatter, snapshots)
    append_list(requirements_frontmatter, "domains", target_domains)
    append_list(requirements_frontmatter, "features", target_features)

    design_body = [
        f"# {design_title}\n\n",
        "## Design\n\n",
        "TBD.\n\n",
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


def verify_repo_snapshots(
    repos: Dict[str, RepoSnapshot],
    reference_dir: Path,
) -> None:
    if not repos:
        fail("No repository SHA mapping found in frontmatter")

    click.echo("🔍 Verifying repository tree SHAs...")
    mismatches = []
    implementations = [
        snapshot["implementation"] is not None for snapshot in repos.values()
    ]
    if any(implementations) and not all(implementations):
        fail("Implementation snapshots must cover every repository")

    for repo_name, snapshot in repos.items():
        recorded_sha = snapshot["implementation"] or snapshot["design"]
        snapshot_kind = "implementation" if snapshot["implementation"] else "design"
        if not snapshot["design"]:
            fail(f"Repository '{repo_name}' is missing its immutable design tree SHA")
        repo = canonical_repo_path(repo_name, reference_dir)
        current_sha = generate_tree_sha(repo)
        if not current_sha:
            fail(f"Failed to generate Git tree SHA for {repo}")
        if current_sha == recorded_sha:
            click.echo(f"  - {repo}: ✓ MATCH ({snapshot_kind})")
        else:
            mismatches.append(str(repo))
            click.echo(
                f"  - {repo}: ✗ MISMATCH\n"
                f"    Current:  {current_sha}\n"
                f"    Recorded: {recorded_sha}"
            )
    if mismatches:
        fail(f"Repository snapshot mismatch: {', '.join(mismatches)}")


def verify_requirement_indices(requirements_body: List[str]) -> None:
    click.echo("🔍 Verifying requirement indices...")
    index = 1
    for line in requirements_body:
        match = re.match(r"^### R(\d+)([\s\S]*)$", line)
        if match:
            found_index = int(match.group(1))
            if found_index != index:
                fail(
                    f"Requirements indices are not sequential. Found R{found_index} but expected R{index}.\n"
                    "    Run 'tool design renumber-requirements <design-doc>' "
                    "to fix this."
                )
            index += 1
    click.echo("  - Indices: ✓ SEQUENTIAL")


def verify_lineage(
    design_frontmatter: List[str],
    requirements_frontmatter: List[str],
    design_path: Path,
    requirements_path: Path,
) -> None:
    relations = [
        relation
        for relation in ("extends", "supersedes")
        if referenced_path(design_frontmatter, relation, design_path)
        or referenced_path(requirements_frontmatter, relation, requirements_path)
    ]
    if len(relations) > 1:
        fail("A document pair cannot both extend and supersede earlier documents")
    if not relations:
        return

    relation = relations[0]
    related_design = referenced_path(design_frontmatter, relation, design_path)
    related_requirements = referenced_path(
        requirements_frontmatter, relation, requirements_path
    )
    if not related_design or not related_requirements:
        fail(f"Design and requirements documents contain different {relation} links")
    related_frontmatter, _ = read_document(
        related_design, f"{relation} design document"
    )
    related_status = extract_frontmatter_scalar(related_frontmatter, "status")
    related_snapshots = extract_repo_snapshots(related_frontmatter)
    if relation == "extends" and related_status != "implemented":
        fail("An extends link must reference an implemented design")
    if relation == "supersedes" and (
        related_status not in {"active", "superseded"}
        or any(
            snapshot["implementation"]
            for snapshot in related_snapshots.values()
        )
    ):
        fail("A supersedes link must reference an unimplemented design")
    expected_requirements = referenced_path(
        related_frontmatter, "requirements", related_design
    )
    if expected_requirements != related_requirements:
        fail(f"Design and requirements documents contain inconsistent {relation} links")


def verify_pair(
    design_path: Path,
    requirements_path: Path,
) -> None:
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

    verify_lineage(
        design_frontmatter,
        requirements_frontmatter,
        design_path,
        requirements_path,
    )

    design_repos = extract_repo_snapshots(design_frontmatter)
    requirements_repos = extract_repo_snapshots(requirements_frontmatter)
    if design_repos != requirements_repos:
        fail("Design and requirements documents contain different repository snapshots")
    if not design_repos:
        fail("No repository snapshots found in frontmatter")

    design_status = extract_frontmatter_scalar(design_frontmatter, "status")
    requirements_status = extract_frontmatter_scalar(requirements_frontmatter, "status")
    if design_status != requirements_status:
        fail("Design and requirements documents contain different statuses")
    if design_status not in {"active", "implemented", "superseded", "cancelled"}:
        fail(f"Unsupported document status: {design_status}")
    if any(snapshot["implementation"] for snapshot in design_repos.values()):
        if design_status != "implemented":
            fail("Implementation snapshots require status 'implemented'")

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

    verify_requirement_indices(requirements_body)
    verify_repo_snapshots(design_repos, design_path.parent)
    click.echo("✅ Design and requirements document pair verified.")


def capture_implementation(design_path: Path, requirements_path: Path) -> None:
    design_frontmatter, design_body = read_document(design_path, "design document")
    requirements_frontmatter, requirements_body = read_document(
        requirements_path, "requirements document"
    )
    linked_requirements = referenced_path(
        design_frontmatter, "requirements", design_path
    )
    if linked_requirements != requirements_path:
        fail("Design document does not link to the requirements document being updated")
    linked_design = referenced_path(
        requirements_frontmatter, "design", requirements_path
    )
    if linked_design != design_path:
        fail("Requirements document does not link back to the design document")
    verify_lineage(
        design_frontmatter,
        requirements_frontmatter,
        design_path,
        requirements_path,
    )

    design_repos = extract_repo_snapshots(design_frontmatter)
    requirements_repos = extract_repo_snapshots(requirements_frontmatter)
    if design_repos != requirements_repos:
        fail("Design and requirements documents contain different repository snapshots")
    if not design_repos:
        fail("No repository snapshots found in frontmatter")

    design_status = extract_frontmatter_scalar(design_frontmatter, "status")
    requirements_status = extract_frontmatter_scalar(requirements_frontmatter, "status")
    if design_status != requirements_status:
        fail("Design and requirements documents contain different statuses")
    if design_status in {"superseded", "cancelled"}:
        fail(f"Cannot implement a {design_status} design")

    repo_paths = {
        repo: canonical_repo_path(repo, design_path.parent) for repo in design_repos
    }
    current = capture_repo_shas(list(repo_paths.values()))
    for repo, snapshot in design_repos.items():
        snapshot["implementation"] = current[str(repo_paths[repo])]

    try:
        design_frontmatter = replace_scalar(
            replace_repos(design_frontmatter, design_repos),
            "status",
            "implemented",
        )
        requirements_frontmatter = replace_scalar(
            replace_repos(requirements_frontmatter, design_repos),
            "status",
            "implemented",
        )
        design_path.write_text(
            assemble(design_frontmatter, design_body),
            encoding="utf-8",
        )
        requirements_path.write_text(
            assemble(requirements_frontmatter, requirements_body),
            encoding="utf-8",
        )
    except OSError as error:
        fail(f"Failed to record implementation snapshots: {error}")

    click.echo(f"✅ Implementation snapshots recorded: {design_path}")
    click.echo(f"✅ Implementation snapshots recorded: {requirements_path}")


def resolve_pair_from_design(path: str) -> Tuple[Path, Path]:
    design_path = validate_design_doc_path(path, check_exists=True)
    design_frontmatter, _ = read_document(design_path, "design document")
    requirements_path = referenced_path(
        design_frontmatter, "requirements", design_path
    )
    if not requirements_path:
        fail("Design document does not reference a requirements document")
    return design_path, validate_requirements_doc_path(
        str(requirements_path), check_exists=True
    )


@design_group.command(name="verify")
@click.argument("path", required=True, type=click.Path())
def design_verify_command(path: str) -> None:
    """Verify a design pair's links, structure, snapshots, and repository SHAs."""
    design_path, requirements_path = resolve_pair_from_design(path)
    verify_pair(design_path, requirements_path)


@design_group.command(name="capture-implementation")
@click.argument("path", required=True, type=click.Path())
def design_capture_implementation_command(path: str) -> None:
    """Record current trees and mark a design pair implemented."""
    design_path, requirements_path = resolve_pair_from_design(path)
    capture_implementation(design_path, requirements_path)


@design_group.command(name="renumber-requirements")
@click.argument("path", required=True, type=click.Path())
def design_renumber_requirements_command(path: str) -> None:
    """Renumber all requirements (### R*) in a design pair."""
    _, requirements_path = resolve_pair_from_design(path)
    frontmatter, body = read_document(requirements_path, "requirements document")

    new_body = []
    index = 1
    changed = False
    for line in body:
        match = re.match(r"^(### R)\d+([\s\S]*)$", line)
        if match:
            prefix, rest = match.groups()
            new_line = f"{prefix}{index}{rest}"
            if new_line != line:
                changed = True
            new_body.append(new_line)
            index += 1
        else:
            new_body.append(line)

    if changed:
        try:
            requirements_path.write_text(
                assemble(frontmatter, new_body), encoding="utf-8"
            )
            click.echo(
                f"✅ Renumbered requirement indices in {requirements_path} (total {index - 1} requirements)."
            )
        except OSError as error:
            fail(f"Failed to write renumbered requirements: {error}")
    else:
        click.echo(
            f"✅ Requirement indices are already sequential (total {index - 1} requirements)."
        )
