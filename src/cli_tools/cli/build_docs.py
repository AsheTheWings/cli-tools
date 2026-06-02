"""
Document builder commands.

This module provides commands to build, initialize, and verify context and
design documents with repository Git tree SHAs.
"""

import os
import sys
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
    res = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
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


def is_purely_backend(repo_name: str) -> bool:
    """Heuristic to determine if a repository is purely backend/infrastructure."""
    repo_lower = repo_name.lower()
    return "backend" in repo_lower or "server" in repo_lower or "mcp" in repo_lower


def is_git_repo(repo_path: Path) -> bool:
    """Check if the given directory path is a valid Git repository."""
    if not repo_path.is_dir():
        return False
    return (repo_path / ".git").exists()


# =========================================================================
# Subcommand: build-context-doc
# =========================================================================

@click.command(name="build-context-doc")
@click.argument("path", required=False, type=click.Path())
@click.option("-r", "--repo", "repos", multiple=True, help="Target repository names or paths")
@click.option("-v", "--verify", is_flag=True, help="Verify recorded SHAs in context doc")
@click.option("-t", "--title", help="Context document title")
@click.option("-f", "--feature", "features", multiple=True, help="Target features (top-level scopes)")
@click.option("-s", "--scope", "scopes", multiple=True, help="Target scopes")
@click.option("-d", "--description", help="Context document description")
def build_context_doc_command(
    path: Optional[str],
    repos: Tuple[str, ...],
    verify: bool,
    title: Optional[str],
    features: Tuple[str, ...],
    scopes: Tuple[str, ...],
    description: Optional[str],
) -> None:
    """
    Build, update, or verify a context document.

    If path is not provided when creating/updating, defaults to the next sequential path
    at /root/Desktop/context/context-YYYYMMDD-N.md.
    """
    today_dash = datetime.now().strftime("%Y-%m-%d")

    if verify:
        if path:
            doc_path = Path(path).resolve()
        else:
            doc_path = get_latest_filename(Path("/root/Desktop/context"), "context")
            if not doc_path:
                click.echo("❌ No context document found to verify.", err=True)
                sys.exit(1)
        
        if not doc_path.exists():
            click.echo(f"❌ File not found: {doc_path}", err=True)
            sys.exit(1)

        try:
            content = doc_path.read_text(encoding="utf-8")
        except Exception as e:
            click.echo(f"❌ Failed to read file {doc_path}: {e}", err=True)
            sys.exit(1)

        frontmatter_lines, _ = parse_frontmatter(content)
        if frontmatter_lines is None:
            click.echo("❌ Invalid markdown file: YAML frontmatter not found.", err=True)
            sys.exit(1)

        repos_dict = extract_repos(frontmatter_lines)
        if not repos_dict:
            click.echo("⚠️  No repositories found under 'repos:' in frontmatter.")
            sys.exit(0)

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

    # Creating/updating mode
    if path:
        doc_path = Path(path).resolve()
        is_new = not doc_path.exists()
    else:
        # If repositories are provided, we initialize a new context doc
        if repos:
            doc_path = get_next_filename(Path("/root/Desktop/context"), "context")
            is_new = True
        else:
            # If no repositories provided, update the latest context doc
            doc_path = get_latest_filename(Path("/root/Desktop/context"), "context")
            if not doc_path:
                click.echo("❌ No context document found to update.", err=True)
                sys.exit(1)
            is_new = False

    # Read existing content if file exists
    existing_frontmatter = {}
    existing_repos = {}
    existing_features = []
    existing_scopes = []
    existing_body = None
    existing_created = None

    if not is_new:
        try:
            content = doc_path.read_text(encoding="utf-8")
            frontmatter_lines, body_lines = parse_frontmatter(content)
            if frontmatter_lines is not None:
                existing_repos = extract_repos(frontmatter_lines)
                existing_features = extract_yaml_list(frontmatter_lines, "features")
                existing_scopes = extract_yaml_list(frontmatter_lines, "scopes")
                existing_body = "".join(body_lines)
                # Simple extraction of key properties to keep
                for line in frontmatter_lines:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        existing_frontmatter[k] = v
                existing_created = existing_frontmatter.get("created")
        except Exception as e:
            click.echo(f"⚠️  Failed to read existing file: {e}. Overwriting as new.")
            is_new = True

    # Resolve what repositories to check
    target_repos = list(repos)
    if not target_repos:
        if existing_repos:
            target_repos = list(existing_repos.keys())
        else:
            click.echo("❌ Please specify repositories using -r/--repo option.", err=True)
            sys.exit(1)

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
    target_features = list(features)
    if not target_features:
        if existing_features:
            target_features = existing_features
        else:
            target_features = ["[feature]"]

    target_scopes = list(scopes)
    if not target_scopes:
        if existing_scopes:
            target_scopes = existing_scopes
        else:
            target_scopes = ["[scope]"]

    # Determine feature scope string for description placeholder
    if features:
        feature_scope = ", ".join(features)
    elif scopes:
        feature_scope = ", ".join(scopes)
    else:
        feature_scope = "[feature_scope]"

    # Build properties
    doc_title = title or existing_frontmatter.get("title") or "[Short context title]"
    
    if description:
        doc_description = description
    elif existing_frontmatter.get("description"):
        doc_description = existing_frontmatter.get("description")
    else:
        doc_description = f"Current behavior and implementation context for {feature_scope}."

    doc_created = existing_created or today_dash

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
    fm.append(f"created: {doc_created}\n")
    fm.append(f"updated: {today_dash}\n")

    # Build body
    if is_new or not existing_body:
        body = []
        body.append(f"# {doc_title}\n\n")
        body.append("## Current Behaviors\n\n")
        
        # Behaviors subheadings
        for r_name in target_repos:
            if not is_purely_backend(r_name):
                body.append(f"### {r_name}\n\n")
                body.append("#### Relevant File Tree\n- TBD by worker Pass 1.\n\n")
                body.append("#### Factual Behaviors\n- TBD by worker Pass 1.\n\n")
                
        body.append("## Current Implementation\n\n")
        
        # Implementation subheadings
        for r_name in target_repos:
            body.append(f"### {r_name}\n\n")
            if is_purely_backend(r_name):
                body.append("#### Relevant File Tree (only if purely backend/omitted from Current Behaviors)\n- TBD by worker Pass 1.\n\n")
            body.append("#### Implementation Details\n- TBD by worker Pass 1.\n\n")
            
        doc_body = "".join(body)
    else:
        doc_body = existing_body

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


# =========================================================================
# Subcommand: build-design-doc
# =========================================================================

@click.command(name="build-design-doc")
@click.argument("path", required=False, type=click.Path())
@click.option("-r", "--repo", "repos", multiple=True, help="Target repository names or absolute paths")
@click.option("-c", "--context-doc", help="Referenced context document absolute path")
@click.option("-v", "--verify", is_flag=True, help="Verify freshness of target repos")
@click.option("-t", "--title", help="Design document title")
@click.option("-f", "--feature", "features", multiple=True, help="Target features (top-level scopes)")
@click.option("-s", "--scope", "scopes", multiple=True, help="Target scopes")
@click.option("-g", "--goal", help="Design goal")
@click.option("-d", "--description", help="Design document description")
def build_design_doc_command(
    path: Optional[str],
    repos: Tuple[str, ...],
    context_doc: Optional[str],
    verify: bool,
    title: Optional[str],
    features: Tuple[str, ...],
    scopes: Tuple[str, ...],
    goal: Optional[str],
    description: Optional[str],
) -> None:
    """
    Build, initialize, or verify a design document.

    If path is not provided when creating, defaults to next sequential path
    at /root/Desktop/design/design-YYYYMMDD-N.md.
    """
    today_dash = datetime.now().strftime("%Y-%m-%d")

    if verify:
        # Resolve target context document to verify
        ctx_doc_path = None
        if path:
            design_path = Path(path).resolve()
        else:
            design_path = get_latest_filename(Path("/root/Desktop/design"), "design")

        if design_path and design_path.exists():
            try:
                content = design_path.read_text(encoding="utf-8")
                frontmatter_lines, _ = parse_frontmatter(content)
                if frontmatter_lines is not None:
                    ctx_doc_path = get_referenced_context_path(frontmatter_lines, design_path)
            except Exception as e:
                click.echo(f"⚠️  Failed to parse context from design document: {e}", err=True)

        if not ctx_doc_path and context_doc:
            ctx_doc_path = Path(context_doc).resolve()

        if not ctx_doc_path:
            # Fallback to the latest context document
            ctx_doc_path = get_latest_filename(Path("/root/Desktop/context"), "context")

        if not ctx_doc_path or not ctx_doc_path.exists():
            click.echo("❌ Could not locate context document to verify.", err=True)
            sys.exit(1)

        # Run verification using helper logic
        try:
            content = ctx_doc_path.read_text(encoding="utf-8")
        except Exception as e:
            click.echo(f"❌ Failed to read context document: {e}", err=True)
            sys.exit(1)

        frontmatter_lines, _ = parse_frontmatter(content)
        if frontmatter_lines is None:
            click.echo("❌ Invalid context document format: YAML frontmatter not found.", err=True)
            sys.exit(1)

        repos_dict = extract_repos(frontmatter_lines)
        if not repos_dict:
            click.echo("⚠️  No repositories found in context document to verify.")
            sys.exit(0)

        click.echo(f"📁 Verifying context freshness: {ctx_doc_path}")
        mismatches = []
        for r_name, recorded_sha in repos_dict.items():
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

    # Creating/updating mode
    if path:
        doc_path = Path(path).resolve()
        is_new = not doc_path.exists()
    else:
        # If context_doc or repos are provided, initialize a new design doc
        if context_doc or repos:
            doc_path = get_next_filename(Path("/root/Desktop/design"), "design")
            is_new = True
        else:
            # Otherwise, update the latest design doc
            doc_path = get_latest_filename(Path("/root/Desktop/design"), "design")
            if not doc_path:
                click.echo("❌ No design document found to update.", err=True)
                sys.exit(1)
            is_new = False

    existing_frontmatter = {}
    existing_repos = []
    existing_features = []
    existing_scopes = []
    existing_body = None
    existing_created = None

    if not is_new:
        try:
            content = doc_path.read_text(encoding="utf-8")
            frontmatter_lines, body_lines = parse_frontmatter(content)
            if frontmatter_lines is not None:
                existing_body = "".join(body_lines)
                existing_features = extract_yaml_list(frontmatter_lines, "features")
                existing_scopes = extract_yaml_list(frontmatter_lines, "scopes")
                for line in frontmatter_lines:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        existing_frontmatter[k.strip()] = v.strip().strip('"').strip("'")
                # Parse repos list if exists
                # Simple yaml array extractor
                in_repos_section = False
                for line in frontmatter_lines:
                    if line.strip().startswith("repos:"):
                        in_repos_section = True
                        continue
                    if in_repos_section:
                        if line.startswith(" ") or line.startswith("\t"):
                            item = line.strip().lstrip("-").strip().strip('"').strip("'")
                            if item:
                                existing_repos.append(item)
                        else:
                            in_repos_section = False
                existing_created = existing_frontmatter.get("created")
        except Exception as e:
            click.echo(f"⚠️  Failed to read existing file: {e}. Overwriting as new.")
            is_new = True

    # Resolve context path
    doc_context = existing_frontmatter.get("context")
    ctx_features = []
    ctx_scopes = []
    
    if context_doc:
        resolved_ctx = Path(context_doc).resolve()
        # Verify freshness of context doc
        click.echo(f"🔍 Verifying freshness of referenced context document: {resolved_ctx}")
        if resolved_ctx.exists():
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
                            if sha and sha != recorded_sha:
                                mismatches.append(r_name)
                    if mismatches:
                        click.echo(f"❌ Error: The referenced context document is stale (mismatches: {', '.join(mismatches)}). Please update context doc first.", err=True)
                        sys.exit(1)
                    else:
                        click.echo("✅ Referenced context freshness verified.")
            except Exception as e:
                click.echo(f"❌ Error: Could not verify context doc freshness: {e}", err=True)
                sys.exit(1)

        # Set relative context path
        if is_new or context_doc:
            doc_context = os.path.relpath(resolved_ctx, doc_path.parent)

    # Determine target repos
    target_repos = list(repos)
    if not target_repos:
        if existing_repos:
            target_repos = existing_repos
        elif context_doc:
            # Copy repos from context doc
            resolved_ctx = Path(context_doc).resolve()
            if resolved_ctx.exists():
                try:
                    ctx_content = resolved_ctx.read_text(encoding="utf-8")
                    ctx_fm, _ = parse_frontmatter(ctx_content)
                    if ctx_fm:
                        target_repos = list(extract_repos(ctx_fm).keys())
                except Exception:
                    pass

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
    target_features = list(features)
    if not target_features:
        if existing_features:
            target_features = existing_features
        elif ctx_features:
            target_features = ctx_features
        else:
            target_features = ["[feature]"]

    target_scopes = list(scopes)
    if not target_scopes:
        if existing_scopes:
            target_scopes = existing_scopes
        elif ctx_scopes:
            target_scopes = ctx_scopes
        else:
            target_scopes = ["[scope]"]

    # Determine design goal placeholder
    if goal:
        design_goal = goal
    elif features:
        design_goal = ", ".join(features)
    elif scopes:
        design_goal = ", ".join(scopes)
    else:
        design_goal = "[design_goal]"

    # Build properties
    doc_title = title or existing_frontmatter.get("title") or "[Short design title]"
    
    if description:
        doc_description = description
    elif existing_frontmatter.get("description"):
        doc_description = existing_frontmatter.get("description")
    else:
        doc_description = f"Design for {design_goal}."

    doc_created = existing_created or today_dash

    # Construct frontmatter
    fm = []
    fm.append(f"title: {doc_title}\n")
    fm.append(f"description: {doc_description}\n")
    fm.append("status: draft\n")
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
    fm.append(f"created: {doc_created}\n")
    fm.append(f"updated: {today_dash}\n")

    # Build body
    if is_new or not existing_body:
        body = []
        body.append(f"# {doc_title}\n\n")
        body.append("## Desired Behaviors\n\n")
        body.append("TBD.\n\n")
        body.append("## Q1. Design Question\n\n")
        body.append("Answer.\n")
        doc_body = "".join(body)
    else:
        doc_body = existing_body

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

