import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_tools.cli.clean_codex_session import (
    DEFAULT_TMP_DIR,
    clean_codex_session_command,
    find_codex_session,
    parse_rollout,
)

SESSION_ID = "019fa63d-3d17-79c3-a41d-0cac9be1b613"


class CleanCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_parse_rollout_removes_tools_by_default(self) -> None:
        lines = [
            json.dumps({
                "timestamp": "2026-07-20T12:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello, clean transcript!"}]
                }
            }),
            json.dumps({
                "timestamp": "2026-07-20T12:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello! How can I help you today?"}]
                }
            }),
            json.dumps({
                "timestamp": "2026-07-20T12:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": {"cmd": "ls -la"}
                }
            }),
            json.dumps({
                "timestamp": "2026-07-20T12:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "file1.txt\nfile2.txt"
                }
            })
        ]

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tmp:
            tmp.write("\n".join(lines))
            tmp_path = Path(tmp.name)

        try:
            entries = parse_rollout(tmp_path, include_tools=False)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["role"], "User")
            self.assertEqual(entries[1]["role"], "Assistant")

            entries_with_tools = parse_rollout(tmp_path, include_tools=True)
            self.assertEqual(len(entries_with_tools), 4)
            self.assertEqual(entries_with_tools[2]["role"], "Tool Call")
            self.assertEqual(entries_with_tools[3]["role"], "Tool Output")
        finally:
            tmp_path.unlink()

    def test_find_codex_session_searches_session_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            rollout = (
                codex_home
                / "sessions"
                / "2026"
                / "07"
                / "28"
                / f"rollout-2026-07-28T01-00-59-{SESSION_ID}.jsonl"
            )
            rollout.parent.mkdir(parents=True)
            rollout.touch()

            self.assertEqual(find_codex_session(SESSION_ID, codex_home), rollout)

    def test_find_codex_session_rejects_ambiguous_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            for directory_name in ("sessions", "archived_sessions"):
                directory = codex_home / directory_name
                directory.mkdir()
                (directory / f"rollout-{SESSION_ID}.jsonl").touch()

            with self.assertRaisesRegex(Exception, "matched multiple rollouts"):
                find_codex_session(SESSION_ID, codex_home)

    def test_cli_clean_defaults_to_tmp_dir(self) -> None:
        lines = [
            json.dumps({
                "timestamp": "2026-07-20T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Default location test"}
            })
        ]

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            session_dir = codex_home / "sessions" / "2026" / "07" / "28"
            session_dir.mkdir(parents=True)
            rollout_path = session_dir / f"rollout-2026-07-28T01-00-59-{SESSION_ID}.jsonl"
            rollout_path.write_text("\n".join(lines), encoding="utf-8")
            expected_clean_file = DEFAULT_TMP_DIR / f"clean-{SESSION_ID}.txt"

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                result = self.runner.invoke(clean_codex_session_command, [SESSION_ID])
            self.assertEqual(result.exit_code, 0)
            self.assertIn(str(expected_clean_file), result.output)
            self.assertTrue(expected_clean_file.exists())
            content = expected_clean_file.read_text(encoding="utf-8")
            self.assertIn("[User]\nDefault location test", content)
            if expected_clean_file.exists():
                expected_clean_file.unlink()

    def test_cli_clean_stdout_flag(self) -> None:
        lines = [
            json.dumps({
                "timestamp": "2026-07-20T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "What is 2 + 2?"}
            }),
            json.dumps({
                "timestamp": "2026-07-20T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "2 + 2 is 4."}
            })
        ]

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            session_dir = codex_home / "sessions"
            session_dir.mkdir()
            rollout_path = session_dir / f"rollout-{SESSION_ID}.jsonl"
            rollout_path.write_text("\n".join(lines), encoding="utf-8")

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                result = self.runner.invoke(
                    clean_codex_session_command, [SESSION_ID, "--stdout"]
                )
            self.assertEqual(result.exit_code, 0)
            self.assertIn("[User]\nWhat is 2 + 2?", result.output)
            self.assertIn("[Assistant]\n2 + 2 is 4.", result.output)
            self.assertNotIn("timestamp", result.output)
            self.assertNotIn("token", result.output)

    def test_cli_rejects_non_uuid_session_id(self) -> None:
        result = self.runner.invoke(clean_codex_session_command, ["not-a-session"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("must be a valid session UUID", result.output)


if __name__ == "__main__":
    unittest.main()
