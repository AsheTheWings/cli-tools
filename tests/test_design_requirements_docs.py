import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_tools.cli import design_requirements_docs as documents
from cli_tools.cli import document_utils as shared


class DesignRequirementsDocsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "source.txt").write_text("baseline\n", encoding="utf-8")
        self.design_dir = self.root / "design"
        self.requirements_dir = self.root / "requirements"
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def paths_patch(self):
        return patch.multiple(
            documents,
            DESIGN_DIR=self.design_dir,
            REQUIREMENTS_DIR=self.requirements_dir,
        )

    def shared_paths_patch(self):
        return patch.multiple(
            shared,
            DESIGN_DIR=self.design_dir,
            REQUIREMENTS_DIR=self.requirements_dir,
        )

    def build_pair(self):
        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_doc_group,
                [
                    "build",
                    "-r",
                    str(self.repo),
                    "-t",
                    'Quoted "Design"',
                    "-f",
                    "Feature",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        return (
            next(self.design_dir.glob("design-*.md")),
            next(self.requirements_dir.glob("requirements-*.md")),
        )

    def test_build_and_verify_pair_from_either_document(self) -> None:
        design_path, requirements_path = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            design_result = self.runner.invoke(
                documents.design_doc_group, ["verify", str(design_path)]
            )
            requirements_result = self.runner.invoke(
                documents.requirements_doc_group,
                ["verify", str(requirements_path)],
            )

        self.assertEqual(design_result.exit_code, 0, design_result.output)
        self.assertEqual(requirements_result.exit_code, 0, requirements_result.output)
        self.assertIn("requirements:", design_path.read_text(encoding="utf-8"))
        self.assertIn("design:", requirements_path.read_text(encoding="utf-8"))

    def test_verify_detects_repository_drift(self) -> None:
        design_path, _ = self.build_pair()
        (self.repo / "source.txt").write_text("changed\n", encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_doc_group, ["verify", str(design_path)]
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("MISMATCH", result.output)

    def test_superseding_design_initializes_a_new_linked_pair(self) -> None:
        design_path, requirements_path = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_doc_group,
                ["build", "-u", str(design_path), "-t", "Replacement"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        designs = sorted(self.design_dir.glob("design-*.md"))
        requirements = sorted(self.requirements_dir.glob("requirements-*.md"))
        self.assertEqual(len(designs), 2)
        self.assertEqual(len(requirements), 2)
        self.assertIn(
            design_path.name, designs[-1].read_text(encoding="utf-8")
        )
        self.assertIn(
            requirements_path.name,
            requirements[-1].read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
