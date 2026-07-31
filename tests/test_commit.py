import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from click.testing import CliRunner

from cli_tools.cli.commit import (
    COMMIT_SYSTEM_INSTRUCTION,
    commit_command,
    generate_commit_message,
    normalize_commit_message,
    plan_document_subject,
    plan_repository_instructions,
)


class CommitPromptTest(unittest.TestCase):
    def test_breaking_change_threshold_and_format_are_explicit(self) -> None:
        self.assertIn("public or relied-upon", COMMIT_SYSTEM_INSTRUCTION)
        self.assertIn("requires consumer or operator action", COMMIT_SYSTEM_INSTRUCTION)
        self.assertIn("append ! before the colon", COMMIT_SYSTEM_INSTRUCTION)
        self.assertIn("BREAKING CHANGE: footer", COMMIT_SYSTEM_INSTRUCTION)
        self.assertIn(
            "documentation of a\n    future breaking change",
            COMMIT_SYSTEM_INSTRUCTION,
        )
        self.assertIn("labeled as 'feat'", COMMIT_SYSTEM_INSTRUCTION)
        self.assertIn("without Markdown code fences", COMMIT_SYSTEM_INSTRUCTION)


class CommitMessageNormalizationTest(unittest.TestCase):
    def test_unwraps_full_git_fence_from_generated_message(self) -> None:
        generated = """```git
refactor(kiro): preserve canonical account state

Merge persisted changes into live account objects.
```"""
        self.assertEqual(
            normalize_commit_message(generated),
            "refactor(kiro): preserve canonical account state\n\n"
            "Merge persisted changes into live account objects.",
        )

    def test_unwraps_unlabelled_and_tilde_fences(self) -> None:
        self.assertEqual(
            normalize_commit_message("```\nfix: strip fences\n```"),
            "fix: strip fences",
        )
        self.assertEqual(
            normalize_commit_message("~~~text\r\nfix: strip fences\r\n~~~"),
            "fix: strip fences",
        )

    def test_preserves_plain_messages_and_internal_body_fences(self) -> None:
        plain = (
            "fix(cli): retain examples\n\n"
            "Document this example:\n```sh\ntool commit -y\n```"
        )
        self.assertEqual(normalize_commit_message(plain), plain)

    @patch("cli_tools.cli.commit.get_tera_client")
    def test_generation_normalizes_before_returning(self, get_client: Mock) -> None:
        client = Mock()
        client.complete = AsyncMock(
            return_value=("```git\nfix(cli): strip output fences\n```", {})
        )
        get_client.return_value = client

        result = asyncio.run(generate_commit_message("diff --git a/a b/a"))

        self.assertEqual(result, "fix(cli): strip output fences")


class PlanCommitInstructionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = [
            "design-20260718-2.md",
            "requirements-20260718-2.md",
        ]

    def test_pair_subject_uses_docs_type_and_filenames(self) -> None:
        self.assertEqual(
            plan_document_subject("create", self.documents),
            "docs: create design-20260718-2.md and "
            "requirements-20260718-2.md pair",
        )
        self.assertEqual(
            plan_document_subject("update", self.documents),
            "docs: update design-20260718-2.md and "
            "requirements-20260718-2.md pair",
        )

    def test_instructions_require_exact_operational_subjects(self) -> None:
        instructions = plan_repository_instructions(
            self.documents,
            self.documents,
        )

        self.assertIn(
            "'docs: create design-20260718-2.md and "
            "requirements-20260718-2.md pair'",
            instructions,
        )
        self.assertIn(
            "'docs: update design-20260718-2.md and "
            "requirements-20260718-2.md pair'",
            instructions,
        )
        self.assertNotIn("docs(create)", instructions)
        self.assertNotIn("docs(update)", instructions)


class SelectiveCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "selected.txt").write_text("initial\n")
        (self.repo / "remaining.txt").write_text("initial\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "chore: initialize fixture")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: commit selected changes",
    )
    def test_stage_pathspec_commits_only_selected_changes(
        self, _generate: AsyncMock
    ) -> None:
        (self.repo / "selected.txt").write_text("selected\n")
        (self.repo / "remaining.txt").write_text("remaining\n")

        result = CliRunner().invoke(
            commit_command,
            [str(self.repo), "--stage", "selected.txt"],
            input="y\nn\n",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.git("show", "--format=", "--name-only", "HEAD"), "selected.txt"
        )
        self.assertEqual(self.git("status", "--short"), "M remaining.txt")

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: commit staged changes",
    )
    def test_staged_mode_leaves_unstaged_changes_out(
        self, _generate: AsyncMock
    ) -> None:
        (self.repo / "selected.txt").write_text("selected\n")
        (self.repo / "remaining.txt").write_text("remaining\n")
        self.git("add", "selected.txt")

        result = CliRunner().invoke(
            commit_command,
            [str(self.repo), "--staged"],
            input="y\nn\n",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.git("show", "--format=", "--name-only", "HEAD"), "selected.txt"
        )
        self.assertEqual(self.git("status", "--short"), "M remaining.txt")

    def test_stage_pathspec_refuses_an_existing_index(self) -> None:
        (self.repo / "selected.txt").write_text("selected\n")
        (self.repo / "remaining.txt").write_text("remaining\n")
        self.git("add", "remaining.txt")

        result = CliRunner().invoke(
            commit_command,
            [str(self.repo), "--stage", "selected.txt"],
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("requires an initially clean index", result.output)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "remaining.txt")
        self.assertEqual(self.git("diff", "--name-only"), "selected.txt")

    def test_staging_modes_are_mutually_exclusive(self) -> None:
        result = CliRunner().invoke(
            commit_command,
            [str(self.repo), "--stage", "selected.txt", "--staged"],
        )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("mutually exclusive", result.output)


if __name__ == "__main__":
    unittest.main()
