"""Git registry branch publisher updating registry.json and per-project files without touching main (INV-19, INV-21, AC-3.4)."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import ProjectStatus
from agent_factory.ports.publisher import RegistryEntry
from agent_factory.ports.runner import Runner

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

    def publish_project(self, status: ProjectStatus) -> RegistryEntry:
        """Publish projects/<project_id>.json and update registry.json on self.branch without touching main."""
        raise NotImplementedError

    def load_index(self) -> dict[str, RegistryEntry]:
        """Load registry.json from self.branch without modifying checked-out working tree."""
        raise NotImplementedError

    def publish(self, status: ProjectStatus) -> None:
        """StatusPublisher-compatible wrapper calling publish_project."""
        raise NotImplementedError

    def load(self, project_id: str) -> ProjectStatus | None:
        """Load ProjectStatus for project_id from the registry branch, or None if absent."""
        raise NotImplementedError

    def delete(self, project_id: str) -> None:
        """Remove project entry from the registry branch."""
        raise NotImplementedError
