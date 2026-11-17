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
        raise NotImplementedError

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[TelegramUpdate]:
        """Return queued updates matching offset >= update_id and record the poll call."""
        raise NotImplementedError

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
    ) -> SentMessage:
        """Record an outgoing Telegram message and return SentMessage."""
        raise NotImplementedError

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Record callback acknowledgment."""
        raise NotImplementedError
