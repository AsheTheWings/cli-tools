"""
Git commit message generation using Tera AI.

This module provides a CLI command to generate conventional commit messages
using Tera AI with gemini-latest model, based on staged changes.
"""

import os
import sys
import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

from cli_tools.inference.tera import get_client as get_tera_client

# Load environment variables
load_dotenv()


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
    # System instructions for commit message generation
    system_instruction = """Generate a commit message following Conventional commits specification.

The commit message should:
1. Follow the format: <type>(<scope>): <subject>
2. Use one of these types: feat, fix, docs, style, refactor, perf, test, chore, ci, build
3. Keep the subject line under 75 characters
4. Use imperative mood in the subject line
5. Don't end the subject line with a period
6. Optionally include a body that explains the changes in detail
7. Optionally include a footer for breaking changes or issue references

NOTE: Always revise your generation, make sure every line is under 75 chars.

Analyze the provided git diff and generate an appropriate commit message."""

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
            system_prompt=system_instruction,
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

        if not commit_message.strip():
            raise RuntimeError("Empty response from Tera API")

        return commit_message.strip()

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


def undo_trailing_stage_commits(cwd: str) -> int:
    """
    Undo trailing 'stage' commits by soft-resetting to the latest non-'stage' commit.

    Scans the commit history from HEAD backward. If one or more consecutive
    commits have the message 'stage', performs `git reset --soft` to the hash
    of the latest commit that is *not* 'stage', effectively un-committing the
    trailing 'stage' commits while keeping their changes staged.

    Args:
        cwd: Working directory for git command

    Returns:
        Number of trailing 'stage' commits that were undone (0 if none found)
    """
    # Get commit hashes and subjects for the last 20 commits
    returncode, stdout, stderr = run_git_command(
        ["log", "-n", "20", "--format=%H %s"], cwd
    )

    if returncode != 0 or not stdout.strip():
        return 0

    lines = stdout.strip().splitlines()
    if not lines:
        return 0

    # Check if HEAD is a 'stage' commit
    head_hash, head_subject = lines[0].split(" ", 1)
    if head_subject.strip() != "stage":
        return 0

    # Walk back to find the latest non-'stage' commit
    target_hash: Optional[str] = None
    stage_count = 0

    for line in lines:
        commit_hash, subject = line.split(" ", 1)
        if subject.strip() == "stage":
            stage_count += 1
        else:
            target_hash = commit_hash
            break

    if target_hash is None:
        # Every commit in the scanned range is 'stage'; scan deeper
        returncode, stdout, stderr = run_git_command(
            ["log", "--format=%H %s", "--grep=^stage$", "--all-match"], cwd
        )
        # Fallback: if everything is 'stage', reset to the very first commit's parent
        # or just abort to avoid destructive behavior
        click.echo(
            "⚠️  All scanned commits are 'stage'. Aborting auto-reset to avoid data loss.",
            err=True,
        )
        return 0

    click.echo(
        f"🔄 Undoing {stage_count} trailing 'stage' commit(s) via soft reset to {target_hash[:12]}..."
    )
    returncode, stdout, stderr = run_git_command(["reset", "--soft", target_hash], cwd)

    if returncode != 0:
        click.echo(f"❌ Failed to undo 'stage' commits: {stderr}", err=True)
        return 0

    click.echo("✅ Undone trailing 'stage' commits. Changes are now staged.")
    return stage_count


@click.command()
@click.argument("path", type=click.Path(exists=True), default=".")
@click.option(
    "-y", "--yes", is_flag=True, help="Automatically commit and push without prompts"
)
@click.option(
    "-i",
    "--instructions",
    type=str,
    default=None,
    help="Extra instructions to include in the commit message generation prompt",
)
def commit_command(path: str, yes: bool, instructions: Optional[str]) -> None:
    """
    Generate a conventional commit message using Tera AI.

    This command:
    1. Stages all changes with 'git add .'
    2. Gets the diff with 'git diff HEAD'
    3. Uses Tera AI to generate a conventional commit message
    4. Prompts for confirmation (unless -y flag is used)
    5. Commits with the generated message if confirmed
    6. Optionally pushes to origin

    PATH: Repository path (defaults to current directory)

    Examples:
        tool commit
        tool commit /path/to/repo
        tool commit . -y  # Auto-commit and push
        tool commit . --instructions "focus on performance improvements"
    """
    repo_path = Path(path).resolve()

    click.echo(f"📁 Repository: {repo_path}")
    click.echo()

    # Check if it's a git repository
    returncode, _, stderr = run_git_command(["rev-parse", "--git-dir"], str(repo_path))
    if returncode != 0:
        click.echo(f"❌ Not a git repository: {repo_path}", err=True)
        sys.exit(1)

    # Pre-step: Undo any trailing "stage" commits so their changes are re-staged
    # and we can generate a real commit message on top of them.
    undone = undo_trailing_stage_commits(str(repo_path))
    if undone:
        click.echo()

    # Step 1: Stage changes in the specified path
    click.echo(f"📝 Staging changes with 'git add .'...")
    returncode, stdout, stderr = run_git_command(["add", "."], str(repo_path))
    if returncode != 0:
        click.echo(f"❌ Failed to stage changes: {stderr}", err=True)
        sys.exit(1)

    # Step 2: Get diff
    click.echo("📊 Getting diff with 'git diff HEAD'...")
    returncode, diff_output, stderr = run_git_command(["diff", "HEAD"], str(repo_path))
    if returncode != 0:
        click.echo(f"❌ Failed to get diff: {stderr}", err=True)
        sys.exit(1)

    if not diff_output or not diff_output.strip():
        click.echo("ℹ️  No changes to commit (working tree clean)")
        sys.exit(0)

    # Show diff summary
    lines = diff_output.split("\n")
    files_changed = [line for line in lines if line.startswith("diff --git")]
    click.echo(f"📄 Files changed: {len(files_changed)}")
    click.echo()

    # Step 3: Get recent commits for context
    click.echo("📜 Fetching recent commits for context...")
    recent_commits = get_recent_commits(str(repo_path), num_commits=5)
    if recent_commits:
        click.echo("✅ Found recent commit history")
    else:
        click.echo("⚠️  No recent commits found (new repository or shallow clone)")
    click.echo()

    # Step 4: Generate commit message with agent
    click.echo("🤖 Generating commit message with Tera AI...")
    try:
        commit_message = asyncio.run(
            generate_commit_message(diff_output, recent_commits, instructions)
        )
    except Exception as e:
        click.echo(f"❌ Failed to generate commit message: {e}", err=True)
        sys.exit(1)

    # Step 4: Display generated message
    click.echo()
    click.echo("=" * 70)
    click.echo("Generated Commit Message:")
    click.echo("=" * 70)
    click.echo(commit_message)
    click.echo("=" * 70)
    click.echo()

    # Step 5: Confirm and commit
    if yes or click.confirm("Proceed with this commit message?", default=True):
        click.echo("💾 Committing changes...")
        returncode, stdout, stderr = run_git_command(
            ["commit", "-m", commit_message], str(repo_path)
        )

        if returncode != 0:
            click.echo(f"❌ Failed to commit: {stderr}", err=True)
            sys.exit(1)

        click.echo("✅ Changes committed successfully!")
        click.echo(stdout)

        # Step 6: Ask about pushing to origin (or auto-push if -y flag)
        click.echo()
        if yes or click.confirm("Push to origin?", default=False):
            # Get current branch name
            returncode, branch_name, stderr = run_git_command(
                ["branch", "--show-current"], str(repo_path)
            )

            if returncode != 0:
                click.echo(f"❌ Failed to get current branch: {stderr}", err=True)
                sys.exit(1)

            branch_name = branch_name.strip()
            if not branch_name:
                click.echo("❌ No branch name found", err=True)
                sys.exit(1)

            click.echo(f"📤 Pushing to origin/{branch_name}...")
            returncode, stdout, stderr = run_git_command(
                ["push", "origin", branch_name], str(repo_path)
            )

            if returncode != 0:
                click.echo(f"❌ Failed to push: {stderr}", err=True)
                sys.exit(1)

            click.echo("✅ Pushed to origin successfully!")
            click.echo(stdout)
    else:
        click.echo("❌ Commit cancelled.")
        # Unstage changes
        click.echo("🔄 Unstaging changes...")
        run_git_command(["reset", "HEAD"], str(repo_path))
        sys.exit(1)
