"""CLI entrypoint, fail-closed config, dependency wiring, and polling loop (INV-02, INV-04, INV-06, INV-19, INV-21)."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.adapters.telegram_client import TelegramHttpClient
from agent_factory.builder import BuildWorker
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.clock import Clock
from agent_factory.ports.code_agent import CodeAgent
from agent_factory.ports.llm import LLM
from agent_factory.ports.notifier import (
    HttpTransport,
    TelegramClient,
    TelegramTransportError,
)
from agent_factory.ports.publisher import RegistryPublisher
from agent_factory.ports.runner import Runner
from agent_factory.ports.store import EventStore
from agent_factory.ports.vcs import VCS
from agent_factory.scaffold import TemplateScaffold
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService
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


def _parse_id_whitelist(raw: str | None, var_name: str, secret_token: str = "") -> frozenset[int]:
    """Parse comma-separated integer IDs fail-closed without leaking secrets in errors (INV-19, INV-21)."""
    if raw is None or not raw.strip():
        raise CliConfigError(f"Missing required environment variable {var_name}")

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise CliConfigError(f"{var_name} must contain at least one integer ID")

    result: set[int] = set()
    for part in parts:
        try:
            val = int(part)
            result.add(val)
        except ValueError:
            clean_err = redact_secrets(
                f"Invalid integer '{part}' in {var_name}",
                extra_secrets=(secret_token,) if secret_token else (),
            )
            raise CliConfigError(clean_err) from None

    return frozenset(result)


def load_cli_config(env: Mapping[str, str] | None = None) -> CliConfig:
    """Load and validate CLI configuration fail-closed without leaking secrets in errors."""
    effective = os.environ if env is None else env

    token = effective.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise CliConfigError("Missing required environment variable TELEGRAM_BOT_TOKEN")

    allowed_chats = _parse_id_whitelist(
        effective.get("ALLOWED_CHAT_IDS"),
        "ALLOWED_CHAT_IDS",
        secret_token=token,
    )
    allowed_users = _parse_id_whitelist(
        effective.get("ALLOWED_USER_IDS"),
        "ALLOWED_USER_IDS",
        secret_token=token,
    )

    db_path = Path(effective.get("AGENT_FACTORY_DB_PATH", "data/agent_factory.db"))
    workspaces_dir = Path(effective.get("AGENT_FACTORY_WORKSPACES_DIR", "workspaces"))
    registry_repo = Path(effective.get("AGENT_FACTORY_REGISTRY_REPO", "."))
    registry_branch = effective.get("AGENT_FACTORY_REGISTRY_BRANCH", "registry").strip() or "registry"

    try:
        poll_timeout = int(effective.get("AGENT_FACTORY_POLL_TIMEOUT", "30"))
        if poll_timeout <= 0:
            raise CliConfigError("AGENT_FACTORY_POLL_TIMEOUT must be positive")
    except ValueError as exc:
        raise CliConfigError(f"Invalid AGENT_FACTORY_POLL_TIMEOUT: {exc}") from None

    try:
        backoff_initial = float(effective.get("AGENT_FACTORY_BACKOFF_INITIAL", "1.0"))
        backoff_max = float(effective.get("AGENT_FACTORY_BACKOFF_MAX", "60.0"))
        if backoff_initial <= 0 or backoff_max <= 0:
            raise CliConfigError("Backoff seconds must be positive")
        if backoff_initial > backoff_max:
            raise CliConfigError(
                f"AGENT_FACTORY_BACKOFF_INITIAL ({backoff_initial}) cannot exceed "
                f"AGENT_FACTORY_BACKOFF_MAX ({backoff_max})"
            )
    except ValueError as exc:
        raise CliConfigError(f"Invalid backoff value: {exc}") from None

    github_owner = effective.get("GITHUB_OWNER", "agent-factory").strip() or "agent-factory"
    deploy_target = effective.get("AGENT_FACTORY_DEPLOY_TARGET", "github-pages").strip() or "github-pages"
    github_token = effective.get("GITHUB_TOKEN", "").strip()
    vercel_token = effective.get("VERCEL_TOKEN", "").strip()
    llm_api_key = effective.get("LLM_API_KEY", "").strip()

    return CliConfig(
        telegram_bot_token=token,
        allowed_chat_ids=allowed_chats,
        allowed_user_ids=allowed_users,
        db_path=db_path,
        workspaces_dir=workspaces_dir,
        registry_repo_path=registry_repo,
        registry_branch=registry_branch,
        poll_timeout_seconds=poll_timeout,
        backoff_initial_seconds=backoff_initial,
        backoff_max_seconds=backoff_max,
        github_owner=github_owner,
        deploy_target=deploy_target,
        github_token=github_token,
        vercel_token=vercel_token,
        llm_api_key=llm_api_key,
    )


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
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.workspaces_dir.mkdir(parents=True, exist_ok=True)

    store = SqliteEventStore(db_path=config.db_path)
    effective_runner = runner if runner is not None else SubprocessRunner()
    effective_llm = llm if llm is not None else FakeLLM()
    orchestrator = Orchestrator(store=store, llm=effective_llm, clock=clock)

    workspace_manager = WorkspaceManager(base_dir=config.workspaces_dir)
    template_scaffold = TemplateScaffold(
        workspace_manager=workspace_manager,
        templates_root=config.templates_root,
    )

    effective_code_agent = (
        code_agent
        if code_agent is not None
        else AgyCliAdapter(workspace_manager=workspace_manager, runner=effective_runner)
    )

    effective_vcs = (
        vcs
        if vcs is not None
        else GitHubVCSAdapter(
            runner=effective_runner,
            owner=config.github_owner,
            github_token=config.github_token,
            secrets=(config.github_token,) if config.github_token else (),
        )
    )

    active_secrets = tuple(
        s for s in (config.telegram_bot_token, config.github_token, config.vercel_token) if s
    )

    effective_registry = (
        registry_publisher
        if registry_publisher is not None
        else GitRegistryPublisher(
            repo_path=config.registry_repo_path,
            branch=config.registry_branch,
            runner=effective_runner,
            secrets=active_secrets,
        )
    )

    build_worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=workspace_manager,
        code_agent=effective_code_agent,
        runner=effective_runner,
        vcs=effective_vcs,
        registry_publisher=effective_registry,
        clock=clock,
        secrets_to_redact=active_secrets,
    )

    effective_tg_client = (
        telegram_client
        if telegram_client is not None
        else TelegramHttpClient(
            bot_token=config.telegram_bot_token,
            transport=transport,
        )
    )

    bot_config = TelegramBotConfig(
        allowed_chat_ids=config.allowed_chat_ids,
        allowed_user_ids=config.allowed_user_ids,
        poll_timeout_seconds=config.poll_timeout_seconds,
        secrets_to_redact=active_secrets,
    )

    bot_service = TelegramBotService(
        client=effective_tg_client,
        orchestrator=orchestrator,
        config=bot_config,
        build_worker=build_worker,
        template_scaffold=template_scaffold,
        registry_publisher=effective_registry,
    )

    return RuntimeBundle(
        config=config,
        store=store,
        llm=effective_llm,
        orchestrator=orchestrator,
        workspace_manager=workspace_manager,
        template_scaffold=template_scaffold,
        code_agent=effective_code_agent,
        vcs=effective_vcs,
        registry_publisher=effective_registry,
        build_worker=build_worker,
        telegram_client=effective_tg_client,
        bot_service=bot_service,
    )


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Register SIGINT and SIGTERM handlers that trigger clean shutdown via stop_event."""
    def _handler(sig: int, frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


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
    current_backoff = initial_backoff_seconds
    iterations = 0
    event = stop_event if stop_event is not None else threading.Event()

    while not event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        try:
            bot_service.poll_once()
            current_backoff = initial_backoff_seconds
        except (TelegramTransportError, OSError) as exc:
            delay = current_backoff
            current_backoff = min(current_backoff * 2.0, max_backoff_seconds)
            if on_error is not None:
                on_error(exc, delay)
            if clock is not None:
                clock.sleep(delay)
            else:
                time.sleep(delay)

    return iterations


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Entrypoint for `python -m agent_factory`."""
    try:
        config = load_cli_config(env)
    except CliConfigError:
        return 1

    event = stop_event if stop_event is not None else threading.Event()
    install_signal_handlers(event)

    runtime = build_runtime(config)
    run_polling_loop(
        runtime.bot_service,
        stop_event=event,
        initial_backoff_seconds=config.backoff_initial_seconds,
        max_backoff_seconds=config.backoff_max_seconds,
    )
    return 0
