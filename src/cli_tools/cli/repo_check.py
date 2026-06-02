"""
Context document management.

This module provides commands to generate, insert, and verify Git tree SHAs
for target repositories listed in context documents.
"""

import os
import sys
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

import click


def resolve_repo_path(repo_name: str, context_doc_dir: Path) -> Optional[Path]:
    """Resolve a repository name to its actual directory path on the filesystem."""
    # Try as direct absolute path
    p = Path(repo_name)
    if p.is_absolute() and p.is_dir():
        return p

    # Try relative to context document directory
    p = (context_doc_dir / repo_name).resolve()
    if p.is_dir():
        return p

    # Try under /root/Desktop/
    p = Path("/root/Desktop") / repo_name
    if p.is_dir():
        return p.resolve()

    # Try relative to current working directory
    p = (Path.cwd() / repo_name).resolve()
    if p.is_dir():
        return p

    return None


def generate_tree_sha(repo_path: Path) -> Optional[str]:
    """
    Generate the temporary-index Git tree SHA for a repository.
    
    This matches the workflow's temporary index tree SHA calculation:
    1. Creates a temporary index file.
    2. Runs `git read-tree --empty` on it.
    3. Runs `git add -A -- .` to add all tracked and untracked non-ignored files.
    4. Runs `git write-tree` to generate the SHA of this tree.
    """
    # Verify it is a Git repository
    res = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None

    # Create temporary index file
    fd, tmp_index = tempfile.mkstemp()
    os.close(fd)
    
    try:
        # Remove file as done in script before git operations
        if os.path.exists(tmp_index):
            os.remove(tmp_index)

        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = tmp_index

        # 1. Empty the index
        res = subprocess.run(
            ["git", "-C", str(repo_path), "read-tree", "--empty"],
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None

        # 2. Add all files to the temporary index
        res = subprocess.run(
            ["git", "-C", str(repo_path), "add", "-A", "--", "."],
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None

        # 3. Write tree and get SHA
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
    body_lines = lines[end_idx:]
    return frontmatter_lines, body_lines


def extract_repos(frontmatter_lines: List[str]) -> Dict[str, str]:
    """Extract repository names and their recorded SHAs from frontmatter lines."""
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
                    # Remove comments and quotes
                    repo_name = repo_name.split("#")[0].strip()
                    repo_name = repo_name.strip('"').strip("'")
                    
                    repo_val = parts[1].strip()
                    repo_val = repo_val.split("#")[0].strip()
                    repo_val = repo_val.strip('"').strip("'")
                    repos[repo_name] = repo_val
            else:
                in_repos = False
    return repos


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


@click.command()
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "-v",
    "--verify",
    is_flag=True,
    help="Verify recorded SHAs against current repository states",
)
def repo_check_command(path: str, verify: bool) -> None:
    """
    Manage and verify Git tree SHAs in context/design documents.

    If PATH is a design document (has 'context:' field), it resolves and acts
    on the referenced context document automatically.

    Examples:
        tool repo-check /root/Desktop/context/context-20260602-1.md
        tool repo-check /root/Desktop/context/context-20260602-1.md --verify
        tool repo-check /root/Desktop/design/design-20260602-1.md -v
    """
    doc_path = Path(path).resolve()
    
    # Read the file
    try:
        content = doc_path.read_text(encoding="utf-8")
    except Exception as e:
        click.echo(f"❌ Failed to read file {doc_path}: {e}", err=True)
        sys.exit(1)

    frontmatter_lines, body_lines = parse_frontmatter(content)
    if frontmatter_lines is None or body_lines is None:
        click.echo("❌ Invalid markdown file: YAML frontmatter not found.", err=True)
        sys.exit(1)

    # Check if this is a design document referencing a context document
    ref_context = get_referenced_context_path(frontmatter_lines, doc_path)
    if ref_context:
        click.echo(f"ℹ️  Detected design document referencing context: {ref_context.name}")
        doc_path = ref_context
        try:
            content = doc_path.read_text(encoding="utf-8")
        except Exception as e:
            click.echo(f"❌ Failed to read referenced context file {doc_path}: {e}", err=True)
            sys.exit(1)
        frontmatter_lines, body_lines = parse_frontmatter(content)
        if frontmatter_lines is None or body_lines is None:
            click.echo("❌ Referenced context file has invalid YAML frontmatter.", err=True)
            sys.exit(1)

    click.echo(f"📁 Context Document: {doc_path}")

    # Extract repositories from the frontmatter
    repos = extract_repos(frontmatter_lines)
    if not repos:
        click.echo("⚠️  No repositories found in 'repos:' section of the frontmatter.")
        sys.exit(0)

    # Resolve paths for all repositories
    click.echo("🔍 Resolving repositories...")
    resolved_paths: Dict[str, Path] = {}
    for r_name in repos.keys():
        resolved = resolve_repo_path(r_name, doc_path.parent)
        if not resolved:
            click.echo(f"❌ Could not resolve path for repository: {r_name}", err=True)
            sys.exit(1)
        resolved_paths[r_name] = resolved
        click.echo(f"  - {r_name} => {resolved}")

    # Generate tree SHAs
    computed_shas: Dict[str, str] = {}
    if verify:
        click.echo("⚡ Generating tree SHAs and verifying...")
        mismatches = []
        for r_name, r_path in resolved_paths.items():
            sha = generate_tree_sha(r_path)
            if not sha:
                click.echo(f"❌ Failed to generate Git tree SHA for {r_name}", err=True)
                sys.exit(1)
            
            recorded_sha = repos.get(r_name, "")
            match = sha == recorded_sha
            
            click.echo(f"  - {r_name}:")
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
    else:
        click.echo("⚡ Generating tree SHAs...")
        for r_name, r_path in resolved_paths.items():
            sha = generate_tree_sha(r_path)
            if not sha:
                click.echo(f"❌ Failed to generate Git tree SHA for {r_name}", err=True)
                sys.exit(1)
            computed_shas[r_name] = sha
            click.echo(f"  - {r_name} => {sha}")

        # Construct new frontmatter lines with updated SHAs
        new_frontmatter_lines = []
        in_repos = False
        for line in frontmatter_lines:
            stripped = line.strip()
            if stripped.startswith("repos:"):
                new_frontmatter_lines.append(line)
                in_repos = True
                # Insert the updated repos with values
                for r_name, r_sha in computed_shas.items():
                    new_frontmatter_lines.append(f"  {r_name}: {r_sha}\n")
                continue

            if in_repos:
                if line.startswith(" ") or line.startswith("\t"):
                    # Skip the old repository keys/values
                    continue
                else:
                    in_repos = False

            new_frontmatter_lines.append(line)

        # Assemble document
        updated_content = "---\n" + "".join(new_frontmatter_lines) + "---" + "".join(body_lines)
        
        click.echo("💾 Updating context document...")
        try:
            doc_path.write_text(updated_content, encoding="utf-8")
        except Exception as e:
            click.echo(f"❌ Failed to write updated context document: {e}", err=True)
            sys.exit(1)

        click.echo(f"✅ Successfully updated tree SHAs in {doc_path}")
