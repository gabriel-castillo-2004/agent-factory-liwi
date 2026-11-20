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
from agent_factory.adapters.agy_llm_adapter import AgyLLMAdapter, FallbackLLMAdapter
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
from agent_factory.adapters.model_resolver import (
    ModelResolver,
    read_configured_model_for_stage,
)
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.adapters.telegram_client import TelegramHttpClient, UrllibHttpTransport
from agent_factory.builder import BuildWorker
from agent_factory.dashboard import DashboardHTTPServer, start_dashboard_server
from agent_factory.orchestrator import CircuitBreaker, Orchestrator, load_limits
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
from agent_factory.runtime_runner import LocalAppRunner
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
        chosen_model = ""

        if self.models_config_path is None:
            raise LLMError("No models configuration path provided for ProductionLLMAdapter")

        if not self.models_config_path.is_file():
            raise LLMError(f"Models config file not found: {self.models_config_path}")
        try:
            with self.models_config_path.open("rb") as f:
                cfg_data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
            clean_err = redact_secrets(
                f"Failed to load models config from {self.models_config_path}: {exc}",
                extra_secrets=self.secrets,
            )
            err = LLMError(clean_err)
            err.__cause__ = None
            err.__context__ = None
            raise err

        if not isinstance(cfg_data, dict) or not cfg_data:
            raise LLMError(f"Models config at {self.models_config_path} is empty or invalid")

        if "endpoint" in cfg_data:
            endpoint = str(cfg_data["endpoint"])

        models_section = cfg_data.get("models")
        if isinstance(models_section, dict):
            models_cfg = models_section
        else:
            models_cfg = cfg_data

        if not models_cfg:
            raise LLMError(f"No models configured in {self.models_config_path}")

        # State routing: MEDIATION (strong) vs INTERVIEW/DEBATE (fast)
        is_mediation = False
        if schema is not None:
            schema_name = getattr(schema, "__name__", str(schema))
            if "Plan" in schema_name:
                is_mediation = True

        for msg in messages:
            content = msg.get("content", "")
            if "Mediator" in content or "mediation" in content.lower():
                is_mediation = True
                break

        if is_mediation:
            chosen_model = str(
                models_cfg.get("strong")
                or models_cfg.get("MEDIATION")
                or models_cfg.get("mediation")
                or models_cfg.get("STRONG")
                or ""
            )
        else:
            chosen_model = str(
                models_cfg.get("fast")
                or models_cfg.get("INTERVIEW")
                or models_cfg.get("DEBATE")
                or models_cfg.get("FAST")
                or models_cfg.get("idle")
                or ""
            )

        if not chosen_model:
            fallback_keys = ("fast", "strong") if not is_mediation else ("strong", "fast")
            for k in fallback_keys:
                if k in models_cfg:
                    chosen_model = str(models_cfg[k])
                    break
            if not chosen_model and models_cfg:
                chosen_model = str(next(iter(models_cfg.values())))

        if not chosen_model:
            raise LLMError("Could not resolve model from models configuration")

        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": list(messages),
        }
        if schema is not None:
            schema_dict = None
            if hasattr(schema, "model_json_schema"):
                schema_dict = schema.model_json_schema()
            elif isinstance(schema, dict):
                schema_dict = schema

            payload["response_format"] = {
                "type": "json_object",
                **({"schema": schema_dict} if schema_dict else {}),
            }
            if schema_dict:
                payload["response_schema"] = schema_dict

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
                if schema is not None:
                    raise LLMError("Empty choices from LLM response when schema expected")
                return {}
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, ValueError) as json_err:
                    if schema is not None:
                        clean_err = redact_secrets(
                            f"Expected JSON object for schema {schema}, got non-JSON text: {json_err}",
                            extra_secrets=self.secrets,
                        )
                        raise LLMError(clean_err)
                if schema is not None:
                    raise LLMError(f"Expected JSON dict for schema {schema}, got: {type(content)}")
                return {"content": content}
            if isinstance(content, dict):
                return content
            if schema is not None:
                raise LLMError(f"Expected JSON dict for schema {schema}, got: {type(content)}")
            return {"content": str(content)}
        except LLMError as l_err:
            l_err.__cause__ = None
            l_err.__context__ = None
            raise
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
    llm_provider: str = "auto"
    llm_fallback_to_agy: bool = True
    agy_cli_timeout_seconds: float = 300.0
    dashboard_port: int = 8000
    lan_mode: bool = False


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
    model_resolver: ModelResolver | None = None
    local_runner: LocalAppRunner | None = None


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


def load_dotenv_into_environ(env_path: Path | str | None = None) -> None:
    """Load key-value pairs from .env into os.environ without overwriting existing variables."""
    target_path = Path(env_path) if env_path is not None else Path(".env")
    if not target_path.is_file():
        return

    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        os.environ.setdefault(key, val)


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

    raw_provider = effective.get("LLM_PROVIDER", "auto").strip().lower() or "auto"
    if raw_provider not in {"auto", "api", "agy"}:
        clean_err = redact_secrets(
            f"Invalid LLM_PROVIDER '{raw_provider}': expected auto, api, or agy",
            extra_secrets=(token,) if token else (),
        )
        err_provider = CliConfigError(clean_err)
        err_provider.__cause__ = None
        err_provider.__context__ = None
        raise err_provider

    raw_fallback = effective.get("LLM_FALLBACK_TO_AGY", "true").strip().lower() or "true"
    if raw_fallback in {"1", "true", "yes", "on"}:
        llm_fallback_to_agy = True
    elif raw_fallback in {"0", "false", "no", "off"}:
        llm_fallback_to_agy = False
    else:
        clean_err = redact_secrets(
            f"Invalid LLM_FALLBACK_TO_AGY '{raw_fallback}': expected boolean",
            extra_secrets=(token,) if token else (),
        )
        err_fb = CliConfigError(clean_err)
        err_fb.__cause__ = None
        err_fb.__context__ = None
        raise err_fb

    agy_cli_timeout_seconds = 300.0
    err_agy_timeout: CliConfigError | None = None
    try:
        raw_agy_timeout = effective.get("AGY_CLI_TIMEOUT_SECONDS", "300.0").strip() or "300.0"
        agy_cli_timeout_seconds = float(raw_agy_timeout)
        if not math.isfinite(agy_cli_timeout_seconds) or agy_cli_timeout_seconds <= 0:
            err_agy_timeout = CliConfigError(
                "AGY_CLI_TIMEOUT_SECONDS must be a finite positive number"
            )
    except ValueError as exc:
        clean_err = redact_secrets(
            f"Invalid AGY_CLI_TIMEOUT_SECONDS: {exc}",
            extra_secrets=(token,) if token else (),
        )
        err_agy_timeout = CliConfigError(clean_err)
    if err_agy_timeout is not None:
        err_agy_timeout.__cause__ = None
        err_agy_timeout.__context__ = None
        raise err_agy_timeout

    dashboard_port = 8000
    err_port: CliConfigError | None = None
    try:
        raw_port = effective.get("DASHBOARD_PORT", "8000").strip() or "8000"
        dashboard_port = int(raw_port)
        if dashboard_port < 0 or dashboard_port > 65535:
            err_port = CliConfigError("DASHBOARD_PORT must be between 0 and 65535")
    except ValueError as exc:
        clean_err = redact_secrets(
            f"Invalid DASHBOARD_PORT: {exc}",
            extra_secrets=(token,) if token else (),
        )
        err_port = CliConfigError(clean_err)
    if err_port is not None:
        err_port.__cause__ = None
        err_port.__context__ = None
        raise err_port

    raw_lan_mode = effective.get("AGENT_FACTORY_LAN_MODE", "false").strip().lower() or "false"
    lan_mode = raw_lan_mode in {"1", "true", "yes", "on"}

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
        llm_provider=raw_provider,
        llm_fallback_to_agy=llm_fallback_to_agy,
        agy_cli_timeout_seconds=agy_cli_timeout_seconds,
        dashboard_port=dashboard_port,
        lan_mode=lan_mode,
    )


def _validate_models_toml(models_config_path: Path) -> None:
    """Validate that models.toml exists, contains valid model mappings, and has no placeholders."""
    if not models_config_path.is_file():
        raise CliConfigError(f"Models configuration file not found: {models_config_path}")

    try:
        with models_config_path.open("rb") as f:
            models_data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        err = CliConfigError(f"Failed to parse models configuration: {exc}")
        err.__cause__ = None
        err.__context__ = None
        raise err

    if not isinstance(models_data, dict) or not models_data:
        raise CliConfigError(f"Models configuration file is empty or invalid: {models_config_path}")

    models_section = models_data.get("models")
    if isinstance(models_section, dict):
        if not models_section:
            raise CliConfigError("models.toml missing required non-empty [models] section")
        models_cfg = models_section
    else:
        known_keys = {
            "fast",
            "strong",
            "interview",
            "debate",
            "mediation",
            "building",
            "waiting_approval",
            "verifying",
            "delivered",
            "failed",
            "idle",
            "change_request",
        }
        if not any(k.lower() in known_keys for k in models_data):
            raise CliConfigError(
                "models.toml missing required non-empty [models] section or model keys"
            )
        models_cfg = models_data

    for state_key, model_val in models_cfg.items():
        if str(model_val).strip().upper() == "CONFIGURE_ME":
            raise CliConfigError(
                f"Models configuration contains unconfigured placeholder 'CONFIGURE_ME' for {state_key}"
            )

    for state_key, model_val in models_data.items():
        if str(model_val).strip().upper() == "CONFIGURE_ME":
            raise CliConfigError(
                f"Models configuration contains unconfigured placeholder 'CONFIGURE_ME' for {state_key}"
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
    model_resolver: ModelResolver | None = None,
) -> RuntimeBundle:
    """Assemble SqliteEventStore, adapters, Orchestrator, BuildWorker, and TelegramBotService."""
    if not config.limits_config_path.is_file():
        raise CliConfigError(
            f"Limits configuration file not found: {config.limits_config_path}"
        )

    active_secrets = tuple(
        s for s in (config.telegram_bot_token, config.github_token, config.vercel_token, config.llm_api_key) if s
    )

    effective_transport = transport if transport is not None else UrllibHttpTransport()
    effective_runner = runner if runner is not None else SubprocessRunner()
    shared_model_resolver = (
        model_resolver
        if model_resolver is not None
        else ModelResolver(
            runner=effective_runner,
            cwd=config.workspaces_dir,
            models_config_path=config.models_config_path,
            secrets=active_secrets,
        )
    )

    if llm is not None:
        effective_llm = llm
    else:
        provider = (config.llm_provider or "auto").strip().lower() or "auto"
        if provider not in {"auto", "api", "agy"}:
            raise CliConfigError(
                f"Invalid LLM_PROVIDER '{config.llm_provider}': expected auto, api, or agy"
            )
        if provider == "agy":
            effective_llm = AgyLLMAdapter(
                runner=effective_runner,
                cwd=config.workspaces_dir,
                timeout_seconds=config.agy_cli_timeout_seconds,
                secrets=active_secrets,
                models_config_path=config.models_config_path,
                model_resolver=shared_model_resolver,
            )
        else:
            if not config.llm_api_key:
                raise CliConfigError("Missing required environment variable LLM_API_KEY")
            _validate_models_toml(config.models_config_path)
            primary_llm = ProductionLLMAdapter(
                api_key=config.llm_api_key,
                models_config_path=config.models_config_path,
                transport=effective_transport,
                secrets=active_secrets,
            )
            if provider == "auto" and config.llm_fallback_to_agy:
                agy_fallback = AgyLLMAdapter(
                    runner=effective_runner,
                    cwd=config.workspaces_dir,
                    timeout_seconds=config.agy_cli_timeout_seconds,
                    secrets=active_secrets,
                    models_config_path=config.models_config_path,
                    model_resolver=shared_model_resolver,
                )
                effective_llm = FallbackLLMAdapter(primary=primary_llm, fallback=agy_fallback)
            else:
                effective_llm = primary_llm

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.workspaces_dir.mkdir(parents=True, exist_ok=True)

    store = SqliteEventStore(db_path=config.db_path)
    try:
        loaded_limits = load_limits(config.limits_config_path)
        if config.limits_config_path == Path("config/limits.toml"):
            loaded_limits["DEBATE"] = max(int(loaded_limits.get("DEBATE", 6)), 6)
            loaded_limits["MEDIATION"] = max(int(loaded_limits.get("MEDIATION", 3)), 3)
        circuit_breaker = CircuitBreaker(
            limits=loaded_limits,
            config_path=config.limits_config_path,
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

        workspace_manager = WorkspaceManager(base_dir=config.workspaces_dir)
        template_scaffold = TemplateScaffold(
            workspace_manager=workspace_manager,
            templates_root=config.templates_root,
        )

        orchestrator = Orchestrator(
            store=store,
            llm=effective_llm,
            clock=clock,
            circuit_breaker=circuit_breaker,
            publisher=effective_registry,
            workspace_manager=workspace_manager,
        )

        building_model = read_configured_model_for_stage(
            config.models_config_path,
            "BUILDING",
        )

        effective_code_agent = (
            code_agent
            if code_agent is not None
            else AgyCliAdapter(
                workspace_manager=workspace_manager,
                runner=effective_runner,
                timeout_seconds=config.agy_cli_timeout_seconds,
                model=building_model,
                model_resolver=shared_model_resolver,
                secrets=active_secrets,
            )
        )

        deploy_publisher: StaticDeployPublisher | None = None
        if config.deploy_target or config.vercel_token:
            deploy_base_url = (
                f"https://{config.github_owner}.github.io"
                if (config.deploy_target in {"gh-pages", "github-pages"} and bool(config.github_owner))
                else ""
            )
            deploy_token = (
                config.vercel_token
                if config.deploy_target.lower() == "vercel"
                else (config.github_token or config.vercel_token)
            )
            deploy_publisher = StaticDeployPublisher(
                runner=effective_runner,
                target=config.deploy_target,
                deploy_base_url=deploy_base_url,
                deploy_token=deploy_token,
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
        orchestrator.vcs = effective_vcs

        local_runner = LocalAppRunner(
            workspace_manager=workspace_manager,
            secrets_to_redact=active_secrets,
            lan_mode=config.lan_mode,
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
            local_app_runner=(
                local_runner if isinstance(effective_runner, SubprocessRunner) else None
            ),
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
            model_resolver=shared_model_resolver,
            local_runner=local_runner,
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
    if env is None and not os.environ.get("TELEGRAM_BOT_TOKEN"):
        load_dotenv_into_environ()
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

    dashboard_server: DashboardHTTPServer | None = None
    dashboard_thread: threading.Thread | None = None
    try:
        if not event.is_set():
            active_secrets = tuple(
                s
                for s in (
                    config.telegram_bot_token,
                    config.github_token,
                    config.vercel_token,
                    config.llm_api_key,
                )
                if s
            )
            dashboard_server, dashboard_thread, dashboard_url = start_dashboard_server(
                orchestrator=runtime.orchestrator,
                port=config.dashboard_port,
                build_worker=runtime.build_worker,
                template_scaffold=runtime.template_scaffold,
                workspace_manager=runtime.workspace_manager,
                bot_service=runtime.bot_service,
                model_resolver=runtime.model_resolver,
                local_runner=runtime.local_runner,
                vcs=runtime.vcs,
                secrets_to_redact=active_secrets,
            )
            print(f"Dashboard local disponible en {dashboard_url}", flush=True)

        run_polling_loop(
            runtime.bot_service,
            stop_event=event,
            initial_backoff_seconds=config.backoff_initial_seconds,
            max_backoff_seconds=config.backoff_max_seconds,
        )
    finally:
        if runtime.local_runner is not None:
            runtime.local_runner.stop_all()
        if dashboard_server is not None:
            dashboard_server.shutdown()
            dashboard_server.server_close()
            dashboard_server.controller.wait_for_idle(timeout=5.0)
        if dashboard_thread is not None and dashboard_thread.is_alive():
            dashboard_thread.join(timeout=2.0)
        runtime.store.close()
    return 0
