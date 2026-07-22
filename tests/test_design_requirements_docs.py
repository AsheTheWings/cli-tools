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

    def build_unindexed_pair(self):
        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                [
                    "build",
                    "-r",
                    str(self.repo),
                    "-t",
                    'Quoted "Design"',
                    "-D",
                    "runtime",
                    "-f",
                    "Feature",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        return (
            next(self.design_dir.glob("design-*.md")),
            next(self.requirements_dir.glob("requirements-*.md")),
        )

    def build_pair(self):
        design_path, requirements_path = self.build_unindexed_pair()
        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group, ["index", str(design_path)]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        return design_path, requirements_path

    def test_build_creates_agent_owned_unnumbered_requirements(self) -> None:
        _, requirements_path = self.build_unindexed_pair()

        content = requirements_path.read_text(encoding="utf-8")
        self.assertIn("### Requirement", content)
        self.assertNotIn("### R1. Requirement", content)

    def test_build_and_verify_pair_from_design_document(self) -> None:
        design_path, requirements_path = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            design_result = self.runner.invoke(
                documents.design_group, ["verify", str(design_path)]
            )
            requirements_result = self.runner.invoke(
                documents.design_group,
                ["verify", str(requirements_path)],
            )

        self.assertEqual(design_result.exit_code, 0, design_result.output)
        self.assertEqual(requirements_result.exit_code, 1, requirements_result.output)
        self.assertIn("Design document", requirements_result.output)
        design_content = design_path.read_text(encoding="utf-8")
        self.assertIn("requirements:", design_content)
        self.assertIn("    design:", design_content)
        self.assertIn("    implementation: null", design_content)
        self.assertIn('domains:\n  - "runtime"', design_content)
        self.assertNotIn("scopes:", design_content)
        self.assertIn("design:", requirements_path.read_text(encoding="utf-8"))

    def test_build_rejects_removed_scope_option(self) -> None:
        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["build", "-r", str(self.repo), "--scope", "runtime"],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("No such option '--scope'", result.output)

    def test_verify_detects_repository_drift(self) -> None:
        design_path, _ = self.build_pair()
        (self.repo / "source.txt").write_text("changed\n", encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group, ["verify", str(design_path)]
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("MISMATCH", result.output)

    def test_verify_rejects_different_pair_statuses(self) -> None:
        design_path, requirements_path = self.build_pair()
        content = requirements_path.read_text(encoding="utf-8")
        requirements_path.write_text(
            content.replace("status: active", "status: cancelled"),
            encoding="utf-8",
        )

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["verify", str(design_path)],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("different statuses", result.output)

    def test_verify_accepts_legacy_scalar_repository_snapshots(self) -> None:
        design_path, requirements_path = self.build_pair()
        for path in (design_path, requirements_path):
            content = path.read_text(encoding="utf-8")
            content = content.replace(
                f'  "{self.repo}":\n'
                f"    design: {shared.generate_tree_sha(self.repo)}\n"
                "    implementation: null\n",
                f'  "{self.repo}": {shared.generate_tree_sha(self.repo)}\n',
            )
            path.write_text(content, encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["verify", str(design_path)],
            )

        self.assertEqual(result.exit_code, 0, result.output)

    def test_superseding_design_initializes_a_new_linked_pair(self) -> None:
        design_path, requirements_path = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
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
        for path in (design_path, requirements_path):
            content = path.read_text(encoding="utf-8")
            path.write_text(
                content.replace("status: active", "status: superseded"),
                encoding="utf-8",
            )
        with self.paths_patch(), self.shared_paths_patch():
            index_result = self.runner.invoke(
                documents.design_group,
                ["index", str(designs[-1])],
            )
            verify_result = self.runner.invoke(
                documents.design_group,
                ["verify", str(designs[-1])],
            )
        self.assertEqual(index_result.exit_code, 0, index_result.output)
        self.assertEqual(verify_result.exit_code, 0, verify_result.output)

    def test_relation_inherits_legacy_scopes_as_domains(self) -> None:
        design_path, requirements_path = self.build_pair()
        for path in (design_path, requirements_path):
            content = path.read_text(encoding="utf-8")
            path.write_text(
                content.replace(
                    'domains:\n  - "runtime"',
                    'scopes:\n  - "legacy-runtime"',
                ),
                encoding="utf-8",
            )

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["build", "-u", str(design_path), "-t", "Replacement"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        replacement = sorted(self.design_dir.glob("design-*.md"))[-1].read_text(
            encoding="utf-8"
        )
        self.assertIn('domains:\n  - "legacy-runtime"', replacement)
        self.assertNotIn("scopes:", replacement)

    def test_extending_design_initializes_a_non_replacing_linked_pair(self) -> None:
        design_path, requirements_path = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            capture_result = self.runner.invoke(
                documents.design_group,
                ["capture-implementation", str(design_path)],
            )
            result = self.runner.invoke(
                documents.design_group,
                ["build", "-e", str(design_path), "-t", "Follow-on"],
            )

        self.assertEqual(capture_result.exit_code, 0, capture_result.output)
        self.assertEqual(result.exit_code, 0, result.output)
        designs = sorted(self.design_dir.glob("design-*.md"))
        requirements = sorted(self.requirements_dir.glob("requirements-*.md"))
        extended_design = designs[-1].read_text(encoding="utf-8")
        extended_requirements = requirements[-1].read_text(encoding="utf-8")
        self.assertIn(f'extends: "{design_path.name}"', extended_design)
        self.assertIn(
            f'extends: "{requirements_path.name}"',
            extended_requirements,
        )
        self.assertNotIn("supersedes:", extended_design)
        with self.paths_patch(), self.shared_paths_patch():
            index_result = self.runner.invoke(
                documents.design_group,
                ["index", str(designs[-1])],
            )
            verify_result = self.runner.invoke(
                documents.design_group,
                ["verify", str(designs[-1])],
            )
        self.assertEqual(index_result.exit_code, 0, index_result.output)
        self.assertEqual(verify_result.exit_code, 0, verify_result.output)

    def test_extend_and_supersede_are_mutually_exclusive(self) -> None:
        design_path, _ = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                [
                    "build",
                    "-e",
                    str(design_path),
                    "-u",
                    str(design_path),
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("mutually exclusive", result.output)

    def test_active_design_cannot_be_extended(self) -> None:
        design_path, _ = self.build_pair()

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["build", "-e", str(design_path)],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("only extend an implemented design", result.output)

    def test_implemented_design_can_be_extended_but_not_superseded(self) -> None:
        design_path, requirements_path = self.build_pair()
        for path in (design_path, requirements_path):
            content = path.read_text(encoding="utf-8")
            path.write_text(
                content.replace("status: active", "status: implemented"),
                encoding="utf-8",
            )

        with self.paths_patch(), self.shared_paths_patch():
            extend_result = self.runner.invoke(
                documents.design_group,
                ["build", "-e", str(design_path)],
            )
            supersede_result = self.runner.invoke(
                documents.design_group,
                ["build", "-u", str(design_path)],
            )

        self.assertEqual(extend_result.exit_code, 0, extend_result.output)
        self.assertEqual(supersede_result.exit_code, 1, supersede_result.output)
        self.assertIn("active, unimplemented design", supersede_result.output)

    def test_capture_implementation_preserves_design_sha_and_tracks_current_tree(
        self,
    ) -> None:
        design_path, requirements_path = self.build_pair()
        original = shared.extract_repo_snapshots(
            shared.parse_frontmatter(design_path.read_text(encoding="utf-8"))[0]
        )
        (self.repo / "source.txt").write_text("implemented\n", encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            capture_result = self.runner.invoke(
                documents.design_group,
                ["capture-implementation", str(design_path)],
            )
            verify_result = self.runner.invoke(
                documents.design_group,
                ["verify", str(design_path)],
            )

        self.assertEqual(capture_result.exit_code, 0, capture_result.output)
        self.assertEqual(verify_result.exit_code, 0, verify_result.output)
        design_frontmatter, _ = shared.parse_frontmatter(
            design_path.read_text(encoding="utf-8")
        )
        requirements_frontmatter, _ = shared.parse_frontmatter(
            requirements_path.read_text(encoding="utf-8")
        )
        captured = shared.extract_repo_snapshots(design_frontmatter)
        self.assertEqual(
            captured,
            shared.extract_repo_snapshots(requirements_frontmatter),
        )
        self.assertEqual(
            shared.extract_frontmatter_scalar(design_frontmatter, "status"),
            "implemented",
        )
        self.assertEqual(
            shared.extract_frontmatter_scalar(requirements_frontmatter, "status"),
            "implemented",
        )
        self.assertEqual(
            captured[str(self.repo)]["design"],
            original[str(self.repo)]["design"],
        )
        self.assertNotEqual(
            captured[str(self.repo)]["implementation"],
            captured[str(self.repo)]["design"],
        )

    def test_index_current_requirements_makes_no_changes(self) -> None:
        design_path, requirements_path = self.build_pair()
        requirements_before = requirements_path.read_text(encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["index", str(design_path)],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("indices are current", result.output)
        self.assertEqual(requirements_path.read_text(encoding="utf-8"), requirements_before)

    def test_index_assigns_and_refreshes_indices(self) -> None:
        design_path, requirements_path = self.build_pair()
        requirements_content = requirements_path.read_text(encoding="utf-8")

        corrupted_content = requirements_content.replace(
            "### R1. Requirement", "### R5. Requirement"
        )
        corrupted_content += "\n### Another Requirement\n\nAnother obligation.\n"
        requirements_path.write_text(corrupted_content, encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["index", str(design_path)],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Indexed 2 requirements", result.output)

        updated_content = requirements_path.read_text(encoding="utf-8")
        self.assertIn("### R1. Requirement", updated_content)
        self.assertIn("### R2. Another Requirement", updated_content)
        self.assertNotIn("### R5. Requirement", updated_content)

    def test_index_ignores_bodyless_legacy_h3_grouping_labels(self) -> None:
        design_path, requirements_path = self.build_pair()
        content = requirements_path.read_text(encoding="utf-8")
        content = content.replace(
            "### R1. Requirement", "### Legacy grouping\n\n### Requirement"
        )
        requirements_path.write_text(content, encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group, ["index", str(design_path)]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        updated = requirements_path.read_text(encoding="utf-8")
        self.assertIn("### Legacy grouping", updated)
        self.assertIn("### R1. Requirement", updated)

    def test_verify_detects_non_sequential_indices(self) -> None:
        design_path, requirements_path = self.build_pair()
        requirements_content = requirements_path.read_text(encoding="utf-8")
        
        # Manually introduce non-sequential indices
        corrupted_content = requirements_content.replace(
            "### R1. Requirement", "### R5. Requirement"
        )
        requirements_path.write_text(corrupted_content, encoding="utf-8")

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group,
                ["verify", str(design_path)],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Requirement indices are not current", result.output)
        self.assertIn("tool design index", result.output)

    def test_verify_rejects_unindexed_requirements(self) -> None:
        design_path, _ = self.build_unindexed_pair()

        with self.paths_patch(), self.shared_paths_patch():
            result = self.runner.invoke(
                documents.design_group, ["verify", str(design_path)]
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("an unindexed requirement", result.output)
        self.assertIn("tool design index", result.output)


if __name__ == "__main__":
    unittest.main()
