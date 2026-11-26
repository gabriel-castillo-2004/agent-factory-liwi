"""Port definition for outbound Telegram Long Polling communication (INV-02, INV-04, ADR-003)."""

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agent_factory.config import normalize_telegram_notification_level


class NotificationCategory(str, Enum):
    """Classification of automatic Telegram notifications for level-based filtering."""

    FINISHED = "finished"
    NEEDS_ATTENTION = "needs_attention"
    DECISION = "decision"
    PROGRESS = "progress"


CRITICAL_NOTIFICATION_CATEGORIES: frozenset[NotificationCategory] = frozenset(
    {
        NotificationCategory.FINISHED,
        NotificationCategory.NEEDS_ATTENTION,
        NotificationCategory.DECISION,
    }
)


def normalize_notification_category(category: NotificationCategory | str) -> NotificationCategory:
    """Coerce a category enum or string into NotificationCategory (defaulting to PROGRESS)."""
    if isinstance(category, NotificationCategory):
        return category
    raw = str(category).strip().lower()
    for member in NotificationCategory:
        if raw == member.value:
            return member
    return NotificationCategory.PROGRESS


def resolve_notification_delivery(
    level: str,
    category: NotificationCategory | str,
) -> tuple[bool, bool]:
    """Return (should_send, disable_notification) according to notification level policy."""
    norm_level = normalize_telegram_notification_level(level)
    norm_category = normalize_notification_category(category)
    is_critical = norm_category in CRITICAL_NOTIFICATION_CATEGORIES

    if norm_level == "silenciado":
        return (False, True)
    if norm_level == "esencial":
        if is_critical:
            return (True, False)
        return (False, True)
    # normal
    if is_critical:
        return (True, False)
    return (True, True)


class TelegramTransportError(RuntimeError):
    """Raised on Telegram API or network failure with all sensitive tokens redacted (INV-19, AC-3.5)."""


class InlineButton(BaseModel):
    """Telegram inline keyboard button definition."""

    text: str
    callback_data: str


class TelegramUpdate(BaseModel):
    """Normalized incoming update from Telegram getUpdates long polling."""

    update_id: int
    chat_id: int
    user_id: int
    text: str = ""
    callback_data: str | None = None
    callback_query_id: str | None = None
    username: str = ""


class SentMessage(BaseModel):
    """Record of an outgoing message sent via Telegram sendMessage."""

    message_id: int
    chat_id: int
    text: str
    buttons: list[list[InlineButton]] = Field(default_factory=list)
    parse_mode: str | None = None
    disable_notification: bool = False
    skipped: bool = False
    category: NotificationCategory | None = None


class HttpResponse(BaseModel):
    """Minimal HTTP response container for outbound API clients."""

    status_code: int
    body: str
    headers: dict[str, str] = Field(default_factory=dict)
    final_url: str = ""


@runtime_checkable
class HttpTransport(Protocol):
    """Protocol for outbound-only HTTP JSON POST requests (zero listening servers, INV-04)."""

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
        headers: Mapping[str, str] | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> HttpResponse:
        """Send an outbound HTTP POST with JSON payload and return HttpResponse."""
        raise NotImplementedError


@runtime_checkable
class TelegramClient(Protocol):
    """Protocol for outbound Telegram Bot API long polling and messaging."""

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[TelegramUpdate]:
        """Poll Telegram getUpdates for pending messages/callbacks starting at offset."""
        raise NotImplementedError

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> SentMessage:
        """Send a text message with optional inline buttons to chat_id."""
        raise NotImplementedError

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Acknowledge a Telegram inline keyboard callback_query."""
        raise NotImplementedError
