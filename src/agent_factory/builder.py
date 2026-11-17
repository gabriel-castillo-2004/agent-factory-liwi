"""Local build worker, job queue, and write-test-fix loop (Section 7 Phase 2, INV-09, INV-13, INV-15, INV-17)."""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from agent_factory.domain.events import EventType, create_event
from agent_factory.domain.models import BuildReport, Plan, validate_project_id
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.clock import Clock
from agent_factory.ports.code_agent import CodeAgent, CodeAgentError, QuotaExhaustedError
from agent_factory.ports.publisher import RegistryPublisher, RegistryPublishError
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.ports.vcs import VCS
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import (
    SandboxViolationError,
    TestLockSnapshot,
    WorkspaceManager,
    compute_test_lock,
    verify_and_restore_test_lock,
)


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
        if project_id in self._jobs:
            return self._jobs[project_id]
        job = BuildJob(project_id=project_id, plan=plan, state=JobState.QUEUED)
        self._jobs[project_id] = job
        self._order.append(project_id)
        return job

    def get_job(self, project_id: str) -> BuildJob | None:
        """Return the BuildJob for project_id, or None if not queued."""
        return self._jobs.get(project_id)

    def next_runnable_job(self) -> BuildJob | None:
        """Return the next job in QUEUED or PAUSED_QUOTA state."""
        for pid in self._order:
            job = self._jobs[pid]
            if job.state in (JobState.QUEUED, JobState.PAUSED_QUOTA):
                return job
        return None


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
        registry_publisher: RegistryPublisher | None = None,
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        self.orchestrator = orchestrator
        self.workspace_manager = workspace_manager
        self.code_agent = code_agent
        self.runner = runner
        self.vcs = vcs
        self.queue = queue if queue is not None else LocalJobQueue()
        self.clock = clock if clock is not None else orchestrator.clock
        if max_ci_rounds is not None:
            self.max_ci_rounds = max_ci_rounds
        else:
            self.max_ci_rounds = self.orchestrator.circuit_breaker._limits.get("ci_rounds", 3)
        self.registry_publisher = registry_publisher
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)

    def _redact(self, text: str) -> str:
        """Scrub configured secrets and credential patterns from diagnostic strings."""
        return redact_secrets(text, extra_secrets=self.secrets_to_redact)

    def _fail_job(
        self,
        project_id: str,
        job: BuildJob,
        reason: str,
        *,
        emit_event: bool = True,
    ) -> BuildReport:
        """Sanitize reason, emit optional PROJECT_FAILED, mark job FAILED, and build report."""
        clean_reason = self._redact(reason)
        if emit_event:
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_FAILED,
                    payload={"reason": clean_reason},
                    at=self.orchestrator._now(),
                )
            )
        job.state = JobState.FAILED
        report = BuildReport(
            invocations=job.invocations_used,
            test_results=job.test_results,
            ci_rounds=job.ci_rounds_used,
            failure_cause=clean_reason,
        )
        job.report = report
        return report

    def enqueue_approved_project(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildJob:
        """Verify project is in BUILDING state, initialize workspace, and enqueue BuildJob."""
        validate_project_id(project_id)
        status = self.orchestrator.get_status(project_id)
        if status.state != ProjectState.BUILDING:
            raise IllegalTransition(status.state, ProjectState.BUILDING)
        plan = self.orchestrator.get_plan(project_id)
        if plan is None:
            raise ValueError(f"No plan found for approved project '{project_id}'")
        self.workspace_manager.create_workspace(project_id, template_files=template_files)
        return self.queue.enqueue(project_id, plan)

    def run_job(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildReport:
        """Run or resume the build loop for project_id until DELIVERED, FAILED, or PAUSED_QUOTA."""
        validate_project_id(project_id)
        status = self.orchestrator.get_status(project_id)
        if status.state != ProjectState.BUILDING:
            raise IllegalTransition(status.state, ProjectState.BUILDING)

        job = self.queue.get_job(project_id)
        if job is None:
            plan = self.orchestrator.get_plan(project_id)
            if plan is None:
                raise ValueError(f"No plan found for approved project '{project_id}'")
            job = self.queue.enqueue(project_id, plan)

        job.state = JobState.RUNNING

        # -------------------------------------------------------------
        # Step A: Test generation (blind to Plan and production code)
        # -------------------------------------------------------------
        if job.lock_snapshot is None:
            # Check template files for contamination before creating workspace
            if template_files:
                for k in template_files:
                    norm = k.replace("\\", "/")
                    if norm.startswith("src/") or norm == "src":
                        raise SandboxViolationError(
                            f"Production file '{k}' found in template files prior to test generation (INV-13)"
                        )

            ws_dir = self.workspace_manager.create_workspace(
                project_id, template_files=template_files
            )

            # Check existing workspace src directory for contamination on disk
            src_dir = ws_dir / "src"
            if src_dir.is_dir() and any(p.is_file() for p in src_dir.rglob("*")):
                raise SandboxViolationError(
                    "Production code in src/ exists before test generation; potential test contamination (INV-13)"
                )

            # Check circuit breaker limit before invoking
            building_limit = (
                self.orchestrator.circuit_breaker.get_limit(ProjectState.BUILDING) or 15
            )
            calls = self.orchestrator.circuit_breaker.get_calls(
                project_id, ProjectState.BUILDING
            )
            if calls >= building_limit:
                status = self.orchestrator.record_state_call(
                    project_id, ProjectState.BUILDING
                )
                return self._fail_job(
                    project_id,
                    job,
                    status.failure_reason
                    or f"BUILDING invocation limit ({building_limit}) exceeded",
                    emit_event=False,
                )

            try:
                self.code_agent.generate_tests(ws_dir, job.plan.acceptance_tests)
            except QuotaExhaustedError as err:
                job.state = JobState.PAUSED_QUOTA
                job.pause_count += 1
                self.clock.sleep(err.retry_after_seconds)
                report = BuildReport(
                    invocations=job.invocations_used,
                    test_results=job.test_results,
                    ci_rounds=job.ci_rounds_used,
                    failure_cause=None,
                )
                job.report = report
                return report
            except (CodeAgentError, CommandTimeoutError) as err:
                # Fatal code agent error: record the invocation in CircuitBreaker and AGENT_INVOKED before failing
                self.orchestrator.record_state_call(project_id, ProjectState.BUILDING)
                job.invocations_used += 1
                return self._fail_job(project_id, job, str(err))

            # Successful test generation: record call in CircuitBreaker and AGENT_INVOKED
            status = self.orchestrator.record_state_call(
                project_id, ProjectState.BUILDING
            )
            job.invocations_used += 1

            if status.state == ProjectState.FAILED:
                return self._fail_job(
                    project_id,
                    job,
                    status.failure_reason or "Circuit breaker tripped",
                    emit_event=False,
                )

            tests_dir = ws_dir / "tests"
            has_tests = tests_dir.is_dir() and any(
                p.is_file() for p in tests_dir.rglob("*")
            )
            if not has_tests:
                return self._fail_job(
                    project_id,
                    job,
                    "Test generation produced zero test files in tests/ (INV-13)",
                )

            job.lock_snapshot = compute_test_lock(ws_dir)
        else:
            ws_dir = self.workspace_manager.create_workspace(
                project_id, template_files=None
            )

        # -------------------------------------------------------------
        # Step B: Local loop (write -> test -> fix -> verify CI)
        # -------------------------------------------------------------
        building_limit = (
            self.orchestrator.circuit_breaker.get_limit(ProjectState.BUILDING) or 15
        )

        while True:
            # Check invocation limit against CircuitBreaker
            calls = self.orchestrator.circuit_breaker.get_calls(
                project_id, ProjectState.BUILDING
            )
            if calls >= building_limit:
                status = self.orchestrator.record_state_call(
                    project_id, ProjectState.BUILDING
                )
                return self._fail_job(
                    project_id,
                    job,
                    status.failure_reason
                    or f"BUILDING invocation limit ({building_limit}) exceeded",
                    emit_event=False,
                )

            # Code agent writes / fixes code
            attempt_num = job.invocations_used + 1
            try:
                self.code_agent.implement_or_fix(
                    ws_dir,
                    job.plan,
                    failure_output=job.last_failure_output,
                    attempt=attempt_num,
                )
            except QuotaExhaustedError as err:
                job.state = JobState.PAUSED_QUOTA
                job.pause_count += 1
                self.clock.sleep(err.retry_after_seconds)
                report = BuildReport(
                    invocations=job.invocations_used,
                    test_results=job.test_results,
                    ci_rounds=job.ci_rounds_used,
                    failure_cause=None,
                )
                job.report = report
                return report
            except (CodeAgentError, CommandTimeoutError) as err:
                # Fatal code agent error: record the invocation in CircuitBreaker and AGENT_INVOKED before failing
                self.orchestrator.record_state_call(project_id, ProjectState.BUILDING)
                job.invocations_used += 1
                return self._fail_job(project_id, job, str(err))

            # Record successful agent invocation
            status = self.orchestrator.record_state_call(
                project_id, ProjectState.BUILDING
            )
            job.invocations_used += 1

            if status.state == ProjectState.FAILED:
                return self._fail_job(
                    project_id,
                    job,
                    status.failure_reason or "Circuit breaker tripped",
                    emit_event=False,
                )

            # Verify test lock
            if not verify_and_restore_test_lock(ws_dir, job.lock_snapshot):
                # Tampering detected and reverted: do not run tests, do not push
                job.last_failure_output = (
                    "Test or runner config tampered; changes reverted (INV-13)"
                )
                continue

            # Run local tests
            try:
                runner_result = self.runner.run(
                    command=["pytest", "-q"],
                    cwd=ws_dir,
                    timeout_seconds=30.0,
                )
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as err:
                return self._fail_job(project_id, job, str(err))

            # Update test results
            if runner_result.test_results:
                job.test_results = dict(runner_result.test_results)
                passed = sum(1 for v in runner_result.test_results.values() if v)
                failed = sum(1 for v in runner_result.test_results.values() if not v)
                total = len(runner_result.test_results)
                self.orchestrator.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.TESTS_UPDATED,
                        payload={"passed": passed, "failed": failed, "total": total},
                        at=self.orchestrator._now(),
                    )
                )

            required_test_ids = [at.id for at in job.plan.acceptance_tests]
            has_all_plan_tests = bool(runner_result.test_results) and all(
                runner_result.test_results.get(tid) is True for tid in required_test_ids
            )
            all_reported_tests_passed = bool(runner_result.test_results) and all(
                runner_result.test_results.values()
            )
            local_tests_passed = (
                runner_result.exit_code == 0
                and has_all_plan_tests
                and all_reported_tests_passed
            )

            if not local_tests_passed:
                output = runner_result.stdout
                if runner_result.stderr:
                    output = f"{output}\n{runner_result.stderr}".strip()
                job.last_failure_output = output
                continue

            # Local tests passed! Transition to VERIFYING for CI verification
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.STATE_TRANSITIONED,
                    payload={
                        "from": ProjectState.BUILDING.value,
                        "to": ProjectState.VERIFYING.value,
                    },
                    at=self.orchestrator._now(),
                )
            )

            try:
                # Push branch
                self.vcs.push_branch(project_id, ws_dir, branch="work")

                # Get CI result
                ci_result = self.vcs.get_ci_result(project_id, branch="work")
            except (OSError, RuntimeError, ValueError) as err:
                return self._fail_job(project_id, job, str(err))

            job.ci_rounds_used += 1
            clean_ci_summary = self._redact(ci_result.summary)
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.CI_ROUND_COMPLETED,
                    payload={
                        "ci_rounds": job.ci_rounds_used,
                        "summary": clean_ci_summary,
                    },
                    at=self.orchestrator._now(),
                )
            )

            if ci_result.passed:
                try:
                    repo_url, deploy_url = self.vcs.merge_and_deploy(
                        project_id, branch="work"
                    )
                except (OSError, RuntimeError, ValueError) as err:
                    return self._fail_job(project_id, job, str(err))

                self.orchestrator.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PROJECT_DELIVERED,
                        payload={"repo_url": repo_url, "deploy_url": deploy_url},
                        at=self.orchestrator._now(),
                    )
                )
                # Write PROJECT_PROMPT.md
                prompt_file = ws_dir / "PROJECT_PROMPT.md"
                prompt_content = (
                    f"# {job.plan.summary}\n\n"
                    f"## Objective\n{job.plan.summary}\n\n"
                    f"## Rationale\n{job.plan.rationale}\n\n"
                    f"## Deployment Target\n{job.plan.deployment_target}\n"
                )
                prompt_file.write_text(prompt_content, encoding="utf-8")

                registry_warning: str | None = None
                if self.registry_publisher is not None:
                    try:
                        delivered_status = self.orchestrator.get_status(project_id)
                        events = self.orchestrator.store.get_events(project_id)
                        self.registry_publisher.publish_project(
                            delivered_status,
                            events=events,
                        )
                    except (
                        RegistryPublishError,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as reg_exc:
                        clean_reg_err = self._redact(str(reg_exc))
                        registry_warning = (
                            f"Registry publish failed (best-effort): {clean_reg_err}"
                        )
                        self.orchestrator.store.append(
                            create_event(
                                project_id=project_id,
                                event_type=EventType.REGISTRY_PUBLISH_FAILED,
                                payload={"reason": registry_warning},
                                at=self.orchestrator._now(),
                            )
                        )

                job.state = JobState.COMPLETED
                report = BuildReport(
                    invocations=job.invocations_used,
                    test_results=job.test_results,
                    ci_rounds=job.ci_rounds_used,
                    failure_cause=registry_warning,
                )
                job.report = report
                return report

            # CI failed: check if CI round limit reached
            if job.ci_rounds_used >= self.max_ci_rounds:
                return self._fail_job(
                    project_id,
                    job,
                    f"CI round limit reached ({self.max_ci_rounds}): {clean_ci_summary}",
                )

            # Transition back to BUILDING for another fix attempt
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.STATE_TRANSITIONED,
                    payload={
                        "from": ProjectState.VERIFYING.value,
                        "to": ProjectState.BUILDING.value,
                    },
                    at=self.orchestrator._now(),
                )
            )
            job.last_failure_output = clean_ci_summary

    def run_next(
        self,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildReport | None:
        """Run the next runnable job in the local queue, or return None if queue is empty."""
        job = self.queue.next_runnable_job()
        if job is None:
            return None
        return self.run_job(job.project_id, template_files=template_files)
