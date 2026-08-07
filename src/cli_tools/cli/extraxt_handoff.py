"""Extract handoff/summary from Codex generation input traces."""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

DEFAULT_TMP_DIR = Path("/root/Desktop/tmp")


def find_latest_input_file(tmp_dir: Path) -> Optional[Path]:
    """Find the most recently modified generation input json file in tmp."""
    if not tmp_dir.is_dir():
        return None
    
    candidates = []
    for path in tmp_dir.glob("*.json"):
        if path.is_file() and ("generation" in path.name or "export" in path.name):
            candidates.append(path)
            
    if not candidates:
        return None
        
    # Return the one with the latest mtime
    return max(candidates, key=lambda p: p.stat().st_mtime)


def clean_handoff_text(text: str) -> str:
    """Clean raw text by stripping leading/trailing client/developer tags."""
    if not text:
        return ""
    
    # Strip wrapper tags
    text = re.sub(r"^<user_developer>\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*</user_developer>$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^<user_client>\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*</user_client>$", "", text, flags=re.IGNORECASE)
    return text.strip()


def find_handoff_in_messages(messages: List[Dict[str, Any]]) -> Optional[str]:
    """Scan messages list (each with role/content) for handoff/checkpoint content."""
    candidates = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content:
            continue
            
        # Look for handoff or checkpoint markers
        if any(marker in content for marker in ("Checkpoint Summary", "The following handoff", "# Checkpoint:")):
            candidates.append(content)
            
    if candidates:
        # Return the most recent candidate
        return candidates[-1]
    return None


def find_handoff_anywhere(data: Any) -> Optional[str]:
    """Recursively search the JSON structure for handoff strings."""
    if isinstance(data, str):
        if any(marker in data for marker in ("Checkpoint Summary", "The following handoff", "# Checkpoint:")):
            return data
            
    elif isinstance(data, dict):
        # Prioritize content.messages if available
        content_field = data.get("content")
        if isinstance(content_field, dict):
            msgs = content_field.get("messages")
            if isinstance(msgs, list):
                res = find_handoff_in_messages(msgs)
                if res:
                    return res
                    
        # Fallback to recursive dict search
        for val in data.values():
            res = find_handoff_anywhere(val)
            if res:
                return res
                
    elif isinstance(data, list):
        for item in data:
            res = find_handoff_anywhere(item)
            if res:
                return res
                
    return None


@click.command(name="extraxt-handoff")
@click.argument("input_path", type=click.Path(exists=True, path_type=Path), required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output .md file path. Defaults to /root/Desktop/tmp/handoff-<trace_id>.md and symlinked to handoff.md.",
)
def extraxt_handoff_command(
    input_path: Optional[Path],
    output: Optional[Path],
) -> None:
    """
    Extract handoff/summary markdown from a Codex generation input JSON.

    If INPUT_PATH is not provided, the tool automatically resolves the latest
    generation/export input JSON file located in /root/Desktop/tmp/.
    """
    if input_path is None:
        resolved_path = find_latest_input_file(DEFAULT_TMP_DIR)
        if not resolved_path:
            raise click.ClickException(f"No generation input JSON file found in {DEFAULT_TMP_DIR}. Please specify an explicit path.")
        input_path = resolved_path
        click.echo(f"🔍 Auto-resolved latest input file: {input_path}")
        
    try:
        with open(input_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception as exc:
        raise click.ClickException(f"Failed to read/parse input file {input_path}: {exc}")
        
    # Extract trace/generation metadata if present
    trace_id = None
    if isinstance(data, dict):
        row = data.get("row")
        if isinstance(row, dict):
            trace_id = row.get("trace_id")
        if not trace_id:
            trace_id = data.get("trace_id")
            
    # Locate handoff text
    raw_text = find_handoff_anywhere(data)
    if not raw_text:
        raise click.ClickException("Could not find any handoff or Checkpoint Summary markers within the input file.")
        
    cleaned_text = clean_handoff_text(raw_text)
    
    # Resolve output path
    if output is None:
        # Construct standard output filename
        stem = f"handoff-{trace_id}" if trace_id else "handoff-extracted"
        output_file = DEFAULT_TMP_DIR / f"{stem}.md"
    else:
        output_file = output
        
    try:
        # Create directories if needed
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(cleaned_text + "\n", encoding="utf-8")
        click.echo(f"✅ Successfully extracted handoff to: {output_file}")
        
        # Also symlink or write to a generic handoff.md for easy static access
        static_file = DEFAULT_TMP_DIR / "handoff.md"
        if output_file != static_file:
            try:
                # Remove if exists
                if static_file.exists() or static_file.is_symlink():
                    static_file.unlink()
                # Create a symlink or write a copy (write copy is safer across sandbox filesystems)
                static_file.write_text(cleaned_text + "\n", encoding="utf-8")
                click.echo(f"🔗 Also copied to static path: {static_file}")
            except Exception:
                pass
                
    except Exception as exc:
        raise click.ClickException(f"Failed to write extracted markdown: {exc}")


if __name__ == "__main__":
    extraxt_handoff_command()
