"""Tests for fail-closed CLI configuration, dependency assembly, exponential backoff loop, and shutdown signals (AC-4.2, AC-4.3, INV-03, INV-19, INV-21, INV-23)."""

import http.client
import io
import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher
from agent_factory.adapters.telegram_client import UrllibHttpTransport
from agent_factory.agents.pragmatist import PragmatistProposal
from agent_factory.cli import (
    CliConfig,
    CliConfigError,
    ProductionLLMAdapter,
    build_runtime,
    install_signal_handlers,
    load_cli_config,
    main,
    run_polling_loop,
)
from agent_factory.domain.models import Plan
from agent_factory.domain.states import ProjectState
from agent_factory.ports.llm import LLMError
from agent_factory.ports.notifier import HttpResponse, TelegramTransportError, TelegramUpdate
from agent_factory.ports.publisher import StatusPublisher


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


def test_cli_production_llm_adapter_uses_urllib_transport_calls_http_and_releases_store_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """H4-01 / H41-01 / H41-02 / M4-N11 (INV-01, INV-02, INV-07, INV-09, INV-23): ProductionLLMAdapter wires UrllibHttpTransport, sends HTTP POST with Bearer auth & models.toml settings, releases SQLite on error, aligns .env.example, and runs __main__.main()."""
    tg_tok = "TEST_H4101_TG_TOK_111"
    llm_key = "TEST_H4101_LLM_KEY_222"

    models_toml = tmp_path / "models_prod.toml"
    models_toml.write_text(
        'IDLE = "model-fast-01"\n'
        'INTERVIEW = "model-interview-01"\n'
        'DEBATE = "model-fast-01"\n'
        'MEDIATION = "model-strong-01"\n'
        'WAITING_APPROVAL = "model-fast-01"\n'
        'BUILDING = "model-fast-01"\n'
        'VERIFYING = "model-fast-01"\n'
        'DELIVERED = "model-fast-01"\n'
        'FAILED = "model-fast-01"\n'
        'CHANGE_REQUEST = "model-fast-01"\n'
        'endpoint = "https://llm.provider.test/v1/chat/completions"\n',
        encoding="utf-8",
    )
    limits_toml = tmp_path / "limits_prod.toml"
    limits_toml.write_text(
        "INTERVIEW = 6\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )

    # 1. Failing build_runtime MUST NOT leave an open SqliteEventStore handle (H41-02)
    leak_dir = tmp_path / "leak_db_dir"
    cfg_no_key = load_cli_config(
        {
            "TELEGRAM_BOT_TOKEN": tg_tok,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "AGENT_FACTORY_DB_PATH": str(leak_dir / "runtime.db"),
            "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws_leak"),
            "AGENT_FACTORY_MODELS_CONFIG": str(models_toml),
            "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
        }
    )
    with pytest.raises(CliConfigError):
        build_runtime(cfg_no_key)
    if leak_dir.exists():
        shutil.rmtree(leak_dir)
    assert not leak_dir.exists()

    # 2. build_runtime without explicit transport defaults to UrllibHttpTransport on ProductionLLMAdapter
    valid_env = {
        "TELEGRAM_BOT_TOKEN": tg_tok,
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
        "LLM_API_KEY": llm_key,
        "AGENT_FACTORY_DB_PATH": str(tmp_path / "prod_default.db"),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws_prod"),
        "AGENT_FACTORY_MODELS_CONFIG": str(models_toml),
        "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
    }
    cfg_valid = load_cli_config(valid_env)
    rt_default = build_runtime(cfg_valid)
    try:
        assert isinstance(getattr(rt_default.llm, "transport", None), UrllibHttpTransport)
    finally:
        rt_default.store.close()

    # 3. ProductionLLMAdapter.complete reads model/endpoint, POSTs JSON with Bearer auth, parses reply, and redacts errors (kills M4-N11)
    class _RecordingLlmHttpTransport:
        def __init__(self, script: list[HttpResponse | Exception]) -> None:
            self._script = list(script)
            self.calls: list[dict[str, Any]] = []

        def post_json(
            self,
            url: str,
            payload: Mapping[str, Any],
            timeout_seconds: float = 35.0,
            headers: Mapping[str, str] | None = None,
        ) -> HttpResponse:
            self.calls.append(
                {
                    "url": url,
                    "payload": dict(payload),
                    "timeout": timeout_seconds,
                    "headers": dict(headers or {}),
                }
            )
            step = self._script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    transport = _RecordingLlmHttpTransport(
        script=[
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "questions": ["Qual usuario?"],
                                            "is_complete": False,
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ),
            ),
            HttpResponse(status_code=401, body=f"Invalid Bearer {llm_key}"),
            http.client.RemoteDisconnected(f"Socket drop {llm_key}"),
        ]
    )
    cfg_tr = cfg_valid.model_copy(update={"db_path": tmp_path / "prod_tr.db"})
    rt_tr = build_runtime(cfg_tr, transport=transport)
    try:
        parsed = rt_tr.llm.complete([{"role": "user", "content": "Hola"}])
        assert parsed == {"questions": ["Qual usuario?"], "is_complete": False}
        assert len(transport.calls) == 1
        req_call = transport.calls[0]
        assert req_call["url"] == "https://llm.provider.test/v1/chat/completions"
        auth = req_call["headers"].get("Authorization") or req_call["headers"].get("authorization")
        assert auth == f"Bearer {llm_key}"
        assert req_call["payload"]["model"] in {"model-fast-01", "model-interview-01", "model-strong-01"}
        assert req_call["payload"]["messages"] == [{"role": "user", "content": "Hola"}]

        for _ in range(2):
            with pytest.raises(LLMError) as llm_err:
                rt_tr.llm.complete([{"role": "user", "content": "fail"}])
            assert llm_key not in str(llm_err.value)
            assert llm_err.value.__cause__ is None
            assert llm_err.value.__context__ is None
    finally:
        rt_tr.store.close()

    # 4. In-process execution of agent_factory.__main__ covers __main__.py cleanly
    for env_key in list(os.environ):
        if env_key in {"ALLOWED_CHAT_IDS", "ALLOWED_USER_IDS", "LLM_API_KEY"}:
            monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", tg_tok)
    monkeypatch.setattr(sys, "argv", ["agent_factory"])
    with pytest.raises(SystemExit) as sys_exit:
        runpy.run_module("agent_factory", run_name="__main__")
    assert sys_exit.value.code == 1
    captured = capsys.readouterr()
    assert tg_tok not in captured.out
    assert tg_tok not in captured.err

    import agent_factory.__main__ as main_mod

    pre_stopped = threading.Event()
    pre_stopped.set()
    assert main_mod.main([], env=valid_env, stop_event=pre_stopped) == 0

    # 5. .env.example documents ALLOWED_CHAT_IDS and ALLOWED_USER_IDS (H4-01)
    env_example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "ALLOWED_CHAT_IDS=" in env_example
    assert "ALLOWED_USER_IDS=" in env_example
    assert "TELEGRAM_ALLOWED_USER_ID" not in env_example


def test_cli_config_scrubs_exception_context_and_polling_loop_wakes_immediately_on_stop_event() -> None:
    """H4-06 / H4-09 / H41-03 (INV-06, INV-19, INV-21, INV-23): CliConfigError clears __cause__/__context__ on parse failures and run_polling_loop wakes immediately via stop_event.wait."""
    tok = "TEST_CTX_SECRET_987654"
    for bad_key in (
        "ALLOWED_CHAT_IDS",
        "ALLOWED_USER_IDS",
        "AGENT_FACTORY_POLL_TIMEOUT",
        "AGENT_FACTORY_BACKOFF_INITIAL",
        "AGENT_FACTORY_BACKOFF_MAX",
    ):
        with pytest.raises(CliConfigError) as err_info:
            load_cli_config(
                {
                    "TELEGRAM_BOT_TOKEN": tok,
                    "ALLOWED_CHAT_IDS": "1001",
                    "ALLOWED_USER_IDS": "2001",
                    bad_key: f"bad_{tok}",
                }
            )
        assert tok not in str(err_info.value)
        assert err_info.value.__cause__ is None
        assert err_info.value.__context__ is None

    # run_polling_loop with clock=None must wake on stop_event.set() instead of blocking 5.0s in time.sleep
    stop_event = threading.Event()
    timers: list[threading.Timer] = []

    def _trigger_stop(_err: Exception, _delay: float) -> None:
        t = threading.Timer(0.05, stop_event.set)
        timers.append(t)
        t.start()

    bot = _ScriptedBot(script=[TelegramTransportError("transient blip")])
    start = time.monotonic()
    try:
        iterations = run_polling_loop(
            bot,  # type: ignore[arg-type]
            clock=None,
            stop_event=stop_event,
            initial_backoff_seconds=5.0,
            max_backoff_seconds=5.0,
            on_error=_trigger_stop,
        )
    finally:
        for t in timers:
            t.cancel()
    elapsed = time.monotonic() - start
    assert iterations == 1
    assert elapsed < 1.5


def test_cli_production_llm_uses_models_section_state_routing_schema_and_runtime_wiring(
    tmp_path: Path,
) -> None:
    """H42-01 / M42-10 (INV-01, INV-09, INV-11, INV-21, INV-23, MASTER_PROMPT §2): Reads [models] fast/strong by state (never hardcoded model-fast-01), forwards JSON schema guidance, rejects empty/corrupt TOML and non-JSON on schema, and wires Orchestrator.publisher + github_owner deploy_base_url."""
    tg_tok = "TEST_H4201_TG_TOK_123"
    llm_key = "TEST_H4201_LLM_KEY_456"

    models_section_toml = tmp_path / "models_section.toml"
    models_section_toml.write_text(
        'endpoint = "https://custom-llm.test/v1/chat/completions"\n\n'
        "[models]\n"
        'fast = "acme-haiku-fast-2026"\n'
        'strong = "acme-opus-mediation-2026"\n',
        encoding="utf-8",
    )
    limits_toml = tmp_path / "limits_h42.toml"
    limits_toml.write_text(
        "INTERVIEW = 6\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )

    class _RecordingTransport:
        def __init__(self, script: list[HttpResponse]) -> None:
            self._script = list(script)
            self.calls: list[dict[str, Any]] = []

        def post_json(
            self,
            url: str,
            payload: Mapping[str, Any],
            timeout_seconds: float = 35.0,
            headers: Mapping[str, str] | None = None,
        ) -> HttpResponse:
            self.calls.append(
                {
                    "url": url,
                    "payload": dict(payload),
                    "timeout": timeout_seconds,
                    "headers": dict(headers or {}),
                }
            )
            return self._script.pop(0)

    transport = _RecordingTransport(
        script=[
            # 1. INTERVIEW / fast model call
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {"choices": [{"message": {"content": '{"questions": [], "is_complete": true}'}}]}
                ),
            ),
            # 2. DEBATE (PragmatistProposal schema) / fast model call with schema instructions
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": '{"approach": "static", "components": ["app.js"], "tradeoffs": []}'
                                }
                            }
                        ]
                    }
                ),
            ),
            # 3. MEDIATION / strong model call with Plan schema
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "summary": "Plan OK",
                                            "rationale": "Simple",
                                            "template_id": "static-web",
                                            "files": [
                                                {
                                                    "path": "src/app.js",
                                                    "purpose": "Main",
                                                    "contract": "export {};",
                                                    "depends_on": [],
                                                }
                                            ],
                                            "acceptance_tests": [
                                                {"id": "AT-01", "description": "Pass", "type": "unit"}
                                            ],
                                            "risks": [],
                                            "deployment_target": "github-pages",
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ),
            ),
            # 4. Non-JSON reply when schema is required -> MUST raise LLMError
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {"choices": [{"message": {"content": "Sure, here is a plain prose answer."}}]}
                ),
            ),
        ]
    )

    adapter = ProductionLLMAdapter(
        api_key=llm_key,
        models_config_path=models_section_toml,
        transport=transport,
        secrets=(llm_key,),
    )

    # Call 1: INTERVIEW -> selects [models].fast (NEVER "model-fast-01")
    out_interview = adapter.complete(
        messages=[
            {"role": "system", "content": "You are an expert product interviewer."},
            {"role": "user", "content": "Build timer"},
        ],
        schema=None,
    )
    assert out_interview == {"questions": [], "is_complete": True}
    assert transport.calls[0]["url"] == "https://custom-llm.test/v1/chat/completions"
    assert transport.calls[0]["payload"]["model"] == "acme-haiku-fast-2026"
    assert transport.calls[0]["payload"]["model"] != "model-fast-01"

    # Call 2: DEBATE (Pragmatist + schema) -> selects fast model AND includes JSON schema guidance
    out_debate = adapter.complete(
        messages=[
            {"role": "system", "content": "You are Pragmatist, an architectural agent."},
            {"role": "user", "content": "Spec details"},
        ],
        schema=PragmatistProposal,
    )
    assert out_debate["approach"] == "static"
    debate_payload = transport.calls[1]["payload"]
    debate_serialized = json.dumps(debate_payload)
    assert debate_payload["model"] == "acme-haiku-fast-2026"
    assert "approach" in debate_serialized
    assert "response_format" in debate_payload or "json" in debate_serialized.lower()

    # Call 3: MEDIATION (Mediator + Plan) -> MUST select [models].strong (kills M42-10)
    out_med = adapter.complete(
        messages=[
            {"role": "system", "content": "You are Mediator, the final debate synthesis agent."},
            {"role": "user", "content": "Spec and proposals"},
        ],
        schema=Plan,
    )
    assert out_med["template_id"] == "static-web"
    med_payload = transport.calls[2]["payload"]
    assert med_payload["model"] == "acme-opus-mediation-2026"
    assert "acceptance_tests" in json.dumps(med_payload)

    # Call 4: Non-JSON text when schema != None MUST fail closed with LLMError
    with pytest.raises(LLMError):
        adapter.complete(
            messages=[{"role": "user", "content": "Propose"}],
            schema=PragmatistProposal,
        )

    # Empty or corrupt models.toml MUST fail closed with LLMError (not silent model-fast-01 fallback)
    empty_toml = tmp_path / "empty_models.toml"
    empty_toml.write_text("", encoding="utf-8")
    corrupt_toml = tmp_path / "corrupt_models.toml"
    corrupt_toml.write_text("broken = [unclosed\n", encoding="utf-8")

    for bad_path in (empty_toml, corrupt_toml):
        bad_adapter = ProductionLLMAdapter(
            api_key=llm_key,
            models_config_path=bad_path,
            transport=_RecordingTransport(
                script=[
                    HttpResponse(
                        status_code=200,
                        body=json.dumps({"choices": [{"message": {"content": "{}"}}]}),
                    )
                ]
            ),
            secrets=(llm_key,),
        )
        with pytest.raises(LLMError):
            bad_adapter.complete([{"role": "user", "content": "Hi"}])

    # build_runtime must wire non-None StatusPublisher in Orchestrator and propagate github_owner to deploy_base_url
    cfg_rt = load_cli_config(
        {
            "TELEGRAM_BOT_TOKEN": tg_tok,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "LLM_API_KEY": llm_key,
            "GITHUB_OWNER": "gabriel-acme-test",
            "AGENT_FACTORY_DEPLOY_TARGET": "github-pages",
            "AGENT_FACTORY_DB_PATH": str(tmp_path / "wired.db"),
            "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws_wired"),
            "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
            "AGENT_FACTORY_MODELS_CONFIG": str(models_section_toml),
            "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
        }
    )
    rt = build_runtime(cfg_rt, transport=transport)
    try:
        assert rt.orchestrator.publisher is not None
        assert isinstance(rt.orchestrator.publisher, StatusPublisher)
        assert isinstance(rt.vcs, GitHubVCSAdapter)
        assert rt.vcs.deploy_publisher is not None
        assert rt.vcs.deploy_publisher.deploy_base_url == "https://gabriel-acme-test.github.io"
    finally:
        rt.store.close()


def test_urllib_http_transport_post_json_sends_headers_json_and_wraps_http_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H42-02 / M42-11 (INV-01, INV-02, INV-19, INV-23): Exercises real UrllibHttpTransport.post_json offline via urllib.request.urlopen interception, validating POST, JSON UTF-8, custom Authorization Bearer headers, and HTTPError/URLError -> TelegramTransportError."""
    auth_tok = "TEST_URLLIB_BEARER_778899"
    tg_url_err_tok = "123456:URLLIB_ERR_SECRET_4455"
    seen_requests: list[tuple[urllib.request.Request, float]] = []

    class _FakeRespCtx:
        def __init__(self, status: int, data: bytes) -> None:
            self.status = status
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    mode = {"next": "ok"}

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 35.0) -> _FakeRespCtx:
        seen_requests.append((req, timeout))
        if mode["next"] == "http_err":
            raise urllib.error.HTTPError(
                url=f"https://api.telegram.org/bot{tg_url_err_tok}/sendMessage",
                code=401,
                msg="Unauthorized",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(f"bad token {auth_tok}".encode()),
            )
        if mode["next"] == "url_err":
            raise urllib.error.URLError(
                f"dial tcp failed for https://api.telegram.org/bot{tg_url_err_tok}/getUpdates"
            )
        return _FakeRespCtx(200, b'{"choices":[{"message":{"content":"{}"}}]}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    transport = UrllibHttpTransport()

    # 1. Success path: POST, UTF-8 JSON body, Content-Type, and custom Authorization header (kills M42-11)
    resp = transport.post_json(
        "https://custom-llm.test/v1/chat/completions",
        {"model": "acme-fast", "messages": [{"role": "user", "content": "Hola ñ"}]},
        timeout_seconds=12.5,
        headers={"Authorization": f"Bearer {auth_tok}", "X-Trace-Id": "trace-42"},
    )
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"choices": [{"message": {"content": "{}"}}]}

    req, used_timeout = seen_requests[0]
    assert used_timeout == 12.5
    assert req.get_method() == "POST"
    assert req.full_url == "https://custom-llm.test/v1/chat/completions"
    assert json.loads(req.data.decode("utf-8")) == {
        "model": "acme-fast",
        "messages": [{"role": "user", "content": "Hola ñ"}],
    }
    norm_headers = {k.lower(): v for k, v in req.header_items()}
    assert norm_headers["content-type"] == "application/json"
    assert norm_headers["authorization"] == f"Bearer {auth_tok}"
    assert norm_headers["x-trace-id"] == "trace-42"

    # 2. HTTPError and URLError MUST wrap cleanly into TelegramTransportError without leaking tokens or __context__
    for err_mode in ("http_err", "url_err"):
        mode["next"] = err_mode
        with pytest.raises(TelegramTransportError) as tp_err:
            transport.post_json(
                f"https://api.telegram.org/bot{tg_url_err_tok}/sendMessage",
                {"chat_id": 1},
            )
        assert tg_url_err_tok not in str(tp_err.value)
        assert auth_tok not in str(tp_err.value)
        assert tp_err.value.__cause__ is None
        assert tp_err.value.__context__ is None

    # 3. M43-14: UrllibHttpTransport.post_json accepts extra_secrets=(...) and scrubs both extra_secrets and custom headers in TelegramTransportError
    custom_sec = "CUSTOM_SECRET_XYZ"
    hdr_sec = "CUSTOM_HDR_TOKEN_445566"

    def _fake_urlopen_custom(req: urllib.request.Request, timeout: float = 35.0) -> _FakeRespCtx:
        raise urllib.error.URLError(
            f"upstream rejected auth ({custom_sec}) and header ({hdr_sec})"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_custom)
    with pytest.raises(TelegramTransportError) as custom_err:
        transport.post_json(
            "https://custom-llm.test/v1/chat/completions",
            {"model": "acme-fast", "messages": []},
            headers={"X-Custom-Auth": hdr_sec},
            extra_secrets=(custom_sec,),
        )
    assert custom_sec not in str(custom_err.value)
    assert hdr_sec not in str(custom_err.value)
    assert "[REDACTED]" in str(custom_err.value)
    assert custom_err.value.__cause__ is None
    assert custom_err.value.__context__ is None


def test_cli_h42_01_rejects_none_models_path_invalid_models_toml_and_vercel_github_io_url(
    tmp_path: Path,
) -> None:
    """H42-01 (INV-07, INV-19, INV-21, INV-23, MASTER_PROMPT §2 / AGENTS.md #12): Rejects models_config_path=None (no model-fast-01 fallback), rejects models.toml missing [models]/model keys or using nested CONFIGURE_ME, and avoids github.io deploy_base_url when deploy_target=vercel."""
    tg_tok = "TEST_H4201B_TG_TOK_111"
    llm_key = "TEST_H4201B_LLM_KEY_222"
    vc_tok = "TEST_H4201B_VC_TOK_333"

    class _FailIfCalledTransport:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def post_json(
            self,
            url: str,
            payload: Mapping[str, Any],
            timeout_seconds: float = 35.0,
            headers: Mapping[str, str] | None = None,
        ) -> HttpResponse:
            self.calls.append({"url": url, "payload": dict(payload)})
            return HttpResponse(status_code=200, body='{"choices":[{"message":{"content":"{}"}}]}')

    no_call_transport = _FailIfCalledTransport()

    # 1. ProductionLLMAdapter MUST reject models_config_path=None instead of hardcoding "model-fast-01"
    with pytest.raises((LLMError, ValueError)):
        adapter_none = ProductionLLMAdapter(
            api_key=llm_key,
            models_config_path=None,
            transport=no_call_transport,
            secrets=(llm_key,),
        )
        adapter_none.complete([{"role": "user", "content": "Hola"}])
    assert len(no_call_transport.calls) == 0

    limits_toml = tmp_path / "limits_h42b.toml"
    limits_toml.write_text(
        "INTERVIEW = 6\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )

    # 2. build_runtime MUST reject empty models.toml, missing [models]/model keys, empty [models], and nested [models] CONFIGURE_ME
    bad_toml_variants = {
        "empty": "",
        "no_models_section": 'endpoint = "https://custom-llm.test/v1/chat/completions"\n[other]\nfoo = "bar"\n',
        "empty_models_section": "[models]\n",
        "nested_configure_me": '[models]\nfast = "CONFIGURE_ME"\nstrong = "acme-strong"\n',
    }
    for label, toml_content in bad_toml_variants.items():
        bad_path = tmp_path / f"{label}_models.toml"
        bad_path.write_text(toml_content, encoding="utf-8")
        cfg_bad = load_cli_config(
            {
                "TELEGRAM_BOT_TOKEN": tg_tok,
                "ALLOWED_CHAT_IDS": "1001",
                "ALLOWED_USER_IDS": "2001",
                "LLM_API_KEY": llm_key,
                "AGENT_FACTORY_DB_PATH": str(tmp_path / f"{label}.db"),
                "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / f"ws_{label}"),
                "AGENT_FACTORY_MODELS_CONFIG": str(bad_path),
                "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
            }
        )
        with pytest.raises(CliConfigError):
            build_runtime(cfg_bad, transport=no_call_transport)

    # 3. deploy_target == "vercel" MUST NOT set deploy_base_url to https://<github_owner>.github.io
    valid_models = tmp_path / "valid_models_vercel.toml"
    valid_models.write_text(
        '[models]\nfast = "acme-fast"\nstrong = "acme-strong"\n',
        encoding="utf-8",
    )
    cfg_vercel = load_cli_config(
        {
            "TELEGRAM_BOT_TOKEN": tg_tok,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "LLM_API_KEY": llm_key,
            "GITHUB_OWNER": "gabriel-vercel-owner",
            "AGENT_FACTORY_DEPLOY_TARGET": "vercel",
            "VERCEL_TOKEN": vc_tok,
            "AGENT_FACTORY_DB_PATH": str(tmp_path / "vercel_runtime.db"),
            "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws_vercel"),
            "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
            "AGENT_FACTORY_MODELS_CONFIG": str(valid_models),
            "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
        }
    )
    rt_vc = build_runtime(cfg_vercel, transport=no_call_transport)
    try:
        assert isinstance(rt_vc.vcs, GitHubVCSAdapter)
        assert isinstance(rt_vc.vcs.deploy_publisher, StaticDeployPublisher)
        assert rt_vc.vcs.deploy_publisher.target == "vercel"
        assert "github.io" not in rt_vc.vcs.deploy_publisher.deploy_base_url
    finally:
        rt_vc.store.close()
