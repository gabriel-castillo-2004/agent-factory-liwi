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
  <title>Agent Factory — Dashboard Local</title>
  <style>
    :root {
      --bg: #0f172a;
      --surface: #1e293b;
      --surface-alt: #0b1324;
      --border: #334155;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0ea5e9;
      --ok: #22c55e;
      --warn: #f59e0b;
      --err: #ef4444;
      --err-bg: rgba(239, 68, 68, 0.14);
      --ok-bg: rgba(34, 197, 94, 0.14);
      --warn-bg: rgba(245, 158, 11, 0.14);
      --info-bg: rgba(56, 189, 248, 0.14);
    }
    html[data-theme="light"] {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-alt: #f1f5f9;
      --border: #cbd5e1;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0284c7;
      --accent-hover: #0369a1;
      --ok: #16a34a;
      --warn: #d97706;
      --err: #dc2626;
      --err-bg: rgba(220, 38, 38, 0.10);
      --ok-bg: rgba(22, 163, 74, 0.10);
      --warn-bg: rgba(217, 119, 6, 0.10);
      --info-bg: rgba(2, 132, 199, 0.10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
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
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 1.1rem; }
    .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .btn {
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.875rem;
      font-weight: 500;
    }
    .btn:hover { border-color: var(--accent); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-primary { background: var(--accent); color: #082f49; border-color: var(--accent); font-weight: 600; }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-warn { background: var(--warn-bg); color: var(--warn); border-color: var(--warn); }
    .btn-danger { background: var(--err-bg); color: var(--err); border-color: var(--err); }
    .layout {
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      gap: 16px;
      padding: 16px 20px;
      max-width: 1440px;
      margin: 0 auto;
    }
    @media (max-width: 900px) {
      .layout { grid-template-columns: 1fr; }
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
    }
    .project-card {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 10px;
      cursor: pointer;
      background: var(--surface-alt);
      transition: border-color 0.15s;
    }
    .project-card:hover, .project-card.selected { border-color: var(--accent); }
    .card-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .badge {
      font-size: 0.75rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid transparent;
    }
    .badge-ok { background: var(--ok-bg); color: var(--ok); border-color: var(--ok); }
    .badge-err { background: var(--err-bg); color: var(--err); border-color: var(--err); }
    .badge-warn { background: var(--warn-bg); color: var(--warn); border-color: var(--warn); }
    .badge-info { background: var(--info-bg); color: var(--accent); border-color: var(--accent); }
    .progress-track {
      width: 100%;
      height: 8px;
      background: var(--bg);
      border-radius: 999px;
      overflow: hidden;
      margin: 8px 0;
    }
    .progress-fill { height: 100%; background: var(--accent); transition: width 0.25s; }
    .muted { color: var(--muted); font-size: 0.85rem; }
    .error-box {
      margin-top: 6px;
      padding: 8px;
      border-radius: 6px;
      background: var(--err-bg);
      color: var(--err);
      font-size: 0.82rem;
      word-break: break-word;
    }
    .actions-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 12px; }
    .tab-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
    }
    .tab-btn.active { background: var(--info-bg); color: var(--accent); border-color: var(--accent); }
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
    .event-item {
      border-left: 3px solid var(--accent);
      padding: 8px 12px;
      margin-bottom: 10px;
      background: var(--surface-alt);
      border-radius: 0 6px 6px 0;
    }
    .event-item.is-error { border-left-color: var(--err); }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .toast { font-size: 0.85rem; color: var(--accent); }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>Agent Factory</span>
      <span class="badge badge-info">127.0.0.1</span>
      <span id="status-note" class="toast"></span>
    </div>
    <div class="controls">
      <label class="muted"><input id="auto-refresh" type="checkbox" checked> Auto-refresco (5s)</label>
      <button id="refresh-btn" class="btn" type="button">Actualizar</button>
      <button id="theme-btn" class="btn" type="button">Tema claro/oscuro</button>
    </div>
  </header>

  <main class="layout">
    <section class="panel">
      <h2 style="margin-top:0">Proyectos</h2>
      <div id="project-list" class="muted">Cargando proyectos...</div>
    </section>

    <section class="panel" id="detail-panel">
      <div id="detail-empty" class="muted">Selecciona un proyecto para inspeccionar Spec, Plan y Eventos.</div>
      <div id="detail-content" style="display:none">
        <div class="card-top">
          <div>
            <h2 id="detail-title" style="margin:0"></h2>
            <div id="detail-sub" class="muted"></div>
          </div>
          <span id="detail-badge" class="badge"></span>
        </div>
        <div class="progress-track"><div id="detail-progress" class="progress-fill" style="width:0%"></div></div>
        <div id="detail-links" class="muted" style="margin-bottom:8px"></div>
        <div id="detail-error" class="error-box" style="display:none"></div>
        <div id="detail-actions" class="actions-row"></div>
        <div class="tabs" style="margin-top:14px">
          <button class="tab-btn active" data-tab="spec" type="button">Spec</button>
          <button class="tab-btn" data-tab="plan" type="button">Plan</button>
          <button class="tab-btn" data-tab="events" type="button">Eventos</button>
        </div>
        <div id="tab-spec"></div>
        <div id="tab-plan" style="display:none"></div>
        <div id="tab-events" style="display:none"></div>
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

    function badgeClass(st) {
      if (st === 'DELIVERED') return 'badge-ok';
      if (st === 'FAILED' || st === 'ERROR') return 'badge-err';
      if (st === 'WAITING_APPROVAL') return 'badge-warn';
      return 'badge-info';
    }

    function setNote(msg) {
      document.getElementById('status-note').textContent = msg || '';
    }

    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
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
        container.innerHTML = '<div class="muted">No hay proyectos registrados todavía.</div>';
        return;
      }
      container.innerHTML = state.projects.map((p) => {
        const selected = p.project_id === state.selectedId ? 'selected' : '';
        const repoUrl = p.links && p.links.repo ? escapeHtml(p.links.repo) : '';
        const repoLink = repoUrl ? '<div><a href="' + repoUrl + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">GitHub Repo</a></div>' : '';
        const errHtml = p.last_error ? '<div class="error-box">' + escapeHtml(p.last_error) + '</div>' : '';
        const busyTag = p.busy ? ' <span class="muted">(en curso)</span>' : '';
        return '<div class="project-card ' + selected + '" data-id="' + escapeHtml(p.project_id) + '">' +
          '<div class="card-top"><strong>' + escapeHtml(p.project_id) + busyTag + '</strong>' +
          '<span class="badge ' + badgeClass(p.state) + '">' + escapeHtml(p.state) + ' (' + escapeHtml(p.progress_pct) + '%)</span></div>' +
          '<div class="muted">' + escapeHtml(p.objective || p.name || '') + '</div>' +
          '<div class="progress-track"><div class="progress-fill" style="width:' + Number(p.progress_pct || 0) + '%"></div></div>' +
          repoLink + errHtml +
          '<div class="actions-row">' +
            (p.state === 'WAITING_APPROVAL' ? '<button class="btn btn-primary" data-act="approve" data-id="' + escapeHtml(p.project_id) + '">Aprobar y Construir</button>' : '') +
            (p.state === 'FAILED' ? '<button class="btn btn-warn" data-act="retry" data-id="' + escapeHtml(p.project_id) + '">Reintentar</button>' : '') +
            '<button class="btn btn-danger" data-act="delete" data-id="' + escapeHtml(p.project_id) + '">Eliminar</button>' +
          '</div></div>';
      }).join('');

      container.querySelectorAll('.project-card').forEach((card) => {
        card.addEventListener('click', () => {
          state.selectedId = card.dataset.id;
          renderProjectList();
          loadDetail(state.selectedId);
        });
      });
      container.querySelectorAll('button[data-act]').forEach((btn) => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          handleAction(btn.dataset.act, btn.dataset.id);
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
      document.getElementById('detail-title').textContent = st.project_id + (st.name ? ' — ' + st.name : '');
      document.getElementById('detail-sub').textContent = st.objective || '';
      const badge = document.getElementById('detail-badge');
      badge.textContent = st.state + ' · ' + st.progress_pct + '%';
      badge.className = 'badge ' + badgeClass(st.state);
      document.getElementById('detail-progress').style.width = Number(st.progress_pct || 0) + '%';

      const links = [];
      if (st.links && st.links.repo) {
        links.push('<a href="' + escapeHtml(st.links.repo) + '" target="_blank" rel="noopener noreferrer">Repo GitHub</a>');
      }
      if (st.links && st.links.deploy) {
        links.push('<a href="' + escapeHtml(st.links.deploy) + '" target="_blank" rel="noopener noreferrer">Deploy Live</a>');
      }
      document.getElementById('detail-links').innerHTML = links.join(' · ');

      const errBox = document.getElementById('detail-error');
      if (st.last_error) {
        errBox.style.display = 'block';
        errBox.textContent = 'Último error: ' + st.last_error;
      } else {
        errBox.style.display = 'none';
      }

      const actions = document.getElementById('detail-actions');
      actions.innerHTML =
        (st.state === 'WAITING_APPROVAL' ? '<button class="btn btn-primary" data-dact="approve">Aprobar y Construir</button>' : '') +
        (st.state === 'FAILED' ? '<button class="btn btn-warn" data-dact="retry">Reintentar</button>' : '') +
        '<button class="btn btn-danger" data-dact="delete">Eliminar</button>';
      actions.querySelectorAll('button[data-dact]').forEach((btn) => {
        btn.addEventListener('click', () => handleAction(btn.dataset.dact, st.project_id));
      });

      const spec = state.detail.spec || {};
      const renderList = (arr) => Array.isArray(arr) && arr.length
        ? '<ul>' + arr.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul>'
        : '<span class="muted">Sin datos</span>';
      document.getElementById('tab-spec').innerHTML =
        '<p><strong>Objetivo:</strong> ' + escapeHtml(spec.objective || '') + '</p>' +
        '<p><strong>Usuarios:</strong></p>' + renderList(spec.users) +
        '<p><strong>Funcionalidades:</strong></p>' + renderList(spec.features) +
        '<p><strong>Reglas de negocio:</strong></p>' + renderList(spec.business_rules) +
        '<p><strong>Casos borde:</strong></p>' + renderList(spec.edge_cases) +
        '<p><strong>Restricciones:</strong></p>' + renderList(spec.constraints) +
        '<p><strong>Criterios de éxito:</strong></p>' + renderList(spec.success_criteria) +
        '<p><strong>Supuestos:</strong></p>' + renderList(spec.assumptions);

      const plan = state.detail.plan;
      if (!plan) {
        document.getElementById('tab-plan').innerHTML = '<div class="muted">Aún no se ha generado un Plan.</div>';
      } else {
        const filesHtml = (plan.files || []).map((f) =>
          '<div class="event-item"><strong>' + escapeHtml(f.path) + '</strong><div class="muted">' + escapeHtml(f.purpose) + '</div><pre>' + escapeHtml(f.contract) + '</pre></div>'
        ).join('');
        const testsHtml = (plan.acceptance_tests || []).map((t) =>
          '<li><strong>[' + escapeHtml(t.id) + '] (' + escapeHtml(t.type) + ')</strong>: ' + escapeHtml(t.description) + '</li>'
        ).join('');
        document.getElementById('tab-plan').innerHTML =
          '<p><strong>Resumen:</strong> ' + escapeHtml(plan.summary) + '</p>' +
          '<p><strong>Justificación:</strong> ' + escapeHtml(plan.rationale) + '</p>' +
          '<p><strong>Plantilla:</strong> <code>' + escapeHtml(plan.template_id) + '</code> · <strong>Target:</strong> <code>' + escapeHtml(plan.deployment_target) + '</code></p>' +
          '<h3>Archivos (' + (plan.files || []).length + ')</h3>' + filesHtml +
          '<h3>Pruebas de aceptación (' + (plan.acceptance_tests || []).length + ')</h3><ul>' + testsHtml + '</ul>';
      }

      const events = state.detail.events || [];
      document.getElementById('tab-events').innerHTML = events.length
        ? events.slice().reverse().map((ev) => {
            const isErr = ev.type === 'PHASE_ERROR' || ev.type === 'PROJECT_FAILED';
            return '<div class="event-item ' + (isErr ? 'is-error' : '') + '">' +
              '<div class="card-top"><strong>' + escapeHtml(ev.type) + '</strong><span class="muted">' + escapeHtml(ev.at) + '</span></div>' +
              '<pre>' + escapeHtml(JSON.stringify(ev.payload, null, 2)) + '</pre></div>';
          }).join('')
        : '<div class="muted">Sin eventos.</div>';

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
          if (!window.confirm('¿Eliminar permanentemente el proyecto "' + pid + '" y su workspace local?')) {
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

    document.getElementById('refresh-btn').addEventListener('click', loadProjects);
    setInterval(() => {
      if (document.getElementById('auto-refresh').checked) {
        loadProjects();
      }
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
