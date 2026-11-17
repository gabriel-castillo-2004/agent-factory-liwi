"""Telegram Long Polling bot service coordinating Orchestrator, BuildWorker, and RegistryPublisher (INV-04, INV-16, INV-21, INV-22)."""

import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from agent_factory.builder import BuildWorker, JobState
from agent_factory.domain.events import EventType
from agent_factory.domain.models import ApprovalEvent, validate_project_id
from agent_factory.domain.states import ProjectState
from agent_factory.inbox import Inbox
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.notifier import InlineButton, TelegramClient, TelegramUpdate
from agent_factory.ports.publisher import RegistryPublisher
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets


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

    def _handle_interview_state(
        self,
        update: TelegramUpdate,
        pid: str,
        command: str = "",
    ) -> UpdateOutcome:
        """Check latest INTERVIEW_ROUND outcome and either ask questions or transition to debate."""
        events = self.orchestrator.store.get_events(pid)
        last_round = None
        for ev in reversed(events):
            if ev.type == EventType.INTERVIEW_ROUND:
                last_round = ev
                break

        questions = last_round.payload.get("questions", []) if last_round else []
        if questions:
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

        waiting_status = self.orchestrator.run_debate(pid)
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
        reply = self._send_reply(update.chat_id, msg_text, buttons=buttons)
        return UpdateOutcome(
            update_id=update.update_id,
            accepted=True,
            command=command,
            project_id=pid,
            action="plan_ready_for_approval",
            reply_text=reply,
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
        self.inbox.mark_processed(dedup_key)

        # 1. Handle Inline Keyboard Callback Queries
        if update.callback_data:
            if update.callback_query_id:
                self.client.answer_callback_query(update.callback_query_id)

            parts = update.callback_data.split(":")
            cb_action = parts[0] if len(parts) > 0 else ""
            pid = parts[1] if len(parts) > 1 else ""
            expected_hash = parts[2] if len(parts) > 2 else ""

            if cb_action == "approve":
                current_status = self.orchestrator.get_status(pid)
                if not current_status.plan_hash or current_status.plan_hash != expected_hash:
                    msg = self._send_reply(
                        update.chat_id,
                        f"Aprobación rechazada: el hash del plan no coincide con {current_status.plan_hash}.",
                    )
                    return UpdateOutcome(
                        update_id=update.update_id,
                        accepted=True,
                        project_id=pid,
                        action="approval_rejected",
                        reply_text=msg,
                    )

                now = (
                    self.orchestrator._now()
                    if hasattr(self.orchestrator, "_now")
                    else datetime.now(UTC)
                )
                self.orchestrator.submit_approval(
                    pid,
                    ApprovalEvent(
                        plan_hash=expected_hash,
                        timestamp=now,
                        decision="approved",
                    ),
                )

                reply_msg = ""
                if self.build_worker is not None:
                    if self.template_scaffold is not None:
                        plan = self.orchestrator.get_plan(pid)
                        template_id = plan.template_id if plan else "static-web"
                        self.template_scaffold.scaffold(pid, template_id)

                    self.build_worker.enqueue_approved_project(pid)
                    report = self.build_worker.run_job(pid)
                    final_status = self.orchestrator.get_status(pid)
                    if final_status.state == ProjectState.DELIVERED:
                        repo_url = final_status.links.repo if final_status.links else ""
                        deploy_url = final_status.links.deploy if final_status.links else ""
                        reply_msg = self._send_reply(
                            update.chat_id,
                            f"Proyecto {pid} entregado con éxito!\nRepo: {repo_url}\nDeploy: {deploy_url}",
                        )
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

            if cb_action == "adjust":
                self.orchestrator.request_adjustment(
                    pid, reason="User requested adjustment via Telegram callback"
                )
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

            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                project_id=pid,
                action="unknown_callback",
            )

        # 2. Handle Text Commands and Messages
        raw_text = (update.text or "").strip()

        if raw_text.startswith("/new"):
            idea = raw_text[4:].strip()
            if not idea:
                msg = self._send_reply(
                    update.chat_id,
                    "Por favor incluye una idea para el proyecto: /new <idea>",
                )
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command="/new",
                    action="error",
                    reply_text=msg,
                )

            pid = _slugify(idea)
            self.active_project_by_chat[update.chat_id] = pid
            self.orchestrator.create_project(pid, name=idea, objective=idea)
            self.orchestrator.handle_interview_message(
                pid, f"tg-msg-{update.update_id}", idea
            )
            return self._handle_interview_state(update, pid, command="/new")

        if raw_text.startswith(("/status", "/estado")):
            parts = raw_text.split(None, 1)
            cmd = parts[0]
            arg = parts[1].strip() if len(parts) > 1 else ""
            pid = arg or self.active_project_by_chat.get(update.chat_id)

            if not pid:
                msg = self._send_reply(
                    update.chat_id,
                    "No se indicó un project_id ni hay un proyecto activo en este chat.",
                )
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command=cmd,
                    action="error",
                    reply_text=msg,
                )

            try:
                validate_project_id(pid)
            except ValueError as exc:
                msg = self._send_reply(
                    update.chat_id,
                    f"project_id inválido: {exc}",
                )
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command=cmd,
                    project_id=pid,
                    action="error",
                    reply_text=msg,
                )

            events = self.orchestrator.store.get_events(pid)
            if not events:
                msg = self._send_reply(
                    update.chat_id,
                    f"El proyecto '{pid}' no existe.",
                )
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command=cmd,
                    project_id=pid,
                    action="error",
                    reply_text=msg,
                )

            status = self.orchestrator.get_status(pid)

            lines = [
                f"Estado de {pid}:",
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
                project_id=pid,
                action="status_reported",
                reply_text=msg,
            )

        if raw_text.startswith("/resume"):
            parts = raw_text.split(None, 1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            pid = arg or self.active_project_by_chat.get(update.chat_id)

            if not pid:
                msg = self._send_reply(
                    update.chat_id,
                    "No se indicó un project_id ni hay un proyecto activo para reanudar.",
                )
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command="/resume",
                    action="resume_rejected",
                    reply_text=msg,
                )

            try:
                validate_project_id(pid)
            except ValueError as exc:
                msg = self._send_reply(
                    update.chat_id,
                    f"project_id inválido: {exc}",
                )
                return UpdateOutcome(
                    update_id=update.update_id,
                    accepted=True,
                    command="/resume",
                    project_id=pid,
                    action="resume_rejected",
                    reply_text=msg,
                )

            if self.build_worker is not None and self.build_worker.queue is not None:
                job = self.build_worker.queue.get_job(pid)
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

            msg = self._send_reply(
                update.chat_id,
                f"El proyecto {pid} no se encuentra en estado de pausa por cuota.",
            )
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                command="/resume",
                project_id=pid,
                action="resume_rejected",
                reply_text=msg,
            )

        # Plain text answers to interview
        pid = self.active_project_by_chat.get(update.chat_id)
        if not pid:
            msg = self._send_reply(
                update.chat_id,
                "No hay un proyecto activo en este chat. Inicia uno con /new <idea>.",
            )
            return UpdateOutcome(
                update_id=update.update_id,
                accepted=True,
                action="ignored",
                reply_text=msg,
            )

        status = self.orchestrator.get_status(pid)
        if status.state == ProjectState.INTERVIEW:
            self.orchestrator.handle_interview_message(
                pid, f"tg-msg-{update.update_id}", raw_text
            )
            return self._handle_interview_state(update, pid)

        msg = self._send_reply(
            update.chat_id,
            f"El proyecto {pid} se encuentra en estado {status.state.value}.",
        )
        return UpdateOutcome(
            update_id=update.update_id,
            accepted=True,
            project_id=pid,
            action="ignored",
            reply_text=msg,
        )

    def poll_once(self) -> list[UpdateOutcome]:
        """Fetch updates via outbound getUpdates(offset=next_offset), advance offset, and handle each update."""
        updates = self.client.get_updates(
            offset=self.next_offset,
            timeout=self.config.poll_timeout_seconds,
        )
        outcomes: list[UpdateOutcome] = []
        for update in updates:
            outcome = self.handle_update(update)
            outcomes.append(outcome)
        return outcomes
