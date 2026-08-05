import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_tools import workspace
from cli_tools.cli.commit import commit_command
from cli_tools.cli.setup import setup_command


def write_config(path: Path, projects: dict) -> None:
    path.write_text(json.dumps({"schemaVersion": 1, "projects": projects}))


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_is_an_empty_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(workspace.load_config(Path(tmp) / "nope.json"), {})

    def test_invalid_json_is_a_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.json"
            path.write_text("{not json")
            with self.assertRaises(workspace.WorkspaceConfigError):
                workspace.load_config(path)

    def test_non_object_top_level_is_a_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.json"
            path.write_text('["direct"]')
            with self.assertRaises(workspace.WorkspaceConfigError):
                workspace.load_config(path)

    def test_env_var_overrides_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom.json"
            write_config(custom, {})
            with patch.dict(
                "os.environ", {workspace.CONFIG_ENV_VAR: str(custom)}
            ):
                self.assertEqual(workspace.default_config_path(), custom)
                self.assertEqual(
                    workspace.load_config(),
                    {"schemaVersion": 1, "projects": {}},
                )


class ParseProjectsTest(unittest.TestCase):
    def test_underscore_documentation_keys_are_ignored(self) -> None:
        config = {
            "schemaVersion": 1,
            "projects": {},
            "_example": {
                "projects": {"fake": {"path": "/x", "workflow": "nope"}}
            },
        }
        self.assertEqual(workspace.parse_projects(config), {})

    def test_unknown_workflow_is_rejected(self) -> None:
        config = {"projects": {"p": {"path": "/tmp/p", "workflow": "yolo"}}}
        with self.assertRaises(workspace.WorkspaceConfigError):
            workspace.parse_projects(config)

    def test_missing_path_is_rejected(self) -> None:
        config = {"projects": {"p": {"workflow": "direct"}}}
        with self.assertRaises(workspace.WorkspaceConfigError):
            workspace.parse_projects(config)

    def test_defaults_workflow_to_direct(self) -> None:
        config = {"projects": {"p": {"path": "/tmp/p"}}}
        project = workspace.parse_projects(config)["p"]
        self.assertEqual(project.workflow, workspace.WORKFLOW_DIRECT)
        self.assertFalse(project.uses_worktree_pr)


class RepoFixture:
    """Builds a real git repository (optionally with a linked worktree)."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "config", "user.name", "Test User")
        self.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "tracked.txt").write_text("initial\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "chore: initialize fixture")

    @staticmethod
    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def add_worktree(self, name: str = "wt") -> Path:
        worktree_path = self.root / name
        self.git(
            self.repo, "worktree", "add", "-q", "-b", f"task/{name}",
            str(worktree_path),
        )
        return worktree_path


class CheckoutDetectionTest(unittest.TestCase):
    def test_main_checkout_and_linked_worktree_are_distinguished(self) -> None:
        fixture = RepoFixture(self)
        self.assertTrue(workspace.is_main_checkout(fixture.repo))
        worktree_path = fixture.add_worktree()
        self.assertFalse(workspace.is_main_checkout(worktree_path))


class ResolveProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture(self)

    def config(self, workflow: str) -> dict:
        return {
            "projects": {
                "fixture": {
                    "path": str(self.fixture.repo),
                    "workflow": workflow,
                }
            }
        }

    def test_matches_main_checkout_and_subdirectories(self) -> None:
        config = self.config("worktree-pr")
        project = workspace.resolve_project(self.fixture.repo, config)
        self.assertEqual(project.name, "fixture")
        nested = self.fixture.repo / "pkg"
        nested.mkdir()
        self.assertEqual(
            workspace.resolve_project(nested, config).name, "fixture"
        )

    def test_linked_worktree_resolves_to_owning_project(self) -> None:
        config = self.config("worktree-pr")
        worktree_path = self.fixture.add_worktree()
        project = workspace.resolve_project(worktree_path, config)
        self.assertEqual(project.name, "fixture")
        self.assertEqual(project.path, self.fixture.repo)

    def test_unrelated_repo_has_no_project(self) -> None:
        config = {
            "projects": {
                "other": {"path": "/elsewhere", "workflow": "direct"}
            }
        }
        self.assertIsNone(workspace.resolve_project(self.fixture.repo, config))

    def test_empty_config_resolves_nothing(self) -> None:
        self.assertIsNone(workspace.resolve_project(self.fixture.repo, {}))


class SetupWorkspaceCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = Path(self.temp_dir.name) / "prompts" / "workspace.json"

    def test_scaffolds_a_parseable_empty_config(self) -> None:
        result = CliRunner().invoke(
            setup_command, ["workspace", "--path", str(self.config)]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        loaded = workspace.load_config(self.config)
        self.assertEqual(loaded["projects"], {})
        self.assertEqual(loaded["schemaVersion"], workspace.SCHEMA_VERSION)
        self.assertIn("_example", loaded)
        self.assertEqual(workspace.parse_projects(loaded), {})

    def test_refuses_to_clobber_without_force(self) -> None:
        existing = {
            "schemaVersion": 1,
            "projects": {
                "mine": {"path": "/a", "workflow": "direct"}
            },
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        result = CliRunner().invoke(
            setup_command, ["workspace", "--path", str(self.config)]
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already exists", result.output)
        self.assertEqual(json.loads(self.config.read_text()), existing)

    def test_force_replaces_the_file(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{}")
        result = CliRunner().invoke(
            setup_command, ["workspace", "--path", str(self.config), "--force"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Replaced", result.output)
        self.assertEqual(json.loads(self.config.read_text())["projects"], {})

    def test_env_var_selects_default_target(self) -> None:
        result = CliRunner().invoke(
            setup_command,
            ["workspace"],
            env={workspace.CONFIG_ENV_VAR: str(self.config)},
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.config.exists())


class CommitPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture(self)
        self.repo = self.fixture.repo
        self.config = self.fixture.root / "workspace.json"

    def configure(self, workflow: str) -> None:
        write_config(
            self.config,
            {"fixture": {"path": str(self.repo), "workflow": workflow}},
        )

    def invoke_commit(self, *args: str):
        return CliRunner().invoke(
            commit_command,
            list(args),
            env={workspace.CONFIG_ENV_VAR: str(self.config)},
        )

    def head(self) -> str:
        return self.fixture.git(self.repo, "rev-parse", "HEAD")

    def test_worktree_pr_main_checkout_commit_is_rejected_before_staging(
        self,
    ) -> None:
        self.configure("worktree-pr")
        (self.repo / "tracked.txt").write_text("changed\n")
        head_before = self.head()

        result = self.invoke_commit(str(self.repo), "-m", "feat: rejected")

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Workspace policy", result.output)
        self.assertIn("worktree+PR", result.output)
        self.assertEqual(self.head(), head_before)
        self.assertEqual(
            self.fixture.git(self.repo, "diff", "--cached", "--name-only"), ""
        )

    def test_commit_inside_linked_worktree_is_not_rejected(self) -> None:
        self.configure("worktree-pr")
        worktree_path = self.fixture.add_worktree()
        (worktree_path / "tracked.txt").write_text("changed\n")

        result = self.invoke_commit(
            str(worktree_path), "-m", "feat: worktree commit"
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Workspace policy", result.output)
        self.assertEqual(
            self.fixture.git(worktree_path, "log", "-1", "--format=%s"),
            "feat: worktree commit",
        )

    def test_direct_project_main_checkout_commits_normally(self) -> None:
        self.configure("direct")
        (self.repo / "tracked.txt").write_text("changed\n")

        result = self.invoke_commit(str(self.repo), "-m", "feat: direct")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Workspace policy", result.output)

    def test_unusable_config_warns_and_proceeds(self) -> None:
        self.config.write_text("{not json")
        (self.repo / "tracked.txt").write_text("changed\n")

        result = self.invoke_commit(str(self.repo), "-m", "feat: tolerant")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Ignoring unusable workspace config", result.output)
