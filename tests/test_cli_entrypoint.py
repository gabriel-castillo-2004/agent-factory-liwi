"""Tests for fail-closed CLI configuration, dependency assembly, exponential backoff loop, and shutdown signals (AC-4.2, AC-4.3, INV-03, INV-19, INV-21, INV-23)."""

import os
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher
from agent_factory.cli import (
    CliConfig,
    CliConfigError,
    build_runtime,
    install_signal_handlers,
    load_cli_config,
    main,
    run_polling_loop,
)
from agent_factory.domain.states import ProjectState
from agent_factory.ports.notifier import TelegramTransportError, TelegramUpdate


class _FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 11, 17, 9, 30, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class _ScriptedBot:
    """Minimal bot double matching TelegramBotService.poll_once() for loop/backoff verification."""

    def __init__(self, script: list[list[Any] | Exception], stop_on_iteration: int | None = None, stop_event: threading.Event | None = None) -> None:
        self._script = list(script)
        self.poll_calls = 0
        self._stop_on_iteration = stop_on_iteration
        self._stop_event = stop_event

    def poll_once(self) -> list[Any]:
        self.poll_calls += 1
        step = self._script.pop(0) if self._script else []
        if (
            self._stop_on_iteration is not None
            and self.poll_calls >= self._stop_on_iteration
            and self._stop_event is not None
        ):
            self._stop_event.set()
        if isinstance(step, Exception):
            raise step
        return step


def test_cli_config_fail_closed_and_redacts_tokens_on_error(tmp_path: Path) -> None:
    """AC-4.2 positive & negative (INV-19, INV-21, INV-23): Valid env loads CliConfig; missing/empty whitelists or token fail closed without leaking secrets."""
    token = "TEST_CLI_SECRET_TOK_998877"
    valid_env = {
        "TELEGRAM_BOT_TOKEN": token,
        "ALLOWED_CHAT_IDS": "1001, 1002",
        "ALLOWED_USER_IDS": "2001",
        "AGENT_FACTORY_DB_PATH": str(tmp_path / "factory.db"),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws"),
        "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
        "AGENT_FACTORY_REGISTRY_BRANCH": "registry",
        "AGENT_FACTORY_POLL_TIMEOUT": "20",
        "AGENT_FACTORY_BACKOFF_INITIAL": "2.0",
        "AGENT_FACTORY_BACKOFF_MAX": "32.0",
        "GITHUB_OWNER": "gabriel-test",
    }
    cfg = load_cli_config(valid_env)
    assert isinstance(cfg, CliConfig)
    assert cfg.telegram_bot_token == token
    assert cfg.allowed_chat_ids == frozenset({1001, 1002})
    assert cfg.allowed_user_ids == frozenset({2001})
    assert cfg.db_path == tmp_path / "factory.db"
    assert cfg.workspaces_dir == tmp_path / "ws"
    assert cfg.poll_timeout_seconds == 20
    assert cfg.backoff_initial_seconds == 2.0
    assert cfg.backoff_max_seconds == 32.0
    assert cfg.github_owner == "gabriel-test"

    # Negative cases (must fail closed and NEVER leak TELEGRAM_BOT_TOKEN)
    bad_envs = [
        {"ALLOWED_CHAT_IDS": "1001", "ALLOWED_USER_IDS": "2001"},
        {"TELEGRAM_BOT_TOKEN": "   ", "ALLOWED_CHAT_IDS": "1001", "ALLOWED_USER_IDS": "2001"},
        {"TELEGRAM_BOT_TOKEN": token, "ALLOWED_USER_IDS": "2001"},
        {"TELEGRAM_BOT_TOKEN": token, "ALLOWED_CHAT_IDS": " , ", "ALLOWED_USER_IDS": "2001"},
        {"TELEGRAM_BOT_TOKEN": token, "ALLOWED_CHAT_IDS": "1001", "ALLOWED_USER_IDS": ""},
        {"TELEGRAM_BOT_TOKEN": token, "ALLOWED_CHAT_IDS": "1001,bad", "ALLOWED_USER_IDS": "2001"},
        {
            "TELEGRAM_BOT_TOKEN": token,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "AGENT_FACTORY_BACKOFF_INITIAL": "10.0",
            "AGENT_FACTORY_BACKOFF_MAX": "2.0",
        },
    ]
    for bad_env in bad_envs:
        with pytest.raises(CliConfigError) as exc_info:
            load_cli_config(bad_env)
        assert token not in str(exc_info.value)

    # main() with invalid env returns non-zero exit code gracefully
    exit_code = main([], env={"TELEGRAM_BOT_TOKEN": token})
    assert exit_code != 0


def test_cli_polling_loop_exponential_backoff_and_clean_shutdown() -> None:
    """AC-4.3 positive & negative (INV-04, INV-06, INV-23): Retries transient TelegramTransportError with bounded exponential backoff, resets after recovery, and stops cleanly on stop_event."""
    stop_event = threading.Event()
    clock = _FakeClock()
    errors_seen: list[tuple[str, float]] = []

    bot = _ScriptedBot(
        script=[
            TelegramTransportError("net blip 1"),
            TelegramTransportError("net blip 2"),
            TelegramTransportError("net blip 3"),
            [{"ok": True}],  # Poll 4 succeeds -> resets backoff
            TelegramTransportError("net blip 4"),
            [{"ok": True}],  # Poll 6 succeeds and triggers stop_event
        ],
        stop_on_iteration=6,
        stop_event=stop_event,
    )

    iterations = run_polling_loop(
        bot,  # type: ignore[arg-type]
        clock=clock,
        stop_event=stop_event,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=3.0,
        on_error=lambda err, delay: errors_seen.append((str(err), delay)),
    )

    assert iterations == 6
    assert bot.poll_calls == 6
    # Hand-crafted oracle: 1.0 -> 2.0 -> 3.0 (capped) -> reset on poll 4 -> 1.0
    assert clock.sleeps == [1.0, 2.0, 3.0, 1.0]
    assert [delay for _, delay in errors_seen] == [1.0, 2.0, 3.0, 1.0]

    # Signal handler registration & stop_event pre-set must execute zero polls
    pre_stopped = threading.Event()
    install_signal_handlers(pre_stopped)
    pre_stopped.set()
    zero_bot = _ScriptedBot(script=[TelegramUpdate(update_id=1, chat_id=1, user_id=2)])
    zero_clock = _FakeClock()
    assert (
        run_polling_loop(
            zero_bot,  # type: ignore[arg-type]
            clock=zero_clock,
            stop_event=pre_stopped,
        )
        == 0
    )
    assert zero_bot.poll_calls == 0

    # Invoking registered SIGINT handler flips an unset stop_event cleanly
    sig_stop = threading.Event()
    install_signal_handlers(sig_stop)
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)
    assert sig_stop.is_set() is True

    # Verify types kept alive in test module imports cleanly
    _ = (FakeTelegramClient, MemoryEventStore)


def test_cli_build_runtime_wires_configured_llm_limits_deploy_and_main_lifecycle(
    tmp_path: Path,
) -> None:
    """H4-01 (AC-4.2, INV-01, INV-17, INV-21, INV-23): build_runtime fails closed without FakeLLM fallback, reads models/limits config, wires StaticDeployPublisher, and main()/__main__ lifecycle works."""
    tg_tok = "TEST_H401_TG_TOK_112233"
    llm_key = "TEST_H401_LLM_KEY_445566"
    vc_tok = "TEST_H401_VC_TOK_778899"

    models_toml = tmp_path / "models.toml"
    models_toml.write_text(
        'IDLE = "fast-model"\n'
        'INTERVIEW = "fast-model"\n'
        'DEBATE = "fast-model"\n'
        'MEDIATION = "strong-model"\n'
        'WAITING_APPROVAL = "fast-model"\n'
        'BUILDING = "fast-model"\n'
        'VERIFYING = "fast-model"\n'
        'DELIVERED = "fast-model"\n'
        'FAILED = "fast-model"\n'
        'CHANGE_REQUEST = "fast-model"\n',
        encoding="utf-8",
    )
    limits_toml = tmp_path / "limits.toml"
    limits_toml.write_text(
        "INTERVIEW = 2\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )

    unconfigured_models = tmp_path / "unconfigured_models.toml"
    unconfigured_models.write_text('INTERVIEW = "CONFIGURE_ME"\n', encoding="utf-8")

    base_env = {
        "TELEGRAM_BOT_TOKEN": tg_tok,
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
        "AGENT_FACTORY_DB_PATH": str(tmp_path / "runtime.db"),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws"),
        "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
        "AGENT_FACTORY_MODELS_CONFIG": str(models_toml),
        "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
        "AGENT_FACTORY_DEPLOY_TARGET": "vercel",
        "VERCEL_TOKEN": vc_tok,
    }

    # Fail-closed when llm is None and LLM_API_KEY is missing (must NOT fall back to FakeLLM)
    cfg_no_key = load_cli_config(base_env)
    with pytest.raises(CliConfigError) as err_no_key:
        build_runtime(cfg_no_key)
    assert llm_key not in str(err_no_key.value)
    assert tg_tok not in str(err_no_key.value)

    # Fail-closed when models.toml has placeholder CONFIGURE_ME or does not exist
    cfg_placeholder = cfg_no_key.model_copy(
        update={"llm_api_key": llm_key, "models_config_path": unconfigured_models}
    )
    with pytest.raises(CliConfigError):
        build_runtime(cfg_placeholder)

    cfg_missing_models = cfg_no_key.model_copy(
        update={"llm_api_key": llm_key, "models_config_path": tmp_path / "missing.toml"}
    )
    with pytest.raises(CliConfigError):
        build_runtime(cfg_missing_models)

    # Valid production env wires non-Fake LLM, custom limits.toml, and StaticDeployPublisher
    valid_env = dict(base_env)
    valid_env["LLM_API_KEY"] = llm_key
    cfg_valid = load_cli_config(valid_env)
    assert cfg_valid.models_config_path == models_toml
    assert cfg_valid.limits_config_path == limits_toml

    runtime = build_runtime(cfg_valid)
    try:
        assert not isinstance(runtime.llm, FakeLLM)
        assert hasattr(runtime.llm, "complete")
        assert runtime.orchestrator.circuit_breaker.get_limit(ProjectState.INTERVIEW) == 2
        assert isinstance(runtime.vcs, GitHubVCSAdapter)
        assert isinstance(runtime.vcs.deploy_publisher, StaticDeployPublisher)
        assert runtime.vcs.deploy_publisher.target == "vercel"
        assert runtime.vcs.deploy_publisher.deploy_token == vc_tok
    finally:
        runtime.store.close()

    # Valid main() lifecycle with stop_event pre-set completes with exit_code == 0 without network calls
    stop_event = threading.Event()
    stop_event.set()
    assert main([], env=valid_env, stop_event=stop_event) == 0

    # Executing `python -m agent_factory` with invalid config exits non-zero without leaking token
    sub_env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "TMP", "TEMP", "HOME", "USERPROFILE"}
    }
    sub_env["TELEGRAM_BOT_TOKEN"] = tg_tok
    sub_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    cli_proc = subprocess.run(
        [sys.executable, "-m", "agent_factory"],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert cli_proc.returncode != 0
    assert tg_tok not in cli_proc.stdout
    assert tg_tok not in cli_proc.stderr


def test_cli_config_rejects_nan_inf_and_never_leaks_token_in_any_error() -> None:
    """H4-09 / M4-03a (INV-19, INV-21, INV-23): Rejects nan/inf/non-positive backoff and poll timeout while redacting embedded bot token in every CliConfigError."""
    tok = "TEST_CLI_SECRET_ERR_445566"

    # Embedded token inside integer/float malformed env fields must never leak in CliConfigError (kills M4-03a)
    for bad_key in (
        "ALLOWED_CHAT_IDS",
        "ALLOWED_USER_IDS",
        "AGENT_FACTORY_POLL_TIMEOUT",
        "AGENT_FACTORY_BACKOFF_INITIAL",
        "AGENT_FACTORY_BACKOFF_MAX",
    ):
        env_leak = {
            "TELEGRAM_BOT_TOKEN": tok,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            bad_key: f"bad_{tok}",
        }
        with pytest.raises(CliConfigError) as err_leak:
            load_cli_config(env_leak)
        assert tok not in str(err_leak.value)

    # Reject nan, inf, -inf, zero, and negative values in numeric config
    for bad_num in ("nan", "NaN", "inf", "-inf", "0", "-1.5"):
        for field_name in ("AGENT_FACTORY_BACKOFF_INITIAL", "AGENT_FACTORY_BACKOFF_MAX"):
            with pytest.raises(CliConfigError):
                load_cli_config(
                    {
                        "TELEGRAM_BOT_TOKEN": tok,
                        "ALLOWED_CHAT_IDS": "1001",
                        "ALLOWED_USER_IDS": "2001",
                        field_name: bad_num,
                    }
                )

    for bad_timeout in ("0", "-10", "nan"):
        with pytest.raises(CliConfigError):
            load_cli_config(
                {
                    "TELEGRAM_BOT_TOKEN": tok,
                    "ALLOWED_CHAT_IDS": "1001",
                    "ALLOWED_USER_IDS": "2001",
                    "AGENT_FACTORY_POLL_TIMEOUT": bad_timeout,
                }
            )
