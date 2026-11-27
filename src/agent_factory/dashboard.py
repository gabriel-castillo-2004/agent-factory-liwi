"""Local loopback HTTP dashboard server and JSON API for monitoring and controlling projects (INV-07, INV-19, INV-22)."""

from __future__ import annotations

import base64
import binascii
import email.parser
import email.policy
import http.server
import json
import logging
import math
import os
import threading
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory import error_triage, idea_engine, telemetry, visual_qa_agent
from agent_factory.adapters.github_vcs_adapter import VcsError
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import ApprovalEvent, ProjectStatus, validate_project_id
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import (
    BACKGROUND_OP_ORPHAN_SECONDS,
    ApprovalError,
    Orchestrator,
    _parse_utc_iso_timestamp,
    synthesize_project_title,
    validate_chat_entry_id,
)
from agent_factory.ports.code_agent import CodeAgentError
from agent_factory.ports.llm import LLMError
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.runtime_runner import LocalAppError, LocalAppRunner
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets
from agent_factory.self_improver import FactorySelfImprover
from agent_factory.staging_manager import StagingManager
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
        self_improver: FactorySelfImprover | None = None,
        staging_manager: StagingManager | None = None,
        autonomous_engine: idea_engine.AutonomousEngine | None = None,
        visual_qa_runner: Callable[..., Mapping[str, Any]] | None = None,
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
        self._active_ops: dict[str, dict[str, Any]] = {}
        self._project_threads: dict[str, threading.Thread] = {}
        self._op_seq = 0
        self._threads: list[threading.Thread] = []
        self._self_improver: FactorySelfImprover | None = self_improver
        self._staging_manager: StagingManager | None = staging_manager
        self._autonomous_engine: idea_engine.AutonomousEngine | None = autonomous_engine
        self._visual_qa_runner: Callable[..., Mapping[str, Any]] | None = visual_qa_runner
        self._legacy_direct_self_improver: bool = (
            isinstance(self_improver, FactorySelfImprover) and staging_manager is None
        )

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

    def _has_live_thread(self, project_id: str) -> bool:
        """Return True if a tracked background thread for project_id is currently alive."""
        with self._lock:
            t = self._project_threads.get(project_id)
            return bool(t is not None and t.is_alive())

    def _reconcile_running_state(
        self,
        project_id: str,
        *,
        force: bool = False,
        reason: str = "",
    ) -> tuple[bool, dict[str, Any] | None, bool]:
        """Reconcile in-memory locks and persistent background ops, auto-healing orphans."""
        validate_project_id(project_id)
        has_live_thread = self._has_live_thread(project_id)
        healed_bg, open_op = self.orchestrator.heal_orphaned_background_op(
            project_id,
            has_live_thread=has_live_thread,
            force=force,
            reason=reason,
            chats_dir=self.chats_dir,
            extra_secrets=self.secrets_to_redact,
        )

        healed_mem = False
        active_mem_op: dict[str, Any] | None = None
        now_dt = _parse_utc_iso_timestamp(self.orchestrator._now().isoformat())
        with self._lock:
            in_active = project_id in self._active_projects
            mem_op = self._active_ops.get(project_id)
            t = self._project_threads.get(project_id)
            thread_alive = bool(t is not None and t.is_alive())
            thread_dead = t is not None and not t.is_alive()

            if healed_bg or force:
                if in_active or mem_op is not None or t is not None:
                    self._active_projects.discard(project_id)
                    self._active_ops.pop(project_id, None)
                    self._project_threads.pop(project_id, None)
                    healed_mem = True
            elif in_active and mem_op is not None:
                started_dt = mem_op.get("started_at_dt")
                elapsed_secs = (
                    (now_dt - started_dt).total_seconds()
                    if (now_dt is not None and started_dt is not None)
                    else 0.0
                )
                op_boot = str(mem_op.get("server_boot_id") or "").strip()
                has_thread_flag = bool(mem_op.get("has_thread"))
                stale_boot = (op_boot != self.orchestrator._server_boot_id) and not thread_alive
                died_unexpectedly = has_thread_flag and thread_dead
                expired = elapsed_secs > BACKGROUND_OP_ORPHAN_SECONDS
                if stale_boot or died_unexpectedly or expired:
                    self._active_projects.discard(project_id)
                    self._active_ops.pop(project_id, None)
                    self._project_threads.pop(project_id, None)
                    healed_mem = True
                else:
                    active_mem_op = dict(mem_op)
            elif in_active:
                if thread_dead:
                    self._active_projects.discard(project_id)
                    self._project_threads.pop(project_id, None)
                    healed_mem = True
                else:
                    active_mem_op = {}

            still_in_active = project_id in self._active_projects
            no_active = not self._active_projects

        if (healed_bg or healed_mem or force) and no_active and self.model_resolver is not None:
            clear_fn = getattr(self.model_resolver, "clear_active_activity", None)
            if callable(clear_fn):
                clear_fn()

        eff_op = active_mem_op or open_op
        running = bool(still_in_active or (open_op is not None and not force))
        return running, (eff_op if eff_op else None), bool(healed_bg or healed_mem)

    def is_busy(self, project_id: str) -> bool:
        """Return True if a background task is currently executing for project_id."""
        return self.is_running(project_id)

    def is_running(self, project_id: str) -> bool:
        """Return True if an active task thread is currently running for project_id."""
        running, _, _ = self._reconcile_running_state(project_id)
        return running

    def mark_running(
        self,
        project_id: str,
        *,
        op_id: str | None = None,
        op_type: str = "background",
        activity_summary: str = "",
        stage: str | None = None,
        agents_count: int | None = None,
    ) -> None:
        """Register project_id as having an active task running in memory."""
        now_dt = _parse_utc_iso_timestamp(self.orchestrator._now().isoformat())
        with self._lock:
            self._op_seq += 1
            eff_op_id = str(op_id or f"mem-{self._op_seq}").strip()
            self._active_projects.add(project_id)
            self._active_ops[project_id] = {
                "op_id": eff_op_id,
                "op_type": str(op_type or "background").strip(),
                "started_at": now_dt.isoformat() if now_dt is not None else None,
                "started_at_dt": now_dt,
                "server_boot_id": self.orchestrator._server_boot_id,
                "has_thread": False,
                "activity_summary": str(activity_summary or "").strip(),
                "stage": str(stage or "").strip(),
                "agents_count": agents_count,
            }

    def mark_stopped(self, project_id: str) -> None:
        """Unregister project_id from active running tasks."""
        with self._lock:
            self._active_projects.discard(project_id)
            self._active_ops.pop(project_id, None)
            self._project_threads.pop(project_id, None)
            no_active = not self._active_projects
        if no_active and self.model_resolver is not None:
            clear_fn = getattr(self.model_resolver, "clear_active_activity", None)
            if callable(clear_fn):
                clear_fn()

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

    def _start_background(
        self,
        project_id: str,
        target: Callable[..., None],
        *,
        op_id: str | None = None,
        op_type: str = "background",
        activity_summary: str = "",
        stage: str | None = None,
        agents_count: int | None = None,
        record_event: bool = False,
        accepts_op_id: bool = False,
    ) -> bool:
        """Run target in a daemon thread if project_id does not already have a running job."""
        with self._lock:
            self._reconcile_running_state(project_id)
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            if record_event:
                started_ev = self.orchestrator.record_background_op_started(
                    project_id,
                    op_type=op_type,
                    op_id=op_id,
                    activity_summary=activity_summary,
                    stage=stage,
                    agents_count=agents_count,
                    extra_secrets=self.secrets_to_redact,
                )
                eff_op_id = str((started_ev.payload or {}).get("op_id") or started_ev.id)
                started_iso = str(
                    (started_ev.payload or {}).get("started_at") or started_ev.at.isoformat()
                )
                started_dt = _parse_utc_iso_timestamp(started_iso)
            else:
                self._op_seq += 1
                eff_op_id = str(op_id or f"mem-{self._op_seq}").strip()
                started_dt = _parse_utc_iso_timestamp(self.orchestrator._now().isoformat())
                started_iso = started_dt.isoformat() if started_dt is not None else None

            self._active_projects.add(project_id)
            self._active_ops[project_id] = {
                "op_id": eff_op_id,
                "op_type": str(op_type or "background").strip(),
                "started_at": started_iso,
                "started_at_dt": started_dt,
                "server_boot_id": self.orchestrator._server_boot_id,
                "has_thread": True,
                "activity_summary": str(activity_summary or "").strip(),
                "stage": str(stage or "").strip(),
                "agents_count": agents_count,
            }

            def _runner() -> None:
                op_status = "completed"
                op_reason = ""
                try:
                    if accepts_op_id:
                        target(eff_op_id)
                    else:
                        target()
                except Exception as exc:
                    op_status = "failed"
                    op_reason = str(exc) or exc.__class__.__name__
                    raise
                finally:
                    try:
                        if record_event:
                            cur_events = self.orchestrator.store.get_events(project_id)
                            open_op = self.orchestrator.get_open_background_op(
                                project_id, events=cur_events
                            )
                            if open_op is not None and str(open_op.get("op_id") or "") == eff_op_id:
                                now_fin = _parse_utc_iso_timestamp(
                                    self.orchestrator._now().isoformat()
                                )
                                dur = (
                                    (now_fin - started_dt).total_seconds()
                                    if (now_fin is not None and started_dt is not None)
                                    else None
                                )
                                self.orchestrator.record_background_op_finished(
                                    project_id,
                                    op_id=eff_op_id,
                                    op_type=op_type,
                                    status=op_status,
                                    reason=op_reason,
                                    duration_seconds=dur,
                                    extra_secrets=self.secrets_to_redact,
                                )
                    finally:
                        with self._lock:
                            cur_op = self._active_ops.get(project_id)
                            if cur_op is not None and str(cur_op.get("op_id") or "") == eff_op_id:
                                self._active_projects.discard(project_id)
                                self._active_ops.pop(project_id, None)
                                self._project_threads.pop(project_id, None)
                            elif cur_op is None:
                                self._project_threads.pop(project_id, None)
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
            self._project_threads[project_id] = t
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

    def _extract_last_recovery(
        self,
        status: ProjectStatus,
        events: Sequence[Event],
    ) -> dict[str, Any] | None:
        """Return latest secret-redacted Doctor Agent recovery/diagnostic record for the active attempt."""
        _ = status
        for ev in reversed(events):
            if ev.type == EventType.PROJECT_RETRIED:
                break
            pl = ev.payload or {}
            if ev.type == EventType.DOCTOR_RECOVERY and isinstance(pl, Mapping):
                return _scrub_value(dict(pl), self.secrets_to_redact)
            if ev.type in (EventType.PHASE_ERROR, EventType.PROJECT_FAILED):
                rec = pl.get("recovery")
                if isinstance(rec, Mapping):
                    return _scrub_value(dict(rec), self.secrets_to_redact)
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
                    eff_status = self.orchestrator.get_status(
                        project_id,
                        notify_publisher=False,
                        notify_notifier=False,
                    )
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
        try:
            self._sync_external_workspace(project_id)
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
        reconciled: tuple[bool, dict[str, Any] | None, bool] | None = None,
    ) -> dict[str, Any]:
        if reconciled is None:
            running, active_op, healed = self._reconcile_running_state(project_id)
            if healed:
                events = self.orchestrator.store.get_events(project_id)
                status = self.orchestrator.get_status(
                    project_id,
                    notify_publisher=False,
                    notify_notifier=False,
                )
        else:
            running, active_op, _ = reconciled
        last_error = self._extract_last_error(status, events)
        last_recovery = self._extract_last_recovery(status, events)
        last_event_at = events[-1].at.isoformat() if events else None
        live_info = self._get_live_status_info(running)
        if running and active_op:
            op_summary = str(active_op.get("activity_summary") or "").strip()
            op_agents = active_op.get("agents_count")
            cur_act = str(live_info.get("activity_summary") or "").strip()
            if op_summary and (
                not cur_act or "Analizando tu idea y preparando preguntas" in cur_act
            ):
                live_info["activity_summary"] = op_summary
            if (not live_info.get("agents_count")) and isinstance(op_agents, int) and op_agents > 0:
                live_info["agents_count"] = op_agents
        if running and status.state == ProjectState.DELIVERED:
            cur_act = str(live_info.get("activity_summary") or "").strip()
            if not cur_act or "Analizando tu idea y preparando preguntas" in cur_act:
                live_info["activity_summary"] = "Refinando calidad con debate de 2 agentes..."
            if not live_info.get("agents_count"):
                live_info["agents_count"] = 2
        raw = status.model_dump(mode="json")
        op_started_at: str | None = None
        if running:
            raw_op_started = str((active_op or {}).get("started_at") or "").strip()
            op_started_at = (
                raw_op_started
                or (str(raw.get("step_started_at") or "").strip() if status.state != ProjectState.DELIVERED else "")
                or last_event_at
            )
            if op_started_at and (
                not raw.get("step_started_at") or status.state == ProjectState.DELIVERED
            ):
                raw["step_started_at"] = op_started_at
        raw["op_started_at"] = op_started_at
        raw["last_error"] = last_error
        raw["last_recovery"] = last_recovery
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
                status = self.orchestrator.get_status(
                    pid,
                    notify_publisher=False,
                    notify_notifier=False,
                )
                has_plan = self.orchestrator.get_plan(pid) is not None
                summaries.append(
                    self._serialize_summary(pid, status, events, has_plan=has_plan)
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    running = self.is_running(pid)
                except Exception:  # noqa: BLE001
                    with self._lock:
                        running = pid in self._active_projects
                live_info = self._get_live_status_info(running)
                clean_err = redact_secrets(
                    str(exc) or exc.__class__.__name__,
                    extra_secrets=self.secrets_to_redact,
                )
                summaries.append(
                    {
                        "project_id": pid,
                        "name": pid,
                        "objective": "",
                        "state": "ERROR",
                        "phase": "ERROR",
                        "progress_pct": 0,
                        "last_error": clean_err,
                        "failure_reason": clean_err,
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

        reconciled = self._reconcile_running_state(project_id)
        if reconciled[2]:
            events = self.orchestrator.store.get_events(project_id)
        status = self.orchestrator.get_status(project_id)
        self._sync_external_workspace(project_id, status)
        spec = self.orchestrator.get_spec(project_id)
        plan = self.orchestrator.get_plan(project_id)
        if hasattr(self.orchestrator, "get_project_detail"):
            orch_detail = self.orchestrator.get_project_detail(project_id)
            raw_questions = orch_detail.get("questions", [])
        else:
            raw_questions = (
                Orchestrator._extract_active_interview_questions(events)
                if status.state == ProjectState.INTERVIEW
                else []
            )
        clean_questions = (
            [q.strip() for q in raw_questions if isinstance(q, str) and q.strip()]
            if isinstance(raw_questions, Sequence)
            and not isinstance(raw_questions, (str, bytes))
            and status.state == ProjectState.INTERVIEW
            else []
        )

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
            project_id,
            status,
            events,
            has_plan=(plan is not None),
            reconciled=reconciled,
        )
        return {
            "project_id": project_id,
            "status": summary,
            "questions": _scrub_value(clean_questions, self.secrets_to_redact),
            "is_running": summary["is_running"],
            "op_started_at": summary.get("op_started_at"),
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
            self._reconcile_running_state(project_id)
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
        """Return delivered version checkpoints (newest first) for Rollback and Historial panels."""
        self._require_project_events(project_id)
        checkpoints = self.orchestrator.list_project_checkpoints(
            project_id,
            chats_dir=self.chats_dir,
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
            self._reconcile_running_state(project_id)
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
            project_id, chats_dir=self.chats_dir, extra_secrets=self.secrets_to_redact
        )
        return {
            "ok": True,
            "project_id": project_id,
            "checkpoint": cp_info,
            "checkpoints": checkpoints,
            "status": summary,
        }

    def evaluate_project_quality(self, project_id: str) -> dict[str, Any]:
        """Run read-only ParentAuditor evaluation, update current version scorecard, and persist report."""
        self._require_project_events(project_id)
        with self._lock:
            self._reconcile_running_state(project_id)
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            self._active_projects.add(project_id)
            ws_mgr = self._sync_external_workspace(project_id)
            auditor = getattr(self.build_worker, "quality_auditor", None)
        try:
            try:
                res = self.orchestrator.evaluate_project_quality(
                    project_id,
                    auditor=auditor,
                    workspace_manager=ws_mgr,
                    chats_dir=self.chats_dir,
                    extra_secrets=self.secrets_to_redact,
                )
            except IllegalTransition as exc:
                clean_msg = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
                raise ProjectConflictError(clean_msg) from exc
        finally:
            with self._lock:
                self._active_projects.discard(project_id)

        report_obj = res.get("report")
        report_payload = (
            report_obj.model_dump(mode="json")
            if hasattr(report_obj, "model_dump")
            else (report_obj if isinstance(report_obj, dict) else {})
        )
        updated = self.orchestrator.get_status(project_id)
        summary = self._serialize_summary(
            project_id,
            updated,
            self.orchestrator.store.get_events(project_id),
            has_plan=(self.orchestrator.get_plan(project_id) is not None),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "version_label": res.get("version_label", ""),
            "commit_sha": res.get("commit_sha", ""),
            "scorecard": _scrub_value(res.get("scorecard") or {}, self.secrets_to_redact),
            "quality_scorecard": _scrub_value(
                res.get("quality_scorecard") or res.get("scorecard") or {},
                self.secrets_to_redact,
            ),
            "report": _scrub_value(report_payload, self.secrets_to_redact),
            "chat_entry": _scrub_value(res.get("chat_entry") or {}, self.secrets_to_redact),
            "checkpoints": _scrub_value(res.get("checkpoints") or [], self.secrets_to_redact),
            "status": summary,
        }

    def improve_project_quality(
        self,
        project_id: str,
        *,
        force_polish: bool = False,
    ) -> dict[str, Any]:
        """Launch bounded 2-agent adversarial debate refinement until Producción (or Excelencia if force_polish=True)."""
        if not isinstance(force_polish, bool):
            raise TypeError("El campo 'force_polish' debe ser booleano.")
        self._require_project_events(project_id)
        with self._lock:
            self._reconcile_running_state(project_id)
            if project_id in self._active_projects:
                raise ProjectConflictError(
                    f"Project '{project_id}' already has an active background operation"
                )
            current_status = self.orchestrator.get_status(project_id)
            if current_status.state != ProjectState.DELIVERED:
                raise ProjectConflictError(
                    str(IllegalTransition(current_status.state, ProjectState.CHANGE_REQUEST))
                )
            ws_mgr = self._sync_external_workspace(project_id, current_status)
            ws_dir = self.orchestrator._resolve_project_workspace_dir(
                project_id, status=current_status, workspace_manager=ws_mgr
            )
            if ws_dir is None or not ws_dir.is_dir():
                raise ValueError(f"El workspace del proyecto '{project_id}' no existe.")
            eff_worker = (
                self.build_worker
                if self.build_worker is not None
                else getattr(self.orchestrator, "build_worker", None)
            )
            if eff_worker is None:
                eval_res = self.orchestrator.evaluate_project_quality(
                    project_id,
                    workspace_manager=ws_mgr,
                    chats_dir=self.chats_dir,
                    extra_secrets=self.secrets_to_redact,
                )
                sc = eval_res.get("scorecard") or {}
                if not force_polish and sc.get("passed"):
                    updated = self.orchestrator.get_status(project_id)
                    return {
                        "ok": True,
                        "project_id": project_id,
                        "already_passed": True,
                        "improved": False,
                        "background_started": False,
                        "scorecard": _scrub_value(sc, self.secrets_to_redact),
                        "checkpoints": _scrub_value(
                            eval_res.get("checkpoints") or [], self.secrets_to_redact
                        ),
                        "status": self._serialize_summary(
                            project_id,
                            updated,
                            self.orchestrator.store.get_events(project_id),
                            has_plan=(self.orchestrator.get_plan(project_id) is not None),
                        ),
                    }
                raise ProjectConflictError(
                    "No hay un BuildWorker configurado para refinar automáticamente el proyecto."
                )

            self.local_runner.stop(project_id)

            def _run_improve(bg_op_id: str | None = None) -> None:
                try:
                    res = self.orchestrator.improve_project_quality(
                        project_id,
                        build_worker=eff_worker,
                        chats_dir=self.chats_dir,
                        force_polish=force_polish,
                        extra_secrets=self.secrets_to_redact,
                        _bg_op_id=bg_op_id,
                    )
                    sc = res.get("scorecard") or {}
                    tier_lbl = sc.get("tier_label") or "🥇 Producción"
                    pct = sc.get("global_score")
                    ver_lbl = res.get("version_label") or ""
                    self._notify_chat_best_effort(
                        project_id,
                        f"Calidad evaluada/refinada para {project_id} ({ver_lbl}): {tier_lbl} ({pct}%).",
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
                            agent="dashboard_quality_improve",
                            extra_secrets=self.secrets_to_redact,
                        )
                    self._notify_chat_best_effort(
                        project_id,
                        f"Error mejorando calidad de {project_id}: "
                        f"{redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)}",
                    )

            background_started = self._start_background(
                project_id,
                _run_improve,
                op_type="quality_improve",
                activity_summary="Refinando calidad con debate de 2 agentes...",
                stage="DELIVERED",
                agents_count=2,
                record_event=True,
                accepts_op_id=True,
            )

        summary = self._serialize_summary(
            project_id,
            self.orchestrator.get_status(project_id),
            self.orchestrator.store.get_events(project_id),
            has_plan=(self.orchestrator.get_plan(project_id) is not None),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "background_started": background_started,
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
        except (LLMError, CommandTimeoutError, TimeoutError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            raise ProjectConflictError(clean_err) from None
        return _scrub_value(result, self.secrets_to_redact)

    def delete_project_chat_entry(
        self,
        project_id: str,
        entry_id: str,
    ) -> dict[str, Any]:
        """Delete a single persisted audit, document, or chat entry by entry_id (INV-22)."""
        validate_project_id(project_id)
        clean_entry_id = validate_chat_entry_id(entry_id)
        if not self.orchestrator.store.get_events(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        deleted = self.orchestrator.delete_project_chat_entry(
            project_id,
            clean_entry_id,
            chats_dir=self.chats_dir,
        )
        if not deleted:
            raise ProjectNotFoundError(
                f"Chat entry '{clean_entry_id}' not found for project '{project_id}'"
            )
        remaining = self.orchestrator.get_project_chat_history(
            project_id,
            chats_dir=self.chats_dir,
            extra_secrets=self.secrets_to_redact,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "deleted_id": clean_entry_id,
            "messages": _scrub_value(remaining, self.secrets_to_redact),
        }

    def retry_project_chat_entry(
        self,
        project_id: str,
        entry_id: str,
    ) -> dict[str, Any]:
        """Retry an existing audit, document, or chat entry updating it in place (INV-19, INV-22)."""
        validate_project_id(project_id)
        clean_entry_id = validate_chat_entry_id(entry_id)
        if not self.orchestrator.store.get_events(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        ws_mgr = self._sync_external_workspace(project_id)
        try:
            result = self.orchestrator.retry_project_chat_entry(
                project_id,
                clean_entry_id,
                chats_dir=self.chats_dir,
                workspace_manager=ws_mgr,
                extra_secrets=self.secrets_to_redact,
            )
        except LookupError as exc:
            raise ProjectNotFoundError(str(exc)) from None
        except (LLMError, CommandTimeoutError, TimeoutError, RuntimeError) as exc:
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
                try:
                    idea_engine.create_project_error_idea(
                        project_id,
                        clean_msg,
                        db_path=self._resolve_idea_db_path(),
                    )
                except Exception as idea_exc:  # noqa: BLE001
                    logger.debug("create_project_error_idea skipped for %s: %s", project_id, idea_exc)
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
            self._reconcile_running_state(project_id)
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

                background_started = self._start_background(
                    project_id,
                    _run_modify_build,
                    op_type="modify",
                    activity_summary="Aplicando cambios solicitados...",
                    stage="BUILDING",
                    agents_count=2,
                    record_event=True,
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

    def send_message(self, project_id: str, message: str) -> dict[str, Any]:
        """Submit an interview reply or plan adjustment request for project_id in background."""
        validate_project_id(project_id)
        if not self.orchestrator.store.get_events(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        clean_message = str(message or "").strip()
        if not clean_message:
            raise ValueError("El campo 'message' es obligatorio.")

        with self._lock:
            self._reconcile_running_state(project_id)
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
            self._reconcile_running_state(project_id)
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
            if updated.state in (ProjectState.DEBATE, ProjectState.MEDIATION) or (
                updated.state == ProjectState.INTERVIEW
                and self.orchestrator.get_spec(project_id).is_complete()
            ):
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

    def recover_project(self, project_id: str) -> dict[str, Any]:
        """Force-unlock any orphaned or stuck background operation, chat, or activity state."""
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        if not events:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        with self._lock:
            _, _, healed_op = self._reconcile_running_state(
                project_id,
                force=True,
                reason="Operación desbloqueada manualmente desde el panel.",
            )
            healed_chats = self.orchestrator.heal_orphaned_chat_entries(
                project_id,
                chats_dir=self.chats_dir,
                force=True,
            )
            self._active_projects.discard(project_id)
            self._active_ops.pop(project_id, None)
            self._project_threads.pop(project_id, None)
            no_active = not self._active_projects
        if no_active and self.model_resolver is not None:
            clear_fn = getattr(self.model_resolver, "clear_active_activity", None)
            if callable(clear_fn):
                clear_fn()

        refreshed_events = self.orchestrator.store.get_events(project_id)
        status = self.orchestrator.get_status(project_id)
        has_plan = self.orchestrator.get_plan(project_id) is not None
        summary = self._serialize_summary(
            project_id,
            status,
            refreshed_events,
            has_plan=has_plan,
            reconciled=(False, None, bool(healed_op or healed_chats)),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "unlocked": bool(healed_op or healed_chats),
            "recovered": True,
            "status": summary,
        }

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Delete project events from SQLite and remove workspaces/<project_id> if present."""
        validate_project_id(project_id)
        with self._lock:
            self._reconcile_running_state(project_id)
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

    @property
    def self_improver(self) -> FactorySelfImprover | None:
        """Return the currently configured self-improver instance without eager creation."""
        return self._self_improver

    @self_improver.setter
    def self_improver(self, value: FactorySelfImprover | None) -> None:
        with self._lock:
            self._self_improver = value

    def _effective_self_improver(self) -> FactorySelfImprover:
        with self._lock:
            if self._self_improver is not None:
                if (
                    hasattr(self._self_improver, "pre_exit_hook")
                    and self._self_improver.pre_exit_hook is None
                ):
                    self._self_improver.pre_exit_hook = lambda: self.local_runner.stop_all()
                return self._self_improver
            store = self.orchestrator.store
            sqlite_store = store if hasattr(store, "_conn") and hasattr(store, "_lock") else None
            eff_runner = (
                self.runner
                if self.runner is not None
                else getattr(self.build_worker, "runner", None)
            )
            eff_model = None
            if self.model_resolver is not None and hasattr(self.model_resolver, "resolve_model"):
                try:
                    eff_model = self.model_resolver.resolve_model("INTERVIEW")
                except (RuntimeError, ValueError, OSError):
                    eff_model = None
            self._self_improver = FactorySelfImprover(
                sqlite_store=sqlite_store,
                uploads_dir=self.chats_dir.parent / "factory_chat_uploads",
                runner=eff_runner,
                model=eff_model,
                secrets_to_redact=self.secrets_to_redact,
                pre_exit_hook=lambda: self.local_runner.stop_all(),
            )
            return self._self_improver

    def _get_staging_manager(self) -> StagingManager:
        """Lazily create and return the controller-scoped StagingManager without mutating Git."""
        with self._lock:
            if self._staging_manager is None:
                raw_root = getattr(self._self_improver, "repo_root", None)
                repo_root = (
                    Path(raw_root).resolve()
                    if raw_root is not None
                    else Path(__file__).resolve().parents[2]
                )
                self._staging_manager = StagingManager(
                    repo_root=repo_root,
                    executor=self._execute_staging_task,
                    on_restart=self._trigger_staging_restart,
                )
            return self._staging_manager

    def shutdown_staging(self, timeout: float = 5.0) -> None:
        """Cleanly stop the staging worker if a StagingManager has been initialized."""
        with self._lock:
            mgr = self._staging_manager
        if mgr is not None and callable(getattr(mgr, "shutdown", None)):
            mgr.shutdown(timeout=timeout)

    def _execute_staging_task(self, worktree_path: Path, task: dict[str, Any]) -> Any:
        """Execute a queued self-improvement inside .liwi-staging delegating dynamically to self.self_improver."""
        improver = self.self_improver
        if improver is None:
            improver = self._effective_self_improver()

        prompt = str(task.get("prompt") or "")
        title = str(task.get("title") or "Auto-mejora Liwi")
        meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        message_id = str(meta["message_id"]) if meta.get("message_id") else None

        is_real_unmodified = (
            type(improver) is FactorySelfImprover and "apply_proposal" not in improver.__dict__
        )
        if not is_real_unmodified:
            res = improver.apply_proposal(
                prompt,
                proposal_title=title,
                message_id=message_id,
                github_token="",
            )
        else:
            store = self.orchestrator.store
            sqlite_store = store if hasattr(store, "_conn") and hasattr(store, "_lock") else None
            staging_improver = FactorySelfImprover(
                sqlite_store=sqlite_store,
                repo_root=worktree_path,
                uploads_dir=improver.uploads_dir,
                runner=improver.runner,
                clock=improver.clock,
                agy_command=improver.agy_command,
                model=improver.model,
                secrets_to_redact=improver.secrets_to_redact,
                apply_timeout_seconds=improver.apply_timeout_seconds,
                verify_timeout_seconds=improver.verify_timeout_seconds,
                restart_fn=lambda _code: None,
                restart_delay_seconds=0.0,
                pre_exit_hook=None,
            )
            if getattr(staging_improver.memory_store, "_owns_conn", False):
                staging_improver.memory_store._conn.close()
            staging_improver.memory_store = improver.memory_store
            staging_improver._status = improver._status
            staging_improver._status_lock = improver._status_lock
            res = staging_improver.apply_proposal(
                prompt,
                proposal_title=title,
                message_id=message_id,
                github_token="",
            )

        if isinstance(res, Mapping) and not res.get("ok"):
            err = str(res.get("error") or "La auto-mejora falló en el worktree de staging.")
            raise RuntimeError(redact_secrets(err, extra_secrets=self.secrets_to_redact))
        return res

    def _trigger_staging_restart(self) -> None:
        """Trigger supervised process restart (exit 42) after real promotion, never calling os._exit under pytest."""
        in_pytest = "PYTEST_CURRENT_TEST" in os.environ
        improver = self.self_improver
        if improver is not None:
            restart_fn = getattr(improver, "restart_fn", None)
            if in_pytest and restart_fn is os._exit:
                return
            schedule_fn = getattr(improver, "_schedule_process_restart", None)
            if callable(schedule_fn):
                schedule_fn()
                return
        if in_pytest:
            return
        self._effective_self_improver()._schedule_process_restart()

    def _should_use_staging(self, use_staging: bool | None = None) -> bool:
        if use_staging is not None:
            return bool(use_staging)
        if self._staging_manager is not None:
            return True
        return not self._legacy_direct_self_improver

    def get_self_improve_staging_status(self) -> dict[str, Any]:
        """Return staging queue/worker status and pending commits without mutating Git or initializing lazily."""
        with self._lock:
            mgr = self._staging_manager
        if mgr is None:
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": False,
                "queue_size": 0,
                "last_error": None,
                "current_task": None,
                "initialized": False,
            }
        status = mgr.get_staging_status()
        if isinstance(status, dict):
            payload = dict(status)
            payload.setdefault("initialized", True)
            return _scrub_value(payload, self.secrets_to_redact)
        return {
            "pending_count": 0,
            "commits": [],
            "is_building": False,
            "queue_size": 0,
            "last_error": None,
            "current_task": None,
            "initialized": True,
        }

    def promote_self_improve_staging(
        self,
        *,
        trigger_restart: bool | None = None,
    ) -> dict[str, Any]:
        """Promote accumulated liwi-staging commits to main and restart only on real success outside pytest."""
        with self._lock:
            mgr = self._staging_manager
        if mgr is None:
            return {
                "ok": True,
                "promoted": False,
                "merged_commits": 0,
                "commits": [],
                "restart_scheduled": False,
                "message": "No hay mejoras pendientes en staging para promover.",
            }

        in_pytest = "PYTEST_CURRENT_TEST" in os.environ
        effective_restart = (not in_pytest) if trigger_restart is None else bool(trigger_restart)
        try:
            res = (
                mgr.promote(trigger_restart=effective_restart)
                if hasattr(mgr, "promote")
                else mgr.promote_staging_to_main(trigger_restart=effective_restart)
            )
        except RuntimeError as exc:
            err_text = redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            lowered = err_text.lower()
            if any(
                token in lowered
                for token in ("construcción", "cola", "en ejecución", "building", "promoting")
            ):
                raise ProjectConflictError(err_text) from exc
            raise ValueError(err_text) from exc

        promoted = bool(res.get("promoted"))
        merged_commits = int(res.get("merged_commits") or 0)
        commits = list(res.get("commits") or [])
        restart_scheduled = bool(promoted and effective_restart)
        if promoted:
            msg = (
                f"Se promovieron {merged_commits} mejora(s) a main. Reiniciando Liwi..."
                if restart_scheduled
                else f"Se promovieron {merged_commits} mejora(s) a main."
            )
        else:
            msg = "No hay mejoras pendientes en staging para promover."
        return _scrub_value(
            {
                "ok": True,
                "promoted": promoted,
                "merged_commits": merged_commits,
                "commits": commits,
                "restart_scheduled": restart_scheduled,
                "message": msg,
            },
            self.secrets_to_redact,
        )

    def run_staging_visual_qa(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        default_base_url: str | None = None,
    ) -> dict[str, Any]:
        """Run bounded-timeout pre-promote visual QA against the local staging view with safe degradation."""
        if timeout_seconds is None:
            eff_timeout = visual_qa_agent.DEFAULT_STAGING_QA_TIMEOUT_SECONDS
        elif isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("El campo 'timeout_seconds' debe ser numérico.")
        else:
            parsed_timeout = float(timeout_seconds)
            if not math.isfinite(parsed_timeout) or parsed_timeout <= 0:
                raise ValueError("El campo 'timeout_seconds' debe ser finito y mayor que 0.")
            eff_timeout = min(
                parsed_timeout,
                visual_qa_agent.MAX_STAGING_QA_TIMEOUT_SECONDS,
            )

        explicit_url = str(base_url).strip() if isinstance(base_url, str) and base_url.strip() else None
        if explicit_url is not None:
            visual_qa_agent._validate_local_staging_url(explicit_url)

        with self._lock:
            mgr = self._staging_manager

        if mgr is None and explicit_url is None:
            return _scrub_value(
                visual_qa_agent._build_skipped_staging_qa_payload(
                    reason="staging_unavailable",
                    detail="Staging no está inicializado o disponible para auditoría visual.",
                    base_url=str(default_base_url or ""),
                    timeout_seconds=eff_timeout,
                ),
                self.secrets_to_redact,
            )

        staging_url: str | None = None
        if mgr is not None:
            try:
                staging_status = self.get_self_improve_staging_status()
            except (OSError, RuntimeError) as exc:
                if explicit_url is None:
                    return _scrub_value(
                        visual_qa_agent._build_skipped_staging_qa_payload(
                            reason="staging_unavailable",
                            detail=f"No se pudo consultar el estado de staging para auditoría visual: {exc}",
                            base_url=str(default_base_url or ""),
                            timeout_seconds=eff_timeout,
                        ),
                        self.secrets_to_redact,
                    )
                staging_status = {}
            for key in ("staging_url", "preview_url", "url"):
                cand = staging_status.get(key)
                if isinstance(cand, str) and cand.strip():
                    staging_url = cand.strip()
                    break
            raw_wt = getattr(mgr, "worktree_path", None)
            pending_count = int(staging_status.get("pending_count") or 0)
            staging_uninitialized = staging_status.get("initialized") is False
            worktree_missing = isinstance(raw_wt, Path) and not raw_wt.is_dir() and pending_count <= 0
            if explicit_url is None and staging_url is None and (staging_uninitialized or worktree_missing):
                detail_msg = (
                    "Staging no está inicializado para auditoría visual."
                    if staging_uninitialized
                    else "El worktree local de staging no está disponible para auditoría visual."
                )
                return _scrub_value(
                    visual_qa_agent._build_skipped_staging_qa_payload(
                        reason="staging_unavailable",
                        detail=detail_msg,
                        base_url=str(default_base_url or ""),
                        timeout_seconds=eff_timeout,
                    ),
                    self.secrets_to_redact,
                )

        target_url = explicit_url or staging_url or (str(default_base_url).strip() if default_base_url else "")
        if not target_url:
            return _scrub_value(
                visual_qa_agent._build_skipped_staging_qa_payload(
                    reason="staging_unavailable",
                    detail="No hay una URL local de staging disponible para auditoría visual.",
                    base_url="",
                    timeout_seconds=eff_timeout,
                ),
                self.secrets_to_redact,
            )

        result = visual_qa_agent.run_staging_visual_qa(
            base_url=target_url,
            timeout_seconds=eff_timeout,
            db_path=self._resolve_idea_db_path(),
            audit_fn=self._visual_qa_runner,
        )
        return _scrub_value(result, self.secrets_to_redact)

    def get_factory_assistant_history(self) -> dict[str, Any]:
        """Return Liwi self-improvement chat history and persistent memory rules."""
        return _scrub_value(self._effective_self_improver().get_history(), self.secrets_to_redact)

    def get_factory_assistant_status(self) -> dict[str, Any]:
        """Return live real-time status of Liwi self-improvement apply execution."""
        return _scrub_value(self._effective_self_improver().get_status(), self.secrets_to_redact)

    def get_factory_assistant_versions(self, *, limit: int = 30) -> dict[str, Any]:
        """Return Git evolutionary version history of Liwi with secrets scrubbed."""
        return _scrub_value(
            self._effective_self_improver().get_evolution_history(limit=limit),
            self.secrets_to_redact,
        )

    def get_factory_assistant_upload(self, filename: str) -> tuple[str, bytes]:
        """Serve a validated screenshot uploaded to factory_chat_uploads."""
        try:
            return self._effective_self_improver().get_uploaded_image(filename)
        except FileNotFoundError as exc:
            raise ProjectNotFoundError(str(exc)) from exc

    def post_factory_assistant_message(
        self,
        message: str,
        *,
        images: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Consult Liwi self-improvement assistant in read-only mode with optional screenshots."""
        res = self._effective_self_improver().consult_message(message, images=images)
        return _scrub_value(res, self.secrets_to_redact)

    def delete_factory_assistant_rule(self, rule_id: str) -> dict[str, Any]:
        """Delete a rule from factory_memory_rules."""
        try:
            res = self._effective_self_improver().delete_memory_rule(rule_id)
        except KeyError as exc:
            raise ProjectNotFoundError(f"Regla '{rule_id}' no encontrada.") from exc
        return _scrub_value(res, self.secrets_to_redact)

    def apply_factory_assistant_proposal(
        self,
        proposal_prompt: str,
        *,
        proposal_title: str = "",
        message_id: str | None = None,
        use_staging: bool | None = None,
    ) -> dict[str, Any]:
        """Enqueue a Liwi improvement in background staging (or run legacy direct apply when injected)."""
        clean_prompt = str(proposal_prompt or "").strip()
        if not clean_prompt:
            raise ValueError("El campo 'proposal_prompt' es obligatorio.")
        clean_title = (
            redact_secrets(
                str(proposal_title or "Auto-mejora Liwi").strip(),
                extra_secrets=self.secrets_to_redact,
            )[:100]
            or "Auto-mejora Liwi"
        )

        if self._should_use_staging(use_staging):
            mgr = self._get_staging_manager()
            metadata: dict[str, Any] = {}
            if message_id:
                metadata["message_id"] = str(message_id)
            try:
                staging_status = mgr.enqueue_improvement(
                    title=clean_title,
                    prompt=clean_prompt,
                    metadata=metadata,
                )
            except RuntimeError as exc:
                raise ProjectConflictError(
                    redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
                ) from exc
            return _scrub_value(
                {
                    "ok": True,
                    "queued": True,
                    "applied": False,
                    "rolled_back": False,
                    "restart_scheduled": False,
                    "title": clean_title,
                    "message": f"Mejora '{clean_title}' encolada en staging en segundo plano.",
                    "staging": staging_status,
                },
                self.secrets_to_redact,
            )

        gh_tok = str(getattr(self.vcs, "github_token", "") or "")
        try:
            res = self._effective_self_improver().apply_proposal(
                clean_prompt,
                proposal_title=clean_title,
                message_id=message_id,
                github_token=gh_tok,
            )
        except RuntimeError as exc:
            if "ejecución" in str(exc).lower():
                raise ProjectConflictError(str(exc)) from exc
            raise ValueError(redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)) from exc
        return _scrub_value(res, self.secrets_to_redact)

    def _resolve_idea_db_path(self) -> Path | None:
        """Resolve SQLite path for improvement_ideas targeting telemetry.db (respecting env override and test isolation)."""
        env_val = os.environ.get(telemetry.TELEMETRY_DB_ENV_VAR, "").strip()
        if env_val:
            return Path(env_val).resolve()
        db_path = getattr(self.orchestrator.store, "db_path", None)
        if db_path and str(db_path) != ":memory:":
            db_parent = Path(db_path).resolve().parent
            repo_data_dir = Path("data").resolve()
            if db_parent == repo_data_dir:
                return (repo_data_dir / "telemetry.db").resolve()
            return (db_parent / "telemetry.db").resolve()
        return None

    def _build_staging_queue_payload(self) -> dict[str, Any]:
        """Return normalized live StagingManager queue state for modal synchronization."""
        try:
            mgr = self._get_staging_manager()
            st = mgr.get_status() if hasattr(mgr, "get_status") else mgr.get_staging_status()
        except Exception:  # noqa: BLE001
            st = {}
        if not isinstance(st, Mapping):
            st = {}
        is_building = bool(st.get("is_building"))
        queue_size = int(st.get("queue_size") or 0)
        cur_title = str(st.get("current_task") or "").strip()
        cur_idea_id = st.get("current_idea_id")
        if not (isinstance(cur_idea_id, int) and not isinstance(cur_idea_id, bool)):
            cur_idea_id = None
        raw_queued_ids = st.get("queued_idea_ids")
        queued_idea_ids = (
            [int(x) for x in raw_queued_ids if isinstance(x, int) and not isinstance(x, bool)]
            if isinstance(raw_queued_ids, Sequence) and not isinstance(raw_queued_ids, (str, bytes))
            else []
        )
        step = str(st.get("current_step") or "").strip()
        if not step and self._self_improver is not None:
            try:
                imp_st = self._self_improver.get_status()
                if isinstance(imp_st, Mapping) and imp_st.get("running"):
                    step = str(imp_st.get("stage") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.debug("self_improver status skipped: %s", exc)
        if not step and is_building:
            step = "Construyendo en .liwi-staging"
        return {
            "active": bool(is_building or queue_size > 0),
            "current_title": cur_title,
            "current_idea_id": cur_idea_id,
            "queued_idea_ids": queued_idea_ids,
            "pending_count": queue_size + (1 if is_building else 0),
            "step": step,
        }

    def list_improvement_ideas(self, *, status: str | None = None) -> dict[str, Any]:
        """Return improvement ideas (optionally filtered by status), status counts, and auto-generation interval."""
        db_path = self._resolve_idea_db_path()
        result = idea_engine.list_ideas(
            status=status,
            include_counts=True,
            db_path=db_path,
        )
        assert isinstance(result, dict)
        ideas_list = list(result["ideas"])
        norm_status = str(status or "").strip().lower()
        if norm_status in {"", "all", "implemented", "aplicada"}:
            hist = idea_engine.list_autonomous_history(db_path=db_path)
            existing_ids = {
                int(it["id"])
                for it in ideas_list
                if isinstance(it.get("id"), int) and int(it["id"]) > 0
            }
            existing_shas = {
                str(it.get("commit_sha") or "").strip()[:7]
                for it in ideas_list
                if str(it.get("commit_sha") or "").strip()
            }
            for entry in hist:
                if str(entry.get("status") or "") == "revertida":
                    continue
                eid = entry.get("idea_id")
                if isinstance(eid, int) and eid in existing_ids:
                    continue
                chash = str(entry.get("commit_hash") or "").strip()
                if chash and chash[:7] in existing_shas:
                    continue
                ideas_list.append(
                    {
                        "id": -int(entry["id"]),
                        "history_entry_id": int(entry["id"]),
                        "created_at": entry.get("applied_at") or "",
                        "completed_at": entry.get("applied_at") or "",
                        "duration_seconds": None,
                        "commit_sha": chash,
                        "title": entry.get("title") or "Mejora autónoma",
                        "category": "Automejora",
                        "impact": "Medio",
                        "simple_description": entry.get("simple_description") or "",
                        "technical_prompt": "",
                        "status": "aplicada",
                        "source": "autonomous_history",
                        "retry_count": 0,
                    }
                )
        interval_hours = idea_engine.get_auto_interval_hours(db_path=db_path)
        return _scrub_value(
            {
                "ideas": ideas_list,
                "counts": result["counts"],
                "status": result["status"],
                "auto_interval_hours": interval_hours,
                "staging_queue": self._build_staging_queue_payload(),
            },
            self.secrets_to_redact,
        )

    def set_idea_radar_settings(self, auto_interval_hours: float) -> dict[str, Any]:
        """Validate and persist automatic ideation frequency (hours; 0 disables auto mode)."""
        db_path = self._resolve_idea_db_path()
        saved = idea_engine.set_auto_interval_hours(auto_interval_hours, db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "auto_interval_hours": saved,
            },
            self.secrets_to_redact,
        )

    def generate_improvement_ideas(
        self,
        *,
        count: int = idea_engine.DEFAULT_IDEATION_COUNT,
        auto_interval_hours: float | None = None,
        generate: bool = True,
    ) -> dict[str, Any]:
        """Persist optional auto_interval_hours and trigger read-only idea generation when requested."""
        db_path = self._resolve_idea_db_path()
        if auto_interval_hours is not None:
            idea_engine.set_auto_interval_hours(auto_interval_hours, db_path=db_path)

        created: list[dict[str, Any]] = []
        if generate:
            eff_runner = (
                self.runner
                if self.runner is not None
                else getattr(self.build_worker, "runner", None)
            )
            raw_root = getattr(self._self_improver, "repo_root", None)
            repo_root = Path(raw_root).resolve() if raw_root is not None else None
            eff_model = None
            if self.model_resolver is not None and hasattr(self.model_resolver, "resolve_model"):
                try:
                    eff_model = self.model_resolver.resolve_model("INTERVIEW")
                except (RuntimeError, ValueError, OSError):
                    eff_model = None

            created = idea_engine.generate_new_ideas(
                source="manual",
                count=count,
                db_path=db_path,
                runner=eff_runner,
                git_runner=eff_runner,
                repo_root=repo_root,
                model=eff_model,
            )

        ideas = idea_engine.list_ideas(db_path=db_path)
        interval_hours = idea_engine.get_auto_interval_hours(db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "created": created,
                "created_count": len(created),
                "ideas": ideas,
                "auto_interval_hours": interval_hours,
            },
            self.secrets_to_redact,
        )

    def implement_improvement_idea(self, idea_id: int) -> dict[str, Any]:
        """Enqueue an improvement idea directly into .liwi-staging and mark it 'encolada'."""
        db_path = self._resolve_idea_db_path()
        mgr = self._get_staging_manager()
        try:
            res = idea_engine.implement_idea(idea_id, staging_manager=mgr, db_path=db_path)
        except KeyError as exc:
            raise ProjectNotFoundError(f"Idea '{idea_id}' no encontrada.") from exc
        except (ValueError, RuntimeError) as exc:
            raise ProjectConflictError(
                redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            ) from exc
        payload = dict(res)
        if "staging_status" in payload and "staging" not in payload:
            payload["staging"] = payload["staging_status"]
        return _scrub_value(payload, self.secrets_to_redact)

    def run_improvement_idea_now(self, idea_id: int) -> dict[str, Any]:
        """Trigger immediate staging execution for a queued (or pending) improvement idea and mark it 'implementando'."""
        db_path = self._resolve_idea_db_path()
        idea = idea_engine.get_idea(idea_id, db_path=db_path)
        if idea is None:
            raise ProjectNotFoundError(f"Idea '{idea_id}' no encontrada.")
        status = str(idea.get("status") or "").strip().lower()
        if status == "descartada":
            raise ProjectConflictError("No se puede ejecutar una idea descartada.")
        if status == "aplicada":
            raise ProjectConflictError("La idea ya fue implementada.")
        if status == "implementando":
            raise ProjectConflictError("La idea ya está en ejecución.")

        mgr = self._get_staging_manager()
        try:
            staging_status = mgr.enqueue_improvement(
                title=idea["title"],
                prompt=idea["technical_prompt"],
                metadata={
                    "idea_id": idea["id"],
                    "category": idea["category"],
                    "impact": idea["impact"],
                    "source": idea["source"],
                    "run_now": True,
                },
            )
        except (ValueError, RuntimeError) as exc:
            raise ProjectConflictError(
                redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            ) from exc

        updated = idea_engine.update_idea_status(idea_id, "implementing", db_path=db_path)
        counts = idea_engine.count_ideas_by_status(db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "idea": updated,
                "counts": counts,
                "staging_status": staging_status,
                "staging": staging_status,
            },
            self.secrets_to_redact,
        )

    def remove_queued_idea(self, idea_id: int) -> dict[str, Any]:
        """Return a queued idea back to 'nueva' and clear queue/execution timestamps."""
        db_path = self._resolve_idea_db_path()
        idea = idea_engine.get_idea(idea_id, db_path=db_path)
        if idea is None:
            raise ProjectNotFoundError(f"Idea '{idea_id}' no encontrada.")
        status = str(idea.get("status") or "").strip().lower()
        if status == "implementando":
            raise ProjectConflictError("No se puede quitar una idea mientras está en ejecución.")
        if status == "aplicada":
            raise ProjectConflictError("La idea ya fue implementada.")
        updated = idea_engine.update_idea_status(idea_id, "nueva", db_path=db_path)
        counts = idea_engine.count_ideas_by_status(db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "idea": updated,
                "counts": counts,
            },
            self.secrets_to_redact,
        )

    def reconcile_idea_queue_now(self) -> dict[str, Any]:
        """Run immediate queue watchdog investigation and recovery."""
        db_path = self._resolve_idea_db_path()
        raw_root = getattr(self._self_improver, "repo_root", None)
        repo_root = Path(raw_root).resolve() if raw_root is not None else Path.cwd()
        try:
            staging_mgr = self._get_staging_manager()
        except Exception:  # noqa: BLE001
            staging_mgr = None
        report = idea_engine.reconcile_and_investigate_queue(
            db_path=db_path,
            repo_root=repo_root,
            staging_manager=staging_mgr,
        )
        counts = idea_engine.count_ideas_by_status(db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "report": report,
                "counts": counts,
            },
            self.secrets_to_redact,
        )

    def drain_idea_queue(self) -> dict[str, Any]:
        """Start sequential execution of all queued ideas in .liwi-staging without auto-ideation."""
        db_path = self._resolve_idea_db_path()
        mgr = self._get_staging_manager()
        res = idea_engine.drain_queued_ideas(
            staging_manager=mgr,
            db_path=db_path,
        )
        return _scrub_value(res, self.secrets_to_redact)

    def dismiss_improvement_idea(self, idea_id: int) -> dict[str, Any]:
        """Mark an improvement idea as 'descartada'."""
        db_path = self._resolve_idea_db_path()
        dismissed = idea_engine.dismiss_idea(idea_id, db_path=db_path)
        if dismissed is None:
            raise ProjectNotFoundError(f"Idea '{idea_id}' no encontrada.")
        return _scrub_value(
            {
                "ok": True,
                "dismissed": True,
                "idea": dismissed,
            },
            self.secrets_to_redact,
        )

    def _has_active_factory_builds(self) -> bool:
        with self._lock:
            if self._active_projects or self._active_ops:
                return True
        for pid in self.orchestrator.store.list_project_ids():
            try:
                validate_project_id(pid)
                if not self.orchestrator.store.get_events(pid):
                    continue
                status = self.orchestrator.get_status(
                    pid,
                    notify_publisher=False,
                    notify_notifier=False,
                )
            except (ValueError, IllegalTransition):
                continue
            raw_state = getattr(status.state, "value", status.state)
            if str(raw_state or "").upper() in {"BUILDING", "RECOVERING"}:
                return True
        return False

    def _has_running_local_apps(self) -> bool:
        runner = self.local_runner
        if (
            isinstance(runner, LocalAppRunner)
            and type(runner).get_status is LocalAppRunner.get_status
            and "get_status" not in runner.__dict__
        ):
            with runner._lock:
                runner._prune_dead_locked()
                return any(
                    entry.process.poll() is None
                    for entry in runner._processes.values()
                )
        for pid in self.orchestrator.store.list_project_ids():
            pid_clean = str(pid or "").strip()
            if not pid_clean:
                continue
            try:
                if runner.get_status(pid_clean).running:
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("local_runner.get_status skipped for %s: %s", pid_clean, exc)
        return False

    def _get_autonomous_engine(self) -> idea_engine.AutonomousEngine:
        with self._lock:
            if self._autonomous_engine is None:
                db_path = self._resolve_idea_db_path()
                raw_root = getattr(self._self_improver, "repo_root", None)
                repo_root = Path(raw_root).resolve() if raw_root is not None else Path.cwd()
                eff_runner = (
                    self.runner
                    if self.runner is not None
                    else getattr(self.build_worker, "runner", None)
                )
                self._autonomous_engine = idea_engine.AutonomousEngine(
                    db_path=db_path,
                    repo_root=repo_root,
                    staging_manager=self._staging_manager,
                    has_active_builds=self._has_active_factory_builds,
                    has_running_local_apps=self._has_running_local_apps,
                    command_runner=eff_runner,
                )
            return self._autonomous_engine

    def get_autonomous_status(self) -> dict[str, Any]:
        """Return live 24/7 autonomous status, quota meter telemetry, and recent autonomous history."""
        staging_queue = self._build_staging_queue_payload()
        try:
            engine = self._get_autonomous_engine()
            snap = engine.get_status_snapshot()
            snap["staging_queue"] = staging_queue
            return _scrub_value(snap, self.secrets_to_redact)
        except Exception as exc:  # noqa: BLE001
            clean_err = redact_secrets(
                str(exc) or exc.__class__.__name__,
                extra_secrets=self.secrets_to_redact,
            )
            logger.warning("Autonomous status degraded fallback: %s", clean_err)
            est_turns = max(
                0,
                idea_engine.DEFAULT_ESTIMATED_WINDOW_CAPACITY
                // max(1, idea_engine.DEFAULT_AVG_TURN_TOKENS),
            )
            return _scrub_value(
                {
                    "autonomous_enabled": False,
                    "cooldown_minutes": idea_engine.DEFAULT_AUTONOMOUS_COOLDOWN_MINUTES,
                    "consecutive_failures": 0,
                    "state": "apagado",
                    "current_task": None,
                    "is_running": False,
                    "busy_reason": None,
                    "pause_reason": None,
                    "last_error": {"message": clean_err},
                    "last_cycle_result": None,
                    "degraded": True,
                    "staging_queue": staging_queue,
                    "quota": {
                        "window_tokens_used": 0,
                        "estimated_window_capacity": idea_engine.DEFAULT_ESTIMATED_WINDOW_CAPACITY,
                        "quota_remaining_pct": 100,
                        "reserve_threshold_pct": idea_engine.DEFAULT_RESERVE_THRESHOLD_PCT,
                        "turns_used": 0,
                        "turns_remaining_est": est_turns,
                        "is_paused": False,
                        "reserve_reached": False,
                        "resume_hhmm": None,
                    },
                    "history": [],
                },
                self.secrets_to_redact,
            )

    def toggle_autonomous_mode(
        self,
        *,
        autonomous_enabled: bool | None = None,
        cooldown_minutes: int | None = None,
        reserve_threshold_pct: int | None = None,
    ) -> dict[str, Any]:
        """Toggle 24/7 autonomous mode or update cooldown / reserve threshold cleanly."""
        engine = self._get_autonomous_engine()
        reconcile_report: dict[str, Any] | None = None
        engine.update_config(
            autonomous_enabled=autonomous_enabled,
            cooldown_minutes=cooldown_minutes,
            reserve_threshold_pct=reserve_threshold_pct,
        )
        if autonomous_enabled is True:
            raw_root = getattr(self._self_improver, "repo_root", None)
            repo_root = Path(raw_root).resolve() if raw_root is not None else Path.cwd()
            try:
                staging_mgr = self._get_staging_manager()
            except Exception:  # noqa: BLE001
                staging_mgr = None
            try:
                reconcile_report = idea_engine.reconcile_and_investigate_queue(
                    db_path=self._resolve_idea_db_path(),
                    repo_root=repo_root,
                    staging_manager=staging_mgr,
                )
            except Exception:  # noqa: BLE001
                reconcile_report = None
            engine.start()
        payload: dict[str, Any] = {"ok": True, **engine.get_status_snapshot()}
        if reconcile_report is not None:
            payload["queue_reconcile"] = reconcile_report
        return _scrub_value(payload, self.secrets_to_redact)

    def revert_autonomous_improvement(self, entry_id: int) -> dict[str, Any]:
        """Revert an autonomous improvement commit on main, verify tests, and discard linked idea."""
        engine = self._get_autonomous_engine()
        try:
            res = engine.revert_autonomous_improvement(entry_id)
        except KeyError as exc:
            raise ProjectNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise ValueError(redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)) from exc
        except RuntimeError as exc:
            raise ProjectConflictError(
                redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            ) from exc

        restart_scheduled = False
        if "PYTEST_CURRENT_TEST" not in os.environ:
            self._trigger_supervised_restart()
            restart_scheduled = True

        return _scrub_value(
            {
                **res,
                "restart_scheduled": restart_scheduled,
                "status": engine.get_status_snapshot(),
            },
            self.secrets_to_redact,
        )

    def run_visual_audit(self, *, base_url: str | None = None) -> dict[str, Any]:
        """Execute headless Playwright synthetic-user audit against dashboard UI and persist ideas."""
        eff_url = str(base_url or f"http://{LOOPBACK_HOST}:8000").strip()
        db_path = self._resolve_idea_db_path()
        raw_root = getattr(self._self_improver, "repo_root", None)
        repo_root = Path(raw_root).resolve() if raw_root is not None else None
        eff_runner = (
            self.runner
            if self.runner is not None
            else getattr(self.build_worker, "runner", None)
        )
        eff_model = None
        if self.model_resolver is not None and hasattr(self.model_resolver, "resolve_model"):
            try:
                eff_model = self.model_resolver.resolve_model("INTERVIEW")
            except (RuntimeError, ValueError, OSError):
                eff_model = None

        audit_res = visual_qa_agent.run_synthetic_user_audit(
            base_url=eff_url,
            db_path=db_path,
            runner=eff_runner,
            repo_root=repo_root,
            model=eff_model,
        )
        ideas = idea_engine.list_ideas(db_path=db_path)
        return _scrub_value(
            {
                **audit_res,
                "ideas": ideas,
            },
            self.secrets_to_redact,
        )

    def _assert_project_exists_for_ideas(self, project_id: str) -> tuple[WorkspaceManager | None, Path | None]:
        validate_project_id(project_id)
        events = self.orchestrator.store.get_events(project_id)
        ws_mgr = self._sync_external_workspace(project_id)
        ws_dir = ws_mgr.get_workspace_dir(project_id) if ws_mgr is not None else None
        if not events and (ws_dir is None or not ws_dir.is_dir()):
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        return ws_mgr, ws_dir

    def list_project_ideas(
        self,
        project_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List auto-improvement ideas for a generated project with per-status counts."""
        self._assert_project_exists_for_ideas(project_id)
        db_path = self._resolve_idea_db_path()
        result = idea_engine.list_project_ideas(
            project_id,
            status=status,
            include_counts=True,
            db_path=db_path,
        )
        assert isinstance(result, dict)
        return _scrub_value(
            {
                "ok": True,
                "project_id": project_id,
                "ideas": result["ideas"],
                "counts": result["counts"],
                "status": result["status"],
            },
            self.secrets_to_redact,
        )

    def generate_project_ideas(
        self,
        project_id: str,
        *,
        count: int = idea_engine.DEFAULT_IDEATION_COUNT,
    ) -> dict[str, Any]:
        """Run read-only project inspection via agy.EXE and store actionable improvements for project_id."""
        _ws_mgr, ws_dir = self._assert_project_exists_for_ideas(project_id)
        if ws_dir is None or not ws_dir.is_dir():
            raise ProjectNotFoundError(f"Workspace de '{project_id}' no encontrado")

        db_path = self._resolve_idea_db_path()
        eff_runner = (
            self.runner
            if self.runner is not None
            else getattr(self.build_worker, "runner", None)
        )
        eff_model = None
        if self.model_resolver is not None and hasattr(self.model_resolver, "resolve_model"):
            try:
                eff_model = self.model_resolver.resolve_model("INTERVIEW")
            except (RuntimeError, ValueError, OSError):
                eff_model = None

        created = idea_engine.generate_project_ideas(
            project_id,
            ws_dir,
            count=count,
            source="manual",
            db_path=db_path,
            runner=eff_runner,
            model=eff_model,
        )
        ideas = idea_engine.list_project_ideas(project_id, db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "project_id": project_id,
                "created": created,
                "created_count": len(created),
                "ideas": ideas,
            },
            self.secrets_to_redact,
        )

    def approve_project_idea(self, project_id: str, idea_id: int) -> dict[str, Any]:
        """Approve a per-project idea, mark 'implementando', and enqueue its change_prompt in modify_project."""
        self._assert_project_exists_for_ideas(project_id)
        db_path = self._resolve_idea_db_path()
        idea = idea_engine.get_project_idea(idea_id, db_path=db_path)
        if idea is None or idea["project_slug"] != project_id:
            raise ProjectNotFoundError(f"Idea '{idea_id}' no encontrada para '{project_id}'.")

        updated = idea_engine.update_project_idea_status(
            idea_id,
            "implementando",
            project_slug=project_id,
            db_path=db_path,
        )
        try:
            modify_res = self.modify_project(project_id, idea["change_prompt"])
        except Exception:
            idea_engine.update_project_idea_status(
                idea_id,
                "nueva",
                project_slug=project_id,
                db_path=db_path,
            )
            raise
        ideas = idea_engine.list_project_ideas(project_id, db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "project_id": project_id,
                "idea": updated,
                "ideas": ideas,
                "modify": modify_res,
            },
            self.secrets_to_redact,
        )

    def dismiss_project_idea(self, project_id: str, idea_id: int) -> dict[str, Any]:
        """Dismiss a per-project improvement idea."""
        self._assert_project_exists_for_ideas(project_id)
        db_path = self._resolve_idea_db_path()
        try:
            updated = idea_engine.update_project_idea_status(
                idea_id,
                "descartada",
                project_slug=project_id,
                db_path=db_path,
            )
        except KeyError as exc:
            raise ProjectNotFoundError(str(exc)) from exc
        ideas = idea_engine.list_project_ideas(project_id, db_path=db_path)
        return _scrub_value(
            {
                "ok": True,
                "dismissed": True,
                "project_id": project_id,
                "idea": updated,
                "ideas": ideas,
            },
            self.secrets_to_redact,
        )

    @staticmethod
    def _is_transient_gateway_fetch_error(
        source: str,
        error_text: str,
        context: Mapping[str, Any] | str | None,
    ) -> bool:
        """Return True for transient gateway/restart HTTP 502/503/504 errors from fetch_500."""
        if str(source or "").strip().lower() != "fetch_500":
            return False
        if isinstance(context, Mapping):
            raw_status = context.get("status")
            if not isinstance(raw_status, bool) and raw_status is not None:
                try:
                    if int(raw_status) in {502, 503, 504}:
                        return True
                except (TypeError, ValueError):
                    pass
        lowered = str(error_text or "").lower()
        return any(f"http {code}" in lowered for code in (502, 503, 504))

    def report_client_error(
        self,
        *,
        source: str,
        error_text: str,
        context: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Record a browser JS or HTTP >= 500 error as an actionable triage ticket."""
        if self._is_transient_gateway_fetch_error(source, error_text, context):
            return {
                "ok": True,
                "ignored": True,
                "reason": "transient_gateway_status",
                "ticket": None,
            }
        ticket = error_triage.build_actionable_fix_ticket(
            source,
            error_text,
            context=context,
            db_path=self._resolve_idea_db_path(),
        )
        return _scrub_value({"ok": True, "ticket": ticket}, self.secrets_to_redact)

    def list_triage_tickets(self) -> dict[str, Any]:
        """List pending actionable triage tickets."""
        tickets = error_triage.list_triage_tickets(
            status="pendiente",
            db_path=self._resolve_idea_db_path(),
        )
        return _scrub_value({"ok": True, "tickets": tickets}, self.secrets_to_redact)

    def repair_triage_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Enqueue a pending triage ticket's repair_prompt into .liwi-staging."""
        db_path = self._resolve_idea_db_path()
        ticket = error_triage.get_triage_ticket(ticket_id, db_path=db_path)
        if ticket is None:
            raise ProjectNotFoundError(f"Ticket de diagnóstico '{ticket_id}' no encontrado.")
        updated = error_triage.update_triage_ticket_status(
            ticket_id,
            "reparando",
            db_path=db_path,
        )
        mgr = self._get_staging_manager()
        try:
            staging_status = mgr.enqueue_improvement(
                title=f"🩹 Auto-reparación #{ticket_id}",
                prompt=ticket["repair_prompt"],
                metadata={"triage_ticket_id": ticket_id},
            )
        except Exception as exc:
            error_triage.update_triage_ticket_status(ticket_id, "pendiente", db_path=db_path)
            raise ProjectConflictError(
                redact_secrets(str(exc), extra_secrets=self.secrets_to_redact)
            ) from exc
        return _scrub_value(
            {
                "ok": True,
                "queued": True,
                "ticket": updated,
                "staging": staging_status,
            },
            self.secrets_to_redact,
        )

    def dismiss_triage_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Dismiss a triage ticket so its alert banner hides."""
        try:
            updated = error_triage.update_triage_ticket_status(
                ticket_id,
                "descartado",
                db_path=self._resolve_idea_db_path(),
            )
        except KeyError as exc:
            raise ProjectNotFoundError(str(exc)) from exc
        return _scrub_value(
            {"ok": True, "dismissed": True, "ticket": updated},
            self.secrets_to_redact,
        )

    def list_triage_solutions(
        self,
        *,
        status: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List Auto-Triage solutions with optional status/scope filter and pending count."""
        db_path = self._resolve_idea_db_path()
        solutions = error_triage.list_triage_solutions(
            status=status,
            scope=scope,
            limit=limit,
            db_path=db_path,
        )
        if status is not None and str(status).strip().lower() == "lista" and scope is None:
            pending_count = len(solutions)
        else:
            pending_count = len(
                error_triage.list_triage_solutions(status="lista", db_path=db_path)
            )
        return _scrub_value(
            {
                "ok": True,
                "solutions": solutions,
                "pending_count": pending_count,
                "status_filter": str(status).strip().lower() if status is not None else None,
            },
            self.secrets_to_redact,
        )

    def decide_triage_solution(self, solution_id: int, decision: str) -> dict[str, Any]:
        """Mark an Auto-Triage solution as approved ('encolada') or dismissed ('descartada')."""
        db_path = self._resolve_idea_db_path()
        try:
            updated = error_triage.decide_triage_solution(
                solution_id,
                decision,
                db_path=db_path,
            )
        except KeyError as exc:
            raise ProjectNotFoundError(str(exc)) from exc
        pending_count = len(
            error_triage.list_triage_solutions(status="lista", db_path=db_path)
        )
        return _scrub_value(
            {
                "ok": True,
                "decision": str(decision).strip().lower(),
                "status": updated["status"],
                "solution": updated,
                "pending_count": pending_count,
            },
            self.secrets_to_redact,
        )


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
      --btn-primary-fg: #082f49;
      --ok: #22c55e;
      --ok-btn: #15803d;
      --ok-btn-hover: #166534;
      --ok-bg: rgba(34, 197, 94, 0.14);
      --warn: #f59e0b;
      --warn-btn: #b45309;
      --warn-btn-hover: #92400e;
      --warn-bg: rgba(245, 158, 11, 0.14);
      --err: #f87171;
      --err-bg: rgba(239, 68, 68, 0.14);
      --info-bg: rgba(56, 189, 248, 0.12);
      --fs-xs: 0.76rem;
      --fs-sm: 0.84rem;
      --fs-base: 0.94rem;
      --fs-lg: 1.15rem;
      --fs-xl: 1.35rem;
      --sp-1: 4px;
      --sp-2: 8px;
      --sp-3: 12px;
      --sp-4: 16px;
      --sp-5: 20px;
      --sp-6: 24px;
    }
    html[data-theme="light"] {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-alt: #f1f5f9;
      --border: #cbd5e1;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0369a1;
      --accent-hover: #075985;
      --btn-primary-fg: #ffffff;
      --ok: #15803d;
      --ok-btn: #15803d;
      --ok-btn-hover: #166534;
      --ok-bg: #dcfce7;
      --warn: #b45309;
      --warn-btn: #b45309;
      --warn-btn-hover: #92400e;
      --warn-bg: #fef3c7;
      --err: #b91c1c;
      --err-bg: #fee2e2;
      --info-bg: #e0f2fe;
    }
    html[data-theme="emerald"] {
      --bg: #0b1915;
      --surface: #112620;
      --surface-alt: #163229;
      --border: #244f42;
      --text: #ecfdf5;
      --muted: #a7d9c6;
      --accent: #34d399;
      --accent-hover: #10b981;
      --btn-primary-fg: #032217;
      --ok: #4ade80;
      --ok-btn: #15803d;
      --ok-btn-hover: #166534;
      --ok-bg: rgba(52, 211, 153, 0.14);
      --warn: #fbbf24;
      --warn-btn: #b45309;
      --warn-btn-hover: #92400e;
      --warn-bg: rgba(251, 191, 36, 0.14);
      --err: #fca5a5;
      --err-bg: rgba(248, 113, 113, 0.15);
      --info-bg: rgba(52, 211, 153, 0.12);
    }
    html[data-theme="sunset"] {
      --bg: #1b1311;
      --surface: #271b18;
      --surface-alt: #332420;
      --border: #523932;
      --text: #fff7ed;
      --muted: #e2bfb0;
      --accent: #fb923c;
      --accent-hover: #f97316;
      --btn-primary-fg: #2b1102;
      --ok: #4ade80;
      --ok-btn: #15803d;
      --ok-btn-hover: #166534;
      --ok-bg: rgba(74, 222, 128, 0.14);
      --warn: #fbbf24;
      --warn-btn: #b45309;
      --warn-btn-hover: #92400e;
      --warn-bg: rgba(251, 191, 36, 0.15);
      --err: #fda4af;
      --err-bg: rgba(251, 113, 133, 0.16);
      --info-bg: rgba(251, 146, 60, 0.14);
    }
    html[data-theme="lavender"] {
      --bg: #14141f;
      --surface: #1d1d2e;
      --surface-alt: #27273d;
      --border: #3d3d5c;
      --text: #f5f3ff;
      --muted: #c4b5fd;
      --accent: #a78bfa;
      --accent-hover: #8b5cf6;
      --btn-primary-fg: #1b0f3b;
      --ok: #4ade80;
      --ok-btn: #15803d;
      --ok-btn-hover: #166534;
      --ok-bg: rgba(74, 222, 128, 0.14);
      --warn: #fbbf24;
      --warn-btn: #b45309;
      --warn-btn-hover: #92400e;
      --warn-bg: rgba(251, 191, 36, 0.14);
      --err: #fda4af;
      --err-bg: rgba(244, 63, 94, 0.16);
      --info-bg: rgba(167, 139, 250, 0.14);
    }
    * { box-sizing: border-box; }
    :focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: var(--fs-base);
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      overflow-x: hidden;
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
      z-index: 30;
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
      color: var(--btn-primary-fg);
      border-color: var(--accent);
      font-weight: 600;
    }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-success {
      background: var(--ok-btn);
      color: #ffffff;
      border-color: var(--ok-btn);
      font-weight: 600;
      box-shadow: 0 2px 4px rgba(21, 128, 61, 0.25);
    }
    .btn-success:hover { background: var(--ok-btn-hover); }
    .btn-warn {
      background: var(--warn-btn);
      color: #ffffff;
      border-color: var(--warn-btn);
      font-weight: 600;
    }
    .btn-warn:hover { background: var(--warn-btn-hover); }
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
      grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
      gap: var(--sp-6);
      padding: var(--sp-6) 28px;
      max-width: 1440px;
      margin: 0 auto;
    }
    @media (max-width: 1023px) {
      .layout {
        grid-template-columns: minmax(0, 1fr);
        padding: var(--sp-3);
        gap: var(--sp-3);
      }
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 22px;
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }
    .sidebar-new-btn {
      width: 100%;
      margin-top: 12px;
      padding: 10px 14px;
      font-weight: 600;
    }
    .project-card {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 16px;
      margin-bottom: 12px;
      cursor: pointer;
      background: var(--surface);
      transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease, background 0.18s ease;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
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
      border-radius: 14px;
      padding: 16px 18px;
      margin: 14px 0 18px;
      border: 1px solid var(--border);
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08);
      display: flex;
      flex-direction: column;
      gap: 12px;
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
    .doctor-recovery-card {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 8px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      font-size: 0.86rem;
    }
    .doctor-recovery-badges {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 6px;
    }
    .doctor-prompt-box {
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed var(--border);
    }
    .doctor-prompt-pre {
      margin: 6px 0;
      padding: 8px 10px;
      border-radius: 6px;
      background: var(--bg);
      border: 1px solid var(--border);
      font-size: 0.8rem;
      white-space: pre-wrap;
      word-break: break-word;
      color: var(--text);
    }
    @keyframes factory-pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.35); }
      50% { box-shadow: 0 0 0 6px rgba(56, 189, 248, 0); }
    }
    #improve-factory-btn.is-building {
      border-color: var(--accent, #38bdf8);
      color: var(--accent, #38bdf8);
      animation: factory-pulse 1.8s ease-in-out infinite;
      font-weight: 600;
    }
    .factory-progress-card {
      background: var(--surface-alt);
      border: 1px solid var(--accent, #38bdf8);
      border-radius: 8px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .factory-steps-bar {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
    }
    .factory-step-pill {
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 0.74rem;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--muted);
      text-align: center;
    }
    .factory-step-pill.is-active {
      border-color: var(--accent, #38bdf8);
      color: var(--text);
      background: rgba(56, 189, 248, 0.12);
    }
    .factory-step-pill.is-done {
      border-color: rgba(34, 197, 94, 0.5);
      color: #4ade80;
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
      gap: 8px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
      margin-bottom: 20px;
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
      background: var(--surface-alt);
      border-style: dashed;
    }
    .phase-card.phase-pending .phase-title,
    .phase-card.phase-pending .phase-meta-badge {
      color: var(--muted);
    }
    .phase-card.phase-pending .phase-circle {
      opacity: 0.55;
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
      display: block;
    }
    .logo-icon-btn {
      padding: 0;
      border: 0;
      background: transparent;
      border-radius: 10px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      position: relative;
      transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .logo-icon-btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 0 0 2px var(--accent);
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
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.25);
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
      list-style-type: decimal;
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
    body:not(.tech-mode) .tech-only { display: none; }
    body.tech-mode .simple-only { display: none; }
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
    #audit-chat-panel {
      display: flex;
      flex-direction: column;
      max-height: 68vh;
      padding: 16px;
      gap: 12px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
    }
    .audit-thread,
    #audit-chat-stream,
    .audit-chat-stream {
      flex: 1 1 auto;
      min-height: 150px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow-y: auto;
      padding: 4px 6px 8px 2px;
      margin: 0;
      background: transparent;
      border: none;
    }
    .audit-chat-composer {
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding-top: 12px;
      border-top: 1px solid var(--border);
      background: var(--surface-alt);
    }
    .audit-msg,
    .chat-msg {
      padding: 12px 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .audit-msg.user,
    .chat-msg.user {
      align-self: flex-end;
      max-width: 78%;
      border-color: rgba(56, 189, 248, 0.38);
      background: var(--info-bg);
      border-bottom-right-radius: 4px;
    }
    .audit-msg.assistant,
    .chat-msg.assistant {
      align-self: flex-start;
      width: 100%;
      max-width: 90%;
      background: var(--surface);
      border-color: var(--border);
      border-bottom-left-radius: 4px;
    }
    .audit-msg-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      font-size: 0.76rem;
      color: var(--muted);
      margin-bottom: 2px;
    }
    .audit-msg-body,
    .chat-msg.assistant .md-body {
      font-size: 0.9rem;
      line-height: 1.6;
      word-break: break-word;
    }
    .audit-msg.user .audit-msg-body {
      white-space: pre-wrap;
    }
    .md-file-ref {
      display: inline-block;
      padding: 1px 7px;
      border-radius: 6px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.82em;
      color: var(--accent);
      background: var(--info-bg);
      border: 1px solid rgba(56, 189, 248, 0.28);
      word-break: break-all;
    }
    .chat-msg-collapsible {
      position: relative;
    }
    .chat-msg-collapsible.collapsed {
      max-height: 170px;
      overflow: hidden;
    }
    .chat-msg-fade {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 54px;
      background: linear-gradient(to bottom, transparent, var(--surface));
      pointer-events: none;
    }
    .chat-collapse-btn {
      align-self: flex-start;
      background: transparent;
      border: 1px solid var(--border);
      color: var(--accent);
      font-size: var(--fs-xs);
      font-weight: 600;
      padding: 3px 10px;
      border-radius: 999px;
      cursor: pointer;
      margin-top: 2px;
    }
    .chat-collapse-btn:hover {
      background: var(--info-bg);
      border-color: var(--accent);
    }
    .audit-msg-actions,
    .chat-msg-footer {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin-top: 6px;
      padding-top: 6px;
      border-top: 1px solid var(--border);
    }
    .chat-action-link {
      background: transparent;
      border: none;
      color: var(--muted);
      font-size: var(--fs-xs);
      padding: 2px 4px;
      border-radius: 4px;
      cursor: pointer;
    }
    .chat-action-link:hover:not(:disabled) {
      color: var(--accent);
      background: var(--info-bg);
    }
    .chat-action-link:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .chat-action-sep {
      color: var(--muted);
      font-size: var(--fs-xs);
      user-select: none;
    }
    .versions-panel {
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
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: var(--sp-4);
      align-items: start;
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
      z-index: 1;
    }
    @media (min-width: 1280px) {
      body.has-live-banner .preview-card {
        top: 126px;
      }
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
      height: clamp(320px, 62vh, 720px);
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
    .themes-menu-wrap,
    .settings-menu-wrap,
    .more-menu-wrap {
      position: relative;
      display: inline-flex;
      align-items: center;
    }
    .themes-popover,
    .settings-popover,
    .more-popover {
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      min-width: 240px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      box-shadow: 0 12px 28px rgba(0, 0, 0, 0.38);
      display: flex;
      flex-direction: column;
      gap: 6px;
      z-index: 35;
    }
    .settings-popover {
      min-width: 268px;
      max-width: min(320px, calc(100vw - 24px));
      max-height: min(75vh, 520px);
      overflow-y: auto;
      padding: 12px;
      gap: 10px;
      z-index: 45;
    }
    .settings-popover .themes-menu-wrap {
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 6px;
      padding-top: 8px;
      border-top: 1px solid var(--border);
    }
    .settings-themes-trigger {
      width: 100%;
      justify-content: space-between;
    }
    .settings-popover .themes-popover {
      position: static;
      top: auto;
      right: auto;
      min-width: 0;
      width: 100%;
      padding: 6px;
      gap: 4px;
      background: var(--bg);
      box-shadow: none;
      z-index: auto;
    }
    .staging-banner-wrap {
      position: relative;
      display: inline-flex;
      align-items: center;
    }
    .staging-pill {
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 600;
      padding: 6px 12px;
      gap: 6px;
      white-space: nowrap;
    }
    .staging-pill.is-building {
      background: var(--info-bg);
      border-color: var(--accent);
      color: var(--accent);
    }
    .staging-pill.is-ready {
      background: var(--ok-bg);
      border-color: var(--ok);
      color: var(--ok);
    }
    .staging-pill.is-error {
      background: var(--err-bg);
      border-color: var(--err);
      color: var(--err);
    }
    .staging-popover {
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      min-width: 280px;
      max-width: min(340px, calc(100vw - 24px));
      max-height: min(60vh, 420px);
      overflow-y: auto;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      box-shadow: 0 12px 28px rgba(0, 0, 0, 0.38);
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 40;
    }
    .staging-popover-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.85rem;
    }
    .staging-popover-head-right {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .staging-popover-close {
      border: 1px solid transparent;
      background: transparent;
      color: var(--muted);
      border-radius: 6px;
      width: 24px;
      height: 24px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 0.85rem;
      line-height: 1;
    }
    .staging-popover-close:hover {
      background: var(--surface-alt);
      border-color: var(--border);
      color: var(--text);
    }
    .staging-commits-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 220px;
      overflow-y: auto;
    }
    .staging-commit-item {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      padding: 6px 8px;
      border-radius: 7px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      font-size: 0.82rem;
      line-height: 1.35;
      color: var(--text);
    }
    .staging-commit-hash {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.74rem;
      color: var(--muted);
      flex-shrink: 0;
    }
    .staging-commit-title {
      word-break: break-word;
      overflow-wrap: anywhere;
    }
    .staging-popover-error {
      padding: 8px 10px;
      border-radius: 8px;
      background: var(--err-bg);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: var(--err);
      font-size: 0.8rem;
      line-height: 1.4;
      word-break: break-word;
    }
    .staging-popover-actions {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding-top: 6px;
      border-top: 1px solid var(--border);
    }
    .staging-promote-status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.8rem;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .staging-live-clock {
      font-variant-numeric: tabular-nums;
      font-weight: 600;
      color: var(--accent);
    }
    .factory-staging-notice {
      margin-top: 8px;
      padding: 8px 12px;
      border-radius: 10px;
      background: var(--ok-bg);
      border: 1px solid rgba(34, 197, 94, 0.35);
      color: var(--ok);
      font-size: 0.84rem;
      font-weight: 500;
      line-height: 1.4;
    }
    .factory-staging-notice.is-error {
      background: var(--err-bg);
      border-color: rgba(239, 68, 68, 0.35);
      color: var(--err);
    }
    .liwi-radar-section {
      margin-top: 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      box-sizing: border-box;
      min-width: 0;
    }
    .liwi-radar-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }
    .liwi-radar-title {
      margin: 0;
      font-size: 0.88rem;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.01em;
    }
    .liwi-radar-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }
    .liwi-radar-select {
      background: var(--surface-alt);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 5px 8px;
      font-size: 0.78rem;
      font-family: inherit;
      cursor: pointer;
    }
    .liwi-radar-select:focus {
      outline: 2px solid var(--accent);
      outline-offset: 1px;
    }
    .liwi-radar-live-status {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      border-radius: 8px;
      background: var(--surface-alt);
      border: 1px solid rgba(148, 163, 184, 0.18);
      font-size: 0.8rem;
      color: var(--muted);
    }
    .liwi-radar-live-status.is-ok {
      color: var(--ok);
      background: var(--ok-bg);
      border-color: rgba(34, 197, 94, 0.32);
    }
    .liwi-radar-live-status.is-error {
      color: var(--err);
      background: var(--err-bg);
      border-color: rgba(239, 68, 68, 0.35);
    }
    .liwi-radar-clock {
      margin-left: auto;
      font-variant-numeric: tabular-nums;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.76rem;
      font-weight: 600;
      color: var(--accent);
    }
    .liwi-radar-empty {
      font-size: 0.82rem;
      color: var(--muted);
      text-align: center;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px dashed rgba(148, 163, 184, 0.24);
      line-height: 1.45;
    }
    .liwi-radar-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      min-width: 0;
    }
    .liwi-radar-card {
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-width: 0;
      box-sizing: border-box;
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .liwi-radar-card:hover,
    .liwi-radar-card.is-expanded {
      border-color: var(--accent);
    }
    .liwi-radar-card-head {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      min-width: 0;
    }
    .liwi-radar-card-icon {
      flex-shrink: 0;
      font-size: 1rem;
      line-height: 1.3;
    }
    .liwi-radar-card-main {
      flex: 1 1 auto;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .liwi-radar-card-title {
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--text);
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .liwi-radar-card-meta {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .liwi-radar-impact {
      display: inline-flex;
      align-items: center;
      font-size: 0.7rem;
      font-weight: 600;
      padding: 1px 7px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--muted);
    }
    .liwi-radar-impact.is-high {
      color: var(--warn);
      background: var(--warn-bg);
      border-color: rgba(245, 158, 11, 0.35);
    }
    .liwi-radar-impact.is-medium {
      color: var(--accent);
      background: var(--info-bg);
      border-color: rgba(56, 189, 248, 0.32);
    }
    .liwi-radar-status-pill {
      display: inline-flex;
      align-items: center;
      font-size: 0.7rem;
      font-weight: 600;
      padding: 1px 7px;
      border-radius: 999px;
      color: var(--ok);
      background: var(--ok-bg);
      border: 1px solid rgba(34, 197, 94, 0.32);
    }
    .liwi-radar-status-pill.is-implementing {
      color: var(--accent);
      background: var(--info-bg);
      border-color: rgba(56, 189, 248, 0.35);
    }
    .liwi-radar-status-pill.is-dismissed {
      color: var(--muted);
      background: var(--surface);
      border-color: var(--border);
    }
    .liwi-radar-filters {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      min-width: 0;
    }
    .liwi-radar-filter-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: border-color 0.15s ease, background 0.15s ease, color 0.15s ease;
    }
    .liwi-radar-filter-btn:hover {
      border-color: var(--accent);
      color: var(--text);
    }
    .liwi-radar-filter-btn.active {
      border-color: var(--accent);
      background: var(--info-bg);
      color: var(--text);
    }
    .liwi-radar-filter-count {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border-radius: 999px;
      font-size: 0.68rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      background: var(--surface);
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .liwi-radar-filter-btn.active .liwi-radar-filter-count {
      background: var(--accent);
      color: var(--btn-primary-fg);
      border-color: transparent;
    }
    .liwi-radar-card-status-note {
      font-size: 0.76rem;
      color: var(--muted);
      font-weight: 500;
    }
    .liwi-radar-card-preview {
      margin: 0;
      font-size: 0.79rem;
      line-height: 1.45;
      color: var(--muted);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .liwi-radar-card.is-expanded .liwi-radar-card-preview {
      display: none;
    }
    .liwi-radar-card-details {
      display: none;
      flex-direction: column;
      gap: 10px;
      padding-top: 8px;
      border-top: 1px solid rgba(148, 163, 184, 0.16);
    }
    .liwi-radar-card.is-expanded .liwi-radar-card-details {
      display: flex;
    }
    .liwi-radar-card-desc {
      margin: 0;
      font-size: 0.81rem;
      line-height: 1.5;
      color: var(--muted);
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .liwi-radar-card-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .liwi-radar-card-feedback {
      font-size: 0.76rem;
      color: var(--muted);
      line-height: 1.4;
    }
    .liwi-radar-card-feedback.is-error {
      color: var(--err);
    }
    .auto-ideas-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      margin-left: 5px;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 700;
      line-height: 1;
      background: var(--accent);
      color: var(--btn-primary-fg);
    }
    .liwi-radar-section.is-radar-focused {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.25);
    }
    .theme-option,
    .more-menu-item {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border: 1px solid transparent;
      border-radius: 8px;
      background: transparent;
      color: var(--text);
      font-size: 0.85rem;
      font-weight: 500;
      text-align: left;
      cursor: pointer;
      text-decoration: none;
    }
    .theme-option:hover,
    .more-menu-item:hover {
      background: var(--surface-alt);
      border-color: var(--border);
    }
    .theme-option.active {
      background: var(--info-bg);
      border-color: var(--accent);
      color: var(--accent);
      font-weight: 600;
    }
    .theme-swatch {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      border: 1px solid var(--border);
      flex-shrink: 0;
    }
    .more-menu-item.danger {
      color: var(--err);
    }
    .more-menu-item.danger:hover {
      background: var(--err-bg);
      border-color: rgba(239, 68, 68, 0.35);
    }
    .guidance-banner.delivered-compact {
      flex-direction: row;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      padding: 14px 16px;
    }
    .delivered-compact-info {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 200px;
      flex: 1 1 240px;
    }
    .delivered-compact-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .audit-unified-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
      margin-bottom: 16px;
    }
    .audit-unified-bar .audit-subtabs {
      border-bottom: 0;
      padding-bottom: 0;
      margin-bottom: 0;
    }
    .audit-primary-split {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      position: relative;
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
      position: sticky;
      top: 56px;
      z-index: 20;
    }
    .live-activity-banner {
      padding: 10px 16px;
      background: linear-gradient(var(--info-bg), var(--info-bg)), var(--surface);
      -webkit-backdrop-filter: blur(10px);
      backdrop-filter: blur(10px);
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }
    .live-activity-banner.stall {
      background: linear-gradient(var(--warn-bg), var(--warn-bg)), var(--surface);
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
    .modal-card {
      max-height: 100dvh;
      overflow: auto;
    }
    .mobile-switcher-row {
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: var(--sp-2);
      min-width: 0;
    }
    .mobile-switcher-btn,
    #mobile-project-switcher {
      display: block;
      flex: 1 1 auto;
      min-width: 0;
      max-width: 100%;
      text-align: left;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      line-height: 1.6;
    }
    .badge-short { display: none; }
    .example-prompts {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin-bottom: 8px;
    }
    .example-chip {
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: var(--fs-xs);
      cursor: pointer;
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .example-chip:hover {
      border-color: var(--accent);
      background: var(--info-bg);
    }
    .toast-stack {
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 70;
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-width: 360px;
      width: calc(100vw - 32px);
      pointer-events: none;
    }
    .toast-item {
      pointer-events: auto;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-left-width: 4px;
      color: var(--text);
      font-size: var(--fs-sm);
      box-shadow: 0 10px 26px rgba(0, 0, 0, 0.35);
    }
    .toast-item.toast-ok { border-left-color: var(--ok); }
    .toast-item.toast-err { border-left-color: var(--err); }
    .toast-item.toast-info { border-left-color: var(--accent); }
    .toast-close {
      background: transparent;
      border: 0;
      color: var(--muted);
      cursor: pointer;
      font-size: 1rem;
      line-height: 1;
      padding: 2px 4px;
    }
    .inline-error-box {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 8px;
      padding: 8px 12px;
      border-radius: 8px;
      background: var(--err-bg);
      border: 1px solid rgba(239, 68, 68, 0.4);
      color: var(--err);
      font-size: var(--fs-sm);
    }
    .optimistic-sent-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 8px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: var(--fs-sm);
      font-weight: 600;
      color: var(--ok);
      background: var(--ok-bg);
      border: 1px solid rgba(34, 197, 94, 0.35);
    }
    .flash-saved {
      outline: 2px solid var(--ok);
      outline-offset: 2px;
      border-radius: 6px;
    }
    .simple-rollback-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-alt);
    }
    .settings-feedback {
      font-size: var(--fs-xs);
      color: var(--ok);
      font-weight: 600;
    }
    .copy-msg-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      padding: 3px 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--muted);
      font-family: inherit;
      font-size: 0.74rem;
      font-weight: 500;
      line-height: 1.2;
      white-space: nowrap;
      flex-shrink: 0;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
    }
    .copy-msg-btn:hover {
      background: var(--surface-alt);
      color: var(--text);
      border-color: var(--accent);
    }
    .copy-msg-btn:active {
      transform: translateY(1px);
    }
    .copy-msg-btn.is-copied {
      color: var(--ok);
      border-color: var(--ok);
      background: var(--ok-bg);
    }
    @media (max-width: 1279px) {
      .detail-work-split.preview-open {
        display: block;
      }
      .detail-work-split.preview-open .preview-card {
        position: fixed;
        inset: 0;
        top: 0;
        z-index: 60;
        border-radius: 0;
        border: 0;
        height: 100dvh;
        max-height: 100dvh;
      }
      .detail-work-split.preview-open #live-preview-iframe {
        flex: 1;
        height: auto;
        min-height: 0;
      }
    }
    @media (max-width: 1023px) {
      .mobile-switcher-row { display: flex; }
      #sidebar-panel.sidebar-collapsed .panel-header,
      #sidebar-panel.sidebar-collapsed #sidebar-projects-body {
        display: none;
      }
      #sidebar-panel.sidebar-collapsed .mobile-switcher-row {
        margin-bottom: 0;
      }
    }
    @media (max-width: 768px) {
      *,
      *::before,
      *::after {
        box-sizing: border-box;
      }
      html,
      body {
        max-width: 100vw;
        overflow-x: hidden;
        box-sizing: border-box;
      }
      main.layout,
      .layout,
      header,
      .panel,
      #sidebar-panel,
      #detail-panel,
      #detail-content,
      #detail-work-split,
      #detail-main-col,
      .live-activity-shell,
      .live-activity-banner,
      .live-activity-main,
      .live-activity-stall,
      .panel-header,
      .project-card,
      .card-header,
      .card-title-wrap,
      .model-usage-row,
      .stage-segments,
      .detail-header,
      .detail-titles,
      .title-row,
      .detail-header-status,
      .banner-content,
      .banner-body,
      .banner-actions,
      .doctor-recovery-card,
      .doctor-recovery-badges,
      .factory-progress-card,
      .factory-steps-bar,
      .secondary-actions,
      .tab-content,
      .plan-meta,
      .file-card,
      .timeline-header,
      .timeline-title-group,
      .phase-card,
      .phase-summary,
      .phase-summary-left,
      .edit-title-form,
      .input-inline,
      .local-controls-row,
      .modify-box,
      .modify-panel-card,
      .new-project-form,
      .new-project-actions,
      .history-toolbar,
      .round-card,
      .round-meta,
      .file-chips,
      .input-row-with-mic,
      .modal-card,
      .audit-toolbar,
      #audit-chat-panel,
      .audit-thread,
      #audit-chat-stream,
      .audit-chat-stream,
      .audit-chat-composer,
      .audit-msg,
      .chat-msg,
      .audit-msg-head,
      .audit-msg-actions,
      .chat-msg-footer,
      .versions-panel,
      .versions-row,
      .versions-row > select,
      .preview-card,
      .preview-toolbar,
      .example-prompts,
      .toast-item,
      .inline-error-box,
      .simple-rollback-bar,
      .audit-filters,
      .audit-card,
      .audit-card-head,
      .audit-reader,
      .audit-reader-bar,
      .audit-toc,
      .quality-scorecard,
      .quality-scorecard-head,
      .quality-bars-grid,
      .quality-bar-item,
      .quality-bar-label,
      .md-code-wrap,
      .md-table-wrap,
      .target-picker,
      .target-chips,
      .file-check-row,
      .diff-file-card,
      .diff-file-head,
      .version-card,
      .version-head,
      .version-actions,
      .history-versions-section,
      .history-versions-header,
      .version-quality-row,
      .version-quality-bars,
      .factory-chat-modal,
      .factory-chat-head,
      .factory-chat-head-left,
      .factory-chat-meta-col,
      .factory-memory-panel,
      .factory-memory-item,
      .factory-chat-stream,
      .factory-msg,
      .factory-pre-wrap,
      .factory-proposal-card,
      .factory-composer-wrap,
      .factory-composer-inner,
      .factory-composer-capsule,
      .factory-paste-previews,
      .factory-composer-row,
      .factory-fs-card,
      .autonomous-controls-wrap,
      .liwi-radar-controls,
      .liwi-radar-subtabs,
      .liwi-radar-filters,
      .liwi-history-card,
      .liwi-history-head,
      .proj-ideas-head,
      .proj-ideas-grid,
      .proj-idea-card {
        min-width: 0;
        max-width: 100%;
        box-sizing: border-box;
      }
      code,
      .md-body code,
      .md-code-wrap pre code,
      .file-path,
      .file-chip,
      .md-file-ref,
      .audit-card-diag,
      .doctor-recovery-card,
      .inline-error-box,
      .audit-msg-body,
      .factory-fs-detail,
      #live-activity-subtext,
      #factory-build-banner-detail,
      #factory-progress-detail {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
        max-width: 100%;
      }
      .md-body,
      .banner-desc,
      .banner-subdesc,
      .audit-card-excerpt,
      .spec-section p,
      .spec-section li,
      .plan-summary,
      .round-summary,
      .timeline-next,
      .factory-msg-user,
      .factory-msg-assistant,
      .version-polish-note,
      .toast-item,
      .readonly-mod-notice,
      .inactivity-note {
        overflow-wrap: anywhere;
        word-break: break-word;
        max-width: 100%;
      }
      pre,
      details.tech-details pre,
      .doctor-prompt-pre,
      .diff-pre,
      .md-code-wrap pre,
      .factory-msg-body pre,
      .factory-pre-wrap pre,
      .factory-proposal-card .doctor-prompt-pre {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
        max-width: 100%;
        overflow-x: auto;
      }
      header {
        width: 100%;
        padding: 10px 12px;
        gap: 8px;
      }
      header .brand {
        width: 100%;
        min-width: 0;
        max-width: 100%;
      }
      header .controls {
        display: flex;
        flex-wrap: nowrap;
        align-items: center;
        gap: 6px;
        width: 100%;
        max-width: 100%;
        min-width: 0;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
        padding-bottom: 2px;
      }
      header .controls .segmented-control,
      header .controls .themes-menu-wrap,
      header .controls .settings-menu-wrap {
        flex-shrink: 0;
        display: inline-flex;
        align-items: center;
      }
      header .controls > button,
      header .controls > div > button {
        min-height: 38px;
        white-space: nowrap;
        flex-shrink: 0;
        font-size: 0.84rem;
        padding: 6px 10px;
      }
      header .controls .btn,
      header .controls .seg-btn,
      #view-mode-simple,
      #view-mode-dev,
      #themes-menu-btn,
      #auto-ideas-btn,
      #improve-factory-btn,
      #settings-menu-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        white-space: nowrap;
        flex-shrink: 0;
        min-height: 38px;
        padding: 5px 9px;
        font-size: 0.78rem;
        line-height: 1.2;
      }
      header .themes-popover,
      header .settings-popover {
        position: fixed;
        top: 108px;
        right: 12px;
        left: auto;
        max-width: calc(100vw - 24px);
        z-index: 80;
      }
      header .settings-popover {
        max-height: calc(100vh - 124px);
        overflow-y: auto;
      }
      header .settings-popover .themes-menu-wrap {
        width: 100%;
        flex-direction: column;
        align-items: stretch;
      }
      header .settings-popover #themes-menu-btn {
        width: 100%;
        justify-content: space-between;
      }
      header .settings-popover .themes-popover {
        position: static;
        top: auto;
        right: auto;
        left: auto;
        min-width: 0;
        width: 100%;
        max-width: 100%;
        box-shadow: none;
        z-index: auto;
      }
      header .controls .staging-banner-wrap {
        flex-shrink: 0;
        display: inline-flex;
        align-items: center;
      }
      #staging-banner-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        white-space: nowrap;
        flex-shrink: 0;
        min-height: 38px;
        padding: 5px 10px;
        font-size: 0.78rem;
        line-height: 1.2;
      }
      header .staging-popover {
        position: fixed;
        top: 108px;
        right: 12px;
        left: 12px;
        min-width: 0;
        max-width: calc(100vw - 24px);
        max-height: calc(100vh - 124px);
        overflow-y: auto;
        z-index: 85;
      }
      .staging-popover .staging-commits-list {
        max-height: 40vh;
      }
      #staging-promote-btn {
        width: 100%;
        min-height: 40px;
      }
      .mobile-switcher-row {
        display: flex;
        flex-wrap: nowrap;
        align-items: center;
        gap: 8px;
        width: 100%;
        max-width: 100%;
        min-width: 0;
      }
      .mobile-switcher-row .mobile-switcher-btn,
      .mobile-switcher-row #mobile-project-switcher,
      .mobile-switcher-row select {
        flex: 1;
        min-width: 0;
        max-width: 100%;
        min-height: 38px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      #mobile-new-project-shortcut {
        white-space: nowrap;
        flex-shrink: 0;
        min-height: 38px;
      }
      #factory-chat-stream .copy-msg-btn,
      .copy-msg-btn {
        min-height: 36px;
        padding: 6px 10px;
        font-size: 0.78rem;
        touch-action: manipulation;
        max-width: 100%;
      }
      #factory-chat-stream .factory-msg-head,
      #factory-chat-stream .factory-proposal-head,
      #factory-chat-stream .factory-pre-bar {
        max-width: 100%;
        min-width: 0;
        flex-wrap: wrap;
      }
      #detail-panel,
      #detail-main-col {
        max-width: 100%;
        min-width: 0;
        box-sizing: border-box;
      }
      .detail-header-group {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        max-width: 100%;
        min-width: 0;
        margin-bottom: 10px;
      }
      .detail-header-group::before {
        content: "";
        flex: 0 0 100%;
        height: 0;
        order: 5;
      }
      .detail-header-group .detail-header,
      .detail-header-group .detail-titles,
      .detail-header-group .title-row {
        display: contents;
      }
      #change-logo-btn {
        order: 1;
        flex: 0 0 auto;
      }
      #detail-title,
      .detail-title {
        order: 2;
        flex: 1 1 150px;
        min-width: 0;
        font-size: 1.25rem;
        line-height: 1.25;
        overflow-wrap: anywhere;
        word-break: break-word;
      }
      #detail-panel #detail-title,
      .detail-header-group .detail-title,
      .project-card .card-title {
        font-size: 1.2rem;
        line-height: 1.25;
        overflow-wrap: anywhere;
        word-break: break-word;
      }
      .detail-header-status {
        order: 3;
        flex: 0 0 auto;
        max-width: 100%;
        margin-top: 2px;
      }
      .project-card .card-header,
      .detail-header-status,
      .model-usage-row,
      .plan-meta,
      .round-meta,
      .timeline-title-group {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
      }
      #edit-title-form {
        order: 4;
        flex: 1 0 100%;
        max-width: 100%;
        min-width: 0;
        margin-top: 8px;
      }
      #edit-title-form .input-inline {
        min-width: 0;
      }
      #edit-title-btn {
        order: 6;
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        margin: 0;
        padding: 4px 8px;
      }
      #detail-time {
        order: 7;
        flex: 0 1 auto;
        min-width: 0;
        margin: 0 0 0 auto !important;
        text-align: right;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      #detail-segments {
        order: 8;
        flex: 1 0 100%;
        width: 100%;
        margin: 4px 0 2px 0;
      }
      .guidance-banner,
      .guidance-banner.delivered-compact,
      .delivered-compact-info,
      .delivered-compact-actions,
      .local-controls-row {
        max-width: 100%;
        min-width: 0;
        box-sizing: border-box;
      }
      .guidance-banner.banner-err,
      .guidance-banner.banner-err .banner-content,
      .guidance-banner.banner-err .banner-body,
      .guidance-banner.banner-err .banner-desc,
      .doctor-recovery-card,
      .doctor-recovery-card .round-summary,
      .doctor-prompt-box {
        max-width: 100%;
        min-width: 0;
        overflow-wrap: anywhere;
        word-break: break-word;
        box-sizing: border-box;
      }
      .doctor-recovery-badges {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        max-width: 100%;
        min-width: 0;
      }
      .doctor-recovery-badges .model-pill,
      .doctor-recovery-badges .test-badge {
        max-width: 100%;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: break-word;
      }
      .banner-actions {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        max-width: 100%;
        min-width: 0;
        box-sizing: border-box;
      }
      .guidance-banner.delivered-compact {
        padding: 12px 14px;
        gap: 10px;
      }
      .delivered-compact-info {
        flex: 1 1 100%;
        min-width: 0;
      }
      .delivered-compact-actions {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        width: 100%;
      }
      .local-controls-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        flex: 1 1 auto;
      }
      .delivered-compact-actions .btn,
      .local-controls-row .btn,
      #project-more-btn,
      #header-more-btn,
      .banner-actions .btn,
      .doctor-prompt-box .btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        white-space: nowrap;
        min-height: 38px;
        padding: 7px 12px;
        font-size: 0.84rem;
        line-height: 1.2;
        max-width: 100%;
      }
      .more-popover {
        max-width: calc(100vw - 32px);
      }
      .tabs {
        display: flex;
        flex-wrap: nowrap;
        align-items: center;
        gap: 6px;
        max-width: 100%;
        min-width: 0;
        overflow-x: auto;
        white-space: nowrap;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
      }
      .tab-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        white-space: nowrap;
        min-height: 38px;
        padding: 6px 11px;
        font-size: 0.84rem;
        line-height: 1.2;
      }
      .audit-unified-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        max-width: 100%;
        min-width: 0;
      }
      .audit-subtabs {
        display: flex;
        flex-wrap: nowrap;
        align-items: center;
        gap: 6px;
        max-width: 100%;
        min-width: 0;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
      }
      .audit-subtab {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        white-space: nowrap;
        min-height: 38px;
        padding: 5px 10px;
        font-size: 0.8rem;
        line-height: 1.2;
      }
      .audit-primary-split {
        display: inline-flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 4px;
        max-width: 100%;
        min-width: 0;
      }
      .audit-primary-split .btn,
      #audit-quick-audit-btn,
      #audit-more-actions-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        white-space: nowrap;
        flex-shrink: 0;
        min-height: 38px;
        padding: 6px 11px;
        font-size: 0.82rem;
        line-height: 1.2;
        max-width: 100%;
      }
      #audit-more-actions-btn {
        padding: 6px 10px;
      }
      input,
      textarea,
      select {
        font-size: 16px;
      }
      input.input-field,
      textarea.input-field,
      select.input-field,
      input.input-inline {
        font-size: 16px;
      }
      .liwi-radar-section {
        padding: 10px 12px;
      }
      .liwi-radar-head {
        flex-direction: column;
        align-items: stretch;
        gap: 8px;
      }
      .liwi-radar-controls {
        width: 100%;
        flex-wrap: wrap;
        justify-content: space-between;
      }
      .liwi-radar-select,
      .liwi-radar-gen-btn {
        min-height: 38px;
        flex: 1 1 auto;
        max-width: 100%;
      }
      .liwi-radar-grid {
        grid-template-columns: 1fr;
        gap: 12px;
      }
      .liwi-radar-filters {
        flex-wrap: nowrap;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        padding-bottom: 2px;
      }
      .liwi-radar-filter-btn {
        flex-shrink: 0;
        white-space: nowrap;
        min-height: 34px;
      }
      .liwi-radar-card-actions {
        width: 100%;
      }
      .liwi-radar-card-actions .btn {
        flex: 1 1 auto;
        min-height: 36px;
      }
    }
    @media (max-width: 600px) {
      .layout {
        padding-bottom: 92px;
      }
      header {
        padding: 10px 14px;
        gap: 8px;
      }
      .brand { font-size: 1rem; gap: 8px; }
      .panel { padding: 14px; }
      .detail-header { flex-wrap: wrap; }
      .badge-full { display: none; }
      .badge-short { display: inline; }
      .tabs {
        overflow-x: auto;
        white-space: nowrap;
        flex-wrap: nowrap;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
      }
      .tab-btn { flex: 0 0 auto; }
      .modal-backdrop {
        padding: 0;
        align-items: flex-end;
      }
      .modal-card {
        max-width: 100%;
        border-radius: 14px 14px 0 0;
      }
      .live-activity-shell {
        position: fixed;
        bottom: 12px;
        left: 12px;
        right: 12px;
        top: auto;
        margin: 0;
        padding: 0;
        z-index: 45;
      }
      .live-activity-banner {
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
        cursor: pointer;
      }
      .live-activity-banner:not(.expanded):not(.stall) #live-activity-subtext {
        display: none;
      }
      .toast-stack {
        right: auto;
        left: 50%;
        transform: translateX(-50%);
        bottom: 72px;
      }
    }
    @media (pointer: coarse) {
      .btn,
      .btn-sm,
      .btn-icon,
      .seg-btn,
      .tab-btn,
      .settings-item,
      .example-chip,
      .mobile-switcher-btn {
        min-height: 44px;
      }
      .btn-icon { min-width: 44px; }
      input[type="checkbox"] {
        width: 22px;
        height: 22px;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      *,
      *::before,
      *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
      .project-card:hover { transform: none; }
    }
    /* B4: Audit Index & Safe Markdown Reader */
    .audit-subtabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 12px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }
    .audit-subtab {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 5px 12px;
      border-radius: 999px;
      font-size: var(--fs-sm);
      font-weight: 600;
      cursor: pointer;
    }
    .audit-subtab.active {
      background: var(--info-bg);
      color: var(--accent);
      border-color: var(--accent);
    }
    .audit-filters {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .audit-filter-btn {
      background: var(--surface-alt);
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: var(--fs-xs);
      cursor: pointer;
    }
    .audit-filter-btn.active {
      background: var(--accent);
      color: var(--btn-primary-fg);
      border-color: var(--accent);
      font-weight: 600;
    }
    #audit-index {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .audit-card {
      border: 1px solid var(--border);
      background: var(--surface-alt);
      border-radius: 12px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
      transition: border-color 0.16s ease;
    }
    .audit-card:hover {
      border-color: rgba(56, 189, 248, 0.32);
    }
    .audit-card-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .audit-card-excerpt {
      font-size: var(--fs-sm);
      color: var(--muted);
      line-height: 1.45;
    }
    .audit-card-diag {
      font-size: var(--fs-xs);
      line-height: 1.45;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px solid rgba(245, 158, 11, 0.4);
      background: var(--warn-bg);
      color: var(--text);
      word-break: break-word;
    }
    .audit-card-diag.diag-err {
      border-color: rgba(239, 68, 68, 0.45);
      background: var(--err-bg);
    }
    .audit-card-diag.diag-live {
      border-color: rgba(56, 189, 248, 0.4);
      background: var(--info-bg);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .audit-reader {
      border: 1px solid var(--border);
      background: var(--surface-alt);
      border-radius: 10px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .audit-reader-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .audit-toc {
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: var(--fs-xs);
    }
    .audit-toc ul {
      margin: 4px 0 0 0;
      padding-left: 16px;
    }
    .audit-toc a {
      color: var(--accent);
      cursor: pointer;
    }
    .quality-scorecard {
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 10px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin: 4px 0;
    }
    .quality-scorecard-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: var(--fs-sm);
      font-weight: 700;
      color: var(--text);
    }
    .quality-bars-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 8px 12px;
    }
    .quality-bar-item {
      display: flex;
      flex-direction: column;
      gap: 3px;
    }
    .quality-bar-label {
      display: flex;
      justify-content: space-between;
      font-size: var(--fs-xs);
      color: var(--muted);
    }
    .quality-bar-track {
      height: 7px;
      width: 100%;
      border-radius: 999px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      overflow: hidden;
    }
    .quality-bar-fill {
      height: 100%;
      border-radius: 999px;
      background: var(--ok);
    }
    .quality-bar-fill.warn { background: var(--accent); }
    .quality-bar-fill.fail { background: var(--err); }
    .quality-bar-fill.na { background: var(--border); }
    .readonly-mod-notice {
      padding: 8px 12px;
      border-radius: 8px;
      background: var(--warn-bg);
      border: 1px solid rgba(245, 158, 11, 0.4);
      color: var(--warn);
      font-size: var(--fs-sm);
      font-weight: 600;
      margin-bottom: 10px;
    }
    .md-body {
      font-size: var(--fs-base);
      line-height: 1.55;
      word-break: break-word;
    }
    .md-body h1, .md-body h2, .md-body h3, .md-body h4, .md-body h5, .md-body h6 {
      margin: 12px 0 6px 0;
      color: var(--text);
      line-height: 1.3;
    }
    .md-body h1 { font-size: 1.15rem; }
    .md-body h2 { font-size: 1.02rem; }
    .md-body h3 { font-size: 0.95rem; }
    .md-body h4, .md-body h5, .md-body h6 { font-size: 0.88rem; }
    .md-body p { margin: 6px 0; }
    .md-body ul, .md-body ol { margin: 6px 0; padding-left: 20px; }
    .md-body blockquote {
      margin: 8px 0;
      padding: 6px 12px;
      border-left: 3px solid var(--accent);
      background: var(--surface);
      color: var(--muted);
    }
    .md-body code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.82rem;
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--surface);
      border: 1px solid var(--border);
    }
    .md-code-wrap {
      margin: 8px 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--bg);
    }
    .md-code-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 4px 10px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      font-size: var(--fs-xs);
      color: var(--muted);
    }
    .md-code-wrap pre {
      margin: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      white-space: pre;
      overflow-x: auto;
      word-break: normal;
    }
    .md-code-wrap pre code {
      border: 0;
      background: transparent;
      padding: 0;
    }
    .md-table-wrap { overflow-x: auto; margin: 8px 0; }
    .md-table {
      width: 100%;
      border-collapse: collapse;
      font-size: var(--fs-sm);
    }
    .md-table th, .md-table td {
      border: 1px solid var(--border);
      padding: 6px 8px;
      text-align: left;
    }
    .md-table th { background: var(--surface); }
    /* B5: Developer Ergonomics (File Picker, Diff Viewer, Versions Timeline) */
    .target-picker {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: 8px;
    }
    .target-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .target-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--info-bg);
      border: 1px solid rgba(56, 189, 248, 0.35);
      color: var(--accent);
      font-size: var(--fs-xs);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .target-chip button {
      background: transparent;
      border: 0;
      color: inherit;
      cursor: pointer;
      padding: 0 2px;
      font-weight: 700;
    }
    .target-checklist {
      max-height: 220px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg);
      padding: 8px;
    }
    .target-folder-group { margin-bottom: 8px; }
    .target-folder-title {
      font-size: var(--fs-xs);
      font-weight: 700;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .file-check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 3px 4px;
      font-size: var(--fs-xs);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      cursor: pointer;
    }
    .file-check-row:hover { background: var(--surface); border-radius: 4px; }
    .diff-file-card {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg);
      margin-top: 8px;
      overflow: hidden;
    }
    .diff-file-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      padding: 7px 10px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      font-size: var(--fs-xs);
    }
    .diff-stats {
      display: inline-flex;
      gap: 6px;
      align-items: center;
    }
    .diff-add-stat { color: var(--ok); font-weight: 700; }
    .diff-del-stat { color: var(--err); font-weight: 700; }
    .diff-lines-scroll {
      overflow-x: auto;
      max-height: 280px;
      overflow-y: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.77rem;
      line-height: 1.45;
      padding: 6px 0;
    }
    .diff-line {
      white-space: pre;
      padding: 0 10px;
      min-width: max-content;
    }
    .diff-line-add {
      background: rgba(34, 197, 94, 0.14);
      color: var(--ok);
    }
    .diff-line-del {
      background: rgba(239, 68, 68, 0.14);
      color: var(--err);
    }
    .diff-line-hunk {
      background: var(--info-bg);
      color: var(--accent);
      font-weight: 600;
    }
    #versions-timeline {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 6px 0 6px 14px;
      border-left: 2px solid var(--border);
      margin-top: 8px;
    }
    .version-node {
      position: relative;
      padding-left: 10px;
    }
    .version-dot {
      position: absolute;
      left: -21px;
      top: 10px;
      width: 11px;
      height: 11px;
      border-radius: 50%;
      background: var(--border);
      border: 2px solid var(--surface);
    }
    .version-node.current .version-dot { background: var(--ok); }
    .version-card {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface-alt);
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }
    .version-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .version-actions {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }
    .badge-muted {
      background: var(--surface-alt);
      color: var(--muted);
      border-color: var(--border);
    }
    .badge-excellence {
      background: var(--info-bg);
      color: var(--accent);
      border-color: var(--accent);
    }
    .history-versions-section {
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 12px;
      padding: 14px 16px;
      margin-bottom: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .history-versions-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .history-versions-header h4 {
      margin: 0;
      font-size: var(--fs-md);
      font-weight: 700;
      color: var(--text);
    }
    .history-versions-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-left: 14px;
      border-left: 2px solid var(--border);
    }
    .version-card.current-version {
      border-color: var(--ok);
      box-shadow: 0 0 0 1px var(--ok-bg);
    }
    .version-quality-row {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .version-quality-bars {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px 12px;
      padding: 6px 0 2px;
    }
    @media (max-width: 720px) {
      .version-quality-bars {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .version-sha-wrap {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .version-polish-note {
      font-size: var(--fs-xs);
      color: var(--text);
      background: var(--info-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
      line-height: 1.4;
    }
    /* Minimalist Liwi Chat ("Mejorar Fábrica") */
    #factory-improve-modal {
      padding: 12px;
      background: rgba(2, 6, 23, 0.8);
      backdrop-filter: blur(6px);
    }
    #factory-improve-modal .factory-chat-modal {
      width: 96vw;
      max-width: 1320px;
      height: 94vh;
      max-height: 94dvh;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 0;
      overflow: hidden;
      overflow-x: hidden;
      box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
    }
    .factory-chat-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px 24px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.14);
      background: var(--bg);
      flex-shrink: 0;
      min-width: 0;
    }
    .factory-chat-head-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex-wrap: wrap;
    }
    .factory-chat-title {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--text);
    }
    .factory-close-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      border-radius: 9999px;
      padding: 6px 12px;
      font-size: 0.84rem;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
    }
    .factory-close-btn:hover {
      background: var(--surface-alt);
      color: var(--text);
      border-color: var(--border);
    }
    .factory-chat-meta-col {
      max-width: 800px;
      width: 100%;
      margin: 0 auto;
      padding: 0 20px;
      box-sizing: border-box;
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    #factory-improve-modal[data-view="radar"] .factory-chat-modal {
      overflow-y: auto;
      scrollbar-width: thin;
    }
    #factory-improve-modal[data-view="chat"] .radar-only-ctrl {
      display: none !important;
    }
    #factory-improve-modal[data-view="radar"] .chat-only-ctrl {
      display: none !important;
    }
    #factory-improve-modal[data-view="radar"] #factory-chat-stream,
    #factory-improve-modal[data-view="radar"] .factory-composer-wrap,
    #factory-improve-modal[data-view="radar"] #factory-memory-toggle,
    #factory-improve-modal[data-view="radar"] #factory-memory-panel {
      display: none !important;
    }
    #factory-improve-modal[data-view="chat"] #liwi-radar-section {
      display: none !important;
    }
    .factory-chat-head-right {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .liwi-pilot-state-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--muted);
      white-space: nowrap;
    }
    .liwi-pilot-state-pill.is-building {
      color: var(--accent);
      background: var(--info-bg);
      border-color: rgba(56, 189, 248, 0.4);
    }
    .liwi-pilot-state-pill.is-investigating {
      color: var(--warn);
      background: var(--warn-bg);
      border-color: rgba(245, 158, 11, 0.45);
    }
    .liwi-pilot-state-pill.is-paused {
      color: var(--warn);
      background: var(--warn-bg);
      border-color: rgba(245, 158, 11, 0.35);
    }
    .liwi-pilot-state-pill.is-on {
      color: var(--ok);
      background: var(--ok-bg);
      border-color: rgba(34, 197, 94, 0.35);
    }
    .liwi-hero-active-card {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid rgba(56, 189, 248, 0.45);
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.08), rgba(15, 23, 42, 0.25));
    }
    .liwi-hero-active-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .liwi-hero-active-kicker {
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--accent);
    }
    .liwi-hero-active-title {
      font-size: 0.92rem;
      font-weight: 700;
      color: var(--text);
      line-height: 1.35;
    }
    .liwi-hero-active-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.78rem;
      color: var(--muted);
    }
    .liwi-queue-drain-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      padding: 8px 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
    }
    .liwi-radar-grid.is-lane-list {
      grid-template-columns: 1fr;
      gap: 10px;
    }
    .liwi-queue-rank {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 24px;
      height: 22px;
      padding: 0 6px;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      background: var(--info-bg);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.3);
      flex-shrink: 0;
    }
    .liwi-retry-badge {
      display: inline-flex;
      align-items: center;
      font-size: 0.7rem;
      font-weight: 600;
      padding: 1px 7px;
      border-radius: 999px;
      color: var(--warn);
      background: var(--warn-bg);
      border: 1px solid rgba(245, 158, 11, 0.35);
    }
    .liwi-impl-badge {
      display: inline-flex;
      align-items: center;
      font-size: 0.7rem;
      font-weight: 600;
      padding: 1px 7px;
      border-radius: 999px;
    }
    .liwi-impl-badge.is-ok {
      color: var(--ok);
      background: var(--ok-bg);
      border: 1px solid rgba(34, 197, 94, 0.32);
    }
    .liwi-impl-badge.is-fail {
      color: var(--err);
      background: var(--err-bg);
      border: 1px solid rgba(239, 68, 68, 0.35);
    }
    .liwi-pilot-settings-panel {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      padding: 10px 12px;
      margin-bottom: 10px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      align-items: center;
    }
    .liwi-pilot-setting-field {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 0.76rem;
      color: var(--muted);
      min-width: 130px;
      flex: 1 1 130px;
    }
    .factory-memory-panel {
      margin-top: 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
      max-height: 190px;
      overflow-y: auto;
      font-size: 0.85rem;
    }
    .factory-memory-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 6px 0;
      border-bottom: 1px solid rgba(148, 163, 184, 0.14);
    }
    .factory-memory-item:last-child {
      border-bottom: none;
    }
    .factory-chat-meta-col .factory-progress-card {
      margin-top: 10px;
      border-radius: 12px;
    }
    .factory-chat-stream {
      flex: 1 1 auto;
      min-height: 0;
      max-width: 800px;
      width: 100%;
      margin: 0 auto;
      padding: 24px 20px 32px;
      box-sizing: border-box;
      overflow-y: auto;
      overflow-x: hidden;
      display: flex;
      flex-direction: column;
      gap: 22px;
      background: transparent;
      border: none;
      border-radius: 0;
      scrollbar-width: thin;
    }
    .factory-chat-empty {
      margin: auto;
      text-align: center;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
      max-width: 480px;
      padding: 24px 12px;
    }
    .factory-msg {
      display: flex;
      flex-direction: column;
      min-width: 0;
      max-width: 100%;
    }
    .factory-msg-user {
      align-self: flex-end;
      max-width: min(78%, 600px);
      background: var(--surface-alt);
      border: 1px solid rgba(148, 163, 184, 0.16);
      border-radius: 20px 20px 6px 20px;
      padding: 12px 18px;
      font-size: 15px;
      line-height: 1.6;
      color: var(--text);
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .factory-msg-assistant {
      align-self: stretch;
      width: 100%;
      background: transparent;
      border: none;
      border-radius: 0;
      padding: 4px 0;
      font-size: 15.5px;
      line-height: 1.65;
      color: var(--text);
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .factory-msg-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
      min-width: 0;
    }
    .factory-msg-role {
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--muted);
      margin-bottom: 6px;
      letter-spacing: 0.01em;
    }
    .factory-msg-head .factory-msg-role {
      margin-bottom: 0;
    }
    .factory-msg-body p {
      margin: 0 0 10px;
    }
    .factory-msg-body p:last-child {
      margin-bottom: 0;
    }
    .factory-msg-body ul,
    .factory-msg-body ol {
      margin: 6px 0 10px;
      padding-left: 20px;
    }
    .factory-msg-body pre {
      overflow-x: auto;
      max-width: 100%;
    }
    #factory-chat-stream .md-code-head {
      gap: 8px;
      flex-wrap: wrap;
    }
    .factory-pre-wrap {
      margin: 8px 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--surface-alt);
      max-width: 100%;
    }
    .factory-pre-bar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      padding: 4px 8px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }
    .factory-pre-wrap pre {
      margin: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }
    .factory-proposal-card {
      margin-top: 14px;
      padding: 14px 16px;
      border-radius: 12px;
      background: var(--surface);
      border: 1px solid rgba(148, 163, 184, 0.22);
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .factory-proposal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }
    .factory-proposal-title {
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text);
      min-width: 0;
    }
    .factory-proposal-card .doctor-prompt-pre {
      margin: 0;
      padding: 10px 12px;
      border-radius: 8px;
      background: var(--surface-alt);
      border: 1px solid rgba(148, 163, 184, 0.16);
      font-size: 0.83rem;
      line-height: 1.5;
      max-height: 220px;
      overflow-y: auto;
    }
    .factory-proposal-actions {
      display: flex;
      align-items: center;
      justify-content: flex-start;
    }
    .factory-chat-thumbs {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .factory-chat-thumbs img {
      max-width: 150px;
      max-height: 96px;
      border-radius: 8px;
      border: 1px solid rgba(148, 163, 184, 0.2);
    }
    .factory-composer-wrap {
      flex-shrink: 0;
      width: 100%;
      padding: 8px 20px 18px;
      box-sizing: border-box;
      background: var(--bg);
    }
    .factory-composer-inner {
      max-width: 800px;
      width: 100%;
      margin: 0 auto;
      box-sizing: border-box;
    }
    .factory-composer-capsule {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 26px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.16);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .factory-composer-capsule:focus-within {
      border-color: var(--accent);
      box-shadow: 0 4px 22px rgba(56, 189, 248, 0.12);
    }
    .factory-paste-previews {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      padding: 2px 6px 8px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.14);
    }
    .factory-paste-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px 3px 4px;
      border-radius: 10px;
      background: var(--surface-alt);
      border: 1px solid rgba(148, 163, 184, 0.2);
      font-size: 0.76rem;
      max-width: 200px;
    }
    .factory-paste-chip img {
      width: 34px;
      height: 34px;
      object-fit: cover;
      border-radius: 7px;
      flex-shrink: 0;
    }
    .factory-paste-chip-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .factory-composer-row {
      display: flex;
      align-items: flex-end;
      gap: 8px;
      min-width: 0;
    }
    .factory-attach-icon-btn {
      width: 36px;
      height: 36px;
      border-radius: 9999px;
      flex-shrink: 0;
      border: none;
      background: transparent;
      color: var(--muted);
      font-size: 1.05rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease;
    }
    .factory-attach-icon-btn:hover:not(:disabled) {
      background: var(--surface-alt);
      color: var(--text);
    }
    .factory-attach-icon-btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .factory-composer-textarea {
      flex: 1 1 auto;
      min-width: 0;
      width: 100%;
      border: none;
      background: transparent;
      color: var(--text);
      font-family: inherit;
      font-size: 15px;
      line-height: 1.5;
      padding: 6px 4px;
      margin: 0;
      resize: none;
      outline: none;
      box-shadow: none;
      max-height: 180px;
      overflow-y: hidden;
      box-sizing: border-box;
    }
    .factory-composer-textarea::placeholder {
      color: var(--muted);
    }
    .factory-send-circle-btn {
      width: 36px;
      height: 36px;
      border-radius: 9999px;
      flex-shrink: 0;
      border: none;
      background: var(--accent);
      color: var(--btn-primary-fg);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 1.05rem;
      font-weight: 700;
      cursor: pointer;
      transition: background 0.15s ease, opacity 0.15s ease, transform 0.1s ease;
    }
    .factory-send-circle-btn:hover:not(:disabled) {
      background: var(--accent-hover);
      transform: translateY(-1px);
    }
    .factory-send-circle-btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none;
    }
    .factory-inline-status {
      min-height: 16px;
      font-size: 0.78rem;
      padding: 5px 14px 0;
      color: var(--muted);
    }
    @media (max-width: 720px) {
      #factory-improve-modal {
        padding: 0;
        align-items: stretch;
      }
      #factory-improve-modal .factory-chat-modal {
        width: 100vw;
        max-width: 100vw;
        height: 100dvh;
        max-height: 100dvh;
        border-radius: 0;
        border: none;
      }
      .factory-chat-head {
        padding: 12px 14px;
      }
      .factory-chat-meta-col {
        padding: 0 12px;
      }
      .factory-chat-stream {
        padding: 16px 14px 20px;
        gap: 18px;
      }
      .factory-composer-wrap {
        padding: 8px 12px 12px;
      }
      .factory-msg-user {
        max-width: 88%;
      }
    }
    .btn-spinner {
      display: inline-block;
      width: 13px;
      height: 13px;
      border: 2px solid currentColor;
      border-top-color: transparent;
      border-radius: 50%;
      vertical-align: -2px;
      margin-right: 6px;
      animation: spin 0.75s linear infinite;
    }
    .live-op-clock,
    .card-live-timer {
      font-variant-numeric: tabular-nums;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.76rem;
      color: var(--accent);
      font-weight: 600;
    }
    #audit-toolbar-status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 0.78rem;
      color: var(--accent);
    }
    .factory-fs-overlay {
      position: fixed;
      inset: 0;
      z-index: 950;
      background: rgba(6, 10, 20, 0.76);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .factory-fs-card {
      position: relative;
      width: 100%;
      max-width: 540px;
      background: var(--surface);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 18px;
      padding: 30px 24px 24px;
      box-shadow: 0 24px 64px rgba(0, 0, 0, 0.45);
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      gap: 12px;
    }
    .factory-fs-minimize {
      position: absolute;
      top: 14px;
      right: 14px;
    }
    @keyframes factory-fs-pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.35); }
      50% { box-shadow: 0 0 0 14px rgba(56, 189, 248, 0); }
    }
    .factory-fs-ring-wrap {
      position: relative;
      width: 68px;
      height: 68px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      animation: factory-fs-pulse 2s ease-in-out infinite;
    }
    .factory-fs-ring {
      position: absolute;
      inset: 0;
      border-radius: 50%;
      border: 3px solid rgba(56, 189, 248, 0.2);
      border-top-color: var(--accent);
      animation: spin 1s linear infinite;
    }
    .factory-fs-ring-icon {
      font-size: 1.5rem;
      line-height: 1;
    }
    .factory-fs-kicker {
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--accent);
    }
    .factory-fs-title {
      margin: 0;
      font-size: 1.15rem;
      font-weight: 700;
      color: var(--text);
    }
    .factory-fs-timer {
      font-size: 1.05rem;
      padding: 3px 12px;
      border-radius: 999px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
    }
    .factory-fs-steps {
      width: 100%;
    }
    .factory-fs-detail {
      font-size: 0.86rem;
      color: var(--muted);
      line-height: 1.45;
      max-width: 460px;
    }
    .triage-alert-banner {
      padding: 10px 14px;
      border-radius: 10px;
      border: 1px solid rgba(245, 158, 11, 0.55);
      background: rgba(245, 158, 11, 0.14);
      color: var(--text);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .triage-alert-main {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .triage-alert-copy {
      flex: 1 1 260px;
      min-width: 0;
    }
    .triage-alert-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .factory-thinking-bubble {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 14px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      color: var(--muted);
      font-size: 0.88rem;
    }
    .autonomous-controls-wrap {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: nowrap;
      flex: 0 0 auto;
    }
    .autonomous-switch.is-on {
      border-color: var(--ok);
      background: var(--ok-bg);
      color: var(--text);
      font-weight: 600;
    }
    .autonomous-switch.is-paused {
      border-color: var(--warn);
      background: rgba(245, 158, 11, 0.16);
      color: var(--text);
    }
    .quota-meter-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--text);
      white-space: nowrap;
    }
    .quota-meter-pill.is-healthy {
      border-color: rgba(34, 197, 94, 0.45);
      background: rgba(34, 197, 94, 0.14);
    }
    .quota-meter-pill.is-warn,
    .quota-meter-pill.is-paused {
      border-color: rgba(245, 158, 11, 0.55);
      background: rgba(245, 158, 11, 0.16);
    }
    .liwi-radar-subtabs {
      display: flex;
      gap: 6px;
      margin: 8px 0 10px 0;
      flex-wrap: wrap;
    }
    .liwi-subtab-btn {
      padding: 5px 11px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--muted);
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
    }
    .liwi-subtab-btn.active {
      border-color: var(--accent);
      color: var(--text);
      background: rgba(56, 189, 248, 0.16);
    }
    .liwi-history-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 290px;
      overflow-y: auto;
    }
    .liwi-history-card {
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .liwi-history-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
      flex-wrap: wrap;
    }
    .liwi-history-meta {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      font-size: 0.76rem;
      color: var(--muted);
    }
    .proj-ideas-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .proj-ideas-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 10px;
    }
    .proj-idea-card {
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .proj-idea-card-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: auto;
    }
    @media (prefers-reduced-motion: reduce) {
      .factory-fs-ring,
      .factory-fs-ring-wrap,
      .btn-spinner {
        animation: none !important;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span id="server-health-dot" class="live-dot" title="Sincronización activa con el servidor"></span>
      <span>Fábrica de Proyectos</span>
      <span id="server-health-label" class="health-label" aria-live="polite" style="display:none">Servidor desconectado</span>
      <span id="status-note" class="sr-only"></span>
    </div>
    <div class="controls">
      <div id="view-mode-segmented" class="segmented-control" role="group" aria-label="Modo técnico: Vista Simple o Vista Desarrollador" title="Modo técnico: alterna entre Vista Simple y Vista Desarrollador">
        <button id="view-mode-simple" type="button" class="seg-btn active" data-view-mode="simple" aria-label="Vista Simple">Simple</button>
        <button id="view-mode-dev" type="button" class="seg-btn" data-view-mode="dev" aria-label="Vista Desarrollador">Desarrollador</button>
        <input id="tech-mode-toggle" type="checkbox" class="sr-only" aria-label="Modo técnico" />
      </div>
      <div id="staging-banner-wrap" class="staging-banner-wrap" style="display:none">
        <button id="staging-banner-btn" class="btn btn-sm staging-pill" type="button" aria-expanded="false" aria-controls="staging-popover" title="Estado de auto-mejoras en staging">
          <span id="staging-banner-spinner" class="btn-spinner" aria-hidden="true" style="display:none"></span>
          <span id="staging-banner-label"></span>
        </button>
        <div id="staging-popover" class="staging-popover" role="region" aria-label="Mejoras en staging" style="display:none">
          <div class="staging-popover-head">
            <strong id="staging-popover-title">Mejoras listas para aplicar</strong>
            <div class="staging-popover-head-right">
              <span id="staging-popover-meta" class="muted"></span>
              <button id="staging-popover-close" class="staging-popover-close" type="button" aria-label="Cerrar panel de staging" title="Cerrar">✕</button>
            </div>
          </div>
          <ul id="staging-commits-list" class="staging-commits-list"></ul>
          <div id="staging-popover-error" class="staging-popover-error" role="alert" style="display:none"></div>
          <div class="staging-popover-actions">
            <button id="staging-promote-btn" class="btn btn-primary btn-sm" type="button">Actualizar ahora</button>
            <div id="staging-promote-status" class="staging-promote-status" aria-live="polite" style="display:none">
              <span class="btn-spinner" aria-hidden="true"></span>
              <span id="staging-promote-status-text">Aplicando actualizaciones y reiniciando...</span>
              <span id="staging-promote-clock" class="staging-live-clock">0s</span>
            </div>
          </div>
        </div>
      </div>
      <div id="autonomous-controls-wrap" class="autonomous-controls-wrap">
        <button id="autonomous-toggle-btn" class="btn btn-sm autonomous-switch" type="button" aria-pressed="false" title="Activar o pausar modo Automejora 24/7">🤖 Automejora: OFF</button>
        <span id="quota-meter-pill" class="quota-meter-pill is-healthy" title="Cuota estimada disponible en esta ventana">🔋 ~100%</span>
      </div>
      <button id="auto-ideas-btn" class="btn btn-sm" type="button" title="Abrir Radar de Auto-Mejoras de Liwi">💡 Auto-Mejoras<span id="auto-ideas-count" class="auto-ideas-badge" style="display:none">0</span></button>
      <button id="improve-factory-btn" class="btn btn-sm" type="button" title="Abrir chat padre de auto-mejora de Liwi">🏭 Mejorar Fábrica</button>
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
          <div class="themes-menu-wrap">
            <button id="themes-menu-btn" class="btn btn-sm settings-themes-trigger" type="button" aria-expanded="false" aria-controls="themes-popover">🎨 Temas</button>
            <div id="themes-popover" class="themes-popover" role="menu" aria-label="Temas de color" style="display:none">
              <button type="button" class="theme-option active" data-theme-choice="dark" role="menuitemradio" aria-checked="true">
                <span class="theme-swatch" style="background:linear-gradient(135deg,#0b0f19 50%,#38bdf8 50%);"></span>
                <span>Noche Profunda (Oscuro)</span>
              </button>
              <button type="button" class="theme-option" data-theme-choice="light" role="menuitemradio" aria-checked="false">
                <span class="theme-swatch" style="background:linear-gradient(135deg,#f8fafc 50%,#0369a1 50%);"></span>
                <span>Luz Editorial (Claro)</span>
              </button>
              <button type="button" class="theme-option" data-theme-choice="emerald" role="menuitemradio" aria-checked="false">
                <span class="theme-swatch" style="background:linear-gradient(135deg,#0b1915 50%,#34d399 50%);"></span>
                <span>Bosque Esmeralda</span>
              </button>
              <button type="button" class="theme-option" data-theme-choice="sunset" role="menuitemradio" aria-checked="false">
                <span class="theme-swatch" style="background:linear-gradient(135deg,#1b1311 50%,#fb923c 50%);"></span>
                <span>Atardecer Terracota</span>
              </button>
              <button type="button" class="theme-option" data-theme-choice="lavender" role="menuitemradio" aria-checked="false">
                <span class="theme-swatch" style="background:linear-gradient(135deg,#14141f 50%,#a78bfa 50%);"></span>
                <span>Grafito Lavanda</span>
              </button>
            </div>
          </div>
          <div id="settings-feedback" class="settings-feedback" style="display:none"></div>
        </div>
      </div>
    </div>
  </header>

  <div class="live-activity-shell">
    <div id="live-activity-banner" class="live-activity-banner" style="display:none">
      <div class="live-activity-main">
        <svg class="live-spinner-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
          <circle cx="12" cy="12" r="9" opacity="0.25"></circle>
          <path d="M21 12a9 9 0 0 0-9-9" stroke-linecap="round"></path>
        </svg>
        <div role="status" aria-live="polite" aria-atomic="true">
          <div id="live-activity-text" style="font-size:0.9rem; font-weight:600;">Procesando...</div>
          <div id="live-activity-subtext" class="muted" style="font-size:0.78rem;"></div>
        </div>
        <span id="live-activity-timer" class="live-activity-timer" aria-hidden="true">00:00s</span>
      </div>
      <div id="live-activity-stall" class="live-activity-stall" style="display:none" role="alert">
        <span>La operación está tardando más de lo esperado (puede seguir procesando o haberse bloqueado)</span>
        <button id="live-activity-reload-btn" class="btn btn-sm btn-warn" type="button">Reintentar / Recargar estado</button>
      </div>
    </div>
    <div id="factory-build-banner" class="live-activity-banner" role="button" tabindex="0" style="display:none; margin-top:8px; cursor:pointer;" title="Abrir chat de Liwi para ver el progreso completo">
      <div class="live-activity-main">
        <svg class="live-spinner-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
          <circle cx="12" cy="12" r="9" opacity="0.25"></circle>
          <path d="M21 12a9 9 0 0 0-9-9" stroke-linecap="round"></path>
        </svg>
        <div style="flex:1 1 auto;">
          <div id="factory-build-banner-title" style="font-size:0.9rem; font-weight:600;">🔨 Construyendo mejora de Liwi...</div>
          <div id="factory-build-banner-detail" class="muted" style="font-size:0.78rem;"></div>
        </div>
        <span id="factory-build-banner-timer" class="live-activity-timer">00:00</span>
      </div>
    </div>
    <div id="factory-restart-banner" class="live-activity-banner" style="display:none; margin-top:8px;" role="status" aria-live="polite">
      <div class="live-activity-main">
        <svg class="live-spinner-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
          <circle cx="12" cy="12" r="9" opacity="0.25"></circle>
          <path d="M21 12a9 9 0 0 0-9-9" stroke-linecap="round"></path>
        </svg>
        <div>
          <div style="font-size:0.9rem; font-weight:600;">🔄 Liwi se está reiniciando para aplicar la mejora...</div>
          <div id="factory-restart-subtext" class="muted" style="font-size:0.78rem;">Esperando reconexión automática con el servidor supervisado.</div>
        </div>
      </div>
    </div>
    <div id="triage-alert-banner" class="triage-alert-banner" role="alert" style="display:none; margin-top:8px;">
      <div class="triage-alert-main">
        <div class="triage-alert-copy">
          <strong id="triage-alert-title" style="font-size:0.88rem;">🩺 Se detectó un inconveniente técnico</strong>
          <div id="triage-alert-summary" class="muted" style="font-size:0.82rem;"></div>
        </div>
        <div class="triage-alert-actions">
          <button id="triage-repair-btn" class="btn btn-primary btn-sm" type="button">✨ Reparar ahora</button>
          <button id="triage-copy-prompt-btn" class="btn btn-sm" type="button">📋 Copiar prompt para Antigravity</button>
          <button id="triage-dismiss-btn" class="btn btn-sm" type="button" aria-label="Descartar aviso" title="Descartar aviso">✕</button>
        </div>
      </div>
      <div id="triage-alert-feedback" class="muted" style="font-size:0.78rem; display:none;"></div>
    </div>
    <div id="triage-solutions-banner" class="triage-alert-banner" role="region" aria-label="Soluciones pendientes de Auto-Triage" style="display:none; margin-top:8px;">
      <div class="triage-alert-main">
        <div class="triage-alert-copy">
          <strong style="font-size:0.86rem;">🩺 Auto-Triage <span id="triage-solutions-count" class="auto-ideas-badge">0</span></strong>
          <div id="triage-solutions-summary" class="muted" style="font-size:0.81rem;"></div>
        </div>
        <div class="triage-alert-actions">
          <button id="triage-solution-approve-btn" class="btn btn-primary btn-sm" type="button">✓ Aprobar</button>
          <button id="triage-solution-dismiss-btn" class="btn btn-sm" type="button" aria-label="Descartar solución" title="Descartar solución">✕ Descartar</button>
        </div>
      </div>
    </div>
  </div>

  <main class="layout">
    <section class="panel sidebar-collapsed" id="sidebar-panel">
      <div class="mobile-switcher-row" style="margin-bottom:10px;">
        <button id="mobile-project-switcher" class="btn btn-sm mobile-switcher-btn" type="button" aria-expanded="false" aria-controls="sidebar-projects-body">Proyecto: Cargando… ▾ (0)</button>
        <button id="mobile-new-project-shortcut" class="btn btn-primary btn-sm" type="button">+ Nuevo</button>
      </div>
      <div class="panel-header">
        <h2 style="margin:0; font-size:var(--fs-lg);">Tus proyectos</h2>
        <button id="toggle-import-project-btn" class="btn btn-sm" type="button">Importar proyecto</button>
      </div>
      <div id="sidebar-projects-body">
        <button id="toggle-new-project-btn" class="btn btn-primary sidebar-new-btn" type="button">+ Nuevo proyecto</button>
        <form id="new-project-form" class="new-project-form" style="display:none">
          <div id="example-prompts" class="example-prompts">
            <span class="muted" style="font-size:var(--fs-xs);">Ejemplos:</span>
            <button type="button" class="example-chip" data-example="Crea un temporizador Pomodoro minimalista con pausas configurables y alertas visuales.">⏱️ Temporizador Pomodoro</button>
            <button type="button" class="example-chip" data-example="Crea un diario didáctico que tenga preguntas guía diarias y calendario para revisar entradas anteriores.">📓 Diario con preguntas guía</button>
            <button type="button" class="example-chip" data-example="Crea una interfaz web local para organizar y descargar mi música favorita con barra de progreso.">🎵 Organizador de música</button>
          </div>
          <div class="muted" style="font-size:var(--fs-xs); margin-bottom:8px;">Consejo: cuéntanos para quién es y 2 o 3 funciones principales que deseas.</div>
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
      </div>
    </section>

    <div id="import-project-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="import-modal-title" style="display:none">
      <form id="import-project-form" class="modal-card">
        <h3 id="import-modal-title" style="margin:0 0 8px 0;">Importar proyecto de esta computadora</h3>
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

    <div id="factory-improve-modal" class="modal-backdrop" data-view="chat" role="dialog" aria-modal="true" aria-labelledby="factory-improve-title" style="display:none">
      <div class="modal-card factory-chat-modal">
        <div class="factory-chat-head">
          <div class="factory-chat-head-left">
            <h3 id="factory-improve-title" class="factory-chat-title">Liwi</h3>
            <button id="factory-memory-toggle" type="button" class="model-pill chat-only-ctrl" aria-expanded="false" aria-controls="factory-memory-panel">🧠 Memoria de Liwi (0)</button>
            <span id="liwi-pilot-state-pill" class="liwi-pilot-state-pill radar-only-ctrl">Apagado</span>
          </div>
          <div class="factory-chat-head-right">
            <button id="liwi-radar-generate-btn" class="btn btn-sm liwi-radar-gen-btn radar-only-ctrl" type="button">💡 Generar ideas</button>
            <button id="liwi-visual-audit-btn" class="btn btn-sm radar-only-ctrl" type="button" title="Auditar interfaz como usuario sintético con Playwright">🕵️ Simular usuario</button>
            <button id="factory-modal-switch-view-btn" type="button" class="model-pill">💡 Ver Radar</button>
            <button id="liwi-pilot-settings-btn" class="btn btn-sm radar-only-ctrl" type="button" aria-expanded="false" aria-controls="liwi-pilot-settings-panel" title="Ajustes del piloto">⚙️</button>
            <button id="close-factory-improve-btn" class="factory-close-btn" type="button" aria-label="Cerrar / Volver">✕ Cerrar</button>
          </div>
        </div>
        <div class="factory-chat-meta-col">
          <div id="factory-memory-panel" class="factory-memory-panel" style="display:none"></div>
          <div id="factory-progress-card" class="factory-progress-card" style="display:none" role="status" aria-live="polite">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
              <strong id="factory-progress-title" style="font-size:0.88rem;">🔨 Construyendo mejora...</strong>
              <span id="factory-progress-timer" class="live-activity-timer">00:00</span>
            </div>
            <div class="factory-steps-bar">
              <div class="factory-step-pill" data-fstep="1">1/3 · Editar IA</div>
              <div class="factory-step-pill" data-fstep="2">2/3 · Ruff + Pytest</div>
              <div class="factory-step-pill" data-fstep="3">3/3 · Commit + Reinicio</div>
            </div>
            <div id="factory-progress-detail" class="muted" style="font-size:0.8rem;"></div>
          </div>
          <section id="liwi-radar-section" class="liwi-radar-section" aria-labelledby="factory-improve-title">
            <h4 id="liwi-radar-title" class="liwi-radar-title" style="display:none">💡 Radar de Auto-Mejoras</h4>
            <div class="liwi-radar-subtabs" role="tablist" aria-label="Vistas de auto-mejoras" style="display:none">
              <button id="liwi-subtab-ideas-btn" class="liwi-subtab-btn active" type="button" role="tab" aria-selected="true">💡 Ideas pendientes</button>
              <button id="liwi-subtab-history-btn" class="liwi-subtab-btn" type="button" role="tab" aria-selected="false">📜 Bitácora Autónoma</button>
            </div>
            <div id="liwi-pilot-settings-panel" class="liwi-pilot-settings-panel" style="display:none">
              <label class="liwi-pilot-setting-field">
                <span>Frecuencia de ideas</span>
                <select id="liwi-radar-frequency" class="liwi-radar-select" aria-label="Frecuencia automática de ideas" title="Frecuencia automática de generación de ideas">
                  <option value="0">Manual</option>
                  <option value="24">Cada 24h</option>
                  <option value="72">Cada 3 días</option>
                </select>
              </label>
              <label class="liwi-pilot-setting-field">
                <span>Reserva de cuota</span>
                <select id="liwi-reserve-threshold" class="liwi-radar-select" aria-label="Reserva de cuota" title="Umbral mínimo de reserva de cuota antes de pausar automejoras">
                  <option value="20">Reserva 20%</option>
                  <option value="30" selected>Reserva 30%</option>
                  <option value="40">Reserva 40%</option>
                  <option value="50">Reserva 50%</option>
                </select>
              </label>
              <label class="liwi-pilot-setting-field">
                <span>Enfriamiento</span>
                <select id="liwi-autonomous-cooldown" class="liwi-radar-select" aria-label="Cooldown autónomo" title="Pausa mínima entre mejoras autónomas">
                  <option value="15">Cada 15m</option>
                  <option value="20">Cada 20m</option>
                  <option value="25" selected>Cada 25m</option>
                  <option value="30">Cada 30m</option>
                </select>
              </label>
            </div>
            <div id="liwi-hero-active-card" class="liwi-hero-active-card" style="display:none" role="status" aria-live="polite">
              <div class="liwi-hero-active-head">
                <span class="liwi-hero-active-kicker">⚙️ En curso ahora</span>
                <span id="liwi-hero-active-timer" class="live-activity-timer">⏱️ 00:00</span>
              </div>
              <div id="liwi-hero-active-title" class="liwi-hero-active-title">Preparando mejora...</div>
              <div class="liwi-hero-active-meta">
                <span id="liwi-hero-active-step">Analizando</span>
                <button id="liwi-queue-reconcile-btn" class="btn btn-sm" type="button">🩺 Verificar ahora</button>
              </div>
            </div>
            <div id="liwi-radar-live-status" class="liwi-radar-live-status" role="status" aria-live="polite" style="display:none">
              <span id="liwi-radar-spinner" class="btn-spinner" aria-hidden="true" style="display:none"></span>
              <span id="liwi-radar-status-text" class="liwi-radar-status-text"></span>
              <span id="liwi-radar-clock" class="liwi-radar-clock" style="display:none">0s</span>
            </div>
            <div id="liwi-radar-filters" class="liwi-radar-filters" role="tablist" aria-label="Carriles del Centro de Auto-Mejora">
              <button id="liwi-radar-filter-pending" class="liwi-radar-filter-btn active" type="button" role="tab" aria-selected="true" data-radar-filter="pending"><span>✨ Nuevas</span><span class="liwi-radar-filter-count" data-radar-filter-count="pending">0</span></button>
              <button id="liwi-radar-filter-in-progress" class="liwi-radar-filter-btn" type="button" role="tab" aria-selected="false" data-radar-filter="queued"><span>⏳ En cola</span><span class="liwi-radar-filter-count" data-radar-filter-count="queued">0</span></button>
              <button id="liwi-radar-filter-implemented" class="liwi-radar-filter-btn" type="button" role="tab" aria-selected="false" data-radar-filter="implemented"><span>✅ Implementadas</span><span class="liwi-radar-filter-count" data-radar-filter-count="implemented">0</span></button>
            </div>
            <div id="liwi-queue-drain-bar" class="liwi-queue-drain-bar" style="display:none">
              <span class="muted" style="font-size:0.78rem;">Ejecuta todas las ideas en espera una por una en .liwi-staging</span>
              <button id="liwi-drain-queue-btn" class="btn btn-primary btn-sm" type="button">▶️ Procesar toda la cola (0)</button>
            </div>
            <div id="liwi-radar-empty" class="liwi-radar-empty" style="display:none">Sin ideas pendientes por ahora. Pulsa «💡 Generar ideas» para descubrir mejoras.</div>
            <div id="liwi-radar-grid" class="liwi-radar-grid" aria-live="polite"></div>
            <div id="liwi-autonomous-history-panel" class="liwi-history-list" style="display:none" aria-live="polite"></div>
          </section>
        </div>
        <div id="factory-chat-stream" class="factory-chat-stream">
          <div class="factory-chat-empty">Cargando historial del asistente...</div>
        </div>
        <div class="factory-composer-wrap">
          <div class="factory-composer-inner">
            <form id="factory-chat-form" class="factory-composer-capsule">
              <div id="factory-paste-previews" class="factory-paste-previews" style="display:none"></div>
              <div class="factory-composer-row">
                <input id="factory-image-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple style="display:none" />
                <button id="factory-attach-btn" class="factory-attach-icon-btn" type="button" title="Adjuntar captura (o Ctrl+V)" aria-label="Adjuntar captura">📎</button>
                <textarea id="factory-chat-input" class="factory-composer-textarea" rows="1" placeholder="Escribe a Liwi... (puedes pegar capturas con Ctrl+V)"></textarea>
                <button id="factory-chat-send-btn" class="factory-send-circle-btn" type="submit" title="Enviar mensaje" aria-label="Enviar mensaje">↑</button>
              </div>
            </form>
            <div id="factory-inline-status" class="factory-inline-status" role="status" aria-live="polite"></div>
          </div>
        </div>
      </div>
    </div>

    <div id="factory-fullscreen-build-overlay" class="factory-fs-overlay" role="dialog" aria-modal="true" aria-labelledby="factory-fs-title" style="display:none">
      <div class="factory-fs-card">
        <button id="factory-fs-minimize-btn" class="btn btn-sm factory-fs-minimize" type="button" title="Minimizar a la barra superior">Minimizar</button>
        <div class="factory-fs-ring-wrap" aria-hidden="true">
          <div class="factory-fs-ring"></div>
          <div class="factory-fs-ring-icon">🔨</div>
        </div>
        <div class="factory-fs-kicker">Actualizando Liwi</div>
        <h3 id="factory-fs-title" class="factory-fs-title">Aplicando mejora...</h3>
        <div id="factory-fs-timer" class="factory-fs-timer live-op-clock">00:00</div>
        <div class="factory-steps-bar factory-fs-steps">
          <div class="factory-step-pill" data-fsstep="1">1/3 Editando código con IA</div>
          <div class="factory-step-pill" data-fsstep="2">2/3 Verificando pruebas</div>
          <div class="factory-step-pill" data-fsstep="3">3/3 Guardando y reiniciando</div>
        </div>
        <div id="factory-fs-detail" class="factory-fs-detail">Iniciando proceso...</div>
      </div>
    </div>

    <section class="panel" id="detail-panel">
      <div id="detail-empty" class="empty-box">
        <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" style="opacity:0.6; margin-bottom:8px;" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="3"></rect>
          <path d="M8 9h8M8 13h5" stroke-linecap="round"></path>
        </svg>
        <div style="font-weight:600; color:var(--text); margin-bottom:4px;">Selecciona un proyecto de la lista para ver su estado y detalles.</div>
        <div class="muted" style="margin-bottom:12px;">También puedes crear uno nuevo describiendo tu idea en español.</div>
        <button id="empty-detail-create-btn" class="btn btn-primary btn-sm" type="button">+ Nuevo proyecto</button>
      </div>
      <div id="detail-content" style="display:none">
        <div id="detail-work-split" class="detail-work-split">
          <div id="detail-main-col">
            <div class="detail-header-group">
              <div class="detail-header">
                <div class="detail-titles">
                  <div class="title-row">
                    <button id="change-logo-btn" class="logo-icon-btn" type="button" title="Cambiar logo" aria-label="Cambiar logo">
                      <img id="detail-logo-img" class="detail-logo" src="" alt="Logo del proyecto" style="display:none" />
                    </button>
                    <h2 id="detail-title" class="detail-title"></h2>
                    <button id="edit-title-btn" class="btn-icon" type="button" title="Editar título" aria-label="Editar título">✏️</button>
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
                <div class="detail-header-status" style="display:flex; align-items:center; gap:8px;">
                  <span id="detail-badge" class="badge"></span>
                  <span id="detail-header-more"></span>
                </div>
              </div>
              <div id="detail-segments" class="stage-segments"></div>
              <div id="detail-time" class="muted" style="font-size:0.78rem; margin:-2px 0 10px 0; text-align:right;"></div>
            </div>

            <div id="detail-guidance"></div>

            <div role="tablist" aria-label="Secciones del proyecto">
              <div class="tabs">
                <button id="tab-audit-btn" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-audit" tabindex="-1" data-tab="audit" type="button" title="Consultar y Auditar proyecto" style="display:none">💬 Consultar y Auditar</button>
                <button id="tab-modify-btn" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-modify" tabindex="-1" data-tab="modify" type="button" style="display:none">✨ Pedir cambios</button>
                <button id="tab-ideas-btn" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-ideas" tabindex="-1" data-tab="ideas" type="button" style="display:none">💡 Auto-Mejoras <span id="project-ideas-tab-badge" class="auto-ideas-badge" style="display:none">0</span></button>
                <button id="tab-spec-btn" class="tab-btn active" role="tab" aria-selected="true" aria-controls="tab-spec" tabindex="0" data-tab="spec" type="button">📋 Resumen y Plan</button>
                <button id="tab-events-btn" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-events" tabindex="-1" data-tab="events" type="button">🕒 Historial</button>
              </div>
            </div>
            <div id="tab-audit" class="tab-content" role="tabpanel" aria-labelledby="tab-audit-btn" style="display:none"></div>
            <div id="tab-modify" class="tab-content" role="tabpanel" aria-labelledby="tab-modify-btn" style="display:none"></div>
            <div id="tab-ideas" class="tab-content" role="tabpanel" aria-labelledby="tab-ideas-btn" style="display:none"></div>
            <div id="tab-spec" class="tab-content" role="tabpanel" aria-labelledby="tab-spec-btn"></div>
            <div id="tab-events" class="tab-content" role="tabpanel" aria-labelledby="tab-events-btn" style="display:none"></div>

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

  <div id="confirm-action-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="confirm-modal-title" style="display:none">
    <div class="modal-card" style="max-width:440px;">
      <h3 id="confirm-modal-title" style="margin:0 0 8px 0;">Confirmar acción</h3>
      <p id="confirm-modal-body" class="muted" style="font-size:var(--fs-sm); margin:0 0 16px 0;"></p>
      <div style="display:flex; justify-content:flex-end; gap:8px;">
        <button id="confirm-modal-cancel" class="btn btn-sm" type="button">Cancelar</button>
        <button id="confirm-modal-ok" class="btn btn-warn btn-sm" type="button">Confirmar</button>
      </div>
    </div>
  </div>

  <div id="toast-stack" class="toast-stack" aria-live="polite" aria-atomic="false"></div>

  <script>
    const STALL_HTTP_SECONDS = 75;
    const STALL_BG_MINUTES = 10;
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
      projectIdeasByPid: {},
      projectIdeasLoadedFor: {},
      voiceTts: false,
      lastSpokenQuestionKey: null,
      editingTitlePid: null,
      adjustOpen: {},
      phaseOpen: {},
      priorOpen: {},
      userChoseTabByPid: {},
      auditSubTabByPid: {},
      auditFilterByPid: {},
      selectedAuditMsgIdByPid: {},
      chatMsgCollapseOverrideByPid: {},
      preserveChatScrollTop: null,
      targetFilesQueryByPid: {},
      interviewPendingByPid: {},
      lastBannerSignature: null,
      lastModifySignature: null,
      lastListSignature: null,
      lastSpecSignature: null,
      lastEventsSignature: null,
      activeOp: null,
      opHandoff: null,
      bgOpPid: null,
      bgOpStartedAtMs: null,
      serverConnected: true,
    };
    let statusNoteTimer = null;
    let lastToastEntry = { msg: '', at: 0 };

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

    function setNote(msg, variant) {
      const cleanMsg = String(msg || '').trim();
      const hiddenEl = document.getElementById('status-note');
      if (hiddenEl) {
        if (statusNoteTimer) {
          clearTimeout(statusNoteTimer);
          statusNoteTimer = null;
        }
        hiddenEl.textContent = cleanMsg;
        if (cleanMsg) {
          statusNoteTimer = setTimeout(() => {
            hiddenEl.textContent = '';
            statusNoteTimer = null;
          }, 4000);
        }
      }
      if (!cleanMsg) return;
      const nowMs = Date.now();
      if (lastToastEntry.msg === cleanMsg && (nowMs - lastToastEntry.at) < 6000) {
        return;
      }
      lastToastEntry = { msg: cleanMsg, at: nowMs };
      const stack = document.getElementById('toast-stack');
      if (!stack) return;
      let kind = variant || 'info';
      if (!variant && /^Error|^No se pudo/i.test(cleanMsg)) {
        kind = 'err';
      } else if (!variant && /^✓|actualizad|guardad|activado|encendida|restaurad/i.test(cleanMsg)) {
        kind = 'ok';
      }
      const item = document.createElement('div');
      item.className = 'toast-item toast-' + kind;
      item.setAttribute('role', kind === 'err' ? 'alert' : 'status');
      const txtSpan = document.createElement('span');
      txtSpan.textContent = cleanMsg;
      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'toast-close';
      closeBtn.setAttribute('aria-label', 'Cerrar aviso');
      closeBtn.textContent = '×';
      closeBtn.addEventListener('click', () => {
        if (item.parentNode) item.parentNode.removeChild(item);
      });
      item.appendChild(txtSpan);
      item.appendChild(closeBtn);
      stack.appendChild(item);
      while (stack.children.length > 3) {
        stack.removeChild(stack.firstChild);
      }
      const ttl = kind === 'err' ? 6500 : 4500;
      setTimeout(() => {
        if (item.parentNode) item.parentNode.removeChild(item);
      }, ttl);
    }

    function flashSavedElement(elOrId) {
      const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
      if (!el) return;
      el.classList.add('flash-saved');
      setTimeout(() => el.classList.remove('flash-saved'), 1400);
    }

    function showSettingsFeedback(msg) {
      const el = document.getElementById('settings-feedback');
      if (!el) return;
      el.textContent = msg;
      el.style.display = 'block';
      setTimeout(() => {
        if (el.textContent === msg) el.style.display = 'none';
      }, 2500);
    }

    async function copyToClipboard(text, okMsg) {
      const value = String(text || '');
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(value);
        } else {
          const tmp = document.createElement('textarea');
          tmp.value = value;
          document.body.appendChild(tmp);
          tmp.select();
          document.execCommand('copy');
          document.body.removeChild(tmp);
        }
        setNote(okMsg || '✓ Copiado al portapapeles.', 'ok');
      } catch (err) {
        setNote('No se pudo copiar: ' + err.message, 'err');
      }
    }

    function trapDialogFocus(dialogEl, onClose) {
      if (!dialogEl) return () => {};
      const openerEl = document.activeElement && document.activeElement.focus ? document.activeElement : null;
      function getFocusable() {
        return Array.from(
          dialogEl.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')
        ).filter((el) => el.offsetParent !== null || el === document.activeElement);
      }
      function onKeyDown(ev) {
        if (dialogEl.style.display === 'none') return;
        if (ev.key === 'Escape') {
          ev.preventDefault();
          if (typeof onClose === 'function') onClose();
          return;
        }
        if (ev.key === 'Tab') {
          const items = getFocusable();
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          if (!dialogEl.contains(document.activeElement)) {
            ev.preventDefault();
            first.focus();
            return;
          }
          if (ev.shiftKey && document.activeElement === first) {
            ev.preventDefault();
            last.focus();
          } else if (!ev.shiftKey && document.activeElement === last) {
            ev.preventDefault();
            first.focus();
          }
        }
      }
      document.addEventListener('keydown', onKeyDown);
      return function release() {
        document.removeEventListener('keydown', onKeyDown);
        if (openerEl && document.body.contains(openerEl)) {
          try { openerEl.focus(); } catch (_) {}
        }
      };
    }

    function askCustomConfirm(opts) {
      const cfg = opts || {};
      const modal = document.getElementById('confirm-action-modal');
      const titleEl = document.getElementById('confirm-modal-title');
      const bodyEl = document.getElementById('confirm-modal-body');
      const okBtn = document.getElementById('confirm-modal-ok');
      const cancelBtn = document.getElementById('confirm-modal-cancel');
      if (!modal || !titleEl || !bodyEl || !okBtn || !cancelBtn) {
        return Promise.resolve(window.confirm(cfg.body || '¿Confirmar acción?'));
      }
      titleEl.textContent = cfg.title || 'Confirmar acción';
      bodyEl.textContent = cfg.body || '';
      okBtn.textContent = cfg.confirmLabel || 'Confirmar';
      modal.style.display = 'flex';
      return new Promise((resolve) => {
        let settled = false;
        let releaseTrap = null;
        function finish(val) {
          if (settled) return;
          settled = true;
          modal.style.display = 'none';
          okBtn.removeEventListener('click', onOk);
          cancelBtn.removeEventListener('click', onCancel);
          modal.removeEventListener('click', onBackdrop);
          if (releaseTrap) releaseTrap();
          resolve(val);
        }
        function onOk() { finish(true); }
        function onCancel() { finish(false); }
        function onBackdrop(ev) { if (ev.target === modal) finish(false); }
        releaseTrap = trapDialogFocus(modal, onCancel);
        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        modal.addEventListener('click', onBackdrop);
        okBtn.focus();
      });
    }

    function showInlineError(targetId, errMsg, retryFn) {
      const el = targetId ? document.getElementById(targetId) : null;
      if (!el) return;
      el.innerHTML = '';
      if (!errMsg) return;
      const box = document.createElement('div');
      box.className = 'inline-error-box';
      box.setAttribute('role', 'alert');
      const msgSpan = document.createElement('span');
      msgSpan.textContent = String(errMsg);
      box.appendChild(msgSpan);
      if (typeof retryFn === 'function') {
        const retryBtn = document.createElement('button');
        retryBtn.type = 'button';
        retryBtn.className = 'btn btn-sm btn-warn';
        retryBtn.textContent = 'Reintentar';
        retryBtn.addEventListener('click', () => {
          el.innerHTML = '';
          retryFn();
        });
        box.appendChild(retryBtn);
      }
      el.appendChild(box);
    }

    const VALID_THEMES = ['dark', 'light', 'emerald', 'sunset', 'lavender'];
    const THEME_LABELS = {
      dark: 'Noche Profunda',
      light: 'Luz Editorial',
      emerald: 'Bosque Esmeralda',
      sunset: 'Atardecer Terracota',
      lavender: 'Grafito Lavanda',
    };

    function applyTheme(theme) {
      const cleanTheme = VALID_THEMES.includes(theme) ? theme : 'dark';
      document.documentElement.setAttribute('data-theme', cleanTheme);
      const btn = document.getElementById('theme-btn');
      if (btn) {
        btn.textContent = cleanTheme === 'light' ? 'Modo oscuro' : 'Modo claro';
      }
      document.querySelectorAll('.theme-option[data-theme-choice]').forEach((opt) => {
        const isAct = opt.getAttribute('data-theme-choice') === cleanTheme;
        opt.classList.toggle('active', isAct);
        opt.setAttribute('aria-checked', isAct ? 'true' : 'false');
      });
      try { localStorage.setItem('af_theme', cleanTheme); } catch (_) {}
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
      const next = cur === 'light' ? 'dark' : 'light';
      applyTheme(next);
      const label = next === 'light' ? 'Modo claro activado' : 'Modo oscuro activado';
      showSettingsFeedback('✓ ' + label);
      setNote('✓ ' + label, 'ok');
    });

    const headerPopoverController = (function createHeaderPopoverController() {
      const entries = new Map();

      function positionPopover(entry) {
        if (!entry || !entry.popover) return;
        const headerEl = document.querySelector('header');
        if (window.innerWidth <= 768 && headerEl && typeof headerEl.getBoundingClientRect === 'function') {
          const rect = headerEl.getBoundingClientRect();
          const topPx = Math.max(56, Math.round(rect.bottom + 6));
          entry.popover.style.top = topPx + 'px';
        } else {
          entry.popover.style.top = '';
        }
      }

      function isOpen(id) {
        const entry = entries.get(id);
        return Boolean(entry && entry.popover && entry.popover.style.display !== 'none');
      }

      function close(id, opts) {
        const entry = entries.get(id);
        if (!entry || !entry.popover) return false;
        const force = Boolean(opts && opts.force);
        if (!force && typeof entry.canClose === 'function' && !entry.canClose()) {
          return false;
        }
        entry.popover.style.display = 'none';
        entry.popover.setAttribute('data-open', 'false');
        if (entry.trigger) entry.trigger.setAttribute('aria-expanded', 'false');
        if (typeof entry.onClose === 'function') entry.onClose();
        return true;
      }

      function closeMoreDropdowns() {
        document.querySelectorAll('.more-popover').forEach((pop) => {
          pop.style.display = 'none';
        });
        document.querySelectorAll('[aria-controls="project-more-popover"], [aria-controls="header-more-popover"], [aria-controls="audit-more-actions-menu"], [aria-controls="quality-more-actions-menu"]').forEach((b) => {
          b.setAttribute('aria-expanded', 'false');
        });
      }

      function closeAll(opts) {
        const exceptId = opts && opts.exceptId ? String(opts.exceptId) : '';
        const force = Boolean(opts && opts.force);
        entries.forEach((entry, id) => {
          if (id === exceptId) return;
          close(id, { force: force });
        });
        closeMoreDropdowns();
      }

      function open(id) {
        const entry = entries.get(id);
        if (!entry || !entry.popover) return false;
        closeAll({ exceptId: id });
        entry.popover.style.display = 'flex';
        entry.popover.setAttribute('data-open', 'true');
        if (entry.trigger) entry.trigger.setAttribute('aria-expanded', 'true');
        positionPopover(entry);
        if (typeof entry.onOpen === 'function') entry.onOpen();
        return true;
      }

      function toggle(id) {
        if (isOpen(id)) {
          return close(id);
        }
        return open(id);
      }

      function register(id, config) {
        if (!id || !config || !config.popover) return;
        entries.set(String(id), {
          id: String(id),
          container: config.container || null,
          trigger: config.trigger || null,
          popover: config.popover,
          canClose: typeof config.canClose === 'function' ? config.canClose : null,
          onOpen: typeof config.onOpen === 'function' ? config.onOpen : null,
          onClose: typeof config.onClose === 'function' ? config.onClose : null,
        });
      }

      function handleOutsideClick(target) {
        entries.forEach((entry, id) => {
          if (!isOpen(id)) return;
          const inContainer = Boolean(entry.container && target && entry.container.contains(target));
          const inTrigger = Boolean(entry.trigger && target && entry.trigger.contains(target));
          const inPopover = Boolean(entry.popover && target && entry.popover.contains(target));
          if (!inContainer && !inTrigger && !inPopover) {
            close(id);
          }
        });
        const insideMore = target && target.closest ? target.closest('.more-menu-wrap, .audit-primary-split') : null;
        if (!insideMore) {
          closeMoreDropdowns();
        }
      }

      function handleKeydown(ev) {
        if (ev && ev.key === 'Escape') {
          closeAll();
        }
      }

      function repositionOpen() {
        entries.forEach((entry, id) => {
          if (isOpen(id)) positionPopover(entry);
        });
      }

      return {
        register: register,
        isOpen: isOpen,
        open: open,
        close: close,
        toggle: toggle,
        closeAll: closeAll,
        handleOutsideClick: handleOutsideClick,
        handleKeydown: handleKeydown,
        repositionOpen: repositionOpen,
      };
    })();
    window.headerPopoverController = headerPopoverController;

    function closeFloatingDropdowns(opts) {
      headerPopoverController.closeAll(opts);
    }

    document.addEventListener('click', (ev) => {
      headerPopoverController.handleOutsideClick(ev.target);
    });
    document.addEventListener('keydown', (ev) => {
      headerPopoverController.handleKeydown(ev);
    });
    window.addEventListener('resize', () => {
      headerPopoverController.repositionOpen();
    });

    function collapseNestedThemesMenu() {
      const btn = document.getElementById('themes-menu-btn');
      const pop = document.getElementById('themes-popover');
      if (pop) pop.style.display = 'none';
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }

    (function initThemesPopover() {
      const btn = document.getElementById('themes-menu-btn');
      const pop = document.getElementById('themes-popover');
      if (!btn || !pop) return;
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        headerPopoverController.close('staging');
        const isOpen = pop.style.display !== 'none';
        pop.style.display = isOpen ? 'none' : 'flex';
        btn.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
      });
      pop.querySelectorAll('.theme-option[data-theme-choice]').forEach((opt) => {
        opt.addEventListener('click', () => {
          const chosen = opt.getAttribute('data-theme-choice') || 'dark';
          applyTheme(chosen);
          headerPopoverController.closeAll();
          const title = THEME_LABELS[chosen] || chosen;
          showSettingsFeedback('✓ Tema: ' + title);
          setNote('✓ Tema aplicado: ' + title, 'ok');
        });
      });
    })();

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
      const wrap = btn.closest('.settings-menu-wrap') || pop.parentElement;
      headerPopoverController.register('settings', {
        container: wrap,
        trigger: btn,
        popover: pop,
        onClose: collapseNestedThemesMenu,
      });
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        headerPopoverController.toggle('settings');
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

    document.querySelectorAll('.tabs').forEach((tabsEl) => {
      tabsEl.setAttribute('role', 'presentation');
    });
    const tabButtonsList = Array.from(document.querySelectorAll('.tab-btn'));
    tabButtonsList.forEach((btn) => {
      btn.addEventListener('click', () => {
        state.activeTab = btn.dataset.tab;
        if (state.selectedId) {
          state.userChoseTabByPid[state.selectedId] = state.activeTab;
        }
        renderTabs();
      });
      btn.addEventListener('keydown', (ev) => {
        const visibleTabs = tabButtonsList.filter((b) => b.style.display !== 'none');
        if (!visibleTabs.length) return;
        const curIdx = visibleTabs.indexOf(btn);
        if (curIdx < 0) return;
        let nextIdx = -1;
        if (ev.key === 'ArrowRight') nextIdx = (curIdx + 1) % visibleTabs.length;
        else if (ev.key === 'ArrowLeft') nextIdx = (curIdx - 1 + visibleTabs.length) % visibleTabs.length;
        else if (ev.key === 'Home') nextIdx = 0;
        else if (ev.key === 'End') nextIdx = visibleTabs.length - 1;
        if (nextIdx >= 0) {
          ev.preventDefault();
          const targetBtn = visibleTabs[nextIdx];
          state.activeTab = targetBtn.dataset.tab;
          if (state.selectedId) {
            state.userChoseTabByPid[state.selectedId] = state.activeTab;
          }
          renderTabs();
          targetBtn.focus();
        }
      });
    });

    function renderTabs() {
      document.querySelectorAll('.tab-btn').forEach((b) => {
        const isActive = b.dataset.tab === state.activeTab;
        b.classList.toggle('active', isActive);
        b.setAttribute('aria-selected', isActive ? 'true' : 'false');
        b.setAttribute('tabindex', isActive ? '0' : '-1');
      });
      const specPane = document.getElementById('tab-spec');
      if (specPane) specPane.style.display = state.activeTab === 'spec' ? 'block' : 'none';
      const eventsPane = document.getElementById('tab-events');
      if (eventsPane) eventsPane.style.display = state.activeTab === 'events' ? 'block' : 'none';
      const auditPane = document.getElementById('tab-audit');
      if (auditPane) auditPane.style.display = state.activeTab === 'audit' ? 'block' : 'none';
      const modifyPane = document.getElementById('tab-modify');
      if (modifyPane) modifyPane.style.display = state.activeTab === 'modify' ? 'block' : 'none';
      const ideasPane = document.getElementById('tab-ideas');
      if (ideasPane) ideasPane.style.display = state.activeTab === 'ideas' ? 'block' : 'none';
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

    function resolveFetchTimeoutMs(url, explicitMs) {
      if (typeof explicitMs === 'number' && explicitMs > 0) return explicitMs;
      const u = String(url || '');
      if (u.indexOf('/chat') >= 0) return 250000;
      if (u.indexOf('/api/factory/assistant/apply') >= 0) return 920000;
      if (u.indexOf('/api/factory/assistant/message') >= 0) return 185000;
      if (/\\/(?:runtime\\/)?start\\b/.test(u)) return 180000;
      if (/\\/(modify|rollback|import|retry)\\b/.test(u)) return 120000;
      return 8000;
    }

    async function apiFetch(url, options = {}) {
      const opts = Object.assign({}, options);
      const timeoutMs = resolveFetchTimeoutMs(url, opts.timeoutMs);
      delete opts.timeoutMs;
      const ctrl = new AbortController();
      const tid = setTimeout(() => ctrl.abort(), timeoutMs);
      opts.signal = ctrl.signal;
      try {
        const res = await fetch(url, opts);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          if (res.status >= 500 && res.status !== 502 && res.status !== 503 && res.status !== 504 && typeof window.reportLiwiClientError === 'function' && String(url || '').indexOf('/api/telemetry/client-error') < 0) {
            window.reportLiwiClientError('fetch_500', 'HTTP ' + res.status + ' en ' + String(url || '') + ': ' + String(data.error || 'Error interno'), { url: String(url || ''), status: res.status });
          }
          throw new Error(data.error || ('HTTP ' + res.status));
        }
        return data;
      } catch (err) {
        if (err && err.name === 'AbortError') {
          const secs = Math.round(timeoutMs / 1000);
          const timeoutErr = new Error('Tiempo de espera agotado (' + secs + 's).');
          timeoutErr.isTimeout = true;
          throw timeoutErr;
        }
        throw err;
      } finally {
        clearTimeout(tid);
      }
    }

    function openNewProjectComposer() {
      const sidePanel = document.getElementById('sidebar-panel');
      if (sidePanel) sidePanel.classList.remove('sidebar-collapsed');
      const swBtn = document.getElementById('mobile-project-switcher');
      if (swBtn) swBtn.setAttribute('aria-expanded', 'true');
      const form = document.getElementById('new-project-form');
      const input = document.getElementById('new-project-input');
      if (form) form.style.display = 'block';
      if (input) input.focus();
    }

    function updateMobileSwitcherLabel() {
      const swBtn = document.getElementById('mobile-project-switcher');
      if (!swBtn) return;
      const count = state.projects.length;
      const active = state.projects.find((p) => p.project_id === state.selectedId) || state.projects[0] || null;
      const label = active
        ? ('Proyecto: ' + (active.name || active.objective || active.project_id) + ' ▾ (' + count + ')')
        : 'Sin proyectos ▾ (0)';
      swBtn.textContent = label;
    }

    function renderProjectList() {
      const container = document.getElementById('project-list');
      updateMobileSwitcherLabel();
      const listSig = JSON.stringify([
        state.selectedId,
        state.projects.map((p) => [p.project_id, p.name, p.state, Boolean(p.is_running || p.busy), p.last_event_at, p.logo_url]),
      ]);
      if (state.lastListSignature === listSig && container.children.length > 0) {
        return;
      }
      state.lastListSignature = listSig;

      if (!state.projects.length) {
        const sidePanel = document.getElementById('sidebar-panel');
        if (sidePanel) sidePanel.classList.remove('sidebar-collapsed');
        container.innerHTML =
          '<div class="empty-box">' +
            '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" style="opacity:0.65; margin-bottom:6px;" aria-hidden="true">' +
              '<rect x="3" y="4" width="18" height="16" rx="3"></rect>' +
              '<path d="M12 9v6M9 12h6" stroke-linecap="round"></path>' +
            '</svg>' +
            '<div style="font-weight:600; color:var(--text); margin-bottom:4px;">No hay proyectos registrados todavía.</div>' +
            '<div class="muted" style="margin-bottom:10px;">Empieza describiendo tu aplicación con tus propias palabras.</div>' +
            '<button id="empty-create-btn" class="btn btn-primary btn-sm" type="button">+ Crear mi primer proyecto</button>' +
          '</div>';
        const emptyBtn = document.getElementById('empty-create-btn');
        if (emptyBtn) {
          emptyBtn.addEventListener('click', openNewProjectComposer);
        }
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
          ? ' <span class="busy-pill"><span class="working-spinner" style="width:11px;height:11px;border-width:2px;vertical-align:-1px;margin-right:3px;" aria-hidden="true"></span>(en curso)</span>'
          : (pausedActive ? ' <span class="busy-pill" style="color:var(--warn)">(pausado)</span>' : '');
        const badgeCls = pausedActive ? 'badge-warn' : info.cls;
        const isDeliveredCard = p.state === 'DELIVERED' || p.state === 'COMPLETED';
        const segMode = isDeliveredCard
          ? 'delivered'
          : ((p.state === 'FAILED' || p.state === 'ERROR') ? 'error' : (pausedActive ? 'paused' : 'active'));
        const stageBarHtml = isDeliveredCard ? '' : renderStageSegments(info.stage, segMode);
        const cardExtraCls = isDeliveredCard ? ' delivered-card' : '';
        const cardLiveClock = (running && p.last_event_at)
          ? '<span class="card-live-timer" data-started-at="' + escapeHtml(p.op_started_at || p.last_event_at) + '" data-pid="' + escapeHtml(p.project_id) + '"></span> · '
          : '';
        const timeHtml = p.last_event_at
          ? '<div class="muted" style="font-size:0.75rem; margin-top:4px; text-align:right;" title="' + escapeHtml(p.last_event_at) + '">' + cardLiveClock + escapeHtml(formatFriendlyTime(p.last_event_at)) + '</div>'
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
          state.lastListSignature = null;
          state.lastBannerSignature = null;
          state.lastModifySignature = null;
          state.lastSpecSignature = null;
          state.lastEventsSignature = null;
          state.lastChatSig = null;
          const sidePanel = document.getElementById('sidebar-panel');
          if (sidePanel && window.innerWidth < 1024) {
            sidePanel.classList.add('sidebar-collapsed');
            const swBtn = document.getElementById('mobile-project-switcher');
            if (swBtn) swBtn.setAttribute('aria-expanded', 'false');
          }
          renderProjectList();
          loadDetail(state.selectedId);
          const detailPanel = document.getElementById('detail-panel');
          if (detailPanel && window.innerWidth < 1024) {
            detailPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
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
        'BACKGROUND_OP_STARTED': 'Operación en segundo plano',
        'BACKGROUND_OP_FINISHED': 'Operación finalizada',
        'DOCTOR_RECOVERY': 'Diagnóstico del Agente Doctor',
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
        case 'BACKGROUND_OP_STARTED':
          return String(pl.activity_summary || 'Inicio de operación en segundo plano');
        case 'BACKGROUND_OP_FINISHED':
          return (pl.status === 'interrupted')
            ? String(pl.reason || 'Operación interrumpida o desbloqueada')
            : 'Operación en segundo plano completada';
        case 'REGISTRY_PUBLISH_FAILED':
          return 'Aviso al publicar registro';
        case 'DOCTOR_RECOVERY':
          if (pl.recovered) {
            return '🩺 Agente Doctor: recuperación automática exitosa';
          }
          if (pl.scope === 'factory') {
            return '🩺 Agente Doctor: diagnóstico interno de la fábrica';
          }
          return '🩺 Agente Doctor: intento de recuperación (' + (pl.attempts || 0) + '/' + (pl.max_attempts || 2) + ')';
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
        case 'BACKGROUND_OP_STARTED':
        case 'BACKGROUND_OP_FINISHED':
          return pl.stage ? stateToPhaseNum(pl.stage) : 5;
        case 'DOCTOR_RECOVERY':
          return pl.stage ? stateToPhaseNum(pl.stage) : 4;
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
        if (
          (ev.type === 'STATE_TRANSITIONED' || ev.type === 'PROJECT_RETRIED')
          && ev.payload
          && ev.payload.to === 'INTERVIEW'
        ) {
          return [];
        }
        if (ev.type === 'INTERVIEW_ROUND' && ev.payload) {
          const raw = Array.isArray(ev.payload.questions) ? ev.payload.questions : [];
          return raw.map((q) => (typeof q === 'string' ? q.trim() : '')).filter(Boolean);
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

    function resolveOperationStartMs(st, pid, fallbackIso) {
      const targetPid = pid || (st && st.project_id) || state.selectedId;
      const startIso = (st && st.op_started_at)
        || fallbackIso
        || (st && (st.step_started_at || st.last_event_at))
        || '';
      const parsedMs = startIso ? Date.parse(startIso) : NaN;
      let startMs = isFinite(parsedMs) ? parsedMs : NaN;
      if (state.opHandoff && state.opHandoff.pid === targetPid && (Date.now() - state.opHandoff.endedAtMs) < 30000) {
        if (!isFinite(startMs) || startMs >= (state.opHandoff.startedAtMs - 1500)) {
          startMs = state.opHandoff.startedAtMs;
        }
      }
      return startMs;
    }

    function resolveOperationElapsedSeconds(st, pid, fallbackIso) {
      const startMs = resolveOperationStartMs(st, pid, fallbackIso);
      if (!isFinite(startMs)) return null;
      return Math.max(0, Math.floor((Date.now() - startMs) / 1000));
    }

    function formatHandoffElapsed(pid, startedIso) {
      const st = (state.detail && state.detail.status && (!pid || state.detail.status.project_id === pid))
        ? state.detail.status
        : null;
      const elapsedSecs = resolveOperationElapsedSeconds(st, pid, startedIso);
      if (elapsedSecs === null) return '';
      return formatClockMmSs(elapsedSecs);
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
      let stalled = false;
      if (state.activeOp && state.activeOp.startedAtMs) {
        label = state.activeOp.label || 'Procesando operación...';
        subtext = state.activeOp.subtext || '';
        startedAtMs = state.activeOp.startedAtMs;
        const httpSecs = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
        stalled = httpSecs >= STALL_HTTP_SECONDS;
      } else if (state.detail && state.detail.status && (state.detail.status.is_running || state.detail.status.busy)) {
        const st = state.detail.status;
        const refIso = st.op_started_at || st.step_started_at || st.last_event_at || '';
        const refMs = resolveOperationStartMs(st, st.project_id, refIso);
        const idleSecs = resolveOperationElapsedSeconds(st, st.project_id, refIso);
        const idleMins = idleSecs !== null ? (idleSecs / 60) : 0;
        if (idleMins >= STALL_BG_MINUTES) {
          label = st.activity_summary || friendlyStateName(st.state) || 'Procesando...';
          subtext = 'Sin cambios recientes en la etapa actual.';
          startedAtMs = refMs;
          stalled = true;
        } else {
          banner.style.display = 'none';
          banner.classList.remove('stall');
          stallEl.style.display = 'none';
          document.body.classList.remove('has-live-banner');
          return;
        }
      } else {
        state.bgOpPid = null;
        state.bgOpStartedAtMs = null;
      }

      if (!startedAtMs) {
        banner.style.display = 'none';
        banner.classList.remove('stall');
        stallEl.style.display = 'none';
        document.body.classList.remove('has-live-banner');
        timerEl.textContent = '00:00s';
        return;
      }

      const elapsedSecs = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
      banner.style.display = 'flex';
      document.body.classList.add('has-live-banner');
      textEl.textContent = label;
      subEl.textContent = subtext;
      timerEl.textContent = formatClockMmSs(elapsedSecs);
      banner.classList.toggle('stall', stalled);
      stallEl.style.display = stalled ? 'flex' : 'none';
    }

    function setButtonLoading(btnEl, loading, loadingText) {
      const btn = typeof btnEl === 'string' ? document.getElementById(btnEl) : btnEl;
      if (!btn || !document.body.contains(btn)) return;
      if (loading) {
        if (btn.dataset.origHtml === undefined) {
          btn.dataset.origHtml = btn.innerHTML;
        }
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        if (loadingText) {
          btn.innerHTML = '<span class="btn-spinner" aria-hidden="true"></span><span>' + escapeHtml(String(loadingText)) + '</span>';
        }
      } else {
        if (btn.dataset.origHtml !== undefined) {
          btn.innerHTML = btn.dataset.origHtml;
          delete btn.dataset.origHtml;
        }
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
      }
    }

    async function runTrackedOperation(opts, asyncFn) {
      const cfg = opts || {};
      const btn = typeof cfg.buttonEl === 'string'
        ? document.getElementById(cfg.buttonEl)
        : cfg.buttonEl;
      if (btn && btn.disabled) return undefined;
      const inlineEl = cfg.inlineTargetId ? document.getElementById(cfg.inlineTargetId) : null;
      const opStart = Date.now();
      setButtonLoading(btn, true, cfg.buttonLoadingText || cfg.label || 'Procesando...');
      if (inlineEl) {
        inlineEl.innerHTML =
          '<span class="inline-op-feedback">' +
            '<span class="working-spinner" style="width:14px;height:14px;" aria-hidden="true"></span>' +
            '<span>' + escapeHtml(cfg.label || 'Procesando...') + '</span> ' +
            '<span class="live-op-clock" data-op-clock-start="' + opStart + '">00:00</span>' +
          '</span>';
      }
      state.activeOp = {
        pid: cfg.pid || state.selectedId || null,
        label: cfg.label || 'Procesando...',
        subtext: cfg.subtext || '',
        startedAtMs: opStart,
      };
      state.lastBannerSignature = null;
      renderLiveActivityBanner();
      let hadError = false;
      try {
        return await asyncFn();
      } catch (err) {
        hadError = true;
        const errText = (err && err.message) ? err.message : String(err || 'Error inesperado.');
        setNote('Error: ' + errText, 'err');
        if (cfg.inlineTargetId) {
          showInlineError(cfg.inlineTargetId, errText, () => runTrackedOperation(cfg, asyncFn));
        }
        return undefined;
      } finally {
        const finishedOp = state.activeOp;
        state.activeOp = null;
        if (!hadError && finishedOp && finishedOp.pid) {
          state.opHandoff = {
            pid: finishedOp.pid,
            startedAtMs: opStart,
            endedAtMs: Date.now(),
          };
        }
        state.lastBannerSignature = null;
        renderLiveActivityBanner();
        if (!hadError) {
          const inlineAfter = cfg.inlineTargetId ? document.getElementById(cfg.inlineTargetId) : null;
          if (inlineAfter) inlineAfter.innerHTML = '';
        }
        const btnAfter = typeof cfg.buttonEl === 'string'
          ? document.getElementById(cfg.buttonEl)
          : cfg.buttonEl;
        setButtonLoading(btnAfter, false);
      }
    }

    (function initActivityBannerInteractions() {
      const reloadBtn = document.getElementById('live-activity-reload-btn');
      if (reloadBtn) {
        reloadBtn.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          const targetPid = (state.activeOp && state.activeOp.pid)
            || (state.detail && state.detail.status && state.detail.status.project_id)
            || state.selectedId;
          state.activeOp = null;
          state.bgOpStartedAtMs = null;
          state.opHandoff = null;
          state.lastBannerSignature = null;
          if (targetPid) {
            if (state.interviewPendingByPid) {
              state.interviewPendingByPid[targetPid] = false;
            }
            if (state.devMetaLoadedSig) {
              delete state.devMetaLoadedSig[targetPid];
            }
          }
          renderLiveActivityBanner();
          if (targetPid) {
            reloadBtn.disabled = true;
            setNote('Limpiando bloqueo y recargando estado del servidor...', 'info');
            try {
              const resp = await fetch('/api/projects/' + encodeURIComponent(targetPid) + '/recover', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
              });
              const data = await resp.json().catch(() => ({}));
              if (resp.ok && data && data.unlocked) {
                setNote('Estado desbloqueado y recargado desde el servidor.', 'ok');
              } else if (resp.ok) {
                setNote('Estado actualizado desde el servidor.', 'info');
              } else {
                setNote((data && data.error) || 'No se pudo recargar el estado.', 'err');
              }
            } catch (err) {
              setNote(err && err.message ? err.message : 'Error al recargar estado.', 'err');
            } finally {
              reloadBtn.disabled = false;
            }
          } else {
            setNote('Recargando estado del servidor...', 'info');
          }
          await loadProjects();
        });
      }
      const banner = document.getElementById('live-activity-banner');
      if (banner) {
        banner.addEventListener('click', () => {
          if (window.innerWidth <= 600) {
            banner.classList.toggle('expanded');
          }
        });
      }
    })();

    function updateLiveTimers() {
      document.querySelectorAll('.live-step-timer[data-started-at]').forEach((el) => {
        const startedAt = el.getAttribute('data-started-at');
        const pid = el.getAttribute('data-pid') || state.selectedId;
        const st = (state.detail && state.detail.status && (!pid || state.detail.status.project_id === pid))
          ? state.detail.status
          : null;
        const elapsedSecs = resolveOperationElapsedSeconds(st, pid, startedAt);
        if (elapsedSecs !== null) {
          el.textContent = 'Tiempo en este paso: ' + formatClockMmSs(elapsedSecs);
        }
      });
      document.querySelectorAll('.live-op-clock[data-op-clock-start]').forEach((el) => {
        const startMs = Number(el.getAttribute('data-op-clock-start'));
        if (Number.isFinite(startMs) && startMs > 0) {
          const secs = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
          const mins = Math.floor(secs / 60);
          el.textContent = String(mins).padStart(2, '0') + ':' + String(secs % 60).padStart(2, '0');
        }
      });
      document.querySelectorAll('.card-live-timer[data-started-at]').forEach((el) => {
        const startedAt = el.getAttribute('data-started-at');
        const startedMs = startedAt ? Date.parse(startedAt) : NaN;
        if (!Number.isNaN(startedMs)) {
          const secs = Math.max(0, Math.floor((Date.now() - startedMs) / 1000));
          const mins = Math.floor(secs / 60);
          el.textContent = String(mins).padStart(2, '0') + ':' + String(secs % 60).padStart(2, '0');
        }
      });
      renderLiveActivityBanner();
    }

    function renderDoctorRecoveryHtml(rec) {
      if (!rec || typeof rec !== 'object') return '';
      const attempts = Number(rec.attempts || 0);
      const maxAtt = Number(rec.max_attempts || 2);
      const usedLesson = Boolean(rec.used_lesson);
      const learnedLesson = Boolean(rec.learned_new_lesson);
      const isFactory = rec.scope === 'factory';
      const lessonType = rec.lesson_applied_type ? String(rec.lesson_applied_type) : '';
      let lessonText = 'Sin lección previa coincidente';
      if (usedLesson) {
        lessonText = 'Lección aplicada' + (lessonType ? (' (' + lessonType + ')') : '');
      } else if (learnedLesson) {
        lessonText = 'Nueva lección aprendida y guardada';
      } else if (isFactory) {
        lessonText = 'Solo lectura (error interno de fábrica)';
      }
      const attemptsBadge = isFactory
        ? '<span class="model-pill">0 modificaciones (solo lectura)</span>'
        : '<span class="model-pill">Intentos: ' + escapeHtml(String(attempts) + '/' + String(maxAtt)) + '</span>';
      const statusBadge = rec.recovered
        ? '<span class="test-badge ok">Auto-recuperado</span>'
        : '<span class="model-pill">' + escapeHtml(lessonText) + '</span>';
      const lessonPill = (rec.recovered && (usedLesson || learnedLesson))
        ? '<span class="model-pill">' + escapeHtml(lessonText) + '</span>'
        : '';
      const diagHtml = rec.diagnosis
        ? '<div class="round-summary" style="margin-top:4px;"><strong>Diagnóstico:</strong> ' + escapeHtml(String(rec.diagnosis)) + '</div>'
        : '';
      const promptHtml = (isFactory && rec.suggested_prompt)
        ? '<div class="doctor-prompt-box">' +
            '<div style="font-weight:600; font-size:0.82rem;">Prompt sugerido para reparar la fábrica:</div>' +
            '<pre class="doctor-prompt-pre">' + escapeHtml(String(rec.suggested_prompt)) + '</pre>' +
            '<div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:6px;">' +
              '<button type="button" class="btn btn-sm" data-copy-doctor-prompt="1">Copiar prompt sugerido</button>' +
              '<button type="button" class="btn btn-primary btn-sm" data-open-factory-improve="1">🏭 Enviar a Mejorar Fábrica</button>' +
            '</div>' +
          '</div>'
        : '';
      return '<div class="doctor-recovery-card">' +
        '<div class="doctor-recovery-badges">' +
          '<span style="font-weight:600;">🩺 Agente Doctor</span>' +
          statusBadge +
          lessonPill +
          attemptsBadge +
        '</div>' +
        diagHtml +
        promptHtml +
      '</div>';
    }

    function wireCopyDoctorPromptButtons(container) {
      if (!container) return;
      container.querySelectorAll('button[data-copy-doctor-prompt]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const box = btn.closest('.doctor-prompt-box');
          const pre = box ? box.querySelector('.doctor-prompt-pre') : null;
          const txt = pre ? pre.textContent : '';
          if (txt) {
            copyToClipboard(txt, '✓ Prompt sugerido copiado al portapapeles.');
          }
        });
      });
      container.querySelectorAll('button[data-open-factory-improve]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const box = btn.closest('.doctor-prompt-box');
          const pre = box ? box.querySelector('.doctor-prompt-pre') : null;
          const txt = pre ? pre.textContent : '';
          if (typeof window.openFactoryImproveModal === 'function') {
            window.openFactoryImproveModal(txt ? ('Diagnostica y prepara esta mejora interna de la fábrica:\\n\\n' + txt) : '');
          }
        });
      });
    }

    function buildGuidanceBanner(st, events, detail) {
      const isRunning = Boolean(st.is_running || st.busy);
      if (isRunning) {
        if (state.activeOp) {
          return '';
        }
        const isDeliveredBusy = st.state === 'DELIVERED' || st.state === 'COMPLETED';
        const rawActSummary = String(st.activity_summary || '').trim();
        const effSummary = isDeliveredBusy
          ? (rawActSummary && !/Analizando tu idea y preparando preguntas/i.test(rawActSummary)
              ? rawActSummary
              : 'Refinando calidad con debate de 2 agentes...')
          : rawActSummary;
        const descText = effSummary
          ? escapeHtml(effSummary)
          : 'Los agentes están avanzando en esta etapa. Esta pantalla se actualiza sola cada 5 segundos.';
        const subDescHtml = st.activity_detail
          ? '<div class="banner-subdesc tech-only">' + escapeHtml(st.activity_detail) + '</div>'
          : '';
        const hasTests = typeof st.tests_passed === 'number' && typeof st.tests_total === 'number' && st.tests_total > 0;
        const testsPending = hasTests && st.tests_passed < st.tests_total;
        const testsHtml = hasTests
          ? '<div class="banner-tests' + (testsPending ? ' pending' : '') + '">' +
              '<span class="simple-only">Verificaciones: ' + st.tests_passed + ' de ' + st.tests_total + (testsPending ? ' en curso' : ' ✓') + '</span>' +
              '<span class="tech-only">' + (testsPending ? '' : '✓ ') + st.tests_passed + ' de ' + st.tests_total + ' pruebas aprobadas</span>' +
            '</div>'
          : '';
        const agentsCount = Number(st.agents_count || (isDeliveredBusy ? 2 : 1));
        const agentsText = agentsCount === 2
          ? '2 agentes colaborando'
          : (agentsCount > 1 ? (agentsCount + ' agentes colaborando') : '1 agente trabajando');
        const agentsPill = '<span class="model-pill">' + escapeHtml(agentsText) + '</span>';
        const modelPill = st.active_model
          ? '<span class="model-pill tech-only">Modelo: ' + escapeHtml(st.active_model) + '</span>'
          : '';
        const timerOriginIso = st.op_started_at || st.step_started_at || '';
        const elapsedSecs = resolveOperationElapsedSeconds(st, st.project_id, timerOriginIso);
        const elapsedText = elapsedSecs !== null ? formatClockMmSs(elapsedSecs) : '';
        const timerPill = (timerOriginIso || elapsedSecs !== null)
          ? '<span class="model-pill live-step-timer" aria-hidden="true" data-pid="' + escapeHtml(st.project_id) + '" data-started-at="' + escapeHtml(timerOriginIso) + '">Tiempo en este paso: ' + escapeHtml(elapsedText || '00:00s') + '</span>'
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
        const d = (detail && typeof detail === 'object')
          ? detail
          : ((state.detail && state.detail.project_id === st.project_id) ? state.detail : {});
        const fallbackQs = (detail === undefined && !('questions' in d))
          ? extractLatestQuestions(events)
          : [];
        const cleanQuestions = (Array.isArray(d.questions) ? d.questions : fallbackQs)
          .map((q) => String(q || '').trim())
          .filter(Boolean);
        const questions = cleanQuestions;
        const inact = formatInactivityNote(st.last_event_at);
        const inactHtml = inact ? '<div class="inactivity-note">' + escapeHtml(inact) + '</div>' : '';
        const pendingHtml = (state.interviewPendingByPid && state.interviewPendingByPid[st.project_id])
          ? '<div class="optimistic-sent-pill">✓ Respuesta enviada · Analizando…</div>'
          : '';
        const draft = escapeHtml((state.replyDrafts && state.replyDrafts[st.project_id]) || '');
        if (!questions.length) {
          return '<div class="guidance-banner banner-warn">' +
            '<div class="banner-content">' +
              '<div class="banner-icon">⚠️</div>' +
              '<div class="banner-body">' +
                '<div class="banner-title">Aún no se han generado las preguntas de aclaración</div>' +
                '<div class="banner-desc">La entrevista inicial no produjo preguntas. Genera las preguntas de aclaración o escribe los detalles directamente abajo.</div>' +
                inactHtml +
                pendingHtml +
              '</div>' +
            '</div>' +
            '<div class="banner-actions" style="margin-bottom:10px;">' +
              '<button id="generate-questions-btn" class="btn btn-primary" type="button" data-dact="retry-interview">Generar preguntas ahora</button>' +
              '<span id="interview-retry-inline-status"></span>' +
            '</div>' +
            '<div class="banner-form">' +
              '<div class="input-row-with-mic">' +
                '<textarea id="interview-reply-input" class="input-field" rows="3" placeholder="Escribe los detalles de tu proyecto aquí (opcional sin preguntas)...">' + draft + '</textarea>' +
                '<button id="interview-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar respuesta por voz (es-MX)">🎤</button>' +
              '</div>' +
              '<div class="banner-actions">' +
                '<button id="interview-reply-btn" class="btn btn-primary" type="button">Enviar respuesta</button>' +
                '<span id="interview-inline-status"></span>' +
              '</div>' +
            '</div>' +
          '</div>';
        }
        return '<div class="guidance-banner banner-info">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">💬</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Esperando tus respuestas</div>' +
              '<ol class="question-list">' + questions.map((q) => '<li>' + escapeHtml(q) + '</li>').join('') + '</ol>' +
              inactHtml +
              pendingHtml +
            '</div>' +
          '</div>' +
          '<div class="banner-form">' +
            '<div class="input-row-with-mic">' +
              '<textarea id="interview-reply-input" class="input-field" rows="3" placeholder="Escribe tu respuesta aquí...">' + draft + '</textarea>' +
              '<button id="interview-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar respuesta por voz (es-MX)">🎤</button>' +
            '</div>' +
            '<div class="banner-actions">' +
              '<button id="interview-reply-btn" class="btn btn-primary" type="button">Enviar respuesta</button>' +
              '<span id="interview-inline-status"></span>' +
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
        const doctorHtml = renderDoctorRecoveryHtml(st.last_recovery);
        return '<div class="guidance-banner banner-err">' +
          '<div class="banner-content">' +
            '<div class="banner-icon">⚠️</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title">Se requiere atención</div>' +
              '<div class="banner-desc">' + escapeHtml(friendlyErr) + '</div>' +
              doctorHtml +
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
        const repoItem = (st.links && st.links.repo)
          ? '<a href="' + escapeHtml(st.links.repo) + '" target="_blank" rel="noopener noreferrer" class="more-menu-item">🔗 Abrir repositorio en GitHub</a>'
          : '';
        const deployItem = (localApp.app_type === 'static' && st.links && st.links.deploy)
          ? '<a href="' + escapeHtml(st.links.deploy) + '" target="_blank" rel="noopener noreferrer" class="more-menu-item">🌐 Ver despliegue en vivo</a>'
          : '';
        const deleteItem = '<button class="more-menu-item danger" type="button" data-dact="delete">🗑️ Eliminar proyecto</button>';
        const moreMenuHtml =
          '<div id="project-more-menu" class="more-menu-wrap">' +
            '<button id="project-more-btn" class="btn btn-sm" type="button" aria-expanded="false" aria-controls="project-more-popover">⋯ Más</button>' +
            '<div id="project-more-popover" class="more-popover" style="display:none">' +
              repoItem +
              deployItem +
              deleteItem +
            '</div>' +
          '</div>';
        const lanBtn = (localApp.running && localApp.lan_url)
          ? '<a href="' + escapeHtml(localApp.lan_url) + '" target="_blank" rel="noopener noreferrer" class="btn btn-sm">Abrir desde celular</a>' +
            '<span class="model-pill tech-only">' + escapeHtml(localApp.lan_url) + '</span>'
          : '';
        const prevBtn = (localApp.running && localApp.url)
          ? '<button id="toggle-preview-btn" class="btn btn-sm" type="button" data-dact="toggle-preview">' +
            (isPrevOpen ? 'Ocultar vista previa' : 'Mostrar vista previa aquí') + '</button>'
          : '';
        const localBtns = (localApp.running && localApp.url)
          ? '<a href="' + escapeHtml(localApp.url) + '" target="_blank" rel="noopener noreferrer" class="btn btn-success btn-sm">Abrir app en local</a>' +
            prevBtn +
            '<button class="btn btn-warn btn-sm" data-dact="local-stop">Apagar</button>' +
            '<span class="model-pill tech-only">' + escapeHtml(localApp.url) + '</span>' +
            lanBtn
          : '<button class="btn btn-primary" data-dact="local-start">Encender en local</button>';
        const descMsg = localApp.app_type === 'python_local'
          ? 'Servidor local Python listo para ejecutarse.'
          : 'Proyecto verificado y listo para usarse.';
        return '<div class="guidance-banner banner-ok delivered-compact">' +
          '<div class="delivered-compact-info">' +
            '<div class="banner-icon">🎉</div>' +
            '<div class="banner-body">' +
              '<div class="banner-title" style="margin-bottom:2px;">¡Proyecto terminado con éxito!</div>' +
              '<div class="banner-desc" style="font-size:var(--fs-xs);">' + escapeHtml(descMsg) + '</div>' +
              '<div id="local-inline-status"></div>' +
            '</div>' +
          '</div>' +
          '<div class="delivered-compact-actions">' +
            '<div class="local-controls-row">' + localBtns + '</div>' +
            moreMenuHtml +
          '</div>' +
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
        if (ev.type === 'STATE_TRANSITIONED' || ev.type === 'PRAGMATIST_PROPOSED' || ev.type === 'STRUCTURALIST_CRITIQUED' || ev.type === 'BACKGROUND_OP_STARTED' || ev.type === 'BACKGROUND_OP_FINISHED') {
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
      return '<div class="file-chips tech-only">' + files.map((f) =>
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
        const recObj = (ev && ev.payload && ev.payload.recovery) || (item.causeEv && item.causeEv.payload && item.causeEv.payload.recovery) || null;
        const doctorTimelineHtml = renderDoctorRecoveryHtml(recObj);
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
          doctorTimelineHtml +
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
        const ciBadge = item.ciEv ? '<span class="test-badge ok tech-only">CI ✓</span>' : '';
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

      const isSuccess = ev.type === 'PROJECT_DELIVERED' || ev.type === 'PROJECT_COMPLETED' || ev.type === 'COMPLETED' || (ev.type === 'DOCTOR_RECOVERY' && pl.recovered);
      const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
      const actionTitle = friendlyEventAction(ev);
      const doctorItemHtml = ev.type === 'DOCTOR_RECOVERY' ? renderDoctorRecoveryHtml(pl) : '';
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
        doctorItemHtml +
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
      badge.innerHTML =
        '<span class="badge-full">' + escapeHtml(info.step) + '</span>' +
        '<span class="badge-short">' + escapeHtml(info.cardStep || info.step) + '</span>';
      badge.className = 'badge ' + info.cls;
      updateMobileSwitcherLabel();
      const isRunning = Boolean(st.is_running || st.busy || (state.detail && state.detail.is_running));
      if (state.interviewPendingByPid[st.project_id]) {
        const sentAt = state.interviewPendingByPid[st.project_id];
        const lastEvMs = st.last_event_at ? Date.parse(st.last_event_at) : 0;
        if (st.state !== 'INTERVIEW' || isRunning || (isFinite(lastEvMs) && lastEvMs > sentAt + 2000)) {
          delete state.interviewPendingByPid[st.project_id];
        }
      }
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
      const opStartedAt = st.op_started_at || (state.detail && state.detail.op_started_at) || null;
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
        op_started_at: opStartedAt,
        step_started_at: stepStartedAt,
        tests_passed: testsPassed,
        tests_total: testsTotal,
        build_round: buildRound,
        max_rounds: maxRounds,
      });
      const rawDetailQs = Array.isArray(state.detail.questions)
        ? state.detail.questions
        : (('questions' in state.detail) ? [] : extractLatestQuestions(events));
      const questions = (Array.isArray(rawDetailQs) ? rawDetailQs : [])
        .map((q) => String(q || '').trim())
        .filter(Boolean);
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
        st.op_started_at,
        st.last_event_at,
        st.last_error,
        JSON.stringify(st.last_recovery || null),
        questions,
        inact,
        st.links,
        st.local_app,
        Boolean(state.previewOpen[st.project_id]),
        Boolean(state.interviewPendingByPid[st.project_id]),
        Boolean(state.activeOp),
        (state.checkpointsByPid[st.project_id] || []).length,
        (state.filesByPid[st.project_id] || []).length,
        Boolean(state.diffByPid[st.project_id]),
      ]);

      const guidanceEl = document.getElementById('detail-guidance');
      if (bannerSig !== state.lastBannerSignature) {
        guidanceEl.innerHTML = buildGuidanceBanner(statusForBanner, events, state.detail);
        state.lastBannerSignature = bannerSig;
        wireCopyDoctorPromptButtons(guidanceEl);

        guidanceEl.querySelectorAll('button[data-dact]').forEach((btn) => {
          btn.addEventListener('click', () => {
            closeFloatingDropdowns();
            handleAction(btn.dataset.dact, st.project_id, btn);
          });
        });
        const projectMoreBtn = document.getElementById('project-more-btn');
        const projectMorePop = document.getElementById('project-more-popover');
        if (projectMoreBtn && projectMorePop) {
          projectMoreBtn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            const isOpen = projectMorePop.style.display !== 'none';
            closeFloatingDropdowns();
            if (!isOpen) {
              projectMorePop.style.display = 'flex';
              projectMoreBtn.setAttribute('aria-expanded', 'true');
            }
          });
        }

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
            await sendProjectMessage(st.project_id, replyInput.value, 'reply', replyBtn);
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
            await sendProjectMessage(st.project_id, adjInput.value, 'adjust', adjBtn);
          });
        }
      } else {
        updateLiveTimers();
      }
      renderLivePreviewPanel(st);

      const headerMoreEl = document.getElementById('detail-header-more');
      const isDeliveredState = st.state === 'DELIVERED' || st.state === 'COMPLETED';
      if (headerMoreEl) {
        const headerMoreSig = String(st.project_id || '') + '|' + (isDeliveredState ? '1' : '0');
        if (headerMoreEl.dataset.sig !== headerMoreSig) {
          headerMoreEl.dataset.sig = headerMoreSig;
          if (isDeliveredState) {
            headerMoreEl.innerHTML = '';
          } else {
            headerMoreEl.innerHTML =
              '<div class="more-menu-wrap">' +
                '<button id="header-more-btn" class="btn btn-sm" type="button" aria-expanded="false" aria-controls="header-more-popover">⋯ Más</button>' +
                '<div id="header-more-popover" class="more-popover" style="display:none">' +
                  '<button class="more-menu-item danger" type="button" data-dact="delete">🗑️ Eliminar proyecto</button>' +
                '</div>' +
              '</div>';
            const hdrMoreBtn = document.getElementById('header-more-btn');
            const hdrMorePop = document.getElementById('header-more-popover');
            if (hdrMoreBtn && hdrMorePop) {
              hdrMoreBtn.addEventListener('click', (ev) => {
                ev.stopPropagation();
                const isOpen = hdrMorePop.style.display !== 'none';
                closeFloatingDropdowns();
                if (!isOpen) {
                  hdrMorePop.style.display = 'flex';
                  hdrMoreBtn.setAttribute('aria-expanded', 'true');
                }
              });
            }
            headerMoreEl.querySelectorAll('button[data-dact]').forEach((btn) => {
              btn.addEventListener('click', () => {
                closeFloatingDropdowns();
                handleAction(btn.dataset.dact, st.project_id, btn);
              });
            });
          }
        }
      }

      // Secondary footer cleared; delete option is grouped inside ⋯ Más menu
      const secEl = document.getElementById('detail-secondary');
      if (secEl) secEl.innerHTML = '';

      // Unified "Resumen y Plan" tab (signature-guarded to avoid 5s DOM flicker)
      const spec = state.detail.spec || {};
      const plan = state.detail.plan || null;
      const specSig = JSON.stringify([st.project_id, spec, plan]);
      if (state.lastSpecSignature !== specSig) {
        state.lastSpecSignature = specSig;
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
          fields.push('<div class="spec-section tech-only"><h4>Reglas de negocio</h4><ul>' + spec.business_rules.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
        }
        if (Array.isArray(spec.edge_cases) && spec.edge_cases.length) {
          fields.push('<div class="spec-section tech-only"><h4>Casos borde</h4><ul>' + spec.edge_cases.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
        }
        if (Array.isArray(spec.constraints) && spec.constraints.length) {
          fields.push('<div class="spec-section tech-only"><h4>Restricciones</h4><ul>' + spec.constraints.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
        }
        if (Array.isArray(spec.success_criteria) && spec.success_criteria.length) {
          fields.push('<div class="spec-section"><h4>Criterios de éxito</h4><ul>' + spec.success_criteria.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
        }
        if (Array.isArray(spec.assumptions) && spec.assumptions.length) {
          fields.push('<div class="spec-section tech-only"><h4>Supuestos</h4><ul>' + spec.assumptions.map((x) => '<li>' + escapeHtml(x) + '</li>').join('') + '</ul></div>');
        }
        const specHtml = fields.length
          ? fields.join('')
          : '<div class="empty-box">Aún estamos definiendo los detalles de este proyecto.</div>';

        let planHtml = '<div class="empty-box">Aún no se ha generado un Plan.</div>';
        if (plan) {
          const filesList = plan.files || [];
          const testsList = plan.acceptance_tests || [];
          const filesHtml = filesList.map((f) =>
            '<div class="file-card"><div class="file-path">' + escapeHtml(f.path) + '</div><div class="file-purpose">' + escapeHtml(f.purpose) + '</div>' +
            (f.contract ? '<details class="tech-details tech-only"><summary>Contrato</summary><pre>' + escapeHtml(f.contract) + '</pre></details>' : '') +
            '</div>'
          ).join('');
          const techTestsHtml = testsList.map((t) =>
            '<li><strong>[' + escapeHtml(t.id) + '] (' + escapeHtml(t.type) + ')</strong>: ' + escapeHtml(t.description) + '</li>'
          ).join('');
          const simpleTestsHtml = testsList.map((t) =>
            '<li>✓ ' + escapeHtml(t.description) + '</li>'
          ).join('');
          planHtml =
            '<div class="plan-summary"><strong>Resumen:</strong> ' + escapeHtml(plan.summary) + '</div>' +
            (plan.rationale ? '<div class="plan-summary tech-only"><strong>Justificación:</strong> ' + escapeHtml(plan.rationale) + '</div>' : '') +
            '<div class="plan-meta tech-only">' +
              '<span class="badge badge-info">Plantilla: ' + escapeHtml(plan.template_id) + '</span>' +
              '<span class="badge badge-info">Target: ' + escapeHtml(plan.deployment_target) + '</span>' +
            '</div>' +
            '<div class="simple-only">' +
              '<h4 style="margin:14px 0 8px 0;">Verificaciones: ' + testsList.length + ' comprobaciones automáticas ✓</h4>' +
              '<ul style="padding-left:20px; font-size:0.92rem;">' + simpleTestsHtml + '</ul>' +
            '</div>' +
            '<div class="tech-only">' +
              '<h4 style="margin:14px 0 8px 0;">Archivos (' + filesList.length + ')</h4>' + filesHtml +
              '<h4 style="margin:14px 0 8px 0;">Pruebas de aceptación (' + testsList.length + ')</h4><ul style="padding-left:20px; font-size:0.92rem;">' + techTestsHtml + '</ul>' +
            '</div>';
        }
        document.getElementById('tab-spec').innerHTML =
          specHtml +
          '<div class="tab-section-divider">' +
            '<h4 class="simple-only" style="margin:0 0 12px 0; font-size:1rem;">Qué se va a construir</h4>' +
            '<h4 class="tech-only" style="margin:0 0 12px 0; font-size:1rem;">Plan de construcción</h4>' +
            planHtml +
          '</div>';
      }

      const isDelivered = st.state === 'DELIVERED' || st.state === 'COMPLETED';
      const wasDeliveredBefore = isDelivered || events.some((ev) =>
        ev && (ev.type === 'PROJECT_DELIVERED' || ev.type === 'PROJECT_COMPLETED' || ev.type === 'COMPLETED')
      );
      renderEventsTab(st, events, info, isRunning, activeModel, wasDeliveredBefore);

      const readOnlyAuditModifying = wasDeliveredBefore && !isDelivered;
      const auditTabBtn = document.getElementById('tab-audit-btn');
      const modifyTabBtn = document.getElementById('tab-modify-btn');
      const ideasTabBtn = document.getElementById('tab-ideas-btn');
      if (ideasTabBtn) ideasTabBtn.style.display = st.project_id ? '' : 'none';
      if (st.project_id && typeof window.loadProjectIdeasFor === 'function') {
        if (!state.projectIdeasLoadedFor[st.project_id]) {
          window.loadProjectIdeasFor(st.project_id);
        } else if (typeof window.renderProjectIdeasTab === 'function') {
          window.renderProjectIdeasTab(st.project_id);
        }
      }
      if (isDelivered || readOnlyAuditModifying) {
        if (auditTabBtn) auditTabBtn.style.display = '';
        if (modifyTabBtn) modifyTabBtn.style.display = isDelivered ? '' : 'none';
        if (isDelivered && !(st.project_id in state.userChoseTabByPid)) {
          state.activeTab = 'audit';
        } else if (!isDelivered && state.activeTab === 'modify') {
          state.activeTab = 'spec';
        }
        const hasRunningAudit = (state.chatHistories[st.project_id] || []).some(
          (m) => m && m.status === 'running'
        );
        if (!state.chatLoadedFor[st.project_id] || (hasRunningAudit && !state.activeOp)) {
          loadProjectChatHistory(st.project_id);
        }
        loadProjectDevAndVersionData(st.project_id, st.last_event_at || String(events.length));
        renderAuditChatTab(st.project_id, readOnlyAuditModifying);
        if (isDelivered) {
          renderModifyTab(st);
        }
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

    function buildVersionDebateBadgeHtml(sc, cp) {
      if (!sc || typeof sc !== 'object') return '';
      const roundsArr = Array.isArray(sc.debate_rounds) ? sc.debate_rounds : [];
      const ds = (sc.debate_summary && typeof sc.debate_summary === 'object')
        ? sc.debate_summary
        : (cp && cp.debate_summary && typeof cp.debate_summary === 'object' ? cp.debate_summary : null);
      if (!roundsArr.length && (!ds || !Number(ds.rounds || 0))) {
        return '';
      }
      const resolvedCount = ds && typeof ds.resolved_count === 'number'
        ? Number(ds.resolved_count)
        : roundsArr.reduce((acc, r) => acc + (r && r.kept !== false ? Number(r.resolved_count || 0) : 0), 0);
      const keptRounds = ds && typeof ds.kept_rounds === 'number'
        ? Number(ds.kept_rounds)
        : roundsArr.filter((r) => r && r.kept !== false).length;
      const impNum = resolvedCount || keptRounds || roundsArr.length;
      let deltaNum = 0;
      if (ds && typeof ds.score_delta === 'number') {
        deltaNum = Math.round(ds.score_delta);
      } else if (roundsArr.length) {
        deltaNum = Math.round(Number(sc.global_score || 0) - Number(roundsArr[0].score_before || 0));
      }
      const deltaTxt = (deltaNum >= 0 ? ('+' + deltaNum) : String(deltaNum)) + '%';
      return '<span class="badge badge-info" title="Debate Crítico Adversario vs. Constructor">' +
        '⚔️ Debate 2 agentes: ' + escapeHtml(String(impNum)) + ' mejoras · ' + escapeHtml(deltaTxt) +
      '</span>';
    }

    function buildHistoryVersionsSectionHtml(st, cps, wasDeliveredBefore, isRunning) {
      if (!wasDeliveredBefore && (!Array.isArray(cps) || !cps.length)) {
        return '';
      }
      const isReadOnly = Boolean(
        isRunning || (st && st.state !== 'DELIVERED' && st.state !== 'COMPLETED')
      );
      const busyAttr = isReadOnly ? ' disabled title="Disponible al finalizar la operación en curso"' : '';
      let bodyHtml = '';
      if (!Array.isArray(cps) || !cps.length) {
        bodyHtml = '<div class="muted" style="font-size:var(--fs-xs);">Cargando historial de versiones…</div>';
      } else {
        bodyHtml = '<div class="history-versions-list">' + cps.map((c) => {
          const sc = (c && c.quality_scorecard && typeof c.quality_scorecard === 'object')
            ? c.quality_scorecard
            : null;
          const tier = classifyScorecardTier(sc);
          const tierText = sc ? (tier.label + ' · ' + tier.pct + '%') : tier.label;
          const debateBadgeHtml = buildVersionDebateBadgeHtml(sc, c);
          const shaShort = c.short_sha || (c.commit_sha ? String(c.commit_sha).slice(0, 7) : '');
          const timeLbl = formatFriendlyTime(c.delivered_at);
          const curBadge = c.is_current ? '<span class="badge badge-ok">Actual</span>' : '';
          const rbTag = c.rollback_from
            ? '<span class="model-pill">Restaurado desde ' + escapeHtml(String(c.rollback_from).slice(0, 7)) + '</span>'
            : '';
          const barsHtml = (sc && Array.isArray(sc.areas) && sc.areas.length)
            ? '<div class="version-quality-bars">' + buildQualityAreaBarsHtml(sc) + '</div>'
            : '<div class="muted" style="font-size:var(--fs-xs);">Sin evaluación detallada por áreas en esta versión.</div>';
          const shaHtml = shaShort
            ? '<span class="version-sha-wrap tech-only">' +
                '<code class="file-chip">' + escapeHtml(shaShort) + '</code>' +
                '<button class="btn btn-sm" type="button" data-copy-version-sha="' + escapeHtml(shaShort) + '">Copiar hash</button>' +
              '</span>'
            : '';
          const polishNote = (c && c.last_polish_note)
            ? String(c.last_polish_note).trim()
            : ((sc && sc.last_polish_note) ? String(sc.last_polish_note).trim() : '');
          const polishNoteHtml = (c.is_current && polishNote)
            ? '<div class="version-polish-note" role="status">ℹ️ ' + escapeHtml(polishNote) + '</div>'
            : '';
          const actionBtnsHtml = c.is_current
            ? '<div class="audit-primary-split">' +
                '<button id="polish-debate-btn" class="btn btn-primary btn-sm" type="button" data-quality-action="polish"' + busyAttr + '>⚔️ Pulir con debate</button>' +
                '<div class="more-menu-wrap">' +
                  '<button id="quality-more-actions-btn" class="btn btn-sm" type="button" aria-expanded="false" aria-controls="quality-more-actions-menu" title="Más opciones de calidad"' + busyAttr + '>▾</button>' +
                  '<div id="quality-more-actions-menu" class="more-popover" style="display:none">' +
                    '<button id="evaluate-quality-btn" class="more-menu-item" type="button" data-quality-action="evaluate"' + busyAttr + '>🛡️ Re-evaluar calidad</button>' +
                    '<button id="improve-quality-btn" class="more-menu-item" type="button" data-quality-action="improve"' + busyAttr + '>✨ Mejorar hasta cumplir estándar</button>' +
                  '</div>' +
                '</div>' +
              '</div>'
            : (c.commit_sha
                ? '<button class="btn btn-sm btn-warn" type="button" data-history-rollback-sha="' + escapeHtml(c.commit_sha) + '"' + busyAttr + '>↩︎ Volver a esta versión</button>'
                : '');
          return '<div class="version-node' + (c.is_current ? ' current' : '') + '">' +
            '<div class="version-dot"></div>' +
            '<div class="version-card' + (c.is_current ? ' current-version' : '') + '">' +
              '<div class="version-head">' +
                '<span class="version-quality-row">' +
                  '<strong>' + escapeHtml(c.version_label || 'v1') + '</strong>' +
                  curBadge +
                  '<span class="badge ' + tier.cls + '">' + escapeHtml(tierText) + '</span>' +
                  debateBadgeHtml +
                  rbTag +
                '</span>' +
                '<span class="muted" style="font-size:var(--fs-xs);">' + escapeHtml(timeLbl) + '</span>' +
              '</div>' +
              (c.summary ? '<div class="muted" style="font-size:var(--fs-xs);">' + escapeHtml(c.summary) + '</div>' : '') +
              polishNoteHtml +
              barsHtml +
              '<div class="version-actions">' +
                actionBtnsHtml +
                shaHtml +
              '</div>' +
            '</div>' +
          '</div>';
        }).join('') + '</div>';
      }
      return '<section id="history-versions-section" class="history-versions-section">' +
        '<div class="history-versions-header">' +
          '<h4>Historial de versiones</h4>' +
          '<span id="history-quality-inline-status"></span>' +
        '</div>' +
        bodyHtml +
      '</section>';
    }

    function renderEventsTab(st, events, info, isRunning, activeModel, wasDeliveredBefore) {
      if (!st) return;
      const isDelivered = st.state === 'DELIVERED' || st.state === 'COMPLETED';
      const isFailed = st.state === 'FAILED' || st.state === 'ERROR';
      const cps = (state.checkpointsByPid && state.checkpointsByPid[st.project_id]) || [];
      const eventsSig = JSON.stringify([
        st.project_id,
        st.state,
        isRunning,
        activeModel,
        events.length,
        st.op_started_at,
        st.last_event_at,
        st.last_error,
        info.stage,
        Boolean(wasDeliveredBefore),
        cps.map((c) => [
          c.version_label,
          c.commit_sha,
          c.is_current,
          c.summary,
          c.quality_scorecard && c.quality_scorecard.global_score,
          c.quality_scorecard && c.quality_scorecard.tier_id,
          c.quality_scorecard && Array.isArray(c.quality_scorecard.debate_rounds) ? c.quality_scorecard.debate_rounds.length : 0,
          c.last_polish_note || (c.quality_scorecard && c.quality_scorecard.last_polish_note) || '',
        ]),
      ]);
      if (state.lastEventsSignature === eventsSig) {
        return;
      }
      state.lastEventsSignature = eventsSig;
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

      const versionsSectionHtml = buildHistoryVersionsSectionHtml(st, cps, wasDeliveredBefore, isRunning);
      const toolbarHtml = '<div class="history-toolbar">' +
        '<span class="muted tech-only">ID: <code>' + escapeHtml(st.project_id) + '</code></span>' +
        '<button id="copy-diag-btn" class="btn btn-sm tech-only" type="button">Copiar diagnóstico</button>' +
      '</div>';

      const eventsEl = document.getElementById('tab-events');
      eventsEl.innerHTML = versionsSectionHtml + toolbarHtml + phasesHtml + priorHtml;
      wireCopyDoctorPromptButtons(eventsEl);

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

      const qualityMoreBtn = eventsEl.querySelector('#quality-more-actions-btn');
      const qualityMoreMenu = eventsEl.querySelector('#quality-more-actions-menu');
      if (qualityMoreBtn && qualityMoreMenu && !isRunning) {
        qualityMoreBtn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          const isOpen = qualityMoreMenu.style.display !== 'none';
          closeFloatingDropdowns();
          if (!isOpen) {
            qualityMoreMenu.style.display = 'flex';
            qualityMoreBtn.setAttribute('aria-expanded', 'true');
          }
        });
      }
      const evalBtn = eventsEl.querySelector('button[data-quality-action="evaluate"]');
      if (evalBtn) {
        evalBtn.addEventListener('click', () => {
          closeFloatingDropdowns();
          submitEvaluateQuality(st.project_id, evalBtn);
        });
      }
      const improveBtn = eventsEl.querySelector('button[data-quality-action="improve"]');
      if (improveBtn) {
        improveBtn.addEventListener('click', () => {
          closeFloatingDropdowns();
          submitImproveQuality(st.project_id, improveBtn);
        });
      }
      const polishBtn = eventsEl.querySelector('button[data-quality-action="polish"]');
      if (polishBtn) {
        polishBtn.addEventListener('click', () => {
          closeFloatingDropdowns();
          submitPolishWithDebate(st.project_id, polishBtn);
        });
      }
      eventsEl.querySelectorAll('button[data-history-rollback-sha]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const sha = btn.getAttribute('data-history-rollback-sha');
          if (!sha) return;
          submitRollbackProject(st.project_id, sha, btn, 'history-quality-inline-status');
        });
      });
      eventsEl.querySelectorAll('button[data-copy-version-sha]').forEach((btn) => {
        btn.addEventListener('click', () => {
          copyToClipboard(btn.getAttribute('data-copy-version-sha'), '✓ Hash copiado al portapapeles.');
        });
      });

      const copyDiagBtn = document.getElementById('copy-diag-btn');
      if (copyDiagBtn) {
        copyDiagBtn.addEventListener('click', () => {
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
          copyToClipboard(diagPayload, '✓ Diagnóstico copiado al portapapeles.');
        });
      }
    }

    function unquoteGitDiffPath(raw) {
      let p = String(raw || '').trim();
      if (p.length >= 2 && p.startsWith('"') && p.endsWith('"')) {
        p = p.slice(1, -1).split('\\\\"').join('"');
      }
      if (p.startsWith('a/') || p.startsWith('b/')) {
        p = p.slice(2);
      }
      return p;
    }

    function parseUnifiedDiffFiles(rawDiff) {
      const text = String(rawDiff || '').replace(/\\r\\n?/g, '\\n').trim();
      if (!text) return [];
      const rawChunks = text.split(/^diff --git /m).filter((c) => c.trim());
      if (!text.startsWith('diff --git ') && rawChunks.length === 1) {
        const lines = text.split('\\n');
        let adds = 0;
        let dels = 0;
        lines.forEach((l) => {
          if (l.startsWith('+') && !l.startsWith('+++')) adds++;
          else if (l.startsWith('-') && !l.startsWith('---')) dels++;
        });
        return [{ path: 'Cambios recientes', adds, dels, lines, isRenamed: false, isBinary: false, tag: '', renameFrom: '' }];
      }
      return rawChunks.map((chunk) => {
        const lines = ('diff --git ' + chunk).split('\\n');
        const hdr = lines[0] || '';
        let filePath = 'archivo';
        const renameFromLine = lines.find((l) => l.startsWith('rename from '));
        const renameToLine = lines.find((l) => l.startsWith('rename to '));
        const renameFrom = renameFromLine ? unquoteGitDiffPath(renameFromLine.slice(12)) : '';
        const renameTo = renameToLine ? unquoteGitDiffPath(renameToLine.slice(10)) : '';
        const isRenamed = Boolean(renameFrom || renameTo);
        const isBinary = lines.some((l) => /^Binary files .+ differ$/i.test(l.trim()) || l.trim() === 'GIT binary patch');
        const quotedHdrMatch = /^diff --git ("a\\/[^"]+"|a\\/\\S+) ("b\\/[^"]+"|b\\/\\S+)$/.exec(hdr);
        const hdrMatch = quotedHdrMatch || /^diff --git a\\/([^\\n]+?) b\\/([^\\n]+)$/.exec(hdr);
        if (renameTo) {
          filePath = renameTo;
        } else {
          const plusLine = lines.find((l) => l.startsWith('+++ ') && l !== '+++ /dev/null');
          const minusLine = lines.find((l) => l.startsWith('--- ') && l !== '--- /dev/null');
          if (plusLine) {
            filePath = unquoteGitDiffPath(plusLine.slice(4));
          } else if (hdrMatch && hdrMatch[2]) {
            filePath = unquoteGitDiffPath(hdrMatch[2]);
          } else if (minusLine) {
            filePath = unquoteGitDiffPath(minusLine.slice(4));
          }
        }
        let adds = 0;
        let dels = 0;
        lines.forEach((l) => {
          if (l.startsWith('+') && !l.startsWith('+++')) adds++;
          else if (l.startsWith('-') && !l.startsWith('---')) dels++;
        });
        const tags = [];
        if (isRenamed) tags.push('Renombrado');
        if (isBinary) tags.push('Binario');
        return { path: filePath, adds, dels, lines, isRenamed, isBinary, tag: tags.join(' · '), renameFrom };
      });
    }

    function renderDiffLineHtml(line) {
      let cls = 'diff-line';
      if (line.startsWith('+') && !line.startsWith('+++')) cls += ' diff-line-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls += ' diff-line-del';
      else if (line.startsWith('@@')) cls += ' diff-line-hunk';
      return '<div class="' + cls + '">' + (line ? escapeHtml(line) : ' ') + '</div>';
    }

    function buildTargetFilesGroupsHtml(filesList, selSet, query) {
      const q = String(query || '').trim().toLowerCase();
      const filtered = q
        ? filesList.filter((fp) => fp.toLowerCase().indexOf(q) >= 0)
        : filesList;
      if (!filtered.length) {
        return '<div class="muted" style="font-size:var(--fs-xs); padding:4px;">Sin archivos que coincidan con "' + escapeHtml(query) + '".</div>';
      }
      const groups = {};
      filtered.forEach((fp) => {
        const slash = fp.lastIndexOf('/');
        const folder = slash >= 0 ? fp.slice(0, slash) : '(raíz)';
        if (!groups[folder]) groups[folder] = [];
        groups[folder].push(fp);
      });
      return Object.keys(groups).sort().map((folder) => {
        const rows = groups[folder].map((fp) =>
          '<label class="file-check-row">' +
            '<input type="checkbox" data-target-file-cb="' + escapeHtml(fp) + '"' + (selSet.has(fp) ? ' checked' : '') + ' />' +
            '<span>' + escapeHtml(fp) + '</span>' +
          '</label>'
        ).join('');
        return '<div class="target-folder-group">' +
          '<div class="target-folder-title">📁 ' + escapeHtml(folder) + '</div>' +
          rows +
        '</div>';
      }).join('');
    }

    function buildTargetChipsHtml(selArr) {
      if (!selArr.length) return '';
      return selArr.map((fp) =>
        '<span class="target-chip">' +
          '<span>' + escapeHtml(fp) + '</span>' +
          '<button type="button" data-remove-target-file="' + escapeHtml(fp) + '" aria-label="Quitar ' + escapeHtml(fp) + '">✕</button>' +
        '</span>'
      ).join('');
    }

    function computeModifySignature(pid) {
      const filesList = (state.filesByPid && state.filesByPid[pid]) || [];
      const selArr = (state.targetFilesSelected && state.targetFilesSelected[pid]) || [];
      const cps = (state.checkpointsByPid && state.checkpointsByPid[pid]) || [];
      const validCps = cps.filter((c) => c && c.commit_sha);
      const diffInfo = (state.diffByPid && state.diffByPid[pid]) || null;
      return JSON.stringify([
        pid,
        filesList.length,
        selArr,
        validCps.map((c) => c.commit_sha + ':' + Boolean(c.is_current)),
        diffInfo && diffInfo.diff ? diffInfo.diff.length : 0,
        Boolean(state.filesOpen && state.filesOpen[pid]),
        Boolean(state.rollbackOpen && state.rollbackOpen[pid]),
        Boolean(state.diffOpen && state.diffOpen[pid]),
      ]);
    }

    function renderModifyTab(st) {
      const container = document.getElementById('tab-modify');
      if (!container || !st) return;
      const pid = st.project_id;
      const modifyDraft = escapeHtml((state.modifyDrafts && state.modifyDrafts[pid]) || '');
      const filesList = (state.filesByPid && state.filesByPid[pid]) || [];
      const selArr = (state.targetFilesSelected && state.targetFilesSelected[pid]) || [];
      const selSet = new Set(selArr);
      const fileQuery = (state.targetFilesQueryByPid && state.targetFilesQueryByPid[pid]) || '';
      const cps = (state.checkpointsByPid && state.checkpointsByPid[pid]) || [];
      const validCps = cps.filter((c) => c && c.commit_sha);
      const diffInfo = (state.diffByPid && state.diffByPid[pid]) || null;

      const modSig = computeModifySignature(pid);
      if (state.lastModifySignature === modSig && container.querySelector('#modify-project-input')) {
        return;
      }
      state.lastModifySignature = modSig;

      const fileOptsHtml = filesList.map((fp) =>
        '<option value="' + escapeHtml(fp) + '"' + (selSet.has(fp) ? ' selected' : '') + '>' + escapeHtml(fp) + '</option>'
      ).join('');
      const rollbackDisplay = validCps.length === 0 ? ' style="display:none"' : '';
      const curCp = validCps.find((c) => c.is_current) || validCps[0] || null;
      const prevCp = validCps.find((c) => !c.is_current && (!curCp || c.commit_sha !== curCp.commit_sha)) || null;
      const curShaShort = (curCp && (curCp.short_sha || String(curCp.commit_sha).slice(0, 7))) || '';
      const diffFiles = diffInfo && diffInfo.diff ? parseUnifiedDiffFiles(diffInfo.diff) : [];
      const diffViewerHtml = diffFiles.length
        ? diffFiles.map((df) => {
            const tagHtml = df.tag ? '<span class="model-pill">' + escapeHtml(df.tag) + '</span>' : '';
            const renameSubHtml = df.renameFrom && df.renameFrom !== df.path
              ? '<span class="muted">(' + escapeHtml(df.renameFrom) + ' → ' + escapeHtml(df.path) + ')</span>'
              : '';
            const statsHtml = df.isBinary
              ? '<span class="muted">Archivo binario</span>'
              : '<span class="diff-add-stat">+' + df.adds + '</span>' +
                '<span class="diff-del-stat">−' + df.dels + '</span>';
            return '<details class="diff-file-card" open>' +
              '<summary class="diff-file-head">' +
                '<span class="diff-stats">' +
                  '<strong>' + escapeHtml(df.path) + '</strong>' +
                  renameSubHtml +
                  tagHtml +
                  statsHtml +
                '</span>' +
                '<span style="display:inline-flex; gap:6px;">' +
                  '<button type="button" class="btn btn-sm" data-copy-diff-path="' + escapeHtml(df.path) + '">Copiar ruta</button>' +
                  (curShaShort ? '<button type="button" class="btn btn-sm" data-copy-diff-hash="' + escapeHtml(curShaShort) + '">Copiar hash</button>' : '') +
                '</span>' +
              '</summary>' +
              '<div class="diff-lines-scroll">' + df.lines.map(renderDiffLineHtml).join('') + '</div>' +
            '</details>';
          }).join('')
        : '<div class="muted" style="font-size:var(--fs-xs); padding:8px 0;">Sin cambios recientes registrados o sin repositorio Git.</div>';

      const timelineHtml = validCps.length
        ? validCps.map((c) => {
            const shaShort = c.short_sha || String(c.commit_sha || '').slice(0, 7);
            const timeLbl = formatFriendlyTime(c.delivered_at);
            const rbTag = c.rollback_from
              ? '<span class="model-pill">Restaurado desde ' + escapeHtml(String(c.rollback_from).slice(0, 7)) + '</span>'
              : '';
            const curBadge = c.is_current
              ? '<span class="badge badge-ok">Actual</span>'
              : '';
            const restoreBtnHtml = c.is_current
              ? '<button class="btn btn-sm" type="button" disabled title="Versión actual en uso">Restaurar esta versión</button>'
              : '<button class="btn btn-sm btn-warn" type="button" data-rollback-sha="' + escapeHtml(c.commit_sha) + '">Restaurar esta versión</button>';
            return '<div class="version-node' + (c.is_current ? ' current' : '') + '">' +
              '<div class="version-dot"></div>' +
              '<div class="version-card">' +
                '<div class="version-head">' +
                  '<span style="display:inline-flex; align-items:center; gap:6px; flex-wrap:wrap;">' +
                    '<strong>' + escapeHtml(c.version_label) + '</strong>' +
                    curBadge +
                    rbTag +
                  '</span>' +
                  '<span class="muted" style="font-size:var(--fs-xs);">' + escapeHtml(timeLbl) + '</span>' +
                '</div>' +
                (c.summary ? '<div class="muted" style="font-size:var(--fs-xs);">' + escapeHtml(c.summary) + '</div>' : '') +
                '<div class="version-actions">' +
                  '<code class="file-chip">' + escapeHtml(shaShort) + '</code>' +
                  '<button class="btn btn-sm" type="button" data-copy-version-sha="' + escapeHtml(shaShort) + '">Copiar hash</button>' +
                  restoreBtnHtml +
                '</div>' +
              '</div>' +
            '</div>';
          }).join('')
        : '<div class="muted" style="font-size:var(--fs-xs);">Sin checkpoints Git registrados.</div>';

      const filesOpenAttr = (state.filesOpen && state.filesOpen[pid]) ? ' open' : '';
      const rollbackOpenAttr = (state.rollbackOpen && state.rollbackOpen[pid]) ? ' open' : '';
      const diffOpenAttr = (state.diffOpen && state.diffOpen[pid]) ? ' open' : '';
      const simpleRbHtml =
        '<div id="simple-rollback-wrap" class="simple-rollback-bar simple-only"' + (prevCp ? '' : ' style="display:none"') + '>' +
          '<div>' +
            '<div style="font-weight:600; font-size:var(--fs-sm);">¿Algo no quedó como esperabas?</div>' +
            '<div class="muted" style="font-size:var(--fs-xs);">Vuelve con un clic a la versión estable anterior (' + escapeHtml(prevCp ? prevCp.version_label : '') + ').</div>' +
          '</div>' +
          '<button id="simple-rollback-btn" class="btn btn-sm" type="button" data-sha="' + escapeHtml(prevCp ? prevCp.commit_sha : '') + '">↩︎ Volver a la versión anterior</button>' +
        '</div>';

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
          simpleRbHtml +
          '<details id="modify-files-details" class="tech-details tech-only"' + filesOpenAttr + '>' +
            '<summary>Archivos objetivo (opcional)' + (selSet.size ? (' · ' + selSet.size + ' seleccionado(s)') : '') + '</summary>' +
            '<div id="modify-target-files-wrap" class="target-picker">' +
              '<input id="target-files-search" class="input-field" type="search" placeholder="Buscar archivos por nombre o carpeta..." value="' + escapeHtml(fileQuery) + '" />' +
              '<div id="target-files-chips" class="target-chips">' + buildTargetChipsHtml(selArr) + '</div>' +
              '<div id="target-files-checklist" class="target-checklist">' + buildTargetFilesGroupsHtml(filesList, selSet, fileQuery) + '</div>' +
              '<select id="modify-target-files" class="sr-only" multiple tabindex="-1" aria-hidden="true">' + fileOptsHtml + '</select>' +
            '</div>' +
          '</details>' +
          '<details id="rollback-details" class="tech-details tech-only"' + rollbackDisplay + rollbackOpenAttr + '>' +
            '<summary>Versiones estables (Rollback)' + (curShaShort ? (' · HEAD: ' + escapeHtml(curShaShort)) : '') + '</summary>' +
            '<div id="versions-timeline">' + timelineHtml + '</div>' +
          '</details>' +
          '<details id="git-diff-details" class="tech-details tech-only"' + diffOpenAttr + '>' +
            '<summary>Últimos cambios (git diff)</summary>' +
            '<div id="git-diff-viewer">' + diffViewerHtml + '</div>' +
          '</details>' +
        '</div>';

      const modifyInput = document.getElementById('modify-project-input');
      const modifyBtn = document.getElementById('modify-project-btn');
      const targetSelect = document.getElementById('modify-target-files');
      const searchInput = document.getElementById('target-files-search');
      const chipsWrap = document.getElementById('target-files-chips');
      const checklistEl = document.getElementById('target-files-checklist');

      function syncTargetFilesSelection(nextArr) {
        const deduped = Array.from(new Set(nextArr.filter(Boolean)));
        state.targetFilesSelected[pid] = deduped;
        state.lastModifySignature = computeModifySignature(pid);
        const nextSet = new Set(deduped);
        if (targetSelect) {
          Array.from(targetSelect.options).forEach((opt) => {
            opt.selected = nextSet.has(opt.value);
          });
        }
        if (chipsWrap) {
          chipsWrap.innerHTML = buildTargetChipsHtml(deduped);
          chipsWrap.querySelectorAll('button[data-remove-target-file]').forEach((rmBtn) => {
            rmBtn.addEventListener('click', () => {
              const targetFp = rmBtn.getAttribute('data-remove-target-file');
              syncTargetFilesSelection((state.targetFilesSelected[pid] || []).filter((x) => x !== targetFp));
            });
          });
        }
        if (checklistEl) {
          checklistEl.querySelectorAll('input[data-target-file-cb]').forEach((cb) => {
            cb.checked = nextSet.has(cb.getAttribute('data-target-file-cb'));
          });
        }
        const detSum = container.querySelector('#modify-files-details > summary');
        if (detSum) {
          detSum.textContent = 'Archivos objetivo (opcional)' + (deduped.length ? (' · ' + deduped.length + ' seleccionado(s)') : '');
        }
      }

      function wireChecklistInputs() {
        if (!checklistEl) return;
        checklistEl.querySelectorAll('input[data-target-file-cb]').forEach((cb) => {
          cb.addEventListener('change', () => {
            const fp = cb.getAttribute('data-target-file-cb');
            const cur = state.targetFilesSelected[pid] || [];
            if (cb.checked) {
              syncTargetFilesSelection([...cur, fp]);
            } else {
              syncTargetFilesSelection(cur.filter((x) => x !== fp));
            }
          });
        });
      }
      wireChecklistInputs();
      if (chipsWrap) {
        chipsWrap.querySelectorAll('button[data-remove-target-file]').forEach((rmBtn) => {
          rmBtn.addEventListener('click', () => {
            const targetFp = rmBtn.getAttribute('data-remove-target-file');
            syncTargetFilesSelection((state.targetFilesSelected[pid] || []).filter((x) => x !== targetFp));
          });
        });
      }
      if (searchInput && checklistEl) {
        searchInput.addEventListener('input', () => {
          state.targetFilesQueryByPid[pid] = searchInput.value;
          const curSet = new Set((state.targetFilesSelected && state.targetFilesSelected[pid]) || []);
          checklistEl.innerHTML = buildTargetFilesGroupsHtml(filesList, curSet, searchInput.value);
          wireChecklistInputs();
        });
      }
      if (modifyInput) {
        modifyInput.addEventListener('input', () => {
          state.modifyDrafts[pid] = modifyInput.value;
        });
      }
      wireMicButton('modify-mic-btn', 'modify-project-input');
      if (modifyBtn && modifyInput) {
        modifyBtn.addEventListener('click', async () => {
          const selFiles = targetSelect
            ? Array.from(targetSelect.selectedOptions).map((o) => o.value).filter(Boolean)
            : (state.targetFilesSelected[pid] || []);
          await submitModifyProject(pid, modifyInput.value, selFiles, modifyBtn);
        });
      }

      container.querySelectorAll('button[data-rollback-sha]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const sha = btn.getAttribute('data-rollback-sha');
          if (!sha) return;
          await submitRollbackProject(pid, sha, btn);
        });
      });
      container.querySelectorAll('button[data-copy-version-sha]').forEach((btn) => {
        btn.addEventListener('click', () => {
          copyToClipboard(btn.getAttribute('data-copy-version-sha'), '✓ Hash copiado al portapapeles.');
        });
      });
      container.querySelectorAll('button[data-copy-diff-path]').forEach((btn) => {
        btn.addEventListener('click', (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          copyToClipboard(btn.getAttribute('data-copy-diff-path'), '✓ Ruta copiada al portapapeles.');
        });
      });
      container.querySelectorAll('button[data-copy-diff-hash]').forEach((btn) => {
        btn.addEventListener('click', (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          copyToClipboard(btn.getAttribute('data-copy-diff-hash'), '✓ Hash copiado al portapapeles.');
        });
      });
      const simpleRbBtn = document.getElementById('simple-rollback-btn');
      if (simpleRbBtn) {
        simpleRbBtn.addEventListener('click', async () => {
          const sha = simpleRbBtn.getAttribute('data-sha');
          if (!sha) return;
          await submitRollbackProject(pid, sha, simpleRbBtn);
        });
      }

      const filesDetails = document.getElementById('modify-files-details');
      if (filesDetails) {
        filesDetails.addEventListener('toggle', () => {
          state.filesOpen[pid] = filesDetails.open;
          state.lastModifySignature = computeModifySignature(pid);
        });
      }
      const rbDetails = document.getElementById('rollback-details');
      if (rbDetails) {
        rbDetails.addEventListener('toggle', () => {
          state.rollbackOpen[pid] = rbDetails.open;
          state.lastModifySignature = computeModifySignature(pid);
        });
      }
      const diffDetails = document.getElementById('git-diff-details');
      if (diffDetails) {
        diffDetails.addEventListener('toggle', () => {
          state.diffOpen[pid] = diffDetails.open;
          state.lastModifySignature = computeModifySignature(pid);
        });
      }
    }

    function extractCleanProjectPathFromFileUrl(rawUrl) {
      let cleaned = String(rawUrl || '').trim().replace(/^[<"'`]+|[>"'`.,;:]+$/g, '');
      const hashIdx = cleaned.indexOf('#');
      if (hashIdx >= 0) cleaned = cleaned.slice(0, hashIdx);
      const qIdx = cleaned.indexOf('?');
      if (qIdx >= 0) cleaned = cleaned.slice(0, qIdx);
      try {
        cleaned = decodeURI(cleaned);
      } catch (_) {
        cleaned = cleaned.replace(/%20/g, ' ');
      }
      cleaned = cleaned.replace(/\\\\/g, '/');
      const wsMatch = new RegExp('/workspaces/[^/]+/(.+)$', 'i').exec(cleaned);
      if (wsMatch && wsMatch[1]) {
        return wsMatch[1].replace(/^\\/+/, '');
      }
      const noScheme = cleaned
        .replace(/^file:\\/\\/+\\/?/i, '')
        .replace(/^[a-zA-Z]:\\//, '')
        .replace(/^\\/+/, '');
      if (!noScheme) return 'archivo';
      const parts = noScheme.split('/').filter(Boolean);
      return parts.length > 3 ? parts.slice(-3).join('/') : parts.join('/');
    }

    function formatSafeInlineMd(escapedLine) {
      const codeSpans = [];
      let s = String(escapedLine || '').replace(/`([^`]+)`/g, (_m, inner) => {
        const tok = '@@AF_ICODE_' + codeSpans.length + '@@';
        codeSpans.push('<code>' + inner + '</code>');
        return tok;
      });
      s = s.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
      s = s.replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
      codeSpans.forEach((html, idx) => {
        s = s.replace('@@AF_ICODE_' + idx + '@@', () => html);
      });
      return s;
    }

    function renderSafeMarkdown(mdText) {
      const normalizedRaw = String(mdText || '').replace(/\\r\\n?/g, '\\n');
      const segments = normalizedRaw.split(/(```[\\s\\S]*?```)/g);
      const normalized = segments.map((seg, idx) => (
        idx % 2 === 1 ? seg : seg.replace(/\\\\r\\\\n|\\\\r|\\\\n/g, '\\n')
      )).join('');
      const codeBlocks = [];
      const withoutFences = normalized.replace(/```([^\\n`]*?)[ \\t]*\\n([\\s\\S]*?)```/g, (_m, lang, code) => {
        const idx = codeBlocks.length;
        codeBlocks.push({ lang: String(lang || '').trim() || 'código', raw: code });
        return '\\n@@AF_FCODE_' + idx + '@@\\n';
      });
      const fileRefs = [];
      function pushFileRef(rawUrl) {
        const idx = fileRefs.length;
        fileRefs.push(extractCleanProjectPathFromFileUrl(rawUrl));
        return '@@AF_FPATH_' + idx + '@@';
      }
      const withCleanPaths = withoutFences
        .replace(/`(file:\\/\\/\\/?[^`\\n]+)`/gi, (_m, u) => pushFileRef(u))
        .replace(/\\[([^\\]\\n]*)\\]\\((file:\\/\\/\\/?(?:[^()\\n]|\\([^()\\n]*\\))+)\\)/gi, (_m, rawLbl, u) => {
          const rel = extractCleanProjectPathFromFileUrl(u);
          const lbl = String(rawLbl || '').trim();
          const tok = pushFileRef(u);
          const cleanLbl = lbl.replace(/\\\\/g, '/').split(/[:#]/)[0].trim();
          if (
            !cleanLbl ||
            cleanLbl.toLowerCase().indexOf('file://') >= 0 ||
            cleanLbl === rel ||
            rel.endsWith('/' + cleanLbl)
          ) {
            return tok;
          }
          return lbl + ' (' + tok + ')';
        })
        .replace(/file:\\/\\/\\/?[^\\s<>)"'`]+/gi, (rawMatch) => {
          const trailMatch = /([.,;:!?]+)$/.exec(rawMatch);
          const trail = trailMatch ? trailMatch[1] : '';
          const core = trail ? rawMatch.slice(0, -trail.length) : rawMatch;
          return pushFileRef(core) + trail;
        });
      const escaped = escapeHtml(withCleanPaths);
      const lines = escaped.split('\\n');
      const toc = [];
      const out = [];
      let inUl = false;
      let inOl = false;
      let paraBuf = [];
      let hCounter = 0;

      function flushPara() {
        if (!paraBuf.length) return;
        out.push('<p>' + paraBuf.map(formatSafeInlineMd).join('<br>') + '</p>');
        paraBuf = [];
      }
      function closeLists() {
        if (inUl) { out.push('</ul>'); inUl = false; }
        if (inOl) { out.push('</ol>'); inOl = false; }
      }
      function isTableRow(ln) {
        const t = String(ln || '').trim();
        return t.startsWith('|') && t.endsWith('|') && t.indexOf('|', 1) >= 0;
      }
      function isTableSep(ln) {
        return /^\\|(?:\\s*:?-{2,}:?\\s*\\|)+$/.test(String(ln || '').trim());
      }
      function splitCells(ln) {
        return String(ln || '').trim().slice(1, -1).split('|').map((c) => formatSafeInlineMd(c.trim()));
      }

      for (let i = 0; i < lines.length; i++) {
        const rawLine = lines[i];
        const trimmed = rawLine.trim();
        const fenceMatch = /^@@AF_FCODE_(\\d+)@@$/.exec(trimmed);
        if (fenceMatch) {
          flushPara();
          closeLists();
          out.push(trimmed);
          continue;
        }
        if (!trimmed) {
          flushPara();
          closeLists();
          continue;
        }
        if (/^-{3,}$/.test(trimmed)) {
          flushPara();
          closeLists();
          out.push('<hr style="border:0;border-top:1px solid var(--border);margin:10px 0;">');
          continue;
        }
        if (i + 1 < lines.length && isTableRow(trimmed) && isTableSep(lines[i + 1])) {
          flushPara();
          closeLists();
          const headers = splitCells(trimmed).map((c) => '<th>' + c + '</th>').join('');
          const bodyRows = [];
          i += 2;
          while (i < lines.length && isTableRow(lines[i])) {
            bodyRows.push('<tr>' + splitCells(lines[i]).map((c) => '<td>' + c + '</td>').join('') + '</tr>');
            i++;
          }
          i--;
          out.push(
            '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
              headers +
            '</tr></thead><tbody>' +
              bodyRows.join('') +
            '</tbody></table></div>'
          );
          continue;
        }
        const hMatch = /^(#{1,6})\\s+(.*)$/.exec(trimmed);
        if (hMatch) {
          flushPara();
          closeLists();
          const lvl = hMatch[1].length;
          const hId = 'md-h-' + (hCounter++);
          const cleanText = hMatch[2].trim();
          if (lvl === 2 || lvl === 3) {
            toc.push({ level: lvl, text: cleanText, id: hId });
          }
          out.push('<h' + lvl + ' id="' + hId + '">' + formatSafeInlineMd(cleanText) + '</h' + lvl + '>');
          continue;
        }
        const bqMatch = /^&gt;\\s?(.*)$/.exec(trimmed);
        if (bqMatch) {
          flushPara();
          closeLists();
          out.push('<blockquote>' + formatSafeInlineMd(bqMatch[1]) + '</blockquote>');
          continue;
        }
        const ulMatch = /^[-*]\\s+(.*)$/.exec(trimmed);
        if (ulMatch) {
          flushPara();
          if (inOl) { out.push('</ol>'); inOl = false; }
          if (!inUl) { out.push('<ul>'); inUl = true; }
          const leadSpaces = (rawLine.match(/^ */) || [''])[0].length;
          const liStyle = leadSpaces >= 2 ? ' style="margin-left:' + Math.min(48, Math.floor(leadSpaces / 2) * 16) + 'px;"' : '';
          out.push('<li' + liStyle + '>' + formatSafeInlineMd(ulMatch[1]) + '</li>');
          continue;
        }
        const olMatch = /^\\d+\\.\\s+(.*)$/.exec(trimmed);
        if (olMatch) {
          flushPara();
          if (inUl) { out.push('</ul>'); inUl = false; }
          if (!inOl) { out.push('<ol>'); inOl = true; }
          const leadSpaces = (rawLine.match(/^ */) || [''])[0].length;
          const liStyle = leadSpaces >= 2 ? ' style="margin-left:' + Math.min(48, Math.floor(leadSpaces / 2) * 16) + 'px;"' : '';
          out.push('<li' + liStyle + '>' + formatSafeInlineMd(olMatch[1]) + '</li>');
          continue;
        }
        closeLists();
        paraBuf.push(trimmed);
      }
      flushPara();
      closeLists();

      let html = out.join('');
      codeBlocks.forEach((cb, idx) => {
        const blockHtml =
          '<div class="md-code-wrap">' +
            '<div class="md-code-head">' +
              '<span>' + escapeHtml(cb.lang) + '</span>' +
              '<button type="button" class="btn btn-sm md-copy-btn" data-md-code-idx="' + idx + '">Copiar</button>' +
            '</div>' +
            '<pre><code>' + escapeHtml(cb.raw) + '</code></pre>' +
          '</div>';
        html = html.replace('@@AF_FCODE_' + idx + '@@', () => blockHtml);
      });
      fileRefs.forEach((relPath, idx) => {
        const refHtml = '<code class="md-file-ref">' + escapeHtml(relPath) + '</code>';
        html = html.replace('@@AF_FPATH_' + idx + '@@', () => refHtml);
      });
      return { html: '<div class="md-body">' + html + '</div>', toc, codeBlocks };
    }

    function inferAuditVersionBadge(entry, cps) {
      let ver = entry && entry.version_label ? String(entry.version_label) : '';
      let sha = entry && entry.commit_sha ? String(entry.commit_sha) : '';
      if (!ver && !sha && Array.isArray(cps) && cps.length && entry && entry.created_at) {
        const msgMs = Date.parse(entry.created_at);
        if (isFinite(msgMs)) {
          const matchCp = cps.find((c) => {
            const cMs = c && c.delivered_at ? Date.parse(c.delivered_at) : NaN;
            return isFinite(cMs) && cMs <= msgMs + 5000;
          }) || cps[0];
          if (matchCp) {
            ver = matchCp.version_label || '';
            sha = matchCp.commit_sha || '';
          }
        }
      }
      const shortSha = sha ? sha.slice(0, 7) : '';
      if (ver && shortSha) return ver + ' · ' + shortSha;
      return ver || shortSha || '';
    }

    function extractMarkdownExcerpt(mdText, maxLen) {
      const clean = String(mdText || '')
        .replace(/```[\\s\\S]*?```/g, ' ')
        .replace(/^#+\\s+/gm, '')
        .replace(/[*`>_-]/g, ' ')
        .replace(/\\s+/g, ' ')
        .trim();
      const limit = maxLen || 130;
      if (clean.length <= limit) return clean;
      return clean.slice(0, limit - 1).trimEnd() + '…';
    }

    function renderAuditStatusBadge(entry) {
      const st = String((entry && entry.status) || 'completed');
      if (st === 'running') {
        return '<span class="badge badge-info">⏳ En curso</span>';
      }
      if (st === 'timed_out' || st === 'interrupted') {
        return '<span class="badge badge-warn">⚠️ Tiempo agotado / Interrumpida</span>';
      }
      if (st === 'failed') {
        return '<span class="badge badge-err">❌ Error</span>';
      }
      return '<span class="badge badge-ok">✅ Completada</span>';
    }

    function renderAuditDiagnosticHtml(entry) {
      if (!entry) return '';
      const st = String(entry.status || 'completed');
      const stage = String(entry.stage_label || '').trim();
      const errDetail = String(entry.error_detail || '').trim();
      if (st === 'running') {
        return '<div class="audit-card-diag diag-live">' +
          '<span class="working-spinner" style="width:14px;height:14px;" aria-hidden="true"></span>' +
          '<span><strong>Etapa actual:</strong> ' + escapeHtml(stage || '1/3 Leyendo archivos del repositorio') + '</span>' +
        '</div>';
      }
      if (st === 'timed_out' || st === 'interrupted' || st === 'failed') {
        const cls = st === 'failed' ? 'audit-card-diag diag-err' : 'audit-card-diag';
        return '<div class="' + cls + '">' +
          '<div><strong>Se detuvo en:</strong> ' + escapeHtml(stage || 'Etapa desconocida') + '</div>' +
          (errDetail ? '<div style="margin-top:4px;">' + escapeHtml(errDetail) + '</div>' : '') +
        '</div>';
      }
      return '';
    }

    function resolveAuditScorecard(rep, cps) {
      if (rep && rep.quality_scorecard && typeof rep.quality_scorecard === 'object' && Array.isArray(rep.quality_scorecard.areas)) {
        return rep.quality_scorecard;
      }
      if (!rep || rep.mode !== 'audit' || !Array.isArray(cps) || !cps.length) return null;
      const repSha = String(rep.commit_sha || '').toLowerCase();
      const repVer = String(rep.version_label || '').toLowerCase();
      for (const cp of cps) {
        if (!cp || !cp.quality_scorecard || !Array.isArray(cp.quality_scorecard.areas)) continue;
        const cpSha = String(cp.commit_sha || '').toLowerCase();
        const cpVer = String(cp.version_label || '').toLowerCase();
        if ((repSha && cpSha && cpSha.startsWith(repSha.slice(0, 7))) || (repVer && cpVer === repVer)) {
          return cp.quality_scorecard;
        }
      }
      return null;
    }

    const QUALITY_DIMENSION_LABELS = {
      ux_ui: 'UX/UI',
      functional_completeness: 'Funcionalidad',
      robustness: 'Robustez',
      architecture_quality: 'Arquitectura',
      security_hygiene: 'Seguridad',
      testing_runtime: 'Pruebas',
    };

    function classifyScorecardTier(sc) {
      if (!sc || typeof sc !== 'object') {
        return { id: 'none', label: '⚪ Sin evaluar', cls: 'badge-muted', pct: null };
      }
      const rawScore = Number(sc.global_score || 0);
      const pct = Math.round(rawScore);
      let tierId = sc.tier_id ? String(sc.tier_id).trim() : '';
      let tierLabel = sc.tier_label ? String(sc.tier_label).trim() : '';
      if (!tierId || !tierLabel) {
        const minArea = Number(sc.min_area_score || 70);
        const areas = Array.isArray(sc.areas) ? sc.areas : [];
        const hasPendingArea = (Array.isArray(sc.failed_dimensions) && sc.failed_dimensions.length > 0) ||
          areas.some((a) => a && a.active !== false && (a.passed === false || Number(a.score || 0) < minArea));
        if (rawScore < 75.0) {
          tierId = 'basic';
          tierLabel = '🥉 Básico';
        } else if (rawScore < 85.0) {
          tierId = 'standard';
          tierLabel = '🥈 Estándar';
        } else if (hasPendingArea) {
          tierId = 'standard';
          tierLabel = '🥈 Estándar (área pendiente)';
        } else if (rawScore < 93.0) {
          tierId = 'production';
          tierLabel = '🥇 Producción';
        } else {
          tierId = 'excellence';
          tierLabel = '💎 Excelencia';
        }
      }
      let cls = 'badge-warn';
      if (tierId === 'excellence') cls = 'badge-excellence';
      else if (tierId === 'production') cls = 'badge-ok';
      else if (tierId === 'basic') cls = 'badge-err';
      return { id: tierId, label: tierLabel, cls, pct };
    }

    function buildQualityAreaBarsHtml(sc) {
      if (!sc || !Array.isArray(sc.areas)) return '';
      return sc.areas.map((area) => {
        const label = area.label || QUALITY_DIMENSION_LABELS[area.dimension] || area.dimension || '';
        const isActive = area.active !== false;
        const scoreNum = isActive ? Math.max(0, Math.min(100, Math.round(Number(area.score || 0)))) : 0;
        const barCls = !isActive ? 'na' : (scoreNum >= 85 ? '' : (scoreNum >= 70 ? 'warn' : 'fail'));
        const rightTxt = isActive ? (scoreNum + '%') : 'N/A';
        return '<div class="quality-bar-item">' +
          '<div class="quality-bar-label"><span>' + escapeHtml(label) + '</span><strong>' + escapeHtml(rightTxt) + '</strong></div>' +
          '<div class="quality-bar-track"><div class="quality-bar-fill ' + barCls + '" style="width:' + scoreNum + '%;"></div></div>' +
        '</div>';
      }).join('');
    }

    function renderQualityScorecardHtml(rep, cps) {
      const sc = resolveAuditScorecard(rep, cps);
      if (!sc) return '';
      const pct = Math.round(Number(sc.global_score || 0));
      const passed = Boolean(sc.passed);
      const tier = classifyScorecardTier(sc);
      const verdictText = passed ? 'Aprobado' : 'Con observaciones';
      const barsHtml = buildQualityAreaBarsHtml(sc);
      return '<div class="quality-scorecard">' +
        '<div class="quality-scorecard-head">' +
          '<span>Global: ' + escapeHtml(String(pct)) + '% · ' + escapeHtml(verdictText) + '</span>' +
          '<span style="display:inline-flex; gap:6px; flex-wrap:wrap;">' +
            '<span class="badge ' + tier.cls + '">' + escapeHtml(tier.label) + '</span>' +
            '<span class="badge ' + (passed ? 'badge-ok' : 'badge-warn') + '">' + escapeHtml(verdictText) + '</span>' +
          '</span>' +
        '</div>' +
        '<div class="quality-bars-grid">' + barsHtml + '</div>' +
      '</div>';
    }

    function buildAuditMetaBadgesHtml(rep, cps) {
      const typeLabel = rep.mode === 'audit' ? '🔍 Auditoría' : '📄 Documento .md';
      const statusBadgeHtml = renderAuditStatusBadge(rep);
      const sc = resolveAuditScorecard(rep, cps);
      const tier = sc ? classifyScorecardTier(sc) : null;
      const scBadgeHtml = sc
        ? '<span class="badge ' + tier.cls + '">' +
            escapeHtml(tier.label) + ' · Global: ' +
            escapeHtml(String(Math.round(Number(sc.global_score || 0)))) + '% · ' +
            escapeHtml(sc.passed ? 'Aprobado' : 'Con observaciones') +
          '</span>'
        : '';
      const stagePillHtml = rep.stage_label
        ? '<span class="model-pill">' + escapeHtml(rep.stage_label) + '</span>'
        : '';
      const verStr = inferAuditVersionBadge(rep, cps);
      const verPillHtml = verStr ? '<span class="model-pill">' + escapeHtml(verStr) + '</span>' : '';
      const durPillHtml = typeof rep.duration_seconds === 'number'
        ? '<span class="duration-pill">' + escapeHtml(formatDurationHuman(rep.duration_seconds, '< 1s')) + '</span>'
        : '';
      const timeHtml = '<span class="muted" style="font-size:var(--fs-xs);">' + escapeHtml(formatFriendlyTime(rep.created_at)) + '</span>';
      return '<span class="badge badge-info">' + escapeHtml(typeLabel) + '</span>' +
        statusBadgeHtml +
        scBadgeHtml +
        stagePillHtml +
        verPillHtml +
        durPillHtml +
        timeHtml;
    }

    function isLongAuditorReply(rawText) {
      const text = String(rawText || '').replace(/\\r\\n?/g, '\\n').trim();
      if (!text) return false;
      if (text.length > 420) return true;
      return text.split('\\n').length > 8;
    }

    function isQuestionsTabEntry(m) {
      if (!m || typeof m !== 'object') return false;
      const kind = String(m.kind || m.mode || 'chat').trim().toLowerCase();
      if (kind === 'audit' || kind === 'document') return false;
      const mode = String(m.mode || '').trim().toLowerCase();
      if (mode === 'audit' || mode === 'document') return false;
      const text = String(m.content || '').trim();
      if (!text) return false;
      if (/^audita el proyecto completo\\s*:/i.test(text)) return false;
      if (/^genera un documento t[eé]cnico y gu[ií]a de uso completa/i.test(text)) return false;
      return m.role === 'user' || m.role === 'assistant';
    }

    function renderAuditChatTab(pid, readOnlyModifying) {
      const container = document.getElementById('tab-audit');
      if (!container) return;
      const st = state.detail && state.detail.status;
      const isReadOnly = Boolean(
        readOnlyModifying !== undefined
          ? readOnlyModifying
          : (st && st.state !== 'DELIVERED' && st.state !== 'COMPLETED')
      );
      const msgs = state.chatHistories[pid] || [];
      const draft = state.chatDrafts[pid] || '';
      const cps = (state.checkpointsByPid && state.checkpointsByPid[pid]) || [];
      const reportEntries = msgs
        .map((m, idx) => ({ entry: m, origIdx: idx }))
        .filter((x) => x.entry && x.entry.role === 'assistant' && (x.entry.mode === 'audit' || x.entry.mode === 'document'))
        .reverse();
      const chatEntries = msgs
        .map((m, idx) => ({ entry: m, origIdx: idx }))
        .filter((x) => isQuestionsTabEntry(x.entry));
      const collapseOverrides = (state.chatMsgCollapseOverrideByPid && state.chatMsgCollapseOverrideByPid[pid]) || {};
      const activeSub = (state.auditSubTabByPid && state.auditSubTabByPid[pid]) || 'index';
      const activeFilter = (state.auditFilterByPid && state.auditFilterByPid[pid]) || 'all';
      const selectedReportId = (state.selectedAuditMsgIdByPid && state.selectedAuditMsgIdByPid[pid]) || null;

      const chatSig = JSON.stringify([
        pid,
        isReadOnly,
        activeSub,
        activeFilter,
        selectedReportId,
        cps.length,
        collapseOverrides,
        msgs.map((m) => [
          m.id,
          m.role,
          m.mode,
          m.content,
          m.status,
          m.stage_label,
          m.error_detail,
          m.duration_seconds,
          m.commit_sha,
          m.version_label,
          m.quality_scorecard && m.quality_scorecard.global_score,
        ]),
      ]);
      if (state.lastChatSig === chatSig && container.querySelector('#audit-index')) {
        return;
      }
      state.lastChatSig = chatSig;

      const disabledAttr = isReadOnly ? ' disabled' : '';
      const readonlyNoticeHtml = isReadOnly
        ? '<div class="readonly-mod-notice">Modo solo lectura · Disponible al terminar los cambios</div>'
        : '';

      const allCodeBlocks = [];

      let indexBodyHtml = '';
      const openReportItem = selectedReportId
        ? reportEntries.find((r) => (r.entry.id || String(r.origIdx)) === selectedReportId)
        : null;

      if (openReportItem) {
        const rep = openReportItem.entry;
        const repId = rep.id ? String(rep.id) : '';
        const repStatus = String(rep.status || 'completed');
        const hasContent = Boolean(rep.content && String(rep.content).trim());
        const diagHtml = renderAuditDiagnosticHtml(rep);
        const scorecardHtml = renderQualityScorecardHtml(rep, cps);
        const parsed = hasContent ? renderSafeMarkdown(rep.content || '') : { html: '', toc: [], codeBlocks: [] };
        const baseCodeOffset = allCodeBlocks.length;
        parsed.codeBlocks.forEach((cb) => allCodeBlocks.push(cb));
        let readerHtml = parsed.html;
        if (baseCodeOffset > 0) {
          readerHtml = readerHtml.replace(/data-md-code-idx="(\\d+)"/g, (_m, n) =>
            'data-md-code-idx="' + (Number(n) + baseCodeOffset) + '"'
          );
        }
        const tocHtml = parsed.toc.length
          ? '<div class="audit-toc"><strong>Tabla de contenido</strong><ul>' +
              parsed.toc.map((t) =>
                '<li style="margin-left:' + (t.level === 3 ? '14px' : '0') + ';">' +
                  '<a href="#' + escapeHtml(t.id) + '" data-toc-jump="' + escapeHtml(t.id) + '">' + escapeHtml(t.text) + '</a>' +
                '</li>'
              ).join('') +
            '</ul></div>'
          : '';
        const retryReaderBtn = (repId && (repStatus === 'failed' || repStatus === 'timed_out' || repStatus === 'interrupted'))
          ? '<button class="btn btn-sm" type="button" data-audit-retry="' + escapeHtml(repId) + '"' + disabledAttr + '>🔄 Reintentar</button>'
          : '';
        const delReaderBtn = repId
          ? '<button class="btn btn-subtle-danger btn-sm" type="button" data-audit-delete="' + escapeHtml(repId) + '">🗑️ Eliminar</button>'
          : '';
        indexBodyHtml =
          '<div class="audit-reader">' +
            '<div class="audit-reader-bar">' +
              '<div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">' +
                '<button id="audit-reader-back-btn" class="btn btn-sm" type="button">← Volver al índice</button>' +
                buildAuditMetaBadgesHtml(rep, cps) +
              '</div>' +
              '<div style="display:flex; gap:6px; flex-wrap:wrap;">' +
                (hasContent ? '<button class="btn btn-sm" type="button" data-audit-dl="' + openReportItem.origIdx + '">Descargar .md</button>' : '') +
                (hasContent ? '<button class="btn btn-sm" type="button" data-audit-apply="' + openReportItem.origIdx + '"' + disabledAttr + '>Enviar hallazgos a Aplicar cambios</button>' : '') +
                retryReaderBtn +
                delReaderBtn +
              '</div>' +
            '</div>' +
            diagHtml +
            scorecardHtml +
            tocHtml +
            readerHtml +
          '</div>';
      } else {
        const effFilter = reportEntries.length >= 2 ? activeFilter : 'all';
        const filteredReports = reportEntries.filter((r) =>
          effFilter === 'all' ? true : (r.entry.mode === effFilter)
        );
        const filtersHtml = reportEntries.length >= 2
          ? ('<div class="audit-filters">' +
              '<button type="button" class="audit-filter-btn' + (activeFilter === 'all' ? ' active' : '') + '" data-audit-filter="all">Todas (' + reportEntries.length + ')</button>' +
              '<button type="button" class="audit-filter-btn' + (activeFilter === 'audit' ? ' active' : '') + '" data-audit-filter="audit">Auditorías</button>' +
              '<button type="button" class="audit-filter-btn' + (activeFilter === 'document' ? ' active' : '') + '" data-audit-filter="document">Documentos</button>' +
            '</div>')
          : '';
        const cardsHtml = filteredReports.length
          ? filteredReports.map((r) => {
              const rep = r.entry;
              const repKey = rep.id || String(r.origIdx);
              const repId = rep.id ? String(rep.id) : '';
              const repStatus = String(rep.status || 'completed');
              const hasContent = Boolean(rep.content && String(rep.content).trim());
              const diagHtml = renderAuditDiagnosticHtml(rep);
              const scorecardHtml = renderQualityScorecardHtml(rep, cps);
              const excerpt = extractMarkdownExcerpt(rep.content || '', 130);
              const retryBtnHtml = (repId && (repStatus === 'failed' || repStatus === 'timed_out' || repStatus === 'interrupted'))
                ? '<button class="btn btn-sm" type="button" data-audit-retry="' + escapeHtml(repId) + '"' + disabledAttr + '>🔄 Reintentar</button>'
                : '';
              const delBtnHtml = repId
                ? '<button class="btn btn-subtle-danger btn-sm" type="button" data-audit-delete="' + escapeHtml(repId) + '">🗑️ Eliminar</button>'
                : '';
              const dlBtnHtml = hasContent
                ? '<button class="btn btn-sm" type="button" data-audit-dl="' + r.origIdx + '">Descargar .md</button>'
                : '';
              const openBtnHtml = repStatus !== 'running'
                ? '<button class="btn btn-primary btn-sm" type="button" data-open-report="' + escapeHtml(repKey) + '">Ver</button>'
                : '';
              return '<div class="audit-card">' +
                '<div class="audit-card-head">' +
                  '<div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">' +
                    buildAuditMetaBadgesHtml(rep, cps) +
                  '</div>' +
                  '<div style="display:flex; gap:6px; flex-wrap:wrap;">' +
                    openBtnHtml +
                    dlBtnHtml +
                    retryBtnHtml +
                    delBtnHtml +
                  '</div>' +
                '</div>' +
                diagHtml +
                scorecardHtml +
                (excerpt ? '<div class="audit-card-excerpt">' + escapeHtml(excerpt) + '</div>' : '') +
              '</div>';
            }).join('')
          : '<div class="empty-box" style="padding:18px;">Aún no hay auditorías ni documentos generados. Usa «🔍 Auditar código» o «📄 Generar documento (.md)».</div>';
        indexBodyHtml = filtersHtml + cardsHtml;
      }

      let lastAssistantChatIdx = -1;
      for (let ci = chatEntries.length - 1; ci >= 0; ci--) {
        if (chatEntries[ci].entry.role === 'assistant') {
          lastAssistantChatIdx = ci;
          break;
        }
      }

      const chatMsgsHtml = chatEntries.length
        ? chatEntries.map((item, chatIdx) => {
            const m = item.entry;
            const idx = item.origIdx;
            const msgKey = String(m.id || ('idx-' + idx));
            const isUser = m.role === 'user';
            const rawContent = String(m.content || '');
            const hasRealContent = Boolean(rawContent.trim());
            const who = isUser ? 'Tú' : 'Auditor';
            const verStr = inferAuditVersionBadge(m, cps);
            let bodyHtml = '';
            let collapseBtnHtml = '';
            if (isUser) {
              bodyHtml = '<div class="audit-msg-body">' + escapeHtml(rawContent.trim()) + '</div>';
            } else if (m.status === 'running' && !hasRealContent) {
              const stLbl = m.stage_label ? String(m.stage_label) : 'Auditor pensando...';
              bodyHtml = '<div class="factory-thinking-bubble"><span class="working-spinner" style="width:14px;height:14px;" aria-hidden="true"></span><span>' + escapeHtml(stLbl) + '</span> <span class="live-op-clock" data-op-clock-start="' + Date.now() + '">00:00</span></div>';
            } else {
              const parsed = renderSafeMarkdown(rawContent);
              const baseOffset = allCodeBlocks.length;
              parsed.codeBlocks.forEach((cb) => allCodeBlocks.push(cb));
              const renderedMd = parsed.html.replace(/data-md-code-idx="(\\d+)"/g, (_mt, n) =>
                'data-md-code-idx="' + (Number(n) + baseOffset) + '"'
              );
              const isLong = isLongAuditorReply(rawContent);
              const defaultExpanded = chatIdx === lastAssistantChatIdx;
              const isExpanded = (msgKey in collapseOverrides)
                ? Boolean(collapseOverrides[msgKey])
                : defaultExpanded;
              if (isLong) {
                const collapsedCls = isExpanded ? '' : ' collapsed';
                const fadeHtml = isExpanded ? '' : '<div class="chat-msg-fade" aria-hidden="true"></div>';
                const toggleLabel = isExpanded ? 'Colapsar' : 'Ver respuesta completa';
                bodyHtml =
                  '<div class="chat-msg-collapsible' + collapsedCls + '">' +
                    renderedMd +
                    fadeHtml +
                  '</div>';
                collapseBtnHtml =
                  '<button type="button" class="chat-collapse-btn" data-chat-toggle="' + escapeHtml(msgKey) + '" aria-expanded="' + (isExpanded ? 'true' : 'false') + '">' +
                    toggleLabel +
                  '</button>';
              } else {
                bodyHtml = renderedMd;
              }
            }
            const actionsHtml = (!isUser && hasRealContent)
              ? '<div class="audit-msg-actions chat-msg-footer">' +
                  '<button class="chat-action-link" type="button" data-audit-dl="' + idx + '">Descargar .md</button>' +
                  '<span class="chat-action-sep" aria-hidden="true">·</span>' +
                  '<button class="chat-action-link" type="button" data-audit-apply="' + idx + '"' + disabledAttr + '>Enviar hallazgos a Aplicar cambios</button>' +
                '</div>'
              : '';
            return '<div class="audit-msg chat-msg ' + (isUser ? 'user' : 'assistant') + '">' +
              '<div class="audit-msg-head">' +
                '<span style="display:inline-flex; gap:6px; align-items:center; flex-wrap:wrap;"><strong>' + escapeHtml(who) + '</strong>' +
                  (verStr ? '<span class="model-pill">' + escapeHtml(verStr) + '</span>' : '') +
                '</span>' +
                '<span>' + escapeHtml(formatFriendlyTime(m.created_at)) + '</span>' +
              '</div>' +
              bodyHtml +
              collapseBtnHtml +
              actionsHtml +
            '</div>';
          }).join('')
        : '<div class="muted" style="font-size:0.88rem; padding:12px 4px;">Haz una pregunta puntual sobre el código, rutas o arquitectura de este proyecto.</div>';

      const placeholderTxt = isReadOnly
        ? 'Disponible al terminar los cambios...'
        : 'Haz una pregunta sobre el proyecto, su arquitectura o posibles mejoras...';

      container.innerHTML =
        readonlyNoticeHtml +
        '<div class="audit-unified-bar">' +
          '<div class="audit-subtabs">' +
            '<button type="button" class="audit-subtab' + (activeSub === 'index' ? ' active' : '') + '" data-audit-sub="index" aria-label="Auditorías y documentos" title="Auditorías y documentos">📑 Auditorías y docs (' + reportEntries.length + ')</button>' +
            '<button type="button" class="audit-subtab' + (activeSub === 'questions' ? ' active' : '') + '" data-audit-sub="questions">💬 Preguntas (' + chatEntries.length + ')</button>' +
          '</div>' +
          '<div class="audit-primary-split">' +
            '<span id="audit-toolbar-status"></span>' +
            '<button id="audit-quick-audit-btn" class="btn btn-primary btn-sm" type="button" data-chat-mode="audit"' + disabledAttr + '>🔍 Auditar código</button>' +
            '<div class="more-menu-wrap">' +
              '<button id="audit-more-actions-btn" class="btn btn-sm" type="button" aria-expanded="false" aria-controls="audit-more-actions-menu" title="Más acciones de análisis"' + disabledAttr + '>▾</button>' +
              '<div id="audit-more-actions-menu" class="more-popover" style="display:none">' +
                '<button id="audit-quick-explain-btn" class="more-menu-item" type="button" data-chat-mode="chat"' + disabledAttr + '>📖 Explicar funcionamiento</button>' +
                '<button id="audit-quick-doc-btn" class="more-menu-item" type="button" data-chat-mode="document"' + disabledAttr + '>📄 Generar documento (.md)</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div id="audit-index" style="display:' + (activeSub === 'index' ? 'flex' : 'none') + ';">' + indexBodyHtml + '</div>' +
        '<div id="audit-questions-pane" style="display:' + (activeSub === 'questions' ? 'block' : 'none') + ';">' +
          '<div id="audit-chat-panel">' +
            '<div id="audit-chat-history" class="audit-thread audit-chat-stream">' + chatMsgsHtml + '</div>' +
            '<div class="audit-chat-composer">' +
              '<div class="input-row-with-mic">' +
                '<textarea id="audit-chat-input" class="input-field" rows="2" placeholder="' + escapeHtml(placeholderTxt) + '"' + disabledAttr + '>' + escapeHtml(draft) + '</textarea>' +
                '<button id="audit-chat-mic-btn" class="btn btn-sm mic-btn" type="button" title="Dictar consulta por voz (es-MX)"' + disabledAttr + '>🎤</button>' +
              '</div>' +
              '<div class="banner-actions" style="margin-top:0;">' +
                '<button id="audit-chat-send-btn" class="btn btn-primary btn-sm" type="button"' + disabledAttr + '>Enviar consulta</button>' +
                '<span id="audit-inline-status"></span>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

      const threadEl = document.getElementById('audit-chat-history');
      if (threadEl && activeSub === 'questions') {
        if (typeof state.preserveChatScrollTop === 'number') {
          threadEl.scrollTop = state.preserveChatScrollTop;
          state.preserveChatScrollTop = null;
        } else {
          threadEl.scrollTop = threadEl.scrollHeight;
        }
      }

      container.querySelectorAll('button[data-chat-toggle]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const msgKey = btn.getAttribute('data-chat-toggle');
          if (!msgKey) return;
          if (!state.chatMsgCollapseOverrideByPid[pid]) {
            state.chatMsgCollapseOverrideByPid[pid] = {};
          }
          const currentlyExpanded = btn.getAttribute('aria-expanded') === 'true';
          state.chatMsgCollapseOverrideByPid[pid][msgKey] = !currentlyExpanded;
          state.preserveChatScrollTop = threadEl ? threadEl.scrollTop : 0;
          state.lastChatSig = null;
          renderAuditChatTab(pid, isReadOnly);
        });
      });

      const inputEl = document.getElementById('audit-chat-input');
      const sendBtn = document.getElementById('audit-chat-send-btn');
      if (inputEl && !isReadOnly) {
        inputEl.addEventListener('input', () => {
          state.chatDrafts[pid] = inputEl.value;
        });
      }
      if (!isReadOnly) {
        wireMicButton('audit-chat-mic-btn', 'audit-chat-input');
      }

      container.querySelectorAll('button[data-audit-sub]').forEach((btn) => {
        btn.addEventListener('click', () => {
          state.auditSubTabByPid[pid] = btn.getAttribute('data-audit-sub');
          state.lastChatSig = null;
          renderAuditChatTab(pid, isReadOnly);
        });
      });
      container.querySelectorAll('button[data-audit-filter]').forEach((btn) => {
        btn.addEventListener('click', () => {
          state.auditFilterByPid[pid] = btn.getAttribute('data-audit-filter');
          state.lastChatSig = null;
          renderAuditChatTab(pid, isReadOnly);
        });
      });
      container.querySelectorAll('button[data-open-report]').forEach((btn) => {
        btn.addEventListener('click', () => {
          state.selectedAuditMsgIdByPid[pid] = btn.getAttribute('data-open-report');
          state.lastChatSig = null;
          renderAuditChatTab(pid, isReadOnly);
        });
      });
      const backBtn = document.getElementById('audit-reader-back-btn');
      if (backBtn) {
        backBtn.addEventListener('click', () => {
          delete state.selectedAuditMsgIdByPid[pid];
          state.lastChatSig = null;
          renderAuditChatTab(pid, isReadOnly);
        });
      }
      container.querySelectorAll('a[data-toc-jump]').forEach((link) => {
        link.addEventListener('click', (ev) => {
          ev.preventDefault();
          const targetId = link.getAttribute('data-toc-jump');
          const el = targetId ? document.getElementById(targetId) : null;
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
      container.querySelectorAll('button.md-copy-btn[data-md-code-idx]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const idx = Number(btn.getAttribute('data-md-code-idx'));
          const entry = allCodeBlocks[idx];
          if (entry) copyToClipboard(entry.raw, '✓ Bloque de código copiado.');
        });
      });

      const quickAuditBtn = document.getElementById('audit-quick-audit-btn');
      const moreActionsBtn = document.getElementById('audit-more-actions-btn');
      const moreActionsMenu = document.getElementById('audit-more-actions-menu');
      const quickExplainBtn = document.getElementById('audit-quick-explain-btn');
      const quickDocBtn = document.getElementById('audit-quick-doc-btn');
      if (moreActionsBtn && moreActionsMenu && !isReadOnly) {
        moreActionsBtn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          const isOpen = moreActionsMenu.style.display !== 'none';
          closeFloatingDropdowns();
          if (!isOpen) {
            moreActionsMenu.style.display = 'flex';
            moreActionsBtn.setAttribute('aria-expanded', 'true');
          }
        });
      }
      if (quickAuditBtn && !isReadOnly) {
        quickAuditBtn.addEventListener('click', () => {
          closeFloatingDropdowns();
          submitProjectChat(pid, '', 'audit', quickAuditBtn);
        });
      }
      if (quickExplainBtn && !isReadOnly) {
        quickExplainBtn.addEventListener('click', () => {
          closeFloatingDropdowns();
          submitProjectChat(pid, 'Explica cómo funciona este proyecto, sus archivos clave y cómo utilizarlo.', 'chat', quickExplainBtn);
        });
      }
      if (quickDocBtn && !isReadOnly) {
        quickDocBtn.addEventListener('click', () => {
          closeFloatingDropdowns();
          submitProjectChat(pid, '', 'document', quickDocBtn);
        });
      }
      if (sendBtn && inputEl && !isReadOnly) {
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
          if (isReadOnly) {
            setNote('Disponible al terminar los cambios.');
            return;
          }
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
          let modifyBox = document.getElementById('modify-project-input');
          if (!modifyBox && state.detail && state.detail.status) {
            state.lastModifySignature = null;
            renderModifyTab(state.detail.status);
            modifyBox = document.getElementById('modify-project-input');
          }
          if (modifyBox) {
            modifyBox.value = textToApply;
            modifyBox.dispatchEvent(new Event('input', { bubbles: true }));
            modifyBox.focus();
            modifyBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
          setNote('Hallazgos copiados al panel "Pedir cambios". Revisa y pulsa Aplicar cambios si deseas ejecutarlos.');
        });
      });

      container.querySelectorAll('button[data-audit-retry]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const entryId = btn.getAttribute('data-audit-retry');
          if (!entryId) return;
          retryProjectChatEntry(pid, entryId, btn);
        });
      });

      container.querySelectorAll('button[data-audit-delete]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const entryId = btn.getAttribute('data-audit-delete');
          if (!entryId) return;
          deleteProjectChatEntry(pid, entryId, btn);
        });
      });
    }

    async function loadProjectChatHistory(pid, onlyIfServerUpdated) {
      if (!pid) return;
      state.chatLoadedFor[pid] = true;
      try {
        const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/chat');
        const serverMsgs = Array.isArray(res.messages) ? res.messages : [];
        if (onlyIfServerUpdated) {
          const hasLiveServer = serverMsgs.some((m) => m && m.status === 'running');
          const curNonOpt = (state.chatHistories[pid] || []).filter(
            (m) => m && !String(m.id || '').startsWith('optimistic-')
          );
          if (!hasLiveServer && serverMsgs.length <= curNonOpt.length) {
            return;
          }
        }
        state.chatHistories[pid] = serverMsgs;
        state.lastChatSig = null;
        if (state.detail && state.detail.project_id === pid) {
          const st = state.detail.status;
          const ro = Boolean(st && st.state !== 'DELIVERED' && st.state !== 'COMPLETED');
          renderAuditChatTab(pid, ro);
        }
      } catch (_) {}
    }

    async function retryProjectChatEntry(pid, entryId, triggerBtn) {
      if (!pid || !entryId) return;
      const curSt = state.detail && state.detail.status;
      if (curSt && curSt.state !== 'DELIVERED' && curSt.state !== 'COMPLETED') {
        setNote('Disponible al terminar los cambios.');
        return;
      }
      state.auditSubTabByPid[pid] = 'index';
      delete state.selectedAuditMsgIdByPid[pid];
      const existingList = Array.isArray(state.chatHistories[pid]) ? state.chatHistories[pid].slice() : [];
      state.chatHistories[pid] = existingList.map((m) =>
        m && m.id === entryId
          ? Object.assign({}, m, {
              status: 'running',
              stage_label: '1/3 Leyendo archivos del repositorio',
              error_detail: '',
            })
          : m
      );
      state.lastChatSig = null;
      renderAuditChatTab(pid, false);

      const pollTimer = setInterval(() => {
        loadProjectChatHistory(pid, true);
      }, 1500);
      try {
        await runTrackedOperation(
          {
            pid,
            label: 'Reintentando auditoría o documento...',
            subtext: 'Lectura segura del código sin alterar archivos.',
            buttonEl: triggerBtn || 'audit-quick-audit-btn',
            inlineTargetId: 'audit-inline-status',
          },
          async () => {
            const res = await apiFetch(
              '/api/projects/' + encodeURIComponent(pid) + '/chat/' + encodeURIComponent(entryId) + '/retry',
              { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
            );
            state.chatHistories[pid] = Array.isArray(res.history) ? res.history : [];
            state.chatLoadedFor[pid] = true;
            state.selectedAuditMsgIdByPid[pid] = entryId;
            state.lastChatSig = null;
            renderAuditChatTab(pid, false);
            if (res && res.reply) {
              speakSpanishText(res.reply);
            }
            setNote('✓ Reintento completado.', 'ok');
          }
        );
      } finally {
        clearInterval(pollTimer);
        await loadProjectChatHistory(pid);
      }
    }

    async function deleteProjectChatEntry(pid, entryId, triggerBtn) {
      if (!pid || !entryId) return;
      const confirmed = await askCustomConfirm({
        title: 'Eliminar registro del historial',
        body: '¿Deseas eliminar esta auditoría o documento del historial persistido? Esta acción no se puede deshacer.',
        confirmLabel: 'Sí, eliminar',
      });
      if (!confirmed) return;
      if (triggerBtn) triggerBtn.disabled = true;
      try {
        const res = await apiFetch(
          '/api/projects/' + encodeURIComponent(pid) + '/chat/' + encodeURIComponent(entryId),
          { method: 'DELETE' }
        );
        if (state.selectedAuditMsgIdByPid[pid] === entryId) {
          delete state.selectedAuditMsgIdByPid[pid];
        }
        state.chatHistories[pid] = Array.isArray(res.messages) ? res.messages : [];
        state.lastChatSig = null;
        renderAuditChatTab(pid, false);
        setNote('✓ Registro eliminado.', 'ok');
      } catch (err) {
        setNote('Error eliminando registro: ' + err.message, 'err');
      } finally {
        if (triggerBtn && document.body.contains(triggerBtn)) {
          triggerBtn.disabled = false;
        }
      }
    }

    async function submitProjectChat(pid, rawMessage, mode, triggerBtn) {
      const curSt = state.detail && state.detail.status;
      if (curSt && curSt.state !== 'DELIVERED' && curSt.state !== 'COMPLETED') {
        setNote('Disponible al terminar los cambios.');
        return;
      }
      const msg = String(rawMessage || '').trim();
      if (mode === 'chat' && !msg) {
        setNote('Escribe una pregunta para consultar el proyecto.');
        return;
      }
      const isReportMode = mode === 'audit' || mode === 'document';
      const prevDraft = state.chatDrafts[pid] || '';
      if (isReportMode) {
        state.auditSubTabByPid[pid] = 'index';
        delete state.selectedAuditMsgIdByPid[pid];
        const prevList = Array.isArray(state.chatHistories[pid])
          ? state.chatHistories[pid].filter((m) => m && !String(m.id || '').startsWith('optimistic-'))
          : [];
        prevList.push({
          id: 'optimistic-' + Date.now(),
          role: 'assistant',
          mode,
          content: '',
          status: 'running',
          stage_label: '1/3 Leyendo archivos del repositorio',
          created_at: new Date().toISOString(),
        });
        state.chatHistories[pid] = prevList;
        state.lastChatSig = null;
        renderAuditChatTab(pid, false);
      } else {
        state.chatDrafts[pid] = '';
        state.auditSubTabByPid[pid] = 'questions';
        const chatInput = document.getElementById('audit-chat-input');
        if (chatInput) chatInput.value = '';
        const prevList = Array.isArray(state.chatHistories[pid])
          ? state.chatHistories[pid].filter((m) => m && !String(m.id || '').startsWith('optimistic-'))
          : [];
        prevList.push({
          id: 'optimistic-u-' + Date.now(),
          role: 'user',
          mode: 'chat',
          content: msg,
          created_at: new Date().toISOString(),
        });
        prevList.push({
          id: 'optimistic-a-' + Date.now(),
          role: 'assistant',
          mode: 'chat',
          content: '',
          status: 'running',
          stage_label: 'Auditor está pensando…',
          created_at: new Date().toISOString(),
        });
        state.chatHistories[pid] = prevList;
        state.lastChatSig = null;
        renderAuditChatTab(pid, false);
      }
      const modeLabel = mode === 'audit'
        ? 'Analizando archivos del repositorio (auditoría)...'
        : (mode === 'document'
          ? 'Analizando archivos del repositorio para documentar...'
          : 'Analizando archivos del repositorio...');
      const btnLoadingTxt = mode === 'audit'
        ? 'Auditando...'
        : (mode === 'document' ? 'Generando .md...' : 'Enviando...');
      const pollTimer = isReportMode
        ? setInterval(() => { loadProjectChatHistory(pid, true); }, 1500)
        : null;
      try {
        await runTrackedOperation(
          {
            pid,
            label: modeLabel,
            buttonLoadingText: btnLoadingTxt,
            subtext: 'Lectura segura del código sin alterar archivos.',
            buttonEl: triggerBtn || 'audit-chat-send-btn',
            inlineTargetId: isReportMode ? 'audit-toolbar-status' : 'audit-inline-status',
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
                state.auditSubTabByPid[pid] = 'questions';
              } else {
                state.auditSubTabByPid[pid] = 'index';
                const hist = Array.isArray(res.history) ? res.history : [];
                const lastRep = hist.slice().reverse().find((x) => x && x.role === 'assistant');
                if (lastRep && lastRep.id) {
                  state.selectedAuditMsgIdByPid[pid] = lastRep.id;
                }
              }
              state.chatHistories[pid] = Array.isArray(res.history) ? res.history : [];
              state.chatLoadedFor[pid] = true;
              state.lastChatSig = null;
              renderAuditChatTab(pid, false);
              if (res && res.reply) {
                speakSpanishText(res.reply);
              }
              setNote('✓ Respuesta generada.', 'ok');
            } catch (innerErr) {
              if (mode === 'chat') {
                state.chatDrafts[pid] = prevDraft || msg;
                state.chatHistories[pid] = (state.chatHistories[pid] || []).filter(
                  (m) => m && !String(m.id || '').startsWith('optimistic-')
                );
                state.lastChatSig = null;
                renderAuditChatTab(pid, false);
              }
              throw innerErr;
            }
          }
        );
      } finally {
        if (pollTimer) clearInterval(pollTimer);
        if (isReportMode) {
          await loadProjectChatHistory(pid);
        }
      }
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
        setNote('Error cargando detalle: ' + err.message, 'err');
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
        const listEl = document.getElementById('project-list');
        if (listEl && !state.projects.length) {
          listEl.innerHTML =
            '<div class="empty-box" role="alert">' +
              '<div style="margin-bottom:8px;">No se pudieron cargar los proyectos: ' +
              escapeHtml(err && err.message ? err.message : 'Error de conexión') +
              '</div>' +
              '<button id="retry-load-projects-btn" class="btn btn-primary btn-sm" type="button">🔄 Reintentar</button>' +
            '</div>';
          const retryBtn = document.getElementById('retry-load-projects-btn');
          if (retryBtn) retryBtn.addEventListener('click', () => { loadProjects(); });
        }
        setNote('Error de conexión: ' + (err && err.message ? err.message : 'Error de conexión'), 'err');
      }
    }

    async function sendProjectMessage(pid, rawMsg, kind, triggerBtn) {
      const message = (rawMsg || '').trim();
      if (!message) {
        setNote('Por favor escribe un mensaje antes de enviar.');
        return;
      }
      const isReply = kind === 'reply';
      const inputId = isReply ? 'interview-reply-input' : 'adjust-plan-input';
      const inlineTarget = isReply ? 'interview-inline-status' : '';
      const fallbackBtnId = isReply ? 'interview-reply-btn' : 'adjust-plan-btn';
      const inputEl = document.getElementById(inputId);
      const prevDraft = rawMsg || '';
      if (inputEl) inputEl.value = '';
      if (isReply) {
        delete state.replyDrafts[pid];
      } else {
        delete state.adjustDrafts[pid];
      }
      await runTrackedOperation(
        {
          pid,
          label: isReply ? 'Enviando respuesta...' : 'Enviando solicitud de ajuste...',
          buttonLoadingText: isReply ? 'Enviando...' : 'Enviando ajuste...',
          subtext: 'Procesando mensaje y actualizando el estado del proyecto.',
          buttonEl: triggerBtn || fallbackBtnId,
          inlineTargetId: inlineTarget,
        },
        async () => {
          try {
            await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/message', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message }),
            });
            if (isReply) {
              state.interviewPendingByPid[pid] = Date.now();
              setNote('✓ Respuesta enviada · Analizando…', 'ok');
            } else {
              state.adjustOpen[pid] = false;
              setNote('✓ Solicitud de ajuste enviada para ' + pid + '.', 'ok');
            }
            state.lastBannerSignature = null;
            await loadProjects();
          } catch (innerErr) {
            if (isReply) {
              state.replyDrafts[pid] = prevDraft;
            } else {
              state.adjustDrafts[pid] = prevDraft;
            }
            const curInput = document.getElementById(inputId);
            if (curInput) curInput.value = prevDraft;
            throw innerErr;
          }
        }
      );
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
          state.lastEventsSignature = null;
          state.lastChatSig = null;
          renderDetail();
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
        panelEl.removeAttribute('role');
        panelEl.removeAttribute('aria-modal');
        if (state.releasePreviewTrap) {
          state.releasePreviewTrap();
          state.releasePreviewTrap = null;
        }
        if (pid && state.previewOpen[pid] && !localApp.running) {
          state.previewOpen[pid] = false;
          delete state.previewRenderedUrl[pid];
          iframeEl.src = 'about:blank';
        }
        return;
      }

      splitEl.classList.add('preview-open');
      panelEl.style.display = 'flex';
      if (window.innerWidth < 1280) {
        panelEl.setAttribute('role', 'dialog');
        panelEl.setAttribute('aria-modal', 'true');
        panelEl.setAttribute('aria-label', 'Vista previa visual');
        if (!state.releasePreviewTrap) {
          state.releasePreviewTrap = trapDialogFocus(panelEl, () => {
            if (closeBtn) closeBtn.click();
          });
        }
      } else {
        panelEl.removeAttribute('role');
        panelEl.removeAttribute('aria-modal');
        if (state.releasePreviewTrap) {
          state.releasePreviewTrap();
          state.releasePreviewTrap = null;
        }
      }
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
          pid,
          label: 'Aplicando cambios y ejecutando pruebas...',
          buttonLoadingText: 'Aplicando...',
          subtext: 'El agente modificará el repositorio y correrá la verificación automática.',
          buttonEl: triggerBtn || 'modify-project-btn',
          inlineTargetId: 'modify-inline-status',
        },
        async () => {
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
          setNote('✓ Cambios en curso para ' + pid + '.', 'ok');
          await loadProjects();
        }
      );
    }

    async function submitQualityAction(pid, actionKind, triggerBtn) {
      if (!pid) return;
      const isPolish = actionKind === 'polish';
      const isImprove = actionKind === 'improve' || isPolish;
      const defaultBtnId = isPolish
        ? 'polish-debate-btn'
        : (isImprove ? 'improve-quality-btn' : 'evaluate-quality-btn');
      const labelTxt = isPolish
        ? 'Ejecutando debate adversario (Crítico vs. Constructor)...'
        : (isImprove
            ? 'Mejorando proyecto hasta estándar Producción...'
            : 'Evaluando calidad integral (6 áreas)...');
      const subTxt = isPolish
        ? 'Crítico y Constructor refinan código, pruebas y git diff (máx. 2 rondas).'
        : (isImprove
            ? 'Refinamiento acotado (máx. 2 rondas), pytest y smoke test.'
            : 'Inspeccionando el código en modo solo lectura.');
      await runTrackedOperation(
        {
          pid,
          label: labelTxt,
          subtext: subTxt,
          buttonEl: triggerBtn || defaultBtnId,
          inlineTargetId: 'history-quality-inline-status',
        },
        async () => {
          const endpoint = isImprove ? '/quality/improve' : '/quality/evaluate';
          const reqBody = isPolish ? { force_polish: true } : {};
          const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reqBody),
          });
          if (res && Array.isArray(res.checkpoints)) {
            state.checkpointsByPid[pid] = res.checkpoints;
          }
          delete state.chatLoadedFor[pid];
          delete state.devMetaLoadedSig[pid];
          state.lastEventsSignature = null;
          state.lastModifySignature = null;
          state.lastBannerSignature = null;
          state.lastChatSig = null;
          if (isPolish) {
            setNote('✓ Debate adversario de pulido iniciado para ' + pid + '.', 'ok');
          } else if (isImprove) {
            if (res && res.already_passed) {
              const tier = classifyScorecardTier(res.scorecard || {});
              setNote('✓ El proyecto ya cumple el estándar (' + tier.label + ').', 'ok');
            } else {
              setNote('✓ Ciclo de mejora de calidad iniciado para ' + pid + '.', 'ok');
            }
          } else {
            const sc = (res && (res.scorecard || res.quality_scorecard)) || {};
            const tier = classifyScorecardTier(sc);
            const pctStr = tier.pct !== null ? (' · ' + tier.pct + '%') : '';
            setNote('✓ Evaluación de calidad completada: ' + tier.label + pctStr + '.', 'ok');
            await loadProjectChatHistory(pid);
          }
          await loadProjects();
        }
      );
    }

    function submitEvaluateQuality(pid, triggerBtn) {
      return submitQualityAction(pid, 'evaluate', triggerBtn);
    }

    function submitImproveQuality(pid, triggerBtn) {
      return submitQualityAction(pid, 'improve', triggerBtn);
    }

    function submitPolishWithDebate(pid, triggerBtn) {
      return submitQualityAction(pid, 'polish', triggerBtn);
    }

    async function submitRollbackProject(pid, commitSha, triggerBtn, inlineTargetId) {
      if (!commitSha) return;
      const confirmed = await askCustomConfirm({
        title: 'Restaurar versión estable',
        body: '¿Deseas restaurar este proyecto a una versión anterior estable? Se verificarán las pruebas automáticas antes de aplicar el cambio.',
        confirmLabel: 'Sí, restaurar versión',
      });
      if (!confirmed) return;
      await runTrackedOperation(
        {
          pid,
          label: 'Restaurando versión estable y ejecutando pruebas...',
          subtext: 'Verificando que el proyecto pase pruebas tras el rollback.',
          buttonEl: triggerBtn || 'rollback-version-btn',
          inlineTargetId: inlineTargetId || 'modify-inline-status',
        },
        async () => {
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
          state.lastEventsSignature = null;
          setNote('✓ Versión restaurada con éxito.', 'ok');
          await loadProjects();
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
          const delOk = await askCustomConfirm({
            title: 'Eliminar proyecto',
            body: '¿Deseas eliminar el proyecto "' + pid + '"? Esta acción no se puede deshacer.',
            confirmLabel: 'Eliminar proyecto',
          });
          if (!delOk) return;
          await runTrackedOperation(
            {
              pid,
              label: 'Eliminando proyecto...',
              buttonLoadingText: 'Eliminando...',
              subtext: 'Limpiando registro y recursos locales.',
              buttonEl: triggerBtn,
            },
            async () => {
              await apiFetch('/api/projects/' + encodeURIComponent(pid), { method: 'DELETE' });
              setNote('Proyecto ' + pid + ' eliminado.');
              if (state.selectedId === pid) {
                state.selectedId = null;
                state.detail = null;
              }
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'approve') {
          await runTrackedOperation(
            {
              pid,
              label: 'Aprobando plan e iniciando construcción...',
              buttonLoadingText: 'Iniciando...',
              subtext: 'Preparando pruebas de aceptación y generador de código.',
              buttonEl: triggerBtn,
            },
            async () => {
              await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/approve', { method: 'POST' });
              setNote('✓ Construcción iniciada para ' + pid + '.', 'ok');
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'retry') {
          await runTrackedOperation(
            {
              pid,
              label: 'Reintentando etapa del proyecto...',
              buttonLoadingText: 'Reintentando...',
              subtext: 'Recuperando estado y preparando el siguiente paso...',
              buttonEl: triggerBtn,
            },
            async () => {
              await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/retry', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clean_workspace: false }),
              });
              setNote('Reintento iniciado para ' + pid + '.');
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'retry-interview') {
          await runTrackedOperation(
            {
              pid,
              label: 'Generando preguntas de aclaración...',
              buttonLoadingText: 'Generando...',
              subtext: 'Consultando al entrevistador para definir los detalles del proyecto...',
              buttonEl: triggerBtn || 'generate-questions-btn',
              inlineTargetId: 'interview-retry-inline-status',
            },
            async () => {
              await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/retry', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clean_workspace: false }),
              });
              setNote('✓ Preguntas de aclaración generadas para ' + pid + '.', 'ok');
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'retry-clean') {
          const cleanOk = await askCustomConfirm({
            title: 'Reconstruir desde cero',
            body: '¿Deseas borrar el espacio de trabajo actual de "' + pid + '" y reconstruir desde cero?',
            confirmLabel: 'Sí, reconstruir',
          });
          if (!cleanOk) return;
          await runTrackedOperation(
            {
              pid,
              label: 'Reconstruyendo desde cero...',
              buttonLoadingText: 'Reconstruyendo...',
              subtext: 'Limpiando espacio de trabajo e iniciando desde cero.',
              buttonEl: triggerBtn,
            },
            async () => {
              await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/retry', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clean_workspace: true }),
              });
              setNote('Reconstrucción desde cero iniciada para ' + pid + '.');
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'local-start') {
          await runTrackedOperation(
            {
              pid,
              label: 'Preparando entorno virtual e instalando dependencias...',
              buttonLoadingText: 'Encendiendo...',
              subtext: 'Iniciando servidor local y verificando respuesta HTTP...',
              buttonEl: triggerBtn,
              inlineTargetId: 'local-inline-status',
            },
            async () => {
              const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/runtime/start', { method: 'POST', timeoutMs: 180000 });
              const url = res.local_app && res.local_app.url ? res.local_app.url : '';
              setNote(url ? ('✓ App encendida en ' + url) : ('✓ App encendida para ' + pid), 'ok');
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        } else if (action === 'local-stop') {
          await runTrackedOperation(
            {
              pid,
              label: 'Apagando servidor local...',
              buttonLoadingText: 'Apagando...',
              subtext: 'Deteniendo proceso local de forma segura.',
              buttonEl: triggerBtn,
              inlineTargetId: 'local-inline-status',
            },
            async () => {
              state.previewOpen[pid] = false;
              delete state.previewRenderedUrl[pid];
              await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/stop', { method: 'POST' });
              setNote('App local apagada para ' + pid + '.');
              state.lastBannerSignature = null;
              await loadProjects();
            }
          );
          return;
        }
        state.lastBannerSignature = null;
        await loadProjects();
      } catch (err) {
        setNote('Error en ' + action + ': ' + err.message, 'err');
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
          state.lastListSignature = null;
          flashSavedElement('detail-title');
          setNote(
            renameGithubRepo && res && res.repo_url
              ? '✓ Título y repositorio en GitHub guardados.'
              : '✓ Título guardado.',
            'ok'
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
            flashSavedElement('detail-logo-img');
            state.lastListSignature = null;
            setNote('✓ Logo actualizado.', 'ok');
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
      const mobileNewBtn = document.getElementById('mobile-new-project-shortcut');
      const emptyDetailBtn = document.getElementById('empty-detail-create-btn');
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
      if (mobileNewBtn) {
        mobileNewBtn.addEventListener('click', openNewProjectComposer);
      }
      if (emptyDetailBtn) {
        emptyDetailBtn.addEventListener('click', openNewProjectComposer);
      }

      document.querySelectorAll('#example-prompts .example-chip').forEach((chip) => {
        chip.addEventListener('click', () => {
          const ex = chip.getAttribute('data-example') || '';
          if (ex && input) {
            input.value = ex;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
          }
        });
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
        await runTrackedOperation(
          {
            label: 'Creando proyecto y preparando entrevista...',
            buttonLoadingText: 'Creando...',
            subtext: 'Analizando tu idea inicial para generar las primeras preguntas.',
            buttonEl: submitBtn,
          },
          async () => {
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
              state.lastListSignature = null;
            }
            setNote('✓ Proyecto iniciado: ' + (res.project_id || '') + '.', 'ok');
            await loadProjects();
          }
        );
      });
    })();

    (function initMobileSwitcher() {
      const swBtn = document.getElementById('mobile-project-switcher');
      const sidePanel = document.getElementById('sidebar-panel');
      if (!swBtn || !sidePanel) return;
      swBtn.addEventListener('click', () => {
        const collapsed = sidePanel.classList.toggle('sidebar-collapsed');
        swBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
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

      let releaseModalTrap = null;
      function closeModal() {
        modal.style.display = 'none';
        pathInput.value = '';
        if (titleInput) titleInput.value = '';
        const stEl = document.getElementById('import-inline-status');
        if (stEl) stEl.innerHTML = '';
        if (releaseModalTrap) {
          releaseModalTrap();
          releaseModalTrap = null;
        }
      }

      openBtn.addEventListener('click', () => {
        modal.style.display = 'flex';
        releaseModalTrap = trapDialogFocus(modal, closeModal);
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
              state.lastListSignature = null;
            }
            setNote('✓ Proyecto importado: ' + (res.project_id || '') + '.', 'ok');
            await loadProjects();
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
          const msg = state.voiceTts ? 'Lectura en voz alta activada' : 'Lectura en voz alta desactivada';
          showSettingsFeedback('✓ ' + msg);
          setNote('✓ ' + msg, 'ok');
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
            const msg = res.lan_mode
              ? ('Acceso Wi-Fi activado' + (res.lan_ip ? (' (' + res.lan_ip + ')') : '') + '. Apaga y enciende tu app para aplicar.')
              : 'Acceso Wi-Fi desactivado (solo 127.0.0.1).';
            showSettingsFeedback('✓ Preferencia Wi-Fi guardada');
            setNote('✓ ' + msg, 'ok');
          } catch (err) {
            lanToggle.checked = !desired;
            setNote('Error cambiando acceso Wi-Fi: ' + err.message, 'err');
          }
        });
      }
    })();

    (function initFactoryImproverModal() {
      const openBtn = document.getElementById('improve-factory-btn');
      const modal = document.getElementById('factory-improve-modal');
      const closeBtn = document.getElementById('close-factory-improve-btn');
      const memToggle = document.getElementById('factory-memory-toggle');
      const memPanel = document.getElementById('factory-memory-panel');
      const progressCard = document.getElementById('factory-progress-card');
      const progressTitle = document.getElementById('factory-progress-title');
      const progressTimer = document.getElementById('factory-progress-timer');
      const progressDetail = document.getElementById('factory-progress-detail');
      const buildBanner = document.getElementById('factory-build-banner');
      const buildBannerTitle = document.getElementById('factory-build-banner-title');
      const buildBannerDetail = document.getElementById('factory-build-banner-detail');
      const buildBannerTimer = document.getElementById('factory-build-banner-timer');
      const fsOverlay = document.getElementById('factory-fullscreen-build-overlay');
      const fsMinBtn = document.getElementById('factory-fs-minimize-btn');
      const fsTitle = document.getElementById('factory-fs-title');
      const fsTimer = document.getElementById('factory-fs-timer');
      const fsDetail = document.getElementById('factory-fs-detail');
      const streamEl = document.getElementById('factory-chat-stream');
      const previewsEl = document.getElementById('factory-paste-previews');
      const formEl = document.getElementById('factory-chat-form');
      const inputEl = document.getElementById('factory-chat-input');
      const attachBtn = document.getElementById('factory-attach-btn');
      const fileInputEl = document.getElementById('factory-image-input');
      const sendBtn = document.getElementById('factory-chat-send-btn');
      const statusEl = document.getElementById('factory-inline-status');
      const restartBanner = document.getElementById('factory-restart-banner');
      if (!openBtn || !modal || !streamEl) return;

      const DEFAULT_OPEN_BTN_LABEL = '🏭 Mejorar Fábrica';
      let pendingImages = [];
      let reconnectTimer = null;
      let statusPollTimer = null;
      let clockTickTimer = null;
      let applyInFlight = false;
      let consultInFlight = false;
      let buildOverlayMinimized = false;
      let lastServerRunning = false;
      let lastActiveStep = 0;
      let allFactoryCodeBlocks = [];
      const locallyQueuedProposalIds = new Set();
      let activeStatus = {
        running: false,
        step: 0,
        total_steps: 3,
        stage: '',
        detail: '',
        title: '',
        elapsed_seconds: 0,
        last_outcome: '',
        syncedAtMs: Date.now(),
      };

      function isBlockingApplyActive() {
        return !!(applyInFlight || (activeStatus.running && !window.__liwiStagingActive));
      }

      function formatMmSs(totalSecs) {
        const secs = Math.max(0, Math.floor(Number(totalSecs) || 0));
        const mins = Math.floor(secs / 60);
        const rem = secs % 60;
        return String(mins).padStart(2, '0') + ':' + String(rem).padStart(2, '0');
      }

      function currentElapsedSeconds() {
        const base = Number(activeStatus.elapsed_seconds) || 0;
        if (!activeStatus.running) return base;
        const delta = Math.max(0, (Date.now() - (activeStatus.syncedAtMs || Date.now())) / 1000);
        return base + delta;
      }

      function setFactoryControlsLocked(locked) {
        if (sendBtn) sendBtn.disabled = !!(locked || consultInFlight);
        if (inputEl) inputEl.disabled = !!locked;
        if (attachBtn) attachBtn.disabled = !!locked;
        streamEl.querySelectorAll('button[data-apply-factory-proposal]').forEach((b) => {
          if (locked) {
            b.disabled = true;
          } else {
            b.disabled = b.getAttribute('data-applied') === '1' || b.getAttribute('data-enqueuing') === '1';
          }
        });
      }

      function updateLiveClockAndHeader() {
        const isRunning = isBlockingApplyActive();
        const mmss = formatMmSs(currentElapsedSeconds());
        if (isRunning) {
          const stepNum = Math.max(1, Number(activeStatus.step) || 1);
          const totalSteps = Math.max(3, Number(activeStatus.total_steps) || 3);
          openBtn.classList.add('is-building');
          openBtn.textContent = '🔨 Construyendo mejora (Paso ' + stepNum + '/' + totalSteps + ' · ' + mmss + ')';
          if (buildBannerTimer) buildBannerTimer.textContent = mmss;
          if (progressTimer) progressTimer.textContent = mmss;
          if (fsTimer) fsTimer.textContent = mmss;
        } else {
          openBtn.classList.remove('is-building');
          openBtn.textContent = DEFAULT_OPEN_BTN_LABEL;
        }
      }

      function stopClockTicker() {
        if (clockTickTimer) {
          clearInterval(clockTickTimer);
          clockTickTimer = null;
        }
      }

      function ensureClockTicker() {
        if (!clockTickTimer) {
          clockTickTimer = setInterval(updateLiveClockAndHeader, 1000);
        }
      }

      function stopStatusPolling() {
        if (statusPollTimer) {
          clearInterval(statusPollTimer);
          statusPollTimer = null;
        }
      }

      function ensureStatusPolling() {
        if (!statusPollTimer) {
          statusPollTimer = setInterval(() => {
            pollFactoryStatus();
          }, 2500);
        }
      }

      function syncFullscreenOverlay(isRunning, stepNum, headline, detailTxt) {
        if (!fsOverlay) return;
        if (!isRunning || buildOverlayMinimized) {
          fsOverlay.style.display = 'none';
          return;
        }
        fsOverlay.style.display = 'flex';
        if (fsTitle) fsTitle.textContent = headline || 'Aplicando mejora...';
        if (fsDetail) fsDetail.textContent = detailTxt || 'Procesando mejora en Liwi...';
        fsOverlay.querySelectorAll('.factory-step-pill[data-fsstep]').forEach((pill) => {
          const pStep = Number(pill.getAttribute('data-fsstep') || 0);
          pill.classList.toggle('is-done', pStep < stepNum);
          pill.classList.toggle('is-active', pStep === stepNum);
        });
      }

      function finishWithUpdatedReload(messageText) {
        stopStatusPolling();
        stopClockTicker();
        if (reconnectTimer) {
          clearInterval(reconnectTimer);
          reconnectTimer = null;
        }
        applyInFlight = false;
        lastServerRunning = false;
        activeStatus.running = false;
        if (restartBanner) restartBanner.style.display = 'none';
        if (buildBanner) buildBanner.style.display = 'none';
        if (fsOverlay) {
          fsOverlay.style.display = 'flex';
          if (fsTitle) fsTitle.textContent = messageText || '✓ ¡Liwi actualizado! Recargando...';
          if (fsDetail) fsDetail.textContent = 'La verificación completa pasó en verde y Liwi ya reinició.';
          fsOverlay.querySelectorAll('.factory-step-pill[data-fsstep]').forEach((pill) => {
            pill.classList.remove('is-active');
            pill.classList.add('is-done');
          });
        }
        if (statusEl) statusEl.textContent = '✅ Mejora aplicada y Liwi actualizado';
        setFactoryControlsLocked(false);
        setTimeout(() => { window.location.reload(); }, 650);
      }

      function clearFactoryBlockingForStaging() {
        if (applyInFlight) return;
        stopStatusPolling();
        stopClockTicker();
        if (fsOverlay) fsOverlay.style.display = 'none';
        if (buildBanner) buildBanner.style.display = 'none';
        if (progressCard) progressCard.style.display = 'none';
        setFactoryControlsLocked(false);
        updateLiveClockAndHeader();
      }
      window.clearFactoryBlockingForStaging = clearFactoryBlockingForStaging;

      function applyStatusSnapshot(st) {
        const s = st && typeof st === 'object' ? st : {};
        const wasRunning = lastServerRunning;
        const isRunning = !!s.running;
        const prevStep = lastActiveStep;
        activeStatus = {
          running: isRunning,
          step: Number(s.step) || 0,
          total_steps: Number(s.total_steps) || 3,
          stage: String(s.stage || ''),
          detail: String(s.detail || ''),
          title: String(s.title || ''),
          elapsed_seconds: Number(s.elapsed_seconds) || 0,
          last_outcome: String(s.last_outcome || ''),
          syncedAtMs: Date.now(),
        };
        lastServerRunning = isRunning;

        if (window.__liwiStagingActive && !applyInFlight) {
          clearFactoryBlockingForStaging();
          if (typeof window.refreshSelfImproveStaging === 'function') {
            window.refreshSelfImproveStaging();
          }
          if (wasRunning && !isRunning) {
            refreshHistory();
          }
          return;
        }

        if (isRunning) {
          ensureStatusPolling();
          ensureClockTicker();
          setFactoryControlsLocked(true);
          const stepNum = Math.max(1, activeStatus.step || 1);
          lastActiveStep = stepNum;
          const totalSteps = Math.max(3, activeStatus.total_steps || 3);
          const headline = activeStatus.title || activeStatus.stage || ('Paso ' + stepNum + '/' + totalSteps);
          const detailLine = (activeStatus.stage ? (activeStatus.stage + ': ') : '') + (activeStatus.detail || 'Procesando mejora en Liwi...');
          if (buildBanner) buildBanner.style.display = 'block';
          if (buildBannerTitle) buildBannerTitle.textContent = '🔨 ' + headline;
          if (buildBannerDetail) buildBannerDetail.textContent = detailLine;
          if (progressCard) {
            progressCard.style.display = 'flex';
            if (progressTitle) progressTitle.textContent = '🔨 ' + headline;
            if (progressDetail) progressDetail.textContent = detailLine;
            progressCard.querySelectorAll('.factory-step-pill').forEach((pill) => {
              const pStep = Number(pill.getAttribute('data-fstep') || 0);
              pill.classList.toggle('is-done', pStep < stepNum);
              pill.classList.toggle('is-active', pStep === stepNum);
            });
          }
          syncFullscreenOverlay(true, stepNum, headline, detailLine);
          updateLiveClockAndHeader();
          return;
        }

        if (!applyInFlight && !reconnectTimer) {
          stopStatusPolling();
          stopClockTicker();
        }
        if (buildBanner) buildBanner.style.display = 'none';
        if (progressCard) progressCard.style.display = 'none';
        updateLiveClockAndHeader();

        if (wasRunning && !applyInFlight && !reconnectTimer) {
          setFactoryControlsLocked(false);
          if (activeStatus.last_outcome === 'applied' || prevStep >= 3) {
            finishWithUpdatedReload('✓ ¡Liwi actualizado! Recargando...');
            return;
          }
          if (fsOverlay) fsOverlay.style.display = 'none';
          if (statusEl && activeStatus.last_outcome === 'rolled_back') {
            statusEl.textContent = 'Mejora revertida automáticamente tras fallo en verificación.';
          }
          refreshHistory();
        } else if (!isRunning && fsOverlay && !applyInFlight && !reconnectTimer) {
          fsOverlay.style.display = 'none';
        }
      }

      async function pollFactoryStatus() {
        try {
          const st = await apiFetch('/api/factory/assistant/status', { cache: 'no-store' });
          applyStatusSnapshot(st);
          return st;
        } catch (_) {
          return null;
        }
      }

      function startReconnectPolling() {
        if (restartBanner) restartBanner.style.display = 'block';
        if (buildBanner) buildBanner.style.display = 'none';
        if (fsOverlay && !buildOverlayMinimized) {
          fsOverlay.style.display = 'flex';
          if (fsTitle) fsTitle.textContent = 'Guardando y reiniciando Liwi...';
          if (fsDetail) fsDetail.textContent = 'Esperando reconexión automática tras verificar todas las pruebas.';
        }
        stopStatusPolling();
        if (reconnectTimer) clearInterval(reconnectTimer);
        let sawDrop = false;
        const startedAt = Date.now();
        reconnectTimer = setInterval(async () => {
          try {
            const resp = await fetch('/api/factory/assistant/status', { cache: 'no-store' });
            if (resp.ok) {
              const st = await resp.json().catch(() => ({}));
              if (!st.running && (sawDrop || (Date.now() - startedAt) > 1200)) {
                finishWithUpdatedReload('✓ ¡Liwi actualizado! Recargando...');
              }
            } else {
              sawDrop = true;
            }
          } catch (_) {
            sawDrop = true;
          }
        }, 1200);
      }

      function autoResizeFactoryInput() {
        if (!inputEl) return;
        inputEl.style.height = 'auto';
        const maxH = 180;
        const sh = inputEl.scrollHeight || 0;
        if (sh > 0) {
          inputEl.style.height = Math.min(sh, maxH) + 'px';
          inputEl.style.overflowY = sh > maxH ? 'auto' : 'hidden';
        } else {
          inputEl.style.overflowY = 'hidden';
        }
      }

      function renderPreviews() {
        if (!previewsEl) return;
        previewsEl.style.display = pendingImages.length ? 'flex' : 'none';
        previewsEl.innerHTML = pendingImages.map((img, idx) =>
          '<span class="factory-paste-chip">' +
            '<img src="' + escapeHtml(img.previewUrl) + '" alt="' + escapeHtml(img.filename) + '" />' +
            '<span class="factory-paste-chip-name">' + escapeHtml(img.filename) + '</span>' +
            '<button type="button" class="btn-icon" data-rm-paste="' + idx + '" aria-label="Quitar captura">✕</button>' +
          '</span>'
        ).join('');
        previewsEl.querySelectorAll('button[data-rm-paste]').forEach((b) => {
          b.addEventListener('click', () => {
            const i = Number(b.getAttribute('data-rm-paste'));
            pendingImages.splice(i, 1);
            renderPreviews();
          });
        });
      }

      function appendImageFile(file) {
        if (!file || !String(file.type || '').startsWith('image/')) return;
        if (pendingImages.length >= 4) {
          if (statusEl) statusEl.textContent = 'Máximo 4 capturas por mensaje.';
          return;
        }
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = String(reader.result || '');
          const b64Idx = dataUrl.indexOf('base64,');
          const b64 = b64Idx >= 0 ? dataUrl.slice(b64Idx + 7) : dataUrl;
          pendingImages.push({
            filename: file.name || ('captura_' + (pendingImages.length + 1) + '.png'),
            data_base64: b64,
            previewUrl: dataUrl,
          });
          renderPreviews();
        };
        reader.readAsDataURL(file);
      }

      function renderMemory(rules) {
        const list = Array.isArray(rules) ? rules : [];
        memToggle.textContent = '🧠 Memoria de Liwi (' + list.length + ')';
        if (!list.length) {
          memPanel.innerHTML = '<div class="muted">Sin reglas guardadas todavía. Las preferencias detectadas en el chat aparecerán aquí.</div>';
          return;
        }
        memPanel.innerHTML = list.map((r) =>
          '<div class="factory-memory-item">' +
            '<span>• ' + escapeHtml(String(r.rule_text || '')) + '</span>' +
            '<button type="button" class="btn btn-sm" data-del-frule="' + escapeHtml(String(r.id || '')) + '">Eliminar</button>' +
          '</div>'
        ).join('');
        memPanel.querySelectorAll('button[data-del-frule]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const rid = btn.getAttribute('data-del-frule');
            if (!rid) return;
            btn.disabled = true;
            try {
              const res = await apiFetch('/api/factory/assistant/memory/' + encodeURIComponent(rid), { method: 'DELETE' });
              renderMemory(res.memory_rules || []);
            } catch (err) {
              if (statusEl) statusEl.textContent = 'Error eliminando regla: ' + err.message;
              btn.disabled = false;
            }
          });
        });
      }

      async function copyTextToClipboard(text) {
        const value = String(text == null ? '' : text);
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          try {
            await navigator.clipboard.writeText(value);
            return true;
          } catch (_) {
            // Fallback to execCommand('copy') when Clipboard API is unavailable or rejected (HTTP/Tailscale/mobile)
          }
        }
        const ta = document.createElement('textarea');
        ta.value = value;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-9999px';
        ta.style.left = '-9999px';
        ta.style.opacity = '0';
        ta.style.pointerEvents = 'none';
        document.body.appendChild(ta);
        try {
          if (typeof ta.focus === 'function') {
            try { ta.focus({ preventScroll: true }); } catch (_) { ta.focus(); }
          }
          ta.select();
          if (typeof ta.setSelectionRange === 'function') {
            ta.setSelectionRange(0, value.length);
          }
          return Boolean(document.execCommand('copy'));
        } catch (_) {
          return false;
        } finally {
          if (ta.parentNode) ta.parentNode.removeChild(ta);
        }
      }

      function extractCleanCopyText(node) {
        if (!node) return '';
        const clone = node.cloneNode(true);
        clone.querySelectorAll('.copy-msg-btn, .md-copy-btn, button, .factory-proposal-actions, .md-code-head, .factory-pre-bar').forEach((el) => {
          el.remove();
        });
        return String(clone.textContent || '').trim();
      }

      function createCopyButton(getTextFn, ariaLabel) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'copy-msg-btn';
        btn.setAttribute('aria-label', ariaLabel || 'Copiar contenido');
        btn.textContent = '📋 Copiar';
        let resetTimer = null;
        btn.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          const cleanText = typeof getTextFn === 'function' ? String(getTextFn() || '').trim() : '';
          if (!cleanText) return;
          await copyTextToClipboard(cleanText);
          if (resetTimer) {
            clearTimeout(resetTimer);
            resetTimer = null;
          }
          btn.textContent = '✓ Copiado';
          btn.classList.add('is-copied');
          resetTimer = setTimeout(() => {
            btn.textContent = '📋 Copiar';
            btn.classList.remove('is-copied');
            resetTimer = null;
          }, 1500);
        });
        return btn;
      }

      function attachFactoryCopyButtons(rootEl, assistantRawTexts) {
        if (!rootEl) return;
        const rawList = Array.isArray(assistantRawTexts) ? assistantRawTexts : [];

        // 1. Copy button on each assistant (Liwi) message
        rootEl.querySelectorAll('.factory-msg-assistant').forEach((msgEl) => {
          if (msgEl.id === 'factory-thinking-bubble') return;
          const headEl = msgEl.querySelector('.factory-msg-head');
          if (!headEl || headEl.querySelector('.copy-msg-btn')) return;
          const bodyEl = msgEl.querySelector('.factory-msg-body');
          const rawIdxAttr = msgEl.getAttribute('data-assistant-idx');
          const rawIdx = rawIdxAttr !== null ? Number(rawIdxAttr) : -1;
          const rawText = rawIdx >= 0 && typeof rawList[rawIdx] === 'string' ? rawList[rawIdx].trim() : '';
          const msgCopyBtn = createCopyButton(
            () => rawText || extractCleanCopyText(bodyEl),
            'Copiar mensaje de Liwi'
          );
          headEl.appendChild(msgCopyBtn);
        });

        // 2. Copy button on each proposal box
        rootEl.querySelectorAll('.factory-proposal-card').forEach((cardEl) => {
          const preEl = cardEl.querySelector('.doctor-prompt-pre');
          if (preEl) preEl.setAttribute('data-copy-attached', '1');
          const headEl = cardEl.querySelector('.factory-proposal-head');
          if (!headEl || headEl.querySelector('.copy-msg-btn') || !preEl) return;
          const propCopyBtn = createCopyButton(
            () => extractCleanCopyText(preEl),
            'Copiar propuesta'
          );
          headEl.appendChild(propCopyBtn);
        });

        // 3. Upgrade markdown code block copy buttons inside #factory-chat-stream (zero duplicates)
        rootEl.querySelectorAll('.md-code-wrap').forEach((wrapEl) => {
          const preEl = wrapEl.querySelector('pre');
          if (preEl) preEl.setAttribute('data-copy-attached', '1');
          const headEl = wrapEl.querySelector('.md-code-head');
          if (!headEl || headEl.querySelector('.copy-msg-btn')) return;
          const legacyBtn = headEl.querySelector('button.md-copy-btn[data-md-code-idx]');
          const idx = legacyBtn ? Number(legacyBtn.getAttribute('data-md-code-idx')) : -1;
          const entry = idx >= 0 ? allFactoryCodeBlocks[idx] : null;
          const codeCopyBtn = createCopyButton(
            () => (entry && typeof entry.raw === 'string' ? entry.raw : extractCleanCopyText(preEl)),
            'Copiar bloque de código'
          );
          if (legacyBtn) {
            legacyBtn.replaceWith(codeCopyBtn);
          } else {
            headEl.appendChild(codeCopyBtn);
          }
        });

        // 4. Any standalone <pre> inside #factory-chat-stream not already wrapped
        rootEl.querySelectorAll('pre').forEach((preEl) => {
          if (
            preEl.getAttribute('data-copy-attached') === '1' ||
            preEl.closest('.md-code-wrap') ||
            preEl.closest('.factory-proposal-card') ||
            preEl.closest('.factory-pre-wrap')
          ) {
            return;
          }
          preEl.setAttribute('data-copy-attached', '1');
          const wrapEl = document.createElement('div');
          wrapEl.className = 'factory-pre-wrap';
          const barEl = document.createElement('div');
          barEl.className = 'factory-pre-bar';
          barEl.appendChild(createCopyButton(() => extractCleanCopyText(preEl), 'Copiar bloque'));
          if (preEl.parentNode) {
            preEl.parentNode.insertBefore(wrapEl, preEl);
            wrapEl.appendChild(barEl);
            wrapEl.appendChild(preEl);
          }
        });
      }

      function renderMessages(msgs) {
        const list = Array.isArray(msgs) ? msgs : [];
        if (!list.length) {
          allFactoryCodeBlocks = [];
          streamEl.innerHTML = '<div class="factory-chat-empty">¿Qué quieres consultar o mejorar en Liwi?</div>';
          return;
        }
        allFactoryCodeBlocks = [];
        const assistantRawTexts = [];
        const isLocked = isBlockingApplyActive();
        streamEl.innerHTML = list.map((m) => {
          const isUser = m.role === 'user';
          const cls = isUser ? 'factory-msg factory-msg-user' : 'factory-msg factory-msg-assistant';
          const imgs = Array.isArray(m.image_urls) ? m.image_urls : [];
          const thumbs = imgs.length
            ? '<div class="factory-chat-thumbs">' + imgs.map((u) => '<img src="' + escapeHtml(String(u)) + '" alt="Captura adjunta" />').join('') + '</div>'
            : '';
          let propHtml = '';
          if (!isUser && m.proposal_prompt) {
            const msgIdStr = String(m.id || '');
            const isApplied = m.status === 'applied';
            const isQueuedLocal = !isApplied && msgIdStr !== '' && locallyQueuedProposalIds.has(msgIdStr);
            const isDisabledProp = isApplied || isQueuedLocal || isLocked;
            const applyLabel = isApplied
              ? '✓ Mejora aplicada'
              : (isQueuedLocal ? '✓ Encolada en staging' : '✨ Aplicar mejora a Liwi');
            const queuedNoticeHtml = isQueuedLocal
              ? '<div class="factory-staging-notice" role="status">✓ Mejora encolada en segundo plano (staging)</div>'
              : '';
            propHtml = '<div class="factory-proposal-card">' +
              '<div class="factory-proposal-head">' +
                '<div class="factory-proposal-title">Propuesta: ' + escapeHtml(String(m.proposal_title || 'Auto-mejora')) + '</div>' +
              '</div>' +
              '<pre class="doctor-prompt-pre">' + escapeHtml(String(m.proposal_prompt)) + '</pre>' +
              '<div class="factory-proposal-actions">' +
                '<button type="button" class="btn btn-primary btn-sm" data-apply-factory-proposal="1" data-applied="' + ((isApplied || isQueuedLocal) ? '1' : '0') + '" data-msg-id="' + escapeHtml(msgIdStr) + '" data-prop-title="' + escapeHtml(String(m.proposal_title || '')) + '"' + (isDisabledProp ? ' disabled' : '') + '>' + applyLabel + '</button>' +
              '</div>' +
              queuedNoticeHtml +
            '</div>';
          }
          let assistantIdxAttr = '';
          if (!isUser) {
            const aIdx = assistantRawTexts.length;
            assistantRawTexts.push(String(m.content || ''));
            assistantIdxAttr = ' data-assistant-idx="' + aIdx + '"';
          }
          const roleHtml = isUser ? '' : '<div class="factory-msg-head"><div class="factory-msg-role">Liwi</div></div>';
          const parsed = renderSafeMarkdown(String(m.content || ''));
          const baseOffset = allFactoryCodeBlocks.length;
          parsed.codeBlocks.forEach((cb) => allFactoryCodeBlocks.push(cb));
          const bodyHtml = parsed.html.replace(/data-md-code-idx="(\\d+)"/g, (_mt, n) =>
            'data-md-code-idx="' + (Number(n) + baseOffset) + '"'
          );
          return '<div class="' + cls + '"' + assistantIdxAttr + '>' +
            roleHtml +
            '<div class="factory-msg-body">' + bodyHtml + '</div>' +
            thumbs +
            propHtml +
          '</div>';
        }).join('');
        attachFactoryCopyButtons(streamEl, assistantRawTexts);
        streamEl.scrollTop = streamEl.scrollHeight;
        streamEl.querySelectorAll('button[data-apply-factory-proposal]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            if (btn.disabled || btn.getAttribute('data-enqueuing') === '1' || applyInFlight) return;
            const card = btn.closest('.factory-proposal-card');
            const pre = card ? card.querySelector('.doctor-prompt-pre') : null;
            const promptText = pre ? pre.textContent : '';
            if (!promptText) return;
            const propTitle = btn.getAttribute('data-prop-title') || 'Auto-mejora Liwi';
            const origHtml = btn.innerHTML;
            const startedAtMs = Date.now();
            btn.disabled = true;
            btn.setAttribute('data-enqueuing', '1');
            btn.setAttribute('aria-busy', 'true');
            btn.innerHTML =
              '<span class="btn-spinner" aria-hidden="true"></span>' +
              '<span>Encolando mejora...</span>' +
              ' <span class="staging-live-clock" data-apply-clock="1">0s</span>';
            const applyClockTimer = setInterval(() => {
              const cEl = btn.querySelector('[data-apply-clock]');
              if (cEl) {
                cEl.textContent = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000)) + 's';
              }
            }, 1000);
            if (statusEl) statusEl.textContent = 'Enviando mejora a segundo plano (staging)...';
            try {
              const res = await apiFetch('/api/factory/assistant/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                timeoutMs: 920000,
                body: JSON.stringify({
                  message_id: btn.getAttribute('data-msg-id') || null,
                  proposal_title: propTitle,
                  proposal_prompt: promptText,
                }),
              });
              clearInterval(applyClockTimer);
              btn.removeAttribute('data-enqueuing');
              btn.removeAttribute('aria-busy');
              if (res && res.queued) {
                window.__liwiStagingActive = true;
                const queuedMid = btn.getAttribute('data-msg-id') || '';
                if (queuedMid) locallyQueuedProposalIds.add(queuedMid);
                btn.setAttribute('data-applied', '1');
                btn.disabled = true;
                btn.textContent = '✓ Encolada en staging';
                const confirmText = '✓ Mejora encolada en segundo plano (staging)';
                if (statusEl) statusEl.textContent = confirmText;
                if (card) {
                  const prevNotice = card.querySelector('.factory-staging-notice');
                  if (prevNotice) prevNotice.remove();
                  const noticeEl = document.createElement('div');
                  noticeEl.className = 'factory-staging-notice';
                  noticeEl.setAttribute('role', 'status');
                  noticeEl.textContent = confirmText;
                  card.appendChild(noticeEl);
                }
                if (res.staging && typeof window.applySelfImproveStagingSnapshot === 'function') {
                  window.applySelfImproveStagingSnapshot(res.staging);
                }
                if (typeof window.refreshSelfImproveStaging === 'function') {
                  window.refreshSelfImproveStaging();
                }
                return;
              }
              if (res && res.ok === false) {
                throw new Error(res.error || res.message || 'No se pudo aplicar la mejora.');
              }
              if (res && Array.isArray(res.messages)) {
                renderMessages(res.messages);
              }
              if (res && res.restart_scheduled) {
                if (statusEl) statusEl.textContent = '✓ Mejora aplicada. Reiniciando Liwi...';
                startReconnectPolling();
              } else if (res && res.rolled_back) {
                applyInFlight = false;
                if (fsOverlay) fsOverlay.style.display = 'none';
                applyStatusSnapshot({ running: false, step: 0, total_steps: 3, last_outcome: 'rolled_back' });
                setFactoryControlsLocked(false);
                if (statusEl) statusEl.textContent = 'Mejora revertida automáticamente tras fallo en verificación.';
              } else {
                btn.setAttribute('data-applied', '1');
                btn.disabled = true;
                btn.textContent = '✓ Mejora aplicada';
                if (statusEl) statusEl.textContent = (res && res.message) ? String(res.message) : '✓ Mejora aplicada.';
              }
            } catch (err) {
              clearInterval(applyClockTimer);
              btn.removeAttribute('data-enqueuing');
              btn.removeAttribute('aria-busy');
              btn.disabled = false;
              btn.innerHTML = origHtml;
              const errMsg = 'Error aplicando mejora: ' + (err && err.message ? err.message : String(err || 'Error inesperado'));
              if (statusEl) statusEl.textContent = errMsg;
              if (card) {
                const prevErr = card.querySelector('.factory-staging-notice');
                if (prevErr) prevErr.remove();
                const errNotice = document.createElement('div');
                errNotice.className = 'factory-staging-notice is-error';
                errNotice.setAttribute('role', 'alert');
                errNotice.textContent = errMsg;
                card.appendChild(errNotice);
              }
            }
          });
        });
      }

      async function refreshHistory() {
        try {
          const hist = await apiFetch('/api/factory/assistant/history');
          renderMemory(hist.memory_rules || []);
          renderMessages(hist.messages || []);
        } catch (err) {
          streamEl.innerHTML = '<div class="muted">No se pudo cargar el historial: ' + escapeHtml(err.message) + '</div>';
        }
      }

      const switchViewBtn = document.getElementById('factory-modal-switch-view-btn');
      const modalTitleEl = document.getElementById('factory-improve-title');

      function setFactoryModalView(mode) {
        const nextMode = mode === 'radar' ? 'radar' : 'chat';
        modal.setAttribute('data-view', nextMode);
        if (modalTitleEl) {
          modalTitleEl.textContent = nextMode === 'radar' ? '💡 Centro de Auto-Mejora' : 'Liwi';
        }
        if (switchViewBtn) {
          switchViewBtn.textContent = nextMode === 'radar' ? '💬 Chat con Liwi' : '💡 Ver Radar';
        }
        if (nextMode === 'chat' && inputEl && !isBlockingApplyActive()) {
          setTimeout(() => { try { inputEl.focus(); } catch (_) {} }, 30);
        }
      }
      window.setFactoryModalView = setFactoryModalView;

      if (switchViewBtn) {
        switchViewBtn.addEventListener('click', () => {
          const cur = modal.getAttribute('data-view') === 'radar' ? 'radar' : 'chat';
          setFactoryModalView(cur === 'radar' ? 'chat' : 'radar');
        });
      }

      window.openFactoryImproveModal = (prefillText, opts) => {
        const cfg = opts && typeof opts === 'object' ? opts : {};
        modal.style.display = 'flex';
        setFactoryModalView(cfg.focusRadar ? 'radar' : 'chat');
        if (prefillText && inputEl) {
          inputEl.value = prefillText;
        }
        autoResizeFactoryInput();
        refreshHistory();
        pollFactoryStatus();
        if (typeof window.refreshLiwiRadarIdeas === 'function') {
          window.refreshLiwiRadarIdeas();
        }
      };

      openBtn.addEventListener('click', () => window.openFactoryImproveModal(''));
      const autoIdeasBtn = document.getElementById('auto-ideas-btn');
      if (autoIdeasBtn) {
        autoIdeasBtn.addEventListener('click', () => window.openFactoryImproveModal('', { focusRadar: true }));
      }
      if (fsMinBtn) {
        fsMinBtn.addEventListener('click', () => {
          buildOverlayMinimized = true;
          if (fsOverlay) fsOverlay.style.display = 'none';
        });
      }
      if (buildBanner) {
        const reopenBuildOverlayOrModal = () => {
          if (isBlockingApplyActive()) {
            buildOverlayMinimized = false;
            if (fsOverlay) fsOverlay.style.display = 'flex';
          } else {
            window.openFactoryImproveModal('');
          }
        };
        buildBanner.addEventListener('click', reopenBuildOverlayOrModal);
        buildBanner.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            reopenBuildOverlayOrModal();
          }
        });
      }
      if (closeBtn) closeBtn.addEventListener('click', () => { modal.style.display = 'none'; });
      modal.addEventListener('click', (ev) => {
        if (ev.target === modal) modal.style.display = 'none';
      });
      if (inputEl) {
        inputEl.addEventListener('input', autoResizeFactoryInput);
        inputEl.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
            ev.preventDefault();
            if (formEl && typeof formEl.requestSubmit === 'function') {
              formEl.requestSubmit();
            } else if (formEl) {
              formEl.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
            }
          }
        });
      }
      if (memToggle) {
        memToggle.addEventListener('click', () => {
          const open = memPanel.style.display !== 'none';
          memPanel.style.display = open ? 'none' : 'block';
          memToggle.setAttribute('aria-expanded', open ? 'false' : 'true');
        });
      }
      if (attachBtn && fileInputEl) {
        attachBtn.addEventListener('click', () => fileInputEl.click());
        fileInputEl.addEventListener('change', () => {
          Array.from(fileInputEl.files || []).forEach(appendImageFile);
          fileInputEl.value = '';
        });
      }
      modal.addEventListener('paste', (ev) => {
        const items = ev.clipboardData && ev.clipboardData.items ? Array.from(ev.clipboardData.items) : [];
        items.forEach((it) => {
          if (it.type && it.type.indexOf('image/') === 0) {
            const f = it.getAsFile();
            if (f) appendImageFile(f);
          }
        });
      });
      if (formEl) {
        formEl.addEventListener('submit', async (ev) => {
          ev.preventDefault();
          if (consultInFlight || isBlockingApplyActive()) return;
          const rawInputVal = inputEl ? inputEl.value : '';
          const msgTxt = rawInputVal.trim();
          if (!msgTxt && !pendingImages.length) {
            if (statusEl) statusEl.textContent = 'Escribe un mensaje o pega una captura.';
            return;
          }
          const payloadImages = pendingImages.map((im) => ({
            filename: im.filename,
            data_base64: im.data_base64,
          }));
          const previewThumbs = pendingImages.map((im) => im.previewUrl);
          if (inputEl) {
            inputEl.value = '';
            autoResizeFactoryInput();
          }
          pendingImages = [];
          renderPreviews();

          const emptyEl = streamEl.querySelector('.factory-chat-empty');
          if (emptyEl) emptyEl.remove();
          const optUserEl = document.createElement('div');
          optUserEl.className = 'factory-msg factory-msg-user';
          optUserEl.innerHTML =
            '<div class="factory-msg-body">' + renderSafeMarkdown(msgTxt || 'Analiza esta captura.').html + '</div>' +
            (previewThumbs.length
              ? '<div class="factory-chat-thumbs">' + previewThumbs.map((u) => '<img src="' + escapeHtml(u) + '" alt="Captura" />').join('') + '</div>'
              : '');
          streamEl.appendChild(optUserEl);

          const thinkingStartMs = Date.now();
          const thinkingEl = document.createElement('div');
          thinkingEl.id = 'factory-thinking-bubble';
          thinkingEl.className = 'factory-msg factory-msg-assistant';
          thinkingEl.innerHTML =
            '<div class="factory-msg-role">Liwi</div>' +
            '<div class="factory-thinking-bubble">' +
              '<span class="working-spinner" style="width:14px;height:14px;" aria-hidden="true"></span>' +
              '<span>Liwi está pensando…</span>' +
              '<span id="factory-thinking-clock" class="live-op-clock" data-op-clock-start="' + thinkingStartMs + '">00:00</span>' +
            '</div>';
          streamEl.appendChild(thinkingEl);
          streamEl.scrollTop = streamEl.scrollHeight;

          consultInFlight = true;
          sendBtn.disabled = true;
          sendBtn.textContent = '…';
          sendBtn.setAttribute('aria-busy', 'true');
          if (statusEl) statusEl.textContent = 'Analizando src/agent_factory/ en modo lectura...';
          try {
            await apiFetch('/api/factory/assistant/message', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              timeoutMs: 185000,
              body: JSON.stringify({ message: msgTxt, images: payloadImages }),
            });
            if (statusEl) statusEl.textContent = '';
            await refreshHistory();
          } catch (err) {
            if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
            if (inputEl && rawInputVal) {
              inputEl.value = rawInputVal;
              autoResizeFactoryInput();
            }
            if (statusEl) statusEl.textContent = 'Error consultando: ' + err.message;
          } finally {
            consultInFlight = false;
            if (!isBlockingApplyActive()) {
              sendBtn.disabled = false;
            }
            sendBtn.textContent = '↑';
            sendBtn.removeAttribute('aria-busy');
          }
        });
      }

      pollFactoryStatus();
    })();

    (function initStagingBanner() {
      const wrap = document.getElementById('staging-banner-wrap');
      const btn = document.getElementById('staging-banner-btn');
      const spinnerEl = document.getElementById('staging-banner-spinner');
      const labelEl = document.getElementById('staging-banner-label');
      const popover = document.getElementById('staging-popover');
      const popTitleEl = document.getElementById('staging-popover-title');
      const popMetaEl = document.getElementById('staging-popover-meta');
      const popCloseBtn = document.getElementById('staging-popover-close');
      const commitsListEl = document.getElementById('staging-commits-list');
      const popErrorEl = document.getElementById('staging-popover-error');
      const promoteBtn = document.getElementById('staging-promote-btn');
      const promoteStatusEl = document.getElementById('staging-promote-status');
      const promoteStatusTextEl = document.getElementById('staging-promote-status-text');
      const promoteClockEl = document.getElementById('staging-promote-clock');
      if (!wrap || !btn || !labelEl || !popover || !commitsListEl || !promoteBtn) return;

      let stagingFetchInFlight = false;
      let promoteInFlight = false;
      let promoteClockTimer = null;
      let promoteStartedAtMs = 0;
      let promoteReloadTimer = null;
      let promoteErrorMsg = '';
      let lastStagingSnapshot = {
        pending_count: 0,
        commits: [],
        is_building: false,
        queue_size: 0,
        last_error: null,
        current_task: null,
        initialized: false,
      };

      function stopPromoteClock() {
        if (promoteClockTimer) {
          clearInterval(promoteClockTimer);
          promoteClockTimer = null;
        }
      }

      function stopPromoteReloadTimer() {
        if (promoteReloadTimer) {
          clearInterval(promoteReloadTimer);
          promoteReloadTimer = null;
        }
      }

      function renderStagingBanner(rawSnapshot) {
        const s = rawSnapshot && typeof rawSnapshot === 'object' ? rawSnapshot : {};
        const pendingCount = Math.max(0, Number(s.pending_count) || 0);
        const commits = Array.isArray(s.commits) ? s.commits : [];
        const effectivePending = pendingCount > 0 ? pendingCount : commits.length;
        const isBuilding = Boolean(s.is_building);
        const queueSize = Math.max(0, Number(s.queue_size) || 0);
        const hasWorkInProgress = isBuilding || queueSize > 0;
        const lastError = s.last_error ? String(s.last_error).trim() : '';
        const currentTask = s.current_task ? String(s.current_task).trim() : '';
        const initialized = Boolean(s.initialized);

        lastStagingSnapshot = {
          pending_count: effectivePending,
          commits: commits,
          is_building: isBuilding,
          queue_size: queueSize,
          last_error: lastError || null,
          current_task: currentTask || null,
          initialized: initialized,
        };
        window.__liwiStagingActive = Boolean(initialized || hasWorkInProgress || effectivePending > 0);
        if (window.__liwiStagingActive && typeof window.clearFactoryBlockingForStaging === 'function') {
          window.clearFactoryBlockingForStaging();
        }

        if (!hasWorkInProgress && effectivePending === 0 && !promoteInFlight) {
          wrap.style.display = 'none';
          popover.style.display = 'none';
          btn.setAttribute('aria-expanded', 'false');
          return;
        }

        wrap.style.display = 'inline-flex';
        btn.classList.remove('is-building', 'is-ready', 'is-error');

        if (hasWorkInProgress) {
          btn.classList.add('is-building');
          if (spinnerEl) spinnerEl.style.display = 'inline-block';
          labelEl.textContent = '⏳ Preparando mejora en segundo plano...';
          if (popTitleEl) popTitleEl.textContent = 'Preparando mejora en segundo plano';
          if (popMetaEl) {
            popMetaEl.textContent = queueSize > 0
              ? ('Cola: ' + queueSize)
              : (effectivePending > 0 ? (effectivePending + ' lista(s)') : 'En curso');
          }
        } else if (effectivePending > 0) {
          btn.classList.add('is-ready');
          if (spinnerEl) spinnerEl.style.display = 'none';
          labelEl.textContent = effectivePending === 1
            ? '🚀 1 mejora lista'
            : ('🚀 ' + effectivePending + ' mejoras listas');
          if (popTitleEl) popTitleEl.textContent = 'Mejoras listas para aplicar';
          if (popMetaEl) {
            popMetaEl.textContent = effectivePending === 1
              ? '1 pendiente'
              : (effectivePending + ' pendientes');
          }
        }

        const listItems = [];
        if (hasWorkInProgress && currentTask) {
          listItems.push(
            '<li class="staging-commit-item">' +
              '<span class="staging-commit-hash">⏳</span>' +
              '<span class="staging-commit-title">En curso: ' + escapeHtml(currentTask) + '</span>' +
            '</li>'
          );
        }
        commits.forEach((c) => {
          const isObj = c && typeof c === 'object';
          const shortHash = isObj && c.hash ? String(c.hash).slice(0, 7) : '';
          const titleText = isObj ? String(c.title || c.hash || 'Mejora verificada') : String(c || 'Mejora verificada');
          listItems.push(
            '<li class="staging-commit-item">' +
              (shortHash ? '<span class="staging-commit-hash">' + escapeHtml(shortHash) + '</span>' : '') +
              '<span class="staging-commit-title">' + escapeHtml(titleText) + '</span>' +
            '</li>'
          );
        });
        if (!listItems.length) {
          listItems.push(
            '<li class="staging-commit-item">' +
              '<span class="staging-commit-title">' +
                escapeHtml(hasWorkInProgress ? 'Verificando mejora en segundo plano...' : 'Mejoras verificadas listas para aplicar.') +
              '</span>' +
            '</li>'
          );
        }
        commitsListEl.innerHTML = listItems.join('');

        if (popErrorEl) {
          const errToShow = promoteErrorMsg || lastError;
          if (errToShow && !promoteInFlight) {
            popErrorEl.textContent = errToShow;
            popErrorEl.style.display = 'block';
          } else {
            popErrorEl.textContent = '';
            popErrorEl.style.display = 'none';
          }
        }

        if (!promoteInFlight) {
          if (effectivePending > 0) {
            promoteBtn.style.display = 'inline-flex';
            promoteBtn.disabled = hasWorkInProgress;
            promoteBtn.textContent = 'Actualizar ahora';
            promoteBtn.title = hasWorkInProgress
              ? 'Espera a que termine la mejora en segundo plano para actualizar'
              : 'Promover mejoras verificadas a main y reiniciar Liwi';
          } else {
            promoteBtn.style.display = 'none';
            promoteBtn.disabled = true;
          }
        }
      }

      async function pollStagingStatus() {
        if (stagingFetchInFlight || promoteInFlight) return null;
        stagingFetchInFlight = true;
        try {
          const data = await apiFetch('/api/self-improve/staging', { cache: 'no-store' });
          renderStagingBanner(data);
          return data;
        } catch (_) {
          return null;
        } finally {
          stagingFetchInFlight = false;
        }
      }

      function startPromoteReloadPolling(restartScheduled) {
        stopPromoteReloadTimer();
        if (!restartScheduled) {
          if (promoteStatusTextEl) promoteStatusTextEl.textContent = '✓ ¡Liwi actualizado! Recargando...';
          setTimeout(() => {
            stopPromoteClock();
            window.location.reload();
          }, 600);
          return;
        }
        let sawDrop = false;
        let probeInFlight = false;
        const pollStartedAt = Date.now();
        promoteReloadTimer = setInterval(async () => {
          if (probeInFlight) return;
          probeInFlight = true;
          const probeController = typeof AbortController === 'function' ? new AbortController() : null;
          const probeAbortTimer = probeController ? setTimeout(() => probeController.abort(), 2500) : null;
          try {
            const fetchOpts = { cache: 'no-store' };
            if (probeController) fetchOpts.signal = probeController.signal;
            const resp = await fetch('/api/self-improve/staging', fetchOpts);
            if (resp.ok) {
              const st = await resp.json().catch(() => null);
              const elapsedMs = Date.now() - pollStartedAt;
              const freshAfterRestart = Boolean(
                st && !st.initialized && Number(st.pending_count || 0) === 0 && elapsedMs >= 1500
              );
              if (sawDrop || freshAfterRestart || elapsedMs >= 4000) {
                stopPromoteReloadTimer();
                stopPromoteClock();
                if (promoteStatusTextEl) promoteStatusTextEl.textContent = '✓ ¡Liwi actualizado! Recargando...';
                window.location.reload();
              }
            } else {
              sawDrop = true;
            }
          } catch (_) {
            sawDrop = true;
          } finally {
            if (probeAbortTimer) clearTimeout(probeAbortTimer);
            probeInFlight = false;
          }
        }, 1000);
      }

      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        headerPopoverController.toggle('staging');
      });

      if (popCloseBtn) {
        popCloseBtn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          headerPopoverController.close('staging');
        });
      }

      headerPopoverController.register('staging', {
        container: wrap,
        trigger: btn,
        popover: popover,
        canClose: () => !promoteInFlight,
        onOpen: () => {
          pollStagingStatus();
        },
      });

      promoteBtn.addEventListener('click', async () => {
        if (promoteInFlight || promoteBtn.disabled) return;
        promoteInFlight = true;
        window.__stagingPromoteInFlight = true;
        promoteErrorMsg = '';
        promoteBtn.disabled = true;
        promoteBtn.setAttribute('aria-busy', 'true');
        promoteBtn.innerHTML = '<span class="btn-spinner" aria-hidden="true"></span><span>Actualizando...</span>';
        if (popErrorEl) {
          popErrorEl.style.display = 'none';
          popErrorEl.textContent = '';
        }
        promoteStartedAtMs = Date.now();
        if (promoteStatusTextEl) promoteStatusTextEl.textContent = 'Aplicando actualizaciones y reiniciando...';
        if (promoteClockEl) promoteClockEl.textContent = '0s';
        if (promoteStatusEl) promoteStatusEl.style.display = 'inline-flex';
        stopPromoteClock();
        promoteClockTimer = setInterval(() => {
          if (promoteClockEl) {
            promoteClockEl.textContent = Math.max(0, Math.floor((Date.now() - promoteStartedAtMs) / 1000)) + 's';
          }
        }, 1000);

        try {
          const res = await apiFetch('/api/self-improve/promote', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            timeoutMs: 60000,
            body: JSON.stringify({}),
          });
          if (res && res.promoted) {
            if (promoteStatusTextEl) {
              promoteStatusTextEl.textContent = 'Aplicando actualizaciones y reiniciando...';
            }
            startPromoteReloadPolling(Boolean(res.restart_scheduled));
            return;
          }
          stopPromoteClock();
          promoteInFlight = false;
          window.__stagingPromoteInFlight = false;
          if (promoteStatusEl) promoteStatusEl.style.display = 'none';
          promoteBtn.removeAttribute('aria-busy');
          promoteBtn.textContent = 'Actualizar ahora';
          promoteBtn.disabled = false;
          if (res && res.message && popErrorEl) {
            promoteErrorMsg = String(res.message);
            popErrorEl.textContent = promoteErrorMsg;
            popErrorEl.style.display = 'block';
          }
          pollStagingStatus();
        } catch (err) {
          stopPromoteClock();
          stopPromoteReloadTimer();
          promoteInFlight = false;
          window.__stagingPromoteInFlight = false;
          if (promoteStatusEl) promoteStatusEl.style.display = 'none';
          promoteBtn.removeAttribute('aria-busy');
          promoteBtn.textContent = 'Actualizar ahora';
          promoteBtn.disabled = false;
          promoteErrorMsg = 'Error al actualizar: ' + (err && err.message ? err.message : String(err || 'Error inesperado'));
          if (popErrorEl) {
            popErrorEl.textContent = promoteErrorMsg;
            popErrorEl.style.display = 'block';
          }
          renderStagingBanner(lastStagingSnapshot);
        }
      });

      window.applySelfImproveStagingSnapshot = renderStagingBanner;
      window.refreshSelfImproveStaging = pollStagingStatus;
      setInterval(() => {
        pollStagingStatus();
      }, 5000);
      pollStagingStatus();
    })();

    (function initLiwiIdeaRadar() {
      const sectionEl = document.getElementById('liwi-radar-section');
      const freqSelect = document.getElementById('liwi-radar-frequency');
      const genBtn = document.getElementById('liwi-radar-generate-btn');
      const filtersBarEl = document.getElementById('liwi-radar-filters');
      const liveStatusEl = document.getElementById('liwi-radar-live-status');
      const spinnerEl = document.getElementById('liwi-radar-spinner');
      const statusTextEl = document.getElementById('liwi-radar-status-text');
      const clockEl = document.getElementById('liwi-radar-clock');
      const emptyEl = document.getElementById('liwi-radar-empty');
      const gridEl = document.getElementById('liwi-radar-grid');
      const heroEl = document.getElementById('liwi-hero-active-card');
      const heroTitleEl = document.getElementById('liwi-hero-active-title');
      const heroStepEl = document.getElementById('liwi-hero-active-step');
      const heroTimerEl = document.getElementById('liwi-hero-active-timer');
      const reconcileBtn = document.getElementById('liwi-queue-reconcile-btn');
      const drainBarEl = document.getElementById('liwi-queue-drain-bar');
      const drainBtn = document.getElementById('liwi-drain-queue-btn');
      if (!sectionEl || !gridEl) return;

      let currentIdeas = [];
      let currentImplementing = [];
      let currentIntervalHours = 0;
      let activeFilter = 'pending';
      let currentCounts = {
        pending: 0,
        queued: 0,
        implementing: 0,
        implemented: 0,
        failed: 0,
        dismissed: 0,
        all: 0,
      };
      let generateInFlight = false;
      let loadSeq = 0;
      let generateClockTimer = null;
      let heroTickTimer = null;
      let statusHideTimer = null;
      let expandedIdeaId = null;
      const actionInFlightIds = new Set();

      function normalizeCounts(rawCounts) {
        const src = (rawCounts && typeof rawCounts === 'object') ? rawCounts : {};
        const pending = Math.max(0, Number(src.pending || 0) || 0);
        const queued = Math.max(0, Number(src.queued || 0) || 0);
        const implementing = Math.max(0, Number(src.implementing || 0) || 0);
        const implemented = Math.max(0, Number(src.implemented || 0) || 0);
        const failed = Math.max(0, Number(src.failed || 0) || 0);
        const dismissed = Math.max(0, Number(src.dismissed || 0) || 0);
        const all = Math.max(
          0,
          Number(src.all !== undefined ? src.all : (pending + queued + implementing + implemented + failed + dismissed)) || 0
        );
        return { pending, queued, implementing, implemented, failed, dismissed, all };
      }

      function formatTime12h(isoStr) {
        if (!isoStr) return '';
        const d = new Date(isoStr);
        if (Number.isNaN(d.getTime())) return '';
        let h = d.getHours();
        const m = String(d.getMinutes()).padStart(2, '0');
        const suffix = h >= 12 ? 'p.m.' : 'a.m.';
        h = h % 12 || 12;
        return h + ':' + m + ' ' + suffix;
      }

      function formatDayAndTime(isoStr) {
        if (!isoStr) return '';
        const d = new Date(isoStr);
        if (Number.isNaN(d.getTime())) return '';
        const now = new Date();
        const sameDay = d.getFullYear() === now.getFullYear() &&
          d.getMonth() === now.getMonth() &&
          d.getDate() === now.getDate();
        const timePart = formatTime12h(isoStr);
        if (sameDay) return 'Hoy, ' + timePart;
        return d.toLocaleDateString('es-MX', { day: '2-digit', month: 'short' }) + ', ' + timePart;
      }

      function formatDurationLabel(secondsVal) {
        const sec = Math.max(0, Math.round(Number(secondsVal || 0) || 0));
        const mins = Math.floor(sec / 60);
        const rem = sec % 60;
        if (mins > 0) return 'Tardó ' + mins + 'm ' + rem + 's';
        return 'Tardó ' + rem + 's';
      }

      function formatWaitLabel(isoStr) {
        if (!isoStr) return '';
        const ms = Date.parse(isoStr);
        if (!Number.isFinite(ms)) return '';
        const mins = Math.max(0, Math.floor((Date.now() - ms) / 60000));
        if (mins < 60) return 'Esperando: ' + mins + 'm';
        const hrs = Math.floor(mins / 60);
        return 'Esperando: ' + hrs + 'h ' + (mins % 60) + 'm';
      }

      function formatMmSsSince(isoStr) {
        const ms = isoStr ? Date.parse(isoStr) : NaN;
        const elapsed = Number.isFinite(ms) ? Math.max(0, Math.floor((Date.now() - ms) / 1000)) : 0;
        const mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
        const ss = String(elapsed % 60).padStart(2, '0');
        return '⏱️ ' + mm + ':' + ss;
      }

      function matchesActiveFilter(idea) {
        const st = String((idea && idea.status) || 'nueva').trim().toLowerCase();
        if (activeFilter === 'pending') {
          return st === 'nueva' || st === 'pending';
        }
        if (activeFilter === 'queued') {
          return st === 'encolada' || st === 'queued';
        }
        if (activeFilter === 'implemented') {
          return st === 'aplicada' || st === 'implemented' || st === 'failed' || st === 'fallida';
        }
        return true;
      }

      function emptyMessageForFilter(filterKey) {
        if (filterKey === 'queued') {
          return 'No hay ideas en cola en este momento.';
        }
        if (filterKey === 'implemented') {
          return 'Todavía no hay mejoras aplicadas ni fallidas registradas.';
        }
        return 'Sin ideas nuevas por ahora. Pulsa «💡 Generar ideas» para descubrir mejoras.';
      }

      function syncFilterTabsUI() {
        if (!filtersBarEl) return;
        const counts = currentCounts;
        const filterCounts = {
          pending: counts.pending,
          queued: counts.queued,
          implemented: counts.implemented + counts.failed,
        };
        filtersBarEl.querySelectorAll('button[data-radar-filter]').forEach((btn) => {
          const key = btn.getAttribute('data-radar-filter') || '';
          const isActive = key === activeFilter;
          btn.classList.toggle('active', isActive);
          btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
          const countEl = btn.querySelector('[data-radar-filter-count]');
          if (countEl && Object.prototype.hasOwnProperty.call(filterCounts, key)) {
            countEl.textContent = String(filterCounts[key]);
          }
        });
      }

      function renderHeroActiveCard() {
        if (!heroEl) return;
        if (heroTickTimer) {
          clearInterval(heroTickTimer);
          heroTickTimer = null;
        }
        const activeIdea = Array.isArray(currentImplementing) && currentImplementing.length
          ? currentImplementing[0]
          : null;
        const sq = (window.__liwiStagingQueue && typeof window.__liwiStagingQueue === 'object')
          ? window.__liwiStagingQueue
          : null;
        const sqActive = Boolean(sq && sq.active);
        if (!activeIdea && !sqActive) {
          window.__liwiStagingStartedIso = null;
          heroEl.style.display = 'none';
          return;
        }
        heroEl.style.display = 'flex';
        if (activeIdea) {
          if (heroTitleEl) heroTitleEl.textContent = String(activeIdea.title || 'Mejora en curso...');
          if (heroStepEl) heroStepEl.textContent = String(activeIdea.current_step || (sq && sq.step) || 'Analizando');
        } else {
          if (!window.__liwiStagingStartedIso) {
            window.__liwiStagingStartedIso = new Date().toISOString();
          }
          if (heroTitleEl) heroTitleEl.textContent = String((sq && sq.current_title) || 'Procesando cola en .liwi-staging...');
          if (heroStepEl) heroStepEl.textContent = String((sq && sq.step) || 'Construyendo en .liwi-staging');
        }
        const startedIso = activeIdea
          ? String(activeIdea.started_at || activeIdea.created_at || '')
          : String(window.__liwiStagingStartedIso || '');
        if (heroTimerEl) {
          heroTimerEl.textContent = formatMmSsSince(startedIso);
          heroTickTimer = setInterval(() => {
            if (heroTimerEl) heroTimerEl.textContent = formatMmSsSince(startedIso);
          }, 1000);
        }
      }

      function categoryIconFor(category) {
        const key = String(category || '').toLowerCase();
        if (key.indexOf('ui') >= 0 || key.indexOf('ux') >= 0 || key.indexOf('interfaz') >= 0 || key.indexOf('dise') >= 0) {
          return '🎨';
        }
        if (key.indexOf('rendim') >= 0 || key.indexOf('perf') >= 0 || key.indexOf('veloc') >= 0) {
          return '⚡';
        }
        if (key.indexOf('segur') >= 0 || key.indexOf('secur') >= 0) {
          return '🛡️';
        }
        if (key.indexOf('estab') >= 0 || key.indexOf('error') >= 0 || key.indexOf('fiab') >= 0 || key.indexOf('bug') >= 0) {
          return '🩺';
        }
        if (key.indexOf('prueb') >= 0 || key.indexOf('test') >= 0 || key.indexOf('calid') >= 0) {
          return '🧪';
        }
        if (key.indexOf('arquit') >= 0 || key.indexOf('refact') >= 0) {
          return '🏗️';
        }
        return '💡';
      }

      function setRadarLiveStatus(text, opts) {
        const cfg = opts || {};
        if (statusHideTimer) {
          clearTimeout(statusHideTimer);
          statusHideTimer = null;
        }
        if (!liveStatusEl) return;
        if (!text) {
          liveStatusEl.style.display = 'none';
          liveStatusEl.classList.remove('is-ok', 'is-error');
          if (spinnerEl) spinnerEl.style.display = 'none';
          if (clockEl) {
            clockEl.style.display = 'none';
            clockEl.textContent = '0s';
          }
          if (statusTextEl) statusTextEl.textContent = '';
          return;
        }
        liveStatusEl.style.display = 'flex';
        liveStatusEl.classList.toggle('is-ok', Boolean(cfg.ok));
        liveStatusEl.classList.toggle('is-error', Boolean(cfg.error));
        if (spinnerEl) spinnerEl.style.display = cfg.spinner ? 'inline-block' : 'none';
        if (statusTextEl) statusTextEl.textContent = String(text);
        if (clockEl) {
          clockEl.style.display = cfg.clock ? 'inline' : 'none';
          if (cfg.clock) clockEl.textContent = cfg.clockText || '0s';
        }
        if (cfg.autoHideMs && cfg.autoHideMs > 0) {
          statusHideTimer = setTimeout(() => {
            if (!generateInFlight) setRadarLiveStatus('');
          }, cfg.autoHideMs);
        }
      }

      function syncFrequencySelect(rawHours) {
        const num = Number(rawHours);
        const safeHours = Number.isFinite(num) && num >= 0 ? num : 0;
        currentIntervalHours = safeHours;
        if (!freqSelect) return;
        const normalized = String(Math.round(safeHours));
        const hasOption = Array.from(freqSelect.options).some((opt) => opt.value === normalized);
        if (hasOption) {
          freqSelect.value = normalized;
        } else {
          freqSelect.value = safeHours >= 72 ? '72' : (safeHours > 0 ? '24' : '0');
        }
      }

      function syncHeaderIdeasBadge() {
        const badgeEl = document.getElementById('auto-ideas-count');
        if (!badgeEl) return;
        const pendingCount = Number(currentCounts.pending || 0);
        if (pendingCount > 0) {
          badgeEl.textContent = String(pendingCount);
          badgeEl.style.display = 'inline-flex';
        } else {
          badgeEl.textContent = '0';
          badgeEl.style.display = 'none';
        }
      }

      function findHistoryEntryForIdea(idea) {
        const hist = Array.isArray(window.__liwiAutonomousHistory) ? window.__liwiAutonomousHistory : [];
        const ideaId = Number(idea && idea.id);
        const byIdea = hist.find((h) => Number(h && h.idea_id) === ideaId);
        if (byIdea) return byIdea;
        const sha = String((idea && idea.commit_sha) || '').trim();
        if (sha) {
          const bySha = hist.find((h) => String((h && h.commit_hash) || '').startsWith(sha.slice(0, 7)));
          if (bySha) return bySha;
        }
        return null;
      }

      function renderIdeasGrid() {
        const rawList = Array.isArray(currentIdeas) ? currentIdeas : [];
        const list = rawList.filter((idea) => matchesActiveFilter(idea));
        syncFilterTabsUI();
        syncHeaderIdeasBadge();
        renderHeroActiveCard();

        gridEl.classList.toggle('is-lane-list', activeFilter === 'queued' || activeFilter === 'implemented');

        const sqState = (window.__liwiStagingQueue && typeof window.__liwiStagingQueue === 'object')
          ? window.__liwiStagingQueue
          : {};
        const sqIsActive = Boolean(sqState.active);
        if (drainBarEl) {
          const showDrain = activeFilter === 'queued' && list.length > 0;
          drainBarEl.style.display = showDrain ? 'flex' : 'none';
          if (drainBtn && showDrain) {
            drainBtn.disabled = sqIsActive;
            drainBtn.textContent = sqIsActive
              ? '⏳ Cola en ejecución en staging...'
              : ('▶️ Procesar toda la cola (' + list.length + ')');
          }
        }

        if (!list.length) {
          expandedIdeaId = null;
          gridEl.innerHTML = '';
          gridEl.style.display = 'none';
          if (emptyEl) {
            emptyEl.textContent = emptyMessageForFilter(activeFilter);
            emptyEl.style.display = 'block';
          }
          return;
        }
        if (expandedIdeaId !== null && !list.some((it) => String(Number(it.id)) === expandedIdeaId)) {
          expandedIdeaId = null;
        }
        if (emptyEl) emptyEl.style.display = 'none';
        gridEl.style.display = 'grid';

        gridEl.innerHTML = list.map((idea, idx) => {
          const idNum = Number(idea.id);
          const idStr = String(idNum);
          const title = String(idea.title || '');
          const category = String(idea.category || '');
          const impact = String(idea.impact || 'Medio');
          const desc = String(idea.simple_description || '');
          const status = String(idea.status || 'nueva').trim().toLowerCase();
          const isExpanded = expandedIdeaId === idStr;
          const isBusy = actionInFlightIds.has(idStr);
          const impactCls = impact.toLowerCase() === 'alto' ? 'is-high' : 'is-medium';
          const trimmedTitle = title.trim();
          const hasLeadingEmoji = /^[\\u2600-\\u27BF\\uD83C-\\uDBFF]/.test(trimmedTitle);
          const icon = hasLeadingEmoji ? '' : categoryIconFor(category);

          if (activeFilter === 'queued') {
            const enqueuedLabel = formatTime12h(idea.queued_at || idea.created_at);
            const waitLabel = formatWaitLabel(idea.queued_at || idea.created_at);
            const retryNum = Number(idea.retry_count || 0) || 0;
            const retryBadge = retryNum > 0
              ? ('<span class="liwi-retry-badge">⚠️ Reintento ' + retryNum + ' · Auto-investigada</span>')
              : '';
            const queuedIdsArr = Array.isArray(sqState.queued_idea_ids) ? sqState.queued_idea_ids : [];
            const inStaging = sqIsActive && (
              idNum === Number(sqState.current_idea_id) ||
              queuedIdsArr.indexOf(idNum) >= 0
            );
            const stagingBadge = inStaging
              ? '<span class="liwi-radar-status-pill is-implementing">🔄 En staging</span>'
              : '';
            return (
              '<article class="liwi-radar-card" data-radar-card-id="' + escapeHtml(idStr) + '">' +
                '<div class="liwi-radar-card-head">' +
                  '<span class="liwi-queue-rank">#' + (idx + 1) + '</span>' +
                  '<div class="liwi-radar-card-main">' +
                    '<div class="liwi-radar-card-title">' + escapeHtml(title) + '</div>' +
                    '<div class="liwi-radar-card-meta">' +
                      stagingBadge +
                      (enqueuedLabel ? ('<span class="liwi-radar-impact">Encolada: ' + escapeHtml(enqueuedLabel) + '</span>') : '') +
                      (waitLabel ? ('<span class="liwi-radar-impact is-medium">' + escapeHtml(waitLabel) + '</span>') : '') +
                      retryBadge +
                    '</div>' +
                    (desc ? ('<p class="liwi-radar-card-preview">' + escapeHtml(desc) + '</p>') : '') +
                  '</div>' +
                '</div>' +
                '<div class="liwi-radar-card-actions">' +
                  '<button type="button" class="btn btn-primary btn-sm" data-radar-run-now="' + escapeHtml(idStr) + '"' + ((isBusy || inStaging) ? ' disabled' : '') + '>▶️ Ejecutar ya</button>' +
                  '<button type="button" class="btn btn-sm" data-radar-remove="' + escapeHtml(idStr) + '"' + (isBusy ? ' disabled' : '') + '>Quitar</button>' +
                '</div>' +
                '<div class="liwi-radar-card-feedback" data-radar-feedback="' + escapeHtml(idStr) + '" style="display:none"></div>' +
              '</article>'
            );
          }

          if (activeFilter === 'implemented') {
            const isFailed = status === 'failed' || status === 'fallida';
            const histEntry = findHistoryEntryForIdea(idea);
            const commitSha = String(idea.commit_sha || (histEntry && histEntry.commit_hash) || '').trim().slice(0, 7);
            const completedLabel = formatDayAndTime(idea.completed_at || (histEntry && histEntry.applied_at) || idea.created_at);
            const durationLabel = idea.duration_seconds !== null && idea.duration_seconds !== undefined
              ? formatDurationLabel(idea.duration_seconds)
              : '';
            const failReason = String(idea.failure_reason || '').trim();
            const badgeHtml = isFailed
              ? '<span class="liwi-impl-badge is-fail">⚠️ Falló tras investigación</span>'
              : '<span class="liwi-impl-badge is-ok">✅ Aplicada</span>';
            const revertBtnHtml = (!isFailed && histEntry && String(histEntry.status || '') !== 'revertida')
              ? ('<button type="button" class="btn btn-sm btn-warn" data-autonomous-revert="' + escapeHtml(String(histEntry.id)) + '">↩️ Deshacer</button>')
              : '';
            return (
              '<article class="liwi-radar-card" data-radar-card-id="' + escapeHtml(idStr) + '">' +
                '<div class="liwi-radar-card-head">' +
                  '<div class="liwi-radar-card-main">' +
                    '<div class="liwi-radar-card-title">' + escapeHtml(title) + '</div>' +
                    '<div class="liwi-radar-card-meta">' +
                      badgeHtml +
                      (completedLabel ? ('<span class="liwi-radar-impact">' + escapeHtml(completedLabel) + '</span>') : '') +
                      (durationLabel ? ('<span class="liwi-radar-impact is-medium">' + escapeHtml(durationLabel) + '</span>') : '') +
                      (commitSha ? ('<code>' + escapeHtml(commitSha) + '</code>') : '') +
                    '</div>' +
                    (failReason ? ('<p class="liwi-radar-card-desc" style="color:var(--err);">' + escapeHtml(failReason) + '</p>') : '') +
                    (!failReason && desc ? ('<p class="liwi-radar-card-preview">' + escapeHtml(desc) + '</p>') : '') +
                  '</div>' +
                  revertBtnHtml +
                '</div>' +
              '</article>'
            );
          }

          return (
            '<article class="liwi-radar-card' + (isExpanded ? ' is-expanded' : '') + '" role="button" tabindex="0" aria-expanded="' + (isExpanded ? 'true' : 'false') + '" data-radar-card-id="' + escapeHtml(idStr) + '">' +
              '<div class="liwi-radar-card-head">' +
                (icon ? ('<span class="liwi-radar-card-icon" aria-hidden="true">' + escapeHtml(icon) + '</span>') : '') +
                '<div class="liwi-radar-card-main">' +
                  '<div class="liwi-radar-card-title">' + escapeHtml(title) + '</div>' +
                  '<div class="liwi-radar-card-meta">' +
                    '<span class="liwi-radar-impact ' + impactCls + '">Impacto ' + escapeHtml(impact) + '</span>' +
                  '</div>' +
                  (desc ? ('<p class="liwi-radar-card-preview">' + escapeHtml(desc) + '</p>') : '') +
                '</div>' +
              '</div>' +
              '<div class="liwi-radar-card-details">' +
                '<p class="liwi-radar-card-desc">' + escapeHtml(desc) + '</p>' +
                '<div class="liwi-radar-card-actions">' +
                  '<button type="button" class="btn btn-primary btn-sm" data-radar-implement="' + escapeHtml(idStr) + '"' + (isBusy ? ' disabled' : '') + '>+ Encolar</button>' +
                  '<button type="button" class="btn btn-sm" data-radar-dismiss="' + escapeHtml(idStr) + '"' + (isBusy ? ' disabled' : '') + '>Descartar</button>' +
                '</div>' +
                '<div class="liwi-radar-card-feedback" data-radar-feedback="' + escapeHtml(idStr) + '" style="display:none"></div>' +
              '</div>' +
            '</article>'
          );
        }).join('');

        if (activeFilter === 'pending') {
          gridEl.querySelectorAll('.liwi-radar-card').forEach((cardEl) => {
            const cid = cardEl.getAttribute('data-radar-card-id') || '';
            const toggleCard = () => {
              if (!cid) return;
              const willExpand = expandedIdeaId !== cid;
              expandedIdeaId = willExpand ? cid : null;
              gridEl.querySelectorAll('.liwi-radar-card').forEach((otherEl) => {
                const otherId = otherEl.getAttribute('data-radar-card-id') || '';
                const isOpen = Boolean(willExpand && otherId === cid);
                otherEl.classList.toggle('is-expanded', isOpen);
                otherEl.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
              });
            };
            cardEl.addEventListener('click', () => toggleCard());
            cardEl.addEventListener('keydown', (ev) => {
              if (ev.target && ev.target.closest('button, a, input, select, textarea')) return;
              if (ev.key === 'Enter' || ev.key === ' ') {
                ev.preventDefault();
                toggleCard();
              }
            });
          });
        }

        gridEl.querySelectorAll('button[data-radar-implement]').forEach((btn) => {
          btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const ideaId = btn.getAttribute('data-radar-implement') || '';
            if (!ideaId || btn.disabled || actionInFlightIds.has(ideaId)) return;
            const cardEl = btn.closest('.liwi-radar-card');
            const dismissBtn = cardEl ? cardEl.querySelector('button[data-radar-dismiss]') : null;
            const feedbackEl = cardEl ? cardEl.querySelector('[data-radar-feedback]') : null;
            const origHtml = btn.innerHTML;
            actionInFlightIds.add(ideaId);
            btn.disabled = true;
            if (dismissBtn) dismissBtn.disabled = true;
            btn.textContent = 'Encolando...';
            try {
              const res = await apiFetch('/api/ideas/' + encodeURIComponent(ideaId) + '/implement', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                timeoutMs: 60000,
                body: JSON.stringify({}),
              });
              actionInFlightIds.delete(ideaId);
              if (expandedIdeaId === ideaId) expandedIdeaId = null;
              window.__liwiStagingActive = true;
              const snap = (res && (res.staging_status || res.staging)) || null;
              if (snap && typeof window.applySelfImproveStagingSnapshot === 'function') {
                window.applySelfImproveStagingSnapshot(snap);
              }
              await loadIdeas();
              setRadarLiveStatus('✓ Idea encolada en segundo plano (.liwi-staging).', { ok: true, autoHideMs: 4000 });
            } catch (err) {
              actionInFlightIds.delete(ideaId);
              btn.disabled = false;
              btn.innerHTML = origHtml;
              if (dismissBtn) dismissBtn.disabled = false;
              if (feedbackEl) {
                feedbackEl.style.display = 'block';
                feedbackEl.classList.add('is-error');
                feedbackEl.textContent = 'Error al encolar: ' + (err && err.message ? err.message : String(err || 'Error inesperado'));
              }
            }
          });
        });

        gridEl.querySelectorAll('button[data-radar-run-now]').forEach((btn) => {
          btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const ideaId = btn.getAttribute('data-radar-run-now') || '';
            if (!ideaId || btn.disabled || actionInFlightIds.has(ideaId)) return;
            const cardEl = btn.closest('.liwi-radar-card');
            const feedbackEl = cardEl ? cardEl.querySelector('[data-radar-feedback]') : null;
            const origHtml = btn.innerHTML;
            actionInFlightIds.add(ideaId);
            btn.disabled = true;
            btn.textContent = 'Ejecutando...';
            try {
              await apiFetch('/api/ideas/' + encodeURIComponent(ideaId) + '/run-now', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                timeoutMs: 60000,
                body: JSON.stringify({}),
              });
              actionInFlightIds.delete(ideaId);
              window.__liwiStagingActive = true;
              await loadIdeas();
              setRadarLiveStatus('✓ Ejecución iniciada en .liwi-staging.', { ok: true, autoHideMs: 4000 });
            } catch (err) {
              actionInFlightIds.delete(ideaId);
              btn.disabled = false;
              btn.innerHTML = origHtml;
              if (feedbackEl) {
                feedbackEl.style.display = 'block';
                feedbackEl.classList.add('is-error');
                feedbackEl.textContent = 'Error al ejecutar: ' + (err && err.message ? err.message : String(err || 'Error inesperado'));
              }
            }
          });
        });

        gridEl.querySelectorAll('button[data-radar-remove]').forEach((btn) => {
          btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const ideaId = btn.getAttribute('data-radar-remove') || '';
            if (!ideaId || btn.disabled || actionInFlightIds.has(ideaId)) return;
            actionInFlightIds.add(ideaId);
            btn.disabled = true;
            btn.textContent = 'Quitando...';
            try {
              await apiFetch('/api/ideas/' + encodeURIComponent(ideaId) + '/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
              });
              actionInFlightIds.delete(ideaId);
              await loadIdeas();
              setRadarLiveStatus('✓ Idea devuelta a Nuevas.', { ok: true, autoHideMs: 3000 });
            } catch (err) {
              actionInFlightIds.delete(ideaId);
              btn.disabled = false;
              btn.textContent = 'Quitar';
              setRadarLiveStatus('Error al quitar: ' + (err && err.message ? err.message : ''), { error: true });
            }
          });
        });

        gridEl.querySelectorAll('button[data-radar-dismiss]').forEach((btn) => {
          btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const ideaId = btn.getAttribute('data-radar-dismiss') || '';
            if (!ideaId || btn.disabled || actionInFlightIds.has(ideaId)) return;
            actionInFlightIds.add(ideaId);
            btn.disabled = true;
            btn.textContent = 'Descartando...';
            try {
              await apiFetch('/api/ideas/' + encodeURIComponent(ideaId) + '/dismiss', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
              });
              actionInFlightIds.delete(ideaId);
              if (expandedIdeaId === ideaId) expandedIdeaId = null;
              await loadIdeas();
            } catch (err) {
              actionInFlightIds.delete(ideaId);
              btn.disabled = false;
              btn.textContent = 'Descartar';
              setRadarLiveStatus('Error al descartar: ' + (err && err.message ? err.message : ''), { error: true });
            }
          });
        });

        gridEl.querySelectorAll('button[data-autonomous-revert]').forEach((btn) => {
          btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const entryId = btn.getAttribute('data-autonomous-revert') || '';
            if (!entryId || btn.disabled) return;
            btn.disabled = true;
            btn.textContent = 'Revirtiendo...';
            try {
              const res = await apiFetch('/api/autonomous/revert/' + encodeURIComponent(entryId), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                timeoutMs: 195000,
                body: '{}',
              });
              await loadIdeas();
              setRadarLiveStatus('↩️ Mejora revertida y verificada correctamente.', { ok: true, autoHideMs: 4500 });
              if (res && res.restart_scheduled) {
                const rb = document.getElementById('factory-restart-banner');
                if (rb) rb.style.display = 'block';
              }
            } catch (err) {
              btn.disabled = false;
              btn.textContent = '↩️ Deshacer';
              setRadarLiveStatus('Error al revertir: ' + (err && err.message ? err.message : ''), { error: true });
            }
          });
        });
      }

      async function loadIdeas() {
        const reqSeq = ++loadSeq;
        const filterKey = activeFilter;
        try {
          const requests = [
            apiFetch('/api/ideas?status=implementing', { cache: 'no-store' }),
          ];
          if (filterKey === 'implemented') {
            requests.push(apiFetch('/api/ideas?status=implemented', { cache: 'no-store' }));
            requests.push(apiFetch('/api/ideas?status=failed', { cache: 'no-store' }));
          } else {
            requests.push(apiFetch('/api/ideas?status=' + encodeURIComponent(filterKey), { cache: 'no-store' }));
          }
          const responses = await Promise.all(requests);
          if (reqSeq !== loadSeq) return;
          const implRes = responses[0] || {};
          const primaryRes = responses[1] || {};
          const failedRes = responses[2] || null;
          currentImplementing = Array.isArray(implRes.ideas) ? implRes.ideas : [];
          let loadedIdeas = Array.isArray(primaryRes.ideas) ? primaryRes.ideas : [];
          if (failedRes && Array.isArray(failedRes.ideas)) {
            const seen = new Set(loadedIdeas.map((x) => String(x && x.id)));
            failedRes.ideas.forEach((fItem) => {
              if (!seen.has(String(fItem && fItem.id))) loadedIdeas.push(fItem);
            });
          }
          currentIdeas = loadedIdeas;
          const latestSq = primaryRes.staging_queue || implRes.staging_queue || null;
          if (latestSq && typeof latestSq === 'object') {
            window.__liwiStagingQueue = latestSq;
          }
          const latestCounts = primaryRes.counts || implRes.counts || null;
          if (latestCounts) {
            currentCounts = normalizeCounts(latestCounts);
          }
          const latestInterval = primaryRes.auto_interval_hours !== undefined
            ? primaryRes.auto_interval_hours
            : implRes.auto_interval_hours;
          if (latestInterval !== undefined) {
            syncFrequencySelect(latestInterval);
          }
          renderIdeasGrid();
        } catch (err) {
          if (reqSeq !== loadSeq) return;
          setRadarLiveStatus(
            'No se pudieron cargar las ideas: ' + (err && err.message ? err.message : String(err || 'Error')),
            { error: true }
          );
        }
      }

      if (drainBtn) {
        drainBtn.addEventListener('click', async () => {
          if (drainBtn.disabled) return;
          drainBtn.disabled = true;
          const orig = drainBtn.textContent;
          drainBtn.textContent = '⏳ Encolando lote...';
          try {
            const res = await apiFetch('/api/ideas/drain-queue', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: '{}',
            });
            if (res && res.paused_quota) {
              setRadarLiveStatus(res.error || '⏳ Cuota en pausa; la cola se procesará al reanudarse.', { error: true });
            } else {
              window.__liwiStagingActive = true;
              const snap = (res && (res.staging_status || res.staging)) || null;
              if (snap && typeof window.applySelfImproveStagingSnapshot === 'function') {
                window.applySelfImproveStagingSnapshot(snap);
              }
              const n = Number((res && res.enqueued_count) || 0);
              setRadarLiveStatus(
                n > 0
                  ? ('▶️ Procesando toda la cola (' + n + ' ideas) en .liwi-staging...')
                  : '✓ La cola ya está siendo procesada en .liwi-staging.',
                { ok: true, autoHideMs: 4500 }
              );
            }
            await loadIdeas();
          } catch (err) {
            setRadarLiveStatus('Error procesando cola: ' + (err && err.message ? err.message : ''), { error: true });
          } finally {
            drainBtn.disabled = false;
            if (drainBtn.textContent === '⏳ Encolando lote...') drainBtn.textContent = orig;
          }
        });
      }

      if (reconcileBtn) {
        reconcileBtn.addEventListener('click', async () => {
          if (reconcileBtn.disabled) return;
          reconcileBtn.disabled = true;
          const orig = reconcileBtn.textContent;
          reconcileBtn.textContent = '🩺 Verificando...';
          try {
            const res = await apiFetch('/api/ideas/queue-reconcile', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: '{}',
            });
            await loadIdeas();
            const actions = (res && res.report && Array.isArray(res.report.actions)) ? res.report.actions.length : 0;
            setRadarLiveStatus(
              actions > 0
                ? ('🩺 Cola investigada y recuperada (' + actions + ' acción/es).')
                : '🩺 Cola verificada: sin atascos detectados.',
              { ok: true, autoHideMs: 4000 }
            );
          } catch (err) {
            setRadarLiveStatus('Error verificando cola: ' + (err && err.message ? err.message : ''), { error: true });
          } finally {
            reconcileBtn.disabled = false;
            reconcileBtn.textContent = orig;
          }
        });
      }

      if (filtersBarEl) {
        filtersBarEl.querySelectorAll('button[data-radar-filter]').forEach((btn) => {
          btn.addEventListener('click', () => {
            const nextFilter = btn.getAttribute('data-radar-filter') || 'pending';
            if (nextFilter === activeFilter) return;
            activeFilter = nextFilter;
            expandedIdeaId = null;
            syncFilterTabsUI();
            loadIdeas();
          });
        });
      }

      if (freqSelect) {
        freqSelect.addEventListener('change', async () => {
          const prevHours = currentIntervalHours;
          const nextHours = Number(freqSelect.value);
          freqSelect.disabled = true;
          try {
            const res = await apiFetch('/api/ideas/settings', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ auto_interval_hours: nextHours }),
            });
            syncFrequencySelect(res && res.auto_interval_hours !== undefined ? res.auto_interval_hours : nextHours);
            const selectedOpt = freqSelect.options[freqSelect.selectedIndex];
            const label = selectedOpt ? selectedOpt.textContent : '';
            setRadarLiveStatus('✓ Frecuencia guardada: ' + label, { ok: true, autoHideMs: 2800 });
          } catch (err) {
            syncFrequencySelect(prevHours);
            setRadarLiveStatus(
              'Error guardando frecuencia: ' + (err && err.message ? err.message : String(err || 'Error')),
              { error: true, autoHideMs: 4000 }
            );
          } finally {
            freqSelect.disabled = false;
          }
        });
      }

      if (genBtn) {
        genBtn.addEventListener('click', async () => {
          if (generateInFlight || genBtn.disabled) return;
          generateInFlight = true;
          const startedAtMs = Date.now();
          genBtn.disabled = true;
          genBtn.setAttribute('aria-busy', 'true');
          setRadarLiveStatus('Escaneando telemetría y generando ideas de mejora...', {
            spinner: true,
            clock: true,
            clockText: '0s',
          });
          if (generateClockTimer) {
            clearInterval(generateClockTimer);
            generateClockTimer = null;
          }
          generateClockTimer = setInterval(() => {
            if (clockEl) {
              clockEl.textContent = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000)) + 's';
            }
          }, 1000);

          try {
            const res = await apiFetch('/api/ideas/generate', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              timeoutMs: 195000,
              body: JSON.stringify({ count: 3 }),
            });
            await loadIdeas();
            const createdCount = Number(res && res.created_count ? res.created_count : 0);
            const summaryMsg = createdCount > 0
              ? ('✓ Se generaron ' + createdCount + (createdCount === 1 ? ' idea nueva.' : ' ideas nuevas.'))
              : 'No se encontraron nuevas ideas inéditas en este escaneo.';
            setRadarLiveStatus(summaryMsg, { ok: createdCount > 0, autoHideMs: 4500 });
          } catch (err) {
            setRadarLiveStatus(
              'Error generando ideas: ' + (err && err.message ? err.message : String(err || 'Error inesperado')),
              { error: true }
            );
          } finally {
            if (generateClockTimer) {
              clearInterval(generateClockTimer);
              generateClockTimer = null;
            }
            generateInFlight = false;
            genBtn.disabled = false;
            genBtn.removeAttribute('aria-busy');
          }
        });
      }

      window.refreshLiwiRadarIdeas = loadIdeas;
      try {
      loadIdeas();
      } catch (_) {}
    })();

    (function initAutonomousAndProjectIdeas() {
      const toggleBtn = document.getElementById('autonomous-toggle-btn');
      const quotaPill = document.getElementById('quota-meter-pill');
      const reserveSelect = document.getElementById('liwi-reserve-threshold');
      const cooldownSelect = document.getElementById('liwi-autonomous-cooldown');
      const visualAuditBtn = document.getElementById('liwi-visual-audit-btn');
      const subtabIdeasBtn = document.getElementById('liwi-subtab-ideas-btn');
      const subtabHistoryBtn = document.getElementById('liwi-subtab-history-btn');
      const radarFiltersEl = document.getElementById('liwi-radar-filters');
      const radarGridEl = document.getElementById('liwi-radar-grid');
      const radarEmptyEl = document.getElementById('liwi-radar-empty');
      const historyPanelEl = document.getElementById('liwi-autonomous-history-panel');
      const liveStatusEl = document.getElementById('liwi-radar-live-status');
      const liveStatusTextEl = document.getElementById('liwi-radar-status-text');
      const liveSpinnerEl = document.getElementById('liwi-radar-spinner');
      const liveClockEl = document.getElementById('liwi-radar-clock');

      let autoSubtab = 'ideas';
      let lastAutoStatus = null;
      let toggleBusy = false;
      let visualAuditBusy = false;
      let projectIdeasGenerateBusy = false;

      function setRadarNotice(text, opts) {
        const cfg = opts || {};
        if (!liveStatusEl) return;
        if (!text) {
          liveStatusEl.style.display = 'none';
          return;
        }
        liveStatusEl.style.display = 'flex';
        liveStatusEl.classList.toggle('is-ok', Boolean(cfg.ok));
        liveStatusEl.classList.toggle('is-error', Boolean(cfg.error));
        if (liveSpinnerEl) liveSpinnerEl.style.display = cfg.spinner ? 'inline-block' : 'none';
        if (liveStatusTextEl) liveStatusTextEl.textContent = String(text);
        if (liveClockEl) {
          liveClockEl.style.display = cfg.clock ? 'inline' : 'none';
          if (cfg.clockText) liveClockEl.textContent = cfg.clockText;
        }
      }

      function renderQuotaAndAutonomousHeader(st) {
        if (!st || typeof st !== 'object') return;
        lastAutoStatus = st;
        window.__liwiAutonomousHistory = Array.isArray(st.history) ? st.history : [];
        if (st.staging_queue && typeof st.staging_queue === 'object') {
          window.__liwiStagingQueue = st.staging_queue;
        }
        const enabled = Boolean(st.autonomous_enabled);
        const liveState = String(st.state || (enabled ? 'activo' : 'apagado'));
        const curTask = String(st.current_task || '').trim();
        const quota = (st.quota && typeof st.quota === 'object') ? st.quota : {};
        const remPct = Number.isFinite(Number(quota.quota_remaining_pct)) ? Number(quota.quota_remaining_pct) : 100;
        const reservePct = Number.isFinite(Number(quota.reserve_threshold_pct)) ? Number(quota.reserve_threshold_pct) : 30;
        const turnsLeft = Number.isFinite(Number(quota.estimated_turns_left)) ? Number(quota.estimated_turns_left) : 0;
        const isPaused = Boolean(quota.is_paused);
        const resumeHhmm = quota.resume_hhmm ? String(quota.resume_hhmm) : '';
        const pilotStatePill = document.getElementById('liwi-pilot-state-pill');

        const sqActiveHeader = Boolean(window.__liwiStagingQueue && window.__liwiStagingQueue.active);
        if (pilotStatePill) {
          pilotStatePill.classList.remove('is-building', 'is-investigating', 'is-paused', 'is-on');
          if (!enabled && sqActiveHeader) {
            pilotStatePill.textContent = '⚙️ Procesando cola';
            pilotStatePill.classList.add('is-building');
          } else if (!enabled) {
            pilotStatePill.textContent = 'Apagado';
          } else if (liveState === 'construyendo') {
            pilotStatePill.textContent = 'Construyendo';
            pilotStatePill.classList.add('is-building');
          } else if (liveState === 'investigando') {
            pilotStatePill.textContent = 'Investigando atasco';
            pilotStatePill.classList.add('is-investigating');
          } else if (liveState === 'pausado_cuota' || isPaused) {
            pilotStatePill.textContent = 'Pausa cuota';
            pilotStatePill.classList.add('is-paused');
          } else {
            pilotStatePill.textContent = 'Activo';
            pilotStatePill.classList.add('is-on');
          }
        }

        if (toggleBtn) {
          let label = '🤖 Automejora: OFF';
          if (enabled) {
            if (liveState === 'construyendo') label = '🤖 Automejora: Construyendo';
            else if (liveState === 'cediendo_paso') label = '🤖 Automejora: Cediendo paso';
            else if (liveState === 'pausado_cuota') label = '🤖 Automejora: Pausa cuota';
            else label = '🤖 Automejora: ON';
          }
          toggleBtn.textContent = label;
          toggleBtn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
          toggleBtn.classList.toggle('is-on', enabled && liveState !== 'pausado_cuota');
          toggleBtn.classList.toggle('is-paused', enabled && liveState === 'pausado_cuota');
          toggleBtn.title = curTask
            ? ('Construyendo: ' + curTask)
            : (st.pause_reason || st.busy_reason || 'Activar o pausar modo Automejora 24/7');
        }

        if (quotaPill) {
          quotaPill.classList.remove('is-healthy', 'is-warn', 'is-paused');
          if (isPaused) {
            quotaPill.classList.add('is-paused');
            quotaPill.textContent = '⏳ Pausa ' + (resumeHhmm ? ('hasta ' + resumeHhmm) : 'cuota');
            quotaPill.title = 'Cuota agotada temporalmente; reanudación automática programada';
          } else if (remPct <= reservePct || remPct <= reservePct + 15) {
            quotaPill.classList.add('is-warn');
            quotaPill.textContent = '🔋 ~' + remPct + '% (' + turnsLeft + 't)';
            quotaPill.title = 'Cerca del umbral de reserva (' + reservePct + '%)';
          } else {
            quotaPill.classList.add('is-healthy');
            quotaPill.textContent = '🔋 ~' + remPct + '% (' + turnsLeft + 't)';
            quotaPill.title = 'Cuota saludable (' + turnsLeft + ' turnos estimados restantes)';
          }
        }

        if (reserveSelect && Number.isFinite(reservePct)) {
          const valStr = String(reservePct);
          if (Array.from(reserveSelect.options).some((o) => o.value === valStr)) {
            reserveSelect.value = valStr;
          }
        }
        if (cooldownSelect && Number.isFinite(Number(st.cooldown_minutes))) {
          const cdStr = String(Number(st.cooldown_minutes));
          if (Array.from(cooldownSelect.options).some((o) => o.value === cdStr)) {
            cooldownSelect.value = cdStr;
          }
        }
        if (autoSubtab === 'history') {
          renderAutonomousHistory();
        }
      }

      function renderAutonomousHistory() {
        if (!historyPanelEl) return;
        const rows = (lastAutoStatus && Array.isArray(lastAutoStatus.history)) ? lastAutoStatus.history : [];
        if (!rows.length) {
          historyPanelEl.innerHTML = '<div class="liwi-radar-empty">Aún no hay mejoras autónomas registradas.</div>';
          return;
        }
        historyPanelEl.innerHTML = rows.map((entry) => {
          const idStr = String(entry.id || '');
          const title = String(entry.title || 'Mejora autónoma');
          const desc = String(entry.simple_description || '');
          const hash = String(entry.commit_hash || '').slice(0, 8);
          const appliedAt = String(entry.applied_at || '').replace('T', ' ').slice(0, 16);
          const isReverted = String(entry.status || '') === 'revertida';
          return (
            '<div class="liwi-history-card">' +
              '<div class="liwi-history-head">' +
                '<div>' +
                  '<strong style="font-size:0.88rem;">' + escapeHtml(title) + '</strong>' +
                  '<div class="liwi-history-meta">' +
                    '<span>' + escapeHtml(appliedAt) + '</span>' +
                    (hash ? (' · <code>' + escapeHtml(hash) + '</code>') : '') +
                    ' · <span>' + (isReverted ? 'Revertida' : 'Activa') + '</span>' +
                  '</div>' +
                '</div>' +
                (!isReverted
                  ? ('<button type="button" class="btn btn-sm btn-warn" data-autonomous-revert="' + escapeHtml(idStr) + '">↩️ Deshacer</button>')
                  : '<span class="badge">Revertida</span>') +
              '</div>' +
              (desc ? ('<div class="muted" style="font-size:0.82rem;">' + escapeHtml(desc) + '</div>') : '') +
            '</div>'
          );
        }).join('');

        historyPanelEl.querySelectorAll('button[data-autonomous-revert]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const entryId = btn.getAttribute('data-autonomous-revert') || '';
            if (!entryId || btn.disabled) return;
            btn.disabled = true;
            btn.textContent = 'Revirtiendo...';
            try {
              const res = await apiFetch('/api/autonomous/revert/' + encodeURIComponent(entryId), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                timeoutMs: 195000,
                body: '{}',
              });
              if (res && res.status) renderQuotaAndAutonomousHeader(res.status);
              if (typeof window.refreshLiwiRadarIdeas === 'function') window.refreshLiwiRadarIdeas();
              setRadarNotice('↩️ Mejora revertida y verificada correctamente.', { ok: true });
              if (res && res.restart_scheduled) {
                const rb = document.getElementById('factory-restart-banner');
                if (rb) rb.style.display = 'block';
              }
            } catch (err) {
              btn.disabled = false;
              btn.textContent = '↩️ Deshacer';
              setRadarNotice('Error al revertir: ' + (err && err.message ? err.message : String(err || '')), { error: true });
            }
          });
        });
      }

      function switchAutoSubtab(nextTab) {
        autoSubtab = nextTab === 'history' ? 'history' : 'ideas';
        if (subtabIdeasBtn) {
          subtabIdeasBtn.classList.toggle('active', autoSubtab === 'ideas');
          subtabIdeasBtn.setAttribute('aria-selected', autoSubtab === 'ideas' ? 'true' : 'false');
        }
        if (subtabHistoryBtn) {
          subtabHistoryBtn.classList.toggle('active', autoSubtab === 'history');
          subtabHistoryBtn.setAttribute('aria-selected', autoSubtab === 'history' ? 'true' : 'false');
        }
        if (autoSubtab === 'history') {
          if (radarFiltersEl) radarFiltersEl.style.display = 'none';
          if (radarGridEl) radarGridEl.style.display = 'none';
          if (radarEmptyEl) radarEmptyEl.style.display = 'none';
          if (historyPanelEl) historyPanelEl.style.display = 'flex';
          renderAutonomousHistory();
        } else {
          if (historyPanelEl) historyPanelEl.style.display = 'none';
          if (radarFiltersEl) radarFiltersEl.style.display = 'flex';
          if (typeof window.refreshLiwiRadarIdeas === 'function') window.refreshLiwiRadarIdeas();
        }
      }

      if (subtabIdeasBtn) subtabIdeasBtn.addEventListener('click', () => switchAutoSubtab('ideas'));
      if (subtabHistoryBtn) subtabHistoryBtn.addEventListener('click', () => switchAutoSubtab('history'));
      const pilotSettingsBtn = document.getElementById('liwi-pilot-settings-btn');
      const pilotSettingsPanel = document.getElementById('liwi-pilot-settings-panel');
      if (pilotSettingsBtn && pilotSettingsPanel) {
        pilotSettingsBtn.addEventListener('click', () => {
          const isOpen = pilotSettingsPanel.style.display !== 'none';
          pilotSettingsPanel.style.display = isOpen ? 'none' : 'flex';
          pilotSettingsBtn.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
        });
      }

      async function pollAutonomousStatus() {
        try {
          const st = await apiFetch('/api/autonomous/status', { cache: 'no-store' });
          renderQuotaAndAutonomousHeader(st);
        } catch (_) {}
      }

      if (toggleBtn) {
        toggleBtn.addEventListener('click', async () => {
          if (toggleBusy) return;
          toggleBusy = true;
          const nextEnabled = !(lastAutoStatus && lastAutoStatus.autonomous_enabled);
          try {
            const res = await apiFetch('/api/autonomous/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ autonomous_enabled: nextEnabled }),
            });
            renderQuotaAndAutonomousHeader(res);
          } catch (err) {
            setNote('No se pudo cambiar modo autónomo: ' + (err && err.message ? err.message : ''));
          } finally {
            toggleBusy = false;
          }
        });
      }

      async function saveAutoSetting(payload) {
        try {
          const res = await apiFetch('/api/autonomous/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          renderQuotaAndAutonomousHeader(res);
          setRadarNotice('✓ Configuración autónoma guardada.', { ok: true });
        } catch (err) {
          setRadarNotice('Error guardando ajuste autónomo: ' + (err && err.message ? err.message : ''), { error: true });
        }
      }

      if (reserveSelect) {
        reserveSelect.addEventListener('change', () => {
          saveAutoSetting({ reserve_threshold_pct: Number(reserveSelect.value) });
        });
      }
      if (cooldownSelect) {
        cooldownSelect.addEventListener('change', () => {
          saveAutoSetting({ cooldown_minutes: Number(cooldownSelect.value) });
        });
      }

      if (visualAuditBtn) {
        visualAuditBtn.addEventListener('click', async () => {
          if (visualAuditBusy) return;
          visualAuditBusy = true;
          visualAuditBtn.disabled = true;
          const t0 = Date.now();
          setRadarNotice('🕵️ Simulando navegación desktop y móvil con Playwright...', {
            spinner: true,
            clock: true,
            clockText: '0s',
          });
          const auditTimer = setInterval(() => {
            if (liveClockEl) {
              liveClockEl.textContent = Math.max(0, Math.floor((Date.now() - t0) / 1000)) + 's';
            }
          }, 1000);
          try {
            const res = await apiFetch('/api/ideas/visual-audit', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              timeoutMs: 195000,
              body: JSON.stringify({ base_url: window.location.origin }),
            });
            const createdNum = Number((res && res.ideas_created_count) || 0);
            const findingsNum = Number((res && res.findings_count) || 0);
            setRadarNotice(
              '✓ Simulación completada: ' + findingsNum + ' hallazgos, ' + createdNum + ' ideas nuevas.',
              { ok: true }
            );
            switchAutoSubtab('ideas');
          } catch (err) {
            setRadarNotice('Error en simulación visual: ' + (err && err.message ? err.message : ''), { error: true });
          } finally {
            clearInterval(auditTimer);
            visualAuditBusy = false;
            visualAuditBtn.disabled = false;
          }
        });
      }

      function syncProjectIdeasBadge(pid) {
        const badgeEl = document.getElementById('project-ideas-tab-badge');
        if (!badgeEl) return;
        const list = (state.projectIdeasByPid && Array.isArray(state.projectIdeasByPid[pid]))
          ? state.projectIdeasByPid[pid]
          : [];
        const activeCount = list.filter((it) => it && String(it.status) === 'nueva').length;
        if (activeCount > 0) {
          badgeEl.textContent = String(activeCount);
          badgeEl.style.display = 'inline-flex';
        } else {
          badgeEl.textContent = '0';
          badgeEl.style.display = 'none';
        }
      }

      function renderProjectIdeasTab(pid) {
        const pane = document.getElementById('tab-ideas');
        if (!pane || !pid) return;
        const list = (state.projectIdeasByPid && Array.isArray(state.projectIdeasByPid[pid]))
          ? state.projectIdeasByPid[pid].filter((it) => it && String(it.status) !== 'descartada')
          : [];
        syncProjectIdeasBadge(pid);

        const cardsHtml = list.length
          ? ('<div class="proj-ideas-grid">' + list.map((it) => {
              const iid = String(it.id || '');
              const title = String(it.title || '');
              const impact = String(it.impact || 'Medio');
              const desc = String(it.simple_description || '');
              const source = String(it.source || 'manual');
              const status = String(it.status || 'nueva');
              const isImpl = status === 'implementando' || status === 'aplicada';
              return (
                '<article class="proj-idea-card">' +
                  '<div style="display:flex; justify-content:space-between; align-items:flex-start; gap:6px; flex-wrap:wrap;">' +
                    '<strong style="font-size:0.9rem;">' + escapeHtml(title) + '</strong>' +
                    '<span class="badge">' + (source === 'runtime_error' ? '🩹 Error en ejecución' : escapeHtml('Impacto ' + impact)) + '</span>' +
                  '</div>' +
                  '<p class="muted" style="margin:0; font-size:0.84rem;">' + escapeHtml(desc) + '</p>' +
                  '<div class="proj-idea-card-actions">' +
                    '<button type="button" class="btn btn-primary btn-sm" data-proj-idea-approve="' + escapeHtml(iid) + '"' + (isImpl ? ' disabled' : '') + '>' +
                      (isImpl ? '⏳ Implementando' : '✨ Aprobar e implementar') +
                    '</button>' +
                    '<button type="button" class="btn btn-sm" data-proj-idea-dismiss="' + escapeHtml(iid) + '">Descartar</button>' +
                  '</div>' +
                '</article>'
              );
            }).join('') + '</div>')
          : '<div class="liwi-radar-empty">Sin mejoras detectadas para este proyecto todavía. Pulsa «💡 Detectar mejoras».</div>';

        pane.innerHTML =
          '<div class="proj-ideas-head">' +
            '<div><strong>💡 Auto-Mejoras del Proyecto</strong><div class="muted" style="font-size:0.8rem;">Sugerencias específicas de producto, UX y errores detectados para esta app.</div></div>' +
            '<button id="project-ideas-generate-btn" class="btn btn-primary btn-sm" type="button"' + (projectIdeasGenerateBusy ? ' disabled' : '') + '>' +
              (projectIdeasGenerateBusy ? '⏳ Detectando...' : '💡 Detectar mejoras') +
            '</button>' +
          '</div>' +
          cardsHtml;

        const genProjBtn = document.getElementById('project-ideas-generate-btn');
        if (genProjBtn) {
          genProjBtn.addEventListener('click', async () => {
            if (projectIdeasGenerateBusy) return;
            projectIdeasGenerateBusy = true;
            renderProjectIdeasTab(pid);
            try {
              const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/ideas/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                timeoutMs: 195000,
                body: JSON.stringify({ count: 3 }),
              });
              state.projectIdeasByPid[pid] = Array.isArray(res && res.ideas) ? res.ideas : [];
              state.projectIdeasLoadedFor[pid] = true;
            } catch (err) {
              setNote('Error detectando mejoras: ' + (err && err.message ? err.message : ''));
            } finally {
              projectIdeasGenerateBusy = false;
              renderProjectIdeasTab(pid);
            }
          });
        }

        pane.querySelectorAll('button[data-proj-idea-approve]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const ideaId = btn.getAttribute('data-proj-idea-approve') || '';
            if (!ideaId || btn.disabled) return;
            btn.disabled = true;
            btn.textContent = '⏳ Aplicando...';
            try {
              const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/ideas/' + encodeURIComponent(ideaId) + '/approve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
              });
              if (Array.isArray(res && res.ideas)) state.projectIdeasByPid[pid] = res.ideas;
              renderProjectIdeasTab(pid);
              loadProjects();
            } catch (err) {
              btn.disabled = false;
              btn.textContent = '✨ Aprobar e implementar';
              setNote('Error aplicando mejora: ' + (err && err.message ? err.message : ''));
            }
          });
        });

        pane.querySelectorAll('button[data-proj-idea-dismiss]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const ideaId = btn.getAttribute('data-proj-idea-dismiss') || '';
            if (!ideaId || btn.disabled) return;
            btn.disabled = true;
            try {
              const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/ideas/' + encodeURIComponent(ideaId) + '/dismiss', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
              });
              if (Array.isArray(res && res.ideas)) state.projectIdeasByPid[pid] = res.ideas;
              renderProjectIdeasTab(pid);
            } catch (_) {
              btn.disabled = false;
            }
          });
        });
      }

      async function loadProjectIdeasFor(pid) {
        if (!pid) return;
        try {
          const res = await apiFetch('/api/projects/' + encodeURIComponent(pid) + '/ideas', { cache: 'no-store' });
          state.projectIdeasByPid[pid] = Array.isArray(res && res.ideas) ? res.ideas : [];
          state.projectIdeasLoadedFor[pid] = true;
          if (state.selectedId === pid) renderProjectIdeasTab(pid);
        } catch (_) {}
      }

      window.loadProjectIdeasFor = loadProjectIdeasFor;
      window.renderProjectIdeasTab = renderProjectIdeasTab;
      window.pollAutonomousStatus = pollAutonomousStatus;
      try { pollAutonomousStatus(); } catch (_) {}
      setInterval(() => { try { pollAutonomousStatus(); } catch (_) {} }, 12000);
    })();

    (function initClientTriageSensorAndBanner() {
      const recentErrorSentAt = new Map();
      const ANTI_SPAM_MS = 15000;
      const bannerEl = document.getElementById('triage-alert-banner');
      const summaryEl = document.getElementById('triage-alert-summary');
      const feedbackEl = document.getElementById('triage-alert-feedback');
      const repairBtn = document.getElementById('triage-repair-btn');
      const copyBtn = document.getElementById('triage-copy-prompt-btn');
      const dismissBtn = document.getElementById('triage-dismiss-btn');
      const solutionsBannerEl = document.getElementById('triage-solutions-banner');
      const solutionsCountEl = document.getElementById('triage-solutions-count');
      const solutionsSummaryEl = document.getElementById('triage-solutions-summary');
      const solutionApproveBtn = document.getElementById('triage-solution-approve-btn');
      const solutionDismissBtn = document.getElementById('triage-solution-dismiss-btn');
      let currentTicket = null;
      let currentSolution = null;

      function showBannerFeedback(msg) {
        if (!feedbackEl) return;
        feedbackEl.textContent = String(msg || '');
        feedbackEl.style.display = msg ? 'block' : 'none';
      }

      function renderTriageBanner(tickets) {
        const list = Array.isArray(tickets) ? tickets : [];
        currentTicket = list.length ? list[0] : null;
        if (!bannerEl) return;
        if (!currentTicket) {
          bannerEl.style.display = 'none';
          return;
        }
        if (summaryEl) {
          summaryEl.textContent = String(currentTicket.simple_summary || currentTicket.error_text || '');
        }
        bannerEl.style.display = 'flex';
      }

      async function refreshTriageTickets() {
        try {
          const data = await apiFetch('/api/triage/tickets', { cache: 'no-store' });
          renderTriageBanner(data && data.tickets);
        } catch (_) {}
      }

      function reportLiwiClientError(source, errorText, context) {
        const cleanText = String(errorText || '').trim();
        if (!cleanText) return;
        const key = String(source || 'browser_js') + '::' + cleanText.slice(0, 180);
        const now = Date.now();
        const prev = recentErrorSentAt.get(key) || 0;
        if (now - prev < ANTI_SPAM_MS) return;
        recentErrorSentAt.set(key, now);
        fetch('/api/telemetry/client-error', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source: String(source || 'browser_js'),
            error_text: cleanText.slice(0, 4000),
            context: context || { href: window.location.href },
          }),
        }).then(() => refreshTriageTickets()).catch(() => {});
      }
      window.reportLiwiClientError = reportLiwiClientError;

      window.addEventListener('error', (ev) => {
        const detail = (ev && ev.error && ev.error.stack)
          ? String(ev.error.stack)
          : (String((ev && ev.message) || 'Error JS') + ' @ ' + String((ev && ev.filename) || '') + ':' + String((ev && ev.lineno) || 0));
        reportLiwiClientError('window.onerror', detail, {
          filename: ev ? ev.filename : '',
          lineno: ev ? ev.lineno : 0,
          colno: ev ? ev.colno : 0,
        });
      });

      window.addEventListener('unhandledrejection', (ev) => {
        const reason = ev ? ev.reason : null;
        const detail = (reason && reason.stack) ? String(reason.stack) : String(reason || 'Unhandled promise rejection');
        reportLiwiClientError('unhandledrejection', detail);
      });

      if (repairBtn) {
        repairBtn.addEventListener('click', async () => {
          if (!currentTicket || repairBtn.disabled) return;
          repairBtn.disabled = true;
          repairBtn.textContent = '⏳ Reparando...';
          try {
            await apiFetch('/api/triage/tickets/' + encodeURIComponent(String(currentTicket.id)) + '/repair', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: '{}',
            });
            showBannerFeedback('✓ Auto-reparación encolada en .liwi-staging.');
            setTimeout(refreshTriageTickets, 1200);
          } catch (err) {
            showBannerFeedback('Error lanzando auto-reparación: ' + (err && err.message ? err.message : ''));
          } finally {
            repairBtn.disabled = false;
            repairBtn.textContent = '✨ Reparar ahora';
          }
        });
      }

      if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
          if (!currentTicket || !currentTicket.repair_prompt) return;
          const txt = String(currentTicket.repair_prompt);
          try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              await navigator.clipboard.writeText(txt);
            } else {
              const ta = document.createElement('textarea');
              ta.value = txt;
              ta.style.position = 'fixed';
              ta.style.opacity = '0';
              document.body.appendChild(ta);
              ta.select();
              document.execCommand('copy');
              document.body.removeChild(ta);
            }
            copyBtn.textContent = '✓ Prompt copiado';
            showBannerFeedback('Prompt técnico copiado al portapapeles.');
            setTimeout(() => { copyBtn.textContent = '📋 Copiar prompt para Antigravity'; }, 2500);
          } catch (_) {
            showBannerFeedback('No se pudo copiar automáticamente al portapapeles.');
          }
        });
      }

      if (dismissBtn) {
        dismissBtn.addEventListener('click', async () => {
          if (!currentTicket) return;
          try {
            await apiFetch('/api/triage/tickets/' + encodeURIComponent(String(currentTicket.id)) + '/dismiss', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: '{}',
            });
            refreshTriageTickets();
          } catch (_) {}
        });
      }

      function renderTriageSolutions(solutions, pendingCount) {
        const list = Array.isArray(solutions) ? solutions : [];
        const total = Number.isFinite(Number(pendingCount)) ? Number(pendingCount) : list.length;
        currentSolution = list.length ? list[0] : null;
        if (solutionsCountEl) {
          solutionsCountEl.textContent = String(total);
        }
        if (!solutionsBannerEl) return;
        if (!currentSolution || total <= 0) {
          solutionsBannerEl.style.display = 'none';
          return;
        }
        if (solutionsSummaryEl) {
          const title = String(currentSolution.title || currentSolution.raw_error || 'Solución propuesta').trim();
          const fix = String(currentSolution.proposed_fix || '').trim();
          solutionsSummaryEl.textContent = fix ? (title + ' — ' + fix) : title;
        }
        solutionsBannerEl.style.display = 'flex';
      }

      async function refreshTriageSolutions() {
        try {
          const data = await apiFetch('/api/triage/solutions?status=lista', { cache: 'no-store' });
          renderTriageSolutions(data && data.solutions, data && data.pending_count);
        } catch (_) {}
      }

      async function submitTriageSolutionDecision(decision) {
        if (!currentSolution) return;
        try {
          await apiFetch('/api/triage/solutions/' + encodeURIComponent(String(currentSolution.id)) + '/decision', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision }),
          });
          await refreshTriageSolutions();
        } catch (_) {}
      }

      if (solutionApproveBtn) {
        solutionApproveBtn.addEventListener('click', () => { submitTriageSolutionDecision('aprobada'); });
      }
      if (solutionDismissBtn) {
        solutionDismissBtn.addEventListener('click', () => { submitTriageSolutionDecision('descartada'); });
      }

      window.loadTriageTickets = refreshTriageTickets;
      window.loadTriageSolutions = refreshTriageSolutions;
      try { refreshTriageTickets(); } catch (_) {}
      try { refreshTriageSolutions(); } catch (_) {}
      setInterval(() => {
        try { refreshTriageTickets(); } catch (_) {}
        try { refreshTriageSolutions(); } catch (_) {}
      }, 15000);
    })();

    setInterval(updateLiveTimers, 1000);
    setInterval(() => {
      try { loadProjects(); } catch (_) {}
    }, 5000);
    try { loadProjects(); } catch (err) {
      const listEl = document.getElementById('project-list');
      if (listEl && !state.projects.length) {
        listEl.innerHTML = '<div class="empty-box" role="alert"><div style="margin-bottom:8px;">Error iniciando vista de proyectos.</div><button id="retry-load-projects-btn" class="btn btn-primary btn-sm" type="button">🔄 Reintentar</button></div>';
        const rBtn = document.getElementById('retry-load-projects-btn');
        if (rBtn) rBtn.addEventListener('click', () => { loadProjects(); });
      }
    }
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
        self._closing = threading.Event()
        super().__init__((host, port), _DashboardRequestHandler)

    def server_close(self) -> None:
        """Ensure locally launched apps, staging worker, and autonomous engine stop cleanly on shutdown."""
        self._closing.set()
        try:
            self.controller.local_runner.stop_all()
        finally:
            try:
                eng = getattr(self.controller, "_autonomous_engine", None)
                if eng is not None and hasattr(eng, "stop"):
                    eng.stop(timeout=2.0)
            finally:
                try:
                    self.controller.shutdown_staging(timeout=2.0)
                finally:
                    super().server_close()


class _DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler serving the self-contained dashboard UI and JSON REST endpoints."""

    server: DashboardHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        """Silence stderr access logs and route diagnostics at debug level."""
        logger.debug("dashboard: " + format, *args)

    def _send_raw_response(self, status_code: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            logger.debug("Client disconnected before receiving HTTP response (%s)", status_code)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        scrubbed = _scrub_value(payload, self.server.controller.secrets_to_redact)
        body = json.dumps(scrubbed, ensure_ascii=False).encode("utf-8")
        self._send_raw_response(status_code, "application/json; charset=utf-8", body)

    def _send_html(self, status_code: int, html: str) -> None:
        body = html.encode("utf-8")
        self._send_raw_response(status_code, "text/html; charset=utf-8", body)

    def _send_bytes(self, status_code: int, content_type: str, body: bytes) -> None:
        self._send_raw_response(status_code, content_type, body)

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

    def _read_json_body(self, *, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        if length <= 0:
            return {}
        if length > max_bytes:
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
            "recover",
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
        if len(parts) == 3 and parts[1] == "runtime" and parts[2] in {"start", "stop"}:
            return urllib.parse.unquote(parts[0]), parts[2]
        # Reject multi-segment or path-traversal attempts (e.g., ../etc) as invalid project_id
        return urllib.parse.unquote(rest), None

    def _send_controller_json(self, fn: Callable[[], dict[str, Any]]) -> None:
        try:
            result = fn()
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

    def _parse_project_chat_entry_route(self) -> tuple[str | None, str | None, str | None]:
        """Parse /api/projects/<project_id>/chat/<entry_id>[/retry] safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/projects/"
        if not raw_path.startswith(prefix):
            return None, None, None
        rest = raw_path[len(prefix) :]
        parts = rest.split("/")
        if len(parts) == 3 and parts[1] == "chat":
            return (
                urllib.parse.unquote(parts[0]),
                urllib.parse.unquote(parts[2]),
                None,
            )
        if len(parts) == 4 and parts[1] == "chat" and parts[3] == "retry":
            return (
                urllib.parse.unquote(parts[0]),
                urllib.parse.unquote(parts[2]),
                "retry",
            )
        return None, None, None

    def _parse_project_quality_route(self) -> tuple[str | None, str | None]:
        """Parse /api/projects/<project_id>/quality/<evaluate|improve> safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/projects/"
        if not raw_path.startswith(prefix):
            return None, None
        rest = raw_path[len(prefix) :]
        parts = rest.split("/")
        if len(parts) >= 3 and parts[-2] == "quality" and parts[-1] in {"evaluate", "improve"}:
            return urllib.parse.unquote("/".join(parts[:-2])), parts[-1]
        return None, None

    def _parse_idea_route(self) -> tuple[int | None, str | None]:
        """Parse /api/ideas/<id>[/<implement|dismiss|run-now>] safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/ideas/"
        if not raw_path.startswith(prefix):
            return None, None
        rest = raw_path[len(prefix) :]
        if not rest:
            return None, None
        parts = rest.split("/")
        if len(parts) == 1:
            raw_id = urllib.parse.unquote(parts[0])
            action: str | None = None
        elif len(parts) == 2 and parts[1] in {"implement", "dismiss", "run-now", "remove"}:
            raw_id = urllib.parse.unquote(parts[0])
            action = parts[1]
        else:
            return None, None
        if not (raw_id.isascii() and raw_id.isdigit()):
            raise ValueError("ID de idea inválido; debe ser un entero positivo.")
        idea_id = int(raw_id)
        if idea_id <= 0:
            raise ValueError("ID de idea inválido; debe ser un entero positivo.")
        return idea_id, action

    def _parse_autonomous_revert_route(self) -> int | None:
        """Parse /api/autonomous/revert/<id> safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/autonomous/revert/"
        if not raw_path.startswith(prefix):
            return None
        raw_id = urllib.parse.unquote(raw_path[len(prefix) :]).strip("/")
        if not (raw_id.isascii() and raw_id.isdigit()):
            raise ValueError("ID de historial autónomo inválido; debe ser un entero positivo.")
        entry_id = int(raw_id)
        if entry_id <= 0:
            raise ValueError("ID de historial autónomo inválido; debe ser un entero positivo.")
        return entry_id

    def _parse_project_ideas_route(self) -> tuple[str | None, str | None, int | None]:
        """Parse /api/projects/<slug>/ideas[/generate|/<id>/approve|/<id>/dismiss] safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/projects/"
        if not raw_path.startswith(prefix):
            return None, None, None
        rest = raw_path[len(prefix) :]
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] == "ideas":
            return urllib.parse.unquote(parts[0]), "list", None
        if len(parts) == 3 and parts[1] == "ideas" and parts[2] == "generate":
            return urllib.parse.unquote(parts[0]), "generate", None
        if len(parts) == 4 and parts[1] == "ideas" and parts[3] in {"approve", "dismiss"}:
            raw_id = urllib.parse.unquote(parts[2])
            if not (raw_id.isascii() and raw_id.isdigit()):
                raise ValueError("ID de idea de proyecto inválido; debe ser un entero positivo.")
            idea_id = int(raw_id)
            if idea_id <= 0:
                raise ValueError("ID de idea de proyecto inválido; debe ser un entero positivo.")
            return urllib.parse.unquote(parts[0]), parts[3], idea_id
        return None, None, None

    def _parse_triage_ticket_route(self) -> tuple[int | None, str | None]:
        """Parse /api/triage/tickets/<id>/<repair|dismiss> safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/triage/tickets/"
        if not raw_path.startswith(prefix):
            return None, None
        rest = raw_path[len(prefix) :]
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] in {"repair", "dismiss"}:
            raw_id = urllib.parse.unquote(parts[0])
            if not (raw_id.isascii() and raw_id.isdigit()):
                raise ValueError("ID de ticket inválido; debe ser un entero positivo.")
            tid = int(raw_id)
            if tid <= 0:
                raise ValueError("ID de ticket inválido; debe ser un entero positivo.")
            return tid, parts[1]
        return None, None

    def _parse_triage_solution_decision_route(self) -> int | None:
        """Parse /api/triage/solutions/<id>/decision safely (INV-22)."""
        raw_path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/triage/solutions/"
        if not raw_path.startswith(prefix):
            return None
        rest = raw_path[len(prefix) :]
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] == "decision":
            raw_id = urllib.parse.unquote(parts[0])
            if not (raw_id.isascii() and raw_id.isdigit()):
                raise ValueError("ID de solución inválido; debe ser un entero positivo.")
            sol_id = int(raw_id)
            if sol_id <= 0:
                raise ValueError("ID de solución inválido; debe ser un entero positivo.")
            return sol_id
        return None

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlsplit(self.path)
        path = parsed_url.path
        ctrl = self.server.controller

        if path == "/" or path == "/index.html":
            self._send_html(200, DASHBOARD_HTML)
            return

        if path == "/api/projects":
            try:
                projects = ctrl.list_projects()
            except Exception as exc:  # noqa: BLE001
                clean_err = redact_secrets(
                    str(exc) or exc.__class__.__name__,
                    extra_secrets=ctrl.secrets_to_redact,
                )
                logger.warning("GET /api/projects degraded after store error: %s", clean_err)
                self._send_json(
                    503,
                    {
                        "error": "No se pudo consultar la lista de proyectos temporalmente.",
                        "detail": clean_err,
                        "degraded": True,
                    },
                )
                return
            self._send_json(200, {"projects": projects})
            return

        if path == "/api/telemetry/summary":
            qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
            if "limit" in qs:
                raw_limit = str(qs["limit"][-1]).strip()
                try:
                    limit_val = int(raw_limit)
                except ValueError:
                    self._send_json(400, {"error": "Parámetro 'limit' inválido; debe ser un entero."})
                    return
                bounded_limit = max(
                    telemetry.MIN_SUMMARY_LIMIT,
                    min(limit_val, telemetry.MAX_SUMMARY_LIMIT),
                )
                self._send_json(200, telemetry.get_telemetry_summary(limit=bounded_limit))
                return
            self._send_json(200, telemetry.get_telemetry_summary())
            return

        if path == "/api/settings/lan-mode":
            self._send_json(200, ctrl.get_lan_mode())
            return

        if path == "/api/factory/assistant/history":
            self._send_json(200, ctrl.get_factory_assistant_history())
            return

        if path == "/api/factory/assistant/status":
            self._send_json(200, ctrl.get_factory_assistant_status())
            return

        if path == "/api/self-improve/staging":
            self._send_json(200, ctrl.get_self_improve_staging_status())
            return

        if path == "/api/ideas":
            def _get_ideas() -> dict[str, Any]:
                qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
                raw_status = str(qs["status"][-1]).strip() if "status" in qs else ""
                eff_status = raw_status or None
                return ctrl.list_improvement_ideas(status=eff_status)

            self._send_controller_json(_get_ideas)
            return

        if path == "/api/autonomous/status":
            self._send_controller_json(ctrl.get_autonomous_status)
            return

        if path == "/api/triage/tickets":
            self._send_controller_json(ctrl.list_triage_tickets)
            return

        if path == "/api/triage/solutions":
            def _get_triage_solutions() -> dict[str, Any]:
                qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
                raw_status = str(qs["status"][-1]).strip() if "status" in qs else ""
                eff_status = raw_status or None
                raw_scope = str(qs["scope"][-1]).strip() if "scope" in qs else ""
                eff_scope = raw_scope or None
                limit_val = 50
                if "limit" in qs:
                    raw_limit = str(qs["limit"][-1]).strip()
                    try:
                        limit_val = int(raw_limit)
                    except ValueError as exc:
                        raise ValueError("Parámetro 'limit' inválido; debe ser un entero.") from exc
                return ctrl.list_triage_solutions(
                    status=eff_status,
                    scope=eff_scope,
                    limit=limit_val,
                )

            self._send_controller_json(_get_triage_solutions)
            return

        proj_ideas_pid, proj_ideas_action, _proj_idea_id = self._parse_project_ideas_route()
        if proj_ideas_pid is not None and proj_ideas_action == "list":
            def _get_project_ideas() -> dict[str, Any]:
                qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
                raw_status = str(qs["status"][-1]).strip() if "status" in qs else ""
                eff_status = raw_status or None
                return ctrl.list_project_ideas(proj_ideas_pid, status=eff_status)

            self._send_controller_json(_get_project_ideas)
            return

        if path == "/api/factory/assistant/versions":
            qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
            if "limit" in qs:
                raw_limit = str(qs["limit"][-1]).strip()
                try:
                    limit_val = int(raw_limit)
                except ValueError:
                    self._send_json(400, {"error": "Parámetro 'limit' inválido; debe ser un entero."})
                    return
                self._send_json(200, ctrl.get_factory_assistant_versions(limit=limit_val))
                return
            self._send_json(200, ctrl.get_factory_assistant_versions())
            return

        if path.startswith("/api/factory/assistant/uploads/"):
            raw_fname = urllib.parse.unquote(path[len("/api/factory/assistant/uploads/") :])
            try:
                mime, data = ctrl.get_factory_assistant_upload(raw_fname)
            except (ValueError, SandboxViolationError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ProjectNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            self._send_bytes(200, mime, data)
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

        if path == "/api/factory/assistant/message":
            def _post_factory_msg() -> dict[str, Any]:
                body = self._read_json_body(max_bytes=8 * 1024 * 1024)
                raw_msg = body.get("message", "")
                if raw_msg is not None and not isinstance(raw_msg, str):
                    raise TypeError("El campo 'message' debe ser texto.")
                raw_imgs = body.get("images") or ()
                if not isinstance(raw_imgs, list) and raw_imgs != ():
                    raise TypeError("El campo 'images' debe ser una lista.")
                return ctrl.post_factory_assistant_message(str(raw_msg or ""), images=raw_imgs)

            self._send_controller_json(_post_factory_msg)
            return

        if path == "/api/factory/assistant/apply":
            def _post_factory_apply() -> dict[str, Any]:
                body = self._read_json_body()
                raw_prompt = body.get("proposal_prompt")
                if raw_prompt is None:
                    raw_prompt = body.get("prompt")
                if not isinstance(raw_prompt, str) or not raw_prompt.strip():
                    raise ValueError("El campo 'proposal_prompt' es obligatorio.")
                raw_title = body.get("proposal_title")
                if raw_title is None:
                    raw_title = body.get("title", "")
                raw_mid = body.get("message_id")
                raw_use_staging = body.get("use_staging")
                if raw_use_staging is not None and not isinstance(raw_use_staging, bool):
                    raise TypeError("El campo 'use_staging' debe ser booleano.")
                return ctrl.apply_factory_assistant_proposal(
                    raw_prompt,
                    proposal_title=str(raw_title or ""),
                    message_id=str(raw_mid) if raw_mid is not None else None,
                    use_staging=raw_use_staging,
                )

            self._send_controller_json(_post_factory_apply)
            return

        if path == "/api/self-improve/promote":
            def _post_self_improve_promote() -> dict[str, Any]:
                body = self._read_json_body()
                raw_restart = body.get("trigger_restart")
                if raw_restart is not None and not isinstance(raw_restart, bool):
                    raise TypeError("El campo 'trigger_restart' debe ser booleano.")
                return ctrl.promote_self_improve_staging(trigger_restart=raw_restart)

            self._send_controller_json(_post_self_improve_promote)
            return

        if path == "/api/staging/visual-qa":
            def _post_staging_visual_qa() -> dict[str, Any]:
                body = self._read_json_body()
                raw_url = body.get("base_url")
                if raw_url is not None and not isinstance(raw_url, str):
                    raise TypeError("El campo 'base_url' debe ser texto.")
                raw_timeout = body.get("timeout_seconds")
                if raw_timeout is not None and (
                    isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float))
                ):
                    raise TypeError("El campo 'timeout_seconds' debe ser numérico.")
                eff_default_url = f"http://{LOOPBACK_HOST}:{self.server.server_address[1]}"
                return ctrl.run_staging_visual_qa(
                    base_url=raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None,
                    timeout_seconds=float(raw_timeout) if raw_timeout is not None else None,
                    default_base_url=eff_default_url,
                )

            self._send_controller_json(_post_staging_visual_qa)
            return

        if path == "/api/ideas/generate":
            def _post_ideas_generate() -> dict[str, Any]:
                body = self._read_json_body()
                has_interval = (
                    "auto_interval_hours" in body and body["auto_interval_hours"] is not None
                )
                raw_interval = body["auto_interval_hours"] if has_interval else None

                has_generate = "generate" in body and body["generate"] is not None
                if has_generate:
                    if not isinstance(body["generate"], bool):
                        raise TypeError("El campo 'generate' debe ser booleano.")
                    should_generate = bool(body["generate"])
                elif has_interval and "count" not in body:
                    should_generate = False
                else:
                    should_generate = True

                raw_count = body.get("count", idea_engine.DEFAULT_IDEATION_COUNT)
                if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                    raise TypeError("El campo 'count' debe ser un entero positivo.")
                if raw_count <= 0:
                    raise ValueError("El campo 'count' debe ser un entero positivo.")

                return ctrl.generate_improvement_ideas(
                    count=raw_count,
                    auto_interval_hours=raw_interval,
                    generate=should_generate,
                )

            self._send_controller_json(_post_ideas_generate)
            return

        if path == "/api/ideas/settings":
            def _post_ideas_settings() -> dict[str, Any]:
                body = self._read_json_body()
                if "auto_interval_hours" not in body:
                    raise ValueError("El campo 'auto_interval_hours' es obligatorio.")
                return ctrl.set_idea_radar_settings(body["auto_interval_hours"])

            self._send_controller_json(_post_ideas_settings)
            return

        if path == "/api/ideas/queue-reconcile":
            self._drain_body()
            self._send_controller_json(ctrl.reconcile_idea_queue_now)
            return

        if path == "/api/ideas/drain-queue":
            self._drain_body()
            self._send_controller_json(ctrl.drain_idea_queue)
            return

        if path == "/api/ideas/visual-audit":
            def _post_visual_audit() -> dict[str, Any]:
                body = self._read_json_body()
                raw_url = body.get("base_url")
                if raw_url is not None and not isinstance(raw_url, str):
                    raise TypeError("El campo 'base_url' debe ser texto.")
                eff_url = (
                    raw_url.strip()
                    if isinstance(raw_url, str) and raw_url.strip()
                    else f"http://{LOOPBACK_HOST}:{self.server.server_address[1]}"
                )
                return ctrl.run_visual_audit(base_url=eff_url)

            self._send_controller_json(_post_visual_audit)
            return

        if path == "/api/telemetry/client-error":
            def _post_client_error() -> dict[str, Any]:
                body = self._read_json_body()
                raw_source = str(body.get("source") or "browser_js").strip()
                raw_err = body.get("error_text")
                if raw_err is None:
                    raw_err = body.get("message")
                if not isinstance(raw_err, str) or not raw_err.strip():
                    raise ValueError("El campo 'error_text' es obligatorio.")
                return ctrl.report_client_error(
                    source=raw_source,
                    error_text=raw_err,
                    context=body.get("context"),
                )

            self._send_controller_json(_post_client_error)
            return

        if path.startswith("/api/triage/tickets/"):
            def _post_triage_ticket_action() -> dict[str, Any]:
                tid, t_action = self._parse_triage_ticket_route()
                if tid is None or t_action not in {"repair", "dismiss"}:
                    raise ProjectNotFoundError("Endpoint not found")
                self._drain_body()
                if t_action == "repair":
                    return ctrl.repair_triage_ticket(tid)
                return ctrl.dismiss_triage_ticket(tid)

            self._send_controller_json(_post_triage_ticket_action)
            return

        if path.startswith("/api/triage/solutions/"):
            def _post_triage_solution_decision() -> dict[str, Any]:
                sol_id = self._parse_triage_solution_decision_route()
                if sol_id is None:
                    raise ProjectNotFoundError("Endpoint not found")
                body = self._read_json_body()
                decision = self._require_str_field(body, "decision")
                return ctrl.decide_triage_solution(sol_id, decision)

            self._send_controller_json(_post_triage_solution_decision)
            return

        if path == "/api/autonomous/toggle":
            def _post_autonomous_toggle() -> dict[str, Any]:
                body = self._read_json_body()
                raw_enabled = body.get("autonomous_enabled")
                if raw_enabled is None and "enabled" in body:
                    raw_enabled = body.get("enabled")
                if raw_enabled is not None and not isinstance(raw_enabled, bool):
                    raise TypeError("El campo 'autonomous_enabled' debe ser booleano.")
                raw_cooldown = body.get("cooldown_minutes")
                if raw_cooldown is not None and (
                    isinstance(raw_cooldown, bool) or not isinstance(raw_cooldown, int)
                ):
                    raise TypeError("El campo 'cooldown_minutes' debe ser entero.")
                raw_reserve = body.get("reserve_threshold_pct")
                if raw_reserve is not None and (
                    isinstance(raw_reserve, bool) or not isinstance(raw_reserve, int)
                ):
                    raise TypeError("El campo 'reserve_threshold_pct' debe ser entero.")
                return ctrl.toggle_autonomous_mode(
                    autonomous_enabled=raw_enabled,
                    cooldown_minutes=raw_cooldown,
                    reserve_threshold_pct=raw_reserve,
                )

            self._send_controller_json(_post_autonomous_toggle)
            return

        if path.startswith("/api/autonomous/revert/"):
            def _post_autonomous_revert() -> dict[str, Any]:
                entry_id = self._parse_autonomous_revert_route()
                if entry_id is None:
                    raise ProjectNotFoundError("Endpoint not found")
                self._drain_body()
                return ctrl.revert_autonomous_improvement(entry_id)

            self._send_controller_json(_post_autonomous_revert)
            return

        proj_ideas_pid, proj_ideas_action, proj_idea_id = self._parse_project_ideas_route()
        if proj_ideas_pid is not None and proj_ideas_action is not None:
            def _post_project_ideas() -> dict[str, Any]:
                if proj_ideas_action == "generate":
                    body = self._read_json_body()
                    raw_count = body.get("count", idea_engine.DEFAULT_IDEATION_COUNT)
                    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count <= 0:
                        raise ValueError("El campo 'count' debe ser un entero positivo.")
                    return ctrl.generate_project_ideas(proj_ideas_pid, count=raw_count)
                if proj_ideas_action == "approve" and proj_idea_id is not None:
                    self._drain_body()
                    return ctrl.approve_project_idea(proj_ideas_pid, proj_idea_id)
                if proj_ideas_action == "dismiss" and proj_idea_id is not None:
                    self._drain_body()
                    return ctrl.dismiss_project_idea(proj_ideas_pid, proj_idea_id)
                raise ProjectNotFoundError("Endpoint not found")

            self._send_controller_json(_post_project_ideas)
            return

        if path.startswith("/api/ideas/"):
            def _post_idea_action() -> dict[str, Any]:
                idea_id, idea_action = self._parse_idea_route()
                if idea_id is None or idea_action not in {"implement", "dismiss", "run-now", "remove"}:
                    raise ProjectNotFoundError("Endpoint not found")
                self._drain_body()
                if idea_action == "implement":
                    return ctrl.implement_improvement_idea(idea_id)
                if idea_action == "run-now":
                    return ctrl.run_improvement_idea_now(idea_id)
                if idea_action == "remove":
                    return ctrl.remove_queued_idea(idea_id)
                return ctrl.dismiss_improvement_idea(idea_id)

            self._send_controller_json(_post_idea_action)
            return

        chat_pid, chat_entry_id, chat_subact = self._parse_project_chat_entry_route()
        if chat_pid is not None and chat_entry_id is not None and chat_subact == "retry":
            self._drain_body()
            self._send_controller_json(
                lambda: ctrl.retry_project_chat_entry(chat_pid, chat_entry_id)
            )
            return

        q_pid, q_action = self._parse_project_quality_route()
        if q_pid is not None and q_action is not None:
            if q_action == "evaluate":
                self._drain_body()
                self._send_controller_json(lambda: ctrl.evaluate_project_quality(q_pid))
            else:
                def _run_quality_improve() -> dict[str, Any]:
                    body = self._read_json_body()
                    raw_force = body.get("force_polish", False)
                    if not isinstance(raw_force, bool):
                        raise TypeError("El campo 'force_polish' debe ser booleano.")
                    return ctrl.improve_project_quality(q_pid, force_polish=raw_force)

                self._send_controller_json(_run_quality_improve)
            return

        pid, action = self._parse_project_route()
        if pid is None or action not in {
            "approve",
            "retry",
            "recover",
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
            elif action == "recover":
                self._drain_body()
                result = ctrl.recover_project(pid)
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
        path = urllib.parse.urlsplit(self.path).path
        ctrl = self.server.controller

        if path == "/api/ideas/settings":
            def _patch_ideas_settings() -> dict[str, Any]:
                body = self._read_json_body()
                if "auto_interval_hours" not in body:
                    raise ValueError("El campo 'auto_interval_hours' es obligatorio.")
                return ctrl.set_idea_radar_settings(body["auto_interval_hours"])

            self._send_controller_json(_patch_ideas_settings)
            return

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

        chat_pid, chat_entry_id, chat_subact = self._parse_project_chat_entry_route()
        if chat_pid is not None and chat_entry_id is not None and chat_subact is None:
            self._send_controller_json(
                lambda: ctrl.delete_project_chat_entry(chat_pid, chat_entry_id)
            )
            return

        raw_del_path = urllib.parse.urlsplit(self.path).path
        mem_prefix = "/api/factory/assistant/memory/"
        if raw_del_path.startswith(mem_prefix):
            raw_rid = urllib.parse.unquote(raw_del_path[len(mem_prefix) :])
            self._send_controller_json(lambda: ctrl.delete_factory_assistant_rule(raw_rid))
            return

        if raw_del_path.startswith("/api/ideas/"):
            def _delete_idea() -> dict[str, Any]:
                idea_id, idea_action = self._parse_idea_route()
                if idea_id is None or idea_action is not None:
                    raise ProjectNotFoundError("Endpoint not found")
                return ctrl.dismiss_improvement_idea(idea_id)

            self._send_controller_json(_delete_idea)
            return

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
    host: str = LOOPBACK_HOST,
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
    self_improver: FactorySelfImprover | None = None,
    staging_manager: StagingManager | None = None,
    autonomous_engine: idea_engine.AutonomousEngine | None = None,
    visual_qa_runner: Callable[..., Mapping[str, Any]] | None = None,
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
        self_improver=self_improver,
        staging_manager=staging_manager,
        autonomous_engine=autonomous_engine,
        visual_qa_runner=visual_qa_runner,
        secrets_to_redact=secrets_to_redact,
    )
    server = DashboardHTTPServer((host, port), controller)
    actual_port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever,
        name="agent-factory-dashboard",
        daemon=True,
    )
    thread.start()
    if "PYTEST_CURRENT_TEST" not in os.environ:
        def _auto_start_autonomous() -> None:
            try:
                db_path = controller._resolve_idea_db_path()
                raw_root = getattr(controller._self_improver, "repo_root", None)
                repo_root = Path(raw_root).resolve() if raw_root is not None else Path.cwd()
                try:
                    staging_mgr = controller._get_staging_manager()
                except Exception:  # noqa: BLE001
                    staging_mgr = None
                auto_st = idea_engine.get_autonomous_state(db_path=db_path)
                idea_engine.reconcile_and_investigate_queue(
                    db_path=db_path,
                    repo_root=repo_root,
                    staging_manager=staging_mgr,
                )
                if auto_st["autonomous_enabled"] and not server._closing.is_set():
                    controller._get_autonomous_engine().start()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Autonomous engine auto-start skipped: %s", exc)

        threading.Thread(
            target=_auto_start_autonomous,
            name="agent-factory-autonomous-autostart",
            daemon=True,
        ).start()
    return server, thread, f"http://{host}:{actual_port}"
