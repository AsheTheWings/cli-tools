import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from cli_tools.cli.clean import DEFAULT_TMP_DIR, clean_command, parse_rollout


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

    def test_cli_clean_defaults_to_tmp_dir(self) -> None:
        lines = [
            json.dumps({
                "timestamp": "2026-07-20T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Default location test"}
            })
        ]

        with tempfile.NamedTemporaryFile("w", prefix="rollout-test-", suffix=".jsonl", delete=False) as tmp:
            tmp.write("\n".join(lines))
            tmp_path = Path(tmp.name)

        expected_clean_file = DEFAULT_TMP_DIR / f"clean-{tmp_path.stem[8:]}.txt"

        try:
            result = self.runner.invoke(clean_command, [str(tmp_path)])
            self.assertEqual(result.exit_code, 0)
            self.assertIn(str(expected_clean_file), result.output)
            self.assertTrue(expected_clean_file.exists())
            content = expected_clean_file.read_text(encoding="utf-8")
            self.assertIn("[User]\nDefault location test", content)
        finally:
            tmp_path.unlink()
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

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tmp:
            tmp.write("\n".join(lines))
            tmp_path = Path(tmp.name)

        try:
            result = self.runner.invoke(clean_command, [str(tmp_path), "--stdout"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("[User]\nWhat is 2 + 2?", result.output)
            self.assertIn("[Assistant]\n2 + 2 is 4.", result.output)
            self.assertNotIn("timestamp", result.output)
            self.assertNotIn("token", result.output)
        finally:
            tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
