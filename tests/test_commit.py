import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cli_tools.cli.commit import (
    COMMIT_SYSTEM_INSTRUCTION,
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


if __name__ == "__main__":
    unittest.main()
