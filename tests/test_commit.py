import unittest

from cli_tools.cli.commit import (
    COMMIT_SYSTEM_INSTRUCTION,
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
