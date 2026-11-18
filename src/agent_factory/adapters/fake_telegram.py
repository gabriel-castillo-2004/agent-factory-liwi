"""Offline scripted fake TelegramClient for deterministic testing (INV-02, INV-03)."""

from collections.abc import Sequence
from typing import Any

from agent_factory.ports.notifier import InlineButton, SentMessage, TelegramUpdate


class FakeTelegramClient:
    """In-memory TelegramClient recording outbound calls and delivering queued updates."""

    def __init__(self, updates: Sequence[TelegramUpdate | Exception] | None = None) -> None:
        self._queued_updates: list[TelegramUpdate | Exception] = list(updates or [])
        self.get_updates_calls: list[dict[str, Any]] = []
        self.sent_messages: list[SentMessage] = []
        self.answered_callbacks: list[dict[str, str]] = []
        self._next_message_id: int = 1

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
    ) -> SentMessage:
        """Record an outgoing Telegram message and return SentMessage."""
        msg = SentMessage(
            message_id=self._next_message_id,
            chat_id=chat_id,
            text=text,
            buttons=[list(row) for row in (buttons or [])],
            parse_mode=parse_mode,
        )
        self._next_message_id += 1
        self.sent_messages.append(msg)
        return msg

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Record callback acknowledgment."""
        self.answered_callbacks.append(
            {"callback_query_id": callback_query_id, "text": text}
        )
