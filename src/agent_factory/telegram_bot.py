"""Telegram Long Polling bot service coordinating Orchestrator, BuildWorker, and RegistryPublisher (INV-04, INV-16, INV-21, INV-22)."""

import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from agent_factory.builder import BuildWorker, JobState
from agent_factory.domain.events import EventType
from agent_factory.domain.models import ApprovalEvent, ProjectStatus, validate_project_id
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.inbox import Inbox
from agent_factory.orchestrator import ApprovalError, Orchestrator
from agent_factory.ports.notifier import (
    InlineButton,
    TelegramClient,
    TelegramTransportError,
    TelegramUpdate,
)
from agent_factory.ports.publisher import RegistryPublisher, RegistryPublishError
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import SandboxViolationError


def _slugify(text: str) -> str:
    """Derive a valid project_id slug from user idea text."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:50] or "app"


class TelegramBotConfig(BaseModel):
    """Strict access-control and polling configuration for TelegramBotService."""

    allowed_chat_ids: frozenset[int] = Field(default_factory=frozenset)
    allowed_user_ids: frozenset[int] = Field(default_factory=frozenset)
    poll_timeout_seconds: int = 30
    secrets_to_redact: tuple[str, ...] = ()


class UpdateOutcome(BaseModel):
    """Deterministic outcome record for a processed Telegram update."""

    update_id: int
    accepted: bool
    duplicate: bool = False
    command: str = ""
    project_id: str | None = None
    action: str = ""
    reply_text: str = ""


class TelegramBotService:
    """Outbound-only Telegram long polling controller routing commands, Q&A, approvals, /status, and /resume."""

    def __init__(
        self,
        *,
        client: TelegramClient,
        orchestrator: Orchestrator,
        config: TelegramBotConfig,
        build_worker: BuildWorker | None = None,
        template_scaffold: TemplateScaffold | None = None,
        registry_publisher: RegistryPublisher | None = None,
        inbox: Inbox | None = None,
    ) -> None:
        self.client = client
        self.orchestrator = orchestrator
        self.config = config
        self.build_worker = build_worker
        self.template_scaffold = template_scaffold
        self.registry_publisher = registry_publisher
        self.inbox = inbox if inbox is not None else Inbox()
        self.next_offset: int | None = None
        self.active_project_by_chat: dict[int, str] = {}
        self._hydrate_from_store()

    def _hydrate_from_store(self) -> None:
        """Restore persisted Telegram update deduplication and chat routing from EventStore."""
        store = self.orchestrator.store
        if hasattr(store, "get_processed_telegram_updates"):
            for uid in store.get_processed_telegram_updates():
                self.inbox.mark_processed(f"tg-update-{uid}")

        if hasattr(store, "get_telegram_active_projects"):
            for chat_id, pid in store.get_telegram_active_projects().items():
                self.active_project_by_chat[int(chat_id)] = pid

        for pid in store.list_project_ids():
            for ev in store.get_events(pid):
                incoming = ev.payload.get("incoming_event_id")
                if isinstance(incoming, str) and incoming.startswith("tg-msg-"):
                    raw_id = incoming[len("tg-msg-") :]
                    if raw_id.lstrip("-").isdigit():
                        self.inbox.mark_processed(f"tg-update-{raw_id}")

    def _record_processed_update(self, update_id: int) -> None:
        """Persist processed Telegram update_id in Inbox and backing store."""
        self.inbox.mark_processed(f"tg-update-{update_id}")
        store = self.orchestrator.store
        if hasattr(store, "record_telegram_update"):
            store.record_telegram_update(update_id)

    def _set_active_project(self, chat_id: int, project_id: str) -> None:
        """Update and persist the active project for chat_id."""
        self.active_project_by_chat[chat_id] = project_id
        store = self.orchestrator.store
        if hasattr(store, "set_telegram_active_project"):
            store.set_telegram_active_project(chat_id, project_id)

    def is_authorized(self, chat_id: int, user_id: int) -> bool:
        """Return True iff both chat_id and user_id match non-empty whitelists (fail-closed, INV-21)."""
        if not self.config.allowed_chat_ids or not self.config.allowed_user_ids:
            return False
        return (
            chat_id in self.config.allowed_chat_ids
            and user_id in self.config.allowed_user_ids
        )

    def _send_reply(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
    ) -> str:
        """Send message via client after redacting secrets and return sanitized text."""
        clean_text = redact_secrets(text, extra_secrets=self.config.secrets_to_redact)
        self.client.send_message(chat_id=chat_id, text=clean_text, buttons=buttons)
        return clean_text

    def _publish_status_to_registry(self, pid: str) -> None:
        """Best-effort publish current project status to registry_publisher if configured."""
        if self.registry_publisher is not None:
            from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher

            is_real_git = isinstance(self.registry_publisher, GitRegistryPublisher)
            orch_has_pub = getattr(self.orchestrator, "publisher", None) is not None
            if not is_real_git or orch_has_pub:
                try:
                    st = self.orchestrator.get_status(pid)
                    self.registry_publisher.publish_project(st)
                except (RegistryPublishError, OSError, RuntimeError, ValueError) as pub_exc:
                    clean_pub_err = redact_secrets(
                        str(pub_exc), extra_secrets=self.config.secrets_to_redact
                    )
                    _ = clean_pub_err

    def _error_outcome(
        self,
        update: TelegramUpdate,
        message: str,
        *,
        command: str = "",
        project_id: str | None = None,
        action: str = "error",
    ) -> UpdateOutcome:
        """Send a sanitized error reply and construct a uniform UpdateOutcome."""
        reply = self._send_reply(update.chat_id, message)
        return UpdateOutcome(
            update_id=update.update_id,
            accepted=True,
            command=command,
            project_id=project_id,
            action=action,
            reply_text=reply,
        )

    def _validate_existing_project(
        self,
        update: TelegramUpdate,
        pid: str | None,
        *,
        command: str = "",
        missing_prompt: str = "No se indicó un project_id ni hay un proyecto activo en este chat.",
    ) -> tuple[ProjectStatus | None, UpdateOutcome | None]:
        """Validate project_id format and verify project existence in EventStore."""
        if not pid:
            return None, self._error_outcome(update, missing_prompt, command=command)

        try:
            validate_project_id(pid)
        except ValueError as exc:
            return None, self._error_outcome(
                update, f"project_id inválido: {exc}", command=command, project_id=pid
            )

        if not self.orchestrator.store.get_events(pid):
            return None, self._error_outcome(
                update, f"El proyecto '{pid}' no existe.", command=command, project_id=pid
            )

        return self.orchestrator.get_status(pid), None

    def _handle_interview_state(
        self,
        update: TelegramUpdate,
        pid: str,
        command: str = "",
    ) -> UpdateOutcome:
        """Check latest INTERVIEW_ROUND outcome and ask questions or run debate."""
        status = self.orchestrator.get_status(pid)
        if status.state == ProjectState.FAILED:
            reason = status.failure_reason or "La entrevista falló."
            return self._error_outcome(
                update,
                f"El proyecto {pid} falló en entrevista: {reason}",
                command=command,
                project_id=pid,
            )

        if status.state != ProjectState.INTERVIEW:
            return self._error_outcome(
                update,
                f"El proyecto {pid} se encuentra en estado {status.state.value}.",
                command=command,
                project_id=pid,
                action="ignored",
            )

        events = self.orchestrator.store.get_events(pid)
        last_round = next(
            (ev for ev in reversed(events) if ev.type == EventType.INTERVIEW_ROUND),
            None,
        )
        questions = last_round.payload.get("questions", []) if last_round else []
        if questions:
            self._publish_status_to_registry(pid)
            q_lines = "\n".join(f"- {q}" for q in questions)
            reply = self._send_reply(
                update.chat_id,
                f"Preguntas para completar la especificación:\n{q_lines}",
            )
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                command=command,
                project_id=pid,
                action="interview_question_asked",
                reply_text=reply,
            )

        try:
            waiting_status = self.orchestrator.run_debate(pid)
        except (ValueError, IllegalTransition, OSError, RuntimeError) as exc:
            return self._error_outcome(
                update,
                f"Error generando plan para {pid}: {exc}",
                command=command,
                project_id=pid,
            )

        if waiting_status.state == ProjectState.FAILED:
            reason = waiting_status.failure_reason or "Error durante el debate."
            return self._error_outcome(
                update,
                f"El proyecto {pid} falló durante el debate: {reason}",
                command=command,
                project_id=pid,
            )

        plan = self.orchestrator.get_plan(pid)
        plan_summary = plan.summary if plan else pid
        msg_text = (
            f"Plan listo para aprobación:\n{plan_summary}\n\n"
            f"Progreso: {waiting_status.progress_pct}%"
        )
        buttons = [
            [
                InlineButton(
                    text="Aprobar",
                    callback_data=f"approve:{pid}:{waiting_status.plan_hash}",
                ),
                InlineButton(
                    text="Ajustar",
                    callback_data=f"adjust:{pid}:{waiting_status.plan_hash}",
                ),
            ]
        ]
        self._publish_status_to_registry(pid)
        reply = self._send_reply(update.chat_id, msg_text, buttons=buttons)
        return UpdateOutcome(
            update_id=update.update_id,
            accepted=True,
            command=command,
            project_id=pid,
            action="plan_ready_for_approval",
            reply_text=reply,
        )

    def _handle_callback(self, update: TelegramUpdate) -> UpdateOutcome:
        """Process inline keyboard callbacks (approve / adjust) enforcing plan_hash and state guards."""
        if update.callback_query_id:
            self.client.answer_callback_query(update.callback_query_id)

        parts = (update.callback_data or "").split(":", 2)
        cb_action = parts[0] if len(parts) > 0 else ""
        pid = parts[1] if len(parts) > 1 else ""
        expected_hash = parts[2] if len(parts) > 2 else ""

        if cb_action not in {"approve", "adjust"}:
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                project_id=pid or None,
                action="unknown_callback",
            )

        current_status, err_outcome = self._validate_existing_project(update, pid)
        if err_outcome is not None or current_status is None:
            assert err_outcome is not None
            return err_outcome

        if not current_status.plan_hash or current_status.plan_hash != expected_hash:
            reject_action = "approval_rejected" if cb_action == "approve" else "adjustment_rejected"
            label = "Aprobación" if cb_action == "approve" else "Ajuste"
            return self._error_outcome(
                update,
                f"{label} rechazada: el hash del plan no coincide con {current_status.plan_hash}.",
                project_id=pid,
                action=reject_action,
            )

        if cb_action == "adjust":
            try:
                self.orchestrator.request_adjustment(
                    pid, reason="User requested adjustment via Telegram callback"
                )
            except (IllegalTransition, ValueError) as exc:
                return self._error_outcome(
                    update,
                    f"No se pudo solicitar ajuste para {pid}: {exc}",
                    project_id=pid,
                    action="adjustment_rejected",
                )
            self._set_active_project(update.chat_id, pid)
            self._publish_status_to_registry(pid)
            msg = self._send_reply(
                update.chat_id,
                f"Plan ajustado para {pid}. ¿Qué modificaciones deseas agregar a la especificación?",
            )
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                project_id=pid,
                action="adjustment_requested",
                reply_text=msg,
            )

        # cb_action == "approve"
        now = (
            self.orchestrator._now()
            if hasattr(self.orchestrator, "_now")
            else datetime.now(UTC)
        )
        try:
            self.orchestrator.submit_approval(
                pid,
                ApprovalEvent(
                    plan_hash=expected_hash,
                    timestamp=now,
                    decision="approved",
                ),
            )
        except (ApprovalError, IllegalTransition, ValueError) as exc:
            return self._error_outcome(
                update,
                f"No se pudo aprobar {pid}: {exc}",
                project_id=pid,
                action="approval_rejected",
            )

        reply_msg = ""
        if self.build_worker is not None:
            try:
                if self.template_scaffold is not None:
                    plan = self.orchestrator.get_plan(pid)
                    template_id = plan.template_id if plan else "static-web"
                    self.template_scaffold.scaffold(pid, template_id)

                self.build_worker.enqueue_approved_project(pid)
                report = self.build_worker.run_job(pid)
            except (
                SandboxViolationError,
                IllegalTransition,
                ValueError,
                OSError,
                RuntimeError,
            ) as exc:
                return self._error_outcome(
                    update,
                    f"Error ejecutando build de {pid}: {exc}",
                    project_id=pid,
                )

            final_status = self.orchestrator.get_status(pid)
            if final_status.state == ProjectState.DELIVERED:
                repo_url = final_status.links.repo if final_status.links else ""
                deploy_url = final_status.links.deploy if final_status.links else ""
                text = f"Proyecto {pid} entregado con éxito!\nRepo: {repo_url}\nDeploy: {deploy_url}"
                if report.failure_cause:
                    text += f"\nAviso: {report.failure_cause}"
                reply_msg = self._send_reply(update.chat_id, text)
            elif final_status.state == ProjectState.FAILED:
                cause = (
                    final_status.failure_reason
                    or (report.failure_cause if report else "Error de construcción")
                )
                reply_msg = self._send_reply(
                    update.chat_id,
                    f"Proyecto {pid} falló durante el build: {cause}",
                )

        return UpdateOutcome(
            update_id=update.update_id,
            accepted=True,
            project_id=pid,
            action="approval_processed",
            reply_text=reply_msg,
        )

    def _handle_new(self, update: TelegramUpdate, raw_text: str) -> UpdateOutcome:
        """Handle /new <idea> command cleanly without overwriting active project on errors."""
        idea = raw_text[4:].strip()
        if not idea:
            return self._error_outcome(
                update,
                "Por favor incluye una idea para el proyecto: /new <idea>",
                command="/new",
            )

        pid = _slugify(idea)
        if self.orchestrator.store.get_events(pid):
            return self._error_outcome(
                update,
                f"Ya existe un proyecto con identificador '{pid}'. Usa otra descripción.",
                command="/new",
                project_id=pid,
            )

        try:
            self.orchestrator.create_project(pid, name=idea, objective=idea)
            self.orchestrator.handle_interview_message(
                pid, f"tg-msg-{update.update_id}", idea
            )
        except (ValueError, IllegalTransition) as exc:
            return self._error_outcome(
                update,
                f"No se pudo crear '{pid}': {exc}",
                command="/new",
                project_id=pid,
            )

        outcome = self._handle_interview_state(update, pid, command="/new")
        if outcome.action != "error":
            self._set_active_project(update.chat_id, pid)
        return outcome

    def _handle_status(self, update: TelegramUpdate, raw_text: str) -> UpdateOutcome:
        """Handle /status [project_id] or /estado [project_id]."""
        parts = raw_text.split(None, 1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        pid = arg or self.active_project_by_chat.get(update.chat_id)

        status, err_outcome = self._validate_existing_project(update, pid, command=cmd)
        if err_outcome is not None or status is None:
            assert err_outcome is not None
            return err_outcome

        lines = [
            f"Estado de {status.project_id}:",
            f"Estado: {status.state.value}",
            f"Progreso: {status.progress_pct}%",
        ]
        if status.links and status.links.repo:
            lines.append(f"Repo: {status.links.repo}")
        if status.links and status.links.deploy:
            lines.append(f"Deploy: {status.links.deploy}")
        if status.failure_reason:
            lines.append(f"Fallo: {status.failure_reason}")

        msg = self._send_reply(update.chat_id, "\n".join(lines))
        return UpdateOutcome(
            update_id=update.update_id,
            accepted=True,
            command=cmd,
            project_id=status.project_id,
            action="status_reported",
            reply_text=msg,
        )

    def _handle_resume(self, update: TelegramUpdate, raw_text: str) -> UpdateOutcome:
        """Handle /resume [project_id] only for jobs paused in JobState.PAUSED_QUOTA."""
        parts = raw_text.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        pid = arg or self.active_project_by_chat.get(update.chat_id)

        if not pid:
            return self._error_outcome(
                update,
                "No se indicó un project_id ni hay un proyecto activo para reanudar.",
                command="/resume",
                action="resume_rejected",
            )

        try:
            validate_project_id(pid)
        except ValueError as exc:
            return self._error_outcome(
                update,
                f"project_id inválido: {exc}",
                command="/resume",
                project_id=pid,
                action="resume_rejected",
            )

        if self.build_worker is not None and self.build_worker.queue is not None:
            job = self.build_worker.queue.get_job(pid)
            if job is not None and job.state == JobState.COMPLETED:
                return self._error_outcome(
                    update,
                    f"El proyecto {pid} ya se encuentra completado.",
                    command="/resume",
                    project_id=pid,
                    action="resume_rejected",
                )
            if job is not None and job.state == JobState.PAUSED_QUOTA:
                self.build_worker.run_job(pid)
                st = self.orchestrator.get_status(pid)
                res_lines = [
                    f"Proyecto {pid} reanudado.",
                    f"Estado: {st.state.value}",
                    f"Progreso: {st.progress_pct}%",
                ]
                if st.links and st.links.repo:
                    res_lines.append(f"Repo: {st.links.repo}")
                if st.links and st.links.deploy:
                    res_lines.append(f"Deploy: {st.links.deploy}")
                msg = self._send_reply(update.chat_id, "\n".join(res_lines))
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command="/resume",
                    project_id=pid,
                    action="resumed",
                    reply_text=msg,
                )

        return self._error_outcome(
            update,
            f"El proyecto {pid} no se encuentra en estado de pausa por cuota.",
            command="/resume",
            project_id=pid,
            action="resume_rejected",
        )

    def handle_update(self, update: TelegramUpdate) -> UpdateOutcome:
        """Validate ACL, deduplicate update_id, and dispatch commands, answers, or approval callbacks."""
        self.next_offset = max(self.next_offset or 0, update.update_id + 1)

        if not self.is_authorized(update.chat_id, update.user_id):
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=False,
                action="rejected_unauthorized",
            )

        dedup_key = f"tg-update-{update.update_id}"
        if self.inbox.is_processed(dedup_key):
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                duplicate=True,
                action="ignored_duplicate",
            )
        self._record_processed_update(update.update_id)

        if update.callback_data:
            return self._handle_callback(update)

        raw_text = (update.text or "").strip()
        first_token = raw_text.split(None, 1)[0].lower() if raw_text else ""
        if first_token == "/new":
            return self._handle_new(update, raw_text)
        if first_token in {"/status", "/estado"}:
            return self._handle_status(update, raw_text)
        if first_token == "/resume":
            return self._handle_resume(update, raw_text)

        pid = self.active_project_by_chat.get(update.chat_id)
        if not pid:
            return self._error_outcome(
                update,
                "No hay un proyecto activo en este chat. Inicia uno con /new <idea>.",
                action="ignored",
            )

        status = self.orchestrator.get_status(pid)
        if status.state == ProjectState.INTERVIEW:
            self.orchestrator.handle_interview_message(
                pid, f"tg-msg-{update.update_id}", raw_text
            )
            return self._handle_interview_state(update, pid)

        return self._error_outcome(
            update,
            f"El proyecto {pid} se encuentra en estado {status.state.value}.",
            project_id=pid,
            action="ignored",
        )

    def poll_once(self) -> list[UpdateOutcome]:
        """Fetch updates via outbound getUpdates(offset=next_offset), advance offset, and handle each update."""
        updates = self.client.get_updates(
            offset=self.next_offset,
            timeout=self.config.poll_timeout_seconds,
        )
        outcomes: list[UpdateOutcome] = []
        for update in updates:
            try:
                outcome = self.handle_update(update)
            except (
                ValueError,
                IllegalTransition,
                ApprovalError,
                SandboxViolationError,
                OSError,
                RuntimeError,
            ) as exc:
                if self.is_authorized(update.chat_id, update.user_id):
                    try:
                        outcome = self._error_outcome(update, f"Error procesando actualización: {exc}")
                    except (TelegramTransportError, OSError, RuntimeError):
                        clean_msg = redact_secrets(
                            f"Error procesando actualización: {exc}",
                            extra_secrets=self.config.secrets_to_redact,
                        )
                        outcome = UpdateOutcome(
                            update_id=update.update_id,
                            accepted=False,
                            action="error",
                            reply_text=clean_msg,
                        )
                else:
                    outcome = UpdateOutcome(
                        update_id=update.update_id,
                        accepted=False,
                        action="rejected_unauthorized",
                    )
            outcomes.append(outcome)
        return outcomes
