"""Local build worker, job queue, and write-test-fix loop (Section 7 Phase 2, INV-09, INV-13, INV-15, INV-17)."""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, Field

from agent_factory.domain.models import BuildReport, Plan
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.clock import Clock
from agent_factory.ports.code_agent import CodeAgent
from agent_factory.ports.runner import Runner
from agent_factory.ports.vcs import VCS
from agent_factory.workspace import TestLockSnapshot, WorkspaceManager


class JobState(StrEnum):
    """Lifecycle states of a local build job in the worker queue."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED_QUOTA = "PAUSED_QUOTA"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BuildJob(BaseModel):
    """Persistent state of an approved project build job (AC-2.4)."""

    project_id: str
    plan: Plan
    state: JobState = JobState.QUEUED
    invocations_used: int = 0
    ci_rounds_used: int = 0
    test_results: dict[str, bool] = Field(default_factory=dict)
    lock_snapshot: TestLockSnapshot | None = None
    last_failure_output: str = ""
    pause_count: int = 0
    report: BuildReport | None = None


class LocalJobQueue:
    """In-memory/local queue of approved build jobs supporting quota pause and resume."""

    def __init__(self) -> None:
        self._jobs: dict[str, BuildJob] = {}
        self._order: list[str] = []

    def enqueue(self, project_id: str, plan: Plan) -> BuildJob:
        """Add or retrieve a BuildJob for project_id."""
        raise NotImplementedError

    def get_job(self, project_id: str) -> BuildJob | None:
        """Return the BuildJob for project_id, or None if not queued."""
        raise NotImplementedError

    def next_runnable_job(self) -> BuildJob | None:
        """Return the next job in QUEUED or PAUSED_QUOTA state."""
        raise NotImplementedError


class BuildWorker:
    """Executes Step A (test generation + lock), Step B (local loop), and CI verification."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        workspace_manager: WorkspaceManager,
        code_agent: CodeAgent,
        runner: Runner,
        vcs: VCS,
        queue: LocalJobQueue | None = None,
        clock: Clock | None = None,
        max_ci_rounds: int | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.workspace_manager = workspace_manager
        self.code_agent = code_agent
        self.runner = runner
        self.vcs = vcs
        self.queue = queue if queue is not None else LocalJobQueue()
        self.clock = clock if clock is not None else orchestrator.clock
        self.max_ci_rounds = max_ci_rounds

    def enqueue_approved_project(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildJob:
        """Verify project is in BUILDING state, initialize workspace, and enqueue BuildJob."""
        raise NotImplementedError

    def run_job(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildReport:
        """Run or resume the build loop for project_id until DELIVERED, FAILED, or PAUSED_QUOTA."""
        raise NotImplementedError

    def run_next(
        self,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildReport | None:
        """Run the next runnable job in the local queue, or return None if queue is empty."""
        raise NotImplementedError
