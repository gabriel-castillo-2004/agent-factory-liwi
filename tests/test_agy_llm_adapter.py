"""Unit and offline integration tests for AgyLLMAdapter, FallbackLLMAdapter, and CLI LLM provider wiring (INV-01, INV-02, INV-03, INV-18, INV-19, INV-21, INV-23)."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.agy_llm_adapter import AgyLLMAdapter, FallbackLLMAdapter
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.cli import (
    CliConfigError,
    ProductionLLMAdapter,
    build_runtime,
    load_cli_config,
)
from agent_factory.domain.models import Plan
from agent_factory.ports.llm import LLM, InvalidLLMResponseError, LLMError
from agent_factory.ports.notifier import HttpResponse
from agent_factory.ports.runner import CommandResult, CommandTimeoutError


class _StaticHttpTransport:
    """Offline scripted HttpTransport returning preconfigured responses."""

    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = list(responses)
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
                "timeout_seconds": timeout_seconds,
                "headers": dict(headers or {}),
            }
        )
        step = self._responses.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_agy_llm_adapter_enforces_json_dict_with_and_without_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgyLLMAdapter injects strict JSON object instructions and returns parsed JSON dict both with and without schema (raising on plain non-JSON text)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot123456:SHOULD_NOT_LEAK_TG")
    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-leak-host-env")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_shouldNotLeakEnvToken123")
    monkeypatch.setenv("CUSTOM_SECRET", "super-secret-val")
    monkeypatch.setenv("AGY_HOME", str(tmp_path / "agy_home"))

    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout='{"questions": ["¿Qué funcionalidades necesitas en tu CRM?"]}',
            ),
            CommandResult(
                exit_code=0,
                stdout='{"approach": "Modular JS", "components": ["ui"], "tradeoffs": []}',
            ),
            CommandResult(
                exit_code=0,
                stdout=(
                    "Aquí tienes el plan solicitado:\n"
                    "```json\n"
                    '{"summary": "Mini CRM", "template_id": "static-web", "files": []}\n'
                    "```\n"
                ),
            ),
            CommandResult(
                exit_code=0,
                stdout="¡Hola! Esto es texto plano no JSON.",
            ),
        ]
    )
    adapter = AgyLLMAdapter(runner=runner, cwd=tmp_path)
    assert isinstance(adapter, LLM)

    # 1. schema=None -> strict JSON instruction injected and returns parsed dict
    out_dict = adapter.complete(
        [
            {"role": "system", "content": "Eres entrevistador."},
            {"role": "user", "content": "Quiero un CRM simple."},
        ],
        schema=None,
    )
    assert out_dict == {"questions": ["¿Qué funcionalidades necesitas en tu CRM?"]}
    assert isinstance(out_dict, dict)

    call1 = runner.calls[0]
    assert call1["command"][0] == "agy"
    assert call1["cwd"] == tmp_path.resolve()
    prompt1 = " ".join(call1["command"])
    assert "Eres entrevistador." in prompt1
    assert "Quiero un CRM simple." in prompt1
    assert "Respond ONLY with a valid JSON object." in prompt1
    assert "JSON Schema:" not in prompt1

    env1 = call1["env"]
    assert env1 is not None
    assert "TELEGRAM_BOT_TOKEN" not in env1
    assert "LLM_API_KEY" not in env1
    assert "GITHUB_TOKEN" not in env1
    assert "CUSTOM_SECRET" not in env1
    assert env1.get("AGY_HOME") == str(tmp_path / "agy_home")

    # 2. schema dict -> clean JSON dict + schema block
    out_clean_json = adapter.complete(
        [{"role": "user", "content": "Propón arquitectura."}],
        schema={"type": "object"},
    )
    assert out_clean_json == {"approach": "Modular JS", "components": ["ui"], "tradeoffs": []}
    prompt2 = " ".join(runner.calls[1]["command"])
    assert "Respond ONLY with a valid JSON object matching the requested schema." in prompt2
    assert "JSON Schema:" in prompt2

    # 3. schema Pydantic -> markdown-fenced JSON dict + schema instructions
    out_fenced = adapter.complete(
        [
            {"role": "system", "content": "Eres mediador."},
            {"role": "user", "content": "Genera el Plan."},
        ],
        schema=Plan,
    )
    assert out_fenced == {"summary": "Mini CRM", "template_id": "static-web", "files": []}

    prompt3 = " ".join(runner.calls[2]["command"])
    assert "Respond ONLY with a valid JSON object matching the requested schema." in prompt3
    assert "acceptance_tests" in prompt3
    assert "template_id" in prompt3

    # 4. schema=None but non-JSON plain text response -> raises InvalidLLMResponseError
    with pytest.raises(InvalidLLMResponseError):
        adapter.complete([{"role": "user", "content": "Quiero algo"}], schema=None)


def test_agy_llm_adapter_errors_redact_secrets_and_clear_cause_context(tmp_path: Path) -> None:
    """Empty stdout, non-JSON/non-dict (with or without schema), exit_code != 0, timeouts, and OSError raise LLMError/InvalidLLMResponseError cleanly."""
    secret_val = "AGY_TEST_SECRET_998877"
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="",
                stderr=f"Auth failure using token={secret_val}",
            ),
            CommandTimeoutError(f"Timed out with {secret_val}"),
            CommandResult(exit_code=0, stdout="", timed_out=True),
            CommandResult(exit_code=0, stdout="   \n  "),
            OSError(f"Cannot execute agy with {secret_val}"),
            CommandResult(exit_code=0, stdout=f"Esto no es JSON válido con {secret_val}"),
            CommandResult(exit_code=0, stdout=f'["not", "a", "dict", "{secret_val}"]'),
            CommandResult(exit_code=0, stdout="   "),
        ]
    )
    adapter = AgyLLMAdapter(
        runner=runner,
        cwd=tmp_path,
        secrets=(secret_val,),
    )

    # First 6 fail even with schema=None (process/timeout/OS errors + empty output + non-JSON text)
    for idx in range(6):
        expected_cls = InvalidLLMResponseError if idx in (3, 5) else LLMError
        with pytest.raises(expected_cls) as exc_info:
            adapter.complete([{"role": "user", "content": "hola"}], schema=None)
        assert secret_val not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None

    # Remaining 2 fail with InvalidLLMResponseError when schema is provided
    for _ in range(2):
        with pytest.raises(InvalidLLMResponseError) as exc_info:
            adapter.complete([{"role": "user", "content": "hola"}], schema={"type": "object"})
        assert isinstance(exc_info.value, LLMError)
        assert secret_val not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None


def test_fallback_llm_adapter_delegates_only_on_llm_error(tmp_path: Path) -> None:
    """FallbackLLMAdapter returns primary response when healthy and switches to AgyLLMAdapter on LLMError for both text and schema calls."""
    secret_val = "FALLBACK_SECRET_554433"
    ok_transport = _StaticHttpTransport(
        responses=[
            HttpResponse(
                status_code=200,
                body=json.dumps({"choices": [{"message": {"content": '{"source": "api"}'}}]}),
            )
        ]
    )
    models_toml = tmp_path / "models.toml"
    models_toml.write_text('[models]\nfast = "fast-m"\nstrong = "strong-m"\n', encoding="utf-8")

    primary_ok = ProductionLLMAdapter(
        api_key=secret_val,
        models_config_path=models_toml,
        transport=ok_transport,
        secrets=(secret_val,),
    )
    runner_unused = FakeRunner(results=[CommandResult(exit_code=0, stdout='{"source": "agy"}')])
    fallback_unused = AgyLLMAdapter(runner=runner_unused, cwd=tmp_path, secrets=(secret_val,))
    combo_ok = FallbackLLMAdapter(primary=primary_ok, fallback=fallback_unused)

    assert combo_ok.transport is ok_transport
    assert combo_ok.complete([{"role": "user", "content": "hola"}]) == {"source": "api"}
    assert len(runner_unused.calls) == 0

    # Primary raises LLMError -> automatically falls back to AgyLLMAdapter (both schema=None text and schema!=None JSON)
    fail_transport = _StaticHttpTransport(
        responses=[
            HttpResponse(status_code=503, body="Service Unavailable"),
            HttpResponse(status_code=503, body="Service Unavailable"),
            HttpResponse(status_code=503, body="Still down"),
        ]
    )
    primary_fail = ProductionLLMAdapter(
        api_key=secret_val,
        models_config_path=models_toml,
        transport=fail_transport,
        secrets=(secret_val,),
    )
    runner_fb = FakeRunner(
        results=[
            CommandResult(exit_code=0, stdout='{"answer": "Respuesta en JSON desde agy"}'),
            CommandResult(exit_code=0, stdout='```json\n{"source": "agy", "ok": true}\n```'),
            CommandResult(exit_code=1, stdout="", stderr=f"agy crash {secret_val}"),
        ]
    )
    fallback_agy = AgyLLMAdapter(runner=runner_fb, cwd=tmp_path, secrets=(secret_val,))
    combo_fb = FallbackLLMAdapter(primary=primary_fail, fallback=fallback_agy)

    res_dict = combo_fb.complete([{"role": "user", "content": "hola"}], schema=None)
    assert res_dict == {"answer": "Respuesta en JSON desde agy"}

    res_json = combo_fb.complete([{"role": "user", "content": "hola"}], schema={"type": "object"})
    assert res_json == {"source": "agy", "ok": True}
    assert len(runner_fb.calls) == 2

    # When fallback ALSO fails, clean LLMError is raised with no __cause__/__context__ and secrets redacted
    with pytest.raises(LLMError) as both_err:
        combo_fb.complete([{"role": "user", "content": "hola"}])
    assert secret_val not in str(both_err.value)
    assert both_err.value.__cause__ is None
    assert both_err.value.__context__ is None


def test_cli_config_and_build_runtime_llm_provider_modes_and_env_example(tmp_path: Path) -> None:
    """CLI wiring supports LLM_PROVIDER=agy (no API key/models.toml), auto with/without fallback, api-only, and fail-closed invalid values."""
    tg_tok = "TEST_AGY_CLI_TG_TOK_123"
    llm_key = "TEST_AGY_CLI_KEY_456"

    limits_toml = tmp_path / "limits.toml"
    limits_toml.write_text(
        "INTERVIEW = 6\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )
    unconfigured_models = tmp_path / "unconfigured_models.toml"
    unconfigured_models.write_text('INTERVIEW = "CONFIGURE_ME"\n', encoding="utf-8")

    # 1. LLM_PROVIDER=agy works WITHOUT LLM_API_KEY and WITHOUT valid models.toml
    agy_env = {
        "TELEGRAM_BOT_TOKEN": tg_tok,
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
        "LLM_PROVIDER": "agy",
        "AGENT_FACTORY_DB_PATH": str(tmp_path / "agy.db"),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws_agy"),
        "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
        "AGENT_FACTORY_MODELS_CONFIG": str(unconfigured_models),
        "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
    }
    cfg_agy = load_cli_config(agy_env)
    assert cfg_agy.llm_provider == "agy"

    agy_runner = FakeRunner(
        results=[
            CommandResult(exit_code=0, stdout='{"greeting": "Hola desde agy"}'),
            CommandResult(exit_code=0, stdout='{"via": "agy"}'),
        ]
    )
    rt_agy = build_runtime(cfg_agy, runner=agy_runner)
    try:
        assert isinstance(rt_agy.llm, AgyLLMAdapter)
        assert rt_agy.llm.complete([{"role": "user", "content": "ping"}]) == {"greeting": "Hola desde agy"}
        assert rt_agy.llm.complete(
            [{"role": "user", "content": "ping"}],
            schema={"type": "object"},
        ) == {"via": "agy"}
        assert len(agy_runner.calls) == 2
    finally:
        rt_agy.store.close()

    # 2. LLM_PROVIDER=auto (default) + LLM_API_KEY + LLM_FALLBACK_TO_AGY=true (default) -> FallbackLLMAdapter
    valid_models = tmp_path / "valid_models.toml"
    valid_models.write_text('[models]\nfast = "m-fast"\nstrong = "m-strong"\n', encoding="utf-8")
    auto_env = {
        "TELEGRAM_BOT_TOKEN": tg_tok,
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
        "LLM_API_KEY": llm_key,
        "AGENT_FACTORY_DB_PATH": str(tmp_path / "auto.db"),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws_auto"),
        "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
        "AGENT_FACTORY_MODELS_CONFIG": str(valid_models),
        "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
    }
    cfg_auto = load_cli_config(auto_env)
    assert cfg_auto.llm_provider == "auto"
    assert cfg_auto.llm_fallback_to_agy is True

    failing_http = _StaticHttpTransport(responses=[HttpResponse(status_code=502, body="Bad Gateway")])
    fb_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout='{"via": "fallback-agy"}')])
    rt_auto = build_runtime(cfg_auto, runner=fb_runner, transport=failing_http)
    try:
        assert isinstance(rt_auto.llm, FallbackLLMAdapter)
        assert rt_auto.llm.complete(
            [{"role": "user", "content": "hola"}],
            schema={"type": "object"},
        ) == {"via": "fallback-agy"}
        assert len(failing_http.calls) == 1
        assert len(fb_runner.calls) == 1
    finally:
        rt_auto.store.close()

    # 3. LLM_PROVIDER=auto with LLM_FALLBACK_TO_AGY=false -> ProductionLLMAdapter only (no fallback)
    no_fb_env = dict(auto_env)
    no_fb_env["LLM_FALLBACK_TO_AGY"] = "false"
    no_fb_env["AGENT_FACTORY_DB_PATH"] = str(tmp_path / "nofb.db")
    cfg_no_fb = load_cli_config(no_fb_env)
    assert cfg_no_fb.llm_fallback_to_agy is False

    failing_http2 = _StaticHttpTransport(responses=[HttpResponse(status_code=502, body="Bad Gateway")])
    unused_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout='{"via": "agy"}')])
    rt_no_fb = build_runtime(cfg_no_fb, runner=unused_runner, transport=failing_http2)
    try:
        assert isinstance(rt_no_fb.llm, ProductionLLMAdapter)
        with pytest.raises(LLMError):
            rt_no_fb.llm.complete([{"role": "user", "content": "hola"}])
        assert len(unused_runner.calls) == 0
    finally:
        rt_no_fb.store.close()

    # 4. LLM_PROVIDER=api without LLM_API_KEY fails closed with CliConfigError
    api_no_key_env = dict(agy_env)
    api_no_key_env["LLM_PROVIDER"] = "api"
    api_no_key_env["AGENT_FACTORY_DB_PATH"] = str(tmp_path / "apifail.db")
    cfg_api_no_key = load_cli_config(api_no_key_env)
    with pytest.raises(CliConfigError):
        build_runtime(cfg_api_no_key)

    # 5. Invalid LLM_PROVIDER or LLM_FALLBACK_TO_AGY fail closed without leaking token
    for bad_key, bad_val in (
        ("LLM_PROVIDER", f"invalid_{tg_tok}"),
        ("LLM_FALLBACK_TO_AGY", f"maybe_{tg_tok}"),
    ):
        bad_env = dict(auto_env)
        bad_env[bad_key] = bad_val
        with pytest.raises(CliConfigError) as err_cfg:
            load_cli_config(bad_env)
        assert tg_tok not in str(err_cfg.value)
        assert err_cfg.value.__cause__ is None
        assert err_cfg.value.__context__ is None

    # 6. .env.example documents LLM_PROVIDER and LLM_FALLBACK_TO_AGY
    env_example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "LLM_PROVIDER=" in env_example
    assert "LLM_FALLBACK_TO_AGY=" in env_example


def test_agy_llm_adapter_prompt_includes_json_schema_and_skeleton_for_plan(tmp_path: Path) -> None:
    """AgyLLMAdapter injects explicit JSON Schema and example JSON skeleton containing all Plan properties and required fields."""
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout=(
                    "```json\n"
                    "{\n"
                    '  "summary": "Full CRM",\n'
                    '  "rationale": "Modular",\n'
                    '  "template_id": "static-web",\n'
                    '  "files": [{"path": "index.html", "purpose": "UI", "contract": "renders app", "depends_on": []}],\n'
                    '  "acceptance_tests": [{"id": "AT-01", "description": "Renders UI", "type": "unit"}],\n'
                    '  "risks": ["Data loss"],\n'
                    '  "deployment_target": "github-pages"\n'
                    "}\n"
                    "```"
                ),
            )
        ]
    )
    adapter = AgyLLMAdapter(runner=runner, cwd=tmp_path)
    res = adapter.complete(
        [
            {"role": "system", "content": "You are Mediator, the final debate synthesis agent."},
            {"role": "user", "content": "Reconcile Pragmatist and Structuralist positions."},
        ],
        schema=Plan,
    )
    assert res["summary"] == "Full CRM"
    assert res["template_id"] == "static-web"
    plan = Plan.model_validate(res)
    assert plan.summary == "Full CRM"
    assert len(plan.files) == 1
    assert len(plan.acceptance_tests) == 1

    call = runner.calls[0]
    prompt = " ".join(call["command"])

    # 1. Structural headings
    assert "JSON Schema:" in prompt
    assert "Example JSON Skeleton:" in prompt

    # 2. Plan top-level properties and required keys
    for key in (
        "summary",
        "rationale",
        "template_id",
        "files",
        "acceptance_tests",
        "risks",
        "deployment_target",
    ):
        assert key in prompt

    # 3. Nested FileSpec and AcceptanceTest required properties
    for key in ("path", "purpose", "contract", "depends_on", "id", "description", "type"):
        assert key in prompt

    # 4. Valid enum values and explicit template constraints
    assert "unit" in prompt
    assert "integration" in prompt
    assert "e2e" in prompt
    assert "static-app" in prompt
    assert "static-web" in prompt
    assert "AT-01" in prompt


def test_agy_llm_adapter_prompt_includes_json_schema_and_skeleton_for_interview_round_output(tmp_path: Path) -> None:
    """AgyLLMAdapter injects explicit JSON Schema and example JSON skeleton for InterviewRoundOutput containing all Spec checklist fields."""
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout=(
                    "```json\n"
                    "{\n"
                    '  "spec": {\n'
                    '    "objective": "CLI Tool",\n'
                    '    "users": ["Devs"],\n'
                    '    "features": ["Linting"],\n'
                    '    "business_rules": ["Strict"],\n'
                    '    "edge_cases": ["Empty input"],\n'
                    '    "constraints": ["No network"],\n'
                    '    "success_criteria": ["Fast"],\n'
                    '    "assumptions": []\n'
                    "  },\n"
                    '  "questions": ["What language?"]\n'
                    "}\n"
                    "```"
                ),
            )
        ]
    )
    adapter = AgyLLMAdapter(runner=runner, cwd=tmp_path)
    from agent_factory.agents.interviewer import InterviewRoundOutput

    res = adapter.complete(
        [
            {"role": "system", "content": "You are an expert product interviewer."},
            {"role": "user", "content": "I want a CLI tool."},
        ],
        schema=InterviewRoundOutput,
    )
    assert "spec" in res
    assert res["spec"]["objective"] == "CLI Tool"
    output = InterviewRoundOutput.model_validate(res)
    assert output.spec.is_complete() is True

    call = runner.calls[0]
    prompt = " ".join(call["command"])

    assert "JSON Schema:" in prompt
    assert "Example JSON Skeleton:" in prompt
    assert "spec" in prompt
    assert "questions" in prompt
    for field in (
        "objective",
        "users",
        "features",
        "business_rules",
        "edge_cases",
        "constraints",
        "success_criteria",
        "assumptions",
    ):
        assert field in prompt
