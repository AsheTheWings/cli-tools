import asyncio
import json
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
            "Document this example:\n```sh\ntool commit --push\n```"
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
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.git("show", "--format=", "--name-only", "HEAD"), "selected.txt"
        )
        self.assertEqual(self.git("status", "--short"), "M remaining.txt")

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: commit selected changes",
    )
    def test_only_flag_is_the_preferred_alias(self, _generate: AsyncMock) -> None:
        (self.repo / "selected.txt").write_text("selected\n")
        (self.repo / "remaining.txt").write_text("remaining\n")

        result = CliRunner().invoke(
            commit_command,
            [str(self.repo), "--only", "selected.txt"],
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


class InvocationContextTest(unittest.TestCase):
    """Pre-flight checks that must fail before staging or generation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "tracked.txt").write_text("initial\n")
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
        return_value="test: deterministic commit",
    )
    def test_non_interactive_stdin_commits_deterministically(
        self, _generate: AsyncMock
    ) -> None:
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(commit_command, [str(self.repo)])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.git("log", "-1", "--format=%s"), "test: deterministic commit"
        )

    def test_yes_flag_is_rejected_loudly_before_any_mutation(self) -> None:
        (self.repo / "tracked.txt").write_text("changed\n")
        head_before = self.git("rev-parse", "HEAD")

        result = CliRunner().invoke(commit_command, [str(self.repo), "-y"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("No such option", result.output)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: should not be used",
    )
    def test_push_on_detached_head_fails_before_committing(
        self, _generate: AsyncMock
    ) -> None:
        head_before = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach")
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--push"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("detached", result.output)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_message_and_instructions_are_mutually_exclusive(self) -> None:
        result = CliRunner().invoke(
            commit_command,
            [str(self.repo), "-m", "fix: x", "-i", "extra"],
        )
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("mutually exclusive", result.output)

    def test_dry_run_cannot_be_combined_with_push(self) -> None:
        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--dry-run", "--push"]
        )
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--dry-run", result.output)


class MessageSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "tracked.txt").write_text("initial\n")
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
        side_effect=AssertionError("generation must be skipped"),
    )
    def test_message_flag_skips_ai_generation(self, generate: AsyncMock) -> None:
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "-m", "fix: manual message"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "fix: manual message")
        generate.assert_not_called()

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: dry run message",
    )
    def test_dry_run_commits_nothing_and_restores_index(
        self, _generate: AsyncMock
    ) -> None:
        head_before = self.git("rev-parse", "HEAD")
        # One file already staged by the user, one modified but unstaged.
        (self.repo / "tracked.txt").write_text("staged change\n")
        self.git("add", "tracked.txt")
        (self.repo / "unstaged.txt").write_text("unstaged\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--dry-run", "--json"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("test: dry run message", result.output)
        self.assertIn("Dry run", result.output)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        # Index restored: only the pre-staged file remains staged.
        self.assertEqual(
            self.git("diff", "--cached", "--name-only"), "tracked.txt"
        )
        self.assertEqual(self.git("status", "--short"), "M  tracked.txt\n?? unstaged.txt")

        summary = json.loads(result.output.strip().splitlines()[-1])
        self.assertTrue(summary["dry_run"])
        self.assertFalse(summary["committed"])
        self.assertEqual(summary["message"], "test: dry run message")

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: json summary",
    )
    def test_json_summary_line_on_success(self, _generate: AsyncMock) -> None:
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--json"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        summary = json.loads(result.output.strip().splitlines()[-1])
        self.assertTrue(summary["committed"])
        self.assertFalse(summary["pushed"])
        self.assertEqual(summary["message"], "test: json summary")
        self.assertEqual(summary["commit"], self.git("rev-parse", "--short", "HEAD"))


class PushBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git_in(self.repo, "init", "-q")
        self.git_in(self.repo, "config", "user.name", "Test User")
        self.git_in(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "tracked.txt").write_text("initial\n")
        self.git_in(self.repo, "add", ".")
        self.git_in(self.repo, "commit", "-q", "-m", "chore: initialize fixture")

        self.origin = self.base / "origin.git"
        self.git_in(self.base, "init", "-q", "--bare", str(self.origin))
        self.git_in(self.repo, "remote", "add", "origin", str(self.origin))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git_in(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def git(self, *args: str) -> str:
        return self.git_in(self.repo, *args)

    def ls_remote(self, remote: str) -> str:
        return self.git("ls-remote", remote)

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: local only",
    )
    def test_commit_stays_local_without_push_flag(self, _generate: AsyncMock) -> None:
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(commit_command, [str(self.repo)])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.ls_remote("origin"), "")

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: push fallback",
    )
    def test_push_falls_back_to_origin_branch_with_warning(
        self, _generate: AsyncMock
    ) -> None:
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--push"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No upstream configured", result.output)
        branch = self.git("branch", "--show-current")
        remote_refs = self.ls_remote("origin")
        self.assertIn(f"refs/heads/{branch}", remote_refs)

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: upstream push",
    )
    def test_push_uses_configured_upstream_remote_and_ref(
        self, _generate: AsyncMock
    ) -> None:
        upstream = self.base / "upstream.git"
        self.git_in(self.base, "init", "-q", "--bare", str(upstream))
        self.git("remote", "add", "upstream", str(upstream))
        self.git("checkout", "-q", "-b", "topic")
        # Publish once so the upstream mapping topic -> upstream/other-name exists.
        self.git("push", "-q", "-u", "upstream", "topic:other-name")
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--push"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("upstream/other-name", result.output)
        upstream_refs = self.ls_remote("upstream")
        self.assertIn("refs/heads/other-name", upstream_refs)
        # The mapping is honored: no same-named branch appears anywhere.
        self.assertNotIn("refs/heads/topic", upstream_refs)
        self.assertEqual(self.ls_remote("origin"), "")

    @patch(
        "cli_tools.cli.commit.generate_commit_message",
        new_callable=AsyncMock,
        return_value="test: push failure",
    )
    def test_push_failure_after_commit_exits_3_but_keeps_commit(
        self, _generate: AsyncMock
    ) -> None:
        self.git("remote", "set-url", "origin", str(self.base / "missing.git"))
        (self.repo / "tracked.txt").write_text("changed\n")

        result = CliRunner().invoke(
            commit_command, [str(self.repo), "--push"]
        )

        self.assertEqual(result.exit_code, 3, result.output)
        self.assertIn("succeeded locally but the push", result.output)
        self.assertEqual(
            self.git("log", "-1", "--format=%s"), "test: push failure"
        )


if __name__ == "__main__":
    unittest.main()
