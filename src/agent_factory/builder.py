"""Local build worker, job queue, and write-test-fix loop (Section 7 Phase 2, INV-09, INV-13, INV-15, INV-17)."""

import re
import threading
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from agent_factory.adapters.agy_cli_adapter import sanitize_agent_summary
from agent_factory.adapters.model_resolver import ACTIVITY_MESSAGES
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
    """Thread-safe local queue of approved build jobs supporting quota pause and resume."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, BuildJob] = {}
        self._order: list[str] = []

    def enqueue(self, project_id: str, plan: Plan) -> BuildJob:
        """Add or retrieve a BuildJob for project_id, replacing any prior FAILED job."""
        with self._lock:
            existing = self._jobs.get(project_id)
            if existing is not None and existing.state != JobState.FAILED:
                return existing
            job = BuildJob(project_id=project_id, plan=plan, state=JobState.QUEUED)
            self._jobs[project_id] = job
            if project_id not in self._order:
                self._order.append(project_id)
            return job

    def get_job(self, project_id: str) -> BuildJob | None:
        """Return the BuildJob for project_id, or None if not queued."""
        with self._lock:
            return self._jobs.get(project_id)

    def remove(self, project_id: str) -> None:
        """Remove project_id from the queue if present."""
        with self._lock:
            self._jobs.pop(project_id, None)
            self._order = [pid for pid in self._order if pid != project_id]

    def next_runnable_job(self) -> BuildJob | None:
        """Return the next job in QUEUED or PAUSED_QUOTA state."""
        with self._lock:
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

    def _active_code_agent_model(self) -> str | None:
        """Return the active model recorded by the code agent's ModelResolver, if available."""
        resolver = getattr(self.code_agent, "model_resolver", None)
        getter = getattr(resolver, "get_active_usage", None) if resolver is not None else None
        if not callable(getter):
            return None
        usage = getter()
        raw_model = usage.get("model") if isinstance(usage, dict) else None
        return self._redact(str(raw_model)) if raw_model else None

    def _sanitize_files_written(
        self,
        raw_files: Sequence[str] | None,
        *,
        tests_only: bool = False,
    ) -> list[str]:
        """Normalize, redact (INV-19), bound, and enforce INV-13 tests/ filter when needed."""
        if not raw_files:
            return []
        cleaned: list[str] = []
        for raw_path in raw_files:
            norm = self._redact(str(raw_path).strip().replace("\\", "/")).lstrip("./")
            if not norm or ".." in norm.split("/"):
                continue
            if tests_only and not (norm == "tests" or norm.startswith("tests/")):
                continue
            short = norm[:120]
            if short not in cleaned:
                cleaned.append(short)
            if len(cleaned) >= 10:
                break
        return cleaned

    def _summarize_short(self, raw_summary: object) -> str | None:
        """Strip JSON wrappers, redact, and compress agent summary text to a clean line <= 140 chars."""
        cleaned = sanitize_agent_summary(raw_summary)
        collapsed = " ".join(cleaned.split())
        if not collapsed:
            return None
        redacted = self._redact(collapsed)
        if len(redacted) > 140:
            return redacted[:137].rstrip() + "..."
        return redacted

    @staticmethod
    def _failure_signature(
        runner_result: object,
        failing_tests: Sequence[str],
    ) -> tuple[int, tuple[str, ...], str]:
        """Compute normalized test-failure signature ignoring volatile pytest timings/addresses."""
        raw_stdout = str(getattr(runner_result, "stdout", "") or "")
        raw_stderr = str(getattr(runner_result, "stderr", "") or "")
        combined = f"{raw_stdout}\n{raw_stderr}".strip()
        # Strip ANSI escapes, pytest duration suffixes (`in 0.41s`), hex pointers, and extra whitespace
        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", combined)
        cleaned = re.sub(r"\bin\s+\d+(?:\.\d+)?s\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", cleaned)
        cleaned = re.sub(r"[=-]{3,}", " ", cleaned)
        normalized = " ".join(cleaned.split())[-600:]
        exit_code = int(getattr(runner_result, "exit_code", 1))
        return exit_code, tuple(sorted(failing_tests)), normalized

    def _summarize_stagnation_cause(
        self,
        runner_result: object,
        failing_tests: Sequence[str],
    ) -> str:
        """Build a concise plain-Spanish explanation of why repeated no-op rounds stalled."""
        raw_stdout = str(getattr(runner_result, "stdout", "") or "")
        raw_stderr = str(getattr(runner_result, "stderr", "") or "")
        combined = f"{raw_stderr}\n{raw_stdout}".strip()
        prefix = "Proceso detenido automáticamente tras 2 rondas consecutivas sin cambios"

        if getattr(runner_result, "timed_out", False):
            return f"{prefix}: las pruebas excedieron el tiempo límite."

        mod_match = re.search(r"No module named ['\"]?([^'\"\r\n\s]+)['\"]?", combined)
        if mod_match:
            missing_mod = self._redact(mod_match.group(1))
            return (
                f"{prefix} mientras las pruebas siguen fallando: "
                f"falta la librería externa '{missing_mod}' en el entorno local."
            )

        imp_match = re.search(r"ImportError:\s*([^\r\n]+)", combined)
        if imp_match:
            imp_err = self._redact(imp_match.group(1).strip())[:100]
            return f"{prefix} mientras las pruebas siguen fallando: error de importación ({imp_err})."

        detail = ""
        for raw_line in combined.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.search(r"(AssertionError|SyntaxError|TypeError|NameError|Error:|FAILED |^E\s+)", line):
                detail = self._redact(" ".join(line.split()))[:110]
                break
        if not detail and failing_tests:
            detail = "pruebas pendientes: " + ", ".join(failing_tests[:5])
        if detail:
            return f"{prefix} mientras las pruebas siguen fallando ({detail})."
        return f"{prefix} porque las pruebas continúan fallando."

    def _report_activity(
        self,
        activity_summary: str,
        *,
        agents_count: int = 1,
        stage: str | None = None,
        detail: str | None = None,
        tests_passed: int | None = None,
        tests_total: int | None = None,
        build_round: int | None = None,
        max_rounds: int | None = None,
    ) -> None:
        resolver = getattr(self.code_agent, "model_resolver", None) or getattr(
            self.orchestrator.llm, "model_resolver", None
        )
        recorder = getattr(resolver, "record_activity", None) if resolver is not None else None
        if callable(recorder):
            recorder(
                activity_summary,
                agents_count=agents_count,
                stage=stage,
                detail=detail,
                tests_passed=tests_passed,
                tests_total=tests_total,
                build_round=build_round,
                max_rounds=max_rounds,
            )

    def _fail_job(
        self,
        project_id: str,
        job: BuildJob,
        reason: BaseException | str,
        *,
        emit_event: bool = True,
        failed_state: ProjectState | None = None,
    ) -> BuildReport:
        """Sanitize reason, emit optional PHASE_ERROR + PROJECT_FAILED, mark job FAILED, and build report."""
        raw_reason = str(reason) if str(reason).strip() else type(reason).__name__
        clean_reason = self._redact(raw_reason)
        if emit_event:
            target_state = failed_state or self.orchestrator.get_status(project_id).state
            self.orchestrator.record_phase_failure(
                project_id,
                failed_state=target_state if target_state != ProjectState.FAILED else ProjectState.BUILDING,
                error=reason,
                agent="build_worker",
                extra_secrets=self.secrets_to_redact,
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
                        msg = (
                            f"Production file '{k}' found in template files prior to test generation (INV-13)"
                        )
                        self.orchestrator.store.append(
                            create_event(
                                project_id=project_id,
                                event_type=EventType.PHASE_ERROR,
                                payload={
                                    "state": ProjectState.BUILDING.value,
                                    "agent": "build_worker",
                                    "error_type": "SandboxViolationError",
                                    "detail": self._redact(msg),
                                },
                                at=self.orchestrator._now(),
                            )
                        )
                        raise SandboxViolationError(msg)

            ws_dir = self.workspace_manager.create_workspace(
                project_id, template_files=template_files
            )

            # Check existing workspace src directory for contamination on disk
            src_dir = ws_dir / "src"
            if src_dir.is_dir() and any(p.is_file() for p in src_dir.rglob("*")):
                msg = (
                    "Production code in src/ exists before test generation; potential test contamination (INV-13)"
                )
                self.orchestrator.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PHASE_ERROR,
                        payload={
                            "state": ProjectState.BUILDING.value,
                            "agent": "build_worker",
                            "error_type": "SandboxViolationError",
                            "detail": self._redact(msg),
                        },
                        at=self.orchestrator._now(),
                    )
                )
                raise SandboxViolationError(msg)

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

            total_acceptance_tests = len(job.plan.acceptance_tests)
            self._report_activity(
                ACTIVITY_MESSAGES["BUILDING_TESTS"],
                agents_count=1,
                stage="BUILDING",
                detail=f"{total_acceptance_tests} criterios de aceptación",
                tests_passed=0,
                tests_total=total_acceptance_tests,
            )
            gen_t0 = time.monotonic()
            try:
                gen_result = self.code_agent.generate_tests(ws_dir, job.plan.acceptance_tests)
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
                gen_duration = round(max(0.0, time.monotonic() - gen_t0), 3)
                # Fatal code agent error: record the invocation in CircuitBreaker and AGENT_INVOKED before failing
                self.orchestrator.record_state_call(
                    project_id,
                    ProjectState.BUILDING,
                    model=self._active_code_agent_model(),
                    agent="generate_tests",
                    duration_seconds=gen_duration,
                    summary_short=self._summarize_short(err),
                    outcome="failed",
                    extra_secrets=self.secrets_to_redact,
                )
                job.invocations_used += 1
                return self._fail_job(
                    project_id, job, err, failed_state=ProjectState.BUILDING
                )

            gen_duration = round(max(0.0, time.monotonic() - gen_t0), 3)
            gen_files = self._sanitize_files_written(
                getattr(gen_result, "files_written", None), tests_only=True
            )
            gen_summary = self._summarize_short(getattr(gen_result, "summary", None))
            # Successful test generation: record call in CircuitBreaker and AGENT_INVOKED
            status = self.orchestrator.record_state_call(
                project_id,
                ProjectState.BUILDING,
                model=getattr(gen_result, "model", None) or self._active_code_agent_model(),
                agent="generate_tests",
                duration_seconds=gen_duration,
                files_written=gen_files,
                summary_short=gen_summary,
                outcome="ok",
                extra_secrets=self.secrets_to_redact,
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
        initial_calls = self.orchestrator.circuit_breaker.get_calls(
            project_id, ProjectState.BUILDING
        )
        max_code_rounds = max(1, building_limit - initial_calls)
        build_round = 0
        failing_tests: list[str] = []
        tests_passed_count = 0
        tests_total_count = len(job.plan.acceptance_tests)
        target_files_preview = ", ".join(f.path for f in job.plan.files[:3])
        consecutive_noop_rounds = 0
        last_failure_sig: tuple[int, tuple[str, ...], str] | None = None

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
            build_round += 1
            attempt_num = job.invocations_used + 1
            if not failing_tests:
                active_summary = ACTIVITY_MESSAGES["BUILDING_CODE"]
            elif len(failing_tests) == 1:
                active_summary = "Corrigiendo 1 prueba pendiente..."
            else:
                active_summary = f"Corrigiendo {len(failing_tests)} pruebas pendientes..."
            round_detail_parts = []
            if target_files_preview:
                round_detail_parts.append(f"Archivos: {target_files_preview}")
            round_detail_parts.append(f"Ronda {build_round} de hasta {max_code_rounds}")

            self._report_activity(
                active_summary,
                agents_count=1,
                stage="BUILDING",
                detail=" · ".join(round_detail_parts),
                tests_passed=tests_passed_count,
                tests_total=tests_total_count,
                build_round=build_round,
                max_rounds=max_code_rounds,
            )
            impl_t0 = time.monotonic()
            try:
                impl_result = self.code_agent.implement_or_fix(
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
                impl_duration = round(max(0.0, time.monotonic() - impl_t0), 3)
                # Fatal code agent error: record the invocation in CircuitBreaker and AGENT_INVOKED before failing
                self.orchestrator.record_state_call(
                    project_id,
                    ProjectState.BUILDING,
                    model=self._active_code_agent_model(),
                    agent="implement_or_fix",
                    duration_seconds=impl_duration,
                    round_num=build_round,
                    max_rounds=max_code_rounds,
                    failing_tests=failing_tests or None,
                    summary_short=self._summarize_short(err),
                    outcome="failed",
                    extra_secrets=self.secrets_to_redact,
                )
                job.invocations_used += 1
                return self._fail_job(
                    project_id, job, err, failed_state=ProjectState.BUILDING
                )

            impl_duration = round(max(0.0, time.monotonic() - impl_t0), 3)
            # Verify test lock BEFORE recording invocation so reverted tamper is flagged cleanly (INV-13)
            tampered = not verify_and_restore_test_lock(ws_dir, job.lock_snapshot)
            impl_files = self._sanitize_files_written(
                getattr(impl_result, "files_written", None), tests_only=False
            )
            impl_summary = (
                "Intentó modificar pruebas protegidas; cambios revertidos"
                if tampered
                else self._summarize_short(getattr(impl_result, "summary", None))
            )

            # Record agent invocation with round/files/outcome metadata
            status = self.orchestrator.record_state_call(
                project_id,
                ProjectState.BUILDING,
                model=getattr(impl_result, "model", None) or self._active_code_agent_model(),
                agent="implement_or_fix",
                duration_seconds=impl_duration,
                round_num=build_round,
                max_rounds=max_code_rounds,
                files_written=impl_files,
                summary_short=impl_summary,
                failing_tests=failing_tests or None,
                outcome="reverted_tamper" if tampered else "ok",
                extra_secrets=self.secrets_to_redact,
            )
            job.invocations_used += 1

            if status.state == ProjectState.FAILED:
                return self._fail_job(
                    project_id,
                    job,
                    status.failure_reason or "Circuit breaker tripped",
                    emit_event=False,
                )

            if tampered:
                # Tampering detected and reverted: do not run tests, do not push
                consecutive_noop_rounds = 0
                last_failure_sig = None
                job.last_failure_output = (
                    "Test or runner config tampered; changes reverted (INV-13)"
                )
                continue

            # Run local tests
            self._report_activity(
                ACTIVITY_MESSAGES["VERIFYING"],
                agents_count=1,
                stage="VERIFYING",
                detail=f"Ronda {build_round} de hasta {max_code_rounds}",
                tests_passed=tests_passed_count,
                tests_total=tests_total_count,
                build_round=build_round,
                max_rounds=max_code_rounds,
            )
            verify_t0 = time.monotonic()
            try:
                runner_result = self.runner.run(
                    command=["pytest", "-q"],
                    cwd=ws_dir,
                    timeout_seconds=30.0,
                )
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as err:
                return self._fail_job(
                    project_id, job, err, failed_state=ProjectState.BUILDING
                )

            local_test_duration = round(max(0.0, time.monotonic() - verify_t0), 3)
            required_test_ids = [at.id for at in job.plan.acceptance_tests]
            # Update test results
            if runner_result.test_results:
                job.test_results = dict(runner_result.test_results)
                passed = sum(1 for v in runner_result.test_results.values() if v)
                failed = sum(1 for v in runner_result.test_results.values() if not v)
                total = len(runner_result.test_results)
                tests_passed_count = passed
                tests_total_count = total
                failing_tests = [
                    tid
                    for tid in required_test_ids
                    if runner_result.test_results.get(tid) is not True
                ] + [
                    k
                    for k, v in runner_result.test_results.items()
                    if not v and k not in required_test_ids
                ]
                self.orchestrator.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.TESTS_UPDATED,
                        payload={
                            "passed": passed,
                            "failed": failed,
                            "total": total,
                            "round": build_round,
                            "duration_seconds": local_test_duration,
                        },
                        at=self.orchestrator._now(),
                    )
                )
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

                current_sig = self._failure_signature(runner_result, failing_tests)
                if not impl_files:
                    same_failure = (
                        last_failure_sig is None or current_sig == last_failure_sig
                    )
                    consecutive_noop_rounds = (
                        consecutive_noop_rounds + 1 if same_failure else 1
                    )
                    last_failure_sig = current_sig
                    if consecutive_noop_rounds >= 2:
                        stagnation_reason = self._summarize_stagnation_cause(
                            runner_result, failing_tests
                        )
                        return self._fail_job(
                            project_id,
                            job,
                            stagnation_reason,
                            failed_state=ProjectState.BUILDING,
                        )
                else:
                    consecutive_noop_rounds = 0
                    last_failure_sig = current_sig
                continue

            # Local tests passed! Reset stagnation tracking and transition to VERIFYING
            consecutive_noop_rounds = 0
            last_failure_sig = None
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
                return self._fail_job(
                    project_id, job, err, failed_state=ProjectState.VERIFYING
                )

            job.ci_rounds_used += 1
            ci_duration = round(max(0.0, time.monotonic() - verify_t0), 3)
            clean_ci_summary = self._redact(ci_result.summary)
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.CI_ROUND_COMPLETED,
                    payload={
                        "ci_rounds": job.ci_rounds_used,
                        "summary": clean_ci_summary,
                        "duration_seconds": ci_duration,
                    },
                    at=self.orchestrator._now(),
                )
            )

            if ci_result.passed:
                self._report_activity(
                    ACTIVITY_MESSAGES["PUBLISHING"],
                    agents_count=1,
                    stage="PUBLISHING",
                    detail=f"Ronda {build_round} de hasta {max_code_rounds}",
                    tests_passed=tests_passed_count,
                    tests_total=tests_total_count,
                    build_round=build_round,
                    max_rounds=max_code_rounds,
                )
                pub_t0 = time.monotonic()
                try:
                    repo_url, deploy_url = self.vcs.merge_and_deploy(
                        project_id, branch="work"
                    )
                except (OSError, RuntimeError, ValueError) as err:
                    return self._fail_job(
                        project_id, job, err, failed_state=ProjectState.VERIFYING
                    )

                pub_duration = round(max(0.0, time.monotonic() - pub_t0), 3)
                self.orchestrator.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PROJECT_DELIVERED,
                        payload={
                            "repo_url": repo_url,
                            "deploy_url": deploy_url,
                            "duration_seconds": pub_duration,
                        },
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
                    failed_state=ProjectState.VERIFYING,
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
