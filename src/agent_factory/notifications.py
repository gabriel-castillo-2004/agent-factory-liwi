"""Clean Spanish Telegram notification templates and level-aware dispatcher (Step 1, INV-19)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agent_factory.config import (
    build_dashboard_url,
    normalize_dashboard_public_url,
    normalize_telegram_notification_level,
    parse_dashboard_public_url,
    parse_telegram_notification_level,
)
from agent_factory.ports.notifier import (
    CRITICAL_NOTIFICATION_CATEGORIES,
    InlineButton,
    NotificationCategory,
    SentMessage,
    TelegramClient,
    TelegramTransportError,
    normalize_notification_category,
    resolve_notification_delivery,
)
from agent_factory.secrets import redact_secrets

_DEFAULT_MAX_CAUSE_CHARS: int = 180
_DEFAULT_MAX_SUMMARY_CHARS: int = 180


def _condense_text(
    text: str,
    *,
    max_chars: int = _DEFAULT_MAX_CAUSE_CHARS,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Redact secrets, drop noisy traceback frames, and collapse text to a brief scannable line."""
    raw = redact_secrets(str(text or ""), extra_secrets=extra_secrets).strip()
    if not raw:
        return ""

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""

    is_traceback = any(
        ln.startswith(("Traceback (most recent call last):", 'File "'))
        for ln in lines
    )
    if is_traceback:
        non_frame_lines = [
            ln
            for ln in lines
            if not ln.startswith(("Traceback (most recent call last):", 'File "'))
        ]
        chosen = non_frame_lines[-1] if non_frame_lines else lines[-1]
    else:
        chosen = lines[0]

    collapsed = " ".join(chosen.split()).strip()
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 1].rstrip() + "…"
    return collapsed


def format_finished_notification(
    project_id: str,
    *,
    summary: str = "",
    repo_url: str = "",
    deploy_url: str = "",
    warning: str = "",
    dashboard_url: str | None = None,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a clean critical notification when a project finishes delivery."""
    link = build_dashboard_url(dashboard_url, project_id=project_id)
    lines = [f"✅ ¡Terminado! · {project_id}"]
    clean_summary = _condense_text(summary, max_chars=_DEFAULT_MAX_SUMMARY_CHARS, extra_secrets=extra_secrets)
    if clean_summary:
        lines.append(clean_summary)
    if repo_url:
        lines.append(f"Repo: {redact_secrets(repo_url.strip(), extra_secrets=extra_secrets)}")
    if deploy_url:
        lines.append(f"Deploy: {redact_secrets(deploy_url.strip(), extra_secrets=extra_secrets)}")
    clean_warning = _condense_text(warning, max_chars=_DEFAULT_MAX_CAUSE_CHARS, extra_secrets=extra_secrets)
    if clean_warning:
        lines.append(f"Aviso: {clean_warning}")
    lines.append(f"Panel: {link}")
    return "\n".join(lines)


def format_needs_attention_notification(
    project_id: str,
    *,
    cause: str = "",
    stage: str = "",
    dashboard_url: str | None = None,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a clean critical alert without log dumps when human attention is needed."""
    link = build_dashboard_url(dashboard_url, project_id=project_id)
    lines = [f"⚠️ Necesita atención · {project_id}"]
    clean_stage = str(stage or "").strip()
    if clean_stage:
        lines.append(f"Etapa: {clean_stage}")
    clean_cause = _condense_text(cause, max_chars=_DEFAULT_MAX_CAUSE_CHARS, extra_secrets=extra_secrets)
    if clean_cause:
        lines.append(f"Causa: {clean_cause}")
    lines.append(f"Panel: {link}")
    return "\n".join(lines)


def format_decision_notification(
    project_id: str,
    *,
    prompt: str = "",
    action_expected: str = "",
    progress_pct: int | None = None,
    dashboard_url: str | None = None,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a clean critical prompt requesting a human decision (plan approval or questions)."""
    link = build_dashboard_url(dashboard_url, project_id=project_id)
    lines = [f"🧭 Decisión requerida · {project_id}"]
    clean_prompt = redact_secrets(str(prompt or "").strip(), extra_secrets=extra_secrets)
    if clean_prompt:
        lines.append(clean_prompt)
    clean_action = _condense_text(
        action_expected,
        max_chars=_DEFAULT_MAX_SUMMARY_CHARS,
        extra_secrets=extra_secrets,
    )
    if clean_action:
        lines.append(f"Acción: {clean_action}")
    if progress_pct is not None:
        lines.append(f"Progreso: {int(progress_pct)}%")
    lines.append(f"Panel: {link}")
    return "\n".join(lines)


def format_progress_notification(
    project_id: str,
    *,
    state: str,
    summary: str = "",
    progress_pct: int | None = None,
    dashboard_url: str | None = None,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a brief intermediate progress notification (suppressed in esencial, silent in normal)."""
    link = build_dashboard_url(dashboard_url, project_id=project_id)
    state_label = str(state or "").strip() or "EN_PROGRESO"
    progress_suffix = f" ({int(progress_pct)}%)" if progress_pct is not None else ""
    lines = [
        f"🔄 Avance · {project_id}",
        f"Estado: {state_label}{progress_suffix}",
    ]
    clean_summary = _condense_text(summary, max_chars=_DEFAULT_MAX_SUMMARY_CHARS, extra_secrets=extra_secrets)
    if clean_summary:
        lines.append(clean_summary)
    lines.append(f"Panel: {link}")
    return "\n".join(lines)


class NotificationDispatcher:
    """Best-effort, level-filtered Telegram notification sender with clean templates and dashboard links."""

    def __init__(
        self,
        *,
        client: TelegramClient | None,
        chat_ids: Sequence[int] = (),
        chat_id_resolver: Callable[[str], int | None] | None = None,
        notification_level: str | None = None,
        dashboard_public_url: str | None = None,
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.chat_ids: tuple[int, ...] = tuple(int(c) for c in chat_ids)
        self.chat_id_resolver = chat_id_resolver
        self._notification_level: str | None = (
            normalize_telegram_notification_level(notification_level)
            if notification_level is not None
            else None
        )
        self._dashboard_public_url: str | None = (
            normalize_dashboard_public_url(dashboard_public_url)
            if dashboard_public_url is not None
            else None
        )
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)

    def effective_level(self) -> str:
        """Return configured notification level or read TELEGRAM_NOTIFICATION_LEVEL from env."""
        if self._notification_level is not None:
            return self._notification_level
        return parse_telegram_notification_level()

    def effective_dashboard_url(self) -> str:
        """Return configured dashboard URL or read DASHBOARD_PUBLIC_URL from env."""
        if self._dashboard_public_url is not None:
            return self._dashboard_public_url
        return parse_dashboard_public_url()

    def _resolve_targets(self, project_id: str, chat_id: int | None) -> list[int]:
        if chat_id is not None:
            return [int(chat_id)]
        if self.chat_id_resolver is not None:
            resolved = self.chat_id_resolver(project_id)
            if resolved is not None:
                return [int(resolved)]
        return list(self.chat_ids)

    def dispatch(
        self,
        chat_id: int,
        text: str,
        *,
        category: NotificationCategory | str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage | None:
        """Apply level policy and send a single secret-redacted notification best-effort."""
        if self.client is None:
            return None
        norm_cat = normalize_notification_category(category)
        should_send, disable_notification = resolve_notification_delivery(
            self.effective_level(),
            norm_cat,
        )
        if not should_send:
            return None

        clean_text = redact_secrets(text, extra_secrets=self.secrets_to_redact)
        try:
            try:
                return self.client.send_message(
                    chat_id=chat_id,
                    text=clean_text,
                    buttons=buttons,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification,
                )
            except TypeError as type_err:
                if "disable_notification" in str(type_err) or "unexpected keyword argument" in str(type_err):
                    return self.client.send_message(
                        chat_id=chat_id,
                        text=clean_text,
                        buttons=buttons,
                        parse_mode=parse_mode,
                    )
                raise
        except (TelegramTransportError, OSError, RuntimeError, ValueError):
            return None

    def _send_to_targets(
        self,
        project_id: str,
        text: str,
        *,
        category: NotificationCategory,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        chat_id: int | None = None,
    ) -> list[SentMessage]:
        sent: list[SentMessage] = []
        for target_chat in self._resolve_targets(project_id, chat_id):
            msg = self.dispatch(
                target_chat,
                text,
                category=category,
                buttons=buttons,
            )
            if msg is not None and not msg.skipped:
                sent.append(msg)
        return sent

    def notify_finished(
        self,
        project_id: str,
        *,
        summary: str = "",
        repo_url: str = "",
        deploy_url: str = "",
        warning: str = "",
        chat_id: int | None = None,
    ) -> list[SentMessage]:
        """Send critical '¡Terminado!' notification with sound and dashboard link."""
        text = format_finished_notification(
            project_id,
            summary=summary,
            repo_url=repo_url,
            deploy_url=deploy_url,
            warning=warning,
            dashboard_url=self.effective_dashboard_url(),
            extra_secrets=self.secrets_to_redact,
        )
        return self._send_to_targets(
            project_id,
            text,
            category=NotificationCategory.FINISHED,
            chat_id=chat_id,
        )

    def notify_needs_attention(
        self,
        project_id: str,
        *,
        cause: str = "",
        stage: str = "",
        chat_id: int | None = None,
    ) -> list[SentMessage]:
        """Send critical 'Necesita atención' alert with sound and concise cause."""
        text = format_needs_attention_notification(
            project_id,
            cause=cause,
            stage=stage,
            dashboard_url=self.effective_dashboard_url(),
            extra_secrets=self.secrets_to_redact,
        )
        return self._send_to_targets(
            project_id,
            text,
            category=NotificationCategory.NEEDS_ATTENTION,
            chat_id=chat_id,
        )

    def notify_decision(
        self,
        project_id: str,
        *,
        prompt: str = "",
        action_expected: str = "",
        progress_pct: int | None = None,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        chat_id: int | None = None,
    ) -> list[SentMessage]:
        """Send critical decision request notification with sound and dashboard link."""
        text = format_decision_notification(
            project_id,
            prompt=prompt,
            action_expected=action_expected,
            progress_pct=progress_pct,
            dashboard_url=self.effective_dashboard_url(),
            extra_secrets=self.secrets_to_redact,
        )
        return self._send_to_targets(
            project_id,
            text,
            category=NotificationCategory.DECISION,
            buttons=buttons,
            chat_id=chat_id,
        )

    def notify_progress(
        self,
        project_id: str,
        *,
        state: str,
        summary: str = "",
        progress_pct: int | None = None,
        chat_id: int | None = None,
    ) -> list[SentMessage]:
        """Send intermediate progress notification (skipped in esencial, silent in normal)."""
        text = format_progress_notification(
            project_id,
            state=state,
            summary=summary,
            progress_pct=progress_pct,
            dashboard_url=self.effective_dashboard_url(),
            extra_secrets=self.secrets_to_redact,
        )
        return self._send_to_targets(
            project_id,
            text,
            category=NotificationCategory.PROGRESS,
            chat_id=chat_id,
        )


__all__ = [
    "CRITICAL_NOTIFICATION_CATEGORIES",
    "NotificationCategory",
    "NotificationDispatcher",
    "format_decision_notification",
    "format_finished_notification",
    "format_needs_attention_notification",
    "format_progress_notification",
    "resolve_notification_delivery",
]
