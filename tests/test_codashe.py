import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from cli_tools.codashe_client import CodasheGatewayClient
from cli_tools.main import main


class FakeSocket:
    def __init__(self, frames: list[dict], terminal_events: list[dict]) -> None:
        self.frames = frames
        self.terminal_events = terminal_events
        self.responses: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        request_id = frame["requestId"]
        if frame["type"] == "handshake":
            reply = {
                "type": "ack",
                "protocolVersion": "1.2.0",
                "requestId": request_id,
                "payload": {"compatible": True},
            }
        elif frame["type"] == "job.submit":
            reply = {
                "type": "ack",
                "protocolVersion": "1.2.0",
                "requestId": request_id,
                "payload": {"jobId": "job-1", "durable": True},
            }
        elif frame["type"] == "job.snapshot":
            reply = {
                "type": "snapshot",
                "protocolVersion": "1.2.0",
                "requestId": request_id,
                "snapshot": {
                    "job": {"id": "job-1", "status": "completed"},
                    "latestSequence": 2,
                    "leases": [],
                    "result": {"executionStatus": "completed", "passed": True},
                },
            }
        elif frame["type"] == "job.subscribe":
            reply = {
                "type": "ack",
                "protocolVersion": "1.2.0",
                "requestId": request_id,
                "payload": {"subscribed": True},
            }
            self.responses.extend(
                json.dumps(
                    {
                        "type": "event",
                        "protocolVersion": "1.2.0",
                        "event": event,
                    }
                )
                for event in self.terminal_events
            )
        else:
            raise AssertionError(f"unexpected frame {frame['type']}")
        self.responses.insert(0, json.dumps(reply))

    def recv(self, *, timeout=None, decode=True):
        del timeout, decode
        if not self.responses:
            raise TimeoutError
        return self.responses.pop(0)


class GatewayClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.frames: list[dict] = []
        self.events = [
            {
                "protocolVersion": "1.2.0",
                "jobId": "job-1",
                "sequence": 1,
                "timestamp": "2026-08-02T00:00:00.000Z",
                "type": "progress.updated",
                "payload": {"percent": 50},
            },
            {
                "protocolVersion": "1.2.0",
                "jobId": "job-1",
                "sequence": 2,
                "timestamp": "2026-08-02T00:00:01.000Z",
                "type": "job.result",
                "payload": {"executionStatus": "completed", "passed": True},
            },
        ]

        def http_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(
                    200,
                    json={
                        "status": "ready",
                        "protocolVersion": "1.2.0",
                        "releaseId": "rel-test",
                    },
                )
            if request.url.path == "/api/protocol/schema.json":
                return httpx.Response(200, json={"version": "1.2.0", "$defs": {}})
            if request.url.path.endswith("/manifest.json"):
                return httpx.Response(200, json={"entries": []})
            return httpx.Response(404)

        self.http = httpx.Client(transport=httpx.MockTransport(http_handler))
        self.client = CodasheGatewayClient(
            "ws://127.0.0.1:4310/ws",
            http_client=self.http,
            websocket_factory=lambda *_args, **_kwargs: FakeSocket(
                self.frames, self.events
            ),
        )

    def tearDown(self) -> None:
        self.http.close()

    def test_submit_discovers_version_and_sends_idempotent_protocol_frames(self) -> None:
        result = self.client.submit(
            {"schemaVersion": 1, "objective": "Exercise the UI"},
            idempotency_key="submission-key",
        )

        self.assertEqual(result, {"jobId": "job-1", "durable": True})
        self.assertEqual([frame["type"] for frame in self.frames], ["handshake", "job.submit"])
        self.assertEqual(self.frames[1]["protocolVersion"], "1.2.0")
        self.assertEqual(self.frames[1]["idempotencyKey"], "submission-key")

    def test_watch_replays_after_cursor_and_stops_on_terminal_event(self) -> None:
        events = list(self.client.events("job-1", after_sequence=0))

        self.assertEqual([event["sequence"] for event in events], [1, 2])
        subscription = next(frame for frame in self.frames if frame["type"] == "job.subscribe")
        self.assertEqual(subscription["afterSequence"], 0)

    def test_schema_and_manifest_use_gateway_http_boundary(self) -> None:
        self.assertEqual(self.client.schema()["version"], "1.2.0")
        self.assertEqual(self.client.manifest("job-1"), {"entries": []})
        with self.assertRaisesRegex(ValueError, "relative path"):
            self.client.job_file("job-1", "../secret")


class FakeCliClient:
    instances: list["FakeCliClient"] = []

    def __init__(self, gateway_url: str, *, timeout: float) -> None:
        self.gateway_url = gateway_url
        self.timeout = timeout
        self.submission = None
        self.__class__.instances.append(self)

    def close(self) -> None:
        return None

    def submit(self, submission, *, idempotency_key=None):
        self.submission = submission
        return {"jobId": "job-cli", "durable": True, "duplicate": False}

    def schema(self):
        return {
            "version": "1.2.0",
            "$defs": {"JobSubmission": {"type": "object"}},
            "properties": {"JobSubmission": {"$ref": "#/$defs/JobSubmission"}},
        }

    def wait(self, job_id, *, timeout=None, poll_interval=1.0):
        del timeout, poll_interval
        return {
            "job": {"id": job_id, "status": "completed"},
            "latestSequence": 4,
            "leases": [],
            "result": {"executionStatus": "completed", "passed": False},
        }

    def events(self, job_id, *, after_sequence=0, follow=True, idle_timeout=1.0, total_timeout=None):
        del follow, idle_timeout, total_timeout
        assert job_id == "job-cli"
        crafted = [
            {
                "jobId": job_id,
                "sequence": 1,
                "timestamp": "2026-08-02T00:00:00.000Z",
                "type": "progress.updated",
                "payload": {
                    "source": "codex",
                    "method": "item/started",
                    "params": {"item": {"type": "reasoning", "content": [], "summary": []}},
                },
            },
            {
                "jobId": job_id,
                "sequence": 2,
                "timestamp": "2026-08-02T00:00:01.000Z",
                "type": "progress.updated",
                "payload": {
                    "source": "codex",
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "mcpToolCall",
                            "server": "cua",
                            "tool": "get_desktop_state",
                            "durationMs": 12,
                            "error": None,
                            "result": {
                                "content": [
                                    {"type": "image", "mimeType": "image/png", "data": "A" * 50_000},
                                    {"type": "text", "text": "desktop ok " * 3000},
                                ]
                            },
                        }
                    },
                },
            },
            {
                "jobId": job_id,
                "sequence": 3,
                "timestamp": "2026-08-02T00:00:02.000Z",
                "type": "agent.message",
                "payload": {"source": "codex", "delta": "partial"},
            },
            {
                "jobId": job_id,
                "sequence": 4,
                "timestamp": "2026-08-02T00:00:03.000Z",
                "type": "progress.updated",
                "payload": {
                    "source": "codex",
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "command": "/usr/bin/zsh -lc 'echo hi'",
                            "exitCode": 0,
                            "durationMs": 3,
                            "aggregatedOutput": "hi\n",
                        }
                    },
                },
            },
        ]
        return iter(
            event for event in crafted if event["sequence"] > after_sequence
        )


class CodasheCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        FakeCliClient.instances.clear()

    def test_top_level_tool_exposes_codashe_group(self) -> None:
        result = self.runner.invoke(main, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("codashe", result.output)

    @patch("cli_tools.cli.codashe.CodasheGatewayClient", FakeCliClient)
    def test_submit_convenience_builds_a_canonical_job_and_prints_json(self) -> None:
        result = self.runner.invoke(
            main,
            [
                "codashe",
                "submit",
                "Exercise the browser flow",
                "--url",
                "https://example.test",
                "--forward",
                "app=http://127.0.0.1:3000",
                "--tag",
                "e2e",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        output = json.loads(result.output)
        self.assertEqual(output["jobId"], "job-cli")
        self.assertTrue(output["accepted"]["durable"])
        submission = FakeCliClient.instances[-1].submission
        self.assertEqual(submission["schemaVersion"], 1)
        self.assertEqual(submission["tags"], ["e2e"])
        self.assertEqual(
            [target["kind"] for target in submission["targets"]],
            ["url", "forwarded_service"],
        )

    @patch("cli_tools.cli.codashe.CodasheGatewayClient", FakeCliClient)
    def test_submit_accepts_full_json_from_stdin(self) -> None:
        submission = {"schemaVersion": 1, "objective": "Use the complete contract"}
        result = self.runner.invoke(
            main,
            ["codashe", "submit", "--file", "-"],
            input=json.dumps(submission),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(FakeCliClient.instances[-1].submission, submission)

    @patch("cli_tools.cli.codashe.CodasheGatewayClient", FakeCliClient)
    def test_wait_can_make_a_failed_guided_verdict_a_distinct_exit(self) -> None:
        result = self.runner.invoke(
            main,
            ["codashe", "wait", "job-cli", "--require-pass"],
        )

        self.assertEqual(result.exit_code, 3, result.output)
        self.assertFalse(json.loads(result.output)["snapshot"]["result"]["passed"])

    @patch("cli_tools.cli.codashe.CodasheGatewayClient", FakeCliClient)
    def test_watch_compacts_events_and_prints_a_resume_cursor(self) -> None:
        result = self.runner.invoke(main, ["codashe", "watch", "job-cli", "--once"])

        self.assertEqual(result.exit_code, 0, result.output)
        lines = [json.loads(line) for line in result.output.strip().splitlines()]

        # Empty reasoning envelopes and streaming deltas are skipped entirely.
        sequences = [line.get("sequence") for line in lines]
        self.assertEqual(sequences, [2, 4, None])

        tool = lines[0]
        self.assertEqual(tool["kind"], "tool")
        self.assertEqual(tool["name"], "cua/get_desktop_state")
        self.assertTrue(tool["ok"])
        rendered = json.dumps(tool)
        self.assertNotIn("AAAA", rendered)
        self.assertIn("base64 chars", rendered)
        self.assertIn("chars]", tool["result"]["text"])

        command = lines[1]
        self.assertEqual(command["kind"], "command")
        self.assertEqual(command["exitCode"], 0)
        self.assertEqual(command["output"], "hi\n")

        cursor = lines[2]
        self.assertEqual(cursor["cursor"], 4)
        self.assertIn("--after", cursor["hint"])

    @patch("cli_tools.cli.codashe.CodasheGatewayClient", FakeCliClient)
    def test_watch_raw_keeps_complete_payloads(self) -> None:
        result = self.runner.invoke(
            main,
            ["codashe", "watch", "job-cli", "--once", "--raw"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        self.assertEqual(len(lines), 4 + 1)  # all events plus the cursor hint
        raw_tool = json.dumps(lines[1])
        self.assertIn("A" * 1000, raw_tool)

    @patch("cli_tools.cli.codashe.CodasheGatewayClient", FakeCliClient)
    def test_schema_prints_a_compact_summary_by_default(self) -> None:
        result = self.runner.invoke(main, ["codashe", "schema"])

        self.assertEqual(result.exit_code, 0, result.output)
        summary = json.loads(result.output)
        self.assertEqual(summary["protocolVersion"], "1.2.0")
        self.assertIn("hint", summary)
        self.assertNotIn("schema", summary)


if __name__ == "__main__":
    unittest.main()
