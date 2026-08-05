"""
Git commit message generation using Tera AI.

This module provides a CLI command to generate conventional commit messages
using Tera AI with gemini-latest model, based on staged changes.
"""

import os
import sys
import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

from cli_tools import workspace
from cli_tools.inference.tera import get_client as get_tera_client

# Load environment variables
load_dotenv()
_desktop_env = Path("/root/Desktop/cli-tools/.env")
if _desktop_env.exists():
    load_dotenv(_desktop_env)

COMMIT_SYSTEM_INSTRUCTION = """Generate a commit message following Conventional commits specification.

The commit message should:
1. Follow the format: <type>: <subject> or <type>(<scope>): <subject>
2. Use one of these types: feat, fix, docs, style, refactor, perf, test, chore, ci, build
3. Keep the subject line under 75 characters
4. Use imperative mood in the subject line
5. Don't end the subject line with a period
6. Optionally include a body that explains the changes in detail
7. Optionally include footers for issue references
8. Mark a change as breaking only when it makes an existing public or relied-upon
   API, CLI, configuration, protocol, data format, or behavior incompatible and
   requires consumer or operator action
9. For a breaking change, append ! before the colon and include a
   BREAKING CHANGE: footer that explains the required action
10. Do not mark additive changes, internal refactors, or documentation of a
    future breaking change as breaking
11. Any change that breaks external behavior (i.e., is a breaking change) must
    be labeled as 'feat', even if it would otherwise be classified as another
    type like 'refactor' or 'chore'
12. Return only the commit message, without Markdown code fences or commentary

NOTE: Always revise your generation, make sure every line is under 75 chars.

Analyze the provided git diff and generate an appropriate commit message."""

_OUTER_CODE_FENCE = re.compile(
    r"\A[ \t]*(?P<fence>`{3,}|~{3,})[^\r\n]*\r?\n"
    r"(?P<body>.*?)\r?\n[ \t]*(?P=fence)[ \t]*\Z",
    re.DOTALL,
)


def normalize_commit_message(message: str) -> str:
    """Trim model output and unwrap one Markdown fence enclosing the full message."""
    normalized = message.strip().lstrip("\ufeff")
    fenced = _OUTER_CODE_FENCE.fullmatch(normalized)
    if fenced:
        normalized = fenced.group("body").strip()
    return normalized


async def generate_commit_message(
    diff_output: str,
    recent_commits: Optional[str] = None,
    instructions: Optional[str] = None,
) -> str:
    """
    Generate a commit message using Tera AI.

    Args:
        diff_output: Git diff output to analyze
        recent_commits: Optional string containing recent commit history
        instructions: Optional extra instructions from the user

    Returns:
        Generated commit message

    Raises:
        Exception: If API call fails
    """
    # Prepare user message with diff and recent commits
    user_message_parts = ["Generate a commit message for the following changes:\n"]

    if recent_commits:
        user_message_parts.append(
            f"\nRecent commit history (follow this pattern):\n```\n{recent_commits}\n```\n"
        )

    if instructions:
        user_message_parts.append(
            f"\nAdditional instructions from the user:\n{instructions}\n"
        )

    user_message_parts.append(f"\n```diff\n{diff_output}\n```")
    user_message = "".join(user_message_parts)

    try:
        client = get_tera_client()

        result = await client.complete(
            system_prompt=COMMIT_SYSTEM_INSTRUCTION,
            user_prompt=user_message,
            model="gemini-latest",
            temperature=0.7,
            reasoning_effort="high",
        )

        if not result:
            raise RuntimeError("No response from Tera API")

        # Handle both (content, usage) and (content, reasoning, usage) returns
        if len(result) == 3:
            commit_message, _reasoning, usage = result
        else:
            commit_message, usage = result

        commit_message = normalize_commit_message(commit_message)
        if not commit_message:
            raise RuntimeError("Empty response from Tera API")

        return commit_message

    except Exception as e:
        raise RuntimeError(f"Failed to generate commit message: {e}") from e


def run_git_command(args: list[str], cwd: str) -> tuple[int, str, str]:
    """
    Run a git command and return the result.

    Args:
        args: Git command arguments (e.g., ['add', '--all'])
        cwd: Working directory for git command

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # Replace invalid chars instead of failing
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return (
            1,
            "",
            "git command not found. Please ensure git is installed and in PATH.",
        )


def plan_document_subject(action: str, documents: list[str]) -> str:
    """Build the operational subject used for plan document changes."""
    design_docs = [name for name in documents if name.startswith("design-")]
    requirements_docs = [
        name for name in documents if name.startswith("requirements-")
    ]
    if len(documents) == 2 and len(design_docs) == len(requirements_docs) == 1:
        target = f"{design_docs[0]} and {requirements_docs[0]} pair"
    else:
        target = " and ".join(documents)
    return f"docs: {action} {target}"


def plan_repository_instructions(
    created_docs: list[str],
    updated_docs: list[str],
) -> str:
    """Build deterministic commit instructions for the plan repository."""
    rules = [
        "For the /root/Desktop/plan repository, follow these rules:",
        "- Use the 'docs' type for design and requirements document changes.",
        "- Start the subject with 'create' or 'update' and include the affected "
        "document filenames.",
        "- Include a brief body describing newly created designs.",
    ]

    if created_docs or updated_docs:
        rules.append("\nSpecifically for the currently staged changes:")
    if created_docs:
        rules.append(f"- Created files: {', '.join(created_docs)}")
        rules.append(
            f"  The subject line MUST be: "
            f"'{plan_document_subject('create', created_docs)}'."
        )
    if updated_docs:
        rules.append(f"- Updated files: {', '.join(updated_docs)}")
        rules.append(
            f"  The subject line MUST be: "
            f"'{plan_document_subject('update', updated_docs)}'."
        )
    return "\n".join(rules)


def get_recent_commits(cwd: str, num_commits: int = 5) -> Optional[str]:
    """
    Get recent commit history for context.

    Args:
        cwd: Working directory for git command
        num_commits: Number of recent commits to fetch (default: 5)

    Returns:
        String containing formatted recent commits, or None if unavailable
    """
    # Use --no-decorate to avoid tag/branch decorations, --oneline for brevity
    returncode, stdout, stderr = run_git_command(
        ["log", "-n", str(num_commits), "--oneline", "--no-decorate"], cwd
    )

    if returncode != 0 or not stdout.strip():
        return None

    return stdout.strip()


def get_current_branch(cwd: str) -> Optional[str]:
    """
    Get the checked-out branch name, or None when HEAD is detached.

    Args:
        cwd: Working directory for git command

    Returns:
        Branch name, or None for detached HEAD (or failure)
    """
    returncode, stdout, _ = run_git_command(["branch", "--show-current"], cwd)
    if returncode != 0:
        return None
    branch = stdout.strip()
    return branch or None


def get_staged_names(cwd: str) -> set[str]:
    """
    Get the set of pathnames currently staged in the index.

    Args:
        cwd: Working directory for git command

    Returns:
        Set of staged pathnames (NUL-separated output, safe for odd names)
    """
    returncode, stdout, _ = run_git_command(
        ["diff", "--cached", "--name-only", "-z"], cwd
    )
    if returncode != 0 or not stdout:
        return set()
    return {name for name in stdout.split("\0") if name}


def resolve_push_args(cwd: str, branch: str) -> tuple[list[str], str, bool]:
    """
    Build the git push argv for a branch, honoring its configured upstream.

    If the branch has an upstream (possibly on a different remote and/or with a
    different remote branch name), push to that remote/ref. Otherwise fall back
    to pushing to origin/<branch>, which may create a new remote branch.

    Args:
        cwd: Working directory for git command
        branch: Local branch name to push

    Returns:
        Tuple of (push argv after "git push", human description of the target,
        whether this is the origin/<branch> fallback)
    """
    returncode, upstream, _ = run_git_command(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd
    )
    upstream = upstream.strip()
    if returncode == 0 and upstream:
        remote, _, remote_branch = upstream.partition("/")
        if remote and remote_branch:
            return (
                [remote, f"{branch}:{remote_branch}"],
                f"{remote}/{remote_branch}",
                False,
            )
    return (["origin", branch], f"origin/{branch}", True)


# Exit code used when the commit succeeded but the push failed, so callers
# (scripts and AI agents) can distinguish "nothing was recorded" (exit 1)
# from "commit landed locally, only the push failed" (exit 3).
EXIT_PUSH_FAILED = 3


@click.command()
@click.argument("path", type=click.Path(exists=True), default=".")
@click.option(
    "--user",
    is_flag=True,
    help="Override the workspace preferences (see 'tool setup workspace'): "
    "skip the worktree-pr policy check and commit in the project's main "
    "checkout. Reserved for direct human use; the override is noted in the "
    "output so it stays observable.",
)
@click.option(
    "--push/--no-push",
    "push",
    default=False,
    help="Push after committing. Default is --no-push: the commit stays local. "
    "Pushes to the branch's configured upstream if it has one, otherwise to "
    "origin/<branch>.",
)
@click.option(
    "-i",
    "--instructions",
    type=str,
    default=None,
    help="Extra instructions to include in the commit message generation prompt "
    "(mutually exclusive with --message)",
)
@click.option(
    "-m",
    "--message",
    "message",
    type=str,
    default=None,
    help="Use this commit message verbatim, skipping AI generation",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the commit message and exit without committing. Restores the "
    "index to its prior state.",
)
@click.option(
    "--only",
    "only_paths",
    multiple=True,
    metavar="PATHSPEC",
    help=(
        "Stage and commit only the given Git pathspec. Repeat for multiple "
        "pathspecs; requires an initially clean index."
    ),
)
@click.option(
    "--staged",
    is_flag=True,
    help="Commit exactly the changes already staged in the index.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print a machine-readable JSON summary as the last stdout line.",
)
def commit_command(
    path: str,
    user: bool,
    push: bool,
    instructions: Optional[str],
    message: Optional[str],
    dry_run: bool,
    only_paths: tuple[str, ...],
    staged: bool,
    json_output: bool,
) -> None:
    """
    Generate a conventional commit message using Tera AI.

    The command is fully deterministic and never prompts: all behavior is
    controlled by arguments and flags, so it is safe to call from scripts
    and AI agents with any stdin.

    \b
    Staging modes (mutually exclusive):
      (default)         stage all changes with 'git add .'
      --only PATHSPEC   stage only the given pathspecs (needs a clean index)
      --staged          use the index exactly as it is

    \b
    Behavior:
      Commits immediately; use --dry-run to preview the message first.
      Pushes only when --push is given.
      Rejects commits made in the main checkout of a project whose workspace
      preferences set workflow "worktree-pr" — use a linked worktree instead,
      or pass --user to override (logged). See 'tool setup workspace'.

    \b
    Exit codes:
      0  success, or nothing staged to commit
      1  failure, invalid invocation context, or workspace policy rejection
      2  usage error (conflicting/invalid flags)
      3  commit succeeded but the requested push failed

    \b
    Examples:
        tool commit
        tool commit /path/to/repo --push
        tool commit . --push               # commit and push
        tool commit .                      # commit locally, no push
        tool commit . --dry-run            # preview the generated message
        tool commit . -m "fix: typo"       # skip AI generation
        tool commit . --only src --only tests
        tool commit . --staged
        tool commit . --user               # override a worktree-pr policy refusal
        tool commit . --instructions "focus on performance improvements"

    PATH: Repository path (defaults to current directory)
    """
    repo_path = Path(path).resolve()

    # Check if it's a git repository
    returncode, _, stderr = run_git_command(["rev-parse", "--git-dir"], str(repo_path))
    if returncode != 0:
        click.echo(f"❌ Not a git repository: {repo_path}", err=True)
        sys.exit(1)

    if only_paths and staged:
        raise click.UsageError("--only and --staged are mutually exclusive")

    if message is not None and instructions is not None:
        raise click.UsageError("--message and --instructions are mutually exclusive")

    if dry_run and push:
        raise click.UsageError("--dry-run cannot be combined with --push")

    click.echo(f"📁 Repository: {repo_path}")
    click.echo()

    # Workspace policy: a project configured for the worktree+PR workflow
    # must not receive commits in its main checkout; its linked worktrees are
    # the intended commit targets. The check runs before any staging so a
    # rejected invocation never mutates the index.
    try:
        project = workspace.resolve_project(repo_path)
    except workspace.WorkspaceConfigError as exc:
        click.echo(f"⚠️  Ignoring unusable workspace config: {exc}", err=True)
        project = None
    policy_override = False
    if (
        project is not None
        and project.uses_worktree_pr
        and workspace.is_main_checkout(repo_path)
    ):
        if user:
            policy_override = True
            click.echo(
                f"⚠️  --user: overriding the worktree-pr workspace policy of "
                f"'{project.name}'; committing in its main checkout.",
                err=True,
            )
        else:
            click.echo(
                f"❌ Workspace policy: '{project.name}' is configured for the "
                f"worktree+PR workflow (see {workspace.default_config_path()}), "
                f"but {repo_path} is its main checkout.\n"
                f"   Work in a linked worktree on a branch and open a PR "
                f"instead, e.g.:\n"
                f"     git -C {project.path} worktree add <worktree-path> "
                f"-b <branch>\n"
                f"   (--user overrides this policy; it is reserved for direct "
                f"human use.)",
                err=True,
            )
            sys.exit(1)

    # Pre-flight checks: fail before mutating the index or paying for an AI
    # generation call when the invocation context cannot work.
    branch_name = get_current_branch(str(repo_path))
    if push and branch_name is None:
        click.echo(
            "❌ --push requested but HEAD is detached (nothing to push from). "
            "Re-run with --no-push to commit without pushing.",
            err=True,
        )
        sys.exit(1)

    staged_before = get_staged_names(str(repo_path))

    if staged:
        click.echo("📝 Using changes already staged in the index...")
    elif only_paths:
        if staged_before:
            click.echo(
                "❌ --only requires an initially clean index; commit or unstage "
                "existing changes, or use --staged after curating the index.",
                err=True,
            )
            sys.exit(1)

        rendered_paths = " ".join(only_paths)
        click.echo(f"📝 Staging selected pathspecs: {rendered_paths}")
        returncode, stdout, stderr = run_git_command(
            ["add", "--", *only_paths], str(repo_path)
        )
        if returncode != 0:
            click.echo(f"❌ Failed to stage selected changes: {stderr}", err=True)
            sys.exit(1)
    else:
        click.echo("📝 Staging all changes with 'git add .'...")
        returncode, stdout, stderr = run_git_command(["add", "."], str(repo_path))
        if returncode != 0:
            click.echo(f"❌ Failed to stage changes: {stderr}", err=True)
            sys.exit(1)

    # Step 2: Get diff
    click.echo("📊 Getting staged diff with 'git diff --cached'...")
    returncode, diff_output, stderr = run_git_command(
        ["diff", "--cached"], str(repo_path)
    )
    if returncode != 0:
        click.echo(f"❌ Failed to get diff: {stderr}", err=True)
        sys.exit(1)

    if not diff_output or not diff_output.strip():
        click.echo("ℹ️  No staged changes to commit")
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "repo": str(repo_path),
                        "branch": branch_name,
                        "committed": False,
                        "pushed": False,
                        "dry_run": dry_run,
                        "policy_override": policy_override,
                        "reason": "no staged changes",
                    }
                )
            )
        sys.exit(0)

    # Show diff summary
    lines = diff_output.split("\n")
    files_changed = [line for line in lines if line.startswith("diff --git")]
    click.echo(f"📄 Files changed: {len(files_changed)}")
    click.echo()

    if message is None:
        # Repository-specific instructions check
        repo_instructions = None
        if repo_path.resolve() == Path("/root/Desktop/plan").resolve():
            created_docs = []
            updated_docs = []
            status_code, status_stdout, status_stderr = run_git_command(
                ["diff", "--cached", "--name-status"], str(repo_path)
            )
            if status_code == 0:
                for line in status_stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        status, file_path = parts[0], parts[1]
                        p = Path(file_path)
                        is_doc = False
                        if file_path.startswith("design/") or file_path.startswith(
                            "requirements/"
                        ):
                            if p.name.startswith("design-") or p.name.startswith(
                                "requirements-"
                            ):
                                is_doc = True
                        if is_doc:
                            if status.startswith("A") or status.startswith("C"):
                                created_docs.append(p.name)
                            elif status.startswith("M") or status.startswith("R"):
                                updated_docs.append(p.name)

            repo_instructions = plan_repository_instructions(
                created_docs, updated_docs
            )

        # Combine instructions
        combined_instructions = []
        if repo_instructions:
            combined_instructions.append(repo_instructions)
        if instructions:
            combined_instructions.append(instructions)
        final_instructions = (
            "\n\n".join(combined_instructions) if combined_instructions else None
        )

        # Get recent commits for context
        click.echo("📜 Fetching recent commits for context...")
        recent_commits = get_recent_commits(str(repo_path), num_commits=5)
        if recent_commits:
            click.echo("✅ Found recent commit history")
        else:
            click.echo("⚠️  No recent commits found (new repository or shallow clone)")
        click.echo()

        # Generate commit message with Tera AI
        click.echo("🤖 Generating commit message with Tera AI...")
        try:
            commit_message = asyncio.run(
                generate_commit_message(
                    diff_output, recent_commits, final_instructions
                )
            )
        except Exception as e:
            click.echo(f"❌ Failed to generate commit message: {e}", err=True)
            sys.exit(1)
        message_source = "generated"
    else:
        commit_message = message.strip().lstrip("\ufeff")
        if not commit_message:
            click.echo("❌ --message must not be empty", err=True)
            sys.exit(1)
        message_source = "provided"

    # Display the message
    click.echo()
    click.echo("=" * 70)
    click.echo(f"Commit Message ({message_source}):")
    click.echo("=" * 70)
    click.echo(commit_message)
    click.echo("=" * 70)
    click.echo()

    if dry_run:
        # Restore the index to its prior state: unstage exactly what the
        # staging step added, leaving any pre-existing index content intact.
        newly_staged = sorted(get_staged_names(str(repo_path)) - staged_before)
        if newly_staged:
            run_git_command(["reset", "-q", "--", *newly_staged], str(repo_path))
        click.echo("🔍 Dry run: nothing committed; index restored to prior state.")
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "repo": str(repo_path),
                        "branch": branch_name,
                        "committed": False,
                        "pushed": False,
                        "dry_run": True,
                        "policy_override": policy_override,
                        "message": commit_message,
                        "message_source": message_source,
                    }
                )
            )
        return

    click.echo("💾 Committing changes...")
    returncode, stdout, stderr = run_git_command(
        ["commit", "-m", commit_message], str(repo_path)
    )

    if returncode != 0:
        click.echo(f"❌ Failed to commit: {stderr}", err=True)
        sys.exit(1)

    _, short_hash, _ = run_git_command(["rev-parse", "--short", "HEAD"], str(repo_path))
    short_hash = short_hash.strip()

    click.echo("✅ Changes committed successfully!")
    click.echo(stdout)

    # Push only when explicitly requested; detached HEAD was pre-validated
    # above for that case.
    pushed = False
    push_description = None
    push_failed = False

    if push:
        push_argv, push_description, is_fallback = resolve_push_args(
            str(repo_path), branch_name
        )
        if is_fallback:
            click.echo(
                f"⚠️  No upstream configured for '{branch_name}'; pushing to "
                f"{push_description} (may create a new remote branch)."
            )
        click.echo(f"📤 Pushing to {push_description}...")
        returncode, stdout, stderr = run_git_command(
            ["push", *push_argv], str(repo_path)
        )

        if returncode != 0:
            push_failed = True
            click.echo(
                f"⚠️  Commit {short_hash} succeeded locally but the push to "
                f"{push_description} failed: {stderr.strip()}",
                err=True,
            )
        else:
            pushed = True
            click.echo(f"✅ Pushed to {push_description} successfully!")
            click.echo(stdout)

    if json_output:
        click.echo(
            json.dumps(
                {
                    "repo": str(repo_path),
                    "branch": branch_name,
                    "commit": short_hash,
                    "committed": True,
                    "pushed": pushed,
                    "push_target": push_description if pushed else None,
                    "push_failed": push_failed,
                    "dry_run": False,
                    "policy_override": policy_override,
                    "message": commit_message,
                    "message_source": message_source,
                }
            )
        )

    if push_failed:
        sys.exit(EXIT_PUSH_FAILED)
