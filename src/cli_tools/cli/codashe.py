"""Terminal and agent-facing commands for codashe-omni."""

from __future__ import annotations

import functools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import click
import httpx

from cli_tools.codashe_client import CodasheError, CodasheGatewayClient

DEFAULT_GATEWAY_URL = "ws://127.0.0.1:4310/ws"
F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class CodasheContext:
    client: CodasheGatewayClient
    pretty: bool


def _guard(function: F) -> F:
    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except click.ClickException:
            raise
        except (CodasheError, httpx.HTTPError, OSError, ValueError, json.JSONDecodeError) as error:
            raise click.ClickException(str(error)) from error

    return wrapped  # type: ignore[return-value]


@click.group("codashe")
@click.option(
    "--gateway-url",
    envvar="CODASHE_GATEWAY_URL",
    default=DEFAULT_GATEWAY_URL,
    show_default=True,
    help="Local codashe gateway WebSocket URL.",
)
@click.option(
    "--request-timeout",
    type=click.FloatRange(min=0.1),
    default=15.0,
    show_default=True,
    help="HTTP and WebSocket request timeout in seconds.",
)
@click.option("--pretty", is_flag=True, help="Indent JSON output for human reading.")
@click.pass_context
def codashe_command(
    ctx: click.Context,
    gateway_url: str,
    request_timeout: float,
    pretty: bool,
) -> None:
    """Delegate, observe, and manage codashe-omni jobs."""
    ctx.call_on_close(lambda: ctx.obj.client.close() if ctx.obj else None)
    ctx.obj = CodasheContext(
        client=CodasheGatewayClient(gateway_url, timeout=request_timeout),
        pretty=pretty,
    )


@codashe_command.command("health")
@click.pass_obj
@_guard
def health_command(context: CodasheContext) -> None:
    """Show gateway, worker-connection, release, and protocol health."""
    _emit(context, {"ok": True, "health": context.client.health()})


@codashe_command.command("schema")
@click.option("--output", type=click.Path(path_type=Path), help="Write the schema to this file.")
@click.option("--print", "print_full", is_flag=True, help="Dump the full schema to stdout.")
@click.pass_obj
@_guard
def schema_command(
    context: CodasheContext,
    output: Path | None,
    print_full: bool,
) -> None:
    """Read the exact public schema used by the running gateway."""
    schema = context.client.schema()
    if print_full:
        _emit(context, {"ok": True, "schema": schema})
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        _emit(
            context,
            {
                "ok": True,
                "path": str(output.resolve()),
                "protocolVersion": schema.get("version"),
            },
        )
    elif not print_full:
        definitions = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
        _emit(
            context,
            {
                "ok": True,
                "protocolVersion": schema.get("version"),
                "topLevelKeys": sorted(schema.keys()),
                "definitions": len(definitions),
                "definitionNames": sorted(definitions.keys())[:15],
                "hint": "use --output FILE to write the full schema to disk, or --print to dump it to stdout",
            },
        )


@codashe_command.command("submit")
@click.argument("objective", required=False)
@click.option(
    "--file",
    "submission_path",
    type=click.Path(path_type=Path),
    help="Canonical JobSubmission JSON file; use '-' for stdin.",
)
@click.option("--url", "urls", multiple=True, help="Direct URL target; repeat as needed.")
@click.option(
    "--forward",
    "forwards",
    multiple=True,
    metavar="NAME=LOCAL_URL",
    help="Caller-local service target; repeat as needed.",
)
@click.option("--tag", "tags", multiple=True, help="Job tag; repeat as needed.")
@click.option(
    "--guided-test",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="GuidedPlan JSON to add to an objective submission.",
)
@click.option(
    "--preferences",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JobPreferences JSON to add to an objective submission.",
)
@click.option("--prior-job", "prior_jobs", multiple=True, help="Prior job reference.")
@click.option("--idempotency-key", help="Stable key for safe submission retry.")
@click.pass_obj
@_guard
def submit_command(
    context: CodasheContext,
    objective: str | None,
    submission_path: Path | None,
    urls: tuple[str, ...],
    forwards: tuple[str, ...],
    tags: tuple[str, ...],
    guided_test: Path | None,
    preferences: Path | None,
    prior_jobs: tuple[str, ...],
    idempotency_key: str | None,
) -> None:
    """Submit a durable job and return immediately after acceptance."""
    modifiers = urls or forwards or tags or guided_test or preferences or prior_jobs
    if submission_path is not None:
        if objective is not None or modifiers:
            raise click.UsageError("--file cannot be combined with objective submission options")
        submission = _read_json_object(submission_path)
    else:
        if not objective:
            raise click.UsageError("provide an OBJECTIVE or --file")
        targets: list[dict[str, Any]] = [
            {"kind": "url", "url": url} for url in urls
        ]
        targets.extend(_forward_target(value) for value in forwards)
        submission = {
            "schemaVersion": 1,
            "objective": objective,
            **({"targets": targets} if targets else {}),
            **({"tags": list(tags)} if tags else {}),
            **({"priorJobs": list(prior_jobs)} if prior_jobs else {}),
            **(
                {"guidedTest": _read_json_object(guided_test)}
                if guided_test
                else {}
            ),
            **(
                {"preferences": _read_json_object(preferences)}
                if preferences
                else {}
            ),
        }
    accepted = context.client.submit(
        submission, idempotency_key=idempotency_key
    )
    _emit(
        context,
        {
            "ok": True,
            "operation": "submit",
            "jobId": accepted.get("jobId"),
            "accepted": accepted,
        },
    )


@codashe_command.command("status")
@click.argument("job_id")
@click.pass_obj
@_guard
def status_command(context: CodasheContext, job_id: str) -> None:
    """Get a current snapshot without replaying job history."""
    _emit(
        context,
        {"ok": True, "jobId": job_id, "snapshot": context.client.snapshot(job_id)},
    )


@codashe_command.command("watch")
@click.argument("job_id")
@click.option("--after", "after_sequence", type=click.IntRange(min=0), default=0)
@click.option("--once", is_flag=True, help="Return after the retained suffix becomes idle.")
@click.option("--idle-timeout", type=click.FloatRange(min=0.05), default=1.0)
@click.option("--timeout", type=click.FloatRange(min=0.1), help="Bound total watch time.")
@click.option(
    "--raw",
    is_flag=True,
    help="Emit complete untruncated event payloads instead of compact summaries.",
)
@click.pass_obj
@_guard
def watch_command(
    context: CodasheContext,
    job_id: str,
    after_sequence: int,
    once: bool,
    idle_timeout: float,
    timeout: float | None,
    raw: bool,
) -> None:
    """Stream ordered JSONL events with automatic cursor-based reconnection."""
    last_sequence: int | None = None
    for event in context.client.events(
        job_id,
        after_sequence=after_sequence,
        follow=not once,
        idle_timeout=idle_timeout,
        total_timeout=timeout,
    ):
        sequence = event.get("sequence")
        if isinstance(sequence, int):
            last_sequence = sequence
        if raw:
            _emit(context, {"ok": True, "event": event}, force_compact=True)
            continue
        summary = _summarize_event(event)
        if summary is not None:
            _emit(context, summary, force_compact=True)
    if last_sequence is not None:
        _emit(
            context,
            {
                "ok": True,
                "cursor": last_sequence,
                "hint": "resume with --after <cursor>",
            },
            force_compact=True,
        )


@codashe_command.command("wait")
@click.argument("job_id")
@click.option("--timeout", type=click.FloatRange(min=0.1), help="Bound total wait time.")
@click.option("--poll-interval", type=click.FloatRange(min=0.05), default=1.0)
@click.option(
    "--require-pass",
    is_flag=True,
    help="Exit 3 when a completed guided test has passed=false.",
)
@click.pass_context
@_guard
def wait_command(
    click_context: click.Context,
    job_id: str,
    timeout: float | None,
    poll_interval: float,
    require_pass: bool,
) -> None:
    """Wait for terminal state and emit the final snapshot."""
    context: CodasheContext = click_context.obj
    snapshot = context.client.wait(
        job_id, timeout=timeout, poll_interval=poll_interval
    )
    _emit(context, {"ok": True, "jobId": job_id, "snapshot": snapshot})
    result = snapshot.get("result")
    if require_pass and isinstance(result, dict) and result.get("passed") is False:
        click_context.exit(3)


@codashe_command.command("steer")
@click.argument("job_id")
@click.argument("message")
@click.option("--idempotency-key")
@click.pass_obj
@_guard
def steer_command(
    context: CodasheContext,
    job_id: str,
    message: str,
    idempotency_key: str | None,
) -> None:
    """Append guidance to the active Codex attempt."""
    _emit(
        context,
        {
            "ok": True,
            "jobId": job_id,
            "delivery": context.client.steer(
                job_id, message, idempotency_key=idempotency_key
            ),
        },
    )


@codashe_command.command("respond")
@click.argument("job_id")
@click.argument("request_token")
@click.argument("response")
@click.option("--idempotency-key")
@click.pass_obj
@_guard
def respond_command(
    context: CodasheContext,
    job_id: str,
    request_token: str,
    response: str,
    idempotency_key: str | None,
) -> None:
    """Answer a pending human-input request with JSON, @FILE, or stdin '-'."""
    value = _parse_json_argument(response)
    _emit(
        context,
        {
            "ok": True,
            "jobId": job_id,
            "response": context.client.respond(
                job_id,
                request_token,
                value,
                idempotency_key=idempotency_key,
            ),
        },
    )


@codashe_command.command("control")
@click.argument("job_id")
@click.argument("action", type=click.Choice(["pause", "resume", "cancel", "retry"]))
@click.option("--idempotency-key")
@click.pass_obj
@_guard
def control_command(
    context: CodasheContext,
    job_id: str,
    action: str,
    idempotency_key: str | None,
) -> None:
    """Pause, resume, cancel, or retry a job."""
    _emit(
        context,
        {
            "ok": True,
            "jobId": job_id,
            "control": context.client.control(
                job_id, action, idempotency_key=idempotency_key
            ),
        },
    )


@codashe_command.command("retention")
@click.argument("job_id")
@click.argument("action", type=click.Choice(["pin", "unpin", "delete"]))
@click.option("--idempotency-key")
@click.option("--yes", is_flag=True, help="Confirm exact terminal-job deletion.")
@click.pass_obj
@_guard
def retention_command(
    context: CodasheContext,
    job_id: str,
    action: str,
    idempotency_key: str | None,
    yes: bool,
) -> None:
    """Pin, unpin, or delete one exact job."""
    if action == "delete" and not yes:
        click.confirm(f"Delete terminal job {job_id}?", abort=True)
    _emit(
        context,
        {
            "ok": True,
            "jobId": job_id,
            "retention": context.client.retention(
                job_id, action, idempotency_key=idempotency_key
            ),
        },
    )


@codashe_command.command("result")
@click.argument("job_id")
@click.pass_obj
@_guard
def result_command(context: CodasheContext, job_id: str) -> None:
    """Show execution state and the independent guided-test verdict."""
    snapshot = context.client.snapshot(job_id)
    _emit(
        context,
        {
            "ok": True,
            "jobId": job_id,
            "executionStatus": (
                snapshot.get("result", {}).get("executionStatus")
                if isinstance(snapshot.get("result"), dict)
                else snapshot.get("job", {}).get("status")
            ),
            "passed": (
                snapshot.get("result", {}).get("passed")
                if isinstance(snapshot.get("result"), dict)
                else None
            ),
            "result": snapshot.get("result"),
        },
    )


@codashe_command.command("artifacts")
@click.argument("job_id")
@click.pass_obj
@_guard
def artifacts_command(context: CodasheContext, job_id: str) -> None:
    """List the synchronized artifact manifest for a job."""
    manifest = context.client.manifest(job_id)
    _emit(context, {"ok": True, "jobId": job_id, "manifest": manifest})


@codashe_command.command("download")
@click.argument("job_id")
@click.argument("relative_path")
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.pass_obj
@_guard
def download_command(
    context: CodasheContext,
    job_id: str,
    relative_path: str,
    output: Path,
) -> None:
    """Download one synchronized job file to an exact local path."""
    body = context.client.job_file(job_id, relative_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    _emit(
        context,
        {
            "ok": True,
            "jobId": job_id,
            "relativePath": relative_path,
            "path": str(output.resolve()),
            "bytes": len(body),
        },
    )


@codashe_command.command("history")
@click.option("--text")
@click.option("--tag")
@click.option("--target")
@click.option("--application")
@click.option("--artifact-type")
@click.pass_obj
@_guard
def history_command(
    context: CodasheContext,
    text: str | None,
    tag: str | None,
    target: str | None,
    application: str | None,
    artifact_type: str | None,
) -> None:
    """Search filesystem-backed job history."""
    query = {
        key: value
        for key, value in {
            "text": text,
            "tag": tag,
            "target": target,
            "application": application,
            "artifactType": artifact_type,
        }.items()
        if value is not None
    }
    _emit(context, {"ok": True, "query": query, "result": context.client.query(query)})


@codashe_command.command("clone")
@click.argument("job_id")
@click.option(
    "--overrides",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Partial JobSubmission JSON merged into reusable source fields.",
)
@click.option("--idempotency-key")
@click.pass_obj
@_guard
def clone_command(
    context: CodasheContext,
    job_id: str,
    overrides: Path | None,
    idempotency_key: str | None,
) -> None:
    """Clone reusable inputs from a prior job with provenance."""
    source = _reusable_submission(context.client.snapshot(job_id))
    changes = _read_json_object(overrides) if overrides else {}
    submission = {**source, **changes}
    if isinstance(source.get("context"), dict) or isinstance(changes.get("context"), dict):
        submission["context"] = {
            **(source.get("context") or {}),
            **(changes.get("context") or {}),
        }
    if isinstance(source.get("preferences"), dict) or isinstance(changes.get("preferences"), dict):
        submission["preferences"] = {
            **(source.get("preferences") or {}),
            **(changes.get("preferences") or {}),
        }
    submission["priorJobs"] = list(
        dict.fromkeys([job_id, *(changes.get("priorJobs") or [])])
    )
    accepted = context.client.submit(submission, idempotency_key=idempotency_key)
    _emit(
        context,
        {
            "ok": True,
            "operation": "clone",
            "sourceJobId": job_id,
            "jobId": accepted.get("jobId"),
            "accepted": accepted,
        },
    )


_LONG_TEXT_LIMIT = 1500
_INLINE_LIMIT = 300


def _truncate(text: str, limit: int = _LONG_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\u2026[+{len(text) - limit} chars]"


def _json_preview(value: Any, limit: int = _INLINE_LIMIT) -> str:
    return _truncate(json.dumps(value, separators=(",", ":")), limit)


def _summarize_tool_result(result: Any) -> Any:
    if result is None:
        return None
    if not isinstance(result, dict):
        return _truncate(str(result))
    error = result.get("error")
    summary: dict[str, Any] = {"error": _truncate(str(error))} if error else {}
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            data = block.get("data")
            if isinstance(data, str):
                media = block.get("mimeType") or block.get("type") or "blob"
                parts.append(f"[{media} payload omitted: {len(data)} base64 chars]")
            elif isinstance(block.get("text"), str):
                parts.append(block["text"])
    if parts:
        summary["text"] = _truncate("\n".join(parts))
    return summary if summary else None


def _summarize_item(
    base: dict[str, Any],
    method: str,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    item_type = item.get("type")
    started = method == "item/started"
    if item_type == "reasoning":
        # Reasoning envelopes almost always carry empty content and pair
        # started/completed duplicates; only surface a populated summary.
        summary_items = item.get("summary") or []
        text = " ".join(
            entry.get("text", "")
            for entry in summary_items
            if isinstance(entry, dict)
        ).strip()
        if not text:
            return None
        return {**base, "kind": "reasoning", "summary": _truncate(text)}
    if item_type == "commandExecution":
        command = item.get("command")
        rendered: dict[str, Any] = {
            **base,
            "kind": "command",
            "command": _truncate(str(command), _INLINE_LIMIT) if command else None,
        }
        if started:
            rendered["phase"] = "running"
            return rendered
        rendered["exitCode"] = item.get("exitCode")
        if item.get("durationMs") is not None:
            rendered["durationMs"] = item.get("durationMs")
        output = item.get("aggregatedOutput")
        if isinstance(output, str) and output:
            rendered["output"] = _truncate(output)
        return rendered
    if item_type == "mcpToolCall":
        name = f"{item.get('server')}/{item.get('tool')}"
        if started:
            return {
                **base,
                "kind": "tool",
                "name": name,
                "args": _json_preview(item.get("arguments")),
            }
        rendered = {
            **base,
            "kind": "tool",
            "name": name,
            "ok": item.get("error") is None,
        }
        if item.get("durationMs") is not None:
            rendered["durationMs"] = item.get("durationMs")
        result = _summarize_tool_result(item.get("result"))
        if result is not None:
            rendered["result"] = result
        return rendered
    if item_type == "agentMessage":
        if started:
            return None
        return {
            **base,
            "kind": "message",
            "text": _truncate(str(item.get("text") or ""), 4000),
        }
    if started:
        return {**base, "kind": item_type or "item", "phase": "started"}
    return {**base, "kind": item_type or "item", "item": _json_preview(item, 800)}


def _summarize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Render one job event as a compact, context-friendly JSONL summary.

    Raw Codex progress items carry full page snapshots, command transcripts,
    and base64 screenshots; this renderer keeps the monitoring signal
    (sequence, tool, command, outcome) and replaces bulk payloads with
    truncated previews. Use ``watch --raw`` for the complete stream.
    """
    event_type = event.get("type")
    base: dict[str, Any] = {
        "ok": True,
        "sequence": event.get("sequence"),
        "timestamp": event.get("timestamp"),
    }
    if event_type == "agent.message":
        # Per-token streaming deltas are noise for polling monitors; the
        # completed agentMessage item carries the final text.
        return None
    if event_type != "progress.updated":
        return {
            **base,
            "type": event_type,
            "payload": _json_preview(event.get("payload"), 1000),
        }
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {**base, "type": event_type}
    if payload.get("source") == "agent":
        body = {
            key: value for key, value in payload.items() if key != "source"
        }
        return {**base, "type": event_type, "agent": _json_preview(body, 1000)}
    method = payload.get("method")
    params = payload.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    if isinstance(item, dict) and method in ("item/started", "item/completed"):
        return _summarize_item(
            {**base, "type": event_type},
            str(method),
            item,
        )
    return {
        **base,
        "type": event_type,
        "method": method,
        "params": _json_preview(params, 600),
    }


def _emit(
    context: CodasheContext,
    payload: Any,
    *,
    force_compact: bool = False,
) -> None:
    pretty = context.pretty and not force_compact
    click.echo(
        json.dumps(
            payload,
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
        )
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if str(path) == "-":
        raw = sys.stdin.read()
    else:
        raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_json_argument(value: str) -> Any:
    if value == "-":
        return json.loads(sys.stdin.read())
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def _forward_target(value: str) -> dict[str, Any]:
    name, separator, local_url = value.partition("=")
    if not separator or not name or not local_url:
        raise ValueError("--forward must use NAME=LOCAL_URL")
    return {"kind": "forwarded_service", "name": name, "localUrl": local_url}


def _reusable_submission(snapshot: dict[str, Any]) -> dict[str, Any]:
    job = snapshot.get("job")
    if not isinstance(job, dict):
        raise ValueError("snapshot has no job object")
    immutable = {"id", "createdAt", "updatedAt", "status", "trace"}
    return {key: value for key, value in job.items() if key not in immutable}
