"""Local loopback HTTP dashboard server and JSON API for monitoring and controlling projects (INV-07, INV-19, INV-22)."""

from __future__ import annotations

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

from agent_factory.builder import BuildWorker
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import ApprovalEvent, ProjectStatus, validate_project_id
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import ApprovalError, Orchestrator
from agent_factory.ports.code_agent import CodeAgentError
from agent_factory.ports.llm import LLMError
from agent_factory.ports.runner import CommandTimeoutError
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotService, _slugify
from agent_factory.workspace import SandboxViolationError, WorkspaceManager

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
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        self.orchestrator = orchestrator
        self.build_worker = build_worker
        self.template_scaffold = template_scaffold
        self.workspace_manager = workspace_manager
        self.bot_service = bot_service
        if model_resolver is not None:
            self.model_resolver = model_resolver
        else:
            self.model_resolver = getattr(orchestrator.llm, "model_resolver", None) or getattr(
                getattr(build_worker, "code_agent", None), "model_resolver", None
            )
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self._lock = threading.RLock()
        self._active_projects: set[str] = set()
        self._threads: list[threading.Thread] = []

    def _get_live_status_info(self, running: bool) -> dict[str, Any]:
        """Return live active model, stage, activity summary, agent count, and start time when running."""
        empty: dict[str, Any] = {
            "active_model": None,
            "active_stage": None,
            "activity_summary": None,
            "agents_count": None,
            "step_started_at": None,
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

        raw_count = live.get("agents_count")
        agents_count = int(raw_count) if isinstance(raw_count, (int, float)) and raw_count > 0 else None
        return {
            "active_model": _scrub_opt(live.get("model")),
            "active_stage": _scrub_opt(live.get("stage")),
            "activity_summary": _scrub_opt(live.get("activity_summary")),
            "agents_count": agents_count,
            "step_started_at": _scrub_opt(live.get("step_started_at")),
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
        raw["active_model"] = live_info["active_model"]
        raw["active_stage"] = live_info["active_stage"]
        raw["activity_summary"] = live_info["activity_summary"]
        raw["agents_count"] = live_info["agents_count"]
        raw["step_started_at"] = live_info["step_started_at"]
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
                        "active_model": live_info["active_model"],
                        "active_stage": live_info["active_stage"],
                        "activity_summary": live_info["activity_summary"],
                        "agents_count": live_info["agents_count"],
                        "step_started_at": live_info["step_started_at"],
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
            "active_model": summary["active_model"],
            "active_stage": summary["active_stage"],
            "activity_summary": summary["activity_summary"],
            "agents_count": summary["agents_count"],
            "step_started_at": summary["step_started_at"],
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
        """Create a new project from prompt and start its first INTERVIEW round in background."""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("El campo 'prompt' es obligatorio.")

        with self._lock:
            pid = self._next_project_id(clean_prompt)
            created = self.orchestrator.create_project(
                pid,
                name=clean_prompt,
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

    def retry_project(self, project_id: str) -> dict[str, Any]:
        """Retry a FAILED or paused project, cleaning partial workspace, and resume pipeline if applicable."""
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
            ws_mgr = self._resolve_workspace_manager()
            ws_dir: Path | None = None
            if ws_mgr is not None:
                ws_dir = ws_mgr.get_workspace_dir(project_id)
            had_workspace = ws_dir is not None and ws_dir.exists()

            if not had_events and not had_workspace:
                raise ProjectNotFoundError(f"Project '{project_id}' not found")

            self.orchestrator.delete_project(project_id)

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
      --muted: #64748b;
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
    .progress-track {
      width: 100%;
      height: 5px;
      background: var(--surface-alt);
      border-radius: 999px;
      overflow: hidden;
      margin: 6px 0;
    }
    .progress-fill {
      height: 100%;
      background: var(--accent);
      border-radius: 999px;
      transition: width 0.3s ease;
    }
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
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="live-dot" title="Sincronización activa"></span>
      <span>Fábrica de Proyectos</span>
      <span id="status-note" class="toast"></span>
    </div>
    <div class="controls">
      <button id="theme-btn" class="btn" type="button">Modo claro</button>
    </div>
  </header>

  <main class="layout">
    <section class="panel">
      <div class="panel-header">
        <h2 style="margin:0; font-size:1.15rem;">Tus proyectos</h2>
        <button id="toggle-new-project-btn" class="btn btn-sm" type="button">+ Nuevo proyecto</button>
      </div>
      <form id="new-project-form" class="new-project-form" style="display:none">
        <textarea id="new-project-input" class="input-field" rows="3" placeholder="¿Qué quieres construir?" required></textarea>
        <div class="new-project-actions">
          <button id="cancel-new-project-btn" class="btn btn-sm" type="button">Cancelar</button>
          <button id="submit-new-project-btn" class="btn btn-primary btn-sm" type="submit">Comenzar</button>
        </div>
      </form>
      <div id="project-list" class="muted" style="margin-top:14px;">Cargando proyectos...</div>
    </section>

    <section class="panel" id="detail-panel">
      <div id="detail-empty" class="empty-box">Selecciona un proyecto de la lista para ver su estado y detalles.</div>
      <div id="detail-content" style="display:none">
        <div class="detail-header">
          <div class="detail-titles">
            <h2 id="detail-title" class="detail-title"></h2>
          </div>
          <span id="detail-badge" class="badge"></span>
        </div>
        <div class="progress-track"><div id="detail-progress" class="progress-fill" style="width:0%"></div></div>
        <div id="detail-time" class="muted" style="font-size:0.78rem; margin:-2px 0 10px 0; text-align:right;"></div>

        <div id="detail-guidance"></div>

        <div class="tabs">
          <button class="tab-btn active" data-tab="spec" type="button">Resumen</button>
          <button class="tab-btn" data-tab="plan" type="button">Plan</button>
          <button class="tab-btn" data-tab="events" type="button">Historial</button>
        </div>
        <div id="tab-spec" class="tab-content"></div>
        <div id="tab-plan" class="tab-content" style="display:none"></div>
        <div id="tab-events" class="tab-content" style="display:none"></div>

        <div id="detail-secondary"></div>
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
      adjustOpen: {},
      phaseOpen: {},
      lastBannerSignature: null,
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

    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.activeTab = btn.dataset.tab;
        renderTabs();
      });
    });

    function renderTabs() {
      document.querySelectorAll('.tab-btn').forEach((b) => {
        b.classList.toggle('active', b.dataset.tab === state.activeTab);
      });
      document.getElementById('tab-spec').style.display = state.activeTab === 'spec' ? 'block' : 'none';
      document.getElementById('tab-plan').style.display = state.activeTab === 'plan' ? 'block' : 'none';
      document.getElementById('tab-events').style.display = state.activeTab === 'events' ? 'block' : 'none';
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
        const pct = Number(p.progress_pct || 0);
        const running = Boolean(p.is_running || p.busy);
        const pausedActive = !running && (
          p.state === 'DEBATE' || p.state === 'MEDIATION' ||
          p.state === 'BUILDING' || p.state === 'VERIFYING'
        );
        const statusTag = running
          ? ' <span class="busy-pill">(en curso)</span>'
          : (pausedActive ? ' <span class="busy-pill" style="color:var(--warn)">(pausado)</span>' : '');
        const badgeCls = pausedActive ? 'badge-warn' : info.cls;
        const timeHtml = p.last_event_at
          ? '<div class="muted" style="font-size:0.75rem; margin-top:4px; text-align:right;" title="' + escapeHtml(p.last_event_at) + '">' + escapeHtml(formatFriendlyTime(p.last_event_at)) + '</div>'
          : '';
        return '<div class="project-card ' + selected + '" data-id="' + escapeHtml(p.project_id) + '">' +
          '<div class="card-header">' +
            '<div class="card-title">' + title + statusTag + '</div>' +
            '<span class="badge ' + badgeCls + '">' + escapeHtml(info.cardStep || info.step) + '</span>' +
          '</div>' +
          '<div class="progress-track"><div class="progress-fill" style="width:' + pct + '%"></div></div>' +
          timeHtml +
        '</div>';
      }).join('');

      container.querySelectorAll('.project-card').forEach((card) => {
        card.addEventListener('click', () => {
          state.selectedId = card.dataset.id;
          state.lastBannerSignature = null;
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
          if (pl.agent === 'implement_or_fix') return 'Escribiendo código';
          if (pl.state === 'VERIFYING') return 'Verificando funcionamiento';
          if (pl.state === 'DEBATE') return 'Evaluación de especialistas';
          if (pl.state === 'MEDIATION') return 'Diseñando plan técnico';
          return 'Trabajando en la construcción';
        case 'TESTS_UPDATED':
          return (pl.total !== undefined)
            ? ('Verificando funcionamiento (' + (pl.passed || 0) + '/' + pl.total + ' pruebas)')
            : 'Verificando funcionamiento';
        case 'CI_ROUND_COMPLETED':
          return 'Verificando funcionamiento en CI';
        case 'PROJECT_RETRIED':
          return 'Reintento iniciado';
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
        case 'TESTS_UPDATED':
        case 'CI_ROUND_COMPLETED':
        case 'PROJECT_RETRIED':
          return 4;
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

    function updateLiveTimers() {
      document.querySelectorAll('.live-step-timer[data-started-at]').forEach((el) => {
        const startedAt = el.getAttribute('data-started-at');
        const formatted = formatElapsedSince(startedAt);
        if (formatted) {
          el.textContent = 'Tiempo en este paso: ' + formatted;
        }
      });
    }

    function buildGuidanceBanner(st, events) {
      const isRunning = Boolean(st.is_running || st.busy);
      if (isRunning) {
        const descText = st.activity_summary
          ? escapeHtml(st.activity_summary)
          : 'Los agentes están avanzando en esta etapa. Esta pantalla se actualiza sola cada 5 segundos.';
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
              '<div class="model-usage-row">' + agentsPill + modelPill + timerPill + '</div>' +
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
            '<textarea id="interview-reply-input" class="input-field" rows="3" placeholder="Escribe tu respuesta aquí...">' + draft + '</textarea>' +
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
          '</div>' +
        '</div>';
      }

      if (st.state === 'FAILED' || st.state === 'ERROR') {
        return '<div class="guidance-banner banner-err">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">⚠️</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Se requiere atención</div>' +
              '<div class="banner-desc">El proceso se detuvo por un inconveniente técnico. Puedes volver a intentarlo con un clic.</div>' +
              '<details class="tech-details">' +
                '<summary>Ver detalle técnico</summary>' +
                '<pre>' + escapeHtml(st.last_error || 'Sin detalle técnico disponible.') + '</pre>' +
              '</details>' +
            '</div>' +
          '</div>' +
          '<div class="banner-actions">' +
            '<button class="btn btn-warn" data-dact="retry">Volver a intentar</button>' +
          '</div>' +
        '</div>';
      }

      if (st.state === 'DELIVERED' || st.state === 'COMPLETED') {
        const repoBtn = (st.links && st.links.repo)
          ? '<a href="' + escapeHtml(st.links.repo) + '" target="_blank" rel="noopener noreferrer" class="btn btn-primary">Abrir repositorio en GitHub</a>'
          : '';
        const deployBtn = (st.links && st.links.deploy)
          ? '<a href="' + escapeHtml(st.links.deploy) + '" target="_blank" rel="noopener noreferrer" class="btn">Ver despliegue en vivo</a>'
          : '';
        return '<div class="guidance-banner banner-ok">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">🎉</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">¡Proyecto terminado con éxito!</div>' +
              '<div class="banner-desc">El proyecto ha sido construido y verificado exitosamente.</div>' +
            '</div>' +
          '</div>' +
          ((repoBtn || deployBtn) ? '<div class="banner-actions">' + repoBtn + deployBtn + '</div>' : '') +
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

      const badge = document.getElementById('detail-badge');
      badge.textContent = info.step;
      badge.className = 'badge ' + info.cls;
      document.getElementById('detail-progress').style.width = Number(st.progress_pct || 0) + '%';
      const timeBox = document.getElementById('detail-time');
      if (timeBox) {
        timeBox.innerHTML = st.last_event_at
          ? '<span title="' + escapeHtml(st.last_event_at) + '">Actualizado: ' + escapeHtml(formatFriendlyTime(st.last_event_at)) + '</span>'
          : '';
      }

      // Contextual guidance banner (only rebuild when signature changes so active drafts stay intact)
      const isRunning = Boolean(st.is_running || st.busy || (state.detail && state.detail.is_running));
      const activeModel = st.active_model || (state.detail && state.detail.active_model) || null;
      const activeStage = st.active_stage || (state.detail && state.detail.active_stage) || null;
      const activitySummary = st.activity_summary || (state.detail && state.detail.activity_summary) || null;
      const agentsCount = st.agents_count || (state.detail && state.detail.agents_count) || null;
      const stepStartedAt = st.step_started_at || (state.detail && state.detail.step_started_at) || null;
      const statusForBanner = Object.assign({}, st, {
        is_running: isRunning,
        active_model: activeModel,
        active_stage: activeStage,
        activity_summary: activitySummary,
        agents_count: agentsCount,
        step_started_at: stepStartedAt,
      });
      const questions = extractLatestQuestions(events);
      const inact = st.state === 'INTERVIEW' && !isRunning ? formatInactivityNote(st.last_event_at) : '';
      const bannerSig = JSON.stringify([
        st.project_id,
        st.state,
        isRunning,
        activeModel,
        activeStage,
        activitySummary,
        agentsCount,
        stepStartedAt,
        st.last_event_at,
        st.last_error,
        questions,
        inact,
        st.links,
      ]);

      const guidanceEl = document.getElementById('detail-guidance');
      if (bannerSig !== state.lastBannerSignature) {
        guidanceEl.innerHTML = buildGuidanceBanner(statusForBanner, events);
        state.lastBannerSignature = bannerSig;

        guidanceEl.querySelectorAll('button[data-dact]').forEach((btn) => {
          btn.addEventListener('click', () => handleAction(btn.dataset.dact, st.project_id));
        });

        const replyInput = document.getElementById('interview-reply-input');
        const replyBtn = document.getElementById('interview-reply-btn');
        if (replyInput) {
          replyInput.addEventListener('input', () => {
            state.replyDrafts[st.project_id] = replyInput.value;
          });
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

      // Secondary action (Delete)
      const secEl = document.getElementById('detail-secondary');
      secEl.innerHTML =
        '<div class="secondary-actions">' +
          '<button class="btn-subtle-danger" data-dact="delete">Eliminar proyecto</button>' +
        '</div>';

      secEl.querySelectorAll('button[data-dact]').forEach((btn) => {
        btn.addEventListener('click', () => handleAction(btn.dataset.dact, st.project_id));
      });

      // Spec tab ("Resumen")
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
      if (!fields.length) {
        document.getElementById('tab-spec').innerHTML = '<div class="empty-box">Aún estamos definiendo los detalles de este proyecto.</div>';
      } else {
        document.getElementById('tab-spec').innerHTML = fields.join('');
      }

      // Plan tab ("Plan")
      const plan = state.detail.plan;
      if (!plan) {
        document.getElementById('tab-plan').innerHTML = '<div class="empty-box">Aún no se ha generado un Plan.</div>';
      } else {
        const filesHtml = (plan.files || []).map((f) =>
          '<div class="file-card"><div class="file-path">' + escapeHtml(f.path) + '</div><div class="file-purpose">' + escapeHtml(f.purpose) + '</div>' +
          (f.contract ? '<details class="tech-details"><summary>Contrato</summary><pre>' + escapeHtml(f.contract) + '</pre></details>' : '') +
          '</div>'
        ).join('');
        const testsHtml = (plan.acceptance_tests || []).map((t) =>
          '<li><strong>[' + escapeHtml(t.id) + '] (' + escapeHtml(t.type) + ')</strong>: ' + escapeHtml(t.description) + '</li>'
        ).join('');
        document.getElementById('tab-plan').innerHTML =
          '<div class="plan-summary"><strong>Resumen:</strong> ' + escapeHtml(plan.summary) + '</div>' +
          (plan.rationale ? '<div class="plan-summary"><strong>Justificación:</strong> ' + escapeHtml(plan.rationale) + '</div>' : '') +
          '<div class="plan-meta">' +
            '<span class="badge badge-info">Plantilla: ' + escapeHtml(plan.template_id) + '</span>' +
            '<span class="badge badge-info">Target: ' + escapeHtml(plan.deployment_target) + '</span>' +
          '</div>' +
          '<h4 style="margin:14px 0 8px 0;">Archivos (' + (plan.files || []).length + ')</h4>' + filesHtml +
          '<h4 style="margin:14px 0 8px 0;">Pruebas de aceptación (' + (plan.acceptance_tests || []).length + ')</h4><ul style="padding-left:20px; font-size:0.92rem;">' + testsHtml + '</ul>';
      }

      // Events tab ("Historial" grouped into 5 clear lifecycle phases)
      const LIFECYCLE_PHASES = [
        { num: 1, id: 'idea', title: '1. Idea y requisitos' },
        { num: 2, id: 'design', title: '2. Diseño de la solución' },
        { num: 3, id: 'approval', title: '3. Aprobación del plan' },
        { num: 4, id: 'build', title: '4. Construcción y pruebas' },
        { num: 5, id: 'delivery', title: '5. Entrega final' },
      ];

      const enriched = events.map((ev, idx) => {
        const nextEv = (idx + 1 < events.length) ? events[idx + 1] : null;
        let durSecs = null;
        if (ev.payload && typeof ev.payload.duration_seconds === 'number' && isFinite(ev.payload.duration_seconds)) {
          durSecs = ev.payload.duration_seconds;
        } else if (nextEv && ev.at && nextEv.at) {
          const diffSecs = (Date.parse(nextEv.at) - Date.parse(ev.at)) / 1000;
          if (isFinite(diffSecs) && diffSecs >= 0) {
            durSecs = diffSecs;
          }
        }
        return {
          ev,
          phaseNum: phaseNumForEvent(ev),
          durationText: durSecs !== null ? formatDurationHuman(durSecs, '< 1s') : '',
        };
      });

      const isDelivered = st.state === 'DELIVERED' || st.state === 'COMPLETED';
      const isFailed = st.state === 'FAILED' || st.state === 'ERROR';
      const activePhaseNum = isDelivered ? 6 : info.stage;

      const eventsEl = document.getElementById('tab-events');
      eventsEl.innerHTML = '<div class="phase-list">' + LIFECYCLE_PHASES.map((ph) => {
        const phaseItems = enriched.filter((it) => it.phaseNum === ph.num);
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
          const durStr = computePhaseDuration(ph.num, phaseItems, enriched);
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

        const stepsHtml = phaseItems.length
          ? '<div class="timeline">' + phaseItems.map((item) => {
              const ev = item.ev;
              const isErr = ev.type === 'PHASE_ERROR' || ev.type === 'PROJECT_FAILED';
              const isSuccess = ev.type === 'PROJECT_DELIVERED' || ev.type === 'PROJECT_COMPLETED' || ev.type === 'COMPLETED';
              let dotClass = 'info';
              if (isErr) dotClass = 'err';
              else if (isSuccess) dotClass = 'ok';
              const rawDate = ev.at ? String(ev.at) : '';
              const friendlyDate = rawDate ? escapeHtml(formatFriendlyTime(rawDate)) : '';
              const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
              const actionTitle = friendlyEventAction(ev);
              const payloadModel = (ev.payload && ev.payload.model) ? String(ev.payload.model) : '';
              const modelPill = payloadModel ? '<span class="model-pill">' + escapeHtml(payloadModel) + '</span>' : '';
              const durPill = item.durationText ? '<span class="duration-pill">Tardó ' + escapeHtml(item.durationText) + '</span>' : '';
              return '<div class="timeline-item">' +
                '<div class="timeline-dot ' + dotClass + '"></div>' +
                '<div class="timeline-header">' +
                  '<span class="timeline-title-group">' +
                    '<span class="timeline-type">' + escapeHtml(actionTitle) + '</span>' +
                    modelPill +
                    durPill +
                  '</span>' +
                  '<span class="timeline-time" title="' + escapeHtml(rawDate) + '">' + friendlyDate + '</span>' +
                '</div>' +
                (hasPayload ? '<details class="tech-details" style="margin-top:4px;"><summary>Ver datos técnicos</summary><pre style="margin:4px 0 0 0;">' + escapeHtml(JSON.stringify(ev.payload, null, 2)) + '</pre></details>' : '') +
              '</div>';
            }).join('') + '</div>'
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

      eventsEl.querySelectorAll('details.phase-card[data-phase-key]').forEach((det) => {
        det.addEventListener('toggle', () => {
          state.phaseOpen[det.getAttribute('data-phase-key')] = det.open;
        });
      });

      renderTabs();
    }

    async function loadDetail(pid) {
      if (!pid) return;
      try {
        state.detail = await apiFetch('/api/projects/' + encodeURIComponent(pid));
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

    async function handleAction(action, pid) {
      try {
        if (action === 'delete') {
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
          await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/retry', { method: 'POST' });
          setNote('Reintento iniciado para ' + pid + '.');
        }
        state.lastBannerSignature = null;
        await loadProjects();
      } catch (err) {
        setNote('Error en ' + action + ': ' + err.message);
      }
    }

    (function initNewProjectForm() {
      const toggleBtn = document.getElementById('toggle-new-project-btn');
      const cancelBtn = document.getElementById('cancel-new-project-btn');
      const form = document.getElementById('new-project-form');
      const input = document.getElementById('new-project-input');
      const submitBtn = document.getElementById('submit-new-project-btn');

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
        if len(parts) == 2 and parts[1] in {"approve", "retry", "message"}:
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

        pid, action = self._parse_project_route()
        if pid is not None and action is None:
            try:
                detail = ctrl.get_project_detail(pid)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ProjectNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            self._send_json(200, detail)
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

        pid, action = self._parse_project_route()
        if pid is None or action not in {"approve", "retry", "message"}:
            self._drain_body()
            self._send_json(404, {"error": "Endpoint not found"})
            return

        try:
            if action == "approve":
                self._drain_body()
                result = ctrl.approve_project(pid)
            elif action == "retry":
                self._drain_body()
                result = ctrl.retry_project(pid)
            else:
                body = self._read_json_body()
                message = self._require_str_field(body, "message")
                result = ctrl.send_message(pid, message)
        except (ValueError, TypeError) as exc:
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
