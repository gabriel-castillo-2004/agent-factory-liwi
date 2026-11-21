"""Local loopback HTTP dashboard server and JSON API for monitoring and controlling projects (INV-07, INV-19, INV-22)."""

from __future__ import annotations

import base64
import binascii
import email.parser
import email.policy
import http.server
import json
import logging
import threading
import urllib.parse
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory.adapters.github_vcs_adapter import VcsError
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import ApprovalEvent, ProjectStatus, validate_project_id
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import (
    ApprovalError,
    Orchestrator,
    synthesize_project_title,
)
from agent_factory.ports.code_agent import CodeAgentError
from agent_factory.ports.llm import LLMError
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.runtime_runner import LocalAppError, LocalAppRunner
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotService, _slugify
from agent_factory.workspace import (
    LOGO_MIME_BY_EXT,
    SandboxViolationError,
    WorkspaceManager,
    generate_project_logo_svg,
    validate_and_sanitize_logo_bytes,
    workspace_nonempty_test_files,
)

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"


class ProjectNotFoundError(LookupError):
    """Raised when a requested project does not exist in store or workspaces."""


class ProjectConflictError(RuntimeError):
    """Raised when an operation cannot run in the project's current state."""


def _scrub_value(val: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively redact secrets across JSON-serializable structures."""
    if isinstance(val, str):
        return redact_secrets(val, extra_secrets=secrets)
    if isinstance(val, list):
        return [_scrub_value(item, secrets) for item in val]
    if isinstance(val, dict):
        return {str(k): _scrub_value(v, secrets) for k, v in val.items()}
    return val


class DashboardController:
    """Thread-safe controller backing the local dashboard HTTP endpoints."""

    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        build_worker: BuildWorker | None = None,
        template_scaffold: TemplateScaffold | None = None,
        workspace_manager: WorkspaceManager | None = None,
        bot_service: TelegramBotService | None = None,
        model_resolver: Any = None,
        local_runner: LocalAppRunner | None = None,
        logos_dir: Path | None = None,
        chats_dir: Path | None = None,
        vcs: Any | None = None,
        runner: Runner | None = None,
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        self.orchestrator = orchestrator
        self.build_worker = build_worker
        self.template_scaffold = template_scaffold
        self.workspace_manager = workspace_manager
        self.bot_service = bot_service
        self.vcs = (
            vcs
            if vcs is not None
            else getattr(build_worker, "vcs", None) or getattr(orchestrator, "vcs", None)
        )
        self.runner = runner
        if model_resolver is not None:
            self.model_resolver = model_resolver
        else:
            self.model_resolver = getattr(orchestrator.llm, "model_resolver", None) or getattr(
                getattr(build_worker, "code_agent", None), "model_resolver", None
            )
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        if logos_dir is not None:
            self.logos_dir = Path(logos_dir).resolve()
        else:
            db_path = getattr(orchestrator.store, "db_path", None)
            if db_path and str(db_path) != ":memory:":
                db_parent = Path(db_path).resolve().parent
                repo_data_dir = Path("data").resolve()
                self.logos_dir = (
                    (repo_data_dir / "logos").resolve()
                    if db_parent == repo_data_dir
                    else (db_parent / "logos").resolve()
                )
            else:
                self.logos_dir = Path("data/logos").resolve()

        if chats_dir is not None:
            self.chats_dir = Path(chats_dir).resolve()
        else:
            db_path = getattr(orchestrator.store, "db_path", None)
            if db_path and str(db_path) != ":memory:":
                db_parent = Path(db_path).resolve().parent
                repo_data_dir = Path("data").resolve()
                self.chats_dir = (
                    (repo_data_dir / "chats").resolve()
                    if db_parent == repo_data_dir
                    else (db_parent / "chats").resolve()
                )
            else:
                self.chats_dir = Path("data/chats").resolve()
        effective_ws_mgr = self._resolve_workspace_manager() or getattr(
            orchestrator, "workspace_manager", None
        )
        worker_local_runner = getattr(build_worker, "local_app_runner", None)
        if local_runner is not None:
            self.local_runner = local_runner
        elif isinstance(worker_local_runner, LocalAppRunner):
            self.local_runner = worker_local_runner
        else:
            self.local_runner = LocalAppRunner(
                workspace_manager=effective_ws_mgr,
                secrets_to_redact=self.secrets_to_redact,
            )
        self._lock = threading.RLock()
        self._active_projects: set[str] = set()
        self._threads: list[threading.Thread] = []

    def _get_live_status_info(self, running: bool) -> dict[str, Any]:
        """Return live active model, stage, activity summary/detail, test progress, and timer."""
        empty: dict[str, Any] = {
            "active_model": None,
            "active_stage": None,
            "activity_summary": None,
            "activity_detail": None,
            "agents_count": None,
            "step_started_at": None,
            "tests_passed": None,
            "tests_total": None,
            "build_round": None,
            "max_rounds": None,
        }
        if not running or self.model_resolver is None:
            return empty

        live: dict[str, Any] = {}
        live_fn = getattr(self.model_resolver, "get_live_status", None)
        if callable(live_fn):
            res = live_fn()
            if isinstance(res, dict):
                live = res
        else:
            usage_fn = getattr(self.model_resolver, "get_active_usage", None)
            if callable(usage_fn):
                res = usage_fn()
                if isinstance(res, dict):
                    live.update(res)
            act_fn = getattr(self.model_resolver, "get_active_activity", None)
            if callable(act_fn):
                res = act_fn()
                if isinstance(res, dict):
                    live.update(res)

        if not live:
            return empty

        def _scrub_opt(val: Any) -> str | None:
            return redact_secrets(str(val), extra_secrets=self.secrets_to_redact) if val else None

        def _opt_int(val: Any, *, allow_zero: bool) -> int | None:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return None
            iv = int(val)
            if iv < 0 or (not allow_zero and iv == 0):
                return None
            return iv

        return {
            "active_model": _scrub_opt(live.get("model")),
            "active_stage": _scrub_opt(live.get("stage")),
            "activity_summary": _scrub_opt(live.get("activity_summary")),
            "activity_detail": _scrub_opt(live.get("activity_detail")),
            "agents_count": _opt_int(live.get("agents_count"), allow_zero=False),
            "step_started_at": _scrub_opt(live.get("step_started_at")),
            "tests_passed": _opt_int(live.get("tests_passed"), allow_zero=True),
            "tests_total": _opt_int(live.get("tests_total"), allow_zero=True),
            "build_round": _opt_int(live.get("build_round"), allow_zero=False),
            "max_rounds": _opt_int(live.get("max_rounds"), allow_zero=False),
        }

    def is_busy(self, project_id: str) -> bool:
        """Return True if a background task is currently executing for project_id."""
        with self._lock:
            return project_id in self._active_projects

    def is_running(self, project_id: str) -> bool:
        """Return True if an active task thread is currently running for project_id."""
        with self._lock:
            return project_id in self._active_projects

    def mark_running(self, project_id: str) -> None:
        """Register project_id as having an active task running in memory."""
        with self._lock:
            self._active_projects.add(project_id)

    def mark_stopped(self, project_id: str) -> None:
        """Unregister project_id from active running tasks."""
        with self._lock:
            self._active_projects.discard(project_id)

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for all active background dashboard tasks to finish within timeout seconds."""
        deadline = datetime.now(UTC).timestamp() + timeout
        while True:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
                self._threads = alive
            if not alive:
                return True
            remaining = deadline - datetime.now(UTC).timestamp()
            if remaining <= 0:
                return False
            alive[0].join(timeout=min(remaining, 0.2))

    def _start_background(self, project_id: str, target: Callable[[], None]) -> bool:
        """Run target in a daemon thread if project_id does not already have a running job."""
        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            self._active_projects.add(project_id)

            def _runner() -> None:
                try:
                    target()
                finally:
                    with self._lock:
                        self._active_projects.discard(project_id)
                        no_active = not self._active_projects
                    if no_active and self.model_resolver is not None:
                        clear_fn = getattr(self.model_resolver, "clear_active_activity", None)
                        if callable(clear_fn):
                            clear_fn()

            t = threading.Thread(
                target=_runner,
                name=f"dashboard-{project_id}",
                daemon=True,
            )
            self._threads = [existing for existing in self._threads if existing.is_alive()]
            self._threads.append(t)
            t.start()
            return True

    def _notify_chat_best_effort(self, project_id: str, text: str) -> None:
        """Send best-effort notification to the Telegram chat associated with project_id."""
        if self.bot_service is None:
            return
        chat_id: int | None = None
        for cid, pid in list(self.bot_service.active_project_by_chat.items()):
            if pid == project_id:
                chat_id = cid
                break
        if chat_id is None and len(self.bot_service.config.allowed_chat_ids) == 1:
            chat_id = next(iter(self.bot_service.config.allowed_chat_ids))
        if chat_id is None:
            return
        try:
            self.bot_service.send_message(chat_id=chat_id, text=text)
        except (OSError, RuntimeError, ValueError):
            pass

    def _extract_last_error(
        self, status: ProjectStatus, events: Sequence[Event]
    ) -> str | None:
        if status.failure_reason:
            return redact_secrets(status.failure_reason, extra_secrets=self.secrets_to_redact)
        for ev in reversed(events):
            if ev.type == EventType.PROJECT_RETRIED:
                break
            if ev.type == EventType.PHASE_ERROR:
                detail = str((ev.payload or {}).get("detail", "")).strip()
                if detail:
                    return redact_secrets(detail, extra_secrets=self.secrets_to_redact)
            if ev.type == EventType.PROJECT_FAILED:
                reason = str((ev.payload or {}).get("reason", "")).strip()
                if reason:
                    return redact_secrets(reason, extra_secrets=self.secrets_to_redact)
        return None

    def _sync_external_workspace(
        self,
        project_id: str,
        status: ProjectStatus | None = None,
    ) -> WorkspaceManager | None:
        """Bind authorized external_workspace_path into active WorkspaceManager instances (INV-22)."""
        ws_mgr = self._resolve_workspace_manager() or getattr(
            self.orchestrator, "workspace_manager", None
        )
        eff_status = status
        if eff_status is None:
            try:
                if self.orchestrator.store.get_events(project_id):
                    eff_status = self.orchestrator.get_status(project_id)
            except (ValueError, IllegalTransition):
                eff_status = None

        ext_path = eff_status.workspace_path if eff_status is not None else None
        if ext_path:
            for mgr in (
                ws_mgr,
                getattr(self.orchestrator, "workspace_manager", None),
                self.local_runner.workspace_manager,
            ):
                if mgr is not None and not mgr.is_external_workspace(project_id):
                    mgr.register_external_workspace(project_id, ext_path)
        return ws_mgr

    def _local_app_payload(self, project_id: str) -> dict[str, Any]:
        """Return safe local_app summary dict for API projections."""
        if self.local_runner.workspace_manager is None:
            ws_mgr = self._resolve_workspace_manager() or getattr(
                self.orchestrator, "workspace_manager", None
            )
            if ws_mgr is None:
                return {"running": False, "url": None, "app_type": "unknown"}
            self.local_runner.workspace_manager = ws_mgr
        self._sync_external_workspace(project_id)
        try:
            return self.local_runner.get_status(project_id).to_api_dict()
        except (SandboxViolationError, ValueError, OSError):
            return {"running": False, "url": None, "app_type": "unknown"}

    def _find_custom_logo(self, project_id: str) -> tuple[Path, str] | None:
        """Locate contained custom logo file for project_id in self.logos_dir (INV-22)."""
        validate_project_id(project_id)
        if not self.logos_dir.is_dir():
            return None
        for ext, mime in LOGO_MIME_BY_EXT.items():
            cand = self.logos_dir / f"{project_id}.{ext}"
            if cand.exists():
                if cand.is_symlink():
                    raise SandboxViolationError(f"Logo symlink rejected: {cand}")
                resolved = cand.resolve()
                if not resolved.is_relative_to(self.logos_dir):
                    raise SandboxViolationError(f"Logo path escapes logos_dir: {resolved}")
                if resolved.is_file():
                    return resolved, mime
        return None

    def _logo_url_for(self, project_id: str) -> str:
        base = f"/api/projects/{project_id}/logo"
        try:
            custom = self._find_custom_logo(project_id)
            if custom is not None:
                ver = custom[0].stat().st_mtime_ns // 1_000_000
                return f"{base}?v={ver}"
        except (SandboxViolationError, OSError, ValueError):
            pass
        return base

    def _serialize_summary(
        self,
        project_id: str,
        status: ProjectStatus,
        events: Sequence[Event],
        *,
        has_plan: bool,
    ) -> dict[str, Any]:
        last_error = self._extract_last_error(status, events)
        last_event_at = events[-1].at.isoformat() if events else None
        running = self.is_running(project_id)
        live_info = self._get_live_status_info(running)
        raw = status.model_dump(mode="json")
        raw["last_error"] = last_error
        raw["has_plan"] = has_plan
        raw["busy"] = running
        raw["is_running"] = running
        raw["local_app"] = self._local_app_payload(project_id)
        raw["logo_url"] = self._logo_url_for(project_id)
        raw.update(live_info)
        raw["last_event_at"] = last_event_at
        return _scrub_value(raw, self.secrets_to_redact)

    def list_projects(self) -> list[dict[str, Any]]:
        """Return status summaries for every project stored in the local EventStore."""
        summaries: list[dict[str, Any]] = []
        for pid in self.orchestrator.store.list_project_ids():
            try:
                validate_project_id(pid)
                events = self.orchestrator.store.get_events(pid)
                if not events:
                    continue
                status = self.orchestrator.get_status(pid)
                has_plan = self.orchestrator.get_plan(pid) is not None
                summaries.append(
                    self._serialize_summary(pid, status, events, has_plan=has_plan)
                )
            except (ValueError, IllegalTransition) as exc:
                running = self.is_running(pid)
                live_info = self._get_live_status_info(running)
                summaries.append(
                    {
                        "project_id": pid,
                        "name": pid,
                        "objective": "",
                        "state": "ERROR",
                        "phase": "ERROR",
                        "progress_pct": 0,
                        "last_error": redact_secrets(str(exc), extra_secrets=self.secrets_to_redact),
                        "failure_reason": redact_secrets(str(exc), extra_secrets=self.secrets_to_redact),
                        "links": {"repo": None, "deploy": None},
                        "tests": {"total": 0, "passed": 0, "failed": 0},
                        "has_plan": False,
                        "busy": running,
                        "is_running": running,
                        "local_app": {"running": False, "url": None, "app_type": "unknown"},
                        "logo_url": f"/api/projects/{pid}/logo",
                        **live_info,
                        "last_event_at": None,
                    }
                )
        return summaries

    def get_project_detail(self, project_id: str) -> dict[str, Any]:
        """Return full status, Spec, Plan, and redacted event log for project_id."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        status = self.orchestrator.get_status(project_id)
        self._sync_external_workspace(project_id, status)
        spec = self.orchestrator.get_spec(project_id)
        plan = self.orchestrator.get_plan(project_id)

        serialized_events = [
            {
                "id": ev.id,
                "project_id": ev.project_id,
                "type": ev.type,
                "payload": _scrub_value(ev.payload, self.secrets_to_redact),
                "at": ev.at.isoformat(),
            }
            for ev in events
        ]

        summary = self._serialize_summary(
            project_id, status, events, has_plan=(plan is not None)
        )
        return {
            "project_id": project_id,
            "status": summary,
            "is_running": summary["is_running"],
            "local_app": summary["local_app"],
            "logo_url": summary["logo_url"],
            "active_model": summary["active_model"],
            "active_stage": summary["active_stage"],
            "activity_summary": summary["activity_summary"],
            "activity_detail": summary["activity_detail"],
            "agents_count": summary["agents_count"],
            "step_started_at": summary["step_started_at"],
            "tests_passed": summary["tests_passed"],
            "tests_total": summary["tests_total"],
            "build_round": summary["build_round"],
            "max_rounds": summary["max_rounds"],
            "last_event_at": summary["last_event_at"],
            "spec": _scrub_value(spec.model_dump(mode="json"), self.secrets_to_redact),
            "plan": _scrub_value(plan.model_dump(mode="json"), self.secrets_to_redact)
            if plan is not None
            else None,
            "events": serialized_events,
        }

    def approve_project(self, project_id: str) -> dict[str, Any]:
        """Approve WAITING_APPROVAL project and launch TemplateScaffold + BuildWorker in background."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            current = self.orchestrator.get_status(project_id)
            if current.state != ProjectState.WAITING_APPROVAL or not current.plan_hash:
                raise ProjectConflictError(
                    f"Project '{project_id}' is in state {current.state.value}; expected WAITING_APPROVAL"
                )

            now = self.orchestrator._now()
            try:
                updated = self.orchestrator.submit_approval(
                    project_id,
                    ApprovalEvent(
                        plan_hash=current.plan_hash,
                        timestamp=now,
                        decision="approved",
                    ),
                )
            except (ApprovalError, IllegalTransition, ValueError) as exc:
                raise ProjectConflictError(str(exc)) from exc

            background_started = False
            if self.build_worker is not None or self.template_scaffold is not None:
                def _run_build() -> None:
                    try:
                        if self.build_worker is not None and self.build_worker.queue is not None:
                            self.build_worker.queue.remove(project_id)
                        if self.template_scaffold is not None:
                            plan = self.orchestrator.get_plan(project_id)
                            template_id = plan.template_id if plan else "static-web"
                            ws_dir = self.template_scaffold.workspace_manager.get_workspace_dir(
                                project_id
                            )
                            if workspace_nonempty_test_files(ws_dir):
                                self.template_scaffold.scaffold(
                                    project_id, template_id, allow_existing=True
                                )
                            else:
                                self.template_scaffold.scaffold(project_id, template_id)
                        if self.build_worker is not None:
                            self.build_worker.enqueue_approved_project(project_id)
                            report = self.build_worker.run_job(project_id)
                            final = self.orchestrator.get_status(project_id)
                            if final.state == ProjectState.DELIVERED:
                                repo_url = final.links.repo if final.links else ""
                                deploy_url = final.links.deploy if final.links else ""
                                self._notify_chat_best_effort(
                                    project_id,
                                    f"Proyecto {project_id} entregado con éxito!\nRepo: {repo_url}\nDeploy: {deploy_url}",
                                )
                            elif final.state == ProjectState.FAILED:
                                reason = final.failure_reason or report.failure_cause or "Build error"
                                self._notify_chat_best_effort(
                                    project_id,
                                    f"Proyecto {project_id} falló durante el build: {reason}",
                                )
                    except (
                        SandboxViolationError,
                        IllegalTransition,
                        CodeAgentError,
                        CommandTimeoutError,
                        ApprovalError,
                        ValueError,
                        TypeError,
                        LookupError,
                        TimeoutError,
                        OSError,
                        RuntimeError,
                    ) as exc:
                        cur = self.orchestrator.get_status(project_id).state
                        if cur not in (ProjectState.FAILED, ProjectState.DELIVERED):
                            self.orchestrator.record_phase_failure(
                                project_id,
                                failed_state=cur,
                                error=exc,
                                agent="dashboard_build",
                                extra_secrets=self.secrets_to_redact,
                            )

                background_started = self._start_background(project_id, _run_build)

        return {
            "ok": True,
            "project_id": project_id,
            "status": self._serialize_summary(
                project_id,
                updated,
                self.orchestrator.store.get_events(project_id),
                has_plan=True,
            ),
            "background_started": background_started,
        }

    def _next_project_id(self, prompt: str) -> str:
        """Derive a unique valid project_id slug from a user prompt."""
        base_pid = _slugify(prompt)
        pid = base_pid
        suffix = 2
        while self.orchestrator.store.get_events(pid):
            tail = f"-{suffix}"
            pid = f"{base_pid[: 63 - len(tail)]}{tail}"
            suffix += 1
        return validate_project_id(pid)

    def _run_debate_pipeline(
        self,
        project_id: str,
        *,
        agent_name: str,
        failure_prefix: str,
    ) -> None:
        """Execute run_debate, notify chat on outcome, and record unexpected phase errors."""
        try:
            res = self.orchestrator.run_debate(project_id)
            if res.state == ProjectState.WAITING_APPROVAL:
                plan = self.orchestrator.get_plan(project_id)
                summary = plan.summary if plan else project_id
                self._notify_chat_best_effort(
                    project_id,
                    f"Plan listo para aprobación ({project_id}):\n{summary}",
                )
            elif res.state == ProjectState.FAILED:
                self._notify_chat_best_effort(
                    project_id,
                    f"{failure_prefix} ({project_id}): {res.failure_reason or 'Error'}",
                )
        except (
            LLMError,
            IllegalTransition,
            ValueError,
            TypeError,
            LookupError,
            TimeoutError,
            OSError,
            RuntimeError,
        ) as exc:
            cur = self.orchestrator.get_status(project_id).state
            if cur not in (ProjectState.FAILED, ProjectState.DELIVERED):
                self.orchestrator.record_phase_failure(
                    project_id,
                    failed_state=cur,
                    error=exc,
                    agent=agent_name,
                    extra_secrets=self.secrets_to_redact,
                )

    def _run_interview_and_maybe_debate(
        self,
        project_id: str,
        event_id: str,
        user_message: str,
    ) -> None:
        """Run one interview round and chain DEBATE -> MEDIATION when Spec completes."""
        try:
            status = self.orchestrator.handle_interview_message(
                project_id, event_id, user_message
            )
            if status.state == ProjectState.FAILED:
                self._notify_chat_best_effort(
                    project_id,
                    f"El proyecto {project_id} falló en entrevista: {status.failure_reason or 'Error'}",
                )
                return
            if status.state != ProjectState.INTERVIEW:
                return

            events = self.orchestrator.store.get_events(project_id)
            last_round = next(
                (ev for ev in reversed(events) if ev.type == EventType.INTERVIEW_ROUND),
                None,
            )
            questions = last_round.payload.get("questions", []) if last_round else []
            if questions:
                q_lines = "\n".join(f"- {q}" for q in questions)
                self._notify_chat_best_effort(
                    project_id,
                    f"Preguntas para {project_id}:\n{q_lines}",
                )
                return
        except (
            LLMError,
            IllegalTransition,
            ValueError,
            TypeError,
            LookupError,
            TimeoutError,
            OSError,
            RuntimeError,
        ) as exc:
            cur = self.orchestrator.get_status(project_id).state
            if cur not in (ProjectState.FAILED, ProjectState.DELIVERED):
                self.orchestrator.record_phase_failure(
                    project_id,
                    failed_state=cur,
                    error=exc,
                    agent="dashboard_interview",
                    extra_secrets=self.secrets_to_redact,
                )
            return

        self._run_debate_pipeline(
            project_id,
            agent_name="dashboard_interview",
            failure_prefix="El debate falló",
        )

    def create_project(self, prompt: str) -> dict[str, Any]:
        """Create a new project from prompt with a clean synthesized title/slug and start INTERVIEW in background."""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("El campo 'prompt' es obligatorio.")

        short_title = synthesize_project_title(clean_prompt)
        with self._lock:
            pid = self._next_project_id(short_title)
            created = self.orchestrator.create_project(
                pid,
                name=short_title,
                objective=clean_prompt,
            )
            event_id = f"web-msg-{uuid.uuid4().hex[:12]}"
            background_started = self._start_background(
                pid,
                lambda: self._run_interview_and_maybe_debate(pid, event_id, clean_prompt),
            )
            summary = self._serialize_summary(
                pid,
                created,
                self.orchestrator.store.get_events(pid),
                has_plan=False,
            )

        return {
            "ok": True,
            "project_id": pid,
            "status": summary,
            "background_started": background_started,
        }

    def rename_project(
        self,
        project_id: str,
        title: str,
        *,
        rename_github_repo: bool | None = None,
    ) -> dict[str, Any]:
        """Validate, sanitize, persist PROJECT_RENAMED, and optionally rename GitHub repository (INV-11)."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        try:
            updated = self.orchestrator.rename_project(
                project_id,
                title,
                rename_github_repo=rename_github_repo,
                vcs=self.vcs,
            )
        except (VcsError, OSError) as exc:
            clean_msg = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            raise ProjectConflictError(clean_msg) from None
        summary = self._serialize_summary(
            project_id,
            updated,
            self.orchestrator.store.get_events(project_id),
            has_plan=(self.orchestrator.get_plan(project_id) is not None),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "name": updated.name,
            "repo_url": updated.links.repo,
            "deploy_url": updated.links.deploy,
            "status": summary,
        }

    def suggest_project_title(self, project_id: str) -> dict[str, Any]:
        """Inspect project workspace repo files and return 'Nombre - Descripción breve' suggestion."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        ws_mgr = self._sync_external_workspace(project_id)
        ws_dir: Path | None = None
        if ws_mgr is not None:
            candidate = ws_mgr.get_workspace_dir(project_id)
            if candidate.is_dir():
                ws_dir = candidate
        suggested = self.orchestrator.suggest_title(
            project_id,
            workspace_dir=ws_dir,
            extra_secrets=self.secrets_to_redact,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "suggested_title": suggested,
        }

    def _effective_git_runner(self) -> Runner:
        """Return explicit Runner or real SubprocessRunner for isolated workspace git/pytest checks."""
        if self.runner is not None:
            return self.runner
        worker_runner = getattr(self.build_worker, "runner", None)
        if worker_runner is not None and worker_runner.__class__.__name__ != "FakeRunner":
            return worker_runner
        vcs_runner = getattr(self.vcs, "runner", None)
        if vcs_runner is not None and vcs_runner.__class__.__name__ != "FakeRunner":
            return vcs_runner
        return SubprocessRunner(default_timeout_seconds=25.0)

    def _require_project_events(self, project_id: str) -> list[Event]:
        """Validate project_id and ensure at least one event exists or raise ProjectNotFoundError."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        return events

    def get_project_checkpoints(self, project_id: str) -> dict[str, Any]:
        """Return delivered version checkpoints (newest first) for Rollback panel."""
        self._require_project_events(project_id)
        checkpoints = self.orchestrator.list_project_checkpoints(
            project_id,
            extra_secrets=self.secrets_to_redact,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "checkpoints": checkpoints,
        }

    def get_project_files(self, project_id: str) -> dict[str, Any]:
        """Return safe sorted relative file paths for Vista Desarrollador without .env."""
        self._require_project_events(project_id)
        ws_mgr = self._sync_external_workspace(project_id)
        files = self.orchestrator.list_project_files(project_id, workspace_manager=ws_mgr)
        return {
            "ok": True,
            "project_id": project_id,
            "files": files,
        }

    def get_project_diff(self, project_id: str) -> dict[str, Any]:
        """Return redacted Git diff for project_id workspace (INV-19, INV-22)."""
        self._require_project_events(project_id)
        ws_mgr = self._sync_external_workspace(project_id)
        diff_info = self.orchestrator.get_project_diff(
            project_id,
            workspace_manager=ws_mgr,
            runner=self._effective_git_runner(),
            extra_secrets=self.secrets_to_redact,
        )
        return {
            "ok": True,
            "project_id": project_id,
            **diff_info,
        }

    def rollback_project(self, project_id: str, commit_sha: str) -> dict[str, Any]:
        """Restore isolated repository to commit_sha, run pytest + smoke test, push if remote, and record DELIVERED."""
        self._require_project_events(project_id)
        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            self._active_projects.add(project_id)
            ws_mgr = self._sync_external_workspace(project_id)
            if self.local_runner.workspace_manager is None and ws_mgr is not None:
                self.local_runner.workspace_manager = ws_mgr
            eff_runner = self._effective_git_runner()
        try:
            try:
                updated, cp_info = self.orchestrator.rollback_project(
                    project_id,
                    commit_sha,
                    workspace_manager=ws_mgr,
                    runner=eff_runner,
                    local_app_runner=self.local_runner,
                    vcs=self.vcs,
                    extra_secrets=self.secrets_to_redact,
                )
            except (IllegalTransition, VcsError, RuntimeError) as exc:
                clean_msg = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
                raise ProjectConflictError(clean_msg) from exc
        finally:
            with self._lock:
                self._active_projects.discard(project_id)

        summary = self._serialize_summary(
            project_id,
            updated,
            self.orchestrator.store.get_events(project_id),
            has_plan=(self.orchestrator.get_plan(project_id) is not None),
        )
        checkpoints = self.orchestrator.list_project_checkpoints(
            project_id, extra_secrets=self.secrets_to_redact
        )
        return {
            "ok": True,
            "project_id": project_id,
            "checkpoint": cp_info,
            "checkpoints": checkpoints,
            "status": summary,
        }

    def get_project_chat(self, project_id: str) -> dict[str, Any]:
        """Return persisted read-only audit/Q&A chat history for project_id."""
        validate_project_id(project_id)
        if not self.orchestrator.store.get_events(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        history = self.orchestrator.get_project_chat_history(
            project_id,
            chats_dir=self.chats_dir,
            extra_secrets=self.secrets_to_redact,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "messages": history,
        }

    def post_project_chat(
        self,
        project_id: str,
        message: str,
        *,
        mode: str = "chat",
    ) -> dict[str, Any]:
        """Run read-only Q&A, audit, or Markdown document generation for project_id."""
        validate_project_id(project_id)
        if not self.orchestrator.store.get_events(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        ws_mgr = self._sync_external_workspace(project_id)
        try:
            result = self.orchestrator.chat_with_project(
                project_id,
                message,
                mode=mode,
                chats_dir=self.chats_dir,
                workspace_manager=ws_mgr,
                extra_secrets=self.secrets_to_redact,
            )
        except LLMError as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            raise ProjectConflictError(clean_err) from None
        return _scrub_value(result, self.secrets_to_redact)

    def import_external_project(
        self,
        local_path: str,
        *,
        title: str = "",
    ) -> dict[str, Any]:
        """Validate and import an existing local directory as a DELIVERED project (INV-22)."""
        ws_mgr = self._resolve_workspace_manager() or getattr(
            self.orchestrator, "workspace_manager", None
        )
        status, meta = self.orchestrator.import_project(
            local_path,
            title=title,
            workspace_manager=ws_mgr,
            extra_secrets=self.secrets_to_redact,
        )
        pid = status.project_id
        self._sync_external_workspace(pid, status)
        events = self.orchestrator.store.get_events(pid)
        summary = self._serialize_summary(
            pid,
            status,
            events,
            has_plan=(self.orchestrator.get_plan(pid) is not None),
        )
        return {
            "ok": True,
            "project_id": pid,
            "app_type": meta.get("app_type", "unknown"),
            "manifest_loaded": bool(meta.get("manifest_loaded", False)),
            "status": summary,
        }

    def get_lan_mode(self) -> dict[str, Any]:
        """Return current LAN/mobile mode setting and local LAN IP when enabled."""
        enabled = self.local_runner.get_lan_mode()
        lan_ip = self.local_runner._lan_ip_resolver() if enabled else None
        return {"ok": True, "lan_mode": enabled, "lan_ip": lan_ip}

    def set_lan_mode(self, enabled: bool) -> dict[str, Any]:
        """Toggle LAN 0.0.0.0 binding mode on LocalAppRunner."""
        if not isinstance(enabled, bool):
            raise TypeError("El campo 'lan_mode' debe ser booleano.")
        info = self.local_runner.set_lan_mode(enabled)
        return {"ok": True, **info}

    def get_project_logo(self, project_id: str) -> tuple[str, bytes]:
        """Return custom logo bytes+mime from logos_dir or auto-generated 64x64 domain SVG."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        ws_mgr = self._resolve_workspace_manager() or getattr(
            self.orchestrator, "workspace_manager", None
        )
        has_ws = ws_mgr is not None and ws_mgr.get_workspace_dir(project_id).is_dir()
        if not events and not has_ws:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        custom = self._find_custom_logo(project_id)
        if custom is not None:
            logo_path, mime = custom
            return mime, logo_path.read_bytes()

        title = project_id
        objective = ""
        if events:
            try:
                status = self.orchestrator.get_status(project_id)
                title = status.name or project_id
                objective = status.objective
            except IllegalTransition:
                first_pl = events[0].payload or {}
                title = str(first_pl.get("name") or project_id)
                objective = str(first_pl.get("objective") or "")
        svg = generate_project_logo_svg(title, objective)
        return "image/svg+xml; charset=utf-8", svg.encode("utf-8")

    def upload_project_logo(
        self,
        project_id: str,
        raw_bytes: bytes,
        filename: str = "",
    ) -> dict[str, Any]:
        """Validate/sanitize uploaded SVG/PNG/JPG/WebP logo <=512KB, save under logos_dir, and copy into workspace."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        ws_mgr = self._sync_external_workspace(project_id)
        ws_dir: Path | None = None
        if ws_mgr is not None:
            cand = ws_mgr.get_workspace_dir(project_id)
            if cand.is_dir():
                ws_dir = cand
        if not events and ws_dir is None:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        ext, mime, clean_bytes = validate_and_sanitize_logo_bytes(
            raw_bytes, filename=filename
        )
        self.logos_dir.mkdir(parents=True, exist_ok=True)
        for old_ext in LOGO_MIME_BY_EXT:
            old_file = self.logos_dir / f"{project_id}.{old_ext}"
            if old_ext != ext and old_file.is_file() and not old_file.is_symlink():
                old_file.unlink()
        target_path = (self.logos_dir / f"{project_id}.{ext}").resolve()
        if not target_path.is_relative_to(self.logos_dir):
            raise SandboxViolationError(f"Logo destination escapes logos_dir: {target_path}")
        target_path.write_bytes(clean_bytes)

        if ws_dir is not None and not ws_dir.is_symlink():
            fav_target = ws_dir / f"favicon.{ext}"
            if not fav_target.is_symlink():
                fav_target.write_bytes(clean_bytes)
            if ext == "svg":
                for sub_name in ("src", "static", "public", "web"):
                    sub_dir = ws_dir / sub_name
                    if sub_dir.is_dir() and (sub_dir / "index.html").is_file():
                        sub_fav = sub_dir / "favicon.svg"
                        if not sub_fav.is_symlink():
                            sub_fav.write_bytes(clean_bytes)

        return {
            "ok": True,
            "project_id": project_id,
            "logo_url": self._logo_url_for(project_id),
            "mime_type": mime,
        }

    def start_local_app(self, project_id: str) -> dict[str, Any]:
        """Start project_id locally via LocalAppRunner and return updated local_app status."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        ws_mgr = self._sync_external_workspace(project_id)
        if not events and (ws_mgr is None or not ws_mgr.get_workspace_dir(project_id).is_dir()):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' has an active background operation in progress"
                )
            if self.local_runner.workspace_manager is None:
                self.local_runner.workspace_manager = ws_mgr
            try:
                local_status = self.local_runner.start(project_id, install_deps=True)
            except (LocalAppError, SandboxViolationError, ValueError) as exc:
                clean_msg = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
                raise ProjectConflictError(clean_msg) from exc

        updated = self.orchestrator.get_status(project_id) if events else None
        summary = (
            self._serialize_summary(
                project_id,
                updated,
                self.orchestrator.store.get_events(project_id),
                has_plan=(self.orchestrator.get_plan(project_id) is not None),
            )
            if updated is not None
            else None
        )
        return {
            "ok": True,
            "project_id": project_id,
            "local_app": local_status.to_api_dict(),
            "status": summary,
        }

    def stop_local_app(self, project_id: str) -> dict[str, Any]:
        """Stop locally running app for project_id cleanly and release its port."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        ws_mgr = self._sync_external_workspace(project_id)
        if not events and (ws_mgr is None or not ws_mgr.get_workspace_dir(project_id).is_dir()):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        if self.local_runner.workspace_manager is None:
            self.local_runner.workspace_manager = ws_mgr
        local_status = self.local_runner.stop(project_id)
        updated = self.orchestrator.get_status(project_id) if events else None
        summary = (
            self._serialize_summary(
                project_id,
                updated,
                self.orchestrator.store.get_events(project_id),
                has_plan=(self.orchestrator.get_plan(project_id) is not None),
            )
            if updated is not None
            else None
        )
        return {
            "ok": True,
            "project_id": project_id,
            "local_app": local_status.to_api_dict(),
            "status": summary,
        }

    def modify_project(
        self,
        project_id: str,
        instructions: str,
        *,
        target_files: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Submit post-delivery modification instructions on a DELIVERED project preserving workspace (INV-13)."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        clean_instructions = str(instructions or "").strip()
        if not clean_instructions:
            raise ValueError("El campo 'instructions' es obligatorio.")

        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            ws_mgr = self._sync_external_workspace(project_id)
            self.local_runner.stop(project_id)
            try:
                updated = self.orchestrator.modify_project(
                    project_id,
                    clean_instructions,
                    target_files=target_files,
                    workspace_manager=ws_mgr,
                )
            except IllegalTransition as exc:
                raise ProjectConflictError(str(exc)) from exc

            if self.build_worker is not None and self.build_worker.queue is not None:
                self.build_worker.queue.remove(project_id)

            background_started = False
            if self.build_worker is not None:
                def _run_modify_build() -> None:
                    assert self.build_worker is not None
                    try:
                        if self.build_worker.queue is not None:
                            self.build_worker.queue.remove(project_id)
                        self.build_worker.enqueue_approved_project(project_id)
                        report = self.build_worker.run_job(project_id)
                        final = self.orchestrator.get_status(project_id)
                        if final.state == ProjectState.DELIVERED:
                            repo_url = final.links.repo if final.links else ""
                            deploy_url = final.links.deploy if final.links else ""
                            self._notify_chat_best_effort(
                                project_id,
                                f"Cambios aplicados con éxito en {project_id}!\nRepo: {repo_url}\nDeploy: {deploy_url}",
                            )
                        elif final.state == ProjectState.FAILED:
                            reason = final.failure_reason or report.failure_cause or "Modify error"
                            self._notify_chat_best_effort(
                                project_id,
                                f"Error aplicando cambios en {project_id}: {reason}",
                            )
                    except (
                        SandboxViolationError,
                        IllegalTransition,
                        CodeAgentError,
                        CommandTimeoutError,
                        ApprovalError,
                        ValueError,
                        TypeError,
                        LookupError,
                        TimeoutError,
                        OSError,
                        RuntimeError,
                    ) as exc:
                        cur = self.orchestrator.get_status(project_id).state
                        if cur not in (ProjectState.FAILED, ProjectState.DELIVERED):
                            self.orchestrator.record_phase_failure(
                                project_id,
                                failed_state=cur,
                                error=exc,
                                agent="dashboard_modify",
                                extra_secrets=self.secrets_to_redact,
                            )

                background_started = self._start_background(project_id, _run_modify_build)

            summary = self._serialize_summary(
                project_id,
                updated,
                self.orchestrator.store.get_events(project_id),
                has_plan=(self.orchestrator.get_plan(project_id) is not None),
            )

        return {
            "ok": True,
            "project_id": project_id,
            "status": summary,
            "background_started": background_started,
        }

    def send_message(self, project_id: str, message: str) -> dict[str, Any]:
        """Submit an interview reply or plan adjustment request for project_id in background."""
        validate_project_id(project_id)
        if not self.orchestrator.store.get_events(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        clean_message = str(message or "").strip()
        if not clean_message:
            raise ValueError("El campo 'message' es obligatorio.")

        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            current = self.orchestrator.get_status(project_id)
            if current.state == ProjectState.INTERVIEW:
                updated = current
            elif current.state == ProjectState.WAITING_APPROVAL:
                try:
                    updated = self.orchestrator.request_adjustment(
                        project_id,
                        reason=clean_message,
                    )
                except (IllegalTransition, ValueError) as exc:
                    raise ProjectConflictError(str(exc)) from exc
            else:
                raise ProjectConflictError(
                    f"Project '{project_id}' is in state {current.state.value}; "
                    "expected INTERVIEW or WAITING_APPROVAL"
                )

            event_id = f"web-msg-{uuid.uuid4().hex[:12]}"
            background_started = self._start_background(
                project_id,
                lambda: self._run_interview_and_maybe_debate(
                    project_id, event_id, clean_message
                ),
            )
            summary = self._serialize_summary(
                project_id,
                updated,
                self.orchestrator.store.get_events(project_id),
                has_plan=(self.orchestrator.get_plan(project_id) is not None),
            )

        return {
            "ok": True,
            "project_id": project_id,
            "status": summary,
            "background_started": background_started,
        }

    def _resolve_workspace_manager(self) -> WorkspaceManager | None:
        return (
            self.workspace_manager
            or getattr(self.build_worker, "workspace_manager", None)
            or getattr(self.template_scaffold, "workspace_manager", None)
        )

    def retry_project(
        self,
        project_id: str,
        *,
        clean_workspace: bool = False,
    ) -> dict[str, Any]:
        """Retry a FAILED or paused project (conserving workspace unless clean_workspace=True) and resume if applicable."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            try:
                updated = self.orchestrator.retry_project(
                    project_id,
                    workspace_manager=self._resolve_workspace_manager(),
                    clean_workspace=clean_workspace,
                )
            except (ValueError, IllegalTransition, SandboxViolationError) as exc:
                raise ProjectConflictError(str(exc)) from exc

            if self.build_worker is not None and self.build_worker.queue is not None:
                self.build_worker.queue.remove(project_id)

            background_started = False
            if updated.state in (ProjectState.DEBATE, ProjectState.MEDIATION):
                background_started = self._start_background(
                    project_id,
                    lambda: self._run_debate_pipeline(
                        project_id,
                        agent_name="dashboard_retry",
                        failure_prefix="Reintento fallido",
                    ),
                )

        return {
            "ok": True,
            "project_id": project_id,
            "status": self._serialize_summary(
                project_id,
                updated,
                self.orchestrator.store.get_events(project_id),
                has_plan=(self.orchestrator.get_plan(project_id) is not None),
            ),
            "background_started": background_started,
        }

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Delete project events from SQLite and remove workspaces/<project_id> if present."""
        validate_project_id(project_id)
        with self._lock:
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' has an active background operation in progress"
                )

            had_events = bool(self.orchestrator.store.get_events(project_id))
            ws_mgr = self._sync_external_workspace(project_id)
            ws_dir: Path | None = None
            if ws_mgr is not None:
                ws_dir = ws_mgr.get_workspace_dir(project_id)
            had_workspace = ws_dir is not None and ws_dir.exists()

            if not had_events and not had_workspace:
                raise ProjectNotFoundError(f"Project '{project_id}' not found")

            self.local_runner.stop(project_id)
            self.orchestrator.delete_project(project_id)
            self.orchestrator.delete_project_chat_history(project_id, chats_dir=self.chats_dir)

            if self.build_worker is not None and self.build_worker.queue is not None:
                self.build_worker.queue.remove(project_id)

            if self.bot_service is not None:
                stale_chats = [
                    cid
                    for cid, pid in self.bot_service.active_project_by_chat.items()
                    if pid == project_id
                ]
                for cid in stale_chats:
                    self.bot_service.active_project_by_chat.pop(cid, None)

            if ws_mgr is not None and had_workspace:
                ws_mgr.remove_workspace(project_id)

            if self.logos_dir.is_dir():
                for ext in LOGO_MIME_BY_EXT:
                    logo_f = self.logos_dir / f"{project_id}.{ext}"
                    if logo_f.is_file() and not logo_f.is_symlink():
                        logo_f.unlink()

        return {"ok": True, "deleted": project_id}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fábrica de Proyectos</title>
  <style>
    :root {
      --bg: #0b0f19;
      --surface: #131b2e;
      --surface-alt: #1a243b;
      --border: #26334d;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0ea5e9;
      --ok: #22c55e;
      --ok-hover: #16a34a;
      --ok-bg: rgba(34, 197, 94, 0.14);
      --warn: #f59e0b;
      --warn-bg: rgba(245, 158, 11, 0.14);
      --err: #ef4444;
      --err-bg: rgba(239, 68, 68, 0.14);
      --info-bg: rgba(56, 189, 248, 0.12);
    }
    html[data-theme="light"] {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-alt: #f1f5f9;
      --border: #e2e8f0;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0284c7;
      --accent-hover: #0369a1;
      --ok: #16a34a;
      --ok-hover: #15803d;
      --ok-bg: #dcfce7;
      --warn: #d97706;
      --warn-bg: #fef3c7;
      --err: #dc2626;
      --err-bg: #fee2e2;
      --info-bg: #e0f2fe;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 24px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 1.15rem;
      letter-spacing: -0.01em;
    }
    .live-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--ok);
      display: inline-block;
      box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.6);
      animation: pulse-ring 2.5s infinite;
    }
    @keyframes pulse-ring {
      0% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.6); }
      70% { box-shadow: 0 0 0 7px rgba(34, 197, 94, 0); }
      100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); }
    }
    .controls { display: flex; align-items: center; gap: 10px; }
    .btn {
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--text);
      padding: 8px 16px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.9rem;
      font-weight: 500;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
    }
    .btn:hover { border-color: var(--accent); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-primary {
      background: var(--accent);
      color: #082f49;
      border-color: var(--accent);
      font-weight: 600;
    }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-success {
      background: var(--ok);
      color: #ffffff;
      border-color: var(--ok);
      font-weight: 600;
      box-shadow: 0 2px 4px rgba(34, 197, 94, 0.2);
    }
    .btn-success:hover { background: var(--ok-hover); }
    .btn-warn {
      background: var(--warn);
      color: #ffffff;
      border-color: var(--warn);
      font-weight: 600;
    }
    .btn-subtle-danger {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
      transition: all 0.15s ease;
    }
    .btn-subtle-danger:hover {
      background: var(--err-bg);
      color: var(--err);
      border-color: rgba(239, 68, 68, 0.2);
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(320px, 390px) 1fr;
      gap: 20px;
      padding: 20px 24px;
      max-width: 1440px;
      margin: 0 auto;
    }
    @media (max-width: 900px) {
      .layout { grid-template-columns: 1fr; }
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .project-card {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 12px;
      cursor: pointer;
      background: var(--surface);
      transition: all 0.2s ease;
      box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }
    .project-card:hover {
      border-color: var(--accent);
      transform: translateY(-1px);
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.06);
    }
    .project-card.selected {
      border-color: var(--accent);
      background: var(--surface-alt);
      box-shadow: 0 0 0 1px var(--accent);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      margin-bottom: 8px;
    }
    .card-title {
      font-size: 1rem;
      font-weight: 600;
      line-height: 1.4;
      color: var(--text);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .busy-pill {
      font-size: 0.72rem;
      font-weight: 500;
      color: var(--muted);
      margin-left: 4px;
    }
    .model-usage-row {
      margin-top: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .model-pill {
      display: inline-flex;
      align-items: center;
      font-size: 0.76rem;
      font-weight: 500;
      padding: 2px 9px;
      border-radius: 999px;
      background: var(--surface);
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .badge {
      font-size: 0.75rem;
      font-weight: 600;
      padding: 3px 9px;
      border-radius: 999px;
      border: 1px solid transparent;
      white-space: nowrap;
    }
    .badge-ok { background: var(--ok-bg); color: var(--ok); border-color: var(--ok); }
    .badge-err { background: var(--err-bg); color: var(--err); border-color: var(--err); }
    .badge-warn { background: var(--warn-bg); color: var(--warn); border-color: var(--warn); }
    .badge-info { background: var(--info-bg); color: var(--accent); border-color: var(--accent); }
    .stage-segments {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 5px;
      margin: 8px 0 6px;
    }
    .stage-seg {
      height: 5px;
      border-radius: 999px;
      background: var(--border);
      transition: background 0.25s ease;
    }
    .stage-seg.done { background: var(--ok); }
    .stage-seg.active { background: var(--accent); }
    .stage-seg.error { background: var(--err); }
    .stage-seg.warn { background: var(--warn); }
    .muted { color: var(--muted); font-size: 0.88rem; }
    .empty-box {
      padding: 30px 16px;
      text-align: center;
      color: var(--muted);
      font-size: 0.92rem;
    }
    .detail-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 12px;
    }
    .detail-titles { flex: 1; }
    .detail-title {
      margin: 0;
      font-size: 1.35rem;
      font-weight: 700;
      line-height: 1.3;
    }
    .detail-sub {
      margin-top: 4px;
      font-size: 0.92rem;
    }
    .guidance-banner {
      border-radius: 12px;
      padding: 16px 18px;
      margin: 16px 0 20px;
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .guidance-banner.banner-warn {
      background: var(--warn-bg);
      border-color: rgba(245, 158, 11, 0.35);
    }
    .guidance-banner.banner-err {
      background: var(--err-bg);
      border-color: rgba(239, 68, 68, 0.35);
    }
    .guidance-banner.banner-ok {
      background: var(--ok-bg);
      border-color: rgba(34, 197, 94, 0.35);
    }
    .guidance-banner.banner-info {
      background: var(--info-bg);
      border-color: rgba(56, 189, 248, 0.35);
    }
    .banner-content {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }
    .banner-icon { font-size: 1.3rem; line-height: 1.2; }
    .banner-body { flex: 1; }
    .banner-title {
      font-weight: 600;
      font-size: 1rem;
      margin-bottom: 4px;
    }
    .banner-desc {
      font-size: 0.92rem;
      color: var(--text);
      line-height: 1.45;
    }
    .banner-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    details.tech-details {
      margin-top: 10px;
      border-top: 1px dashed var(--border);
      padding-top: 8px;
    }
    details.tech-details summary {
      cursor: pointer;
      font-size: 0.82rem;
      color: var(--muted);
      font-weight: 500;
      user-select: none;
    }
    details.tech-details summary:hover {
      color: var(--text);
    }
    details.tech-details pre {
      margin-top: 8px;
      background: var(--surface-alt);
      padding: 8px 10px;
      border-radius: 6px;
      font-size: 0.8rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      color: var(--err);
      border: 1px solid var(--border);
    }
    .secondary-actions {
      display: flex;
      justify-content: flex-end;
      padding-top: 12px;
      margin-top: 16px;
      border-top: 1px solid var(--border);
    }
    .tabs {
      display: flex;
      gap: 6px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
      margin-bottom: 16px;
    }
    .tab-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      padding: 6px 14px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.9rem;
      transition: all 0.15s ease;
    }
    .tab-btn.active {
      background: var(--info-bg);
      color: var(--accent);
      border-color: var(--accent);
    }
    .tab-content { padding-top: 4px; }
    .spec-section { margin-bottom: 16px; }
    .spec-section h4 {
      margin: 0 0 6px 0;
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text);
    }
    .spec-section p {
      margin: 0;
      font-size: 0.92rem;
      line-height: 1.5;
    }
    .spec-section ul {
      margin: 0;
      padding-left: 20px;
      font-size: 0.92rem;
      line-height: 1.5;
    }
    .spec-section li { margin-bottom: 4px; }
    .plan-summary {
      font-size: 0.92rem;
      margin-bottom: 12px;
      line-height: 1.5;
    }
    .plan-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .file-card {
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      margin-bottom: 8px;
    }
    .file-path {
      font-weight: 600;
      font-family: monospace;
      font-size: 0.88rem;
    }
    .file-purpose {
      font-size: 0.84rem;
      color: var(--muted);
      margin-top: 2px;
    }
    pre {
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
      overflow-x: auto;
      font-size: 0.8rem;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .timeline {
      display: flex;
      flex-direction: column;
      padding: 6px 0;
    }
    .timeline-item {
      position: relative;
      padding-left: 22px;
      padding-bottom: 16px;
      border-left: 2px solid var(--border);
    }
    .timeline-item:last-child {
      border-left-color: transparent;
      padding-bottom: 0;
    }
    .timeline-dot {
      position: absolute;
      left: -6px;
      top: 4px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--border);
    }
    .timeline-dot.ok { background: var(--ok); }
    .timeline-dot.warn { background: var(--warn); }
    .timeline-dot.err { background: var(--err); }
    .timeline-dot.info { background: var(--accent); }
    .timeline-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }
    .timeline-title-group {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .timeline-type {
      font-weight: 600;
      font-size: 0.88rem;
    }
    .timeline-time {
      font-size: 0.78rem;
      color: var(--muted);
    }
    .duration-pill {
      display: inline-flex;
      align-items: center;
      font-size: 0.74rem;
      font-weight: 500;
      padding: 1px 8px;
      border-radius: 999px;
      background: var(--surface-alt);
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .timeline-next {
      font-size: 0.8rem;
      color: var(--muted);
      margin: 2px 0 4px 0;
    }
    .phase-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 4px 0;
    }
    .phase-card {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface);
      transition: all 0.15s ease;
    }
    .phase-card.phase-active {
      border-color: var(--accent);
      box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.25);
    }
    .phase-card.phase-error {
      border-color: var(--err);
    }
    .phase-card.phase-pending {
      opacity: 0.55;
      background: var(--surface-alt);
    }
    .phase-summary {
      list-style: none;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 11px 14px;
      cursor: pointer;
      user-select: none;
    }
    .phase-summary::-webkit-details-marker { display: none; }
    .phase-card.phase-pending .phase-summary {
      cursor: default;
    }
    .phase-summary-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .phase-circle {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.78rem;
      font-weight: 700;
      flex-shrink: 0;
      border: 1.5px solid var(--border);
      color: var(--muted);
      background: var(--surface-alt);
    }
    .phase-completed .phase-circle {
      background: var(--ok);
      border-color: var(--ok);
      color: #ffffff;
    }
    .phase-active .phase-circle {
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }
    .phase-error .phase-circle {
      background: var(--err);
      border-color: var(--err);
      color: #ffffff;
    }
    .phase-title {
      font-weight: 600;
      font-size: 0.94rem;
      color: var(--text);
    }
    .phase-meta-badge {
      font-size: 0.76rem;
      font-weight: 500;
      padding: 2px 9px;
      border-radius: 999px;
      background: var(--surface-alt);
      color: var(--muted);
      border: 1px solid var(--border);
      white-space: nowrap;
    }
    .phase-completed .phase-meta-badge {
      background: var(--ok-bg);
      color: var(--ok);
      border-color: rgba(34, 197, 94, 0.3);
    }
    .phase-active .phase-meta-badge {
      background: var(--info-bg);
      color: var(--accent);
      border-color: rgba(56, 189, 248, 0.35);
    }
    .phase-error .phase-meta-badge {
      background: var(--err-bg);
      color: var(--err);
      border-color: rgba(239, 68, 68, 0.35);
    }
    .phase-steps {
      border-top: 1px solid var(--border);
      padding: 12px 14px 12px 18px;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .toast { font-size: 0.85rem; color: var(--accent); }
    .btn-sm { padding: 5px 11px; font-size: 0.82rem; border-radius: 6px; }
    .title-row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .card-title-wrap {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex: 1;
    }
    .card-logo {
      width: 30px;
      height: 30px;
      border-radius: 8px;
      object-fit: cover;
      flex-shrink: 0;
      border: 1px solid var(--border);
      background: var(--surface-alt);
    }
    .detail-logo {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      object-fit: cover;
      flex-shrink: 0;
      border: 1px solid var(--border);
      background: var(--surface-alt);
    }
    .btn-icon {
      border: 1px solid transparent;
      background: transparent;
      color: var(--muted);
      padding: 3px 7px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
      line-height: 1;
      transition: all 0.15s ease;
    }
    .btn-icon:hover {
      border-color: var(--border);
      background: var(--surface-alt);
      color: var(--text);
    }
    .edit-title-form {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 6px;
    }
    .rename-github-check {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 0.78rem;
      color: var(--muted);
      cursor: pointer;
      user-select: none;
    }
    .input-inline {
      flex: 1;
      min-width: 210px;
      padding: 6px 10px;
      font-size: 0.92rem;
      resize: none;
    }
    .local-controls-row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .modify-box {
      margin-top: 4px;
      padding-top: 12px;
      border-top: 1px dashed rgba(34, 197, 94, 0.35);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .modify-box-title {
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--text);
    }
    .new-project-form {
      margin-top: 12px;
      padding: 12px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 10px;
    }
    .new-project-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 8px;
    }
    .input-field {
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      font-family: inherit;
      font-size: 0.92rem;
      line-height: 1.4;
      resize: vertical;
    }
    .input-field:focus {
      outline: none;
      border-color: var(--accent);
    }
    .banner-form {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .question-list {
      margin: 6px 0 4px 0;
      padding-left: 20px;
      font-size: 0.92rem;
      line-height: 1.45;
    }
    .question-list li { margin-bottom: 4px; }
    .inactivity-note {
      font-size: 0.84rem;
      color: var(--warn);
      margin-top: 6px;
      font-weight: 500;
    }
    details.adjust-details {
      margin-top: 6px;
      border-top: 1px dashed var(--border);
      padding-top: 8px;
    }
    details.adjust-details summary {
      cursor: pointer;
      font-size: 0.85rem;
      color: var(--muted);
      font-weight: 500;
      user-select: none;
    }
    details.adjust-details summary:hover {
      color: var(--text);
    }
    .working-spinner {
      display: inline-block;
      width: 18px;
      height: 18px;
      border: 2.5px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    .banner-subdesc {
      font-size: 0.85rem;
      color: var(--muted);
      margin-top: 4px;
      line-height: 1.4;
    }
    .banner-tests {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--ok);
      margin-top: 6px;
    }
    .banner-tests.pending { color: var(--accent); }
    .banner-calm {
      font-size: 0.8rem;
      color: var(--muted);
      margin-top: 8px;
    }
    .tech-toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 0.85rem;
      color: var(--muted);
      cursor: pointer;
      user-select: none;
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--surface);
    }
    body:not(.tech-mode) .tech-only { display: none; }
    .history-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }
    .round-card {
      border: 1px solid var(--border);
      background: var(--surface-alt);
      border-radius: 8px;
      padding: 8px 11px;
      margin-top: 4px;
    }
    .round-summary {
      font-size: 0.85rem;
      color: var(--text);
      margin-top: 4px;
      line-height: 1.4;
    }
    .round-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin-top: 6px;
    }
    .file-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 6px;
    }
    .file-chip {
      display: inline-block;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.74rem;
      padding: 1px 7px;
      border-radius: 5px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--muted);
    }
    .test-badge {
      display: inline-flex;
      align-items: center;
      font-size: 0.74rem;
      font-weight: 600;
      padding: 1px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--muted);
    }
    .test-badge.ok {
      color: var(--ok);
      border-color: rgba(34, 197, 94, 0.35);
      background: var(--ok-bg);
    }
    details.prior-attempts-box {
      margin-top: 14px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface-alt);
      padding: 10px 14px;
    }
    details.prior-attempts-box > summary {
      cursor: pointer;
      font-size: 0.86rem;
      font-weight: 600;
      color: var(--muted);
      user-select: none;
    }
    details.prior-attempts-box > summary:hover { color: var(--text); }
    .mic-btn.listening {
      border-color: var(--err);
      background: var(--err-bg);
      color: var(--err);
    }
    .input-row-with-mic {
      display: flex;
      gap: 8px;
      align-items: flex-start;
    }
    .input-row-with-mic > textarea { flex: 1; }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(2, 6, 23, 0.72);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
      z-index: 50;
    }
    .modal-card {
      width: 100%;
      max-width: 500px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      box-shadow: 0 12px 32px rgba(0,0,0,0.45);
    }
    .audit-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }
    .audit-thread {
      display: flex;
      flex-direction: column;
      gap: 10px;
      max-height: 440px;
      overflow-y: auto;
      padding: 10px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 12px;
    }
    .audit-msg {
      padding: 10px 12px;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: var(--surface);
    }
    .audit-msg.user {
      border-color: rgba(56, 189, 248, 0.45);
      background: var(--info-bg);
    }
    .audit-msg-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .audit-msg-body {
      white-space: pre-wrap;
      font-size: 0.88rem;
      line-height: 1.48;
      word-break: break-word;
    }
    .audit-msg-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed var(--border);
    }
    .tech-mode-hint { font-size: 0.76rem; }
    .versions-box {
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 9px;
      background: var(--surface-alt);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .versions-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .versions-row > select { flex: 1; min-width: 190px; }
    .diff-pre {
      max-height: 300px;
      overflow: auto;
      padding: 10px;
      border-radius: 8px;
      background: var(--bg);
      border: 1px solid var(--border);
      font-size: 0.78rem;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .detail-work-split.preview-open {
      display: grid;
      grid-template-columns: minmax(340px, 1fr) minmax(360px, 1fr);
      gap: 16px;
      align-items: start;
    }
    @media (max-width: 1080px) {
      .detail-work-split.preview-open { grid-template-columns: 1fr; }
    }
    .preview-card {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface-alt);
      overflow: hidden;
      display: flex;
      flex-direction: column;
      position: sticky;
      top: 72px;
    }
    .preview-toolbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
    }
    #live-preview-iframe {
      width: 100%;
      height: 540px;
      border: 0;
      background: #ffffff;
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .live-dot.disconnected {
      background: var(--err);
      box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7);
      animation: none;
    }
    .health-label {
      font-size: 0.78rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--err-bg);
      color: var(--err);
      border: 1px solid rgba(239, 68, 68, 0.35);
    }
    .segmented-control {
      display: inline-flex;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 2px;
      gap: 2px;
    }
    .seg-btn {
      background: transparent;
      border: 0;
      color: var(--muted);
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .seg-btn:hover { color: var(--text); }
    .seg-btn.active {
      background: var(--surface);
      color: var(--text);
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
    }
    .settings-menu-wrap {
      position: relative;
    }
    .settings-popover {
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      min-width: 260px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      box-shadow: 0 12px 28px rgba(0, 0, 0, 0.38);
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 30;
    }
    .settings-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-size: 0.85rem;
      color: var(--text);
      cursor: pointer;
    }
    .live-activity-shell {
      max-width: 1440px;
      margin: 12px auto 0 auto;
      padding: 0 24px;
    }
    .live-activity-banner {
      padding: 10px 16px;
      background: var(--info-bg);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }
    .live-activity-banner.stall {
      background: var(--warn-bg);
      border-color: rgba(245, 158, 11, 0.45);
    }
    .live-activity-main {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 240px;
      flex: 1;
    }
    .live-spinner-svg {
      width: 18px;
      height: 18px;
      flex-shrink: 0;
      color: var(--accent);
      animation: spin 0.9s linear infinite;
    }
    .live-activity-timer {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.84rem;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--accent);
    }
    .live-activity-stall {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.84rem;
      color: var(--warn);
      font-weight: 500;
    }
    .inline-op-feedback {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 0.82rem;
      color: var(--accent);
    }
    .project-card.delivered-card {
      padding: 10px 14px;
    }
    .tab-section-divider {
      margin: 22px 0 14px 0;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }
    .modify-panel-card {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span id="server-health-dot" class="live-dot" title="Sincronización activa con el servidor"></span>
      <span>Fábrica de Proyectos</span>
      <span id="server-health-label" class="health-label" aria-live="polite" style="display:none">Servidor desconectado</span>
      <span id="status-note" class="toast"></span>
    </div>
    <div class="controls">
      <div id="view-mode-segmented" class="segmented-control" role="group" aria-label="Modo técnico: Vista Simple o Vista Desarrollador" title="Modo técnico: alterna entre Vista Simple y Vista Desarrollador">
        <button id="view-mode-simple" type="button" class="seg-btn active" data-view-mode="simple" aria-label="Vista Simple">Simple</button>
        <button id="view-mode-dev" type="button" class="seg-btn" data-view-mode="dev" aria-label="Vista Desarrollador">Desarrollador</button>
        <input id="tech-mode-toggle" type="checkbox" class="sr-only" aria-label="Modo técnico" />
      </div>
      <div class="settings-menu-wrap">
        <button id="settings-menu-btn" class="btn btn-sm" type="button" aria-expanded="false" aria-controls="settings-popover">⚙️ Ajustes</button>
        <div id="settings-popover" class="settings-popover" style="display:none">
          <label class="settings-item" title="Permitir acceso desde celulares u otros equipos en tu red Wi-Fi local (0.0.0.0)">
            <span>Acceso Wi-Fi (celular)</span>
            <input id="lan-mode-toggle" type="checkbox" />
          </label>
          <label class="settings-item" title="Leer respuestas nuevas en voz alta en español">
            <span>Leer respuestas en voz alta</span>
            <input id="voice-tts-toggle" type="checkbox" />
          </label>
          <div class="settings-item" style="padding-top:6px; border-top:1px solid var(--border);">
            <span>Apariencia</span>
            <button id="theme-btn" class="btn btn-sm" type="button">Modo claro</button>
          </div>
        </div>
      </div>
    </div>
  </header>

  <div class="live-activity-shell">
    <div id="live-activity-banner" class="live-activity-banner" style="display:none" role="status" aria-live="polite">
      <div class="live-activity-main">
        <svg class="live-spinner-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
          <circle cx="12" cy="12" r="9" opacity="0.25"></circle>
          <path d="M21 12a9 9 0 0 0-9-9" stroke-linecap="round"></path>
        </svg>
        <div>
          <div id="live-activity-text" style="font-size:0.9rem; font-weight:600;">Procesando...</div>
          <div id="live-activity-subtext" class="muted" style="font-size:0.78rem;"></div>
        </div>
        <span id="live-activity-timer" class="live-activity-timer">00:00s</span>
      </div>
      <div id="live-activity-stall" class="live-activity-stall" style="display:none">
        <span>La operación está tardando más de lo esperado (puede seguir procesando o haberse bloqueado)</span>
        <button id="live-activity-reload-btn" class="btn btn-sm btn-warn" type="button">Reintentar / Recargar estado</button>
      </div>
    </div>
  </div>

  <main class="layout">
    <section class="panel">
      <div class="panel-header">
        <h2 style="margin:0; font-size:1.15rem;">Tus proyectos</h2>
        <div style="display:flex; gap:6px; flex-wrap:wrap;">
          <button id="toggle-import-project-btn" class="btn btn-sm" type="button">Importar proyecto</button>
          <button id="toggle-new-project-btn" class="btn btn-sm" type="button">+ Nuevo proyecto</button>
        </div>
      </div>
      <form id="new-project-form" class="new-project-form" style="display:none">
        <div class="input-row-with-mic">
          <textarea id="new-project-input" class="input-field" rows="3" placeholder="¿Qué quieres construir?" required></textarea>
          <button id="new-project-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar idea por voz (es-MX)">🎤</button>
        </div>
        <div class="new-project-actions">
          <button id="cancel-new-project-btn" class="btn btn-sm" type="button">Cancelar</button>
          <button id="submit-new-project-btn" class="btn btn-primary btn-sm" type="submit">Comenzar</button>
        </div>
      </form>
      <div id="project-list" class="muted" style="margin-top:14px;">Cargando proyectos...</div>
    </section>

    <div id="import-project-modal" class="modal-backdrop" style="display:none">
      <form id="import-project-form" class="modal-card">
        <h3 style="margin:0 0 8px 0;">Importar proyecto de esta computadora</h3>
        <p class="muted" style="font-size:0.86rem; margin:0 0 12px 0;">Ingresa la ruta local de una carpeta existente para auditarla, ejecutarla o modificarla sin copiar archivos.</p>
        <input id="import-path-input" class="input-field" type="text" placeholder="Ej: C:\\Users\\tu-usuario\\proyectos\\mi-app" required style="margin-bottom:8px;" />
        <input id="import-title-input" class="input-field" type="text" maxlength="80" placeholder="Título opcional (si lo dejas vacío se detecta del repo)" style="margin-bottom:12px;" />
        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <span id="import-inline-status"></span>
          <div style="display:flex; gap:8px;">
            <button id="cancel-import-project-btn" class="btn btn-sm" type="button">Cancelar</button>
            <button id="submit-import-project-btn" class="btn btn-primary btn-sm" type="submit">Importar</button>
          </div>
        </div>
      </form>
    </div>

    <section class="panel" id="detail-panel">
      <div id="detail-empty" class="empty-box">Selecciona un proyecto de la lista para ver su estado y detalles.</div>
      <div id="detail-content" style="display:none">
        <div id="detail-work-split" class="detail-work-split">
          <div id="detail-main-col">
            <div class="detail-header">
              <div class="detail-titles">
                <div class="title-row">
                  <img id="detail-logo-img" class="detail-logo" src="" alt="Logo del proyecto" style="display:none" />
                  <h2 id="detail-title" class="detail-title"></h2>
                  <button id="edit-title-btn" class="btn-icon" type="button" title="Editar título" aria-label="Editar título">✏️</button>
                  <button id="change-logo-btn" class="btn btn-sm" type="button" title="Subir logo propio">Cambiar logo</button>
                  <input id="logo-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml" style="display:none" />
                </div>
                <form id="edit-title-form" class="edit-title-form" style="display:none">
                  <input id="edit-title-input" class="input-field input-inline" type="text" maxlength="80" placeholder="Nombre del proyecto" required />
                  <button id="suggest-title-btn" class="btn btn-sm" type="button" title="Analizar archivos del repositorio y sugerir título">✨ Sugerir analizando repo</button>
                  <label id="rename-github-wrap" class="rename-github-check" style="display:none">
                    <input id="rename-github-checkbox" type="checkbox" checked />
                    <span>Renombrar también en GitHub</span>
                  </label>
                  <button id="save-title-btn" class="btn btn-primary btn-sm" type="submit">Guardar</button>
                  <button id="cancel-title-btn" class="btn btn-sm" type="button">Cancelar</button>
                </form>
              </div>
              <span id="detail-badge" class="badge"></span>
            </div>
            <div id="detail-segments" class="stage-segments"></div>
            <div id="detail-time" class="muted" style="font-size:0.78rem; margin:-2px 0 10px 0; text-align:right;"></div>

            <div id="detail-guidance"></div>

            <div class="tabs">
              <button id="tab-audit-btn" class="tab-btn" data-tab="audit" type="button" title="Consultar y Auditar proyecto" style="display:none">💬 Consultar y Auditar</button>
              <button id="tab-modify-btn" class="tab-btn" data-tab="modify" type="button" style="display:none">✨ Pedir cambios</button>
              <button id="tab-spec-btn" class="tab-btn active" data-tab="spec" type="button">📋 Resumen y Plan</button>
              <button id="tab-events-btn" class="tab-btn" data-tab="events" type="button">🕒 Historial</button>
            </div>
            <div id="tab-audit" class="tab-content" style="display:none"></div>
            <div id="tab-modify" class="tab-content" style="display:none"></div>
            <div id="tab-spec" class="tab-content"></div>
            <div id="tab-events" class="tab-content" style="display:none"></div>

            <div id="detail-secondary"></div>
          </div>
          <div id="live-preview-panel" class="preview-card" style="display:none">
            <div class="preview-toolbar">
              <strong style="font-size:0.88rem;">Vista previa visual</strong>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button id="reload-preview-btn" class="btn btn-sm" type="button">Recargar vista previa</button>
                <a id="open-preview-tab-link" href="#" target="_blank" rel="noopener noreferrer" class="btn btn-sm">Abrir en pestaña</a>
                <button id="close-preview-btn" class="btn btn-sm" type="button">Cerrar</button>
              </div>
            </div>
            <iframe id="live-preview-iframe" title="Vista previa en vivo" src="about:blank"></iframe>
          </div>
        </div>
      </div>
    </section>
  </main>

  <script>
    const state = {
      projects: [],
      selectedId: null,
      activeTab: 'spec',
      detail: null,
      replyDrafts: {},
      adjustDrafts: {},
      modifyDrafts: {},
      chatDrafts: {},
      chatHistories: {},
      chatLoadedFor: {},
      lastChatSig: null,
      checkpointsByPid: {},
      filesByPid: {},
      diffByPid: {},
      devMetaLoadedSig: {},
      targetFilesSelected: {},
      previewOpen: {},
      previewRenderedUrl: {},
      diffOpen: {},
      filesOpen: {},
      rollbackOpen: {},
      voiceTts: false,
      lastSpokenQuestionKey: null,
      editingTitlePid: null,
      adjustOpen: {},
      phaseOpen: {},
      priorOpen: {},
      userChoseTabByPid: {},
      lastBannerSignature: null,
      lastModifySignature: null,
      activeOp: null,
      bgOpPid: null,
      bgOpStartedAtMs: null,
      serverConnected: true,
    };
    let statusNoteTimer = null;

    function escapeHtml(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function stateToPhaseNum(code) {
      switch (code) {
        case 'CREATED':
        case 'IDLE':
        case 'INTERVIEW':
          return 1;
        case 'DEBATE':
        case 'MEDIATION':
          return 2;
        case 'WAITING_APPROVAL':
          return 3;
        case 'BUILDING':
        case 'VERIFYING':
          return 4;
        case 'DELIVERED':
        case 'COMPLETED':
          return 5;
        default:
          return 1;
      }
    }

    function findFailedPhaseNum(events) {
      if (!Array.isArray(events)) return 1;
      for (let i = events.length - 1; i >= 0; i--) {
        const ev = events[i];
        const pl = ev && ev.payload ? ev.payload : {};
        if (ev.type === 'PHASE_ERROR' && pl.state) {
          return stateToPhaseNum(pl.state);
        }
        if (ev.type === 'PROJECT_FAILED' && pl.failed_from) {
          return stateToPhaseNum(pl.failed_from);
        }
      }
      return 1;
    }

    function stepInfo(st, events) {
      switch (st) {
        case 'INTERVIEW':
        case 'CREATED':
        case 'IDLE':
          return { stage: 1, cardStep: 'Etapa 1 de 5', step: 'Etapa 1 de 5 · Definiendo detalles', cls: 'badge-info' };
        case 'DEBATE':
        case 'MEDIATION':
          return { stage: 2, cardStep: 'Etapa 2 de 5', step: 'Etapa 2 de 5 · Diseñando la solución', cls: 'badge-info' };
        case 'WAITING_APPROVAL':
          return { stage: 3, cardStep: 'Etapa 3 de 5', step: 'Etapa 3 de 5 · Esperando aprobación', cls: 'badge-warn' };
        case 'BUILDING':
        case 'VERIFYING':
          return { stage: 4, cardStep: 'Etapa 4 de 5', step: 'Etapa 4 de 5 · Construyendo y probando', cls: 'badge-info' };
        case 'DELIVERED':
        case 'COMPLETED':
          return { stage: 5, cardStep: '¡Terminado!', step: 'Etapa 5 de 5 · ¡Terminado!', cls: 'badge-ok' };
        case 'FAILED':
        case 'ERROR': {
          const fNum = findFailedPhaseNum(events);
          return { stage: fNum, cardStep: 'Necesita atención', step: 'Etapa ' + fNum + ' de 5 · Necesita atención', cls: 'badge-err' };
        }
        default:
          return { stage: 1, cardStep: st || 'Iniciando', step: 'Etapa 1 de 5 · ' + (st || 'Iniciando'), cls: 'badge-info' };
      }
    }

    function formatFriendlyTime(isoString) {
      if (!isoString) return '';
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return String(isoString);
      try {
        const timeStr = d.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' });
        const now = new Date();
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const targetDate = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        const diffDays = Math.round((today - targetDate) / (1000 * 60 * 60 * 24));
        if (diffDays === 0) {
          return 'Hoy, ' + timeStr;
        } else if (diffDays === 1) {
          return 'Ayer, ' + timeStr;
        } else {
          const datePart = d.toLocaleDateString('es-MX', { day: 'numeric', month: 'short' }).replace('.', '');
          return datePart + ', ' + timeStr;
        }
      } catch (_) {
        return String(isoString);
      }
    }

    function setNote(msg) {
      const el = document.getElementById('status-note');
      if (!el) return;
      if (statusNoteTimer) {
        clearTimeout(statusNoteTimer);
        statusNoteTimer = null;
      }
      el.textContent = msg || '';
      if (msg) {
        statusNoteTimer = setTimeout(() => {
          el.textContent = '';
          statusNoteTimer = null;
        }, 4000);
      }
    }

    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      const btn = document.getElementById('theme-btn');
      if (btn) {
        btn.textContent = theme === 'dark' ? 'Modo claro' : 'Modo oscuro';
      }
      try { localStorage.setItem('af_theme', theme); } catch (_) {}
    }

    (function initTheme() {
      let saved = 'dark';
      try {
        saved = localStorage.getItem('af_theme') ||
          (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      } catch (_) {}
      applyTheme(saved);
    })();

    document.getElementById('theme-btn').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });

    function applyTechMode(enabled) {
      const on = Boolean(enabled);
      document.body.classList.toggle('tech-mode', on);
      const cb = document.getElementById('tech-mode-toggle');
      if (cb) cb.checked = on;
      const simpleBtn = document.getElementById('view-mode-simple');
      const devBtn = document.getElementById('view-mode-dev');
      if (simpleBtn) {
        simpleBtn.classList.toggle('active', !on);
        simpleBtn.setAttribute('aria-pressed', !on ? 'true' : 'false');
      }
      if (devBtn) {
        devBtn.classList.toggle('active', on);
        devBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      try { localStorage.setItem('af_tech_mode', on ? '1' : '0'); } catch (_) {}
    }

    (function initTechMode() {
      let saved = false;
      try { saved = localStorage.getItem('af_tech_mode') === '1'; } catch (_) {}
      applyTechMode(saved);
    })();

    const techToggleEl = document.getElementById('tech-mode-toggle');
    if (techToggleEl) {
      techToggleEl.addEventListener('change', () => {
        applyTechMode(techToggleEl.checked);
      });
    }
    const simpleModeBtnEl = document.getElementById('view-mode-simple');
    if (simpleModeBtnEl) {
      simpleModeBtnEl.addEventListener('click', () => applyTechMode(false));
    }
    const devModeBtnEl = document.getElementById('view-mode-dev');
    if (devModeBtnEl) {
      devModeBtnEl.addEventListener('click', () => applyTechMode(true));
    }

    (function initSettingsPopover() {
      const btn = document.getElementById('settings-menu-btn');
      const pop = document.getElementById('settings-popover');
      if (!btn || !pop) return;
      function closeMenu() {
        pop.style.display = 'none';
        btn.setAttribute('aria-expanded', 'false');
      }
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const isOpen = pop.style.display !== 'none';
        if (isOpen) {
          closeMenu();
        } else {
          pop.style.display = 'flex';
          btn.setAttribute('aria-expanded', 'true');
        }
      });
      document.addEventListener('click', (ev) => {
        if (pop.style.display === 'none') return;
        if (!pop.contains(ev.target) && ev.target !== btn) {
          closeMenu();
        }
      });
      document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' && pop.style.display !== 'none') {
          closeMenu();
        }
      });
    })();

    function renderStageSegments(stageNum, mode) {
      const stage = Math.max(1, Math.min(5, Number(stageNum || 1)));
      const segs = [];
      for (let i = 1; i <= 5; i++) {
        let cls = '';
        if (mode === 'delivered') {
          cls = 'done';
        } else if (i < stage) {
          cls = 'done';
        } else if (i === stage) {
          cls = mode === 'error' ? 'error' : (mode === 'paused' ? 'warn' : 'active');
        }
        segs.push('<div class="stage-seg' + (cls ? (' ' + cls) : '') + '"></div>');
      }
      return '<div class="stage-segments" aria-label="Etapa ' + stage + ' de 5">' + segs.join('') + '</div>';
    }

    function sanitizeDisplaySummary(text) {
      if (text === null || text === undefined) return '';
      let s = String(text).trim();
      if (!s) return '';
      const fenceRe = /```json[\\s\\S]*?```/gi;
      const fenceMatches = s.match(fenceRe);
      if (fenceMatches && fenceMatches.length) {
        for (let idx = fenceMatches.length - 1; idx >= 0; idx--) {
          const inner = fenceMatches[idx].replace(/^```json\\s*/i, '').replace(/```$/, '').trim();
          try {
            const parsed = JSON.parse(inner);
            if (parsed && typeof parsed.summary === 'string' && parsed.summary.trim()) {
              s = parsed.summary.trim();
              break;
            }
          } catch (_) {}
        }
        s = s.replace(fenceRe, ' ').trim();
      }
      s = s.replace(/```json[\\s\\S]*/gi, ' ').replace(/```/g, ' ').trim();
      if (s.indexOf('files_written') !== -1 || (s.startsWith('{') && s.indexOf('"summary"') !== -1)) {
        try {
          const obj = JSON.parse(s);
          if (obj && typeof obj === 'object' && typeof obj.summary === 'string') {
            s = obj.summary.trim();
          } else {
            s = '';
          }
        } catch (_) {
          const m = /"summary"\\s*:\\s*"([^"]*)/i.exec(s);
          if (m && m[1] && m[1].trim()) {
            s = m[1].trim();
          } else {
            s = s.replace(/\\{[\\s\\S]*"(?:files_written|summary)"[\\s\\S]*$/i, '').trim();
          }
        }
      }
      return s.replace(/\\s+/g, ' ').trim();
    }

    function humanizeError(rawErr) {
      const txt = String(rawErr || '');
      if (/sin cambios/i.test(txt)) {
        return txt.split('\\n')[0].trim();
      }
      if (/ModuleNotFoundError|ImportError|No module named|librería externa/i.test(txt)) {
        return 'Las pruebas fallaron por una dependencia externa no instalada en el entorno local.';
      }
      if (/timed?\\s*out|timeout/i.test(txt)) {
        return 'La operación tardó más de lo esperado y superó el tiempo límite.';
      }
      if (/tamper|INV-13|zero test files/i.test(txt)) {
        return 'No se pudieron generar o validar correctamente las pruebas automáticas.';
      }
      if (/circuit breaker|max.*invocations|budget/i.test(txt)) {
        return 'Se alcanzó el límite máximo de intentos automáticos para proteger tus créditos.';
      }
      if (/rate\\s*limit|429|quota|capacity/i.test(txt)) {
        return 'El servicio de inteligencia artificial alcanzó un límite temporal. Intenta de nuevo en unos minutos.';
      }
      if (/exit code|command.*failed|subprocess/i.test(txt)) {
        return 'Ocurrió un fallo técnico al ejecutar la verificación automática.';
      }
      return 'El proceso se detuvo por un inconveniente técnico. Puedes volver a intentarlo con un clic.';
    }

    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.activeTab = btn.dataset.tab;
        if (state.selectedId) {
          state.userChoseTabByPid[state.selectedId] = state.activeTab;
        }
        renderTabs();
      });
    });

    function renderTabs() {
      document.querySelectorAll('.tab-btn').forEach((b) => {
        b.classList.toggle('active', b.dataset.tab === state.activeTab);
      });
      const specPane = document.getElementById('tab-spec');
      if (specPane) specPane.style.display = state.activeTab === 'spec' ? 'block' : 'none';
      const eventsPane = document.getElementById('tab-events');
      if (eventsPane) eventsPane.style.display = state.activeTab === 'events' ? 'block' : 'none';
      const auditPane = document.getElementById('tab-audit');
      if (auditPane) auditPane.style.display = state.activeTab === 'audit' ? 'block' : 'none';
      const modifyPane = document.getElementById('tab-modify');
      if (modifyPane) modifyPane.style.display = state.activeTab === 'modify' ? 'block' : 'none';
    }

    function speakSpanishText(rawText) {
      if (!state.voiceTts || !('speechSynthesis' in window) || typeof SpeechSynthesisUtterance === 'undefined') {
        return;
      }
      const clean = String(rawText || '')
        .replace(/```[\\s\\S]*?```/g, ' bloque de código ')
        .replace(/[#>*_`]/g, ' ')
        .replace(/\\s+/g, ' ')
        .trim();
      if (!clean) return;
      try {
        window.speechSynthesis.cancel();
        const utt = new SpeechSynthesisUtterance(clean.slice(0, 1500));
        utt.lang = 'es-MX';
        window.speechSynthesis.speak(utt);
      } catch (_) {}
    }

    function wireMicButton(btnOrId, inputOrId) {
      const btn = typeof btnOrId === 'string' ? document.getElementById(btnOrId) : btnOrId;
      const target = typeof inputOrId === 'string' ? document.getElementById(inputOrId) : inputOrId;
      if (!btn || !target) return;
      const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRec) {
        btn.style.display = 'none';
        return;
      }
      if (btn.dataset.micWired === '1') return;
      btn.dataset.micWired = '1';
      let activeRec = null;
      btn.addEventListener('click', () => {
        if (activeRec) {
          try { activeRec.stop(); } catch (_) {}
          return;
        }
        try {
          const rec = new SpeechRec();
          rec.lang = 'es-MX';
          rec.interimResults = false;
          rec.maxAlternatives = 1;
          activeRec = rec;
          btn.classList.add('listening');
          rec.onresult = (ev) => {
            const transcript = Array.from(ev.results || [])
              .map((r) => (r[0] ? r[0].transcript : ''))
              .join(' ')
              .trim();
            if (transcript) {
              const cur = (target.value || '').trim();
              target.value = cur ? (cur + ' ' + transcript) : transcript;
              target.dispatchEvent(new Event('input', { bubbles: true }));
              target.focus();
            }
          };
          rec.onerror = () => {
            setNote('No se pudo capturar audio con el micrófono.');
          };
          rec.onend = () => {
            btn.classList.remove('listening');
            activeRec = null;
          };
          rec.start();
        } catch (_) {
          btn.classList.remove('listening');
          activeRec = null;
          btn.style.display = 'none';
        }
      });
    }

    function downloadMarkdownFile(filename, content) {
      const safeName = String(filename || 'documento.md').replace(/[^a-zA-Z0-9._-]+/g, '-');
      const finalName = safeName.toLowerCase().endsWith('.md') ? safeName : (safeName + '.md');
      const blob = new Blob([String(content || '')], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = finalName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    async function apiFetch(url, options = {}) {
      const res = await fetch(url, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || ('HTTP ' + res.status));
      }
      return data;
    }

    function renderProjectList() {
      const container = document.getElementById('project-list');
      if (!state.projects.length) {
        container.innerHTML = '<div class="empty-box">No hay proyectos registrados todavía.</div>';
        return;
      }
      container.innerHTML = state.projects.map((p) => {
        const selected = p.project_id === state.selectedId ? 'selected' : '';
        const info = stepInfo(p.state);
        const title = escapeHtml(p.name || p.objective || p.project_id);
        const running = Boolean(p.is_running || p.busy);
        const pausedActive = !running && (
          p.state === 'DEBATE' || p.state === 'MEDIATION' ||
          p.state === 'BUILDING' || p.state === 'VERIFYING'
        );
        const statusTag = running
          ? ' <span class="busy-pill">(en curso)</span>'
          : (pausedActive ? ' <span class="busy-pill" style="color:var(--warn)">(pausado)</span>' : '');
        const badgeCls = pausedActive ? 'badge-warn' : info.cls;
        const isDeliveredCard = p.state === 'DELIVERED' || p.state === 'COMPLETED';
        const segMode = isDeliveredCard
          ? 'delivered'
          : ((p.state === 'FAILED' || p.state === 'ERROR') ? 'error' : (pausedActive ? 'paused' : 'active'));
        const stageBarHtml = isDeliveredCard ? '' : renderStageSegments(info.stage, segMode);
        const cardExtraCls = isDeliveredCard ? ' delivered-card' : '';
        const timeHtml = p.last_event_at
          ? '<div class="muted" style="font-size:0.75rem; margin-top:4px; text-align:right;" title="' + escapeHtml(p.last_event_at) + '">' + escapeHtml(formatFriendlyTime(p.last_event_at)) + '</div>'
          : '';
        const logoUrl = escapeHtml(p.logo_url || ('/api/projects/' + encodeURIComponent(p.project_id) + '/logo'));
        return '<div class="project-card ' + selected + cardExtraCls + '" data-id="' + escapeHtml(p.project_id) + '">' +
          '<div class="card-header">' +
            '<div class="card-title-wrap">' +
              '<img class="card-logo" src="' + logoUrl + '" alt="" />' +
              '<div class="card-title">' + title + statusTag + '</div>' +
            '</div>' +
            '<span class="badge ' + badgeCls + '">' + escapeHtml(info.cardStep || info.step) + '</span>' +
          '</div>' +
          stageBarHtml +
          timeHtml +
        '</div>';
      }).join('');

      container.querySelectorAll('.project-card').forEach((card) => {
        card.addEventListener('click', () => {
          state.selectedId = card.dataset.id;
          state.lastBannerSignature = null;
          state.lastModifySignature = null;
          state.lastChatSig = null;
          renderProjectList();
          loadDetail(state.selectedId);
        });
      });
    }

    function eventLabel(type) {
      const labels = {
        'PROJECT_CREATED': 'Proyecto creado',
        'STATE_TRANSITIONED': 'Cambio de estado',
        'INTERVIEW_ROUND': 'Ronda de entrevista',
        'SPEC_UPDATED': 'Especificación actualizada',
        'PRAGMATIST_PROPOSED': 'Propuesta de debate',
        'STRUCTURALIST_CRITIQUED': 'Crítica de debate',
        'PLAN_CREATED': 'Plan generado',
        'APPROVAL_RECORDED': 'Aprobación del plan',
        'TESTS_REUSED': 'Pruebas existentes reutilizadas',
        'TESTS_UPDATED': 'Pruebas actualizadas',
        'AGENT_INVOKED': 'Agente invocado',
        'CI_ROUND_COMPLETED': 'Verificación CI completada',
        'PROJECT_FAILED': 'Detenido por error',
        'PROJECT_RETRIED': 'Reanudación iniciada',
        'PHASE_ERROR': 'Error en la etapa',
        'PROJECT_DELIVERED': '¡Proyecto entregado con éxito!',
        'REGISTRY_PUBLISH_FAILED': 'Error al publicar registro',
      };
      return labels[type] || type;
    }

    function friendlyEventAction(ev) {
      const pl = ev && ev.payload ? ev.payload : {};
      switch (ev.type) {
        case 'PROJECT_CREATED':
          return 'Proyecto creado e iniciado';
        case 'PROJECT_RENAMED':
          return pl.name ? ('Título actualizado a "' + pl.name + '"') : 'Título actualizado';
        case 'INTERVIEW_ROUND':
          return 'Preguntas y respuestas de requisitos';
        case 'SPEC_UPDATED':
          return 'Especificación del proyecto guardada';
        case 'PRAGMATIST_PROPOSED':
          return 'Propuesta del especialista';
        case 'STRUCTURALIST_CRITIQUED':
          return 'Crítica de estructura';
        case 'PLAN_CREATED':
          return 'Plan técnico unificado';
        case 'APPROVAL_RECORDED':
          return 'Plan aprobado por ti';
        case 'ADJUSTMENT_REQUESTED':
          return 'Ajuste solicitado al plan';
        case 'AGENT_INVOKED':
          if (pl.agent === 'interviewer') return 'Analizando idea y preparando preguntas';
          if (pl.agent === 'generate_tests') return 'Creando pruebas automáticas';
          if (pl.agent === 'update_tests') return 'Actualizando pruebas automáticas';
          if (pl.agent === 'implement_or_fix') return 'Escribiendo código';
          if (pl.state === 'VERIFYING') return 'Verificando funcionamiento';
          if (pl.state === 'DEBATE') return 'Evaluación de especialistas';
          if (pl.state === 'MEDIATION') return 'Diseñando plan técnico';
          return 'Trabajando en la construcción';
        case 'TESTS_REUSED':
          return 'Se reutilizaron las pruebas automáticas existentes';
        case 'TESTS_UPDATED':
          return (pl.total !== undefined)
            ? ('Verificando funcionamiento (' + (pl.passed || 0) + '/' + pl.total + ' pruebas)')
            : 'Verificando funcionamiento';
        case 'CI_ROUND_COMPLETED':
          return 'Verificando funcionamiento en CI';
        case 'PROJECT_RETRIED':
          return pl.workspace_cleaned ? 'Reconstrucción desde cero iniciada' : 'Reintento iniciado';
        case 'PHASE_ERROR':
          return 'Error en la etapa';
        case 'PROJECT_FAILED':
          return 'Detenido por error';
        case 'PROJECT_DELIVERED':
        case 'PROJECT_COMPLETED':
        case 'COMPLETED':
          return '¡Proyecto entregado con éxito!';
        case 'REGISTRY_PUBLISH_FAILED':
          return 'Aviso al publicar registro';
        case 'STATE_TRANSITIONED':
          return pl.to ? ('Avanzó a ' + friendlyStateName(pl.to)) : 'Cambio de etapa';
        default:
          return eventLabel(ev.type);
      }
    }

    function phaseNumForEvent(ev) {
      const pl = ev && ev.payload ? ev.payload : {};
      switch (ev.type) {
        case 'PROJECT_CREATED':
        case 'INTERVIEW_ROUND':
        case 'SPEC_UPDATED':
          return 1;
        case 'PRAGMATIST_PROPOSED':
        case 'STRUCTURALIST_CRITIQUED':
        case 'PLAN_CREATED':
          return 2;
        case 'APPROVAL_RECORDED':
        case 'ADJUSTMENT_REQUESTED':
          return 3;
        case 'TESTS_REUSED':
        case 'TESTS_UPDATED':
        case 'CI_ROUND_COMPLETED':
          return 4;
        case 'PROJECT_RETRIED':
          return pl.to ? stateToPhaseNum(pl.to) : 4;
        case 'PROJECT_DELIVERED':
        case 'PROJECT_COMPLETED':
        case 'COMPLETED':
        case 'REGISTRY_PUBLISH_FAILED':
          return 5;
        case 'AGENT_INVOKED':
          if (pl.agent === 'interviewer' || pl.state === 'INTERVIEW') return 1;
          if (pl.state === 'DEBATE' || pl.state === 'MEDIATION') return 2;
          if (pl.state === 'DELIVERED') return 5;
          return 4;
        case 'STATE_TRANSITIONED':
          if (pl.from === 'WAITING_APPROVAL' && pl.to === 'INTERVIEW') return 3;
          if (pl.to === 'DEBATE' || pl.to === 'MEDIATION' || pl.to === 'WAITING_APPROVAL') return 2;
          if (pl.to === 'BUILDING' || pl.to === 'VERIFYING') return 4;
          if (pl.to === 'DELIVERED' || pl.to === 'COMPLETED') return 5;
          return 1;
        case 'PHASE_ERROR':
          return pl.state ? stateToPhaseNum(pl.state) : 4;
        case 'PROJECT_FAILED':
          return pl.failed_from ? stateToPhaseNum(pl.failed_from) : 4;
        default:
          return 1;
      }
    }

    function computePhaseDuration(phaseNum, phaseItems, allItems) {
      if (!phaseItems.length) return '';
      if (phaseNum === 3) {
        const approvalItem = phaseItems.find((it) => it.ev.type === 'APPROVAL_RECORDED');
        const planItem = allItems.find((it) => it.ev.type === 'PLAN_CREATED');
        if (approvalItem && planItem && approvalItem.ev.at && planItem.ev.at) {
          const span = (Date.parse(approvalItem.ev.at) - Date.parse(planItem.ev.at)) / 1000;
          if (isFinite(span) && span >= 0) {
            return formatDurationHuman(span, '< 1s');
          }
        }
      }
      const firstAt = phaseItems[0].ev.at ? Date.parse(phaseItems[0].ev.at) : NaN;
      const lastAt = phaseItems[phaseItems.length - 1].ev.at ? Date.parse(phaseItems[phaseItems.length - 1].ev.at) : NaN;
      if (isFinite(firstAt) && isFinite(lastAt) && lastAt > firstAt) {
        return formatDurationHuman((lastAt - firstAt) / 1000, '< 1s');
      }
      let sum = 0;
      let sawDur = false;
      let sawPragDur = false;
      phaseItems.forEach((it) => {
        const pl = it.ev.payload || {};
        if (typeof pl.duration_seconds === 'number' && isFinite(pl.duration_seconds)) {
          if (it.ev.type === 'STRUCTURALIST_CRITIQUED' && sawPragDur) return;
          if (it.ev.type === 'PRAGMATIST_PROPOSED') sawPragDur = true;
          sum += Math.max(0, pl.duration_seconds);
          sawDur = true;
        }
      });
      return sawDur ? formatDurationHuman(sum, '< 1s') : '< 1s';
    }

    function extractLatestQuestions(events) {
      if (!Array.isArray(events)) return [];
      for (let i = events.length - 1; i >= 0; i--) {
        const ev = events[i];
        if (ev.type === 'STATE_TRANSITIONED' && ev.payload && ev.payload.to === 'INTERVIEW') {
          return [];
        }
        if (ev.type === 'INTERVIEW_ROUND' && ev.payload) {
          return Array.isArray(ev.payload.questions) ? ev.payload.questions : [];
        }
      }
      return [];
    }

    function formatInactivityNote(lastEventAt) {
      if (!lastEventAt) return '';
      const lastMs = new Date(lastEventAt).getTime();
      if (isNaN(lastMs)) return '';
      const elapsedMins = (Date.now() - lastMs) / 60000;
      if (elapsedMins >= 120) {
        return 'Sin actividad desde hace varias horas.';
      }
      if (elapsedMins > 30) {
        return 'Sin actividad desde hace más de 30 minutos.';
      }
      return '';
    }

    function friendlyStateName(code) {
      const names = {
        'CREATED': 'Inicio del proyecto',
        'IDLE': 'En espera',
        'INTERVIEW': 'Entrevista inicial',
        'DEBATE': 'Evaluación técnica',
        'MEDIATION': 'Diseño del plan',
        'WAITING_APPROVAL': 'Revisión y aprobación del plan',
        'BUILDING': 'Construcción de la aplicación',
        'VERIFYING': 'Pruebas y verificación',
        'DELIVERED': 'Proyecto terminado y entregado',
        'CHANGE_REQUEST': 'Preparando cambios',
        'COMPLETED': 'Proyecto terminado',
        'FAILED': 'Detenido por error',
      };
      return names[code] || String(code || '');
    }

    function formatDurationHuman(totalSeconds, subSecondLabel) {
      if (typeof totalSeconds !== 'number' || !isFinite(totalSeconds) || totalSeconds < 0) {
        return '';
      }
      if (totalSeconds < 1) {
        return subSecondLabel || '< 1s';
      }
      const rounded = Math.round(totalSeconds);
      if (rounded < 60) {
        return rounded + 's';
      }
      let mins = Math.floor(rounded / 60);
      const secs = rounded % 60;
      if (mins < 60) {
        return secs > 0 ? (mins + ' min ' + secs + 's') : (mins + ' min');
      }
      const hours = Math.floor(mins / 60);
      mins = mins % 60;
      return mins > 0 ? (hours + ' h ' + mins + ' min') : (hours + ' h');
    }

    function formatElapsedSince(startedIso) {
      if (!startedIso) return '';
      const ms = Date.parse(startedIso);
      if (!isFinite(ms)) return '';
      const elapsedSecs = Math.max(0, Math.floor((Date.now() - ms) / 1000));
      return formatDurationHuman(elapsedSecs, '0s') || '0s';
    }

    function formatClockMmSs(totalSeconds) {
      const secs = Math.max(0, Math.floor(Number(totalSeconds || 0)));
      const mm = String(Math.floor(secs / 60)).padStart(2, '0');
      const ss = String(secs % 60).padStart(2, '0');
      return mm + ':' + ss + 's';
    }

    function updateServerHealth(connected) {
      state.serverConnected = Boolean(connected);
      const dot = document.getElementById('server-health-dot');
      const lbl = document.getElementById('server-health-label');
      if (dot) {
        dot.classList.toggle('disconnected', !state.serverConnected);
        dot.title = state.serverConnected ? 'Servidor conectado' : 'Servidor desconectado';
      }
      if (lbl) {
        lbl.style.display = state.serverConnected ? 'none' : 'inline-block';
        lbl.textContent = 'Servidor desconectado';
      }
    }

    function renderLiveActivityBanner() {
      const banner = document.getElementById('live-activity-banner');
      const textEl = document.getElementById('live-activity-text');
      const subEl = document.getElementById('live-activity-subtext');
      const timerEl = document.getElementById('live-activity-timer');
      const stallEl = document.getElementById('live-activity-stall');
      if (!banner || !textEl || !subEl || !timerEl || !stallEl) return;

      let label = '';
      let subtext = '';
      let startedAtMs = null;
      if (state.activeOp && state.activeOp.startedAtMs) {
        label = state.activeOp.label || 'Procesando operación...';
        subtext = state.activeOp.subtext || '';
        startedAtMs = state.activeOp.startedAtMs;
      } else if (state.detail && state.detail.status && (state.detail.status.is_running || state.detail.status.busy)) {
        const st = state.detail.status;
        label = st.activity_summary || friendlyStateName(st.state) || 'Procesando...';
        subtext = st.activity_detail || '';
        const parsedMs = st.step_started_at ? Date.parse(st.step_started_at) : NaN;
        if (isFinite(parsedMs)) {
          startedAtMs = parsedMs;
        } else {
          if (state.bgOpPid !== st.project_id || !state.bgOpStartedAtMs) {
            state.bgOpPid = st.project_id;
            state.bgOpStartedAtMs = Date.now();
          }
          startedAtMs = state.bgOpStartedAtMs;
        }
      } else {
        state.bgOpPid = null;
        state.bgOpStartedAtMs = null;
      }

      if (!startedAtMs) {
        banner.style.display = 'none';
        banner.classList.remove('stall');
        stallEl.style.display = 'none';
        timerEl.textContent = '00:00s';
        return;
      }

      const elapsedSecs = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
      banner.style.display = 'flex';
      textEl.textContent = label;
      subEl.textContent = subtext;
      timerEl.textContent = formatClockMmSs(elapsedSecs);
      const stalled = elapsedSecs >= 75;
      banner.classList.toggle('stall', stalled);
      stallEl.style.display = stalled ? 'flex' : 'none';
    }

    async function runTrackedOperation(opts, asyncFn) {
      const cfg = opts || {};
      const btn = typeof cfg.buttonEl === 'string'
        ? document.getElementById(cfg.buttonEl)
        : cfg.buttonEl;
      const inlineEl = cfg.inlineTargetId ? document.getElementById(cfg.inlineTargetId) : null;
      if (btn) btn.disabled = true;
      if (inlineEl) {
        inlineEl.innerHTML =
          '<span class="inline-op-feedback">' +
            '<span class="working-spinner" style="width:14px;height:14px;" aria-hidden="true"></span>' +
            '<span>' + escapeHtml(cfg.label || 'Procesando...') + '</span>' +
          '</span>';
      }
      state.activeOp = {
        label: cfg.label || 'Procesando...',
        subtext: cfg.subtext || '',
        startedAtMs: Date.now(),
        retryFn: cfg.retryFn || null,
      };
      renderLiveActivityBanner();
      try {
        return await asyncFn();
      } finally {
        state.activeOp = null;
        renderLiveActivityBanner();
        const inlineAfter = cfg.inlineTargetId ? document.getElementById(cfg.inlineTargetId) : null;
        if (inlineAfter) inlineAfter.innerHTML = '';
        const btnAfter = typeof cfg.buttonEl === 'string'
          ? document.getElementById(cfg.buttonEl)
          : cfg.buttonEl;
        if (btnAfter && document.body.contains(btnAfter)) {
          btnAfter.disabled = false;
        }
      }
    }

    (function initActivityReloadBtn() {
      const reloadBtn = document.getElementById('live-activity-reload-btn');
      if (!reloadBtn) return;
      reloadBtn.addEventListener('click', () => {
        state.activeOp = null;
        state.bgOpStartedAtMs = null;
        renderLiveActivityBanner();
        setNote('Recargando estado del servidor...');
        loadProjects();
      });
    })();

    function updateLiveTimers() {
      document.querySelectorAll('.live-step-timer[data-started-at]').forEach((el) => {
        const startedAt = el.getAttribute('data-started-at');
        const formatted = formatElapsedSince(startedAt);
        if (formatted) {
          el.textContent = 'Tiempo en este paso: ' + formatted;
        }
      });
      renderLiveActivityBanner();
    }

    function buildGuidanceBanner(st, events) {
      const isRunning = Boolean(st.is_running || st.busy);
      if (isRunning) {
        const descText = st.activity_summary
          ? escapeHtml(st.activity_summary)
          : 'Los agentes están avanzando en esta etapa. Esta pantalla se actualiza sola cada 5 segundos.';
        const subDescHtml = st.activity_detail
          ? '<div class="banner-subdesc">' + escapeHtml(st.activity_detail) + '</div>'
          : '';
        const hasTests = typeof st.tests_passed === 'number' && typeof st.tests_total === 'number' && st.tests_total > 0;
        const testsPending = hasTests && st.tests_passed < st.tests_total;
        const testsHtml = hasTests
          ? '<div class="banner-tests' + (testsPending ? ' pending' : '') + '">' +
              (testsPending ? '' : '✓ ') + st.tests_passed + ' de ' + st.tests_total + ' pruebas aprobadas' +
            '</div>'
          : '';
        const agentsCount = Number(st.agents_count || 1);
        const agentsText = agentsCount === 2
          ? '2 agentes colaborando'
          : (agentsCount > 1 ? (agentsCount + ' agentes colaborando') : '1 agente trabajando');
        const agentsPill = '<span class="model-pill">' + escapeHtml(agentsText) + '</span>';
        const modelPill = st.active_model
          ? '<span class="model-pill">Modelo: ' + escapeHtml(st.active_model) + '</span>'
          : '';
        const elapsedText = st.step_started_at ? formatElapsedSince(st.step_started_at) : '';
        const timerPill = st.step_started_at
          ? '<span class="model-pill live-step-timer" data-started-at="' + escapeHtml(st.step_started_at) + '">Tiempo en este paso: ' + escapeHtml(elapsedText || '0s') + '</span>'
          : '';
        return '<div class="guidance-banner banner-info">' +
          '<div class="banner-content">' +
            '<div class="banner-icon"><span class="working-spinner" aria-hidden="true"></span></div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Trabajando automáticamente</div>' +
              '<div class="banner-desc">' + descText + '</div>' +
              subDescHtml +
              testsHtml +
              '<div class="model-usage-row">' + agentsPill + modelPill + timerPill + '</div>' +
              '<div class="banner-calm">Es normal que tome varias rondas. Te avisaremos si necesita tu ayuda.</div>' +
            '</div>' +
          '</div>' +
        '</div>';
      }

      if (st.state === 'INTERVIEW') {
        const questions = extractLatestQuestions(events);
        const qHtml = questions.length
          ? '<ul class="question-list">' + questions.map((q) => '<li>' + escapeHtml(q) + '</li>').join('') + '</ul>'
          : '<div class="banner-desc">Cuéntanos los detalles que deseas incluir para completar la especificación.</div>';
        const inact = formatInactivityNote(st.last_event_at);
        const inactHtml = inact ? '<div class="inactivity-note">' + escapeHtml(inact) + '</div>' : '';
        const draft = escapeHtml((state.replyDrafts && state.replyDrafts[st.project_id]) || '');
        return '<div class="guidance-banner banner-info">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">💬</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Esperando tus respuestas</div>' +
              qHtml +
              inactHtml +
            '</div>' +
          '</div>' +
          '<div class="banner-form">' +
            '<div class="input-row-with-mic">' +
              '<textarea id="interview-reply-input" class="input-field" rows="3" placeholder="Escribe tu respuesta aquí...">' + draft + '</textarea>' +
              '<button id="interview-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar respuesta por voz (es-MX)">🎤</button>' +
            '</div>' +
            '<div class="banner-actions">' +
              '<button id="interview-reply-btn" class="btn btn-primary" type="button">Enviar respuesta</button>' +
            '</div>' +
          '</div>' +
        '</div>';
      }

      if (st.state === 'WAITING_APPROVAL') {
        const adjustDraft = escapeHtml((state.adjustDrafts && state.adjustDrafts[st.project_id]) || '');
        const adjustOpenAttr = (state.adjustOpen && state.adjustOpen[st.project_id]) ? ' open' : '';
        return '<div class="guidance-banner banner-warn">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">📋</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">El plan está listo para revisión</div>' +
              '<div class="banner-desc">El plan está listo. Revisa qué se va a construir y pulsa el botón para iniciar.</div>' +
            '</div>' +
          '</div>' +
          '<div class="banner-actions">' +
            '<button class="btn btn-success" data-dact="approve">Aprobar y construir</button>' +
          '</div>' +
          '<details id="adjust-plan-details" class="adjust-details"' + adjustOpenAttr + '>' +
            '<summary>Pedir un ajuste al plan</summary>' +
            '<div class="banner-form" style="margin-top:8px;">' +
              '<textarea id="adjust-plan-input" class="input-field" rows="2" placeholder="Describe qué quieres ajustar o agregar al plan...">' + adjustDraft + '</textarea>' +
              '<div class="banner-actions">' +
                '<button id="adjust-plan-btn" class="btn btn-sm" type="button">Enviar ajuste</button>' +
              '</div>' +
            '</div>' +
          '</details>' +
        '</div>';
      }

      if (
        st.state === 'DEBATE' || st.state === 'MEDIATION' ||
        st.state === 'BUILDING' || st.state === 'VERIFYING'
      ) {
        return '<div class="guidance-banner banner-warn">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">⏸️</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Proceso pausado</div>' +
              '<div class="banner-desc">El proceso se detuvo al reiniciar la aplicación.</div>' +
            '</div>' +
          '</div>' +
          '<div class="banner-actions">' +
            '<button class="btn btn-warn" data-dact="retry">Reanudar proyecto</button>' +
            '<button class="btn btn-subtle-danger btn-sm" data-dact="retry-clean">Reconstruir desde cero</button>' +
          '</div>' +
        '</div>';
      }

      if (st.state === 'FAILED' || st.state === 'ERROR') {
        const friendlyErr = humanizeError(st.last_error);
        return '<div class="guidance-banner banner-err">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">⚠️</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Se requiere atención</div>' +
              '<div class="banner-desc">' + escapeHtml(friendlyErr) + '</div>' +
              '<details class="tech-details tech-only">' +
                '<summary>Ver detalle técnico</summary>' +
                '<pre>' + escapeHtml(st.last_error || 'Sin detalle técnico disponible.') + '</pre>' +
              '</details>' +
            '</div>' +
          '</div>' +
          '<div class="banner-actions">' +
            '<button class="btn btn-warn" data-dact="retry">Volver a intentar</button>' +
            '<button class="btn btn-subtle-danger btn-sm" data-dact="retry-clean">Reconstruir desde cero</button>' +
          '</div>' +
        '</div>';
      }

      if (st.state === 'DELIVERED' || st.state === 'COMPLETED') {
        const pid = st.project_id;
        const localApp = st.local_app || { running: false, url: null, app_type: 'unknown' };
        const isPrevOpen = Boolean(state.previewOpen && state.previewOpen[pid] && localApp.running && localApp.url);
        const repoBtn = (st.links && st.links.repo)
          ? '<a href="' + escapeHtml(st.links.repo) + '" target="_blank" rel="noopener noreferrer" class="btn">Abrir repositorio en GitHub</a>'
          : '';
        const deployBtn = (localApp.app_type === 'static' && st.links && st.links.deploy)
          ? '<a href="' + escapeHtml(st.links.deploy) + '" target="_blank" rel="noopener noreferrer" class="btn">Ver despliegue en vivo</a>'
          : '';
        const lanBtn = (localApp.running && localApp.lan_url)
          ? '<a href="' + escapeHtml(localApp.lan_url) + '" target="_blank" rel="noopener noreferrer" class="btn btn-sm">Abrir desde celular</a>' +
            '<span class="model-pill">' + escapeHtml(localApp.lan_url) + '</span>'
          : '';
        const prevBtn = (localApp.running && localApp.url)
          ? '<button id="toggle-preview-btn" class="btn btn-sm" type="button" data-dact="toggle-preview">' +
            (isPrevOpen ? 'Ocultar vista previa' : 'Mostrar vista previa aquí') + '</button>'
          : '';
        const localBtns = (localApp.running && localApp.url)
          ? '<a href="' + escapeHtml(localApp.url) + '" target="_blank" rel="noopener noreferrer" class="btn btn-success">Abrir app en local</a>' +
            prevBtn +
            '<button class="btn btn-warn" data-dact="local-stop">Apagar</button>' +
            '<span class="model-pill tech-only">' + escapeHtml(localApp.url) + '</span>' +
            lanBtn
          : '<button class="btn btn-primary" data-dact="local-start">Encender en local</button>';
        const descMsg = localApp.app_type === 'python_local'
          ? 'Proyecto local con servidor Python listo para ejecutarse en tu computadora.'
          : 'El proyecto ha sido construido y verificado exitosamente.';
        return '<div class="guidance-banner banner-ok">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">🎉</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">¡Proyecto terminado con éxito!</div>' +
              '<div class="banner-desc">' + escapeHtml(descMsg) + '</div>' +
            '</div>' +
          '</div>' +
          '<div class="banner-actions">' +
            '<div class="local-controls-row">' + localBtns + '</div>' +
            repoBtn + deployBtn +
          '</div>' +
          '<div id="local-inline-status"></div>' +
        '</div>';
      }

      return '<div class="guidance-banner">' +
        '<div class="banner-content">' +
          '<div class="banner-body">' +
            '<div class="banner-title">Estado actual: ' + escapeHtml(st.state) + '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
    }

    function splitProjectAttempts(events) {
      if (!Array.isArray(events) || !events.length) {
        return { activeEvents: [], priorEvents: [], priorGroups: [] };
      }
      let lastRetryIdx = -1;
      for (let i = events.length - 1; i >= 0; i--) {
        if (events[i].type === 'PROJECT_RETRIED') {
          lastRetryIdx = i;
          break;
        }
      }
      if (lastRetryIdx < 0) {
        return { activeEvents: events, priorEvents: [], priorGroups: [] };
      }
      const targetPhase = phaseNumForEvent(events[lastRetryIdx]);
      const activeEvents = [];
      const priorEvents = [];
      for (let i = 0; i < events.length; i++) {
        const ev = events[i];
        if (i < lastRetryIdx) {
          const evPhase = phaseNumForEvent(ev);
          if (
            evPhase >= targetPhase ||
            ev.type === 'PHASE_ERROR' ||
            ev.type === 'PROJECT_FAILED' ||
            ev.type === 'PROJECT_RETRIED'
          ) {
            priorEvents.push(ev);
            continue;
          }
        }
        activeEvents.push(ev);
      }
      const priorGroups = [];
      let currentChunk = [];
      priorEvents.forEach((ev) => {
        if (ev.type === 'PROJECT_RETRIED' && currentChunk.length) {
          priorGroups.push(currentChunk);
          currentChunk = [ev];
        } else {
          currentChunk.push(ev);
        }
      });
      if (currentChunk.length) {
        priorGroups.push(currentChunk);
      }
      return { activeEvents, priorEvents, priorGroups };
    }

    function explicitEventDurationSeconds(ev) {
      const pl = ev && ev.payload ? ev.payload : null;
      if (pl && typeof pl.duration_seconds === 'number' && isFinite(pl.duration_seconds) && pl.duration_seconds >= 0) {
        return pl.duration_seconds;
      }
      return null;
    }

    function failedRoundRatio(entry) {
      if (!entry || entry.kind !== 'round' || entry.outcome === 'reverted_tamper') return '';
      const tpl = entry.testsEv && entry.testsEv.payload ? entry.testsEv.payload : null;
      if (!tpl || typeof tpl.total !== 'number' || tpl.total <= 0) return '';
      const passed = Number(tpl.passed || 0);
      if (passed >= tpl.total) return '';
      return passed + '/' + tpl.total;
    }

    function pairAndGroupPhaseEvents(rawEvents) {
      const items = [];
      let roundCounter = 0;
      let i = 0;
      while (i < rawEvents.length) {
        const ev = rawEvents[i];
        const pl = ev && ev.payload ? ev.payload : {};
        if (ev.type === 'STATE_TRANSITIONED') {
          items.push({ kind: 'tech_only', ev });
          i++;
          continue;
        }
        if (ev.type === 'PHASE_ERROR') {
          let k = i + 1;
          const techBetween = [];
          while (k < rawEvents.length && rawEvents[k].type === 'STATE_TRANSITIONED') {
            techBetween.push({ kind: 'tech_only', ev: rawEvents[k] });
            k++;
          }
          if (k < rawEvents.length && rawEvents[k].type === 'PROJECT_FAILED') {
            const failEv = rawEvents[k];
            const fpl = failEv.payload || {};
            const rawErr = fpl.reason || pl.error || '';
            items.push({
              kind: 'fused_error',
              ev: failEv,
              causeEv: ev,
              rawError: rawErr,
              msg: humanizeError(rawErr),
              durSecs: explicitEventDurationSeconds(failEv) ?? explicitEventDurationSeconds(ev),
            });
            techBetween.forEach((t) => items.push(t));
            i = k + 1;
            continue;
          }
          const rawErr = pl.error || '';
          items.push({
            kind: 'fused_error',
            ev,
            rawError: rawErr,
            msg: humanizeError(rawErr),
            durSecs: explicitEventDurationSeconds(ev),
          });
          i++;
          continue;
        }
        if (ev.type === 'PROJECT_FAILED') {
          const rawErr = pl.reason || pl.error || '';
          items.push({
            kind: 'fused_error',
            ev,
            rawError: rawErr,
            msg: humanizeError(rawErr),
            durSecs: explicitEventDurationSeconds(ev),
          });
          i++;
          continue;
        }
        if (ev.type === 'AGENT_INVOKED' && pl.agent === 'generate_tests') {
          const files = Array.isArray(pl.files_written) ? pl.files_written.slice(0, 10) : [];
          items.push({
            kind: 'gen_tests',
            ev,
            filesWritten: files,
            summaryShort: pl.summary_short ? String(pl.summary_short) : '',
            durSecs: explicitEventDurationSeconds(ev),
          });
          i++;
          continue;
        }
        if (ev.type === 'AGENT_INVOKED' && (pl.agent === 'implement_or_fix' || pl.round !== undefined)) {
          roundCounter++;
          const roundNum = (typeof pl.round === 'number' && pl.round > 0) ? pl.round : roundCounter;
          const maxRounds = (typeof pl.max_rounds === 'number' && pl.max_rounds > 0) ? pl.max_rounds : null;
          let testsEv = null;
          let ciEv = null;
          const deferredTech = [];
          let j = i + 1;
          while (j < rawEvents.length) {
            const nxt = rawEvents[j];
            if (nxt.type === 'STATE_TRANSITIONED') {
              deferredTech.push({ kind: 'tech_only', ev: nxt });
              j++;
              continue;
            }
            if (nxt.type === 'TESTS_UPDATED' && !testsEv) {
              testsEv = nxt;
              j++;
              continue;
            }
            if (nxt.type === 'CI_ROUND_COMPLETED' && !ciEv) {
              ciEv = nxt;
              j++;
              continue;
            }
            break;
          }
          let durSum = 0;
          let hasDur = false;
          [ev, testsEv, ciEv].forEach((part) => {
            if (!part) return;
            const d = explicitEventDurationSeconds(part);
            if (d !== null) {
              durSum += d;
              hasDur = true;
            }
          });
          const files = Array.isArray(pl.files_written) ? pl.files_written.slice(0, 10) : [];
          items.push({
            kind: 'round',
            ev,
            testsEv,
            ciEv,
            roundNum,
            maxRounds,
            filesWritten: files,
            summaryShort: pl.summary_short ? String(pl.summary_short) : '',
            outcome: pl.outcome ? String(pl.outcome) : '',
            failingTests: Array.isArray(pl.failing_tests) ? pl.failing_tests : [],
            durSecs: hasDur ? durSum : null,
          });
          deferredTech.forEach((t) => items.push(t));
          i = j;
          continue;
        }
        items.push({
          kind: 'step',
          ev,
          durSecs: explicitEventDurationSeconds(ev),
        });
        i++;
      }

      // Collapse intermediate repeated failed rounds with identical test counts
      const collapsed = [];
      let idx = 0;
      while (idx < items.length) {
        const ratio = failedRoundRatio(items[idx]);
        if (!ratio) {
          collapsed.push(items[idx]);
          idx++;
          continue;
        }
        let end = idx;
        while (end + 1 < items.length && failedRoundRatio(items[end + 1]) === ratio) {
          end++;
        }
        const keepFirst = idx === 0 ? 1 : 0;
        const collStart = idx + keepFirst;
        if (keepFirst) {
          collapsed.push(items[idx]);
        }
        if (end - collStart + 1 >= 2) {
          collapsed.push({
            kind: 'stalled_group',
            rounds: items.slice(collStart, end + 1),
            ratio,
          });
        } else {
          for (let c = collStart; c <= end; c++) {
            collapsed.push(items[c]);
          }
        }
        idx = end + 1;
      }
      return collapsed;
    }

    function renderFileChips(files) {
      if (!Array.isArray(files) || !files.length) return '';
      return '<div class="file-chips">' + files.map((f) =>
        '<span class="file-chip">' + escapeHtml(f) + '</span>'
      ).join('') + '</div>';
    }

    function renderTimelineEntry(item) {
      if (item.kind === 'stalled_group') {
        return '<div class="timeline-item">' +
          '<div class="timeline-dot warn"></div>' +
          '<details class="tech-details" style="margin-top:0; border-top:none; padding-top:0;">' +
            '<summary>' + item.rounds.length + ' rondas intermedias con mismo resultado (' + escapeHtml(item.ratio) + ' pruebas)</summary>' +
            '<div style="margin-top:8px;">' + item.rounds.map((r) => renderTimelineEntry(r)).join('') + '</div>' +
          '</details>' +
        '</div>';
      }

      const ev = item.ev;
      const pl = ev && ev.payload ? ev.payload : {};
      const rawDate = ev && ev.at ? String(ev.at) : '';
      const friendlyDate = rawDate ? escapeHtml(formatFriendlyTime(rawDate)) : '';

      if (item.kind === 'tech_only') {
        const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
        return '<div class="timeline-item tech-only">' +
          '<div class="timeline-dot info"></div>' +
          '<div class="timeline-header">' +
            '<span class="timeline-title-group">' +
              '<span class="timeline-type">' + escapeHtml(friendlyEventAction(ev)) + '</span>' +
            '</span>' +
            '<span class="timeline-time" title="' + escapeHtml(rawDate) + '">' + friendlyDate + '</span>' +
          '</div>' +
          (hasPayload ? '<details class="tech-details tech-only" style="margin-top:4px;"><summary>Ver datos técnicos</summary><pre style="margin:4px 0 0 0;">' + escapeHtml(JSON.stringify(ev.payload, null, 2)) + '</pre></details>' : '') +
        '</div>';
      }

      if (item.kind === 'fused_error') {
        const techPayload = item.causeEv
          ? { phase_error: item.causeEv.payload || {}, project_failed: ev.payload || {} }
          : (ev.payload || {});
        const techText = item.rawError ? (String(item.rawError) + '\\n\\n' + JSON.stringify(techPayload, null, 2)) : JSON.stringify(techPayload, null, 2);
        const durPill = item.durSecs !== null ? '<span class="duration-pill">Tardó ' + escapeHtml(formatDurationHuman(item.durSecs, '< 1s')) + '</span>' : '';
        return '<div class="timeline-item">' +
          '<div class="timeline-dot err"></div>' +
          '<div class="timeline-header">' +
            '<span class="timeline-title-group">' +
              '<span class="timeline-type">El proceso se detuvo</span>' +
              durPill +
            '</span>' +
            '<span class="timeline-time" title="' + escapeHtml(rawDate) + '">' + friendlyDate + '</span>' +
          '</div>' +
          '<div class="round-summary">' + escapeHtml(item.msg) + '</div>' +
          '<details class="tech-details tech-only" style="margin-top:4px;"><summary>Ver detalle técnico</summary><pre style="margin:4px 0 0 0;">' + escapeHtml(techText) + '</pre></details>' +
        '</div>';
      }

      if (item.kind === 'gen_tests') {
        const modelPill = pl.model ? '<span class="model-pill tech-only">' + escapeHtml(String(pl.model)) + '</span>' : '';
        const durPill = item.durSecs !== null ? '<span class="duration-pill">Tardó ' + escapeHtml(formatDurationHuman(item.durSecs, '< 1s')) + '</span>' : '';
        const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
        const cleanGenSummary = sanitizeDisplaySummary(item.summaryShort);
        return '<div class="timeline-item">' +
          '<div class="timeline-dot ok"></div>' +
          '<div class="timeline-header">' +
            '<span class="timeline-title-group">' +
              '<span class="timeline-type">Pruebas automáticas creadas</span>' +
              modelPill +
              durPill +
            '</span>' +
            '<span class="timeline-time" title="' + escapeHtml(rawDate) + '">' + friendlyDate + '</span>' +
          '</div>' +
          (cleanGenSummary ? '<div class="round-summary">' + escapeHtml(cleanGenSummary) + '</div>' : '') +
          renderFileChips(item.filesWritten) +
          (hasPayload ? '<details class="tech-details tech-only" style="margin-top:4px;"><summary>Ver datos técnicos</summary><pre style="margin:4px 0 0 0;">' + escapeHtml(JSON.stringify(ev.payload, null, 2)) + '</pre></details>' : '') +
        '</div>';
      }

      if (item.kind === 'round') {
        const tpl = item.testsEv && item.testsEv.payload ? item.testsEv.payload : null;
        const hasTests = tpl && typeof tpl.total === 'number' && tpl.total > 0;
        const passed = hasTests ? Number(tpl.passed || 0) : 0;
        const allOk = hasTests && passed >= tpl.total;
        const isTamper = item.outcome === 'reverted_tamper';
        const testsFailed = (hasTests && passed < tpl.total) ||
          item.outcome === 'failed' ||
          (!hasTests && Array.isArray(item.failingTests) && item.failingTests.length > 0);
        const dotClass = isTamper ? 'warn' : (allOk ? 'ok' : (hasTests ? 'warn' : 'info'));
        const roundTitle = isTamper
          ? ('Ronda ' + item.roundNum + ': ajuste no permitido revertido')
          : ('Ronda ' + item.roundNum + (item.maxRounds ? (' de ' + item.maxRounds) : ''));
        const testBadge = hasTests
          ? '<span class="test-badge' + (allOk ? ' ok' : '') + '">' + passed + '/' + tpl.total + (allOk ? ' ✓' : ' pruebas') + '</span>'
          : '';
        const ciBadge = item.ciEv ? '<span class="test-badge ok">CI ✓</span>' : '';
        const modelPill = pl.model ? '<span class="model-pill tech-only">' + escapeHtml(String(pl.model)) + '</span>' : '';
        const durPill = item.durSecs !== null ? '<span class="duration-pill">Tardó ' + escapeHtml(formatDurationHuman(item.durSecs, '< 1s')) + '</span>' : '';
        const cleanRoundSummary = sanitizeDisplaySummary(item.summaryShort);
        const bodyText = isTamper
          ? 'Se detectó un cambio no autorizado en las pruebas automáticas y se restauró su versión protegida.'
          : (testsFailed
            ? ('Falló la verificación de pruebas' + (cleanRoundSummary ? (' · ' + cleanRoundSummary) : ''))
            : cleanRoundSummary);
        const combinedTech = {
          agent: ev.payload || {},
          tests: tpl || undefined,
          ci: item.ciEv ? (item.ciEv.payload || {}) : undefined,
        };
        const endDate = (item.ciEv && item.ciEv.at) || (item.testsEv && item.testsEv.at) || rawDate;
        const friendlyEnd = endDate ? escapeHtml(formatFriendlyTime(endDate)) : '';
        return '<div class="timeline-item">' +
          '<div class="timeline-dot ' + dotClass + '"></div>' +
          '<div class="round-card">' +
            '<div class="timeline-header">' +
              '<span class="timeline-title-group">' +
                '<span class="timeline-type">' + escapeHtml(roundTitle) + '</span>' +
                testBadge +
                ciBadge +
                durPill +
                modelPill +
              '</span>' +
              '<span class="timeline-time" title="' + escapeHtml(endDate) + '">' + friendlyEnd + '</span>' +
            '</div>' +
            (bodyText ? '<div class="round-summary">' + escapeHtml(bodyText) + '</div>' : '') +
            renderFileChips(item.filesWritten) +
            '<details class="tech-details tech-only" style="margin-top:6px;"><summary>Ver datos técnicos</summary><pre style="margin:4px 0 0 0;">' + escapeHtml(JSON.stringify(combinedTech, null, 2)) + '</pre></details>' +
          '</div>' +
        '</div>';
      }

      const isSuccess = ev.type === 'PROJECT_DELIVERED' || ev.type === 'PROJECT_COMPLETED' || ev.type === 'COMPLETED';
      const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
      const actionTitle = friendlyEventAction(ev);
      const modelPill = pl.model ? '<span class="model-pill tech-only">' + escapeHtml(String(pl.model)) + '</span>' : '';
      const durPill = item.durSecs !== null ? '<span class="duration-pill">Tardó ' + escapeHtml(formatDurationHuman(item.durSecs, '< 1s')) + '</span>' : '';
      return '<div class="timeline-item">' +
        '<div class="timeline-dot ' + (isSuccess ? 'ok' : 'info') + '"></div>' +
        '<div class="timeline-header">' +
          '<span class="timeline-title-group">' +
            '<span class="timeline-type">' + escapeHtml(actionTitle) + '</span>' +
            modelPill +
            durPill +
          '</span>' +
          '<span class="timeline-time" title="' + escapeHtml(rawDate) + '">' + friendlyDate + '</span>' +
        '</div>' +
        (hasPayload ? '<details class="tech-details tech-only" style="margin-top:4px;"><summary>Ver datos técnicos</summary><pre style="margin:4px 0 0 0;">' + escapeHtml(JSON.stringify(ev.payload, null, 2)) + '</pre></details>' : '') +
      '</div>';
    }

    function renderDetail() {
      const empty = document.getElementById('detail-empty');
      const box = document.getElementById('detail-content');
      if (!state.detail) {
        state.lastBannerSignature = null;
        empty.style.display = 'block';
        box.style.display = 'none';
        return;
      }
      empty.style.display = 'none';
      box.style.display = 'block';

      const st = state.detail.status;
      const events = state.detail.events || [];
      const info = stepInfo(st.state, events);
      const titleText = st.name || st.objective || st.project_id;
      document.getElementById('detail-title').textContent = titleText;
      const detailLogoEl = document.getElementById('detail-logo-img');
      if (detailLogoEl) {
        const nextLogoSrc = st.logo_url || ('/api/projects/' + encodeURIComponent(st.project_id) + '/logo');
        if (detailLogoEl.getAttribute('src') !== nextLogoSrc) {
          detailLogoEl.setAttribute('src', nextLogoSrc);
        }
        detailLogoEl.style.display = 'block';
      }
      if (state.editingTitlePid && state.editingTitlePid !== st.project_id) {
        const editForm = document.getElementById('edit-title-form');
        const editBtn = document.getElementById('edit-title-btn');
        if (editForm) editForm.style.display = 'none';
        if (editBtn) editBtn.style.display = '';
        state.editingTitlePid = null;
      }

      const badge = document.getElementById('detail-badge');
      badge.textContent = info.step;
      badge.className = 'badge ' + info.cls;
      const isRunning = Boolean(st.is_running || st.busy || (state.detail && state.detail.is_running));
      const pausedActive = !isRunning && (
        st.state === 'DEBATE' || st.state === 'MEDIATION' ||
        st.state === 'BUILDING' || st.state === 'VERIFYING'
      );
      const segMode = (st.state === 'DELIVERED' || st.state === 'COMPLETED')
        ? 'delivered'
        : ((st.state === 'FAILED' || st.state === 'ERROR') ? 'error' : (pausedActive ? 'paused' : 'active'));
      const segBox = document.getElementById('detail-segments');
      if (segBox) {
        segBox.style.display = segMode === 'delivered' ? 'none' : '';
        segBox.innerHTML = segMode === 'delivered' ? '' : renderStageSegments(info.stage, segMode);
      }
      const timeBox = document.getElementById('detail-time');
      if (timeBox) {
        timeBox.innerHTML = st.last_event_at
          ? '<span title="' + escapeHtml(st.last_event_at) + '">Actualizado: ' + escapeHtml(formatFriendlyTime(st.last_event_at)) + '</span>'
          : '';
      }

      // Contextual guidance banner (only rebuild when signature changes so active drafts stay intact)
      const activeModel = st.active_model || (state.detail && state.detail.active_model) || null;
      const activeStage = st.active_stage || (state.detail && state.detail.active_stage) || null;
      const activitySummary = st.activity_summary || (state.detail && state.detail.activity_summary) || null;
      const activityDetail = st.activity_detail || (state.detail && state.detail.activity_detail) || null;
      const agentsCount = st.agents_count || (state.detail && state.detail.agents_count) || null;
      const stepStartedAt = st.step_started_at || (state.detail && state.detail.step_started_at) || null;
      const testsPassed = (st.tests_passed !== null && st.tests_passed !== undefined)
        ? st.tests_passed
        : ((state.detail && state.detail.tests_passed !== undefined) ? state.detail.tests_passed : null);
      const testsTotal = (st.tests_total !== null && st.tests_total !== undefined)
        ? st.tests_total
        : ((state.detail && state.detail.tests_total !== undefined) ? state.detail.tests_total : null);
      const buildRound = st.build_round || (state.detail && state.detail.build_round) || null;
      const maxRounds = st.max_rounds || (state.detail && state.detail.max_rounds) || null;
      const statusForBanner = Object.assign({}, st, {
        is_running: isRunning,
        active_model: activeModel,
        active_stage: activeStage,
        activity_summary: activitySummary,
        activity_detail: activityDetail,
        agents_count: agentsCount,
        step_started_at: stepStartedAt,
        tests_passed: testsPassed,
        tests_total: testsTotal,
        build_round: buildRound,
        max_rounds: maxRounds,
      });
      const questions = extractLatestQuestions(events);
      const inact = st.state === 'INTERVIEW' && !isRunning ? formatInactivityNote(st.last_event_at) : '';
      const bannerSig = JSON.stringify([
        st.project_id,
        st.name,
        st.state,
        isRunning,
        activeModel,
        activeStage,
        activitySummary,
        activityDetail,
        agentsCount,
        stepStartedAt,
        testsPassed,
        testsTotal,
        buildRound,
        maxRounds,
        st.last_event_at,
        st.last_error,
        questions,
        inact,
        st.links,
        st.local_app,
        Boolean(state.previewOpen[st.project_id]),
        (state.checkpointsByPid[st.project_id] || []).length,
        (state.filesByPid[st.project_id] || []).length,
        Boolean(state.diffByPid[st.project_id]),
      ]);

      const guidanceEl = document.getElementById('detail-guidance');
      if (bannerSig !== state.lastBannerSignature) {
        guidanceEl.innerHTML = buildGuidanceBanner(statusForBanner, events);
        state.lastBannerSignature = bannerSig;

        guidanceEl.querySelectorAll('button[data-dact]').forEach((btn) => {
          btn.addEventListener('click', () => handleAction(btn.dataset.dact, st.project_id, btn));
        });

        const replyInput = document.getElementById('interview-reply-input');
        const replyBtn = document.getElementById('interview-reply-btn');
        if (replyInput) {
          replyInput.addEventListener('input', () => {
            state.replyDrafts[st.project_id] = replyInput.value;
          });
        }
        wireMicButton('interview-mic-btn', 'interview-reply-input');
        if (st.state === 'INTERVIEW' && !isRunning && questions.length) {
          const qKey = st.project_id + ':' + questions.join('|');
          if (state.lastSpokenQuestionKey !== qKey) {
            state.lastSpokenQuestionKey = qKey;
            speakSpanishText(questions.join('. '));
          }
        }
        if (replyBtn && replyInput) {
          replyBtn.addEventListener('click', async () => {
            replyBtn.disabled = true;
            try {
              await sendProjectMessage(st.project_id, replyInput.value, 'reply');
            } finally {
              if (document.body.contains(replyBtn)) replyBtn.disabled = false;
            }
          });
        }

        const adjDetails = document.getElementById('adjust-plan-details');
        const adjInput = document.getElementById('adjust-plan-input');
        const adjBtn = document.getElementById('adjust-plan-btn');
        if (adjDetails) {
          adjDetails.addEventListener('toggle', () => {
            state.adjustOpen[st.project_id] = adjDetails.open;
          });
        }
        if (adjInput) {
          adjInput.addEventListener('input', () => {
            state.adjustDrafts[st.project_id] = adjInput.value;
          });
        }
        if (adjBtn && adjInput) {
          adjBtn.addEventListener('click', async () => {
            adjBtn.disabled = true;
            try {
              await sendProjectMessage(st.project_id, adjInput.value, 'adjust');
            } finally {
              if (document.body.contains(adjBtn)) adjBtn.disabled = false;
            }
          });
        }
      } else {
        updateLiveTimers();
      }
      renderLivePreviewPanel(st);

      // Secondary action (Delete)
      const secEl = document.getElementById('detail-secondary');
      secEl.innerHTML =
        '<div class="secondary-actions">' +
          '<button class="btn-subtle-danger" data-dact="delete">Eliminar proyecto</button>' +
        '</div>';

      secEl.querySelectorAll('button[data-dact]').forEach((btn) => {
        btn.addEventListener('click', () => handleAction(btn.dataset.dact, st.project_id, btn));
      });

      // Unified "Resumen y Plan" tab
      const spec = state.detail.spec || {};
      const fields = [];
      if (spec.objective && String(spec.objective).trim()) {
        fields.push('<div class="spec-section"><h4>Objetivo</h4><p>' + escapeHtml(spec.objective) + '</p></div>');
      }
      if (Array.isArray(spec.users) && spec.users.length) {
        fields.push('<div class="spec-section"><h4>Usuarios</h4><ul>' + spec.users.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      if (Array.isArray(spec.features) && spec.features.length) {
        fields.push('<div class="spec-section"><h4>Funcionalidades</h4><ul>' + spec.features.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      if (Array.isArray(spec.business_rules) && spec.business_rules.length) {
        fields.push('<div class="spec-section"><h4>Reglas de negocio</h4><ul>' + spec.business_rules.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      if (Array.isArray(spec.edge_cases) && spec.edge_cases.length) {
        fields.push('<div class="spec-section"><h4>Casos borde</h4><ul>' + spec.edge_cases.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      if (Array.isArray(spec.constraints) && spec.constraints.length) {
        fields.push('<div class="spec-section"><h4>Restricciones</h4><ul>' + spec.constraints.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      if (Array.isArray(spec.success_criteria) && spec.success_criteria.length) {
        fields.push('<div class="spec-section"><h4>Criterios de éxito</h4><ul>' + spec.success_criteria.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      if (Array.isArray(spec.assumptions) && spec.assumptions.length) {
        fields.push('<div class="spec-section"><h4>Supuestos</h4><ul>' + spec.assumptions.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
      }
      const specHtml = fields.length
        ? fields.join('')
        : '<div class="empty-box">Aún estamos definiendo los detalles de este proyecto.</div>';

      const plan = state.detail.plan;
      let planHtml = '<div class="empty-box">Aún no se ha generado un Plan.</div>';
      if (plan) {
        const filesHtml = (plan.files || []).map((f) =>
          '<div class="file-card"><div class="file-path">' + escapeHtml(f.path) + '</div><div class="file-purpose">' + escapeHtml(f.purpose) + '</div>' +
          (f.contract ? '<details class="tech-details tech-only"><summary>Contrato</summary><pre>' + escapeHtml(f.contract) + '</pre></details>' : '') +
          '</div>'
        ).join('');
        const testsHtml = (plan.acceptance_tests || []).map((t) =>
          '<li><strong>[' + escapeHtml(t.id) + '] (' + escapeHtml(t.type) + ')</strong>: ' + escapeHtml(t.description) + '</li>'
        ).join('');
        planHtml =
          '<div class="plan-summary"><strong>Resumen:</strong> ' + escapeHtml(plan.summary) + '</div>' +
          (plan.rationale ? '<div class="plan-summary"><strong>Justificación:</strong> ' + escapeHtml(plan.rationale) + '</div>' : '') +
          '<div class="plan-meta">' +
            '<span class="badge badge-info">Plantilla: ' + escapeHtml(plan.template_id) + '</span>' +
            '<span class="badge badge-info">Target: ' + escapeHtml(plan.deployment_target) + '</span>' +
          '</div>' +
          '<h4 style="margin:14px 0 8px 0;">Archivos (' + (plan.files || []).length + ')</h4>' + filesHtml +
          '<h4 style="margin:14px 0 8px 0;">Pruebas de aceptación (' + (plan.acceptance_tests || []).length + ')</h4><ul style="padding-left:20px; font-size:0.92rem;">' + testsHtml + '</ul>';
      }
      document.getElementById('tab-spec').innerHTML =
        specHtml +
        '<div class="tab-section-divider">' +
          '<h4 style="margin:0 0 12px 0; font-size:1rem;">Plan de construcción</h4>' +
          planHtml +
        '</div>';

      // Events tab ("Historial" grouped into 5 clear lifecycle phases)
      const LIFECYCLE_PHASES = [
        { num: 1, id: 'idea', title: '1. Idea y requisitos' },
        { num: 2, id: 'design', title: '2. Diseño de la solución' },
        { num: 3, id: 'approval', title: '3. Aprobación del plan' },
        { num: 4, id: 'build', title: '4. Construcción y pruebas' },
        { num: 5, id: 'delivery', title: '5. Entrega final' },
      ];

      const splitRes = splitProjectAttempts(events);
      const activeEnriched = splitRes.activeEvents.map((ev) => ({
        ev,
        phaseNum: phaseNumForEvent(ev),
      }));

      const isDelivered = st.state === 'DELIVERED' || st.state === 'COMPLETED';
      const isFailed = st.state === 'FAILED' || st.state === 'ERROR';
      const activePhaseNum = isDelivered ? 6 : info.stage;

      const phasesHtml = '<div class="phase-list">' + LIFECYCLE_PHASES.map((ph) => {
        const phaseItems = activeEnriched.filter((it) => it.phaseNum === ph.num);
        let phaseStatus = 'pending';
        if (isDelivered) {
          phaseStatus = 'completed';
        } else if (isFailed) {
          if (ph.num < activePhaseNum) phaseStatus = 'completed';
          else if (ph.num === activePhaseNum) phaseStatus = 'error';
          else phaseStatus = 'pending';
        } else {
          if (ph.num < activePhaseNum) phaseStatus = 'completed';
          else if (ph.num === activePhaseNum) phaseStatus = 'active';
          else phaseStatus = 'pending';
        }

        if (phaseStatus === 'pending') {
          return '<div class="phase-card phase-pending">' +
            '<div class="phase-summary">' +
              '<span class="phase-summary-left">' +
                '<span class="phase-circle">' + ph.num + '</span>' +
                '<span class="phase-title">' + escapeHtml(ph.title) + '</span>' +
              '</span>' +
              '<span class="phase-meta-badge">Pendiente</span>' +
            '</div>' +
          '</div>';
        }

        let badgeLabel = 'En curso';
        let circleHtml = String(ph.num);
        if (phaseStatus === 'completed') {
          const durStr = computePhaseDuration(ph.num, phaseItems, activeEnriched);
          badgeLabel = durStr ? ('Completado en ' + durStr) : 'Completado';
          circleHtml = '✓';
        } else if (phaseStatus === 'error') {
          badgeLabel = 'Requiere atención';
          circleHtml = '!';
        }

        const stateKey = st.project_id + ':' + ph.id;
        const defaultOpen = (phaseStatus === 'active' || phaseStatus === 'error');
        const isOpen = (stateKey in state.phaseOpen) ? state.phaseOpen[stateKey] : defaultOpen;
        const openAttr = isOpen ? ' open' : '';

        const phaseRawEvents = phaseItems.map((it) => it.ev);
        const grouped = pairAndGroupPhaseEvents(phaseRawEvents);
        const hasVisible = grouped.some((g) => g.kind !== 'tech_only');
        const emptyFallback = !hasVisible
          ? '<div class="muted" style="font-size:0.86rem;">' + (phaseStatus === 'completed' ? 'Etapa completada.' : 'En progreso en esta fase...') + '</div>'
          : '';
        const stepsHtml = grouped.length
          ? (emptyFallback + '<div class="timeline">' + grouped.map(renderTimelineEntry).join('') + '</div>')
          : '<div class="muted" style="font-size:0.86rem;">En progreso en esta fase...</div>';

        return '<details class="phase-card phase-' + phaseStatus + '" data-phase-key="' + escapeHtml(stateKey) + '"' + openAttr + '>' +
          '<summary class="phase-summary">' +
            '<span class="phase-summary-left">' +
              '<span class="phase-circle">' + circleHtml + '</span>' +
              '<span class="phase-title">' + escapeHtml(ph.title) + '</span>' +
            '</span>' +
            '<span class="phase-meta-badge">' + escapeHtml(badgeLabel) + '</span>' +
          '</summary>' +
          '<div class="phase-steps">' + stepsHtml + '</div>' +
        '</details>';
      }).join('') + '</div>';

      let priorHtml = '';
      if (splitRes.priorGroups.length) {
        const priorKey = st.project_id + ':prior';
        const priorOpenAttr = state.priorOpen[priorKey] ? ' open' : '';
        const attemptsMarkup = splitRes.priorGroups.map((grp, idx) => {
          const grpGrouped = pairAndGroupPhaseEvents(grp);
          const lbl = splitRes.priorGroups.length > 1 ? ('Intento ' + (idx + 1)) : 'Intento anterior';
          return '<div style="margin-top:10px;">' +
            '<div class="muted" style="font-size:0.8rem; font-weight:600; margin-bottom:6px;">' + escapeHtml(lbl) + '</div>' +
            '<div class="timeline">' + grpGrouped.map(renderTimelineEntry).join('') + '</div>' +
          '</div>';
        }).join('');
        priorHtml = '<details class="prior-attempts-box" data-prior-key="' + escapeHtml(priorKey) + '"' + priorOpenAttr + '>' +
          '<summary>Ver intentos anteriores (' + splitRes.priorGroups.length + ')</summary>' +
          attemptsMarkup +
        '</details>';
      }

      const toolbarHtml = '<div class="history-toolbar">' +
        '<span class="muted tech-only">ID: <code>' + escapeHtml(st.project_id) + '</code></span>' +
        '<button id="copy-diag-btn" class="btn btn-sm tech-only" type="button">Copiar diagnóstico</button>' +
      '</div>';

      const eventsEl = document.getElementById('tab-events');
      eventsEl.innerHTML = toolbarHtml + phasesHtml + priorHtml;

      eventsEl.querySelectorAll('details.phase-card[data-phase-key]').forEach((det) => {
        det.addEventListener('toggle', () => {
          state.phaseOpen[det.getAttribute('data-phase-key')] = det.open;
        });
      });
      eventsEl.querySelectorAll('details.prior-attempts-box[data-prior-key]').forEach((det) => {
        det.addEventListener('toggle', () => {
          state.priorOpen[det.getAttribute('data-prior-key')] = det.open;
        });
      });

      const copyDiagBtn = document.getElementById('copy-diag-btn');
      if (copyDiagBtn) {
        copyDiagBtn.addEventListener('click', async () => {
          const diagPayload = JSON.stringify({
            project_id: st.project_id,
            state: st.state,
            is_running: isRunning,
            active_model: activeModel,
            tests: st.tests || null,
            last_error: st.last_error || null,
            events_count: events.length,
            events: events,
          }, null, 2);
          try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              await navigator.clipboard.writeText(diagPayload);
            } else {
              const tmp = document.createElement('textarea');
              tmp.value = diagPayload;
              document.body.appendChild(tmp);
              tmp.select();
              document.execCommand('copy');
              document.body.removeChild(tmp);
            }
            setNote('Diagnóstico copiado al portapapeles.');
          } catch (err) {
            setNote('No se pudo copiar el diagnóstico: ' + err.message);
          }
        });
      }

      const auditTabBtn = document.getElementById('tab-audit-btn');
      const modifyTabBtn = document.getElementById('tab-modify-btn');
      if (isDelivered) {
        if (auditTabBtn) auditTabBtn.style.display = '';
        if (modifyTabBtn) modifyTabBtn.style.display = '';
        if (!(st.project_id in state.userChoseTabByPid)) {
          state.activeTab = 'audit';
        }
        if (!state.chatLoadedFor[st.project_id]) {
          loadProjectChatHistory(st.project_id);
        }
        loadProjectDevAndVersionData(st.project_id, st.last_event_at || String(events.length));
        renderAuditChatTab(st.project_id);
        renderModifyTab(st);
      } else {
        if (auditTabBtn) auditTabBtn.style.display = 'none';
        if (modifyTabBtn) modifyTabBtn.style.display = 'none';
        if (state.activeTab === 'audit' || state.activeTab === 'modify') {
          state.activeTab = 'spec';
        }
      }

      renderTabs();
      renderLiveActivityBanner();
    }

    function renderModifyTab(st) {
      const container = document.getElementById('tab-modify');
      if (!container || !st) return;
      const pid = st.project_id;
      const modifyDraft = escapeHtml((state.modifyDrafts && state.modifyDrafts[pid]) || '');
      const filesList = (state.filesByPid && state.filesByPid[pid]) || [];
      const selSet = new Set((state.targetFilesSelected && state.targetFilesSelected[pid]) || []);
      const cps = (state.checkpointsByPid && state.checkpointsByPid[pid]) || [];
      const validCps = cps.filter((c) => c && c.commit_sha);
      const diffInfo = (state.diffByPid && state.diffByPid[pid]) || null;

      const modSig = JSON.stringify([
        pid,
        filesList.length,
        validCps.map((c) => c.commit_sha + ':' + Boolean(c.is_current)),
        Boolean(diffInfo && diffInfo.diff),
        Boolean(state.filesOpen && state.filesOpen[pid]),
        Boolean(state.rollbackOpen && state.rollbackOpen[pid]),
        Boolean(state.diffOpen && state.diffOpen[pid]),
      ]);
      if (state.lastModifySignature === modSig && container.querySelector('#modify-project-input')) {
        return;
      }
      state.lastModifySignature = modSig;

      const fileOptsHtml = filesList.map((fp) =>
        '<option value="' + escapeHtml(fp) + '"' + (selSet.has(fp) ? ' selected' : '') + '>' + escapeHtml(fp) + '</option>'
      ).join('');
      const cpOptsHtml = validCps.map((c) => {
        const timeLbl = formatFriendlyTime(c.delivered_at);
        const curTag = c.is_current ? ' (actual)' : '';
        const txt = c.version_label + curTag + (timeLbl ? (' · ' + timeLbl) : '');
        return '<option value="' + escapeHtml(c.commit_sha) + '" title="' + escapeHtml(c.commit_sha) + '">' + escapeHtml(txt) + '</option>';
      }).join('');
      const rollbackDisabled = validCps.length === 0 ? ' disabled' : '';
      const rollbackDisplay = validCps.length === 0 ? ' style="display:none"' : '';
      const curCp = validCps.find((c) => c.is_current) || validCps[0] || null;
      const diffText = diffInfo && diffInfo.diff ? diffInfo.diff : 'Sin cambios recientes registrados o sin repositorio Git.';
      const filesOpenAttr = (state.filesOpen && state.filesOpen[pid]) ? ' open' : '';
      const rollbackOpenAttr = (state.rollbackOpen && state.rollbackOpen[pid]) ? ' open' : '';
      const diffOpenAttr = (state.diffOpen && state.diffOpen[pid]) ? ' open' : '';

      container.innerHTML =
        '<div class="modify-panel-card">' +
          '<div class="modify-box" style="margin-top:0;">' +
            '<div class="modify-box-title">Pedir cambios o mejoras</div>' +
            '<div class="input-row-with-mic">' +
              '<textarea id="modify-project-input" class="input-field" rows="3" placeholder="Describe qué quieres mejorar, cambiar o agregar al proyecto...">' + modifyDraft + '</textarea>' +
              '<button id="modify-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar cambios por voz (es-MX)">🎤</button>' +
            '</div>' +
            '<div class="banner-actions">' +
              '<button id="modify-project-btn" class="btn btn-primary btn-sm" type="button">Aplicar cambios</button>' +
              '<span id="modify-inline-status"></span>' +
            '</div>' +
          '</div>' +
          '<details id="modify-files-details" class="tech-details tech-only"' + filesOpenAttr + '>' +
            '<summary>Archivos objetivo (opcional)' + (selSet.size ? (' · ' + selSet.size + ' seleccionado(s)') : '') + '</summary>' +
            '<div id="modify-target-files-wrap" style="margin-top:8px;">' +
              '<select id="modify-target-files" class="input-field" multiple size="5">' + fileOptsHtml + '</select>' +
            '</div>' +
          '</details>' +
          '<details id="rollback-details" class="tech-details tech-only"' + rollbackDisplay + rollbackOpenAttr + '>' +
            '<summary>Versiones estables (Rollback)' + (curCp ? (' · HEAD: ' + escapeHtml(curCp.short_sha || curCp.commit_sha)) : '') + '</summary>' +
            '<div class="versions-box" style="margin-top:8px;">' +
              '<div class="versions-row">' +
                '<select id="rollback-version-select" class="input-field"' + rollbackDisabled + '>' +
                  (validCps.length ? cpOptsHtml : '<option value="">Sin checkpoints Git registrados</option>') +
                '</select>' +
                '<button id="rollback-version-btn" class="btn btn-sm btn-warn" type="button"' + rollbackDisabled + '>Restaurar esta versión</button>' +
              '</div>' +
            '</div>' +
          '</details>' +
          '<details id="git-diff-details" class="tech-details tech-only"' + diffOpenAttr + '>' +
            '<summary>Últimos cambios (git diff)</summary>' +
            '<pre id="git-diff-pre" class="diff-pre">' + escapeHtml(diffText) + '</pre>' +
          '</details>' +
        '</div>';

      const modifyInput = document.getElementById('modify-project-input');
      const modifyBtn = document.getElementById('modify-project-btn');
      const targetSelect = document.getElementById('modify-target-files');
      if (modifyInput) {
        modifyInput.addEventListener('input', () => {
          state.modifyDrafts[pid] = modifyInput.value;
        });
      }
      if (targetSelect) {
        targetSelect.addEventListener('change', () => {
          state.targetFilesSelected[pid] = Array.from(targetSelect.selectedOptions).map((o) => o.value);
        });
      }
      wireMicButton('modify-mic-btn', 'modify-project-input');
      if (modifyBtn && modifyInput) {
        modifyBtn.addEventListener('click', async () => {
          const selFiles = targetSelect
            ? Array.from(targetSelect.selectedOptions).map((o) => o.value).filter(Boolean)
            : [];
          await submitModifyProject(pid, modifyInput.value, selFiles, modifyBtn);
        });
      }

      const rbSelect = document.getElementById('rollback-version-select');
      const rbBtn = document.getElementById('rollback-version-btn');
      if (rbBtn && rbSelect) {
        rbBtn.addEventListener('click', async () => {
          const sha = rbSelect.value;
          if (!sha) return;
          await submitRollbackProject(pid, sha, rbBtn);
        });
      }

      const filesDetails = document.getElementById('modify-files-details');
      if (filesDetails) {
        filesDetails.addEventListener('toggle', () => {
          state.filesOpen[pid] = filesDetails.open;
        });
      }
      const rbDetails = document.getElementById('rollback-details');
      if (rbDetails) {
        rbDetails.addEventListener('toggle', () => {
          state.rollbackOpen[pid] = rbDetails.open;
        });
      }
      const diffDetails = document.getElementById('git-diff-details');
      if (diffDetails) {
        diffDetails.addEventListener('toggle', () => {
          state.diffOpen[pid] = diffDetails.open;
        });
      }
    }

    function renderAuditChatTab(pid) {
      const container = document.getElementById('tab-audit');
      if (!container) return;
      const msgs = state.chatHistories[pid] || [];
      const draft = state.chatDrafts[pid] || '';
      const chatSig = pid + ':' + JSON.stringify(msgs.map((m) => [m.id, m.role, m.content]));
      if (state.lastChatSig === chatSig && container.querySelector('#audit-chat-input')) {
        return;
      }
      state.lastChatSig = chatSig;

      const msgsHtml = msgs.length
        ? msgs.map((m, idx) => {
            const isUser = m.role === 'user';
            const modeLabel = m.mode === 'audit' ? 'Auditoría' : (m.mode === 'document' ? 'Documento .md' : 'Consulta');
            const who = isUser ? ('Tú · ' + modeLabel) : ('Auditor · ' + modeLabel);
            const actionsHtml = !isUser
              ? '<div class="audit-msg-actions">' +
                  '<button class="btn btn-sm" type="button" data-audit-dl="' + idx + '">Descargar .md</button>' +
                  '<button class="btn btn-sm" type="button" data-audit-apply="' + idx + '">Enviar hallazgos a Aplicar cambios</button>' +
                '</div>'
              : '';
            return '<div class="audit-msg ' + (isUser ? 'user' : 'assistant') + '">' +
              '<div class="audit-msg-head"><span><strong>' + escapeHtml(who) + '</strong></span><span>' + escapeHtml(formatFriendlyTime(m.created_at)) + '</span></div>' +
              '<div class="audit-msg-body">' + escapeHtml(m.content || '') + '</div>' +
              actionsHtml +
            '</div>';
          }).join('')
        : '<div class="muted" style="font-size:0.86rem;">Consulta código, solicita una auditoría de solo lectura o genera documentación en Markdown sin alterar tus archivos.</div>';

      container.innerHTML =
        '<div class="muted" style="font-size:0.84rem; margin-bottom:10px;"><strong>Consultar y Auditar proyecto</strong> — Inspección de solo lectura y generación de documentación.</div>' +
        '<div class="audit-toolbar">' +
          '<button id="audit-quick-audit-btn" class="btn btn-sm" type="button">🔍 Auditar código</button>' +
          '<button id="audit-quick-explain-btn" class="btn btn-sm" type="button">📖 Explicar funcionamiento</button>' +
          '<button id="audit-quick-doc-btn" class="btn btn-sm" type="button">📄 Generar documento (.md)</button>' +
        '</div>' +
        '<div id="audit-chat-history" class="audit-thread">' + msgsHtml + '</div>' +
        '<div class="input-row-with-mic">' +
          '<textarea id="audit-chat-input" class="input-field" rows="2" placeholder="Haz una pregunta sobre el proyecto, su arquitectura o posibles mejoras...">' + escapeHtml(draft) + '</textarea>' +
          '<button id="audit-chat-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar consulta por voz (es-MX)">🎤</button>' +
        '</div>' +
        '<div class="banner-actions">' +
          '<button id="audit-chat-send-btn" class="btn btn-primary btn-sm" type="button">Enviar consulta</button>' +
          '<span id="audit-inline-status"></span>' +
        '</div>';

      const threadEl = document.getElementById('audit-chat-history');
      if (threadEl) {
        threadEl.scrollTop = threadEl.scrollHeight;
      }

      const inputEl = document.getElementById('audit-chat-input');
      const sendBtn = document.getElementById('audit-chat-send-btn');
      if (inputEl) {
        inputEl.addEventListener('input', () => {
          state.chatDrafts[pid] = inputEl.value;
        });
      }
      wireMicButton('audit-chat-mic-btn', 'audit-chat-input');

      const quickAuditBtn = document.getElementById('audit-quick-audit-btn');
      const quickExplainBtn = document.getElementById('audit-quick-explain-btn');
      const quickDocBtn = document.getElementById('audit-quick-doc-btn');
      if (quickAuditBtn) {
        quickAuditBtn.addEventListener('click', () => submitProjectChat(pid, '', 'audit', quickAuditBtn));
      }
      if (quickExplainBtn) {
        quickExplainBtn.addEventListener('click', () =>
          submitProjectChat(pid, 'Explica cómo funciona este proyecto, sus archivos clave y cómo utilizarlo.', 'chat', quickExplainBtn)
        );
      }
      if (quickDocBtn) {
        quickDocBtn.addEventListener('click', () => submitProjectChat(pid, '', 'document', quickDocBtn));
      }
      if (sendBtn && inputEl) {
        sendBtn.addEventListener('click', () => submitProjectChat(pid, inputEl.value, 'chat', sendBtn));
      }

      container.querySelectorAll('button[data-audit-dl]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const idx = Number(btn.getAttribute('data-audit-dl'));
          const entry = msgs[idx];
          if (!entry) return;
          const fallbackName = entry.mode === 'audit' ? (pid + '-auditoria.md') : (pid + '-documentacion.md');
          downloadMarkdownFile(entry.document_title || fallbackName, entry.content || '');
        });
      });

      container.querySelectorAll('button[data-audit-apply]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const idx = Number(btn.getAttribute('data-audit-apply'));
          const entry = msgs[idx];
          if (!entry) return;
          const textToApply = (entry.suggested_changes && String(entry.suggested_changes).trim())
            ? String(entry.suggested_changes).trim()
            : String(entry.content || '').trim();
          state.modifyDrafts[pid] = textToApply;
          state.activeTab = 'modify';
          state.userChoseTabByPid[pid] = 'modify';
          renderTabs();
          const modifyBox = document.getElementById('modify-project-input');
          if (modifyBox) {
            modifyBox.value = textToApply;
            modifyBox.dispatchEvent(new Event('input', { bubbles: true }));
            modifyBox.focus();
            modifyBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
          setNote('Hallazgos copiados al panel "Pedir cambios". Revisa y pulsa Aplicar cambios si deseas ejecutarlos.');
        });
      });
    }

    async function loadProjectChatHistory(pid) {
      if (!pid) return;
      state.chatLoadedFor[pid] = true;
      try {
        const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/chat');
        state.chatHistories[pid] = Array.isArray(res.messages) ? res.messages : [];
        state.lastChatSig = null;
        if (state.detail && state.detail.project_id === pid) {
          renderAuditChatTab(pid);
        }
      } catch (_) {}
    }

    async function submitProjectChat(pid, rawMessage, mode, triggerBtn) {
      const msg = String(rawMessage || '').trim();
      if (mode === 'chat' && !msg) {
        setNote('Escribe una pregunta para consultar el proyecto.');
        return;
      }
      const modeLabel = mode === 'audit'
        ? 'Analizando archivos del repositorio (auditoría)...'
        : (mode === 'document'
          ? 'Analizando archivos del repositorio para documentar...'
          : 'Analizando archivos del repositorio...');
      await runTrackedOperation(
        {
          label: modeLabel,
          subtext: 'Lectura segura del código sin alterar archivos.',
          buttonEl: triggerBtn || 'audit-chat-send-btn',
          inlineTargetId: 'audit-inline-status',
        },
        async () => {
          try {
            const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/chat', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message: msg, mode: mode || 'chat' }),
            });
            if (mode === 'chat') {
              state.chatDrafts[pid] = '';
            }
            state.chatHistories[pid] = Array.isArray(res.history) ? res.history : [];
            state.chatLoadedFor[pid] = true;
            state.lastChatSig = null;
            renderAuditChatTab(pid);
            if (res && res.reply) {
              speakSpanishText(res.reply);
            }
            setNote('Respuesta generada.');
          } catch (err) {
            setNote('Error en chat auditor: ' + err.message);
          }
        }
      );
    }

    async function loadDetail(pid) {
      if (!pid) return;
      try {
        state.detail = await apiFetch('/api/projects/' + encodeURIComponent(pid));
        updateServerHealth(true);
        renderDetail();
      } catch (err) {
        state.detail = null;
        renderDetail();
        setNote('Error cargando detalle: ' + err.message);
      }
    }

    async function loadProjects() {
      try {
        const data = await apiFetch('/api/projects');
        updateServerHealth(true);
        state.projects = data.projects || [];
        if (state.selectedId && !state.projects.some((p) => p.project_id === state.selectedId)) {
          state.selectedId = null;
          state.detail = null;
        }
        if (!state.selectedId && state.projects.length) {
          state.selectedId = state.projects[0].project_id;
        }
        renderProjectList();
        if (state.selectedId) {
          await loadDetail(state.selectedId);
        } else {
          renderDetail();
        }
      } catch (err) {
        updateServerHealth(false);
        setNote('Error de conexión: ' + err.message);
      }
    }

    async function sendProjectMessage(pid, rawMsg, kind) {
      const message = (rawMsg || '').trim();
      if (!message) {
        setNote('Por favor escribe un mensaje antes de enviar.');
        return;
      }
      try {
        await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/message', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message }),
        });
        if (kind === 'reply') {
          delete state.replyDrafts[pid];
          setNote('Respuesta enviada para ' + pid + '.');
        } else {
          delete state.adjustDrafts[pid];
          state.adjustOpen[pid] = false;
          setNote('Solicitud de ajuste enviada para ' + pid + '.');
        }
        state.lastBannerSignature = null;
        await loadProjects();
      } catch (err) {
        setNote('Error enviando mensaje: ' + err.message);
      }
    }

    async function loadProjectDevAndVersionData(pid, sig) {
      if (!pid) return;
      const metaKey = pid + ':' + String(sig || '');
      if (state.devMetaLoadedSig[pid] === metaKey) {
        return;
      }
      state.devMetaLoadedSig[pid] = metaKey;
      try {
        const [cpRes, filesRes, diffRes] = await Promise.all([
          apiFetch('/api/projects/' + encodeURIComponent(pid) + '/checkpoints').catch(() => null),
          apiFetch('/api/projects/' + encodeURIComponent(pid) + '/files').catch(() => null),
          apiFetch('/api/projects/' + encodeURIComponent(pid) + '/diff').catch(() => null),
        ]);
        if (cpRes && Array.isArray(cpRes.checkpoints)) {
          state.checkpointsByPid[pid] = cpRes.checkpoints;
        }
        if (filesRes && Array.isArray(filesRes.files)) {
          state.filesByPid[pid] = filesRes.files;
        }
        if (diffRes) {
          state.diffByPid[pid] = diffRes;
        }
        if (state.detail && state.detail.project_id === pid) {
          state.lastModifySignature = null;
          renderModifyTab(state.detail.status);
        }
      } catch (_) {}
    }

    function renderLivePreviewPanel(st) {
      const splitEl = document.getElementById('detail-work-split');
      const panelEl = document.getElementById('live-preview-panel');
      const iframeEl = document.getElementById('live-preview-iframe');
      const openLink = document.getElementById('open-preview-tab-link');
      const reloadBtn = document.getElementById('reload-preview-btn');
      const closeBtn = document.getElementById('close-preview-btn');
      if (!splitEl || !panelEl || !iframeEl) return;

      const pid = st && st.project_id;
      const localApp = (st && st.local_app) || {};
      const shouldShow = Boolean(pid && state.previewOpen[pid] && localApp.running && localApp.url);
      if (!shouldShow) {
        splitEl.classList.remove('preview-open');
        panelEl.style.display = 'none';
        if (pid && state.previewOpen[pid] && !localApp.running) {
          state.previewOpen[pid] = false;
          delete state.previewRenderedUrl[pid];
          iframeEl.src = 'about:blank';
        }
        return;
      }

      splitEl.classList.add('preview-open');
      panelEl.style.display = 'flex';
      if (openLink) openLink.href = localApp.url;
      if (state.previewRenderedUrl[pid] !== localApp.url) {
        state.previewRenderedUrl[pid] = localApp.url;
        iframeEl.src = localApp.url;
      }
      if (reloadBtn && !reloadBtn.dataset.wired) {
        reloadBtn.dataset.wired = '1';
        reloadBtn.addEventListener('click', () => {
          const curPid = state.selectedId;
          const curSt = state.detail && state.detail.status;
          const url = curSt && curSt.local_app && curSt.local_app.url;
          if (!url) return;
          const sep = url.indexOf('?') >= 0 ? '&' : '?';
          iframeEl.src = url + sep + 'preview_t=' + Date.now();
          if (curPid) state.previewRenderedUrl[curPid] = url;
        });
      }
      if (closeBtn && !closeBtn.dataset.wired) {
        closeBtn.dataset.wired = '1';
        closeBtn.addEventListener('click', () => {
          const curPid = state.selectedId;
          if (curPid) {
            state.previewOpen[curPid] = false;
            delete state.previewRenderedUrl[curPid];
          }
          iframeEl.src = 'about:blank';
          state.lastBannerSignature = null;
          renderDetail();
        });
      }
    }

    async function submitModifyProject(pid, rawInstructions, targetFiles, triggerBtn) {
      const instructions = (rawInstructions || '').trim();
      if (!instructions) {
        setNote('Describe qué cambios o mejoras deseas aplicar.');
        return;
      }
      await runTrackedOperation(
        {
          label: 'Aplicando cambios y ejecutando pruebas...',
          subtext: 'El agente modificará el repositorio y correrá la verificación automática.',
          buttonEl: triggerBtn || 'modify-project-btn',
          inlineTargetId: 'modify-inline-status',
        },
        async () => {
          try {
            const bodyPayload = { instructions };
            if (Array.isArray(targetFiles) && targetFiles.length) {
              bodyPayload.target_files = targetFiles;
            }
            await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/modify', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(bodyPayload),
            });
            delete state.modifyDrafts[pid];
            delete state.targetFilesSelected[pid];
            delete state.devMetaLoadedSig[pid];
            state.lastBannerSignature = null;
            state.lastModifySignature = null;
            setNote('Cambios en curso para ' + pid + '.');
            await loadProjects();
          } catch (err) {
            setNote('Error aplicando cambios: ' + err.message);
          }
        }
      );
    }

    async function submitRollbackProject(pid, commitSha, triggerBtn) {
      if (!commitSha) return;
      if (!window.confirm('¿Deseas restaurar "' + pid + '" a esta versión estable y volver a verificar las pruebas?')) {
        return;
      }
      await runTrackedOperation(
        {
          label: 'Restaurando versión estable y ejecutando pruebas...',
          subtext: 'Verificando que el proyecto pase pruebas tras el rollback.',
          buttonEl: triggerBtn || 'rollback-version-btn',
          inlineTargetId: 'modify-inline-status',
        },
        async () => {
          try {
            const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/rollback', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ commit_sha: commitSha }),
            });
            if (res && Array.isArray(res.checkpoints)) {
              state.checkpointsByPid[pid] = res.checkpoints;
            }
            delete state.devMetaLoadedSig[pid];
            state.previewOpen[pid] = false;
            delete state.previewRenderedUrl[pid];
            state.lastBannerSignature = null;
            state.lastModifySignature = null;
            setNote('Versión restaurada con éxito para ' + pid + '.');
            await loadProjects();
          } catch (err) {
            setNote('Error restaurando versión: ' + err.message);
          }
        }
      );
    }

    async function handleAction(action, pid, triggerBtn) {
      try {
        if (action === 'toggle-preview') {
          state.previewOpen[pid] = !Boolean(state.previewOpen[pid]);
          state.lastBannerSignature = null;
          renderDetail();
          return;
        } else if (action === 'delete') {
          if (!window.confirm('¿Deseas eliminar el proyecto "' + pid + '"? Esta acción no se puede deshacer.')) {
            return;
          }
          await apiFetch('/api/projects/' + encodeURIComponent(pid), { method: 'DELETE' });
          setNote('Proyecto ' + pid + ' eliminado.');
          if (state.selectedId === pid) {
            state.selectedId = null;
            state.detail = null;
          }
        } else if (action === 'approve') {
          await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/approve', { method: 'POST' });
          setNote('Construcción iniciada para ' + pid + '.');
        } else if (action === 'retry') {
          await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clean_workspace: false }),
          });
          setNote('Reintento iniciado para ' + pid + '.');
        } else if (action === 'retry-clean') {
          if (!window.confirm('¿Deseas borrar el workspace actual de "' + pid + '" y reconstruir desde cero?')) {
            return;
          }
          await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clean_workspace: true }),
          });
          setNote('Reconstrucción desde cero iniciada para ' + pid + '.');
        } else if (action === 'local-start') {
          await runTrackedOperation(
            {
              label: 'Preparando entorno virtual e instalando dependencias...',
              subtext: 'Iniciando servidor local y verificando respuesta HTTP...',
              buttonEl: triggerBtn,
              inlineTargetId: 'local-inline-status',
            },
            async () => {
              const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/start', { method: 'POST' });
              const url = res.local_app && res.local_app.url ? res.local_app.url : '';
              setNote(url ? ('App encendida en ' + url) : ('App encendida para ' + pid));
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'local-stop') {
          state.previewOpen[pid] = false;
          delete state.previewRenderedUrl[pid];
          await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/stop', { method: 'POST' });
          setNote('App local apagada para ' + pid + '.');
        }
        state.lastBannerSignature = null;
        await loadProjects();
      } catch (err) {
        setNote('Error en ' + action + ': ' + err.message);
      }
    }

    (function initEditTitleForm() {
      const editBtn = document.getElementById('edit-title-btn');
      const form = document.getElementById('edit-title-form');
      const input = document.getElementById('edit-title-input');
      const suggestBtn = document.getElementById('suggest-title-btn');
      const renameGithubWrap = document.getElementById('rename-github-wrap');
      const renameGithubCheckbox = document.getElementById('rename-github-checkbox');
      const saveBtn = document.getElementById('save-title-btn');
      const cancelBtn = document.getElementById('cancel-title-btn');
      if (!editBtn || !form || !input || !saveBtn || !cancelBtn) return;

      function closeEditor() {
        form.style.display = 'none';
        editBtn.style.display = '';
        state.editingTitlePid = null;
      }

      editBtn.addEventListener('click', () => {
        if (!state.detail || !state.detail.status) return;
        const st = state.detail.status;
        state.editingTitlePid = st.project_id;
        input.value = st.name || st.objective || st.project_id;
        const repoUrl = (st.links && st.links.repo) || st.repo_url || '';
        const hasRepo = Boolean(repoUrl && String(repoUrl).trim());
        if (renameGithubWrap && renameGithubCheckbox) {
          renameGithubWrap.style.display = hasRepo ? 'inline-flex' : 'none';
          renameGithubCheckbox.checked = hasRepo;
        }
        editBtn.style.display = 'none';
        form.style.display = 'flex';
        input.focus();
        input.select();
      });

      cancelBtn.addEventListener('click', closeEditor);
      if (suggestBtn) {
        suggestBtn.addEventListener('click', async () => {
          const pid = state.editingTitlePid || (state.detail && state.detail.project_id);
          if (!pid) return;
          suggestBtn.disabled = true;
          try {
            const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/suggest-title', {
              method: 'POST',
            });
            if (res && res.suggested_title) {
              input.value = res.suggested_title;
              input.focus();
              input.select();
              setNote('Sugerencia generada analizando el repo. Puedes editarla o guardar.');
            }
          } catch (err) {
            setNote('Error sugiriendo título: ' + err.message);
          } finally {
            suggestBtn.disabled = false;
          }
        });
      }
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
          ev.preventDefault();
          closeEditor();
        }
      });

      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const pid = state.editingTitlePid || (state.detail && state.detail.project_id);
        const title = (input.value || '').trim();
        if (!pid || !title) {
          setNote('Escribe un título válido para el proyecto.');
          return;
        }
        const renameGithubRepo = Boolean(
          renameGithubWrap &&
          renameGithubCheckbox &&
          renameGithubWrap.style.display !== 'none' &&
          renameGithubCheckbox.checked
        );
        saveBtn.disabled = true;
        try {
          const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/title', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title, rename_github_repo: renameGithubRepo }),
          });
          closeEditor();
          state.lastBannerSignature = null;
          setNote(
            renameGithubRepo && res && res.repo_url
              ? 'Título y repositorio en GitHub actualizados.'
              : 'Título actualizado.'
          );
          await loadProjects();
        } catch (err) {
          setNote('Error actualizando título: ' + err.message);
        } finally {
          saveBtn.disabled = false;
        }
      });
    })();

    (function initLogoUpload() {
      const btn = document.getElementById('change-logo-btn');
      const fileInput = document.getElementById('logo-file-input');
      if (!btn || !fileInput) return;

      btn.addEventListener('click', () => {
        if (!state.detail || !state.detail.project_id) return;
        fileInput.value = '';
        fileInput.click();
      });

      fileInput.addEventListener('change', () => {
        const pid = state.detail && state.detail.project_id;
        const file = fileInput.files && fileInput.files[0];
        if (!pid || !file) return;
        if (file.size > 512 * 1024) {
          setNote('El archivo de logo supera el máximo de 512 KB.');
          return;
        }
        const reader = new FileReader();
        reader.onload = async () => {
          try {
            const result = String(reader.result || '');
            const b64Idx = result.indexOf('base64,');
            const b64 = b64Idx >= 0 ? result.slice(b64Idx + 7) : result;
            const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/logo', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ filename: file.name || 'logo.png', data_base64: b64 }),
            });
            if (res && res.logo_url) {
              const detailLogo = document.getElementById('detail-logo-img');
              if (detailLogo) detailLogo.setAttribute('src', res.logo_url);
            }
            setNote('Logo actualizado.');
            await loadProjects();
          } catch (err) {
            setNote('Error subiendo logo: ' + err.message);
          }
        };
        reader.readAsDataURL(file);
      });
    })();

    (function initNewProjectForm() {
      const toggleBtn = document.getElementById('toggle-new-project-btn');
      const cancelBtn = document.getElementById('cancel-new-project-btn');
      const form = document.getElementById('new-project-form');
      const input = document.getElementById('new-project-input');
      const submitBtn = document.getElementById('submit-new-project-btn');
      wireMicButton('new-project-mic-btn', 'new-project-input');

      toggleBtn.addEventListener('click', () => {
        const showing = form.style.display !== 'none';
        form.style.display = showing ? 'none' : 'block';
        if (!showing) {
          input.focus();
        }
      });

      cancelBtn.addEventListener('click', () => {
        form.style.display = 'none';
        input.value = '';
      });

      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const prompt = (input.value || '').trim();
        if (!prompt) {
          setNote('Describe qué quieres construir.');
          return;
        }
        submitBtn.disabled = true;
        try {
          const res = await apiFetch('/api/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
          });
          input.value = '';
          form.style.display = 'none';
          if (res.project_id) {
            state.selectedId = res.project_id;
            state.lastBannerSignature = null;
          }
          setNote('Proyecto iniciado: ' + (res.project_id || '') + '.');
          await loadProjects();
        } catch (err) {
          setNote('Error creando proyecto: ' + err.message);
        } finally {
          submitBtn.disabled = false;
        }
      });
    })();

    (function initImportProjectModal() {
      const openBtn = document.getElementById('toggle-import-project-btn');
      const modal = document.getElementById('import-project-modal');
      const form = document.getElementById('import-project-form');
      const pathInput = document.getElementById('import-path-input');
      const titleInput = document.getElementById('import-title-input');
      const cancelBtn = document.getElementById('cancel-import-project-btn');
      const submitBtn = document.getElementById('submit-import-project-btn');
      if (!openBtn || !modal || !form || !pathInput || !cancelBtn || !submitBtn) return;

      function closeModal() {
        modal.style.display = 'none';
        pathInput.value = '';
        if (titleInput) titleInput.value = '';
      }

      openBtn.addEventListener('click', () => {
        modal.style.display = 'flex';
        pathInput.focus();
      });
      cancelBtn.addEventListener('click', closeModal);
      modal.addEventListener('click', (ev) => {
        if (ev.target === modal) closeModal();
      });
      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const localPath = (pathInput.value || '').trim();
        const title = titleInput ? (titleInput.value || '').trim() : '';
        if (!localPath) {
          setNote('Ingresa la ruta de la carpeta local a importar.');
          return;
        }
        await runTrackedOperation(
          {
            label: 'Analizando archivos del repositorio e importando...',
            subtext: 'Registrando carpeta local en la fábrica.',
            buttonEl: submitBtn,
            inlineTargetId: 'import-inline-status',
          },
          async () => {
            try {
              const res = await apiFetch('/api/projects/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ local_path: localPath, title }),
              });
              closeModal();
              if (res.project_id) {
                state.selectedId = res.project_id;
                state.lastBannerSignature = null;
                state.lastModifySignature = null;
              }
              setNote('Proyecto importado: ' + (res.project_id || '') + '.');
              await loadProjects();
            } catch (err) {
              setNote('Error importando proyecto: ' + err.message);
            }
          }
        );
      });
    })();

    (function initVoiceAndLanToggles() {
      const ttsToggle = document.getElementById('voice-tts-toggle');
      if (ttsToggle) {
        let savedTts = false;
        try { savedTts = localStorage.getItem('af_voice_tts') === '1'; } catch (_) {}
        state.voiceTts = savedTts;
        ttsToggle.checked = savedTts;
        ttsToggle.addEventListener('change', () => {
          state.voiceTts = Boolean(ttsToggle.checked);
          try { localStorage.setItem('af_voice_tts', state.voiceTts ? '1' : '0'); } catch (_) {}
          if (!state.voiceTts && 'speechSynthesis' in window) {
            try { window.speechSynthesis.cancel(); } catch (_) {}
          }
        });
      }

      const lanToggle = document.getElementById('lan-mode-toggle');
      if (lanToggle) {
        apiFetch('/api/settings/lan-mode')
          .then((res) => {
            if (res && typeof res.lan_mode === 'boolean') {
              lanToggle.checked = res.lan_mode;
            }
          })
          .catch(() => {});
        lanToggle.addEventListener('change', async () => {
          const desired = Boolean(lanToggle.checked);
          try {
            const res = await apiFetch('/api/settings/lan-mode', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ lan_mode: desired }),
            });
            setNote(
              res.lan_mode
                ? ('Acceso Wi-Fi activado' + (res.lan_ip ? (' (' + res.lan_ip + ')') : '') + '. Apaga y enciende tu app para aplicar.')
                : 'Acceso Wi-Fi desactivado (solo 127.0.0.1).'
            );
          } catch (err) {
            lanToggle.checked = !desired;
            setNote('Error cambiando acceso Wi-Fi: ' + err.message);
          }
        });
      }
    })();

    setInterval(updateLiveTimers, 1000);
    setInterval(() => {
      loadProjects();
    }, 5000);
    loadProjects();
  </script>
</body>
</html>
"""


class DashboardHTTPServer(http.server.ThreadingHTTPServer):
    """Thread-capable local HTTP server bound strictly to 127.0.0.1."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        controller: DashboardController,
    ) -> None:
        host, port = server_address
        if host != LOOPBACK_HOST:
            raise ValueError(
                f"DashboardHTTPServer must bind exclusively to {LOOPBACK_HOST}, got '{host}'"
            )
        self.controller = controller
        super().__init__((host, port), _DashboardRequestHandler)

    def server_close(self) -> None:
        """Ensure any locally launched apps stop cleanly when HTTP server shuts down."""
        try:
            self.controller.local_runner.stop_all()
        finally:
            super().server_close()


class _DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler serving the self-contained dashboard UI and JSON REST endpoints."""

    server: DashboardHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        """Silence stderr access logs and route diagnostics at debug level."""
        logger.debug("dashboard: " + format, *args)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        scrubbed = _scrub_value(payload, self.server.controller.secrets_to_redact)
        body = json.dumps(scrubbed, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status_code: int, content_type: str, body: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _extract_uploaded_logo_payload(self) -> tuple[bytes, str]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            raise ValueError("Cuerpo vacío al subir logo.")
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        if length <= 0:
            raise ValueError("Cuerpo vacío al subir logo.")
        if length > 2 * 1024 * 1024:
            raise ValueError("El tamaño de la petición supera el máximo permitido (512 KB imagen).")

        content_type = str(self.headers.get("Content-Type", "")).strip()
        if "multipart/form-data" in content_type.lower():
            raw_bytes = self.rfile.read(length)
            msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
                + raw_bytes
            )
            if msg.is_multipart():
                for part in msg.iter_parts():
                    fn = part.get_filename()
                    disp = str(part.get("Content-Disposition", ""))
                    if fn or "filename=" in disp or part.get_content_maintype() == "image":
                        payload = part.get_payload(decode=True) or b""
                        return payload, str(fn or "")
            raise ValueError("No se encontró archivo de imagen válido en multipart/form-data.")

        raw_bytes = self.rfile.read(length)
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON request body") from exc
        if not isinstance(parsed, dict):
            raise TypeError("JSON request body must be an object")

        raw_b64 = parsed.get("data_base64")
        if not isinstance(raw_b64, str) or not raw_b64.strip():
            raise ValueError("El campo 'data_base64' es obligatorio.")
        b64_clean = raw_b64.strip()
        if "base64," in b64_clean[:64]:
            b64_clean = b64_clean.split("base64,", 1)[1].strip()
        try:
            decoded = base64.b64decode(b64_clean, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Codificación base64 inválida para el logo.") from exc
        filename = str(parsed.get("filename") or "").strip()
        return decoded, filename

    def _drain_body(self) -> None:
        raw_len = self.headers.get("Content-Length")
        if raw_len:
            try:
                length = int(raw_len)
                if length > 0:
                    self.rfile.read(min(length, 1024 * 1024))
            except ValueError:
                pass

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ValueError("Request body too large")
        raw_bytes = self.rfile.read(length)
        if not raw_bytes.strip():
            return {}
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON request body") from exc
        if not isinstance(parsed, dict):
            raise TypeError("JSON request body must be an object")
        return parsed

    @staticmethod
    def _require_str_field(body: dict[str, Any], field: str) -> str:
        val = body.get(field)
        if not isinstance(val, str):
            raise TypeError(f"El campo '{field}' es obligatorio y debe ser texto.")
        clean = val.strip()
        if not clean:
            raise ValueError(f"El campo '{field}' es obligatorio.")
        return clean

    def _parse_project_route(self) -> tuple[str | None, str | None]:
        """Parse /api/projects/<project_id>[/<action>] enforcing raw path safety."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/projects/"
        if not raw_path.startswith(prefix):
            return None, None
        rest = raw_path[len(prefix) :]
        if not rest:
            return None, None
        parts = rest.split("/")
        if len(parts) == 1:
            return urllib.parse.unquote(parts[0]), None
        if len(parts) == 2 and parts[1] in {
            "approve",
            "retry",
            "message",
            "start",
            "stop",
            "modify",
            "title",
            "suggest-title",
            "logo",
            "chat",
            "checkpoints",
            "files",
            "diff",
            "rollback",
        }:
            return urllib.parse.unquote(parts[0]), parts[1]
        # Reject multi-segment or path-traversal attempts (e.g., ../etc) as invalid project_id
        return urllib.parse.unquote(rest), None

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        ctrl = self.server.controller

        if path == "/" or path == "/index.html":
            self._send_html(200, DASHBOARD_HTML)
            return

        if path == "/api/projects":
            self._send_json(200, {"projects": ctrl.list_projects()})
            return

        if path == "/api/settings/lan-mode":
            self._send_json(200, ctrl.get_lan_mode())
            return

        pid, action = self._parse_project_route()
        if pid is not None and action == "logo":
            try:
                mime, data = ctrl.get_project_logo(pid)
            except (ValueError, SandboxViolationError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ProjectNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            self._send_bytes(200, mime, data)
            return

        get_json_actions = {
            None: ctrl.get_project_detail,
            "chat": ctrl.get_project_chat,
            "checkpoints": ctrl.get_project_checkpoints,
            "files": ctrl.get_project_files,
            "diff": ctrl.get_project_diff,
        }
        if pid is not None and action in get_json_actions:
            try:
                payload = get_json_actions[action](pid)
            except (ValueError, SandboxViolationError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ProjectNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            self._send_json(200, payload)
            return

        self._send_json(404, {"error": "Endpoint not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        ctrl = self.server.controller

        if path == "/api/projects":
            try:
                body = self._read_json_body()
                prompt = self._require_str_field(body, "prompt")
                result = ctrl.create_project(prompt)
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ProjectConflictError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        if path == "/api/projects/import":
            try:
                body = self._read_json_body()
                local_path = self._require_str_field(body, "local_path")
                raw_title = body.get("title", "")
                if raw_title is not None and not isinstance(raw_title, str):
                    raise TypeError("El campo 'title' debe ser texto.")
                result = ctrl.import_external_project(local_path, title=str(raw_title or ""))
            except (ValueError, TypeError, SandboxViolationError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ProjectConflictError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        if path == "/api/settings/lan-mode":
            try:
                body = self._read_json_body()
                raw_flag = body.get("lan_mode")
                if raw_flag is None and "enabled" in body:
                    raw_flag = body.get("enabled")
                if not isinstance(raw_flag, bool):
                    raise TypeError("El campo 'lan_mode' debe ser booleano.")
                result = ctrl.set_lan_mode(raw_flag)
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        pid, action = self._parse_project_route()
        if pid is None or action not in {
            "approve",
            "retry",
            "message",
            "start",
            "stop",
            "modify",
            "suggest-title",
            "logo",
            "chat",
            "rollback",
        }:
            self._drain_body()
            self._send_json(404, {"error": "Endpoint not found"})
            return

        try:
            if action == "approve":
                self._drain_body()
                result = ctrl.approve_project(pid)
            elif action == "retry":
                body = self._read_json_body()
                raw_clean = body.get("clean_workspace", False)
                if not isinstance(raw_clean, bool):
                    raise TypeError("El campo 'clean_workspace' debe ser booleano.")
                result = ctrl.retry_project(pid, clean_workspace=raw_clean)
            elif action == "start":
                self._drain_body()
                result = ctrl.start_local_app(pid)
            elif action == "stop":
                self._drain_body()
                result = ctrl.stop_local_app(pid)
            elif action == "modify":
                body = self._read_json_body()
                instructions = self._require_str_field(body, "instructions")
                raw_targets = body.get("target_files")
                if raw_targets is not None and not isinstance(raw_targets, list):
                    raise TypeError("El campo 'target_files' debe ser una lista de rutas.")
                result = ctrl.modify_project(pid, instructions, target_files=raw_targets)
            elif action == "rollback":
                body = self._read_json_body()
                commit_sha = self._require_str_field(body, "commit_sha")
                result = ctrl.rollback_project(pid, commit_sha)
            elif action == "suggest-title":
                self._drain_body()
                result = ctrl.suggest_project_title(pid)
            elif action == "logo":
                raw_logo, filename = self._extract_uploaded_logo_payload()
                result = ctrl.upload_project_logo(pid, raw_logo, filename=filename)
            elif action == "chat":
                body = self._read_json_body()
                raw_mode = body.get("mode", "chat")
                if not isinstance(raw_mode, str):
                    raise TypeError("El campo 'mode' debe ser texto.")
                raw_msg = body.get("message", "")
                if raw_msg is not None and not isinstance(raw_msg, str):
                    raise TypeError("El campo 'message' debe ser texto.")
                result = ctrl.post_project_chat(pid, str(raw_msg or ""), mode=raw_mode)
            else:
                body = self._read_json_body()
                message = self._require_str_field(body, "message")
                result = ctrl.send_message(pid, message)
        except (ValueError, TypeError, SandboxViolationError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except ProjectNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
            return
        except ProjectConflictError as exc:
            self._send_json(409, {"error": str(exc)})
            return

        self._send_json(200, result)

    def do_PATCH(self) -> None:
        ctrl = self.server.controller
        pid, action = self._parse_project_route()
        if pid is None or action != "title":
            self._drain_body()
            self._send_json(404, {"error": "Endpoint not found"})
            return

        try:
            body = self._read_json_body()
            title = self._require_str_field(body, "title")
            raw_rename_repo = body.get("rename_github_repo")
            if raw_rename_repo is not None and not isinstance(raw_rename_repo, bool):
                raise ValueError("Field 'rename_github_repo' must be a boolean")
            result = ctrl.rename_project(
                pid,
                title,
                rename_github_repo=raw_rename_repo,
            )
        except (ValueError, TypeError, SandboxViolationError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except ProjectNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
            return
        except ProjectConflictError as exc:
            self._send_json(409, {"error": str(exc)})
            return

        self._send_json(200, result)

    def do_DELETE(self) -> None:
        self._drain_body()
        ctrl = self.server.controller
        pid, action = self._parse_project_route()

        if pid is None or action is not None:
            self._send_json(404, {"error": "Endpoint not found"})
            return

        try:
            result = ctrl.delete_project(pid)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except ProjectNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
            return
        except ProjectConflictError as exc:
            self._send_json(409, {"error": str(exc)})
            return

        self._send_json(200, result)


def start_dashboard_server(
    *,
    orchestrator: Orchestrator,
    port: int = 8000,
    build_worker: BuildWorker | None = None,
    template_scaffold: TemplateScaffold | None = None,
    workspace_manager: WorkspaceManager | None = None,
    bot_service: TelegramBotService | None = None,
    model_resolver: Any = None,
    local_runner: LocalAppRunner | None = None,
    logos_dir: Path | None = None,
    chats_dir: Path | None = None,
    vcs: Any | None = None,
    runner: Runner | None = None,
    secrets_to_redact: Sequence[str] = (),
) -> tuple[DashboardHTTPServer, threading.Thread, str]:
    """Create and start DashboardHTTPServer on 127.0.0.1:<port> in a daemon thread."""
    controller = DashboardController(
        orchestrator=orchestrator,
        build_worker=build_worker,
        template_scaffold=template_scaffold,
        workspace_manager=workspace_manager,
        bot_service=bot_service,
        model_resolver=model_resolver,
        local_runner=local_runner,
        logos_dir=logos_dir,
        chats_dir=chats_dir,
        vcs=vcs,
        runner=runner,
        secrets_to_redact=secrets_to_redact,
    )
    server = DashboardHTTPServer((LOOPBACK_HOST, port), controller)
    actual_port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever,
        name="agent-factory-dashboard",
        daemon=True,
    )
    thread.start()
    return server, thread, f"http://{LOOPBACK_HOST}:{actual_port}"
