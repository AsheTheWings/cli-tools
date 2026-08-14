import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_tools import project_manifest
from cli_tools.cli.commit import commit_command


class RepoFixture:
    def __init__(self, test: unittest.TestCase, *, workflow: str = "worktree-pr") -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.repo = self.root / "main"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.name", "Test User")
        self.git(self.repo, "config", "user.email", "test@example.com")
        self.git(
            self.repo,
            "remote",
            "add",
            "origin",
            "git@github.com:Example/fixture.git",
        )
        self.write_manifest(workflow)
        (self.repo / "tracked.txt").write_text("initial\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "chore: initialize fixture")

    def write_manifest(self, workflow: str, *, checkout: str = "main") -> None:
        (self.repo / ".project.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "project": "fixture",
                    "repository": "Example/fixture",
                    "primaryBranch": "main",
                    "primaryCheckout": checkout,
                    "changeDelivery": {"mode": workflow},
                },
                indent=2,
            )
            + "\n"
        )

    @staticmethod
    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def add_worktree(self) -> Path:
        path = self.root / "change"
        self.git(self.repo, "worktree", "add", "-q", "-b", "change", str(path))
        return path


class ProjectManifestResolutionTests(unittest.TestCase):
    def test_main_and_linked_worktree_resolve_accepted_manifest(self) -> None:
        fixture = RepoFixture(self)
        worktree = fixture.add_worktree()
        main = project_manifest.resolve_project(fixture.repo)
        linked = project_manifest.resolve_project(worktree)
        self.assertEqual(main, linked)
        self.assertTrue(project_manifest.is_primary_checkout(fixture.repo))
        self.assertFalse(project_manifest.is_primary_checkout(worktree))

    def test_feature_manifest_cannot_weaken_accepted_policy(self) -> None:
        fixture = RepoFixture(self)
        worktree = fixture.add_worktree()
        manifest = json.loads((worktree / ".project.json").read_text())
        manifest["changeDelivery"]["mode"] = "direct"
        (worktree / ".project.json").write_text(json.dumps(manifest) + "\n")
        fixture.git(worktree, "add", ".project.json")
        fixture.git(worktree, "commit", "-q", "-m", "weaken branch policy")
        self.assertEqual(
            project_manifest.resolve_project(worktree).workflow,
            "worktree-pr",
        )

    def test_checkout_name_mismatch_fails_closed(self) -> None:
        fixture = RepoFixture(self)
        fixture.write_manifest("worktree-pr", checkout="other")
        fixture.git(fixture.repo, "add", ".project.json")
        fixture.git(fixture.repo, "commit", "-q", "-m", "break layout")
        with self.assertRaisesRegex(project_manifest.ProjectManifestError, "directory"):
            project_manifest.resolve_project(fixture.repo)

    def test_initial_worktree_pr_manifest_can_bootstrap_from_linked_worktree(self) -> None:
        fixture = RepoFixture(self)
        fixture.git(fixture.repo, "rm", "-q", ".project.json")
        fixture.git(fixture.repo, "commit", "-q", "-m", "pre-manifest main")
        worktree = fixture.add_worktree()
        fixture.write_manifest("worktree-pr")
        (worktree / ".project.json").write_text(
            (fixture.repo / ".project.json").read_text()
        )
        (fixture.repo / ".project.json").unlink()
        fixture.git(worktree, "add", ".project.json")
        fixture.git(worktree, "commit", "-q", "-m", "bootstrap manifest")
        resolved = project_manifest.resolve_project(worktree)
        self.assertEqual(resolved.workflow, "worktree-pr")

    def test_uncommitted_initial_manifest_can_authorize_only_linked_commit(self) -> None:
        fixture = RepoFixture(self)
        fixture.git(fixture.repo, "rm", "-q", ".project.json")
        fixture.git(fixture.repo, "commit", "-q", "-m", "pre-manifest main")
        worktree = fixture.add_worktree()
        manifest = {
            "schemaVersion": 1,
            "project": "fixture",
            "repository": "Example/fixture",
            "primaryBranch": "main",
            "primaryCheckout": "main",
            "changeDelivery": {"mode": "worktree-pr"},
        }
        (worktree / ".project.json").write_text(json.dumps(manifest) + "\n")
        resolved = project_manifest.resolve_project(worktree)
        self.assertEqual(resolved.workflow, "worktree-pr")

    def test_initial_direct_manifest_cannot_bootstrap_from_linked_worktree(self) -> None:
        fixture = RepoFixture(self)
        fixture.git(fixture.repo, "rm", "-q", ".project.json")
        fixture.git(fixture.repo, "commit", "-q", "-m", "pre-manifest main")
        worktree = fixture.add_worktree()
        manifest = {
            "schemaVersion": 1,
            "project": "fixture",
            "repository": "Example/fixture",
            "primaryBranch": "main",
            "primaryCheckout": "main",
            "changeDelivery": {"mode": "direct"},
        }
        (worktree / ".project.json").write_text(json.dumps(manifest) + "\n")
        fixture.git(worktree, "add", ".project.json")
        fixture.git(worktree, "commit", "-q", "-m", "unsafe bootstrap")
        with self.assertRaisesRegex(
            project_manifest.ProjectManifestError, "must select worktree-pr"
        ):
            project_manifest.resolve_project(worktree)


class CommitPolicyTests(unittest.TestCase):
    def invoke(self, path: Path, *args: str):
        with patch(
            "cli_tools.cli.commit.generate_commit_message",
            return_value="test: project manifest policy",
        ):
            return CliRunner().invoke(commit_command, [str(path), *args])

    def test_worktree_pr_rejects_primary_before_staging(self) -> None:
        fixture = RepoFixture(self)
        (fixture.repo / "tracked.txt").write_text("changed\n")
        result = self.invoke(fixture.repo)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("worktree-pr requires a linked worktree", result.output)
        self.assertEqual(fixture.git(fixture.repo, "diff", "--cached", "--name-only"), "")

    def test_worktree_pr_allows_linked_worktree(self) -> None:
        fixture = RepoFixture(self)
        worktree = fixture.add_worktree()
        (worktree / "tracked.txt").write_text("changed\n")
        result = self.invoke(worktree)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            fixture.git(worktree, "log", "-1", "--format=%s"),
            "test: project manifest policy",
        )

    def test_direct_requires_primary_checkout(self) -> None:
        fixture = RepoFixture(self, workflow="direct")
        worktree = fixture.add_worktree()
        (worktree / "tracked.txt").write_text("changed\n")
        result = self.invoke(worktree)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("direct delivery requires the primary checkout", result.output)

    def test_direct_primary_commits(self) -> None:
        fixture = RepoFixture(self, workflow="direct")
        (fixture.repo / "tracked.txt").write_text("changed\n")
        result = self.invoke(fixture.repo)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_missing_manifest_fails_closed_before_staging(self) -> None:
        fixture = RepoFixture(self)
        fixture.git(fixture.repo, "rm", "-q", ".project.json")
        fixture.git(fixture.repo, "commit", "-q", "-m", "remove manifest")
        (fixture.repo / "tracked.txt").write_text("changed\n")
        result = self.invoke(fixture.repo)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("does not contain .project.json", result.output)
        self.assertEqual(fixture.git(fixture.repo, "diff", "--cached", "--name-only"), "")

    def test_user_can_observably_bootstrap_missing_manifest(self) -> None:
        fixture = RepoFixture(self, workflow="direct")
        fixture.git(fixture.repo, "rm", "-q", ".project.json")
        fixture.git(fixture.repo, "commit", "-q", "-m", "remove manifest")
        (fixture.repo / "tracked.txt").write_text("changed\n")
        result = self.invoke(fixture.repo, "--user")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("overriding unavailable project policy", result.output)


if __name__ == "__main__":
    unittest.main()
