"""Local build worker, job queue, and write-test-fix loop (Section 7 Phase 2, INV-09, INV-13, INV-15, INV-17)."""

import difflib
import importlib.util
import json
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_factory.adapters.agy_cli_adapter import (
    build_sanitized_cli_env,
    sanitize_agent_summary,
)
from agent_factory.adapters.model_resolver import ACTIVITY_MESSAGES
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.agents.quality_auditor import (
    DebateRoundRecord,
    ParentAuditor,
    QualityAuditReport,
    build_quality_markdown_report,
)
from agent_factory.config import (
    FactorySettings,
    build_workspace_pytest_env,
    parse_positive_timeout,
    workspace_pytest_timeout_args,
)
from agent_factory.domain.events import Event, EventType, create_event
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    BuildReport,
    Plan,
    validate_project_id,
)
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.clock import Clock
from agent_factory.ports.code_agent import CodeAgent, CodeAgentError, QuotaExhaustedError
from agent_factory.ports.publisher import RegistryPublisher, RegistryPublishError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.ports.vcs import VCS
from agent_factory.runtime_runner import LocalAppRunner, detect_workspace_app, run_http_smoke_test
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import (
    SandboxViolationError,
    TestLockSnapshot,
    WorkspaceManager,
    compute_test_lock,
    ensure_workspace_favicon,
    ensure_workspace_pytest_ini,
    is_python_cache_path,
    read_workspace_git_diff,
    read_workspace_git_head_sha,
    verify_and_restore_test_lock,
    verify_isolated_git_root,
    workspace_nonempty_test_files,
    write_workspace_manifest,
)

_PYTEST_HEADER_PREFIXES: tuple[str, ...] = (
    "platform ",
    "rootdir:",
    "configfile:",
    "inifile:",
    "plugins:",
    "cachedir:",
    "collected ",
    "collecting ...",
    "testpaths:",
)
_PYTEST_SECTION_BANNER_RE = re.compile(
    r"^=+\s*(?:test session starts|failures|errors|short test summary info|warnings summary)\s*=+$",
    re.IGNORECASE,
)
_PYTEST_TIMEOUT_HDR_RE = re.compile(
    r"^timeout(?:\s+(?:method|func_only))?:\s+(?:\d+(?:\.\d+)?s|thread|signal|true|false)$",
    re.IGNORECASE,
)


_PYTEST_PROGRESS_LINE_RE = re.compile(
    r"^(?:[.FsExX]+\s*\[\s*\d{1,3}%\s*\]|\.{2,})$"
)
_PYTEST_PROGRESS_EXTRACT_RE = re.compile(
    r"^(?:[^\s:]+\.py\s+)?([.FsExX]+)(?:\s*\[\s*\d{1,3}%\s*\])?$"
)
_PYTEST_SUMMARY_LINE_RE = re.compile(
    r"^(?:=+\s*)?(?:"
    r"(?:\d+\s+(?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?|reruns?)\s*(?:,\s*)?)+"
    r"|\bno tests ran\b"
    r")(?:\s+in\s+\d+(?:\.\d+)?s(?:\s*\([^)]*\))?)?(?:\s*=+)?$",
    re.IGNORECASE,
)
_PYTEST_COUNT_TOKEN_RE = re.compile(
    r"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed)\b",
    re.IGNORECASE,
)
_PYTEST_VERBOSE_NODE_RE = re.compile(
    r"::\S+\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)\b",
)


def _filter_pytest_diagnostic_lines(raw_text: str) -> list[str]:
    """Strip ANSI codes, dividers, section banners, progress dots, and pytest header lines."""
    ansi_free = re.sub(r"\x1b\[[0-9;]*m", "", raw_text)
    result: list[str] = []
    for raw in ansi_free.splitlines():
        ln = " ".join(raw.split())
        if not ln:
            continue
        if ln.lower().startswith(_PYTEST_HEADER_PREFIXES):
            continue
        if _PYTEST_TIMEOUT_HDR_RE.match(ln):
            continue
        if _PYTEST_PROGRESS_LINE_RE.match(ln):
            continue
        if re.fullmatch(r"[=\-_~*\s]+", ln) or _PYTEST_SECTION_BANNER_RE.match(ln):
            continue
        result.append(ln)
    return result


def _parse_pytest_counts(stdout: str, stderr: str) -> dict[str, int]:
    """Extract passed/failed/skipped/errors/xfailed counts from pytest stdout/stderr."""
    ansi_free = re.sub(r"\x1b\[[0-9;]*m", "", f"{stdout}\n{stderr}")
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "xfailed": 0}
    lines = [" ".join(raw.split()) for raw in ansi_free.splitlines() if raw.strip()]

    for ln in reversed(lines):
        if _PYTEST_SUMMARY_LINE_RE.match(ln):
            for num_str, kind in _PYTEST_COUNT_TOKEN_RE.findall(ln):
                low = kind.lower()
                key = "errors" if low.startswith("error") else low
                counts[key] += int(num_str)
            return counts

    # Fallback 1: verbose per-test outcome lines (e.g. "tests/test_app.py::test_1 PASSED [ 50%]")
    verbose_hits = _PYTEST_VERBOSE_NODE_RE.findall("\n".join(lines))
    if verbose_hits:
        mapping = {
            "PASSED": "passed",
            "FAILED": "failed",
            "SKIPPED": "skipped",
            "ERROR": "errors",
            "XFAIL": "xfailed",
        }
        for outcome in verbose_hits:
            counts[mapping[outcome]] += 1
        return counts

    # Fallback 2: quiet progress characters (e.g. "...... [100%]" or "tests/test_app.py ..F. [100%]")
    for ln in lines:
        prog_match = _PYTEST_PROGRESS_EXTRACT_RE.match(ln)
        if not prog_match:
            continue
        for ch in prog_match.group(1):
            if ch == ".":
                counts["passed"] += 1
            elif ch == "F":
                counts["failed"] += 1
            elif ch == "E":
                counts["errors"] += 1
            elif ch == "s":
                counts["skipped"] += 1
            elif ch in ("x", "X"):
                counts["xfailed"] += 1
    return counts


def _extract_failing_nodeids(stdout: str, stderr: str) -> list[str]:
    """Extract failing or skipped test identifiers from pytest stdout/stderr."""
    ansi_free = re.sub(r"\x1b\[[0-9;]*m", "", f"{stdout}\n{stderr}")
    found: list[str] = []
    for raw in ansi_free.splitlines():
        ln = " ".join(raw.split())
        m = re.match(r"^(?:FAILED|ERROR)\s+([^\s]+)", ln)
        if not m:
            m = re.search(r"^([^\s]+::[^\s]+)\s+(?:FAILED|SKIPPED|ERROR|XFAIL)\b", ln)
        if m:
            ident = m.group(1).strip()
            if ident and ident not in found:
                found.append(ident)
            if len(found) >= 10:
                break
    return found


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
        local_app_runner: LocalAppRunner | None = None,
        quality_auditor: ParentAuditor | None = None,
        quality_audit_enabled: bool | None = None,
        chats_dir: Path | None = None,
        max_quality_refinement_rounds: int = 2,
        workspace_pytest_timeout: float | None = None,
        settings: FactorySettings | None = None,
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
        self.local_app_runner = local_app_runner
        self.quality_auditor = quality_auditor
        self.quality_audit_enabled = quality_audit_enabled
        self.chats_dir: Path | None = Path(chats_dir).resolve() if chats_dir is not None else None
        self.max_quality_refinement_rounds = max(0, min(2, int(max_quality_refinement_rounds)))
        if workspace_pytest_timeout is not None:
            self.workspace_pytest_timeout = parse_positive_timeout(
                workspace_pytest_timeout,
                env_var_name="workspace_pytest_timeout",
                default=180.0,
            )
        elif settings is not None:
            self.workspace_pytest_timeout = float(settings.workspace_pytest_timeout)
        else:
            self.workspace_pytest_timeout = FactorySettings().workspace_pytest_timeout
        if getattr(orchestrator, "build_worker", None) is None:
            orchestrator.build_worker = self

    @staticmethod
    def _extract_modification_context(events: Sequence[Event]) -> tuple[str | None, list[str]]:
        """Return latest modification instructions and optional target_files after the most recent PROJECT_DELIVERED."""
        last_delivered_idx = -1
        for idx, ev in enumerate(events):
            if ev.type == EventType.PROJECT_DELIVERED:
                last_delivered_idx = idx
        if last_delivered_idx < 0:
            return None, []
        instr: str | None = None
        target_files: list[str] = []
        for ev in reversed(events[last_delivered_idx + 1 :]):
            pl = ev.payload or {}
            if not target_files and isinstance(pl.get("target_files"), list):
                target_files = [str(x).strip() for x in pl["target_files"] if str(x).strip()]
            if instr is None:
                if ev.type == EventType.STATE_TRANSITIONED and pl.get("instructions"):
                    text = str(pl["instructions"]).strip()
                    if text:
                        instr = text
                elif ev.type == EventType.SPEC_UPDATED and pl.get("change_instructions"):
                    text = str(pl["change_instructions"]).strip()
                    if text:
                        instr = text
        return instr, target_files

    @staticmethod
    def _extract_modification_instructions(events: Sequence[Event]) -> str | None:
        """Return latest modification instructions emitted after the most recent PROJECT_DELIVERED."""
        instr, _ = BuildWorker._extract_modification_context(events)
        return instr

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
            if is_python_cache_path(norm):
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

    def _bound_redacted(self, text: str, limit: int = 220) -> str:
        """Normalize whitespace, redact secrets (INV-19), and truncate safely to limit chars."""
        redacted = self._redact(" ".join(text.split()))
        if len(redacted) > limit:
            return redacted[: limit - 3].rstrip() + "..."
        return redacted

    def _summarize_stagnation_cause(
        self,
        runner_result: object,
        failing_tests: Sequence[str],
    ) -> str:
        """Build a concise plain-Spanish explanation including the redacted primary pytest failure line."""
        raw_stdout = str(getattr(runner_result, "stdout", "") or "")
        raw_stderr = str(getattr(runner_result, "stderr", "") or "")
        combined = f"{raw_stderr}\n{raw_stdout}".strip()
        prefix = "Proceso detenido automáticamente tras 2 rondas consecutivas sin cambios"

        if getattr(runner_result, "timed_out", False):
            return f"{prefix}: las pruebas excedieron el tiempo límite."

        lines = _filter_pytest_diagnostic_lines(combined)
        filtered_combined = "\n".join(lines)

        failed_line = next(
            (ln for ln in lines if ln.startswith(("FAILED ", "ERROR "))),
            "",
        )
        error_colon_line = next(
            (
                ln
                for ln in lines
                if ln.startswith(("ERROR:", "pytest: error:"))
                and not ln.lower().startswith("error: usage:")
            ),
            "",
        )
        usage_error_line = next(
            (ln for ln in lines if ln.startswith("ERROR:")),
            "",
        )
        exc_line = next(
            (
                re.sub(r"^E\s+", "", ln).strip()
                for ln in lines
                if not ln.lower().startswith("error: usage:")
                and re.search(
                    r"(AssertionError|ModuleNotFoundError|ImportError|SyntaxError|TypeError|NameError|RuntimeError|Error:|Exception:)",
                    ln,
                )
            ),
            "",
        )

        mod_match = re.search(r"No module named ['\"]?([^'\"\r\n\s]+)['\"]?", filtered_combined)
        if mod_match:
            missing_mod = self._redact(mod_match.group(1))
            extra = f" - {self._bound_redacted(failed_line, 110)}" if failed_line else ""
            raw_cause = f"falta la librería externa '{missing_mod}' (ModuleNotFoundError{extra})"
            return f"{prefix} ({self._bound_redacted(raw_cause, 220)})."

        imp_match = re.search(r"ImportError:\s*([^\r\n]+)", filtered_combined)
        if imp_match:
            raw_imp = f"ImportError: {imp_match.group(1).strip()}"
            if failed_line and imp_match.group(1).strip() not in failed_line:
                raw_imp = f"{failed_line} - {raw_imp}"
            return f"{prefix} ({self._bound_redacted(raw_imp, 220)})."

        skip_count = 0
        skip_count_match = re.search(r"\b(\d+)\s+skipped\b", filtered_combined, re.IGNORECASE)
        if skip_count_match:
            try:
                skip_count = int(skip_count_match.group(1))
            except ValueError:
                pass
        elif hasattr(runner_result, "skipped") and isinstance(runner_result.skipped, int):
            skip_count = runner_result.skipped

        skip_match = re.search(
            r"(?:^|[\r\n])\s*(?:[^:\r\n]+:\d+:\s*)?SKIPPED\s*(?:\[\d+\]|\(\d+\))?[:\s]+(?:[^:\r\n]+:\d+[:\s]+)?([^\r\n]+)",
            filtered_combined,
        )
        if not skip_match:
            skip_match = re.search(r"SKIPPED\s*\(([^\)\r\n]+)\)", filtered_combined)

        skip_reason = ""
        if skip_match:
            raw_sr = skip_match.group(1).strip()
            raw_sr = re.sub(r"\s+in\s+\d+(?:\.\d+)?s\b.*$", "", raw_sr, flags=re.IGNORECASE).strip()
            skip_reason = raw_sr

        any_skipped_line = next((ln for ln in lines if ln.startswith("SKIPPED")), "")

        if (
            not failed_line
            and not error_colon_line
            and not exc_line
            and not usage_error_line
            and (skip_reason or skip_count > 0 or any_skipped_line)
        ):
            if skip_reason:
                raw_cause = f"pruebas omitidas [SKIPPED] por requerir herramientas externas ausentes: {skip_reason}"
            elif skip_count > 0:
                raw_cause = f"pruebas omitidas [SKIPPED] ({skip_count} omitidas) por requerir herramientas externas ausentes"
            else:
                raw_cause = f"pruebas omitidas [SKIPPED] por requerir herramientas externas ausentes: {any_skipped_line}"
            return f"{prefix} ({self._bound_redacted(raw_cause, 220)})."

        skip_suffix = ""
        if skip_reason:
            skip_suffix = f" (además pruebas omitidas [SKIPPED]: {skip_reason})"
        elif skip_count > 0:
            skip_suffix = f" (además {skip_count} pruebas omitidas [SKIPPED])"

        detail = ""
        if failed_line:
            if " - " not in failed_line and exc_line and exc_line not in failed_line:
                detail = f"{failed_line} - {exc_line}"
            else:
                detail = failed_line
            detail = f"{detail}{skip_suffix}"
        elif error_colon_line:
            detail = f"{error_colon_line}{skip_suffix}"
        elif exc_line:
            detail = f"{exc_line}{skip_suffix}"
        elif usage_error_line:
            detail = usage_error_line
        else:
            summary_line = next(
                (
                    ln
                    for ln in reversed(lines)
                    if re.search(
                        r"(?:\b\d+\s+(?:failed|errors?|skipped|xfailed|deselected)\b|\bno tests ran\b)",
                        ln,
                        re.IGNORECASE,
                    )
                ),
                "",
            )
            if summary_line:
                detail = summary_line
            elif failing_tests:
                detail = "pruebas pendientes: " + ", ".join(failing_tests[:5])
            elif lines:
                detail = " ".join(lines[-2:])

        if detail:
            return f"{prefix} ({self._bound_redacted(detail, 220)})."
        return f"{prefix} porque las pruebas continúan fallando."

    def _save_round_checkpoint(
        self,
        project_id: str,
        snapshot: TestLockSnapshot,
        build_round: int,
        files_written: Sequence[str],
        tests_passed: int,
        tests_total: int,
    ) -> None:
        """Persist local round checkpoint metadata inside workspace root via WorkspaceManager (INV-19, INV-22)."""
        payload = {
            "project_id": project_id,
            "round": int(build_round),
            "files_written": [self._redact(str(p)) for p in files_written],
            "tests_passed": int(tests_passed),
            "tests_total": int(tests_total),
            "tests_hash": snapshot.tests_hash,
            "updated_at": self.orchestrator._now().isoformat(),
        }
        try:
            self.workspace_manager.write_file(
                project_id,
                ".agent-factory/checkpoint.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except OSError:
            pass

    @staticmethod
    def _validate_workspace_links(ws_dir: Path) -> None:
        """Ensure src/ and tests/ do not escape workspace root via symlinks/junctions (INV-22)."""
        resolved_ws = ws_dir.resolve()
        for sub in ("src", "tests"):
            candidate = ws_dir / sub
            if candidate.is_symlink() or (
                candidate.exists() and not candidate.resolve().is_relative_to(resolved_ws)
            ):
                raise SandboxViolationError(
                    f"Workspace subdirectory '{sub}' escapes '{resolved_ws}': {candidate}"
                )

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
        if failed_state != ProjectState.BUILDING or not job.last_failure_output:
            job.last_failure_output = clean_reason
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

    def _lock_reused_tests(
        self,
        project_id: str,
        job: BuildJob,
        ws_dir: Path,
        reused_files: list[str],
        total_tests: int,
    ) -> None:
        """Seal SHA-256 test lock over existing tests and record TESTS_REUSED (INV-13)."""
        self._report_activity(
            "Reutilizando pruebas existentes...",
            agents_count=1,
            stage="BUILDING",
            detail=f"{len(reused_files)} archivo(s) de pruebas conservados",
            tests_passed=0,
            tests_total=total_tests,
        )
        job.lock_snapshot = compute_test_lock(ws_dir)
        self.orchestrator.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.TESTS_REUSED,
                payload={
                    "files": reused_files[:10],
                    "tests_hash": job.lock_snapshot.tests_hash,
                    "summary_short": (
                        f"Se reutilizaron {len(reused_files)} archivo(s) de pruebas existentes"
                    ),
                },
                at=self.orchestrator._now(),
            )
        )

    def _invoke_test_phase(
        self,
        project_id: str,
        job: BuildJob,
        ws_dir: Path,
        *,
        mod_instructions: str | None,
    ) -> BuildReport | None:
        """Run initial test generation or post-delivery test extension and seal SHA-256 lock (INV-13)."""
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

        target_tests = list(job.plan.acceptance_tests)
        agent_label = "generate_tests"
        if mod_instructions:
            agent_label = "update_tests"
            target_tests.append(
                AcceptanceTest(
                    id=f"MOD-{job.invocations_used + 1}",
                    description=mod_instructions,
                    type=AcceptanceTestType.E2E,
                )
            )
            self._report_activity(
                "Actualizando pruebas para los cambios...",
                agents_count=1,
                stage="BUILDING",
                detail="Ampliando pruebas antes de sellar el candado",
                tests_passed=0,
                tests_total=len(target_tests),
            )
        else:
            self._report_activity(
                ACTIVITY_MESSAGES["BUILDING_TESTS"],
                agents_count=1,
                stage="BUILDING",
                detail=f"{len(target_tests)} criterios de aceptación",
                tests_passed=0,
                tests_total=len(target_tests),
            )

        gen_t0 = time.monotonic()
        try:
            if mod_instructions:
                update_fn = getattr(self.code_agent, "update_tests", None)
                if callable(update_fn):
                    gen_result = update_fn(ws_dir, target_tests, mod_instructions)
                elif hasattr(self.code_agent, "_test_gen_steps"):
                    steps = getattr(self.code_agent, "_test_gen_steps", None)
                    if steps:
                        gen_result = self.code_agent.generate_tests(ws_dir, target_tests)
                    else:
                        reused = workspace_nonempty_test_files(ws_dir)
                        self._lock_reused_tests(
                            project_id, job, ws_dir, reused, len(target_tests)
                        )
                        return None
                elif workspace_nonempty_test_files(ws_dir):
                    reused = workspace_nonempty_test_files(ws_dir)
                    self._lock_reused_tests(
                        project_id, job, ws_dir, reused, len(target_tests)
                    )
                    return None
                else:
                    gen_result = self.code_agent.generate_tests(ws_dir, target_tests)
            else:
                gen_result = self.code_agent.generate_tests(ws_dir, target_tests)
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
            self.orchestrator.record_state_call(
                project_id,
                ProjectState.BUILDING,
                model=self._active_code_agent_model(),
                agent=agent_label,
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
        status = self.orchestrator.record_state_call(
            project_id,
            ProjectState.BUILDING,
            model=getattr(gen_result, "model", None) or self._active_code_agent_model(),
            agent=agent_label,
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
            p.is_file() and not is_python_cache_path(p.relative_to(ws_dir))
            for p in tests_dir.rglob("*")
        )
        if not has_tests:
            return self._fail_job(
                project_id,
                job,
                "Test generation produced zero test files in tests/ (INV-13)",
            )

        ensure_workspace_pytest_ini(self.workspace_manager, project_id)
        job.lock_snapshot = compute_test_lock(ws_dir)
        return None

    def _write_manifest(
        self,
        project_id: str,
        ws_dir: Path,
        job: BuildJob,
        *,
        repo_url: str | None = None,
        deploy_url: str | None = None,
    ) -> Path:
        """Write redacted portable .agent-factory/manifest.json inside ws_dir (INV-19, INV-22)."""
        cur_status = self.orchestrator.get_status(project_id)
        eff_repo = (
            repo_url
            if repo_url is not None
            else ((cur_status.links.repo if cur_status.links else None) or "")
        )
        eff_deploy = (
            deploy_url
            if deploy_url is not None
            else ((cur_status.links.deploy if cur_status.links else None) or "")
        )
        return write_workspace_manifest(
            ws_dir,
            {
                "schema_version": "1.0",
                "project_id": project_id,
                "title": cur_status.name or project_id,
                "app_type": detect_workspace_app(ws_dir).app_type,
                "objective": cur_status.objective or job.plan.summary,
                "plan_summary": job.plan.summary,
                "repo_url": eff_repo,
                "deploy_url": eff_deploy,
                "updated_at": self.orchestrator._now().isoformat(),
            },
            extra_secrets=self.secrets_to_redact,
        )

    _SNAPSHOT_SKIP_DIRS: frozenset[str] = frozenset(
        {
            ".git",
            ".agent-factory",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
        }
    )

    def _resolve_quality_auditor(self) -> ParentAuditor | None:
        if self.quality_audit_enabled is False:
            return None
        if self.quality_auditor is not None:
            return self.quality_auditor
        llm = getattr(self.orchestrator, "llm", None)
        if llm is None:
            return ParentAuditor(None) if self.quality_audit_enabled is True else None
        if (
            self.quality_audit_enabled is None
            and llm.__class__.__name__ == "FakeLLM"
            and not getattr(llm, "_responses", None)
        ):
            return None
        return ParentAuditor(llm)

    def _should_persist_audit_history(self) -> bool:
        if self.chats_dir is not None:
            return True
        db_path = getattr(self.orchestrator.store, "db_path", None)
        return bool(db_path and str(db_path) != ":memory:")

    @classmethod
    def _snapshot_workspace_files(cls, ws_dir: Path) -> dict[str, bytes]:
        """Capture bounded snapshot of regular non-secret workspace files cleanly (INV-19, INV-22)."""
        ws_resolved = ws_dir.resolve()
        snap: dict[str, bytes] = {}
        for item in ws_resolved.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            try:
                resolved_item = item.resolve()
            except OSError:
                continue
            if not resolved_item.is_relative_to(ws_resolved):
                continue
            rel = resolved_item.relative_to(ws_resolved).as_posix()
            parts = rel.split("/")
            if any(p in cls._SNAPSHOT_SKIP_DIRS for p in parts):
                continue
            if parts[-1] == ".env" or parts[-1].startswith(".env."):
                continue
            try:
                snap[rel] = resolved_item.read_bytes()
            except OSError:
                continue
        return snap

    def _commit_git_checkpoint(
        self,
        project_id: str,
        ws_dir: Path,
        message: str,
    ) -> str | None:
        """Create an isolated Git checkpoint commit when using real Runner + Git repo."""
        if self.runner.__class__.__name__ == "FakeRunner" or not (ws_dir / ".git").exists():
            return read_workspace_git_head_sha(ws_dir, runner=self.runner)
        try:
            ws_resolved = verify_isolated_git_root(ws_dir, self.runner)
        except (SandboxViolationError, ValueError, OSError):
            return None
        clean_env = build_sanitized_cli_env()
        self.runner.run(["git", "add", "-A"], cwd=ws_resolved, timeout_seconds=10.0, env=clean_env)
        diff_res = self.runner.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=ws_resolved,
            timeout_seconds=10.0,
            env=clean_env,
        )
        head_sha = read_workspace_git_head_sha(ws_resolved, runner=self.runner)
        if diff_res.exit_code != 0 or not head_sha:
            allow_empty = ["--allow-empty"] if (not head_sha and diff_res.exit_code == 0) else []
            self.runner.run(
                [
                    "git",
                    "-c",
                    "user.name=AgentFactory",
                    "-c",
                    "user.email=agent@factory.local",
                    "commit",
                    *allow_empty,
                    "--no-verify",
                    "-m",
                    f"chore({project_id}): {message}",
                ],
                cwd=ws_resolved,
                timeout_seconds=10.0,
                env=clean_env,
            )
        return read_workspace_git_head_sha(ws_resolved, runner=self.runner)

    def _restore_quality_checkpoint(
        self,
        ws_dir: Path,
        *,
        commit_sha: str | None,
        snapshot: Mapping[str, bytes],
        lock_snapshot: TestLockSnapshot,
    ) -> None:
        """Restore workspace cleanly to verified Git commit or contained file snapshot (INV-13, INV-22)."""
        ws_resolved = ws_dir.resolve()
        if (
            commit_sha
            and self.runner.__class__.__name__ != "FakeRunner"
            and (ws_resolved / ".git").exists()
        ):
            try:
                verified_ws = verify_isolated_git_root(ws_resolved, self.runner)
                clean_env = build_sanitized_cli_env()
                self.runner.run(
                    ["git", "reset", "--hard", commit_sha],
                    cwd=verified_ws,
                    timeout_seconds=10.0,
                    env=clean_env,
                )
                self.runner.run(
                    ["git", "clean", "-fd", "-e", ".agent-factory"],
                    cwd=verified_ws,
                    timeout_seconds=10.0,
                    env=clean_env,
                )
                verify_and_restore_test_lock(ws_resolved, lock_snapshot)
                return
            except (SandboxViolationError, ValueError, OSError):
                pass

        for existing in list(ws_resolved.rglob("*")):
            if existing.is_symlink() or not existing.is_file():
                continue
            try:
                resolved_item = existing.resolve()
            except OSError:
                continue
            if not resolved_item.is_relative_to(ws_resolved):
                continue
            rel = resolved_item.relative_to(ws_resolved).as_posix()
            parts = rel.split("/")
            if any(p in self._SNAPSHOT_SKIP_DIRS for p in parts):
                continue
            if parts[-1] == ".env" or parts[-1].startswith(".env."):
                continue
            if rel not in snapshot:
                try:
                    resolved_item.unlink(missing_ok=True)
                except OSError:
                    pass

        for rel, data in snapshot.items():
            target = (ws_resolved / rel).resolve()
            if not target.is_relative_to(ws_resolved) or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        verify_and_restore_test_lock(ws_resolved, lock_snapshot)

    def _run_local_pytest(
        self,
        project_id: str,
        job: BuildJob,
        ws_dir: Path,
        *,
        build_round: int,
    ) -> tuple[bool, int, int, list[str], CommandResult, float]:
        """Run pytest inside ws_dir, parse test counts, update job results, and emit TESTS_UPDATED."""
        pytest_ini_path = self.workspace_manager.resolve_safe_path(project_id, "pytest.ini")
        pytest_prefix: list[str] = ["pytest"]
        if isinstance(self.runner, SubprocessRunner):
            resolved_pytest = resolve_executable("pytest")
            pytest_prefix = [resolved_pytest] if resolved_pytest else [sys.executable, "-m", "pytest"]
        pytest_cmd = [
            *pytest_prefix,
            "-v",
            "--tb=short",
            "-ra",
            "-o",
            "addopts=",
            *workspace_pytest_timeout_args(),
            *(["-c", str(pytest_ini_path)] if pytest_ini_path.is_file() else []),
            f"--rootdir={ws_dir}",
        ]
        verify_t0 = time.monotonic()
        runner_result = self.runner.run(
            command=pytest_cmd,
            cwd=ws_dir,
            timeout_seconds=self.workspace_pytest_timeout,
            env=build_workspace_pytest_env(),
        )
        local_test_duration = round(max(0.0, time.monotonic() - verify_t0), 3)
        required_test_ids = [at.id for at in job.plan.acceptance_tests]
        parsed_counts = _parse_pytest_counts(runner_result.stdout, runner_result.stderr)
        no_skipped_or_errors = (
            parsed_counts["failed"] == 0
            and parsed_counts["errors"] == 0
            and parsed_counts["skipped"] == 0
            and parsed_counts["xfailed"] == 0
        )

        tests_passed_count = 0
        tests_total_count = len(job.plan.acceptance_tests)
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
            has_all_plan_tests = all(
                runner_result.test_results.get(tid) is True for tid in required_test_ids
            )
            all_reported_tests_passed = all(runner_result.test_results.values())
            local_tests_passed = (
                runner_result.exit_code == 0
                and has_all_plan_tests
                and all_reported_tests_passed
                and no_skipped_or_errors
            )
        else:
            passed = parsed_counts["passed"]
            failed = (
                parsed_counts["failed"]
                + parsed_counts["errors"]
                + parsed_counts["skipped"]
                + parsed_counts["xfailed"]
            )
            total = passed + failed
            local_tests_passed = (
                runner_result.exit_code == 0 and passed > 0 and no_skipped_or_errors
            )
            if total > 0:
                tests_passed_count = passed
                tests_total_count = total
                if local_tests_passed:
                    job.test_results = (
                        {tid: True for tid in required_test_ids}
                        if required_test_ids
                        else {f"test_{idx}": True for idx in range(1, passed + 1)}
                    )
                    failing_tests = []
                else:
                    failing_tests = _extract_failing_nodeids(
                        runner_result.stdout, runner_result.stderr
                    )
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
            else:
                failing_tests = []

        return (
            local_tests_passed,
            tests_passed_count,
            tests_total_count,
            failing_tests,
            runner_result,
            verify_t0,
        )

    def _run_local_smoke_test(
        self,
        project_id: str,
        ws_dir: Path,
        *,
        build_round: int,
        max_code_rounds: int,
        tests_passed_count: int,
        tests_total_count: int,
    ) -> tuple[bool, str | None, tuple[str, ...], tuple[str, ...]]:
        """Run HTTP + optional Playwright browser smoke checks, returning (passed, redacted_error, failed_resources, browser_findings)."""
        if not (
            (self.local_app_runner is not None or isinstance(self.runner, SubprocessRunner))
            and detect_workspace_app(ws_dir).app_type in ("static", "python_local")
        ):
            return True, None, (), ()

        self._report_activity(
            "Verificando arranque local (HTTP Smoke Test)...",
            agents_count=1,
            stage="VERIFYING",
            detail=f"Ronda {build_round} de hasta {max_code_rounds}",
            tests_passed=tests_passed_count,
            tests_total=tests_total_count,
            build_round=build_round,
            max_rounds=max_code_rounds,
        )
        smoke_runner = (
            self.local_app_runner
            if self.local_app_runner is not None
            else LocalAppRunner(
                workspace_manager=self.workspace_manager,
                secrets_to_redact=self.secrets_to_redact,
            )
        )
        smoke_res = run_http_smoke_test(smoke_runner, project_id, ws_dir)
        browser_findings = tuple(
            self._redact(item)
            for item in (getattr(smoke_res, "browser_findings", ()) or ())
            if str(item or "").strip()
        )
        if smoke_res.passed:
            return True, None, (), browser_findings
        return False, self._redact(smoke_res.summary), tuple(smoke_res.failed_resources), browser_findings

    def _compute_round_diff_text(
        self,
        ws_dir: Path,
        *,
        pre_snapshot: Mapping[str, bytes],
        pre_sha: str | None = None,
    ) -> str:
        """Compute bounded, secret-redacted diff for AdversarialCritic review across Git or snapshot workspaces."""
        if (ws_dir / ".git").exists() and self.runner.__class__.__name__ != "FakeRunner":
            if pre_sha:
                try:
                    ws_resolved = verify_isolated_git_root(ws_dir, self.runner)
                    clean_env = build_sanitized_cli_env()
                    diff_res = self.runner.run(
                        ["git", "diff", pre_sha, "--no-color"],
                        cwd=ws_resolved,
                        timeout_seconds=15.0,
                        env=clean_env,
                    )
                    if diff_res.exit_code == 0 and diff_res.stdout.strip():
                        redacted = self._redact(diff_res.stdout.strip())
                        return redacted[:24000]
                except (CommandTimeoutError, SandboxViolationError, OSError, ValueError):
                    pass
            has_git, _, git_diff = read_workspace_git_diff(
                ws_dir,
                self.runner,
                extra_secrets=self.secrets_to_redact,
                max_chars=24000,
            )
            if has_git and git_diff and git_diff != "Sin diferencias registradas.":
                return git_diff

        cur_snapshot = self._snapshot_workspace_files(ws_dir)
        all_paths = sorted(set(pre_snapshot) | set(cur_snapshot))
        chunks: list[str] = []
        for rel in all_paths:
            old_raw = pre_snapshot.get(rel)
            new_raw = cur_snapshot.get(rel)
            if old_raw == new_raw:
                continue
            old_lines = (
                old_raw.decode("utf-8", errors="replace").splitlines(keepends=True)
                if old_raw is not None
                else []
            )
            new_lines = (
                new_raw.decode("utf-8", errors="replace").splitlines(keepends=True)
                if new_raw is not None
                else []
            )
            udiff = "".join(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            if udiff.strip():
                chunks.append(self._redact(udiff.strip()))
        combined = "\n\n".join(chunks).strip() or "Sin diferencias detectadas en archivos."
        return combined[:24000]

    def _run_quality_audit_and_refinement(
        self,
        project_id: str,
        job: BuildJob,
        ws_dir: Path,
        *,
        build_round: int,
        max_code_rounds: int,
        effective_plan: Plan,
        tests_passed_count: int,
        tests_total_count: int,
        initial_report: QualityAuditReport | None = None,
        auditor_override: ParentAuditor | None = None,
        max_ref_rounds_override: int | None = None,
        skip_pytest_if_no_tests: bool = False,
        force_polish: bool = False,
        browser_smoke_findings: Sequence[str] = (),
    ) -> tuple[QualityAuditReport | None, int, int, bool, float]:
        """Run 2-agent Adversarial Debate (Critic vs. Builder) + bounded 2-round refinement with anti-regression checkpointing."""
        auditor = auditor_override if auditor_override is not None else self._resolve_quality_auditor()
        if auditor is None:
            return None, tests_passed_count, tests_total_count, False, 0.0

        assert job.lock_snapshot is not None
        audit_t0 = time.monotonic()
        building_limit = (
            self.orchestrator.circuit_breaker.get_limit(ProjectState.BUILDING) or 15
        )
        max_ref_rounds = (
            max(0, min(2, int(max_ref_rounds_override)))
            if max_ref_rounds_override is not None
            else self.max_quality_refinement_rounds
        )

        def _record_critic_call(rep: QualityAuditReport, call_dur: float) -> None:
            if rep.source != "llm":
                return
            cur_calls = self.orchestrator.circuit_breaker.get_calls(
                project_id, ProjectState.BUILDING
            )
            if cur_calls < building_limit:
                self.orchestrator.record_state_call(
                    project_id,
                    ProjectState.BUILDING,
                    model=self._active_code_agent_model(),
                    agent="quality_auditor",
                    duration_seconds=call_dur,
                    summary_short=self._bound_redacted(rep.summary, 180),
                    outcome="ok" if rep.passed else "needs_refinement",
                    extra_secrets=self.secrets_to_redact,
                )

        def _invoke_initial_critic() -> QualityAuditReport:
            cur_status = self.orchestrator.get_status(project_id)
            spec = self.orchestrator.get_spec(project_id)
            self._report_activity(
                "Evaluando calidad integral (Crítico Adversario · 6 sub-auditores)...",
                agents_count=6,
                stage="VERIFYING",
                detail="Auditoría inicial",
                tests_passed=tests_passed_count,
                tests_total=tests_total_count,
                build_round=build_round,
                max_rounds=max_code_rounds,
            )
            call_t0 = time.monotonic()
            rep = auditor.audit_workspace(
                project_id,
                ws_dir,
                spec=spec,
                plan=job.plan,
                test_results=job.test_results,
                round_number=1,
                project_name=cur_status.name or project_id,
                challenge_for_polish=force_polish,
                browser_smoke_findings=browser_smoke_findings,
                extra_secrets=self.secrets_to_redact,
            )
            _record_critic_call(rep, round(max(0.0, time.monotonic() - call_t0), 3))
            return rep

        best_report = initial_report if initial_report is not None else _invoke_initial_critic()
        best_test_results = dict(job.test_results)
        best_tests_passed = tests_passed_count
        best_tests_total = tests_total_count
        best_snapshot = self._snapshot_workspace_files(ws_dir)
        best_sha = self._commit_git_checkpoint(
            project_id, ws_dir, f"quality checkpoint r0 ({best_report.global_score:.1f}%)"
        )
        restored_best = False
        debate_records: list[DebateRoundRecord] = list(best_report.debate_rounds)

        should_stop = (
            max_ref_rounds <= 0
            or (not force_polish and best_report.passed)
            or (force_polish and best_report.passed and best_report.global_score >= 100.0)
        )
        if should_stop:
            total_dur = round(max(0.0, time.monotonic() - audit_t0), 2)
            return best_report, best_tests_passed, best_tests_total, restored_best, total_dur

        for ref_round in range(1, max_ref_rounds + 1):
            cur_calls = self.orchestrator.circuit_breaker.get_calls(
                project_id, ProjectState.BUILDING
            )
            if cur_calls >= building_limit:
                break

            round_objections = auditor.critic.build_objections(
                ws_dir,
                best_report,
                challenge_for_polish=force_polish,
                extra_secrets=self.secrets_to_redact,
            )
            if force_polish and best_report.passed and not round_objections:
                break

            builder_response, surgical_instructions = auditor.respond_to_objections(
                project_id,
                ws_dir,
                best_report,
                round_objections,
                refinement_round=ref_round,
                max_rounds=max_ref_rounds,
                challenge_for_polish=force_polish,
                extra_secrets=self.secrets_to_redact,
            )
            score_before_round = best_report.global_score

            self._report_activity(
                f"Debate de calidad (ronda {ref_round}/{max_ref_rounds}): Crítico vs. Constructor...",
                agents_count=2,
                stage="BUILDING",
                detail=f"Puntaje previo: {score_before_round:.1f}%",
                tests_passed=best_tests_passed,
                tests_total=best_tests_total,
                build_round=build_round + ref_round,
                max_rounds=max_code_rounds,
            )
            ref_t0 = time.monotonic()
            try:
                impl_result = self.code_agent.implement_or_fix(
                    ws_dir,
                    effective_plan,
                    failure_output=surgical_instructions,
                    attempt=job.invocations_used + 1,
                )
            except (QuotaExhaustedError, CodeAgentError, CommandTimeoutError):
                self._restore_quality_checkpoint(
                    ws_dir,
                    commit_sha=best_sha,
                    snapshot=best_snapshot,
                    lock_snapshot=job.lock_snapshot,
                )
                job.test_results = dict(best_test_results)
                restored_best = True
                debate_records.append(
                    DebateRoundRecord(
                        round_number=ref_round,
                        score_before=score_before_round,
                        score_after=score_before_round,
                        objections=round_objections,
                        builder_response=builder_response,
                        builder_instructions=surgical_instructions,
                        resolved_count=0,
                        kept=False,
                        discard_reason="code_agent_error",
                    )
                )
                break

            ref_dur = round(max(0.0, time.monotonic() - ref_t0), 3)
            tampered = not verify_and_restore_test_lock(ws_dir, job.lock_snapshot)
            impl_files = self._sanitize_files_written(
                getattr(impl_result, "files_written", None), tests_only=False
            )
            self.orchestrator.record_state_call(
                project_id,
                ProjectState.BUILDING,
                model=getattr(impl_result, "model", None) or self._active_code_agent_model(),
                agent="implement_or_fix",
                duration_seconds=ref_dur,
                round_num=build_round + ref_round,
                max_rounds=max_code_rounds,
                files_written=impl_files,
                summary_short=self._summarize_short(getattr(impl_result, "summary", None)),
                outcome="reverted_tamper" if tampered else "ok",
                extra_secrets=self.secrets_to_redact,
            )
            job.invocations_used += 1

            if tampered:
                self._restore_quality_checkpoint(
                    ws_dir,
                    commit_sha=best_sha,
                    snapshot=best_snapshot,
                    lock_snapshot=job.lock_snapshot,
                )
                job.test_results = dict(best_test_results)
                restored_best = True
                debate_records.append(
                    DebateRoundRecord(
                        round_number=ref_round,
                        score_before=score_before_round,
                        score_after=score_before_round,
                        objections=round_objections,
                        builder_response=builder_response,
                        builder_instructions=surgical_instructions,
                        resolved_count=0,
                        kept=False,
                        discard_reason="test_lock_tampered",
                    )
                )
                continue

            if (
                skip_pytest_if_no_tests
                and self.runner.__class__.__name__ != "FakeRunner"
                and not workspace_nonempty_test_files(ws_dir)
            ):
                tests_ok = True
                cur_passed = best_tests_passed
                cur_total = best_tests_total
            else:
                try:
                    (
                        tests_ok,
                        cur_passed,
                        cur_total,
                        _failing,
                        _raw_res,
                        _t0,
                    ) = self._run_local_pytest(
                        project_id, job, ws_dir, build_round=build_round + ref_round
                    )
                except (CommandTimeoutError, OSError, RuntimeError, ValueError):
                    tests_ok = False
                    cur_passed = 0
                    cur_total = best_tests_total

            smoke_ok = False
            round_browser_findings: tuple[str, ...] = ()
            if tests_ok:
                cur_meta = self.orchestrator.get_status(project_id)
                ensure_workspace_favicon(
                    ws_dir,
                    cur_meta.name or project_id,
                    cur_meta.objective or job.plan.summary,
                )
                smoke_ok, _, _, round_browser_findings = self._run_local_smoke_test(
                    project_id,
                    ws_dir,
                    build_round=build_round + ref_round,
                    max_code_rounds=max_code_rounds,
                    tests_passed_count=cur_passed,
                    tests_total_count=cur_total,
                )

            if not tests_ok or not smoke_ok:
                self._restore_quality_checkpoint(
                    ws_dir,
                    commit_sha=best_sha,
                    snapshot=best_snapshot,
                    lock_snapshot=job.lock_snapshot,
                )
                job.test_results = dict(best_test_results)
                restored_best = True
                if best_tests_total > 0:
                    self.orchestrator.store.append(
                        create_event(
                            project_id=project_id,
                            event_type=EventType.TESTS_UPDATED,
                            payload={
                                "passed": best_tests_passed,
                                "failed": 0,
                                "total": best_tests_total,
                                "round": build_round + ref_round,
                                "restored_checkpoint": True,
                            },
                            at=self.orchestrator._now(),
                        )
                    )
                debate_records.append(
                    DebateRoundRecord(
                        round_number=ref_round,
                        score_before=score_before_round,
                        score_after=score_before_round,
                        objections=round_objections,
                        builder_response=builder_response,
                        builder_instructions=surgical_instructions,
                        resolved_count=0,
                        kept=False,
                        discard_reason="pytest_or_smoke_failed",
                    )
                )
                continue

            diff_text = self._compute_round_diff_text(
                ws_dir, pre_snapshot=best_snapshot, pre_sha=best_sha
            )
            cur_status = self.orchestrator.get_status(project_id)
            spec = self.orchestrator.get_spec(project_id)
            self._report_activity(
                "Crítico Adversario inspeccionando git diff...",
                agents_count=6,
                stage="VERIFYING",
                detail=f"Revisión diff ronda {ref_round}",
                tests_passed=cur_passed,
                tests_total=cur_total,
                build_round=build_round + ref_round,
                max_rounds=max_code_rounds,
            )
            diff_t0 = time.monotonic()
            new_report, resolved_count, diff_summary = auditor.review_refinement_diff(
                project_id,
                ws_dir,
                previous_report=best_report,
                prior_objections=round_objections,
                diff_text=diff_text,
                spec=spec,
                plan=job.plan,
                test_results=job.test_results,
                round_number=ref_round + 1,
                project_name=cur_status.name or project_id,
                challenge_for_polish=force_polish,
                browser_smoke_findings=round_browser_findings,
                extra_secrets=self.secrets_to_redact,
            )
            _record_critic_call(new_report, round(max(0.0, time.monotonic() - diff_t0), 3))

            improved_or_same = new_report.global_score >= best_report.global_score and (
                not best_report.passed or new_report.passed
            )
            if not improved_or_same:
                self._restore_quality_checkpoint(
                    ws_dir,
                    commit_sha=best_sha,
                    snapshot=best_snapshot,
                    lock_snapshot=job.lock_snapshot,
                )
                job.test_results = dict(best_test_results)
                restored_best = True
                debate_records.append(
                    DebateRoundRecord(
                        round_number=ref_round,
                        score_before=score_before_round,
                        score_after=new_report.global_score,
                        objections=round_objections,
                        builder_response=builder_response,
                        builder_instructions=surgical_instructions,
                        resolved_count=resolved_count,
                        kept=False,
                        diff_summary=diff_summary,
                        discard_reason="score_regression",
                    )
                )
                continue

            debate_records.append(
                DebateRoundRecord(
                    round_number=ref_round,
                    score_before=score_before_round,
                    score_after=new_report.global_score,
                    objections=round_objections,
                    builder_response=builder_response,
                    builder_instructions=surgical_instructions,
                    resolved_count=resolved_count,
                    kept=True,
                    diff_summary=diff_summary,
                )
            )
            best_report = new_report
            best_test_results = dict(job.test_results)
            best_tests_passed = cur_passed
            best_tests_total = cur_total
            best_snapshot = self._snapshot_workspace_files(ws_dir)
            best_sha = self._commit_git_checkpoint(
                project_id,
                ws_dir,
                f"quality checkpoint r{ref_round} ({best_report.global_score:.1f}%)",
            )
            restored_best = False
            if (not force_polish and best_report.passed) or (
                force_polish
                and best_report.passed
                and (
                    best_report.global_score >= 100.0
                    or (
                        best_report.tier_id == "excellence"
                        and not best_report.objections
                        and not best_report.findings
                    )
                )
            ):
                break

        best_report.debate_rounds = debate_records
        cur_meta_final = self.orchestrator.get_status(project_id)
        best_report.markdown_report = build_quality_markdown_report(
            best_report,
            project_name=cur_meta_final.name or project_id,
            restored_best_checkpoint=restored_best,
            extra_secrets=self.secrets_to_redact,
        )
        total_dur = round(max(0.0, time.monotonic() - audit_t0), 2)
        return best_report, best_tests_passed, best_tests_total, restored_best, total_dur

    def _publish_registry_best_effort(
        self,
        project_id: str,
        job: BuildJob,
    ) -> str | None:
        """Notify RegistryPublisher best-effort after PROJECT_DELIVERED and record failure events cleanly."""
        if self.registry_publisher is None:
            return None
        try:
            same_pub = (
                getattr(self.orchestrator, "publisher", None) is self.registry_publisher
            )
            if same_pub:
                delivered_status = self.orchestrator.get_status(
                    project_id, notify_publisher=False
                )
            else:
                delivered_status = self.orchestrator.get_status(project_id)
            events = self.orchestrator.store.get_events(project_id)
            self.registry_publisher.publish_project(delivered_status, events=events)
            if same_pub and hasattr(self.orchestrator, "mark_status_published"):
                self.orchestrator.mark_status_published(delivered_status)
            return None
        except (RegistryPublishError, OSError, RuntimeError, ValueError) as reg_exc:
            clean_reg_err = self._redact(str(reg_exc))
            registry_warning = f"Registry publish failed (best-effort): {clean_reg_err}"
            job.last_failure_output = registry_warning
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.REGISTRY_PUBLISH_FAILED,
                    payload={"reason": registry_warning},
                    at=self.orchestrator._now(),
                )
            )
            return registry_warning

    def _push_quality_improvement(
        self,
        project_id: str,
        ws_dir: Path,
        *,
        status_repo_url: str | None,
        status_deploy_url: str | None,
    ) -> tuple[str | None, str | None]:
        """Push verified post-refinement commit cleanly to origin or FakeVCS."""
        from agent_factory.adapters.github_vcs_adapter import build_git_push_env

        repo_url = status_repo_url
        deploy_url = status_deploy_url
        if self.vcs.__class__.__name__ == "FakeVCS":
            self.vcs.push_branch(project_id, ws_dir, branch="work")
            ci_res = self.vcs.get_ci_result(project_id, branch="work")
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.CI_ROUND_COMPLETED,
                    payload={
                        "ci_rounds": 1,
                        "summary": self._redact(ci_res.summary),
                        "duration_seconds": 0.0,
                    },
                    at=self.orchestrator._now(),
                )
            )
            merged_repo, merged_deploy = self.vcs.merge_and_deploy(project_id, branch="work")
            return repo_url or merged_repo, deploy_url or merged_deploy

        git_cfg = ws_dir / ".git" / "config"
        has_origin = False
        if git_cfg.is_file():
            try:
                has_origin = '[remote "origin"]' in git_cfg.read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                has_origin = False
        if has_origin and self.runner.__class__.__name__ != "FakeRunner":
            ws_resolved = verify_isolated_git_root(ws_dir, self.runner)
            clean_env = build_sanitized_cli_env()
            cur_branch = "main"
            head_file = ws_resolved / ".git" / "HEAD"
            if head_file.is_file():
                try:
                    htxt = head_file.read_text(encoding="utf-8", errors="ignore").strip()
                    if htxt.startswith("ref: refs/heads/"):
                        cur_branch = htxt[len("ref: refs/heads/") :].strip() or "main"
                except OSError:
                    pass
            gh_tok = str(getattr(self.vcs, "github_token", "") or "")
            repo_base = str(getattr(self.vcs, "repo_base_url", "") or "https://github.com")
            push_env, push_secrets = build_git_push_env(
                clean_env, gh_tok, repo_base_url=repo_base
            )
            push_res = self.runner.run(
                ["git", "push", "-u", "origin", cur_branch],
                cwd=ws_resolved,
                timeout_seconds=20.0,
                env=push_env,
            )
            if push_res.exit_code != 0:
                detail = redact_secrets(
                    (push_res.stderr or push_res.stdout or "git push failed").strip(),
                    extra_secrets=(*self.secrets_to_redact, *push_secrets),
                )
                raise RuntimeError(f"Error publicando mejora de calidad a origin: {detail}")
        return repo_url, deploy_url

    def improve_project_quality(
        self,
        project_id: str,
        *,
        auditor: ParentAuditor | None = None,
        chats_dir: Path | None = None,
        force_polish: bool = False,
    ) -> dict[str, Any]:
        """Audit project quality and run 2-agent adversarial debate until Producción (or Excelencia under force_polish)."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")
        status = self.orchestrator.get_status(project_id)
        if status.state != ProjectState.DELIVERED:
            raise IllegalTransition(status.state, ProjectState.CHANGE_REQUEST)

        if status.workspace_path and not self.workspace_manager.is_external_workspace(project_id):
            self.workspace_manager.register_external_workspace(project_id, status.workspace_path)
        ws_dir = self.orchestrator._resolve_project_workspace_dir(
            project_id, status=status, workspace_manager=self.workspace_manager
        )
        if ws_dir is None or not ws_dir.is_dir():
            raise ValueError(f"El workspace del proyecto '{project_id}' no existe.")

        eff_chats_dir = chats_dir if chats_dir is not None else self.chats_dir
        should_persist = bool(
            eff_chats_dir is not None
            or (
                getattr(self.orchestrator.store, "db_path", None)
                and str(getattr(self.orchestrator.store, "db_path", "")) != ":memory:"
            )
        )
        checkpoints = self.orchestrator.list_project_checkpoints(
            project_id, chats_dir=eff_chats_dir, extra_secrets=self.secrets_to_redact
        )
        cur_cp = next(
            (c for c in checkpoints if c.get("is_current")),
            checkpoints[0] if checkpoints else None,
        )
        prior_delivered = sum(1 for ev in events if ev.type == EventType.PROJECT_DELIVERED)
        cur_version = (cur_cp.get("version_label") if cur_cp else "") or f"v{max(1, prior_delivered)}"
        cur_sha = (cur_cp.get("commit_sha") if cur_cp else "") or (
            read_workspace_git_head_sha(ws_dir, runner=self.runner) or ""
        )

        eff_auditor = (
            auditor
            or self._resolve_quality_auditor()
            or ParentAuditor(getattr(self.orchestrator, "llm", None))
        )
        initial_test_results: dict[str, bool] = (
            {"suite": status.tests.failed == 0} if status.tests.total > 0 else {}
        )
        initial_browser_findings: tuple[str, ...] = ()
        has_playwright = (
            "playwright" in sys.modules or importlib.util.find_spec("playwright") is not None
        )
        if has_playwright and (
            self.local_app_runner is not None or isinstance(self.runner, SubprocessRunner)
        ):
            _, _, _, initial_browser_findings = self._run_local_smoke_test(
                project_id,
                ws_dir,
                build_round=1,
                max_code_rounds=1,
                tests_passed_count=status.tests.passed,
                tests_total_count=status.tests.total,
            )
        t0 = time.monotonic()
        initial_report = eff_auditor.audit_workspace(
            project_id,
            ws_dir,
            spec=self.orchestrator.get_spec(project_id),
            plan=self.orchestrator.get_plan(project_id),
            test_results=initial_test_results or None,
            round_number=1,
            project_name=status.name or project_id,
            version_label=cur_version,
            challenge_for_polish=force_polish,
            browser_smoke_findings=initial_browser_findings,
            extra_secrets=self.secrets_to_redact,
        )

        already_maxed = (
            (not force_polish and initial_report.passed)
            or (force_polish and initial_report.passed and initial_report.global_score >= 100.0)
        )
        if already_maxed:
            dur = round(max(0.0, time.monotonic() - t0), 2)
            if should_persist:
                self.orchestrator.record_automated_quality_audit(
                    project_id,
                    report=initial_report,
                    commit_sha=cur_sha,
                    version_label=cur_version,
                    duration_seconds=dur,
                    chats_dir=eff_chats_dir,
                    prompt_label=f"Verificación de calidad ({cur_version})",
                    extra_secrets=self.secrets_to_redact,
                )
            scorecard = initial_report.to_scorecard_dict(extra_secrets=self.secrets_to_redact)
            return {
                "ok": True,
                "project_id": project_id,
                "already_passed": True,
                "improved": False,
                "version_label": cur_version,
                "commit_sha": cur_sha,
                "scorecard": scorecard,
                "quality_scorecard": scorecard,
                "checkpoints": self.orchestrator.list_project_checkpoints(
                    project_id, chats_dir=eff_chats_dir, extra_secrets=self.secrets_to_redact
                ),
                "status": self.orchestrator.get_status(project_id),
            }

        existing_plan = self.orchestrator.get_plan(project_id)
        effective_plan = (
            existing_plan
            if existing_plan is not None
            else Plan(
                summary=status.objective or f"Mejora de calidad {status.name or project_id}",
                rationale="Refinamiento acotado de calidad mediante debate adversario de 2 agentes.",
                template_id="static-web",
                files=[],
                acceptance_tests=[],
                risks=[],
                deployment_target="local",
            )
        )
        already_passing_polish = bool(force_polish and initial_report.passed)
        reason_label = (
            f"Pulido con debate ({initial_report.tier_label} · {initial_report.global_score:.0f}%)"
            if already_passing_polish
            else f"Mejora de calidad ({initial_report.tier_label} · {initial_report.global_score:.0f}%)"
        )
        if not already_passing_polish:
            self.orchestrator.transition_for_quality_improvement(
                project_id,
                plan=effective_plan,
                reason=reason_label,
            )

        ensure_workspace_pytest_ini(self.workspace_manager, project_id)
        lock_snapshot = compute_test_lock(ws_dir)
        job = BuildJob(
            project_id=project_id,
            plan=effective_plan,
            state=JobState.RUNNING,
            lock_snapshot=lock_snapshot,
            test_results=dict(initial_test_results),
        )
        max_ref = max(1, min(2, int(self.max_quality_refinement_rounds or 2)))
        try:
            (
                best_report,
                _tests_passed_count,
                _tests_total_count,
                restored_best,
                quality_dur,
            ) = self._run_quality_audit_and_refinement(
                project_id,
                job,
                ws_dir,
                build_round=1,
                max_code_rounds=max_ref + 1,
                effective_plan=effective_plan,
                tests_passed_count=status.tests.passed,
                tests_total_count=status.tests.total,
                initial_report=initial_report,
                auditor_override=eff_auditor,
                max_ref_rounds_override=max_ref,
                skip_pytest_if_no_tests=True,
                force_polish=force_polish,
            )
            assert best_report is not None

            has_kept_round = any(r.kept for r in best_report.debate_rounds)
            if already_passing_polish and (not has_kept_round or not best_report.passed):
                cur_meta = self.orchestrator.get_status(project_id)
                best_report.markdown_report = build_quality_markdown_report(
                    best_report,
                    project_name=cur_meta.name or project_id,
                    version_label=cur_version,
                    restored_best_checkpoint=restored_best,
                    extra_secrets=self.secrets_to_redact,
                )
                scorecard = best_report.to_scorecard_dict(extra_secrets=self.secrets_to_redact)
                if should_persist:
                    self.orchestrator.record_automated_quality_audit(
                        project_id,
                        report=best_report,
                        commit_sha=cur_sha,
                        version_label=cur_version,
                        duration_seconds=quality_dur,
                        chats_dir=eff_chats_dir,
                        prompt_label=f"Debate de pulido sin cambios aplicados ({cur_version})",
                        extra_secrets=self.secrets_to_redact,
                    )
                return {
                    "ok": True,
                    "project_id": project_id,
                    "already_passed": True,
                    "improved": False,
                    "restored_best": restored_best,
                    "version_label": cur_version,
                    "commit_sha": cur_sha,
                    "scorecard": scorecard,
                    "quality_scorecard": scorecard,
                    "checkpoints": self.orchestrator.list_project_checkpoints(
                        project_id, chats_dir=eff_chats_dir, extra_secrets=self.secrets_to_redact
                    ),
                    "status": cur_meta,
                }

            if already_passing_polish:
                self.orchestrator.transition_for_quality_improvement(
                    project_id,
                    plan=effective_plan,
                    reason=reason_label,
                )

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
            if existing_plan is not None:
                self._write_manifest(project_id, ws_dir, job)
            self._commit_git_checkpoint(
                project_id,
                ws_dir,
                f"quality refined ({best_report.tier_label} {best_report.global_score:.1f}%)",
            )
            repo_url, deploy_url = self._push_quality_improvement(
                project_id,
                ws_dir,
                status_repo_url=status.links.repo if status.links else None,
                status_deploy_url=status.links.deploy if status.links else None,
            )

            next_version = f"v{prior_delivered + 1}"
            new_sha = read_workspace_git_head_sha(ws_dir, runner=self.runner) or cur_sha
            cur_meta = self.orchestrator.get_status(project_id)
            best_report.markdown_report = build_quality_markdown_report(
                best_report,
                project_name=cur_meta.name or project_id,
                version_label=next_version,
                restored_best_checkpoint=restored_best,
                extra_secrets=self.secrets_to_redact,
            )
            scorecard = best_report.to_scorecard_dict(extra_secrets=self.secrets_to_redact)
            summary_prefix = "Debate de pulido" if force_polish else "Mejora de calidad"
            summary_txt = (
                f"{summary_prefix} ({initial_report.tier_label} → {best_report.tier_label} · "
                f"{best_report.global_score:.0f}%)"
            )
            delivered_pl: dict[str, object] = {
                "repo_url": repo_url,
                "deploy_url": deploy_url,
                "duration_seconds": quality_dur,
                "version_label": next_version,
                "summary": summary_txt,
                "quality_scorecard": scorecard,
            }
            if new_sha:
                delivered_pl["commit_sha"] = new_sha
            self.orchestrator.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_DELIVERED,
                    payload=delivered_pl,
                    at=self.orchestrator._now(),
                )
            )
            if should_persist:
                prompt_lbl = (
                    f"Debate de calidad y pulido ({next_version})"
                    if best_report.debate_rounds
                    else f"Mejora automática hasta Producción ({next_version})"
                )
                self.orchestrator.record_automated_quality_audit(
                    project_id,
                    report=best_report,
                    commit_sha=new_sha or "",
                    version_label=next_version,
                    duration_seconds=quality_dur,
                    chats_dir=eff_chats_dir,
                    prompt_label=prompt_lbl,
                    extra_secrets=self.secrets_to_redact,
                )
            self._publish_registry_best_effort(project_id, job)
            job.state = JobState.COMPLETED
            return {
                "ok": True,
                "project_id": project_id,
                "already_passed": False,
                "improved": True,
                "restored_best": restored_best,
                "version_label": next_version,
                "commit_sha": new_sha,
                "scorecard": scorecard,
                "quality_scorecard": scorecard,
                "checkpoints": self.orchestrator.list_project_checkpoints(
                    project_id, chats_dir=eff_chats_dir, extra_secrets=self.secrets_to_redact
                ),
                "status": self.orchestrator.get_status(project_id),
            }
        except Exception as exc:
            cur_state = self.orchestrator.get_status(project_id).state
            if cur_state not in (ProjectState.FAILED, ProjectState.DELIVERED):
                self._fail_job(project_id, job, exc, failed_state=cur_state)
            raise

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
        if status.workspace_path and not self.workspace_manager.is_external_workspace(project_id):
            self.workspace_manager.register_external_workspace(project_id, status.workspace_path)

        mod_instructions, mod_target_files = self._extract_modification_context(
            self.orchestrator.store.get_events(project_id)
        )

        job = self.queue.get_job(project_id)
        if job is None:
            plan = self.orchestrator.get_plan(project_id)
            if plan is None:
                raise ValueError(f"No plan found for approved project '{project_id}'")
            job = self.queue.enqueue(project_id, plan)

        job.state = JobState.RUNNING

        # -------------------------------------------------------------
        # Step A: Test generation / extension + lock snapshot (INV-13)
        # -------------------------------------------------------------
        if job.lock_snapshot is None:
            if mod_instructions:
                ws_dir = self.workspace_manager.create_workspace(
                    project_id, template_files=None
                )
                self._validate_workspace_links(ws_dir)
                ensure_workspace_pytest_ini(self.workspace_manager, project_id)
                early_report = self._invoke_test_phase(
                    project_id, job, ws_dir, mod_instructions=mod_instructions
                )
                if early_report is not None:
                    return early_report
            else:
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
                self._validate_workspace_links(ws_dir)
                ensure_workspace_pytest_ini(self.workspace_manager, project_id)

                # Reuse valid existing tests on incremental retry/resume while locking them (INV-13)
                reused_test_files = workspace_nonempty_test_files(ws_dir)
                if reused_test_files:
                    self._lock_reused_tests(
                        project_id,
                        job,
                        ws_dir,
                        reused_test_files,
                        len(job.plan.acceptance_tests),
                    )
                else:
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

                    early_report = self._invoke_test_phase(
                        project_id, job, ws_dir, mod_instructions=None
                    )
                    if early_report is not None:
                        return early_report
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
            effective_plan = (
                job.plan.model_copy(
                    update={"summary": f"{job.plan.summary} | Cambio solicitado: {mod_instructions}"}
                )
                if mod_instructions
                else job.plan
            )
            effective_failure = job.last_failure_output
            if mod_instructions and not effective_failure and build_round == 1:
                target_hint = (
                    f"\nTarget files to inspect/update: {', '.join(mod_target_files)}"
                    if mod_target_files
                    else ""
                )
                effective_failure = (
                    "Post-delivery modification requested (preserve existing behavior and apply changes):\n"
                    f"{mod_instructions}{target_hint}"
                )
            elif mod_target_files and effective_failure:
                effective_failure = (
                    f"{effective_failure}\nTarget files to inspect/update: {', '.join(mod_target_files)}"
                )
            try:
                impl_result = self.code_agent.implement_or_fix(
                    ws_dir,
                    effective_plan,
                    failure_output=effective_failure,
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
            try:
                (
                    local_tests_passed,
                    tests_passed_count,
                    tests_total_count,
                    failing_tests,
                    runner_result,
                    verify_t0,
                ) = self._run_local_pytest(
                    project_id,
                    job,
                    ws_dir,
                    build_round=build_round,
                )
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as err:
                return self._fail_job(
                    project_id, job, err, failed_state=ProjectState.BUILDING
                )

            self._save_round_checkpoint(
                project_id,
                job.lock_snapshot,
                build_round,
                impl_files,
                tests_passed_count,
                tests_total_count,
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

            # Ensure clean favicon.svg exists before smoke check / publishing (prevents 404 in browser console)
            cur_meta = self.orchestrator.get_status(project_id)
            ensure_workspace_favicon(
                ws_dir,
                cur_meta.name or project_id,
                cur_meta.objective or job.plan.summary,
            )

            # Local HTTP Smoke Test before promoting to VERIFYING
            smoke_passed, smoke_err, smoke_failed_resources, smoke_browser_findings = (
                self._run_local_smoke_test(
                    project_id,
                    ws_dir,
                    build_round=build_round,
                    max_code_rounds=max_code_rounds,
                    tests_passed_count=tests_passed_count,
                    tests_total_count=tests_total_count,
                )
            )
            if not smoke_passed:
                assert smoke_err is not None
                job.last_failure_output = smoke_err
                smoke_sig = (-1, smoke_failed_resources, smoke_err)
                if not impl_files:
                    same_failure = last_failure_sig is None or smoke_sig == last_failure_sig
                    consecutive_noop_rounds = (
                        consecutive_noop_rounds + 1 if same_failure else 1
                    )
                    last_failure_sig = smoke_sig
                    if consecutive_noop_rounds >= 2:
                        return self._fail_job(
                            project_id,
                            job,
                            f"Proceso detenido automáticamente tras 2 rondas consecutivas sin cambios ({self._bound_redacted(smoke_err, limit=180)}).",
                            failed_state=ProjectState.BUILDING,
                        )
                else:
                    consecutive_noop_rounds = 0
                    last_failure_sig = smoke_sig
                continue

            # Parent Auditor + 6 Sub-Auditors + bounded 2-round quality refinement before delivery
            (
                quality_report,
                tests_passed_count,
                tests_total_count,
                quality_restored_best,
                quality_audit_dur,
            ) = self._run_quality_audit_and_refinement(
                project_id,
                job,
                ws_dir,
                build_round=build_round,
                max_code_rounds=max_code_rounds,
                effective_plan=effective_plan,
                tests_passed_count=tests_passed_count,
                tests_total_count=tests_total_count,
                browser_smoke_findings=smoke_browser_findings,
            )

            # Local tests, HTTP Smoke Test, and Quality Audit passed! Transition to VERIFYING
            consecutive_noop_rounds = 0
            last_failure_sig = None
            job.last_failure_output = ""
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
                # Write portable manifest before commit/push in Git
                self._write_manifest(project_id, ws_dir, job)
                # Push branch
                self.vcs.push_branch(project_id, ws_dir, branch="work")

                # Get CI result
                ci_result = self.vcs.get_ci_result(project_id, branch="work")
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as err:
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
                except (CommandTimeoutError, OSError, RuntimeError, ValueError) as err:
                    return self._fail_job(
                        project_id, job, err, failed_state=ProjectState.VERIFYING
                    )

                pub_duration = round(max(0.0, time.monotonic() - pub_t0), 3)
                prior_delivered = sum(
                    1
                    for ev in self.orchestrator.store.get_events(project_id)
                    if ev.type == EventType.PROJECT_DELIVERED
                )
                version_label = f"v{prior_delivered + 1}"
                commit_sha = read_workspace_git_head_sha(ws_dir, runner=self.runner)
                delivered_payload: dict[str, object] = {
                    "repo_url": repo_url,
                    "deploy_url": deploy_url,
                    "duration_seconds": pub_duration,
                    "version_label": version_label,
                }
                if commit_sha:
                    delivered_payload["commit_sha"] = commit_sha
                if quality_report is not None:
                    quality_report.markdown_report = build_quality_markdown_report(
                        quality_report,
                        project_name=cur_meta.name or project_id,
                        version_label=version_label,
                        restored_best_checkpoint=quality_restored_best,
                        extra_secrets=self.secrets_to_redact,
                    )
                    delivered_payload["quality_scorecard"] = quality_report.to_scorecard_dict(
                        extra_secrets=self.secrets_to_redact
                    )
                self.orchestrator.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PROJECT_DELIVERED,
                        payload=delivered_payload,
                        at=self.orchestrator._now(),
                    )
                )
                if quality_report is not None and self._should_persist_audit_history():
                    self.orchestrator.record_automated_quality_audit(
                        project_id,
                        report=quality_report,
                        commit_sha=commit_sha or "",
                        version_label=version_label,
                        duration_seconds=quality_audit_dur,
                        chats_dir=self.chats_dir,
                        extra_secrets=self.secrets_to_redact,
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
                self._write_manifest(
                    project_id,
                    ws_dir,
                    job,
                    repo_url=repo_url,
                    deploy_url=deploy_url,
                )

                registry_warning = self._publish_registry_best_effort(project_id, job)

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
            ci_failed_checks = ", ".join(self._redact(c) for c in ci_result.failed_checks)
            job.last_failure_output = clean_ci_summary or ci_failed_checks or "CI checks failed"

    def run_next(
        self,
        template_files: Mapping[str, str] | None = None,
    ) -> BuildReport | None:
        """Run the next runnable job in the local queue, or return None if queue is empty."""
        job = self.queue.next_runnable_job()
        if job is None:
            return None
        return self.run_job(job.project_id, template_files=template_files)
