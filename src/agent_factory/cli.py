"""CLI entrypoint, fail-closed config, dependency wiring, and polling loop (INV-02, INV-04, INV-06, INV-19, INV-21)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
    from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
    from agent_factory.builder import BuildWorker
    from agent_factory.orchestrator import Orchestrator
    from agent_factory.ports.clock import Clock
    from agent_factory.ports.code_agent import CodeAgent
    from agent_factory.ports.llm import LLM
    from agent_factory.ports.notifier import HttpTransport, TelegramClient
    from agent_factory.ports.publisher import RegistryPublisher
    from agent_factory.ports.runner import Runner
    from agent_factory.ports.store import EventStore
    from agent_factory.ports.vcs import VCS
    from agent_factory.scaffold import TemplateScaffold
    from agent_factory.telegram_bot import TelegramBotService
    from agent_factory.workspace import WorkspaceManager


class CliConfigError(ValueError):
    """Raised when required CLI configuration or whitelists are missing or invalid (INV-19, INV-21)."""


class CliConfig(BaseModel):
    """Fail-closed runtime configuration loaded from environment variables."""

    telegram_bot_token: str
    allowed_chat_ids: frozenset[int] = Field(default_factory=frozenset)
    allowed_user_ids: frozenset[int] = Field(default_factory=frozenset)
    db_path: Path = Path("data/agent_factory.db")
    workspaces_dir: Path = Path("workspaces")
    registry_repo_path: Path = Path(".")
    registry_branch: str = "registry"
    poll_timeout_seconds: int = 30
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    models_config_path: Path = Path("config/models.toml")
    limits_config_path: Path = Path("config/limits.toml")
    templates_root: Path = Path("templates")
    github_owner: str = "agent-factory"
    deploy_target: str = "github-pages"
    github_token: str = ""
    vercel_token: str = ""
    llm_api_key: str = ""


class RuntimeBundle(BaseModel):
    """Assembled dependency container for offline tests and production CLI execution."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: CliConfig
    store: EventStore
    llm: LLM
    orchestrator: Orchestrator
    workspace_manager: WorkspaceManager
    template_scaffold: TemplateScaffold
    code_agent: CodeAgent | AgyCliAdapter
    vcs: VCS
    registry_publisher: RegistryPublisher | GitRegistryPublisher
    build_worker: BuildWorker
    telegram_client: TelegramClient
    bot_service: TelegramBotService


def load_cli_config(env: Mapping[str, str] | None = None) -> CliConfig:
    """Load and validate CLI configuration fail-closed without leaking secrets in errors."""
    raise NotImplementedError("load_cli_config is not implemented yet (PASO B)")


def build_runtime(
    config: CliConfig,
    *,
    llm: LLM | None = None,
    runner: Runner | None = None,
    transport: HttpTransport | None = None,
    telegram_client: TelegramClient | None = None,
    vcs: VCS | None = None,
    registry_publisher: RegistryPublisher | None = None,
    code_agent: CodeAgent | None = None,
    clock: Clock | None = None,
) -> RuntimeBundle:
    """Assemble SqliteEventStore, adapters, Orchestrator, BuildWorker, and TelegramBotService."""
    raise NotImplementedError("build_runtime is not implemented yet (PASO B)")


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Register SIGINT and SIGTERM handlers that trigger clean shutdown via stop_event."""
    raise NotImplementedError("install_signal_handlers is not implemented yet (PASO B)")


def run_polling_loop(
    bot_service: TelegramBotService,
    *,
    clock: Clock | None = None,
    stop_event: threading.Event | None = None,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 60.0,
    max_iterations: int | None = None,
    on_error: Callable[[Exception, float], None] | None = None,
) -> int:
    """Execute Telegram long-polling loop with bounded exponential backoff and clean shutdown."""
    raise NotImplementedError("run_polling_loop is not implemented yet (PASO B)")


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Entrypoint for `python -m agent_factory`."""
    raise NotImplementedError("main is not implemented yet (PASO B)")
