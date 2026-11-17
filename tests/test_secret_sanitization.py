"""Tests for strict secret sanitization across Telegram, GitRegistryPublisher, and BuildWorker (AC-3.5, INV-07, INV-19, INV-23)."""

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
