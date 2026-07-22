"""
Clean session transcript rollout files into dialogue entries.

Strips timestamps, token counts, developer/system instructions, rate limits,
tool calls, tool outputs, and internal metadata, leaving only clean dialogue turns (User, Assistant).
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

DEFAULT_TMP_DIR = Path("/root/Desktop/tmp")


def clean_text(text: str) -> str:
    if not text:
        return ""
    # Strip known system/context tags
    pattern = (
        r"<(environment_context|permissions instructions|multi_agent_mode|"
        r"turn_aborted|collaboration_mode)>.*?</\1>"
    )
    text = re.sub(pattern, "", text, flags=re.DOTALL).strip()
    return text


def parse_rollout(
    file_path: Path, include_tools: bool = False
) -> List[Dict[str, Any]]:
    raw_entries: List[Dict[str, Any]] = []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue

            if not isinstance(data, dict):
                continue

            event_type = data.get("type")
            payload = data.get("payload", {})
            if not isinstance(payload, dict):
                continue

            if event_type == "response_item":
                item_type = payload.get("type")
                role = payload.get("role")

                if item_type == "message":
                    if role == "developer":
                        continue
                    content = payload.get("content", [])
                    raw_text = ""
                    for c in content:
                        if isinstance(c, dict):
                            raw_text += (
                                c.get("text", "")
                                or c.get("input_text", "")
                                or c.get("output_text", "")
                            )
                        elif isinstance(c, str):
                            raw_text += c
                    text = clean_text(raw_text)
                    if text:
                        raw_entries.append({
                            "role": role.capitalize() if role else "Message",
                            "content": text,
                        })

                elif include_tools and item_type in (
                    "function_call",
                    "custom_tool_call",
                    "tool_search_call",
                    "web_search_call",
                    "image_generation_call",
                ):
                    name = (
                        payload.get("name")
                        or (
                            payload.get("input", {}).get("name")
                            if isinstance(payload.get("input"), dict)
                            else None
                        )
                        or "tool"
                    )
                    args = payload.get("arguments") or payload.get("input")
                    raw_entries.append({
                        "role": "Tool Call",
                        "name": name,
                        "content": args if isinstance(args, (dict, list, str)) else str(args),
                    })

                elif include_tools and item_type in (
                    "function_call_output",
                    "custom_tool_call_output",
                    "tool_search_output",
                ):
                    out = payload.get("output")
                    out_text = ""
                    if isinstance(out, list):
                        for item in out:
                            if isinstance(item, dict):
                                out_text += item.get("text", "") or item.get("input_text", "")
                            else:
                                out_text += str(item)
                    elif isinstance(out, dict):
                        out_text = json.dumps(out, indent=2)
                    else:
                        out_text = str(out) if out is not None else ""
                    out_text = out_text.strip()
                    if out_text:
                        raw_entries.append({
                            "role": "Tool Output",
                            "content": out_text,
                        })

            elif event_type == "event_msg":
                msg_type = payload.get("type")
                if msg_type == "user_message":
                    raw_text = payload.get("message", "")
                    text = clean_text(raw_text)
                    if text:
                        raw_entries.append({
                            "role": "User",
                            "content": text,
                        })
                elif msg_type == "agent_message":
                    raw_text = payload.get("message", "")
                    text = clean_text(raw_text)
                    if text:
                        raw_entries.append({
                            "role": "Assistant",
                            "content": text,
                        })

    # Deduplicate consecutive identical entries
    cleaned: List[Dict[str, Any]] = []
    for entry in raw_entries:
        if cleaned:
            prev = cleaned[-1]
            if (
                prev.get("role") == entry.get("role")
                and prev.get("content") == entry.get("content")
                and prev.get("name") == entry.get("name")
            ):
                continue
        cleaned.append(entry)

    return cleaned


def format_as_text(entries: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for entry in entries:
        role = entry["role"]
        content = entry["content"]
        if isinstance(content, (dict, list)):
            content_str = json.dumps(content, indent=2)
        else:
            content_str = str(content)

        if role == "Tool Call":
            name = entry.get("name", "tool")
            blocks.append(f"[Tool Call: {name}]\n{content_str}")
        elif role == "Tool Output":
            blocks.append(f"[Tool Output]\n{content_str}")
        else:
            blocks.append(f"[{role}]\n{content_str}")
    return "\n\n".join(blocks)


@click.command(name="clean")
@click.argument("session_file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file or directory path. Defaults to /root/Desktop/tmp/<clean_filename>.",
)
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "jsonl"], case_sensitive=False),
    default="text",
    help="Output format: text dialogue (default), json array, or jsonl.",
)
@click.option(
    "--include-tools",
    is_flag=True,
    help="Include tool calls and tool outputs in the clean transcript.",
)
@click.option(
    "--stdout",
    is_flag=True,
    help="Print cleaned transcript directly to stdout instead of saving to file.",
)
def clean_command(
    session_file_path: Path,
    output: Optional[Path],
    output_format: str,
    include_tools: bool,
    stdout: bool,
) -> None:
    """
    Clean session transcript rollout file by removing non-viable fields.

    Strips timestamps, token usage, rate limits, developer/system instructions,
    tool calls, tool outputs, and metadata, returning clean dialogue entries.

    By default, writes the cleaned file to /root/Desktop/tmp/<clean_filename>.

    Usage:
        tool clean /path/to/rollout-session.jsonl
        tool clean /path/to/rollout-session.jsonl --stdout
        tool clean /path/to/rollout-session.jsonl -f json
        tool clean /path/to/rollout-session.jsonl -o /custom/path.txt
    """
    try:
        entries = parse_rollout(session_file_path, include_tools=include_tools)
    except Exception as exc:
        click.echo(f"Error parsing session file: {exc}", err=True)
        sys.exit(1)

    if output_format == "json":
        result = json.dumps(entries, indent=2)
        ext = ".json"
    elif output_format == "jsonl":
        result = "\n".join(json.dumps(entry) for entry in entries)
        ext = ".jsonl"
    else:
        result = format_as_text(entries)
        ext = ".txt"

    if stdout:
        click.echo(result)
        return

    # Determine output file path
    if output is None:
        out_dir = DEFAULT_TMP_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = session_file_path.stem
        if stem.startswith("rollout-"):
            clean_stem = f"clean-{stem[8:]}"
        else:
            clean_stem = f"clean-{stem}"
        out_file = out_dir / f"{clean_stem}{ext}"
    elif output.is_dir():
        output.mkdir(parents=True, exist_ok=True)
        stem = session_file_path.stem
        if stem.startswith("rollout-"):
            clean_stem = f"clean-{stem[8:]}"
        else:
            clean_stem = f"clean-{stem}"
        out_file = output / f"{clean_stem}{ext}"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        out_file = output

    try:
        out_file.write_text(result + "\n", encoding="utf-8")
        click.echo(str(out_file))
    except Exception as exc:
        click.echo(f"Error writing to output file: {exc}", err=True)
        sys.exit(1)
