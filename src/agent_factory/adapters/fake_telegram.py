"""Offline scripted fake TelegramClient for deterministic testing (INV-02, INV-03)."""

from collections.abc import Sequence
from typing import Any

from agent_factory.config import (
    normalize_telegram_notification_level,
    parse_telegram_notification_level,
)
from agent_factory.ports.notifier import (
    InlineButton,
    NotificationCategory,
    SentMessage,
    TelegramUpdate,
    normalize_notification_category,
    resolve_notification_delivery,
)


class FakeTelegramClient:
    """In-memory TelegramClient recording outbound calls and delivering queued updates."""

    def __init__(
        self,
        updates: Sequence[TelegramUpdate | Exception] | None = None,
        *,
        notification_level: str | None = None,
    ) -> None:
        self._queued_updates: list[TelegramUpdate | Exception] = list(updates or [])
        self.get_updates_calls: list[dict[str, Any]] = []
        self.sent_messages: list[SentMessage] = []
        self.suppressed_messages: list[SentMessage] = []
        self.answered_callbacks: list[dict[str, str]] = []
        self._next_message_id: int = 1
        self.notification_level: str | None = (
            normalize_telegram_notification_level(notification_level)
            if notification_level is not None
            else None
        )

    def effective_notification_level(self, override: str | None = None) -> str:
        """Resolve active notification level from explicit override, instance config, or env."""
        if override is not None:
            return normalize_telegram_notification_level(override)
        if self.notification_level is not None:
            return self.notification_level
        return parse_telegram_notification_level()

    def enqueue_update(self, update: TelegramUpdate | Exception) -> None:
        """Append an update or exception to the inbound long-polling queue."""
        self._queued_updates.append(update)

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[TelegramUpdate]:
        """Return queued updates matching offset >= update_id and record the poll call."""
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        if not self._queued_updates:
            return []

        if isinstance(self._queued_updates[0], Exception):
            raise self._queued_updates.pop(0)

        # Discard acknowledged updates: update_id < offset
        if offset is not None:
            self._queued_updates = [
                item
                for item in self._queued_updates
                if isinstance(item, Exception) or item.update_id >= offset
            ]

        matching: list[TelegramUpdate] = []
        remaining: list[TelegramUpdate | Exception] = []
        for item in self._queued_updates:
            if isinstance(item, TelegramUpdate):
                matching.append(item)
            else:
                remaining.append(item)

        self._queued_updates = remaining
        return matching

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
        *,
        category: NotificationCategory | str | None = None,
        notification_level: str | None = None,
    ) -> SentMessage:
        """Record an outgoing Telegram message (respecting notification level if categorized)."""
        norm_category: NotificationCategory | None = None
        effective_disable = bool(disable_notification)
        if category is not None:
            norm_category = normalize_notification_category(category)
            should_send, policy_disable = resolve_notification_delivery(
                self.effective_notification_level(notification_level),
                norm_category,
            )
            if not should_send:
                suppressed = SentMessage(
                    message_id=0,
                    chat_id=chat_id,
                    text=text,
                    buttons=[list(row) for row in (buttons or [])],
                    parse_mode=parse_mode,
                    disable_notification=True,
                    skipped=True,
                    category=norm_category,
                )
                self.suppressed_messages.append(suppressed)
                return suppressed
            effective_disable = bool(disable_notification or policy_disable)

        msg = SentMessage(
            message_id=self._next_message_id,
            chat_id=chat_id,
            text=text,
            buttons=[list(row) for row in (buttons or [])],
            parse_mode=parse_mode,
            disable_notification=effective_disable,
            skipped=False,
            category=norm_category,
        )
        self._next_message_id += 1
        self.sent_messages.append(msg)
        return msg

    def send_notification(
        self,
        chat_id: int,
        text: str,
        *,
        category: NotificationCategory | str = NotificationCategory.PROGRESS,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        parse_mode: str | None = None,
        notification_level: str | None = None,
    ) -> SentMessage | None:
        """Send an automatic categorized notification applying level policy (returns None when suppressed)."""
        msg = self.send_message(
            chat_id=chat_id,
            text=text,
            buttons=buttons,
            parse_mode=parse_mode,
            category=category,
            notification_level=notification_level,
        )
        return None if msg.skipped else msg

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Record callback acknowledgment."""
        self.answered_callbacks.append(
            {"callback_query_id": callback_query_id, "text": text}
        )
