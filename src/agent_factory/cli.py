"""CLI entrypoint, fail-closed config, dependency wiring, and polling loop (INV-02, INV-04, INV-06, INV-19, INV-21)."""

from __future__ import annotations

import http.client
import json
import math
import os
import signal
import sys
import threading
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.adapters.telegram_client import TelegramHttpClient, UrllibHttpTransport
from agent_factory.builder import BuildWorker
from agent_factory.orchestrator import CircuitBreaker, Orchestrator
from agent_factory.ports.clock import Clock
from agent_factory.ports.code_agent import CodeAgent
from agent_factory.ports.llm import LLM, LLMError
from agent_factory.ports.notifier import (
    HttpResponse,
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


class ProductionLLMAdapter:
    """Production LLM adapter executing model completions behind the LLM port (INV-02, INV-09)."""

    def __init__(
        self,
        api_key: str,
        *,
        models_config_path: Path | None = None,
        transport: HttpTransport | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.api_key = api_key
        self.models_config_path = models_config_path
        self.transport = transport
        combined = list(secrets)
        if api_key and api_key not in combined:
            combined.append(api_key)
        self.secrets = tuple(combined)

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute completion request over HTTP transport."""
        if self.transport is None:
            raise LLMError("No HttpTransport configured for ProductionLLMAdapter")

        endpoint = "https://api.openai.com/v1/chat/completions"
        chosen_model = "model-fast-01"

        if self.models_config_path and self.models_config_path.is_file():
            try:
                with self.models_config_path.open("rb") as f:
                    cfg_data = tomllib.load(f)
                if "endpoint" in cfg_data:
                    endpoint = str(cfg_data["endpoint"])
                chosen_model = str(
                    cfg_data.get("INTERVIEW")
                    or cfg_data.get("IDLE")
                    or cfg_data.get("FAST")
                    or "model-fast-01"
                )
            except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
                endpoint = "https://api.openai.com/v1/chat/completions"
                chosen_model = "model-fast-01"

        payload = {
            "model": chosen_model,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        err_to_raise: LLMError | None = None
        resp: HttpResponse | None = None
        try:
            resp = self.transport.post_json(
                endpoint,
                payload,
                timeout_seconds=35.0,
                headers=headers,
            )
        except (http.client.HTTPException, OSError, RuntimeError, ValueError) as exc:
            clean_msg = redact_secrets(
                f"LLM transport error: {exc}",
                extra_secrets=self.secrets,
            )
            err_to_raise = LLMError(clean_msg)

        if err_to_raise is not None:
            err_to_raise.__cause__ = None
            err_to_raise.__context__ = None
            raise err_to_raise

        if resp is None or resp.status_code != 200:
            status_code = resp.status_code if resp else "unknown"
            raw_body = resp.body if resp else ""
            clean_body = redact_secrets(
                f"LLM API error HTTP {status_code}: {raw_body}",
                extra_secrets=self.secrets,
            )
            err = LLMError(clean_body)
            err.__cause__ = None
            err.__context__ = None
            raise err

        try:
            data = json.loads(resp.body)
            choices = data.get("choices", [])
            if not choices:
                return {}
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                return {"content": content}
            if isinstance(content, dict):
                return content
            return {"content": str(content)}
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            clean_msg = redact_secrets(
                f"Failed to parse LLM response: {exc}",
                extra_secrets=self.secrets,
            )
            err_to_raise = LLMError(clean_msg)

        if err_to_raise is not None:
            err_to_raise.__cause__ = None
            err_to_raise.__context__ = None
            raise err_to_raise

        return {}


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
    err_to_raise: CliConfigError | None = None
    for part in parts:
        try:
            val = int(part)
            result.add(val)
        except ValueError:
            clean_err = redact_secrets(
                f"Invalid integer '{part}' in {var_name}",
                extra_secrets=(secret_token,) if secret_token else (),
            )
            err_to_raise = CliConfigError(clean_err)
            break

    if err_to_raise is not None:
        err_to_raise.__cause__ = None
        err_to_raise.__context__ = None
        raise err_to_raise

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

    err_poll: CliConfigError | None = None
    poll_timeout = 30
    try:
        raw_poll = effective.get("AGENT_FACTORY_POLL_TIMEOUT", "30")
        poll_timeout = int(raw_poll)
        if poll_timeout <= 0:
            err_poll = CliConfigError("AGENT_FACTORY_POLL_TIMEOUT must be positive")
    except ValueError as exc:
        clean_err = redact_secrets(
            f"Invalid AGENT_FACTORY_POLL_TIMEOUT: {exc}",
            extra_secrets=(token,) if token else (),
        )
        err_poll = CliConfigError(clean_err)

    if err_poll is not None:
        err_poll.__cause__ = None
        err_poll.__context__ = None
        raise err_poll

    err_backoff: CliConfigError | None = None
    backoff_initial = 1.0
    backoff_max = 60.0
    try:
        raw_initial = effective.get("AGENT_FACTORY_BACKOFF_INITIAL", "1.0")
        raw_max = effective.get("AGENT_FACTORY_BACKOFF_MAX", "60.0")
        backoff_initial = float(raw_initial)
        backoff_max = float(raw_max)
        if not math.isfinite(backoff_initial) or backoff_initial <= 0:
            err_backoff = CliConfigError("AGENT_FACTORY_BACKOFF_INITIAL must be a finite positive number")
        elif not math.isfinite(backoff_max) or backoff_max <= 0:
            err_backoff = CliConfigError("AGENT_FACTORY_BACKOFF_MAX must be a finite positive number")
        elif backoff_initial > backoff_max:
            err_backoff = CliConfigError(
                f"AGENT_FACTORY_BACKOFF_INITIAL ({backoff_initial}) cannot exceed "
                f"AGENT_FACTORY_BACKOFF_MAX ({backoff_max})"
            )
    except ValueError as exc:
        clean_err = redact_secrets(
            f"Invalid backoff value: {exc}",
            extra_secrets=(token,) if token else (),
        )
        err_backoff = CliConfigError(clean_err)

    if err_backoff is not None:
        err_backoff.__cause__ = None
        err_backoff.__context__ = None
        raise err_backoff

    models_config = Path(effective.get("AGENT_FACTORY_MODELS_CONFIG", "config/models.toml"))
    limits_config = Path(effective.get("AGENT_FACTORY_LIMITS_CONFIG", "config/limits.toml"))
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
        models_config_path=models_config,
        limits_config_path=limits_config,
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
    if not config.limits_config_path.is_file():
        raise CliConfigError(
            f"Limits configuration file not found: {config.limits_config_path}"
        )

    if llm is None:
        if not config.llm_api_key:
            raise CliConfigError("Missing required environment variable LLM_API_KEY")

        if not config.models_config_path.is_file():
            raise CliConfigError(
                f"Models configuration file not found: {config.models_config_path}"
            )

        try:
            with config.models_config_path.open("rb") as f:
                models_data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            err = CliConfigError(f"Failed to parse models configuration: {exc}")
            err.__cause__ = None
            err.__context__ = None
            raise err

        for state_key, model_val in models_data.items():
            if str(model_val).strip() == "CONFIGURE_ME":
                raise CliConfigError(
                    f"Models configuration contains unconfigured placeholder 'CONFIGURE_ME' for {state_key}"
                )

    active_secrets = tuple(
        s for s in (config.telegram_bot_token, config.github_token, config.vercel_token, config.llm_api_key) if s
    )

    effective_transport = transport if transport is not None else UrllibHttpTransport()

    if llm is not None:
        effective_llm = llm
    else:
        effective_llm = ProductionLLMAdapter(
            api_key=config.llm_api_key,
            models_config_path=config.models_config_path,
            transport=effective_transport,
            secrets=active_secrets,
        )

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.workspaces_dir.mkdir(parents=True, exist_ok=True)

    store = SqliteEventStore(db_path=config.db_path)
    try:
        effective_runner = runner if runner is not None else SubprocessRunner()
        circuit_breaker = CircuitBreaker(config_path=config.limits_config_path)
        orchestrator = Orchestrator(
            store=store,
            llm=effective_llm,
            clock=clock,
            circuit_breaker=circuit_breaker,
        )

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

        deploy_publisher: StaticDeployPublisher | None = None
        if config.deploy_target or config.vercel_token:
            deploy_publisher = StaticDeployPublisher(
                runner=effective_runner,
                target=config.deploy_target,
                deploy_token=config.vercel_token,
                secrets=active_secrets,
            )

        effective_vcs = (
            vcs
            if vcs is not None
            else GitHubVCSAdapter(
                runner=effective_runner,
                owner=config.github_owner,
                github_token=config.github_token,
                secrets=active_secrets,
                deploy_publisher=deploy_publisher,
            )
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
                transport=effective_transport,
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
    except Exception:
        store.close()
        raise


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
                event.wait(delay)

    return iterations


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Entrypoint for `python -m agent_factory`."""
    effective_env = os.environ if env is None else env
    token = effective_env.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        config = load_cli_config(env)
    except CliConfigError as err:
        clean_err = redact_secrets(str(err), extra_secrets=(token,) if token else ())
        sys.stderr.write(f"Configuration error: {clean_err}\n")
        return 1

    event = stop_event if stop_event is not None else threading.Event()
    install_signal_handlers(event)

    try:
        runtime = build_runtime(config)
    except CliConfigError as err:
        clean_err = redact_secrets(str(err), extra_secrets=(token,) if token else ())
        sys.stderr.write(f"Runtime configuration error: {clean_err}\n")
        return 1

    try:
        run_polling_loop(
            runtime.bot_service,
            stop_event=event,
            initial_backoff_seconds=config.backoff_initial_seconds,
            max_backoff_seconds=config.backoff_max_seconds,
        )
    finally:
        runtime.store.close()
    return 0
