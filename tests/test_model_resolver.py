"""Unit and integration tests for dynamic ModelResolver and AgyLLMAdapter/CLI runtime fallback (INV-09, INV-19, INV-23)."""

import json
import logging
import sys
import threading
from pathlib import Path

import pytest

from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
from agent_factory.adapters.agy_llm_adapter import AgyLLMAdapter, FallbackLLMAdapter
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.model_resolver import (
    ModelResolver,
    parse_available_models,
    read_configured_model_for_stage,
)
from agent_factory.cli import build_runtime, load_cli_config
from agent_factory.domain.models import Plan
from agent_factory.ports.llm import LLMError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError

REAL_AGY_INVALID_MODEL_STDERR = (
    'error: invalid model selection (--model "gemini-2.5-pro" --effort ""): '
    "model gemini-2.5-pro is not recognized as a known model or custom model in settings\n\n"
    "Available models:\n"
    "  - Gemini 3.5 Pro\n"
    "  - Gemini 4 Neon (High)\n"
    "  - Gemini 4 Argon (High)\n"
    "  - Claude Sonnet 4.5\n"
    "  - Gemini 3.8 Flash (High)\n"
    "  - Gemini 4 Hydrogen (High)\n\n"
    'Use --model "<name>" to specify a model.\n'
)


def test_parse_available_models_extracts_real_agy_error_formats_and_handles_empty() -> None:
    """parse_available_models extracts model names under Available models: across bullet, plain, and inline formats."""
    parsed = parse_available_models(REAL_AGY_INVALID_MODEL_STDERR)
    assert parsed == [
        "Gemini 3.5 Pro",
        "Gemini 4 Neon (High)",
        "Gemini 4 Argon (High)",
        "Claude Sonnet 4.5",
        "Gemini 3.8 Flash (High)",
        "Gemini 4 Hydrogen (High)",
    ]

    plain_block = (
        "Available models:\n\n"
        "  Gemini 4 Neon (High)\n"
        "  Gemini 3.8 Flash (High)\n"
        "  Gemini 4 Neon (High)\n"
        "\n"
        "Flags:\n"
        "  --model  Choose model\n"
    )
    assert parse_available_models(plain_block) == [
        "Gemini 4 Neon (High)",
        "Gemini 3.8 Flash (High)",
    ]

    assert parse_available_models("Available models: Alpha One, Beta Two\n") == [
        "Alpha One",
        "Beta Two",
    ]
    assert parse_available_models("Fatal crash without model list") == []
    assert parse_available_models("") == []


def test_model_resolver_discover_cache_timeout_and_sanitized_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discover_available_models probes agy cleanly without secrets, caches results, supports force_refresh, and degrades safely on mock failures."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET_BOT_TOK_PROBE")
    monkeypatch.setenv("LLM_API_KEY", "SECRET_LLM_KEY_PROBE")

    runner = FakeRunner(
        results=[
            CommandResult(exit_code=1, stdout="", stderr=REAL_AGY_INVALID_MODEL_STDERR),
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="Available models:\n  - Refreshed Model X\n",
            ),
        ]
    )
    resolver = ModelResolver(
        runner=runner,
        cwd=tmp_path,
        secrets=("SECRET_BOT_TOK_PROBE",),
        auto_probe=True,
    )

    first = resolver.discover_available_models()
    assert "Gemini 4 Neon (High)" in first
    assert len(runner.calls) == 1
    assert runner.calls[0]["command"] == ["agy", "-p", "ping", "--model", "__probe__"]
    assert runner.calls[0]["timeout_seconds"] == 10.0
    env_sent = runner.calls[0]["env"]
    assert env_sent is not None
    assert "TELEGRAM_BOT_TOKEN" not in env_sent
    assert "LLM_API_KEY" not in env_sent

    # Cached call hits memory without running runner again
    second = resolver.discover_available_models(force_refresh=False)
    assert second == first
    assert len(runner.calls) == 1

    # force_refresh=True re-probes
    refreshed = resolver.discover_available_models(force_refresh=True)
    assert refreshed == ["Refreshed Model X"]
    assert len(runner.calls) == 2

    # Offline mock returning empty/timeout falls back safely to configured model
    fail_runner = FakeRunner(results=[CommandTimeoutError("timeout")])
    safe_resolver = ModelResolver(runner=fail_runner, cwd=tmp_path, auto_probe=True)
    assert safe_resolver.discover_available_models() == []
    assert safe_resolver.resolve_model_for_role("BUILDING", "gemini-2.5-pro") == "gemini-2.5-pro"


def test_model_resolver_role_preferences_and_models_toml_overrides(tmp_path: Path) -> None:
    """resolve_model_for_role matches case-insensitively or picks best stage preference when configured_model is missing."""
    models_toml = tmp_path / "models.toml"
    models_toml.write_text(
        'INTERVIEW = "gemini-2.5-flash"\n'
        'MEDIATION = "gemini-2.5-pro"\n'
        'BUILDING = "gemini-2.5-pro"\n'
        '[models]\n'
        'fast = "gemini-2.5-flash"\n'
        'strong = "gemini-2.5-pro"\n',
        encoding="utf-8",
    )
    assert read_configured_model_for_stage(models_toml, "BUILDING") == "gemini-2.5-pro"
    assert read_configured_model_for_stage(models_toml, "INTERVIEW") == "gemini-2.5-flash"

    runner = FakeRunner(
        results=[CommandResult(exit_code=1, stdout="", stderr=REAL_AGY_INVALID_MODEL_STDERR)]
    )
    resolver = ModelResolver(
        runner=runner,
        cwd=tmp_path,
        models_config_path=models_toml,
        auto_probe=True,
    )

    # Unavailable configured model "gemini-2.5-pro" resolves to Neon (High) for strong stages
    assert resolver.resolve_model_for_role("BUILDING", "gemini-2.5-pro") == "Gemini 4 Neon (High)"
    assert resolver.resolve_model_for_role("MEDIATION", "gemini-2.5-pro") == "Gemini 4 Neon (High)"
    assert resolver.resolve_model_for_role("DEBATE", "gemini-2.5-flash") == "Gemini 4 Neon (High)"

    # Fast stage ("INTERVIEW") prioritizes Gemini 3.8 Flash
    assert (
        resolver.resolve_model_for_role("INTERVIEW", "gemini-2.5-flash")
        == "Gemini 3.8 Flash (High)"
    )

    # Exact or case-insensitive match in discovered models is preserved
    assert resolver.resolve_model_for_role("BUILDING", "claude sonnet 4.5") == "Claude Sonnet 4.5"

    # Next fallback walks remaining strong preferences in order until exhausted
    fb1 = resolver.next_fallback_model("BUILDING", "Gemini 4 Neon (High)")
    assert fb1 == "Gemini 4 Argon (High)"
    fb2 = resolver.next_fallback_model("BUILDING", "Gemini 4 Argon (High)")
    assert fb2 == "Claude Sonnet 4.5"
    fb3 = resolver.next_fallback_model("BUILDING", "Claude Sonnet 4.5")
    assert fb3 == "Gemini 3.8 Flash (High)"

    # Custom preference list in models.toml [fallbacks] is honored
    custom_toml = tmp_path / "custom_models.toml"
    custom_toml.write_text(
        '[models]\nfast = "x"\nstrong = "y"\n'
        '[fallbacks]\nBUILDING = ["Hydrogen", "Gemini 3.5 Pro"]\n',
        encoding="utf-8",
    )
    custom_runner = FakeRunner(
        results=[CommandResult(exit_code=1, stdout="", stderr=REAL_AGY_INVALID_MODEL_STDERR)]
    )
    custom_resolver = ModelResolver(
        runner=custom_runner,
        models_config_path=custom_toml,
        auto_probe=True,
    )
    assert custom_resolver.resolve_model_for_role("BUILDING", "unknown") == "Gemini 4 Hydrogen (High)"


def test_model_resolver_thread_safety() -> None:
    """ModelResolver handles concurrent lookups and failure markings safely under RLock."""
    runner = FakeRunner(
        results=[CommandResult(exit_code=1, stdout="", stderr=REAL_AGY_INVALID_MODEL_STDERR)]
    )
    resolver = ModelResolver(runner=runner, auto_probe=True)
    results: list[str | None] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker(idx: int) -> None:
        try:
            stage = "BUILDING" if idx % 2 == 0 else "INTERVIEW"
            chosen = resolver.resolve_model_for_role(stage, "gemini-2.5-pro")
            with lock:
                results.append(chosen)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert errors == []
    assert len(results) == 12
    assert all(r is not None for r in results)
    assert len(runner.calls) == 1


def test_agy_llm_adapter_retries_on_invalid_model_and_shares_resolver_in_cli(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AgyLLMAdapter substitutes failed model on invalid model selection, logs without secrets, retries up to 3 attempts, and shares ModelResolver with AgyCliAdapter in build_runtime."""
    secret_val = "SECRET_MODEL_LOG_GUARD_9911"
    models_toml = tmp_path / "models.toml"
    models_toml.write_text(
        'INTERVIEW = "gemini-2.5-flash"\n'
        'MEDIATION = "gemini-2.5-pro"\n'
        'BUILDING = "gemini-2.5-pro"\n'
        '[models]\nfast = "gemini-2.5-flash"\nstrong = "gemini-2.5-pro"\n',
        encoding="utf-8",
    )
    limits_toml = tmp_path / "limits.toml"
    limits_toml.write_text("INTERVIEW = 6\nDEBATE = 6\nMEDIATION = 3\nBUILDING = 15\nci_rounds = 3\n", encoding="utf-8")

    # 1. Runtime invalid model selection on MEDIATION -> retries with Neon (High) -> succeeds
    plan_json = json.dumps(
        {
            "summary": "App",
            "rationale": "Simple",
            "template_id": "static-web",
            "files": [{"path": "index.html", "purpose": "UI", "contract": "HTML"}],
            "acceptance_tests": [{"id": "AT-01", "description": "Works", "type": "unit"}],
            "risks": [],
            "deployment_target": "github-pages",
        }
    )
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="",
                stderr=f"{REAL_AGY_INVALID_MODEL_STDERR} token={secret_val}",
            ),
            CommandResult(exit_code=0, stdout=plan_json),
        ]
    )
    adapter = AgyLLMAdapter(
        runner=runner,
        cwd=tmp_path,
        models_config_path=models_toml,
        secrets=(secret_val,),
    )

    with caplog.at_level(logging.WARNING):
        out = adapter.complete(
            [{"role": "system", "content": "You are Mediator."}],
            schema=Plan,
        )

    assert out["summary"] == "App"
    assert len(runner.calls) == 2
    assert "--model" in runner.calls[0]["command"]
    assert "gemini-2.5-pro" in runner.calls[0]["command"]
    assert "Gemini 4 Neon (High)" in runner.calls[1]["command"]
    assert secret_val not in caplog.text
    assert "Gemini 4 Neon (High)" in caplog.text

    # 2. Exhausting 3 distinct model attempts raises LLMError after max 3 calls
    exhaust_runner = FakeRunner(
        results=[
            CommandResult(exit_code=1, stderr=REAL_AGY_INVALID_MODEL_STDERR),
            CommandResult(exit_code=429, stderr="Quota exhausted on Gemini 4 Neon (High)"),
            CommandResult(exit_code=429, stderr="Quota exhausted on Gemini 4 Argon (High)"),
            CommandResult(exit_code=0, stdout='{"should": "not be reached"}'),
        ]
    )
    exhaust_adapter = AgyLLMAdapter(
        runner=exhaust_runner,
        cwd=tmp_path,
        models_config_path=models_toml,
    )
    with pytest.raises(LLMError):
        exhaust_adapter.complete([{"role": "system", "content": "Mediator"}], schema=Plan)
    assert len(exhaust_runner.calls) == 3

    # 3. cli.build_runtime shares the exact same ModelResolver between AgyLLMAdapter and AgyCliAdapter
    for provider in ("agy", "auto"):
        env: dict[str, str] = {
            "TELEGRAM_BOT_TOKEN": "TEST_TOK_SHARED_111",
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "LLM_PROVIDER": provider,
            "AGENT_FACTORY_DB_PATH": str(tmp_path / f"{provider}.db"),
            "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / f"ws_{provider}"),
            "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
            "AGENT_FACTORY_MODELS_CONFIG": str(models_toml),
            "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
        }
        if provider == "auto":
            env["LLM_API_KEY"] = "TEST_KEY_SHARED_222"
        cfg = load_cli_config(env)
        rt = build_runtime(cfg, runner=FakeRunner())
        try:
            assert isinstance(rt.code_agent, AgyCliAdapter)
            assert rt.model_resolver is not None
            assert rt.code_agent.model_resolver is rt.model_resolver
            if isinstance(rt.llm, AgyLLMAdapter):
                assert rt.llm.model_resolver is rt.model_resolver
            elif isinstance(rt.llm, FallbackLLMAdapter):
                assert isinstance(rt.llm.fallback, AgyLLMAdapter)
                assert rt.llm.fallback.model_resolver is rt.model_resolver
        finally:
            rt.store.close()
    _ = sys.executable
