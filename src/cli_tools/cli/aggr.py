"""
Aggregate file contents into a single temporary file.

Reads absolute file paths from stdin (one per line), reads each file's
full content, and writes everything into a temporary file with separators
showing the original file path.
"""

import sys
import tempfile
from pathlib import Path

import click


@click.command()
def aggr_command() -> None:
    """
    Aggregate file contents from stdin into a single tmp file.

    Reads absolute file paths from stdin (one per line), reads each file's
    full content, and writes everything into a temporary file separated by
    '--- <filepath>' markers.

    The final temporary file path is printed to stdout.

    Usage:
        cat files.txt | tool aggr
        echo -e "/a/b.py\\n/c/d.py" | tool aggr
    """
    # Read file paths from stdin
    file_paths = [line.strip() for line in sys.stdin if line.strip()]

    if not file_paths:
        click.echo("No file paths provided on stdin.", err=True)
        sys.exit(1)

    # Validate paths and read contents
    parts = []
    for fp in file_paths:
        path = Path(fp)
        if not path.is_absolute():
            click.echo(f"Path is not absolute: {fp}", err=True)
            sys.exit(1)
        if not path.exists():
            click.echo(f"File not found: {fp}", err=True)
            sys.exit(1)
        if not path.is_file():
            click.echo(f"Not a file: {fp}", err=True)
            sys.exit(1)

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            click.echo(f"Cannot read {fp}: {exc}", err=True)
            sys.exit(1)

        parts.append(f"--- {fp}\n{content}")

    # Write to a temporary file
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="tool_aggr_",
        suffix=".txt",
        delete=False,
    ) as tmp:
        tmp.write("\n".join(parts))
        tmp_path = tmp.name

    click.echo(tmp_path)
