"""Tests for strict secret sanitization across Telegram, GitRegistryPublisher, and BuildWorker (AC-3.5, INV-07, INV-19, INV-23)."""

import http.client
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.telegram_client import TelegramHttpClient
from agent_factory.builder import BuildWorker
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    FileSpec,
    Plan,
    ProjectStatus,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.code_agent import CodeAgentError
from agent_factory.ports.llm import LLMError
from agent_factory.ports.notifier import (
    HttpResponse,
    TelegramTransportError,
    TelegramUpdate,
)
from agent_factory.ports.publisher import RegistryPublishError
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService
from agent_factory.workspace import WorkspaceManager

DUMMY_TG_TOKEN = "TEST_DUMMY_TG_BOT_99112233"
DUMMY_GH_TOKEN = "TEST_DUMMY_GH_PAT_88445566"
DUMMY_VC_TOKEN = "TEST_DUMMY_VC_TOK_77667788"


class _TokenFailingTransport:
    """Offline transport raising errors containing fake tokens to prove sanitization."""

    def __init__(self, error_message: str) -> None:
        self.error_message = error_message

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
    ) -> HttpResponse:
        raise RuntimeError(self.error_message)


def test_redact_secrets_strips_explicit_tokens_urls_and_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3.5 unit (INV-07, INV-19, INV-23): Redacts explicit tokens, env vars, Bearer headers, and Telegram bot URLs."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TG_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", DUMMY_GH_TOKEN)
    monkeypatch.setenv("VERCEL_TOKEN", DUMMY_VC_TOKEN)

    raw = (
        f"Failed calling https://api.telegram.org/bot123456:{DUMMY_TG_TOKEN}/getUpdates; "
        f"git remote https://x-access-token:{DUMMY_GH_TOKEN}@github.com/org/repo.git; "
        f"Authorization: Bearer {DUMMY_VC_TOKEN}; "
        f"GITHUB_TOKEN={DUMMY_GH_TOKEN}"
    )

    cleaned = redact_secrets(raw, extra_secrets=(DUMMY_TG_TOKEN, DUMMY_GH_TOKEN, DUMMY_VC_TOKEN))
    assert DUMMY_TG_TOKEN not in cleaned
    assert DUMMY_GH_TOKEN not in cleaned
    assert DUMMY_VC_TOKEN not in cleaned
    assert "[REDACTED]" in cleaned


def test_telegram_registry_and_build_worker_never_leak_tokens_on_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3.5 positive & negative (INV-07, INV-19, INV-23): Tokens never appear in Telegram exceptions/replies, RegistryPublishError, EventStore, or BuildReport."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TG_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", DUMMY_GH_TOKEN)
    monkeypatch.setenv("VERCEL_TOKEN", DUMMY_VC_TOKEN)

    # 1. TelegramHttpClient sanitizes bot_token from transport errors
    tg_client = TelegramHttpClient(
        bot_token=DUMMY_TG_TOKEN,
        transport=_TokenFailingTransport(
            f"HTTP failure at https://api.telegram.org/bot{DUMMY_TG_TOKEN}/sendMessage"
        ),
    )
    with pytest.raises(TelegramTransportError) as tg_err:
        tg_client.send_message(chat_id=1001, text="Hola")
    assert DUMMY_TG_TOKEN not in str(tg_err.value)
    assert "[REDACTED]" in str(tg_err.value)

    # 2. GitRegistryPublisher sanitizes GITHUB_TOKEN / VERCEL_TOKEN from Runner stderr
    reg_pub = GitRegistryPublisher(
        repo_path=tmp_path / "origin.git",
        branch="registry",
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=128,
                    stderr=f"fatal: auth failed for GITHUB_TOKEN={DUMMY_GH_TOKEN} and {DUMMY_VC_TOKEN}",
                )
            ]
        ),
        work_dir=tmp_path / "reg_work",
        secrets=(DUMMY_GH_TOKEN, DUMMY_VC_TOKEN),
    )
    with pytest.raises(RegistryPublishError) as reg_err:
        reg_pub.publish_project(
            ProjectStatus(project_id="proj-sec-01", state=ProjectState.DELIVERED, progress_pct=100)
        )
    assert DUMMY_GH_TOKEN not in str(reg_err.value)
    assert DUMMY_VC_TOKEN not in str(reg_err.value)
    assert "[REDACTED]" in str(reg_err.value)

    # 3. BuildWorker + EventStore + TelegramBotService never store or reply raw tokens on VCS/deploy error
    store = MemoryEventStore()
    plan = Plan(
        summary="Secret safety app",
        rationale="Static",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="Main", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="Pass", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Secret safety",
                    "users": ["Gabriel"],
                    "features": ["f1"],
                    "business_rules": ["r1"],
                    "edge_cases": ["e1"],
                    "constraints": ["c1"],
                    "success_criteria": ["s1"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "a", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    orch.create_project("proj-sec-02", "Secret Safety", "Verify redaction")
    orch.handle_interview_message("proj-sec-02", "evt-s1", "Full spec")
    waiting = orch.run_debate("proj-sec-02")
    assert waiting.plan_hash is not None
    orch.submit_approval(
        "proj-sec-02",
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 22, 20, tzinfo=UTC),
            decision="approved",
        ),
    )

    class _TokenLeakingVCS(FakeVCS):
        def merge_and_deploy(self, project_id: str, branch: str = "work") -> tuple[str, str]:
            raise RuntimeError(
                f"GitHub/Vercel error: GITHUB_TOKEN={DUMMY_GH_TOKEN} VERCEL_TOKEN={DUMMY_VC_TOKEN}"
            )

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=_TokenLeakingVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        secrets_to_redact=(DUMMY_TG_TOKEN, DUMMY_GH_TOKEN, DUMMY_VC_TOKEN),
    )
    report = worker.run_job("proj-sec-02")

    assert report.failure_cause is not None
    assert DUMMY_GH_TOKEN not in report.failure_cause
    assert DUMMY_VC_TOKEN not in report.failure_cause

    status_after = orch.get_status("proj-sec-02")
    assert status_after.state == ProjectState.FAILED
    assert status_after.failure_reason is not None
    assert DUMMY_GH_TOKEN not in status_after.failure_reason
    assert DUMMY_VC_TOKEN not in status_after.failure_reason

    events_dump = "\n".join(e.model_dump_json() for e in store.get_events("proj-sec-02"))
    assert DUMMY_GH_TOKEN not in events_dump
    assert DUMMY_VC_TOKEN not in events_dump

    # Telegram bot reporting status/failure also redacts any secret pattern
    fake_tg = FakeTelegramClient(
        updates=[TelegramUpdate(update_id=900, chat_id=1001, user_id=2001, text="/status proj-sec-02")]
    )
    bot = TelegramBotService(
        client=fake_tg,
        orchestrator=orch,
        build_worker=worker,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
            secrets_to_redact=(DUMMY_TG_TOKEN, DUMMY_GH_TOKEN, DUMMY_VC_TOKEN),
        ),
    )
    outcomes = bot.poll_once()
    assert len(outcomes) == 1
    assert DUMMY_GH_TOKEN not in outcomes[0].reply_text
    assert DUMMY_VC_TOKEN not in outcomes[0].reply_text
    assert DUMMY_TG_TOKEN not in outcomes[0].reply_text


def test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3-04 / A3-07 (INV-07, INV-19, INV-23): Pattern redaction without env/extra_secrets, env-only redaction, URL preservation, HTTPException & __cause__/__context__ scrub (kills M21, M22)."""
    tg_pat_tok = "bot123456:ABC-DEF_1234567890"
    gh_pat_tok = "ghp_" + "0000TestDummyPat000011112222"
    gh_fine_tok = "github_pat_" + "00TEST00_0000TestDummyFinePat"
    bearer_tok = "bearer_test_only_tok_9876"
    url_tok = "url_test_only_tok_5432"

    # 1. Regex patterns MUST work with env={} and extra_secrets=() (kills M21)
    raw = (
        f"tg={tg_pat_tok}; "
        f"api=https://api.telegram.org/{tg_pat_tok}/getUpdates; "
        f"gh={gh_pat_tok}; "
        f"fine={gh_fine_tok}; "
        f"git=https://x-access-token:{url_tok}@github.com/org/repo.git; "
        f"auth=Bearer {bearer_tok}; "
        "env=GITHUB_TOKEN=raw_inline_tok_123"
    )
    cleaned = redact_secrets(raw, extra_secrets=(), env={})
    assert "ABC-DEF_1234567890" not in cleaned
    assert gh_pat_tok not in cleaned
    assert gh_fine_tok not in cleaned
    assert url_tok not in cleaned
    assert bearer_tok not in cleaned
    assert "raw_inline_tok_123" not in cleaned
    assert "[REDACTED]" in cleaned

    # 2. Public URL with port and @ in path MUST NOT be corrupted
    public_url = "Visit https://example.com:8080/users/@gabriel?tab=1"
    assert redact_secrets(public_url, extra_secrets=(), env={}) == public_url

    # 3. Opaque secret in os.environ alone (no extra_secrets, no regex match) MUST be redacted (kills M22)
    opaque_env_tok = "OPAQUE_ENV_ONLY_SECRET_334455"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", opaque_env_tok)
    env_cleaned = redact_secrets(f"Failure detail containing {opaque_env_tok}")
    assert opaque_env_tok not in env_cleaned
    assert "[REDACTED]" in env_cleaned

    # 4. TelegramHttpClient catches http.client.HTTPException and scrubs __cause__ / __context__
    class _HttpExceptionTransport:
        def post_json(
            self,
            url: str,
            payload: Mapping[str, Any],
            timeout_seconds: float = 35.0,
        ) -> HttpResponse:
            raise http.client.BadStatusLine(f"https://api.telegram.org/bot{DUMMY_TG_TOKEN}/getUpdates")

    for bad_transport in (
        _HttpExceptionTransport(),
        _TokenFailingTransport(f"https://api.telegram.org/bot{DUMMY_TG_TOKEN}/sendMessage"),
    ):
        tg_client = TelegramHttpClient(bot_token=DUMMY_TG_TOKEN, transport=bad_transport)
        with pytest.raises(TelegramTransportError) as err_info:
            tg_client.get_updates()
        exc = err_info.value
        assert DUMMY_TG_TOKEN not in str(exc)
        assert exc.__cause__ is None or DUMMY_TG_TOKEN not in str(exc.__cause__)
        assert exc.__context__ is None or DUMMY_TG_TOKEN not in str(exc.__context__)
        assert DUMMY_TG_TOKEN not in "".join(traceback.format_exception(exc))


def test_secret_sanitization_across_build_worker_paths_orchestrator_and_bot_egress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3-02 / A3-04 (INV-07, INV-19, INV-23): Sanitizes secrets in vcs.push_branch, get_ci_result, code_agent, Orchestrator, and TelegramBotService egress (kills M13)."""
    sec_tok = "TEST_PATH_SECRET_778899"
    monkeypatch.setenv("GITHUB_TOKEN", sec_tok)

    def _new_approved_orch(project_id: str) -> tuple[MemoryEventStore, Orchestrator]:
        st = MemoryEventStore()
        pl = Plan(
            summary="Path secret check",
            rationale="Static",
            template_id="static-web",
            files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
            acceptance_tests=[AcceptanceTest(id="AT-01", description="Pass", type=AcceptanceTestType.UNIT)],
            risks=[],
            deployment_target="github-pages",
        )
        llm = FakeLLM(
            responses=[
                {
                    "spec": {
                        "objective": "Verify redaction",
                        "users": ["Gabriel"],
                        "features": ["f1"],
                        "business_rules": ["r1"],
                        "edge_cases": ["e1"],
                        "constraints": ["c1"],
                        "success_criteria": ["s1"],
                        "assumptions": [],
                    },
                    "questions": [],
                },
                {"approach": "a", "components": ["src/app.js"], "tradeoffs": []},
                {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
                pl.model_dump(mode="json"),
            ]
        )
        o = Orchestrator(store=st, llm=llm)
        o.create_project(project_id, "Sec Check", "Check")
        o.handle_interview_message(project_id, "ev-1", "Full spec")
        w = o.run_debate(project_id)
        assert w.plan_hash is not None
        o.submit_approval(
            project_id,
            ApprovalEvent(
                plan_hash=w.plan_hash,
                timestamp=datetime(2026, 11, 16, 22, 50, tzinfo=UTC),
                decision="approved",
            ),
        )
        return st, o

    # Case A: vcs.push_branch raises exception with secret
    class _PushFailingVCS(FakeVCS):
        def push_branch(self, project_id: str, workspace_dir: Path, branch: str = "work") -> str:
            raise RuntimeError(f"push failed: https://x-access-token:{sec_tok}@github.com/o/r.git")

    st_a, orch_a = _new_approved_orch("proj-push-sec")
    rep_a = BuildWorker(
        orchestrator=orch_a,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_a"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, test_results={"AT-01": True})]),
        vcs=_PushFailingVCS(),
        secrets_to_redact=(sec_tok,),
    ).run_job("proj-push-sec")
    assert sec_tok not in (rep_a.failure_cause or "")
    assert sec_tok not in (orch_a.get_status("proj-push-sec").failure_reason or "")
    assert sec_tok not in "\n".join(e.model_dump_json() for e in st_a.get_events("proj-push-sec"))

    # Case B: vcs.get_ci_result raises exception with secret
    class _CiFailingVCS(FakeVCS):
        def get_ci_result(self, project_id: str, branch: str = "work") -> CIRunResult:
            raise RuntimeError(f"CI API error Authorization: Bearer {sec_tok}")

    st_b, orch_b = _new_approved_orch("proj-ci-sec")
    rep_b = BuildWorker(
        orchestrator=orch_b,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_b"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, test_results={"AT-01": True})]),
        vcs=_CiFailingVCS(),
        secrets_to_redact=(sec_tok,),
    ).run_job("proj-ci-sec")
    assert sec_tok not in (rep_b.failure_cause or "")
    assert sec_tok not in "\n".join(e.model_dump_json() for e in st_b.get_events("proj-ci-sec"))

    # Case C: code_agent raises CodeAgentError with secret
    class _SecretFailingCodeAgent(FakeCodeAgent):
        def implement_or_fix(
            self,
            workspace_dir: Path,
            plan: Plan,
            failure_output: str = "",
            attempt: int = 1,
        ) -> Any:
            raise CodeAgentError(f"CLI crashed with token {sec_tok}")

    st_c, orch_c = _new_approved_orch("proj-agent-sec")
    rep_c = BuildWorker(
        orchestrator=orch_c,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_c"),
        code_agent=_SecretFailingCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
        ),
        runner=FakeRunner(),
        vcs=FakeVCS(),
        secrets_to_redact=(sec_tok,),
    ).run_job("proj-agent-sec")
    assert sec_tok not in (rep_c.failure_cause or "")
    assert sec_tok not in "\n".join(e.model_dump_json() for e in st_c.get_events("proj-agent-sec"))

    # Case D: Orchestrator interview/debate LLMError with secret never stores raw secret
    st_d = MemoryEventStore()
    orch_d = Orchestrator(
        store=st_d,
        llm=FakeLLM(responses=[LLMError(f"Provider rejected GITHUB_TOKEN={sec_tok}")]),
    )
    orch_d.create_project("proj-orch-sec", "Orch Sec", "Test")
    fail_status = orch_d.handle_interview_message("proj-orch-sec", "ev-d", "Build app")
    assert fail_status.state == ProjectState.FAILED
    assert sec_tok not in (fail_status.failure_reason or "")
    assert sec_tok not in "\n".join(e.model_dump_json() for e in st_d.get_events("proj-orch-sec"))

    # Case E: TelegramBotService egress redacts BOT_ONLY secret directly from Plan summary (kills M13)
    bot_only_secret = "BOT_ONLY_OPAQUE_EGRESS_112233"
    st_e = MemoryEventStore()
    plan_with_secret = Plan(
        summary=f"App summary leaking {bot_only_secret}",
        rationale="Static",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="Pass", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm_e = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Egress test",
                    "users": ["Gabriel"],
                    "features": ["f1"],
                    "business_rules": ["r1"],
                    "edge_cases": ["e1"],
                    "constraints": ["c1"],
                    "success_criteria": ["s1"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "a", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
            plan_with_secret.model_dump(mode="json"),
        ]
    )
    orch_e = Orchestrator(store=st_e, llm=llm_e)
    tg_e = FakeTelegramClient()
    bot_e = TelegramBotService(
        client=tg_e,
        orchestrator=orch_e,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
            secrets_to_redact=(bot_only_secret,),
        ),
    )
    bot_out = bot_e.handle_update(
        TelegramUpdate(update_id=950, chat_id=1001, user_id=2001, text="/new Egress app")
    )
    assert bot_only_secret not in bot_out.reply_text
    assert bot_only_secret not in tg_e.sent_messages[-1].text
    assert "[REDACTED]" in tg_e.sent_messages[-1].text
