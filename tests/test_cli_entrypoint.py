"""Tests for fail-closed CLI configuration, dependency assembly, exponential backoff loop, and shutdown signals (AC-4.2, AC-4.3, INV-03, INV-19, INV-21, INV-23)."""

import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.cli import (
    CliConfig,
    CliConfigError,
    install_signal_handlers,
    load_cli_config,
    main,
    run_polling_loop,
)
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
