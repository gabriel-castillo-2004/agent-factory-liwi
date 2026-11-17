"""Telegram Long Polling bot service coordinating Orchestrator, BuildWorker, and RegistryPublisher (INV-04, INV-16, INV-21, INV-22)."""

from pydantic import BaseModel, Field

from agent_factory.builder import BuildWorker
from agent_factory.inbox import Inbox
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.notifier import TelegramClient, TelegramUpdate
from agent_factory.ports.publisher import RegistryPublisher
from agent_factory.scaffold import TemplateScaffold


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
        raise NotImplementedError

    def handle_update(self, update: TelegramUpdate) -> UpdateOutcome:
        """Validate ACL, deduplicate update_id, and dispatch commands, answers, or approval callbacks."""
        raise NotImplementedError

    def poll_once(self) -> list[UpdateOutcome]:
        """Fetch updates via outbound getUpdates(offset=next_offset), advance offset, and handle each update."""
        raise NotImplementedError
