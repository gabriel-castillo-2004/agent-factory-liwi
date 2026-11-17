"""Git registry branch publisher updating registry.json and per-project files without touching main (INV-19, INV-21, AC-3.4)."""

import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import ProjectStatus, validate_project_id
from agent_factory.ports.publisher import RegistryEntry, RegistryPublishError
from agent_factory.ports.runner import CommandResult, Runner
from agent_factory.secrets import redact_secrets

PROTECTED_MAIN_BRANCHES: frozenset[str] = frozenset({"main", "master"})


class GitRegistryPublisher:
    """Publishes ProjectStatus entries and registry.json onto an isolated git registry branch via Runner."""

    def __init__(
        self,
        repo_path: Path | str,
        branch: str = "registry",
        remote: str = "origin",
        push_to_remote: bool = True,
        runner: Runner | None = None,
        work_dir: Path | None = None,
        secrets: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.branch = branch
        self.remote = remote
        self.push_to_remote = push_to_remote
        self.runner: Runner = runner if runner is not None else SubprocessRunner()
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self.secrets: tuple[str, ...] = tuple(secrets)
        self.env = dict(env) if env is not None else None

    def _run_git(self, args: Sequence[str], cwd: Path) -> CommandResult:
        """Run a git command via self.runner and redact secrets if execution fails."""
        cmd = ["git"] + list(args)
        try:
            res = self.runner.run(cmd, cwd=cwd, env=self.env)
        except (OSError, RuntimeError, ValueError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets, env=self.env)
            raise RegistryPublishError(f"Git execution failed: {clean_err}") from None

        if res.exit_code != 0:
            err_text = res.stderr or res.stdout or f"exit code {res.exit_code}"
            clean_err = redact_secrets(err_text, extra_secrets=self.secrets, env=self.env)
            raise RegistryPublishError(
                f"Git command failed ({' '.join(args[:2])}): {clean_err}"
            )
        return res

    def _resolve_work_dir(self) -> Path:
        """Return the effective working directory for the registry repository."""
        if self.work_dir is not None:
            return self.work_dir
        return Path(tempfile.gettempdir()) / f"af_registry_{abs(hash(str(self.repo_path)))}"

    def _ensure_repository_ready(self, work_dir: Path) -> None:
        """Ensure work_dir is cloned and checked out on self.branch."""
        if not (work_dir / ".git").exists():
            work_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run_git(["clone", str(self.repo_path), str(work_dir)], cwd=work_dir.parent)

            # Check if self.branch exists on local or remote
            check_local = self.runner.run(
                ["git", "show-ref", "--verify", f"refs/heads/{self.branch}"],
                cwd=work_dir,
                env=self.env,
            )
            check_remote = self.runner.run(
                ["git", "show-ref", "--verify", f"refs/remotes/{self.remote}/{self.branch}"],
                cwd=work_dir,
                env=self.env,
            )
            if check_local.exit_code == 0 or check_remote.exit_code == 0:
                self._run_git(["checkout", self.branch], cwd=work_dir)
            else:
                self._run_git(["checkout", "--orphan", self.branch], cwd=work_dir)
                st = self.runner.run(["git", "status", "--porcelain"], cwd=work_dir, env=self.env)
                if st.stdout.strip():
                    self._run_git(["rm", "-rf", "."], cwd=work_dir)
                for child in work_dir.iterdir():
                    if child.name != ".git":
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
        else:
            self._run_git(["checkout", self.branch], cwd=work_dir)

    def publish_project(self, status: ProjectStatus) -> RegistryEntry:
        """Publish projects/<project_id>.json and update registry.json on self.branch without touching main."""
        if self.branch.strip().lower() in PROTECTED_MAIN_BRANCHES:
            raise ValueError(f"Cannot publish registry onto protected branch '{self.branch}'")

        validate_project_id(status.project_id)
        work_dir = self._resolve_work_dir()
        self._ensure_repository_ready(work_dir)

        repo_url = status.links.repo if status.links else None
        deploy_url = status.links.deploy if status.links else None
        entry = RegistryEntry(
            project_id=status.project_id,
            name=status.name,
            objective=status.objective,
            state=status.state,
            progress_pct=status.progress_pct,
            repo_url=repo_url,
            deploy_url=deploy_url,
            plan_hash=status.plan_hash,
            updated_at=status.updated_at,
        )

        reg_file = work_dir / "registry.json"
        index_data: dict[str, object] = {"version": "1.0", "updated_at": None, "projects": {}}
        if reg_file.exists():
            try:
                raw_index = json.loads(reg_file.read_text(encoding="utf-8"))
                if isinstance(raw_index, dict):
                    index_data = raw_index
            except (json.JSONDecodeError, OSError):
                index_data = {"version": "1.0", "updated_at": None, "projects": {}}

        if "projects" not in index_data or not isinstance(index_data["projects"], dict):
            index_data["projects"] = {}

        projects_dict = index_data["projects"]
        if isinstance(projects_dict, dict):
            projects_dict[status.project_id] = entry.model_dump(mode="json")
        now_ts = status.updated_at or datetime.now(UTC)
        index_data["updated_at"] = now_ts.isoformat()
        reg_file.write_text(json.dumps(index_data, indent=2, default=str), encoding="utf-8")

        proj_dir = work_dir / "projects"
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj_file = proj_dir / f"{status.project_id}.json"
        proj_file.write_text(status.model_dump_json(indent=2), encoding="utf-8")

        self._run_git(["add", "registry.json", "projects"], cwd=work_dir)

        diff_check = self.runner.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=work_dir,
            env=self.env,
        )
        if diff_check.exit_code != 0:
            self._run_git(
                [
                    "-c",
                    "user.name=AgentFactory",
                    "-c",
                    "user.email=agent@factory.local",
                    "commit",
                    "-m",
                    f"chore(registry): update {status.project_id}",
                ],
                cwd=work_dir,
            )
            if self.push_to_remote:
                self._run_git(["push", "-u", self.remote, self.branch], cwd=work_dir)

        return entry

    def load_index(self) -> dict[str, RegistryEntry]:
        """Load registry.json from self.branch without modifying checked-out working tree."""
        work_dir = self._resolve_work_dir()
        reg_file = work_dir / "registry.json"
        if reg_file.exists():
            try:
                data = json.loads(reg_file.read_text(encoding="utf-8"))
                projects = data.get("projects", {})
                return {pid: RegistryEntry.model_validate(p) for pid, p in projects.items()}
            except (json.JSONDecodeError, ValueError, OSError):
                return {}

        try:
            res = self._run_git(
                ["show", f"refs/heads/{self.branch}:registry.json"],
                cwd=self.repo_path,
            )
            data = json.loads(res.stdout)
            projects = data.get("projects", {})
            return {pid: RegistryEntry.model_validate(p) for pid, p in projects.items()}
        except (json.JSONDecodeError, ValueError, OSError, RegistryPublishError):
            return {}

    def publish(self, status: ProjectStatus) -> None:
        """StatusPublisher-compatible wrapper calling publish_project."""
        self.publish_project(status)

    def load(self, project_id: str) -> ProjectStatus | None:
        """Load ProjectStatus for project_id from the registry branch, or None if absent."""
        validate_project_id(project_id)
        work_dir = self._resolve_work_dir()
        proj_file = work_dir / "projects" / f"{project_id}.json"
        if proj_file.exists():
            try:
                return ProjectStatus.model_validate_json(proj_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None

        try:
            res = self._run_git(
                ["show", f"refs/heads/{self.branch}:projects/{project_id}.json"],
                cwd=self.repo_path,
            )
            return ProjectStatus.model_validate_json(res.stdout)
        except (ValueError, OSError, RegistryPublishError):
            return None

    def delete(self, project_id: str) -> None:
        """Remove project entry from the registry branch."""
        validate_project_id(project_id)
        work_dir = self._resolve_work_dir()
        self._ensure_repository_ready(work_dir)

        reg_file = work_dir / "registry.json"
        if reg_file.exists():
            try:
                index_data = json.loads(reg_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                index_data = {}
            if (
                isinstance(index_data, dict)
                and "projects" in index_data
                and isinstance(index_data["projects"], dict)
                and project_id in index_data["projects"]
            ):
                del index_data["projects"][project_id]
                reg_file.write_text(json.dumps(index_data, indent=2), encoding="utf-8")

        proj_file = work_dir / "projects" / f"{project_id}.json"
        if proj_file.exists():
            proj_file.unlink(missing_ok=True)

        self._run_git(["add", "registry.json", "projects"], cwd=work_dir)
        diff_check = self.runner.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=work_dir,
            env=self.env,
        )
        if diff_check.exit_code != 0:
            self._run_git(
                [
                    "-c",
                    "user.name=AgentFactory",
                    "-c",
                    "user.email=agent@factory.local",
                    "commit",
                    "-m",
                    f"chore(registry): delete {project_id}",
                ],
                cwd=work_dir,
            )
            if self.push_to_remote:
                self._run_git(["push", self.remote, self.branch], cwd=work_dir)
