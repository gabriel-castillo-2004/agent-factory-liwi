"""Local loopback HTTP dashboard server and JSON API for monitoring and controlling projects (INV-07, INV-19, INV-22)."""

from __future__ import annotations

import http.server
import json
import logging
import shutil
import threading
import urllib.parse
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
from agent_factory.ports.runner import CommandTimeoutError
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotService
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
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        self.orchestrator = orchestrator
        self.build_worker = build_worker
        self.template_scaffold = template_scaffold
        self.workspace_manager = workspace_manager
        self.bot_service = bot_service
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self._lock = threading.RLock()
        self._active_projects: set[str] = set()
        self._threads: list[threading.Thread] = []

    def is_busy(self, project_id: str) -> bool:
        """Return True if a background task is currently executing for project_id."""
        with self._lock:
            return project_id in self._active_projects

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
        raw = status.model_dump(mode="json")
        raw["last_error"] = last_error
        raw["has_plan"] = has_plan
        raw["busy"] = self.is_busy(project_id)
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
                        "busy": self.is_busy(pid),
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

        return {
            "project_id": project_id,
            "status": self._serialize_summary(
                project_id, status, events, has_plan=(plan is not None)
            ),
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

    def retry_project(self, project_id: str) -> dict[str, Any]:
        """Retry a FAILED project and resume DEBATE/MEDIATION pipeline in background if applicable."""
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
                updated = self.orchestrator.retry_project(project_id)
            except (ValueError, IllegalTransition) as exc:
                raise ProjectConflictError(str(exc)) from exc

            if self.build_worker is not None and self.build_worker.queue is not None:
                self.build_worker.queue.remove(project_id)

            background_started = False
            if updated.state in (ProjectState.DEBATE, ProjectState.MEDIATION):
                def _run_pipeline() -> None:
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
                                f"Reintento de {project_id} falló: {res.failure_reason or 'Error'}",
                            )
                    except (
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
                                agent="dashboard_retry",
                                extra_secrets=self.secrets_to_redact,
                            )

                background_started = self._start_background(project_id, _run_pipeline)

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
            ws_dir: Path | None = None
            if self.workspace_manager is not None:
                ws_dir = self.workspace_manager.get_workspace_dir(project_id)
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

            if ws_dir is not None and ws_dir.exists():
                shutil.rmtree(ws_dir)

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
    .timeline-type {
      font-weight: 600;
      font-size: 0.88rem;
    }
    .timeline-time {
      font-size: 0.78rem;
      color: var(--muted);
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .toast { font-size: 0.85rem; color: var(--accent); }
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
      </div>
      <div id="project-list" class="muted" style="margin-top:14px;">Cargando proyectos...</div>
    </section>

    <section class="panel" id="detail-panel">
      <div id="detail-empty" class="empty-box">Selecciona un proyecto de la lista para ver su estado y detalles.</div>
      <div id="detail-content" style="display:none">
        <div class="detail-header">
          <div class="detail-titles">
            <h2 id="detail-title" class="detail-title"></h2>
            <div id="detail-sub" class="detail-sub muted"></div>
          </div>
          <span id="detail-badge" class="badge"></span>
        </div>
        <div class="progress-track"><div id="detail-progress" class="progress-fill" style="width:0%"></div></div>

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
    };

    function escapeHtml(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function stepInfo(st) {
      switch (st) {
        case 'INTERVIEW':
        case 'CREATED':
          return { step: 'Paso 1: Definiendo detalles', cls: 'badge-info' };
        case 'DEBATE':
        case 'MEDIATION':
          return { step: 'Paso 2: Preparando el plan', cls: 'badge-info' };
        case 'WAITING_APPROVAL':
          return { step: 'Paso 3: Listo para aprobar', cls: 'badge-warn' };
        case 'BUILDING':
        case 'VERIFYING':
          return { step: 'Paso 4: Construyendo...', cls: 'badge-info' };
        case 'DELIVERED':
        case 'COMPLETED':
          return { step: '¡Terminado!', cls: 'badge-ok' };
        case 'FAILED':
        case 'ERROR':
          return { step: 'Necesita atención', cls: 'badge-err' };
        default:
          return { step: st || 'Iniciando', cls: 'badge-info' };
      }
    }

    function setNote(msg) {
      document.getElementById('status-note').textContent = msg || '';
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
        const busyTag = p.busy ? ' <span class="busy-pill">(en curso)</span>' : '';
        return '<div class="project-card ' + selected + '" data-id="' + escapeHtml(p.project_id) + '">' +
          '<div class="card-header">' +
            '<div class="card-title">' + title + busyTag + '</div>' +
            '<span class="badge ' + info.cls + '">' + escapeHtml(info.step) + '</span>' +
          '</div>' +
          '<div class="progress-track"><div class="progress-fill" style="width:' + pct + '%"></div></div>' +
        '</div>';
      }).join('');

      container.querySelectorAll('.project-card').forEach((card) => {
        card.addEventListener('click', () => {
          state.selectedId = card.dataset.id;
          renderProjectList();
          loadDetail(state.selectedId);
        });
      });
    }

    function renderDetail() {
      const empty = document.getElementById('detail-empty');
      const box = document.getElementById('detail-content');
      if (!state.detail) {
        empty.style.display = 'block';
        box.style.display = 'none';
        return;
      }
      empty.style.display = 'none';
      box.style.display = 'block';

      const st = state.detail.status;
      const info = stepInfo(st.state);
      const titleText = st.name || st.objective || st.project_id;
      document.getElementById('detail-title').textContent = titleText;
      document.getElementById('detail-sub').textContent = (st.name && st.objective) ? st.objective : (st.project_id || '');

      const badge = document.getElementById('detail-badge');
      badge.textContent = info.step + ' (' + (st.progress_pct || 0) + '%)';
      badge.className = 'badge ' + info.cls;
      document.getElementById('detail-progress').style.width = Number(st.progress_pct || 0) + '%';

      // Contextual guidance banner
      let bannerHtml = '';
      if (st.state === 'WAITING_APPROVAL') {
        bannerHtml = '<div class="guidance-banner banner-warn">' +
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
        '</div>';
      } else if (st.state === 'FAILED' || st.state === 'ERROR') {
        bannerHtml = '<div class="guidance-banner banner-err">' +
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
      } else if (st.state === 'DELIVERED' || st.state === 'COMPLETED') {
        const repoBtn = (st.links && st.links.repo)
          ? '<a href="' + escapeHtml(st.links.repo) + '" target="_blank" rel="noopener noreferrer" class="btn btn-primary">Abrir repositorio en GitHub</a>'
          : '';
        const deployBtn = (st.links && st.links.deploy)
          ? '<a href="' + escapeHtml(st.links.deploy) + '" target="_blank" rel="noopener noreferrer" class="btn">Ver despliegue en vivo</a>'
          : '';
        bannerHtml = '<div class="guidance-banner banner-ok">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">🎉</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">¡Proyecto terminado con éxito!</div>' +
              '<div class="banner-desc">El proyecto ha sido construido y verificado exitosamente.</div>' +
            '</div>' +
          '</div>' +
          ((repoBtn || deployBtn) ? '<div class="banner-actions">' + repoBtn + deployBtn + '</div>' : '') +
        '</div>';
      } else {
        bannerHtml = '<div class="guidance-banner banner-info">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">⚙️</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Trabajando automáticamente</div>' +
              '<div class="banner-desc">Los agentes están avanzando en esta etapa. Esta pantalla se actualiza sola cada 5 segundos.</div>' +
            '</div>' +
          '</div>' +
        '</div>';
      }
      document.getElementById('detail-guidance').innerHTML = bannerHtml;

      // Secondary action (Delete)
      document.getElementById('detail-secondary').innerHTML =
        '<div class="secondary-actions">' +
          '<button class="btn-subtle-danger" data-dact="delete">Eliminar proyecto</button>' +
        '</div>';

      box.querySelectorAll('button[data-dact]').forEach((btn) => {
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

      // Events tab ("Historial")
      const events = state.detail.events || [];
      if (!events.length) {
        document.getElementById('tab-events').innerHTML = '<div class="empty-box">No hay eventos registrados aún.</div>';
      } else {
        document.getElementById('tab-events').innerHTML = '<div class="timeline">' + events.slice().reverse().map((ev) => {
          const isErr = ev.type === 'PHASE_ERROR' || ev.type === 'PROJECT_FAILED';
          const isSuccess = ev.type === 'PROJECT_DELIVERED' || ev.type === 'PROJECT_COMPLETED';
          let dotClass = 'info';
          if (isErr) dotClass = 'err';
          else if (isSuccess) dotClass = 'ok';
          const dateStr = ev.at ? escapeHtml(ev.at) : '';
          const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
          return '<div class="timeline-item">' +
            '<div class="timeline-dot ' + dotClass + '"></div>' +
            '<div class="timeline-header">' +
              '<span class="timeline-type">' + escapeHtml(ev.type) + '</span>' +
              '<span class="timeline-time">' + dateStr + '</span>' +
            '</div>' +
            (hasPayload ? '<pre style="margin:4px 0 0 0;">' + escapeHtml(JSON.stringify(ev.payload, null, 2)) + '</pre>' : '') +
          '</div>';
        }).join('') + '</div>';
      }

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
        await loadProjects();
      } catch (err) {
        setNote('Error en ' + action + ': ' + err.message);
      }
    }

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
        if len(parts) == 2 and parts[1] in {"approve", "retry"}:
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
        self._drain_body()
        ctrl = self.server.controller
        pid, action = self._parse_project_route()

        if pid is None or action not in {"approve", "retry"}:
            self._send_json(404, {"error": "Endpoint not found"})
            return

        try:
            if action == "approve":
                result = ctrl.approve_project(pid)
            else:
                result = ctrl.retry_project(pid)
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
    secrets_to_redact: Sequence[str] = (),
) -> tuple[DashboardHTTPServer, threading.Thread, str]:
    """Create and start DashboardHTTPServer on 127.0.0.1:<port> in a daemon thread."""
    controller = DashboardController(
        orchestrator=orchestrator,
        build_worker=build_worker,
        template_scaffold=template_scaffold,
        workspace_manager=workspace_manager,
        bot_service=bot_service,
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
