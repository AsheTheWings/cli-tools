"""
Document builder and verification commands for context and design documents.

This module provides commands to build, initialize, and verify context and
design documents with repository Git tree SHAs.
"""

import os
import sys
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union

import click


def resolve_repo_path(repo_name: str, context_doc_dir: Path) -> Optional[Path]:
    """Resolve a repository name to its actual directory path on the filesystem."""
    candidates = [repo_name]
    
    # Only split relative repository paths (like "portfolio/dev" -> "portfolio")
    # Never strip directories from absolute paths to avoid false matches on parents like /root/Desktop
    if "/" in repo_name and not repo_name.startswith("/"):
        parts = repo_name.rsplit("/", 1)
        candidates.append(parts[0])

    for cand in candidates:
        if not cand:
            continue
        # Try as direct absolute path
        p = Path(cand)
        if p.is_absolute() and p.is_dir():
            return p

        # Try relative to context document directory
        p = (context_doc_dir / cand).resolve()
        if p.is_dir():
            return p

        # Try under /root/Desktop/
        p = Path("/root/Desktop") / cand
        if p.is_dir():
            return p.resolve()

        # Try relative to current working directory
        p = (Path.cwd() / cand).resolve()
        if p.is_dir():
            return p

    return None


def generate_tree_sha(repo_path: Path) -> Optional[str]:
    """
    Generate the temporary-index Git tree SHA for a repository.
    
    Matches the workflow's temporary index tree SHA calculation:
    1. Creates a temporary index file.
    2. Runs `git read-tree --empty` on it.
    3. Runs `git add -A -- .` to add all tracked/untracked files.
    4. Runs `git write-tree` to generate the SHA of this tree.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None
    except FileNotFoundError:
        click.echo("❌ Error: 'git' command not found. Please ensure Git is installed.", err=True)
        return None

    fd, tmp_index = tempfile.mkstemp()
    os.close(fd)
    
    try:
        if os.path.exists(tmp_index):
            os.remove(tmp_index)

        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = tmp_index

        res = subprocess.run(
            ["git", "-C", str(repo_path), "read-tree", "--empty"],
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None

        res = subprocess.run(
            ["git", "-C", str(repo_path), "add", "-A", "--", "."],
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None

        res = subprocess.run(
            ["git", "-C", str(repo_path), "write-tree"],
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None

        return res.stdout.strip()

    except FileNotFoundError:
        click.echo("❌ Error: 'git' command not found during Git operations.", err=True)
        return None
    finally:
        if os.path.exists(tmp_index):
            os.remove(tmp_index)


def parse_frontmatter(content: str) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """Split markdown content into frontmatter lines and body lines."""
    lines = content.splitlines(keepends=True)
    if not lines or not lines[0].strip() == "---":
        return None, None

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        return None, None

    frontmatter_lines = lines[1:end_idx]
    body_lines = lines[end_idx + 1:]
    return frontmatter_lines, body_lines


def extract_repos(frontmatter_lines: List[str]) -> Dict[str, str]:
    """Extract repository names and recorded SHAs from frontmatter lines."""
    in_repos = False
    repos = {}
    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("repos:"):
            in_repos = True
            continue
        if in_repos:
            if line.startswith(" ") or line.startswith("\t"):
                if ":" in stripped:
                    parts = stripped.split(":", 1)
                    repo_name = parts[0].strip()
                    repo_name = repo_name.split("#")[0].strip()
                    repo_name = repo_name.strip('"').strip("'")
                    
                    repo_val = parts[1].strip()
                    repo_val = repo_val.split("#")[0].strip()
                    repo_val = repo_val.strip('"').strip("'")
                    repos[repo_name] = repo_val
            else:
                in_repos = False
    return repos


def extract_yaml_list(frontmatter_lines: List[str], key: str) -> List[str]:
    """Extract YAML array items for a given key from frontmatter lines."""
    in_section = False
    items = []
    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(f"{key}:"):
            in_section = True
            continue
        if in_section:
            if line.startswith(" ") or line.startswith("\t"):
                # Strip list prefix '-' if present
                item = stripped.lstrip("-").strip()
                item = item.split("#")[0].strip()
                item = item.strip('"').strip("'")
                if item:
                    items.append(item)
            else:
                in_section = False
    return items


def get_referenced_context_path(frontmatter_lines: List[str], doc_path: Path) -> Optional[Path]:
    """Find referenced context document path if this is a design document."""
    for line in frontmatter_lines:
        stripped = line.strip()
        if stripped.startswith("context:"):
            parts = stripped.split(":", 1)
            ctx_val = parts[1].strip().strip('"').strip("'")
            ctx_path = (doc_path.parent / ctx_val).resolve()
            if ctx_path.exists():
                return ctx_path
    return None


def get_next_filename(parent_dir: Path, prefix: str) -> Path:
    """Calculate the next sequential filename in parent_dir based on format 'prefix-YYYYMMDD-N.md'."""
    parent_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    
    max_n = 0
    for f in parent_dir.glob(f"{prefix}-{today}-*.md"):
        parts = f.stem.split("-")
        if len(parts) >= 3:
            try:
                n = int(parts[2])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
                
    next_n = max_n + 1
    return parent_dir / f"{prefix}-{today}-{next_n}.md"


def get_latest_filename(parent_dir: Path, prefix: str) -> Optional[Path]:
    """Find the most recent file in parent_dir based on format 'prefix-YYYYMMDD-N.md'."""
    if not parent_dir.exists():
        return None
        
    latest_file = None
    max_date_n = (0, 0)  # (YYYYMMDD, N)
    
    for f in parent_dir.glob(f"{prefix}-*.md"):
        parts = f.stem.split("-")
        if len(parts) >= 3:
            try:
                date_val = int(parts[1])
                n_val = int(parts[2])
                if (date_val, n_val) > max_date_n:
                    max_date_n = (date_val, n_val)
                    latest_file = f
            except ValueError:
                pass
    return latest_file


def is_git_repo(repo_path: Path) -> bool:
    """Check if the given directory path is a valid Git repository."""
    if not repo_path.is_dir():
        return False
    return (repo_path / ".git").exists()


def validate_context_doc_path(path_str: str, check_exists: bool = True) -> Path:
    """Validate that a context document conforms to path/name conventions."""
    path = Path(path_str)
    if not path.is_absolute():
        opt_path = (Path("/root/Desktop/context") / path).resolve()
        if opt_path.exists():
            path = opt_path
        else:
            path = path.resolve()
    else:
        path = path.resolve()

    expected_dir = Path("/root/Desktop/context").resolve()
    if path.parent != expected_dir:
        click.echo(f"❌ Context document '{path_str}' must be located in '{expected_dir}'", err=True)
        sys.exit(1)
    if not re.match(r"^context-\d{8}-\d+\.md$", path.name):
        click.echo(f"❌ Context document file name '{path.name}' does not follow the naming convention 'context-YYYYMMDD-N.md'", err=True)
        sys.exit(1)
    if check_exists and not path.exists():
        click.echo(f"❌ Context document not found: {path}", err=True)
        sys.exit(1)
    return path


def validate_design_doc_path(path_str: str, check_exists: bool = True) -> Path:
    """Validate that a design document conforms to path/name conventions."""
    path = Path(path_str)
    if not path.is_absolute():
        opt_path = (Path("/root/Desktop/design") / path).resolve()
        if opt_path.exists():
            path = opt_path
        else:
            path = path.resolve()
    else:
        path = path.resolve()

    expected_dir = Path("/root/Desktop/design").resolve()
    if path.parent != expected_dir:
        click.echo(f"❌ Design document '{path_str}' must be located in '{expected_dir}'", err=True)
        sys.exit(1)
    if not re.match(r"^design-\d{8}-\d+\.md$", path.name):
        click.echo(f"❌ Design document file name '{path.name}' does not follow the naming convention 'design-YYYYMMDD-N.md'", err=True)
        sys.exit(1)
    if check_exists and not path.exists():
        click.echo(f"❌ Design document not found: {path}", err=True)
        sys.exit(1)
    return path


# =========================================================================
# Context Document CLI Group
# =========================================================================

@click.group(name="context-doc")
def context_doc_group() -> None:
    """Commands to build and verify context documents."""
    pass


@context_doc_group.command(name="build")
@click.option("-r", "--repo", "repos", multiple=True, required=True, help="Target repository names or paths")
@click.option("-t", "--title", help="Context document title")
@click.option("-f", "--feature", "features", multiple=True, help="Target features (top-level scopes)")
@click.option("-s", "--scope", "scopes", multiple=True, help="Target scopes")
@click.option("-d", "--description", help="Context document description")
def build_context_doc_command(
    repos: Tuple[str, ...],
    title: Optional[str],
    features: Tuple[str, ...],
    scopes: Tuple[str, ...],
    description: Optional[str],
) -> None:
    """
    Build a new context document.

    Generates the next sequential path at /root/Desktop/context/context-YYYYMMDD-N.md.
    """
    doc_path = get_next_filename(Path("/root/Desktop/context"), "context")

    if doc_path.exists():
        click.echo(f"❌ File already exists at {doc_path}. build is for building new files only.", err=True)
        sys.exit(1)

    target_repos = list(repos)

    # Resolve and compute SHAs
    click.echo("🔍 Resolving target repositories...")
    computed_shas: Dict[str, str] = {}
    for r_name in target_repos:
        resolved = resolve_repo_path(r_name, doc_path.parent)
        if not resolved:
            click.echo(f"❌ Could not resolve path for repository: {r_name}", err=True)
            sys.exit(1)
        if not is_git_repo(resolved):
            click.echo(f"❌ Repository '{r_name}' resolved to '{resolved}' is not a Git repository.", err=True)
            sys.exit(1)
        sha = generate_tree_sha(resolved)
        if not sha:
            click.echo(f"❌ Failed to generate Git tree SHA for {r_name}", err=True)
            sys.exit(1)
        computed_shas[r_name] = sha
        click.echo(f"  - {r_name} => {sha}")

    # Resolve target features and scopes
    target_features = list(features) if features else ["[feature]"]
    target_scopes = list(scopes) if scopes else ["[scope]"]

    # Determine feature scope string for description placeholder
    if features:
        feature_scope = ", ".join(features)
    elif scopes:
        feature_scope = ", ".join(scopes)
    else:
        feature_scope = "[feature_scope]"

    # Build properties
    doc_title = title or "[Short context title]"
    
    if description:
        doc_description = description
    else:
        doc_description = f"Current behavior and implementation context for {feature_scope}."

    # Construct frontmatter
    fm = []
    fm.append(f"title: {doc_title}\n")
    fm.append(f"description: {doc_description}\n")
    fm.append("status: draft\n")
    fm.append("repos:\n")
    for r_name, r_sha in computed_shas.items():
        fm.append(f"  {r_name}: {r_sha}\n")
    fm.append("features:\n")
    for feat in target_features:
        fm.append(f"  - {feat}\n")
    fm.append("scopes:\n")
    for sc in target_scopes:
        fm.append(f"  - {sc}\n")

    # Build body
    body = []
    body.append(f"# {doc_title}\n\n")
    body.append("## Current Implementation\n\n")
    
    # Implementation subheadings
    for r_name in target_repos:
        body.append(f"### {r_name}\n\n")
        body.append("#### Relevant File Tree\n- TBD by worker Pass 1.\n\n")
        body.append("#### Implementation Details\n- TBD by worker Pass 1.\n\n")
        
    doc_body = "".join(body)

    # Assemble and write
    updated_content = "---\n" + "".join(fm) + "---\n" + doc_body
    
    click.echo(f"💾 Saving context document to: {doc_path}...")
    try:
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(updated_content, encoding="utf-8")
    except Exception as e:
        click.echo(f"❌ Failed to write context document: {e}", err=True)
        sys.exit(1)

    click.echo(f"✅ Context document successfully written to {doc_path}")


@context_doc_group.command(name="verify")
@click.argument("path", required=True, type=click.Path())
def context_doc_verify_command(path: str) -> None:
    """
    Verify the structure and baseline SHAs of a context document.
    """
    doc_path = validate_context_doc_path(path, check_exists=True)

    if not doc_path.exists():
        click.echo(f"❌ File not found: {doc_path}", err=True)
        sys.exit(1)

    try:
        content = doc_path.read_text(encoding="utf-8")
    except Exception as e:
        click.echo(f"❌ Failed to read file {doc_path}: {e}", err=True)
        sys.exit(1)

    frontmatter_lines, body_lines = parse_frontmatter(content)
    if frontmatter_lines is None:
        click.echo("❌ Invalid markdown file: YAML frontmatter not found.", err=True)
        sys.exit(1)

    repos_dict = extract_repos(frontmatter_lines)
    if not repos_dict:
        click.echo("⚠️  No repositories found under 'repos:' in frontmatter.")
        sys.exit(0)

    # 1. Structure Verification
    click.echo("🔍 Verifying context document structure...")
    body_str = "".join(body_lines)
    if "## Current Implementation" not in body_str:
        click.echo("❌ Missing '## Current Implementation' section.", err=True)
        sys.exit(1)

    for r_name in repos_dict.keys():
        h3_repo = f"### {r_name}"
        if h3_repo not in body_str:
            click.echo(f"❌ Missing repository subheading '{h3_repo}' under Current Implementation.", err=True)
            sys.exit(1)

        if "#### Relevant File Tree" not in body_str:
            click.echo(f"❌ Missing '#### Relevant File Tree' section for repository {r_name}.", err=True)
            sys.exit(1)

        if "#### Implementation Details" not in body_str:
            click.echo(f"❌ Missing '#### Implementation Details' section for repository {r_name}.", err=True)
            sys.exit(1)

    # 2. SHA Freshness/Integrity Verification
    click.echo(f"📁 Context Document: {doc_path}")
    click.echo("🔍 Resolving repositories and verifying SHAs...")
    mismatches = []
    for r_name, recorded_sha in repos_dict.items():
        resolved = resolve_repo_path(r_name, doc_path.parent)
        if not resolved:
            click.echo(f"❌ Could not resolve path for repository: {r_name}", err=True)
            sys.exit(1)
        if not is_git_repo(resolved):
            click.echo(f"❌ Repository '{r_name}' resolved to '{resolved}' is not a Git repository.", err=True)
            sys.exit(1)
        
        sha = generate_tree_sha(resolved)
        if not sha:
            click.echo(f"❌ Failed to generate Git tree SHA for {r_name}", err=True)
            sys.exit(1)
            
        match = sha == recorded_sha
        click.echo(f"  - {r_name} ({resolved}):")
        click.echo(f"    Current:  {sha}")
        click.echo(f"    Recorded: {recorded_sha if recorded_sha else '<none>'}")
        if match:
            click.echo("    Status:   \033[92m✓ MATCH\033[0m")
        else:
            click.echo("    Status:   \033[91m✗ MISMATCH\033[0m")
            mismatches.append(r_name)

    click.echo()
    if mismatches:
        click.echo(f"❌ Verification failed! The following repos do not match: {', '.join(mismatches)}", err=True)
        sys.exit(1)
    else:
        click.echo("✅ Verification passed! All repo SHAs match.")
        sys.exit(0)


# =========================================================================
# Design Document CLI Group
# =========================================================================

@click.group(name="design-doc")
def design_doc_group() -> None:
    """Commands to build and verify design documents."""
    pass


@design_doc_group.command(name="build")
@click.option("-r", "--repo", "repos", multiple=True, help="Target repository names or absolute paths")
@click.option("-c", "--context-doc", help="Referenced context document absolute path")
@click.option("-t", "--title", help="Design document title")
@click.option("-f", "--feature", "features", multiple=True, help="Target features (top-level scopes)")
@click.option("-s", "--scope", "scopes", multiple=True, help="Target scopes")
@click.option("-d", "--description", help="Design document description")
@click.option("-u", "--supersede", help="Design document to supersede")
def build_design_doc_command(
    repos: Tuple[str, ...],
    context_doc: Optional[str],
    title: Optional[str],
    features: Tuple[str, ...],
    scopes: Tuple[str, ...],
    description: Optional[str],
    supersede: Optional[str],
) -> None:
    """
    Build a new design document.

    Generates the next sequential path at /root/Desktop/design/design-YYYYMMDD-N.md.
    """
    doc_path = get_next_filename(Path("/root/Desktop/design"), "design")

    if doc_path.exists():
        click.echo(f"❌ File already exists at {doc_path}. build is for building new files only.", err=True)
        sys.exit(1)

    # Resolve supersede path and context path
    doc_supersedes = None
    resolved_supersede_path = None
    if supersede:
        resolved_supersede_path = validate_design_doc_path(supersede, check_exists=True)
        doc_supersedes = os.path.relpath(resolved_supersede_path, doc_path.parent)

    resolved_context_doc = context_doc
    if not resolved_context_doc and resolved_supersede_path:
        # Read superseded doc and extract context path
        try:
            supersede_content = resolved_supersede_path.read_text(encoding="utf-8")
            sup_fm, _ = parse_frontmatter(supersede_content)
            if sup_fm:
                sup_ctx_val = None
                for line in sup_fm:
                    if line.strip().startswith("context:"):
                        parts = line.split(":", 1)
                        sup_ctx_val = parts[1].strip().strip('"').strip("'")
                        break
                if sup_ctx_val:
                    resolved_context_doc = str((resolved_supersede_path.parent / sup_ctx_val).resolve())
                    click.echo(f"ℹ️ Derived context document from superseded document: {resolved_context_doc}")
        except Exception as e:
            click.echo(f"⚠️ Failed to parse context from superseded document: {e}", err=True)

    # Resolve context path and target repositories
    doc_context = None
    ctx_features = []
    ctx_scopes = []
    target_repos = list(repos)
    
    resolved_ctx = None
    if resolved_context_doc:
        resolved_ctx = validate_context_doc_path(resolved_context_doc, check_exists=True)

    if resolved_ctx:
        click.echo(f"🔍 Verifying freshness of referenced context document: {resolved_ctx}")
        try:
            ctx_content = resolved_ctx.read_text(encoding="utf-8")
            ctx_fm, _ = parse_frontmatter(ctx_content)
            if ctx_fm:
                ctx_repos = extract_repos(ctx_fm)
                ctx_features = extract_yaml_list(ctx_fm, "features")
                ctx_scopes = extract_yaml_list(ctx_fm, "scopes")
                
                mismatches = []
                for r_name, recorded_sha in ctx_repos.items():
                    resolved = resolve_repo_path(r_name, resolved_ctx.parent)
                    if resolved:
                        if not is_git_repo(resolved):
                            click.echo(f"❌ Repository '{r_name}' resolved to '{resolved}' is not a Git repository.", err=True)
                            sys.exit(1)
                        sha = generate_tree_sha(resolved)
                        if not sha or sha != recorded_sha:
                            mismatches.append(r_name)
                    else:
                        mismatches.append(r_name)
                        
                if mismatches:
                    if not repos: # context doc as sole input
                        click.echo(f"❌ Error: The referenced context document is stale (mismatches: {', '.join(mismatches)}). Please update context doc first.", err=True)
                        sys.exit(1)
                    else: # repos were provided with context doc
                        click.echo(f"⚠️ Warning: Context document freshness check failed (mismatches: {', '.join(mismatches)}). Context doc will not be linked and non-provided target repos will be omitted.")
                        doc_context = None
                        target_repos = list(repos)
                else:
                    click.echo("✅ Referenced context freshness verified.")
                    doc_context = os.path.relpath(resolved_ctx, doc_path.parent)
                    # Aggregate target repos
                    target_repos = list(repos)
                    for r_name in ctx_repos.keys():
                        if r_name not in target_repos:
                            target_repos.append(r_name)
        except Exception as e:
            click.echo(f"❌ Error: Could not verify context doc freshness: {e}", err=True)
            sys.exit(1)

    if not target_repos:
        click.echo("❌ Error: No target repositories specified. You must provide repositories using -r/--repo or link a valid context document via -c/--context-doc.", err=True)
        sys.exit(1)

    # Validate target repos are git repos
    for r_name in target_repos:
        resolved = resolve_repo_path(r_name, doc_path.parent)
        if not resolved:
            click.echo(f"❌ Could not resolve path for repository: {r_name}", err=True)
            sys.exit(1)
        if not is_git_repo(resolved):
            click.echo(f"❌ Repository '{r_name}' resolved to '{resolved}' is not a Git repository.", err=True)
            sys.exit(1)

    # Resolve target features and scopes
    target_features = list(features) if features else (ctx_features if ctx_features else ["[feature]"])
    target_scopes = list(scopes) if scopes else (ctx_scopes if ctx_scopes else ["[scope]"])

    # Determine feature scope string for description placeholder
    if features:
        feature_scope = ", ".join(features)
    elif scopes:
        feature_scope = ", ".join(scopes)
    elif ctx_features:
        feature_scope = ", ".join(ctx_features)
    elif ctx_scopes:
        feature_scope = ", ".join(ctx_scopes)
    else:
        feature_scope = "[feature/scope]"

    # Build properties
    doc_title = title or "[Short design title]"
    
    if description:
        doc_description = description
    else:
        doc_description = f"Design for {feature_scope}."

    # Construct frontmatter
    fm = []
    fm.append(f"title: {doc_title}\n")
    fm.append(f"description: {doc_description}\n")
    fm.append("status: draft\n")
    if doc_supersedes:
        fm.append(f"supersedes: {doc_supersedes}\n")
    if doc_context:
        fm.append(f"context: {doc_context}\n")
    if target_repos:
        fm.append("repos:\n")
        for r_name in target_repos:
            fm.append(f"  - {r_name}\n")
    fm.append("features:\n")
    for feat in target_features:
        fm.append(f"  - {feat}\n")
    fm.append("scopes:\n")
    for sc in target_scopes:
        fm.append(f"  - {sc}\n")

    # Build body
    body = []
    body.append(f"# {doc_title}\n\n")
    # Group Q&A by repository
    for r_name in target_repos:
        body.append(f"## {r_name}\n\n")
        body.append("### Q1. Design Question\n\n")
        body.append("Answer.\n\n")
    doc_body = "".join(body)

    # Assemble and write
    updated_content = "---\n" + "".join(fm) + "---\n" + doc_body

    click.echo(f"💾 Saving design document to: {doc_path}...")
    try:
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(updated_content, encoding="utf-8")
    except Exception as e:
        click.echo(f"❌ Failed to write design document: {e}", err=True)
        sys.exit(1)

    click.echo(f"✅ Design document successfully written to {doc_path}")


@design_doc_group.command(name="verify")
@click.argument("path", required=True, type=click.Path())
def design_doc_verify_command(path: str) -> None:
    """
    Verify the structure of a design document and the freshness of its baseline context.
    """
    design_path = validate_design_doc_path(path, check_exists=True)

    if not design_path.exists():
        click.echo(f"❌ Design document not found: {design_path}", err=True)
        sys.exit(1)

    try:
        design_content = design_path.read_text(encoding="utf-8")
    except Exception as e:
        click.echo(f"❌ Failed to read design document: {e}", err=True)
        sys.exit(1)

    design_fm_lines, design_body_lines = parse_frontmatter(design_content)
    if design_fm_lines is None:
        click.echo("❌ Invalid design document format: YAML frontmatter not found.", err=True)
        sys.exit(1)

    # 1. Structure Verification
    click.echo(f"📁 Design Document: {design_path}")
    click.echo("🔍 Verifying design document structure...")
    design_repos = extract_yaml_list(design_fm_lines, "repos")
    design_body_str = "".join(design_body_lines)
    for r_name in design_repos:
        h2_repo = f"## {r_name}"
        if h2_repo not in design_body_str:
            click.echo(f"❌ Missing repository subheading '{h2_repo}' in design document.", err=True)
            sys.exit(1)

    # 2. Context Freshness Verification
    ctx_doc_path = get_referenced_context_path(design_fm_lines, design_path)
    if ctx_doc_path:
        validate_context_doc_path(str(ctx_doc_path), check_exists=True)

        try:
            ctx_content = ctx_doc_path.read_text(encoding="utf-8")
        except Exception as e:
            click.echo(f"❌ Failed to read context document: {e}", err=True)
            sys.exit(1)

        ctx_fm_lines, _ = parse_frontmatter(ctx_content)
        if ctx_fm_lines is None:
            click.echo("❌ Invalid context document format: YAML frontmatter not found.", err=True)
            sys.exit(1)

        ctx_repos = extract_repos(ctx_fm_lines)
        if not ctx_repos:
            click.echo("⚠️  No repositories found in context document to verify.")
            sys.exit(0)

        click.echo(f"📁 Verifying context freshness: {ctx_doc_path}")
        mismatches = []
        for r_name, recorded_sha in ctx_repos.items():
            resolved = resolve_repo_path(r_name, ctx_doc_path.parent)
            if not resolved:
                click.echo(f"❌ Could not resolve path for repository: {r_name}", err=True)
                sys.exit(1)
            if not is_git_repo(resolved):
                click.echo(f"❌ Repository '{r_name}' resolved to '{resolved}' is not a Git repository.", err=True)
                sys.exit(1)

            sha = generate_tree_sha(resolved)
            if not sha:
                click.echo(f"❌ Failed to generate Git tree SHA for {r_name}", err=True)
                sys.exit(1)

            match = sha == recorded_sha
            click.echo(f"  - {r_name} ({resolved}):")
            click.echo(f"    Current:  {sha}")
            click.echo(f"    Recorded: {recorded_sha if recorded_sha else '<none>'}")
            if match:
                click.echo("    Status:   \033[92m✓ FRESH\033[0m")
            else:
                click.echo("    Status:   \033[91m✗ STALE (MISMATCH)\033[0m")
                mismatches.append(r_name)

        click.echo()
        if mismatches:
            click.echo(f"❌ Mismatch detected! The following repos are stale: {', '.join(mismatches)}", err=True)
            sys.exit(1)
        else:
            click.echo("✅ Freshness verified! All repo SHAs match baseline.")
            sys.exit(0)
    else:
        click.echo("ℹ️  No referenced context document found (or linked) in design document.")
        sys.exit(0)
