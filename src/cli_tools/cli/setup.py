"""
Scaffolding commands for local CLI tool configuration.

``tool setup`` is the home for commands that create (and later maintain) the
configuration files consumed by other ``tool`` subcommands and by agent
instructions, starting with the workspace preferences file.
"""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from cli_tools import workspace

WORKSPACE_SCAFFOLD = {
    "schemaVersion": workspace.SCHEMA_VERSION,
    "projects": {},
    "_example": {
        "projects": {
            "my-project": {
                "path": "/root/Desktop/my-project",
                "workflow": "worktree-pr",
            },
            "scratch-notes": {
                "path": "/root/Desktop/notes",
                "workflow": "direct",
            },
        },
        "_fields": {
            "projects.<name>.path": (
                "Absolute path to the project's main checkout: the directory "
                "that contains the .git directory. Linked worktrees of the "
                "project resolve to this entry automatically."
            ),
            "projects.<name>.workflow": (
                "'worktree-pr': agents create or reuse a git worktree on a "
                "branch and open a PR; they never edit or commit in the main "
                "checkout. 'tool commit' rejects commits made in the main "
                "checkout of such a project. 'direct': agents apply changes "
                "directly in the main checkout."
            ),
        },
    },
}


@click.group()
def setup_command() -> None:
    """Scaffold and maintain configuration consumed by tool subcommands."""


@setup_command.command("workspace")
@click.option(
    "--path",
    "config_path",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Where to write the file. Defaults to the canonical location "
        f"({workspace.DEFAULT_CONFIG_PATH}), overridable via the "
        f"{workspace.CONFIG_ENV_VAR} environment variable."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing configuration file.",
)
def workspace_setup(config_path: Optional[str], force: bool) -> None:
    """
    Scaffold the workspace preferences file (workspace.json).

    The file declares per-project preferences under the "projects" key:
    currently the agent workflow ("worktree-pr" or "direct"), with room for
    more project preferences over time. It is consumed by agent instructions
    (see the codex/agents AGENTS.md) and by 'tool commit' policy checks.

    \b
    Behavior:
      Creates the file (and any missing parent directories) with an empty
      "projects" map plus a self-documenting "_example" block that readers
      ignore. Never overwrites an existing file unless --force is given.

    \b
    Examples:
        tool setup workspace
        tool setup workspace --force
    """
    target = (
        Path(config_path).expanduser()
        if config_path
        else workspace.default_config_path()
    )
    existed = target.exists()
    if existed and not force:
        click.echo(
            f"❌ Configuration already exists: {target}\n"
            f"   Edit it directly, or re-run with --force to replace it with "
            f"the scaffold (existing project preferences would be lost).",
            err=True,
        )
        sys.exit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(WORKSPACE_SCAFFOLD, indent=2) + "\n"
    target.write_text(rendered, encoding="utf-8")

    # Fail loudly if the file we just wrote cannot be consumed by readers.
    try:
        workspace.parse_projects(workspace.load_config(target))
    except workspace.WorkspaceConfigError as exc:  # pragma: no cover
        click.echo(f"❌ Wrote an unusable configuration: {exc}", err=True)
        sys.exit(1)

    action = "Replaced" if existed else "Created"
    click.echo(f"✅ {action} workspace preferences: {target}")
    click.echo()
    click.echo("Next steps:")
    click.echo(
        '  1. Add entries under "projects" (see the "_example" block for the '
        "shape; readers ignore all _-prefixed keys)."
    )
    click.echo(
        '  2. Set "workflow" to "worktree-pr" for projects where agents must '
        'work in git worktrees and open PRs, or "direct" for projects where '
        "they apply changes in the main checkout."
    )
    click.echo(
        "  3. Commit the file in the directives repository so the preferences "
        "stay versioned."
    )
