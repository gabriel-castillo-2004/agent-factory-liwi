"""Outbound-only Telegram Bot API long polling client (INV-02, INV-04, INV-19, ADR-003)."""

from collections.abc import Mapping, Sequence
from typing import Any

from agent_factory.ports.notifier import (
    HttpResponse,
    HttpTransport,
    InlineButton,
    SentMessage,
    TelegramUpdate,
)


class UrllibHttpTransport:
    """Standard-library outbound HTTP JSON POST transport (no server sockets, no extra deps)."""

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
    ) -> HttpResponse:
        """Execute outbound HTTPS POST using urllib.request."""
        raise NotImplementedError


class TelegramHttpClient:
    """Telegram Bot API adapter using outbound getUpdates/sendMessage only (no webhooks, INV-04)."""

    def __init__(
        self,
        bot_token: str,
        transport: HttpTransport | None = None,
        api_base_url: str = "https://api.telegram.org",
    ) -> None:
        self.bot_token = bot_token
        self.transport: HttpTransport = (
            transport if transport is not None else UrllibHttpTransport()
        )
        self.api_base_url = api_base_url.rstrip("/")

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[TelegramUpdate]:
        """Call Telegram getUpdates API via outbound HttpTransport."""
        raise NotImplementedError

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
    ) -> SentMessage:
        """Call Telegram sendMessage API with optional inline keyboard."""
        raise NotImplementedError

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Call Telegram answerCallbackQuery API."""
        raise NotImplementedError
