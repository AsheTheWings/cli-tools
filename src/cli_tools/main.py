"""
CLI Tools - Command-line utilities for AI-powered workflows.

Usage:
    tool commit [PATH]         # Generate AI-powered git commit message
    tool commit . -y            # Auto-commit and push without prompts
    tool command "description"   # Generate shell command from natural language
    tool beep 1 30              # Beep every 1 minute for 30 minutes
"""

import click
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from the current directory or parents (per-project overrides),
# then fall back to the cli-tools package .env so the global `tool` command always has its
# config (e.g. TERA_API_KEY) regardless of the directory it is invoked from.
load_dotenv()
_package_env = Path(__file__).resolve().parents[2] / ".env"
if _package_env.exists():
    load_dotenv(_package_env)


@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option()
def main(ctx) -> None:
    """CLI Tools - AI-powered command-line utilities."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# =========================================================================
# CLI Utility Subcommands
# =========================================================================

from cli_tools.cli.commit import commit_command
from cli_tools.cli.generate_command import command_generator
from cli_tools.cli.beep import beep_command
from cli_tools.cli.aggr import aggr_command
from cli_tools.cli.design_requirements_docs import (
    design_doc_group,
    requirements_doc_group,
)
from cli_tools.cli.review_reports import review_report_group

main.add_command(commit_command, "commit")
main.add_command(command_generator, "command")
main.add_command(beep_command, "beep")
main.add_command(aggr_command, "aggr")
main.add_command(design_doc_group, "design-doc")
main.add_command(requirements_doc_group, "requirements-doc")
main.add_command(review_report_group, "review-report")


if __name__ == "__main__":
    main()
