"""Tests for Telegram Long Polling client, strict ACL, Q&A/approval flow, /status, /resume, and deduplication (AC-3.1, AC-3.2, AC-3.3, INV-03, INV-04, INV-05, INV-16, INV-21, INV-22, INV-23)."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.telegram_client import TelegramHttpClient
from agent_factory.builder import BuildWorker, JobState, LocalJobQueue
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import CircuitBreaker, Orchestrator
from agent_factory.ports.code_agent import QuotaExhaustedError
from agent_factory.ports.llm import LLMError
from agent_factory.ports.notifier import (
    HttpResponse,
    InlineButton,
    SentMessage,
    TelegramClient,
    TelegramTransportError,
    TelegramUpdate,
)
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService
from agent_factory.workspace import WorkspaceManager


class _FakeClock:
    """Deterministic clock for Phase 3 offline tests."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 11, 16, 22, 10, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class _RecordingHttpTransport:
    """Scripted offline HttpTransport capturing outbound URLs and JSON payloads (zero sockets)."""

    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self._responses:
            return HttpResponse(status_code=200, body=json.dumps({"ok": True, "result": []}))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _complete_plan_payload(summary: str = "Unit converter static app") -> dict[str, Any]:
    plan = Plan(
        summary=summary,
        rationale="Pure ES modules on static-web template",
        template_id="static-web",
        files=[
            FileSpec(
                path="src/app.js",
                purpose="Core converter logic",
                contract="export function convert(v) { return v; }",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT-01",
                description="Converts base units accurately",
                type=AcceptanceTestType.UNIT,
            ),
            AcceptanceTest(
                id="AT-02",
                description="Renders cleanly without console.error",
                type=AcceptanceTestType.E2E,
            ),
        ],
        risks=["None"],
        deployment_target="github-pages",
    )
    return plan.model_dump(mode="json")


def _complete_spec_payload(objective: str = "Build unit converter") -> dict[str, Any]:
    return {
        "objective": objective,
        "users": ["Gabriel"],
        "features": ["Convert meters to feet"],
        "business_rules": ["Round to 2 decimals"],
        "edge_cases": ["Negative input"],
        "constraints": ["100% static HTML/JS"],
        "success_criteria": ["Zero console errors"],
        "assumptions": [],
    }


def _partial_spec_payload(objective: str = "Build unit converter") -> dict[str, Any]:
    return {
        "objective": objective,
        "users": [],
        "features": ["Convert units"],
        "business_rules": [],
        "edge_cases": [],
        "constraints": [],
        "success_criteria": [],
        "assumptions": [],
    }


def test_telegram_http_client_uses_outbound_long_polling_only_and_parses_updates() -> None:
    """AC-3.1 positive/negative (INV-02, INV-04, INV-23): Uses outbound getUpdates/sendMessage POSTs only, never opens listening sockets."""
    token = "TEST_BOT_TOKEN_1001"
    transport = _RecordingHttpTransport(
        responses=[
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 501,
                                "message": {
                                    "message_id": 11,
                                    "from": {"id": 2001, "username": "gabriel"},
                                    "chat": {"id": 1001},
                                    "text": "/new App de notas",
                                },
                            },
                            {
                                "update_id": 502,
                                "callback_query": {
                                    "id": "cbq-99",
                                    "from": {"id": 2001, "username": "gabriel"},
                                    "message": {"message_id": 12, "chat": {"id": 1001}},
                                    "data": "approve:app-de-notas:deadbeef",
                                },
                            },
                        ],
                    }
                ),
            ),
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "message_id": 88,
                            "chat": {"id": 1001},
                            "text": "Plan listo",
                        },
                    }
                ),
            ),
            HttpResponse(
                status_code=500,
                body=json.dumps({"ok": False, "description": "Internal Server Error"}),
            ),
        ]
    )

    client = TelegramHttpClient(bot_token=token, transport=transport)
    assert isinstance(client, TelegramClient)

    updates = client.get_updates(offset=501, timeout=25)
    assert len(updates) == 2
    assert updates[0].update_id == 501
    assert updates[0].chat_id == 1001
    assert updates[0].user_id == 2001
    assert updates[0].text == "/new App de notas"
    assert updates[1].callback_data == "approve:app-de-notas:deadbeef"
    assert updates[1].callback_query_id == "cbq-99"

    assert transport.calls[0]["url"].endswith("/getUpdates")
    assert transport.calls[0]["payload"]["offset"] == 501
    assert transport.calls[0]["payload"]["timeout"] == 25

    sent = client.send_message(
        chat_id=1001,
        text="Plan listo",
        buttons=[
            [
                InlineButton(text="Aprobar", callback_data="approve:app-de-notas:deadbeef"),
                InlineButton(text="Ajustar", callback_data="adjust:app-de-notas:deadbeef"),
            ]
        ],
    )
    assert sent.message_id == 88
    assert sent.chat_id == 1001
    assert transport.calls[1]["url"].endswith("/sendMessage")
    assert transport.calls[1]["payload"]["chat_id"] == 1001
    assert (
        transport.calls[1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["text"]
        == "Aprobar"
    )

    # Negative: non-200 HTTP response raises clean TelegramTransportError without leaking token
    with pytest.raises(TelegramTransportError) as err:
        client.get_updates(offset=503, timeout=25)
    assert token not in str(err.value)


def test_telegram_bot_enforces_strict_whitelist_on_chat_id_and_user_id() -> None:
    """AC-3.1 negative/positive (INV-21, INV-23): Unauthorized chat_id, user_id, or empty whitelist triggers zero Orchestrator/EventStore/LLM calls."""
    store = MemoryEventStore()
    llm = FakeLLM(
        responses=[
            {"spec": _complete_spec_payload(), "questions": []},
            {"approach": "Simple", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["OK"], "robust_proposal": "Guard", "architectural_safeguards": []},
            _complete_plan_payload(),
        ]
    )
    breaker = CircuitBreaker(
        limits={"INTERVIEW": 6, "DEBATE": 6, "MEDIATION": 4, "BUILDING": 15}
    )
    orch = Orchestrator(store=store, llm=llm, circuit_breaker=breaker, clock=_FakeClock())

    # 1. Fail-closed on empty whitelist
    empty_client = FakeTelegramClient(
        updates=[
            TelegramUpdate(update_id=1, chat_id=1001, user_id=2001, text="/new Should fail"),
        ]
    )
    empty_bot = TelegramBotService(
        client=empty_client,
        orchestrator=orch,
        config=TelegramBotConfig(allowed_chat_ids=frozenset(), allowed_user_ids=frozenset()),
    )
    assert empty_bot.is_authorized(1001, 2001) is False
    empty_outcomes = empty_bot.poll_once()
    assert len(empty_outcomes) == 1
    assert empty_outcomes[0].accepted is False
    assert empty_outcomes[0].action == "rejected_unauthorized"
    assert store.list_project_ids() == []
    assert len(llm.calls) == 0
    assert len(empty_client.sent_messages) == 0

    # 2. Unauthorized chat_id or unauthorized user_id inside allowed chat_id are both rejected
    client = FakeTelegramClient(
        updates=[
            TelegramUpdate(update_id=10, chat_id=9999, user_id=2001, text="/new Rogue chat"),
            TelegramUpdate(update_id=11, chat_id=1001, user_id=8888, text="/new Rogue user"),
            TelegramUpdate(update_id=12, chat_id=1001, user_id=2001, text="/new Legit app"),
        ]
    )
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    outcomes = bot.poll_once()
    assert len(outcomes) == 3
    assert outcomes[0].accepted is False
    assert outcomes[0].action == "rejected_unauthorized"
    assert outcomes[1].accepted is False
    assert outcomes[1].action == "rejected_unauthorized"
    assert outcomes[2].accepted is True
    assert outcomes[2].project_id == "legit-app"
    # Offset must advance past unauthorized updates so polling never jams
    assert bot.next_offset == 13
    assert store.list_project_ids() == ["legit-app"]
    # Only the authorized user's /new triggered the LLM
    assert len(llm.calls) == 4


def test_telegram_bot_new_project_interview_questions_reply_debate_and_approval_flow() -> None:
    """AC-3.2 positive & negative (INV-04, INV-05, INV-23): /new -> questions -> user reply -> debate -> Aprobar/Ajustar inline buttons."""
    store = MemoryEventStore()
    llm = FakeLLM(
        responses=[
            # Round 1: asks 2 clarifying questions
            {
                "spec": _partial_spec_payload("Conversor de moneda"),
                "questions": ["¿Qué monedas soportará?", "¿Requiere modo oscuro?"],
            },
            # Round 2: completes Spec after user answer
            {
                "spec": _complete_spec_payload("Conversor de moneda"),
                "questions": [],
            },
            # Debate 3 steps
            {"approach": "Static module", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Check NaN"], "robust_proposal": "Clamp", "architectural_safeguards": []},
            _complete_plan_payload("Conversor estático"),
        ]
    )
    breaker = CircuitBreaker(
        limits={"INTERVIEW": 6, "DEBATE": 6, "MEDIATION": 4, "BUILDING": 15}
    )
    orch = Orchestrator(store=store, llm=llm, circuit_breaker=breaker, clock=_FakeClock())
    client = FakeTelegramClient(
        updates=[
            # Empty /new negative case
            TelegramUpdate(update_id=20, chat_id=1001, user_id=2001, text="/new   "),
            # Valid /new starting interview
            TelegramUpdate(update_id=21, chat_id=1001, user_id=2001, text="/new Conversor de moneda"),
        ]
    )
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    first_batch = bot.poll_once()
    assert len(first_batch) == 2
    assert store.list_project_ids() == ["conversor-de-moneda"]
    assert first_batch[1].accepted is True
    assert first_batch[1].action == "interview_question_asked"
    assert orch.get_status("conversor-de-moneda").state == ProjectState.INTERVIEW
    # Bot sent questions to the user
    assert "¿Qué monedas soportará?" in client.sent_messages[-1].text

    # User replies with plain text -> completes spec and runs debate -> WAITING_APPROVAL
    reply_outcome = bot.handle_update(
        TelegramUpdate(update_id=22, chat_id=1001, user_id=2001, text="MXN y USD, sí oscuro")
    )
    assert reply_outcome.accepted is True
    assert reply_outcome.action == "plan_ready_for_approval"
    status_waiting = orch.get_status("conversor-de-moneda")
    assert status_waiting.state == ProjectState.WAITING_APPROVAL
    assert status_waiting.plan_hash is not None
    assert status_waiting.progress_pct == 35

    plan_msg = client.sent_messages[-1]
    assert "Conversor estático" in plan_msg.text
    assert len(plan_msg.buttons) >= 1
    flat_buttons = [btn for row in plan_msg.buttons for btn in row]
    btn_labels = [b.text for b in flat_buttons]
    assert "Aprobar" in btn_labels
    assert "Ajustar" in btn_labels

    # Negative INV-05: wrong plan_hash callback MUST NOT open BUILDING
    bad_cb = bot.handle_update(
        TelegramUpdate(
            update_id=23,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-bad",
            callback_data="approve:conversor-de-moneda:0000wronghash",
        )
    )
    assert bad_cb.accepted is True
    assert bad_cb.action == "approval_rejected"
    assert orch.get_status("conversor-de-moneda").state == ProjectState.WAITING_APPROVAL

    # Negative INV-05: clicking Ajustar transitions WAITING_APPROVAL -> INTERVIEW (never BUILDING)
    adjust_cb = bot.handle_update(
        TelegramUpdate(
            update_id=24,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-adj",
            callback_data=f"adjust:conversor-de-moneda:{status_waiting.plan_hash}",
        )
    )
    assert adjust_cb.accepted is True
    assert adjust_cb.action == "adjustment_requested"
    assert orch.get_status("conversor-de-moneda").state == ProjectState.INTERVIEW

    # Round 3 completes interview and re-runs debate -> WAITING_APPROVAL v2
    llm.enqueue({"spec": _complete_spec_payload("Conversor de moneda"), "questions": []})
    llm.enqueue({"approach": "Static module", "components": ["src/app.js"], "tradeoffs": []})
    llm.enqueue(
        {"critique": ["Check NaN"], "robust_proposal": "Clamp", "architectural_safeguards": []}
    )
    llm.enqueue(_complete_plan_payload("Conversor estático v2"))
    bot.handle_update(
        TelegramUpdate(update_id=25, chat_id=1001, user_id=2001, text="Añade EUR también")
    )
    status_v2 = orch.get_status("conversor-de-moneda")
    assert status_v2.state == ProjectState.WAITING_APPROVAL
    assert status_v2.plan_hash is not None

    approve_cb = bot.handle_update(
        TelegramUpdate(
            update_id=26,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-ok",
            callback_data=f"approve:conversor-de-moneda:{status_v2.plan_hash}",
        )
    )
    assert approve_cb.accepted is True
    assert orch.get_status("conversor-de-moneda").state == ProjectState.BUILDING


def test_telegram_bot_status_resume_and_update_id_deduplication(tmp_path: Path) -> None:
    """AC-3.3 positive & negative (INV-16, INV-22, INV-23): /status, /resume on PAUSED_QUOTA, and update_id deduplication."""
    clock = _FakeClock()
    store = MemoryEventStore()
    llm = FakeLLM(
        responses=[
            {"spec": _complete_spec_payload("Reloj Pomodoro"), "questions": []},
            {"approach": "Pure JS", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Timer drift"], "robust_proposal": "Delta", "architectural_safeguards": []},
            _complete_plan_payload("Reloj Pomodoro"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, clock=clock)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    code_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_app.py": "def test_at_01(): assert True\ndef test_at_02(): assert True\n"}
        ],
        impl_steps=[
            QuotaExhaustedError("CLI quota paused", retry_after_seconds=2.0),
            {"src/app.js": "export function convert(v) { return v; }\n"},
        ],
    )
    runner = FakeRunner(
        results=[CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "AT-02": True})]
    )
    vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")])
    queue = LocalJobQueue()
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=code_agent,
        runner=runner,
        vcs=vcs,
        queue=queue,
        clock=clock,
    )

    client = FakeTelegramClient()
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        build_worker=worker,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    # Create + complete spec + debate -> WAITING_APPROVAL, then duplicate update_id must be ignored!
    first = bot.handle_update(
        TelegramUpdate(update_id=100, chat_id=1001, user_id=2001, text="/new Reloj Pomodoro")
    )
    assert first.accepted is True
    assert first.project_id == "reloj-pomodoro"
    sent_before_dup = len(client.sent_messages)
    llm_calls_before_dup = len(llm.calls)

    dup = bot.handle_update(
        TelegramUpdate(update_id=100, chat_id=1001, user_id=2001, text="/new Reloj Pomodoro")
    )
    assert dup.duplicate is True
    assert dup.action == "ignored_duplicate"
    assert store.list_project_ids() == ["reloj-pomodoro"]
    assert len(client.sent_messages) == sent_before_dup
    assert len(llm.calls) == llm_calls_before_dup

    # /status during WAITING_APPROVAL
    st_waiting = bot.handle_update(
        TelegramUpdate(update_id=101, chat_id=1001, user_id=2001, text="/status reloj-pomodoro")
    )
    assert "WAITING_APPROVAL" in st_waiting.reply_text
    assert "35" in st_waiting.reply_text

    # Negative /status with invalid project_id (INV-22) and unknown project_id
    st_invalid = bot.handle_update(
        TelegramUpdate(update_id=102, chat_id=1001, user_id=2001, text="/status ../escape")
    )
    assert st_invalid.accepted is True
    assert st_invalid.action == "error"

    st_missing = bot.handle_update(
        TelegramUpdate(update_id=103, chat_id=1001, user_id=2001, text="/status no-existe")
    )
    assert st_missing.accepted is True
    assert st_missing.action == "error"

    # Negative /resume on non-building/non-paused project must not run BuildWorker
    res_bad = bot.handle_update(
        TelegramUpdate(update_id=104, chat_id=1001, user_id=2001, text="/resume reloj-pomodoro")
    )
    assert res_bad.action != "resumed"
    assert orch.get_status("reloj-pomodoro").state == ProjectState.WAITING_APPROVAL

    # Approve -> BuildWorker pauses on QuotaExhaustedError -> PAUSED_QUOTA
    plan_hash = orch.get_status("reloj-pomodoro").plan_hash
    assert plan_hash is not None
    bot.handle_update(
        TelegramUpdate(
            update_id=105,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-pom",
            callback_data=f"approve:reloj-pomodoro:{plan_hash}",
        )
    )
    job = queue.get_job("reloj-pomodoro")
    assert job is not None
    assert job.state == JobState.PAUSED_QUOTA

    # Positive /resume -> finishes build -> DELIVERED, then /status shows links & 100%
    res_ok = bot.handle_update(
        TelegramUpdate(update_id=106, chat_id=1001, user_id=2001, text="/resume reloj-pomodoro")
    )
    assert res_ok.action == "resumed"
    delivered_status = orch.get_status("reloj-pomodoro")
    assert delivered_status.state == ProjectState.DELIVERED
    assert delivered_status.progress_pct == 100

    st_done = bot.handle_update(
        TelegramUpdate(update_id=107, chat_id=1001, user_id=2001, text="/estado reloj-pomodoro")
    )
    assert "DELIVERED" in st_done.reply_text
    assert "100%" in st_done.reply_text
    assert "https://github.com/agent-factory-test/reloj-pomodoro" in st_done.reply_text
    assert "https://agent-factory-test.github.io/reloj-pomodoro" in st_done.reply_text


def test_telegram_bot_persists_dedup_and_active_project_across_restart(tmp_path: Path) -> None:
    """A3-03 (INV-16, INV-23): Restarting TelegramBotService over SqliteEventStore skips already processed update_ids and routes plain replies to active project."""
    db_path = tmp_path / "telegram_bot_persist.db"
    cfg = TelegramBotConfig(
        allowed_chat_ids=frozenset({1001}),
        allowed_user_ids=frozenset({2001}),
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": _partial_spec_payload("App Persistente"),
                "questions": ["¿Qué formato prefieres?"],
            },
            {
                "spec": _complete_spec_payload("App Persistente"),
                "questions": [],
            },
            {"approach": "Static ES", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Empty state"], "robust_proposal": "Fallback", "architectural_safeguards": []},
            _complete_plan_payload("App Persistente"),
        ]
    )

    # First run before restart
    store_1 = SqliteEventStore(db_path=db_path)
    orch_1 = Orchestrator(store=store_1, llm=llm, clock=_FakeClock())
    bot_1 = TelegramBotService(
        client=FakeTelegramClient(),
        orchestrator=orch_1,
        config=cfg,
    )
    out_new = bot_1.handle_update(
        TelegramUpdate(update_id=501, chat_id=1001, user_id=2001, text="/new App Persistente")
    )
    assert out_new.action == "interview_question_asked"
    out_st = bot_1.handle_update(
        TelegramUpdate(update_id=502, chat_id=1001, user_id=2001, text="/status app-persistente")
    )
    assert out_st.action == "status_reported"
    assert len(llm.calls) == 1
    store_1.close()

    # Simulated process restart: fresh SqliteEventStore, Orchestrator, and TelegramBotService (no shared Inbox)
    store_2 = SqliteEventStore(db_path=db_path)
    try:
        orch_2 = Orchestrator(store=store_2, llm=llm, clock=_FakeClock())
        client_2 = FakeTelegramClient(
            updates=[
                TelegramUpdate(update_id=501, chat_id=1001, user_id=2001, text="/new App Persistente"),
                TelegramUpdate(update_id=502, chat_id=1001, user_id=2001, text="/status app-persistente"),
                TelegramUpdate(
                    update_id=503,
                    chat_id=1001,
                    user_id=2001,
                    text="Gabriel, 100% estático en Markdown",
                ),
            ]
        )
        bot_2 = TelegramBotService(
            client=client_2,
            orchestrator=orch_2,
            config=cfg,
        )

        outcomes = bot_2.poll_once()
        assert len(outcomes) == 3
        assert outcomes[0].duplicate is True
        assert outcomes[0].action == "ignored_duplicate"
        assert outcomes[1].duplicate is True
        assert outcomes[1].action == "ignored_duplicate"
        assert outcomes[2].accepted is True
        assert outcomes[2].project_id == "app-persistente"
        assert outcomes[2].action == "plan_ready_for_approval"
        assert orch_2.get_status("app-persistente").state == ProjectState.WAITING_APPROVAL
        # 1 call before restart + 1 interview round 2 + 3 debate calls = 5 total (0 from duplicate 501)
        assert len(llm.calls) == 5
    finally:
        store_2.close()


def test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch(
    tmp_path: Path,
) -> None:
    """A3-03 / A3-05 (INV-04, INV-05, INV-22, INV-23): Rejects adjust with wrong hash, enforces request_adjustment precondition, checks offset across polls, answered_callbacks, COMPLETED /resume guard, and poll_once exception resilience (kills M19, M20, M26, M28)."""
    clock = _FakeClock()
    store = MemoryEventStore()
    llm = FakeLLM(
        responses=[
            # Round 1: ask 1 question to verify request_adjustment fails in INTERVIEW (M28)
            {
                "spec": _partial_spec_payload("App Resiliente"),
                "questions": ["¿Objetivo exacto?"],
            },
            # Round 2: complete spec + 3 debate calls
            {"spec": _complete_spec_payload("App Resiliente"), "questions": []},
            {"approach": "Static ES", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["OK"], "robust_proposal": "Guard", "architectural_safeguards": []},
            _complete_plan_payload("App Resiliente"),
            # For failed interviewer LLM during /new inside batch 2
            LLMError("Upstream interviewer error"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, clock=clock)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "AT-02": True})]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        queue=LocalJobQueue(),
        clock=clock,
    )
    client = FakeTelegramClient()
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        build_worker=worker,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    # 1. Start interview -> verify request_adjustment from INTERVIEW raises IllegalTransition (kills M28)
    bot.handle_update(
        TelegramUpdate(update_id=699, chat_id=1001, user_id=2001, text="/new App Resiliente")
    )
    pid = "app-resiliente"
    assert orch.get_status(pid).state == ProjectState.INTERVIEW
    with pytest.raises(IllegalTransition):
        orch.request_adjustment(pid, "Not in WAITING_APPROVAL")

    # Complete interview -> WAITING_APPROVAL
    bot.handle_update(
        TelegramUpdate(update_id=700, chat_id=1001, user_id=2001, text="Gabriel, estática")
    )
    status_wait = orch.get_status(pid)
    assert status_wait.state == ProjectState.WAITING_APPROVAL
    real_hash = status_wait.plan_hash
    assert real_hash is not None

    # 2. Wrong hash in adjust:<pid>:<wrong_hash> MUST NOT revert WAITING_APPROVAL -> INTERVIEW (INV-05)
    bad_adj = bot.handle_update(
        TelegramUpdate(
            update_id=701,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-adj-wrong",
            callback_data=f"adjust:{pid}:wronghash0000",
        )
    )
    assert bad_adj.accepted is True
    assert bad_adj.action != "adjustment_requested"
    assert orch.get_status(pid).state == ProjectState.WAITING_APPROVAL
    assert "cb-adj-wrong" in [c["callback_query_id"] for c in client.answered_callbacks]

    # Reset offset cursor cleanly and check consecutive poll_once() cursor propagation
    client.enqueue_update(
        TelegramUpdate(
            update_id=702,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-ok-1",
            callback_data=f"approve:{pid}:{real_hash}",
        )
    )
    bot.next_offset = None
    batch_1 = bot.poll_once()
    assert len(batch_1) == 1
    assert orch.get_status(pid).state == ProjectState.DELIVERED
    assert worker.queue.get_job(pid) is not None
    assert worker.queue.get_job(pid).state == JobState.COMPLETED
    # Kills M20: callback must have been acknowledged
    assert "cb-ok-1" in [c["callback_query_id"] for c in client.answered_callbacks]

    # request_adjustment from DELIVERED also raises IllegalTransition (kills M28)
    with pytest.raises(IllegalTransition):
        orch.request_adjustment(pid, "Already delivered")

    # 3. Batch 2: consecutive poll_once() must send offset=703 (kills M26) and survive every error case
    for upd in (
        TelegramUpdate(
            update_id=703,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-dup-approve",
            callback_data=f"approve:{pid}:{real_hash}",
        ),
        TelegramUpdate(
            update_id=704,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-bad-pid",
            callback_data="approve:../escape:deadbeef",
        ),
        TelegramUpdate(
            update_id=705,
            chat_id=1001,
            user_id=2001,
            text="/new App Resiliente",
        ),
        TelegramUpdate(
            update_id=706,
            chat_id=1001,
            user_id=2001,
            text=f"/resume {pid}",
        ),
        TelegramUpdate(
            update_id=707,
            chat_id=1001,
            user_id=2001,
            text="/new Falla Entrevista",
        ),
        TelegramUpdate(
            update_id=708,
            chat_id=1001,
            user_id=2001,
            text=f"/status {pid}",
        ),
    ):
        client.enqueue_update(upd)

    batch_2 = bot.poll_once()
    assert client.get_updates_calls[1]["offset"] == 703
    assert len(batch_2) == 6
    assert batch_2[0].action != "approval_processed"
    assert batch_2[1].action == "error"
    assert batch_2[2].action == "error"
    # Kills M19: COMPLETED job in queue must NOT resume
    assert batch_2[3].action != "resumed"
    assert orch.get_status(pid).state == ProjectState.DELIVERED
    # Failed Interviewer leaves falla-entrevista in FAILED without advancing to DEBATE
    assert batch_2[4].action != "plan_ready_for_approval"
    assert orch.get_status("falla-entrevista").state == ProjectState.FAILED
    # Subsequent update in the same batch still executes cleanly
    assert batch_2[5].action == "status_reported"
    assert "DELIVERED" in batch_2[5].reply_text

    bot.poll_once()
    assert client.get_updates_calls[2]["offset"] == 709


def test_telegram_http_client_handles_null_text_ok_false_invalid_json_and_answer_callback() -> None:
    """A3-05 / A3-07 (INV-04, INV-19, INV-23): Handles text=None messages, answerCallbackQuery, HTTP 200 ok=false (kills M25), and invalid JSON."""
    token = "TEST_HTTP_EDGE_TOK_9988"
    transport = _RecordingHttpTransport(
        responses=[
            HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 801,
                                "message": {
                                    "message_id": 15,
                                    "chat": {"id": 1001},
                                    "text": None,
                                },
                            },
                            {
                                "update_id": 802,
                                "message": {
                                    "message_id": 16,
                                    "from": {"id": 2001},
                                    "chat": {"id": 1001},
                                    "text": None,
                                },
                            },
                        ],
                    }
                ),
            ),
            HttpResponse(status_code=200, body=json.dumps({"ok": True, "result": True})),
            HttpResponse(
                status_code=200,
                body=json.dumps({"ok": False, "description": f"Conflict for bot{token}"}),
            ),
            HttpResponse(status_code=200, body="<<<not-valid-json>>>"),
        ]
    )
    client = TelegramHttpClient(bot_token=token, transport=transport)

    updates = client.get_updates(offset=801, timeout=15)
    assert len(updates) == 2
    assert updates[0].text == ""
    assert updates[0].user_id == 0
    assert updates[1].text == ""
    assert updates[1].user_id == 2001

    client.answer_callback_query("cbq-123", text="Procesando")
    assert transport.calls[1]["url"].endswith("/answerCallbackQuery")
    assert transport.calls[1]["payload"]["callback_query_id"] == "cbq-123"
    assert transport.calls[1]["payload"]["text"] == "Procesando"

    # HTTP 200 with ok=False must raise TelegramTransportError without token leak (kills M25)
    with pytest.raises(TelegramTransportError) as err_ok_false:
        client.get_updates(offset=803, timeout=15)
    assert token not in str(err_ok_false.value)

    # HTTP 200 with invalid JSON must raise TelegramTransportError
    with pytest.raises(TelegramTransportError):
        client.get_updates(offset=804, timeout=15)


def test_telegram_compact_buttons_approve_command_chunking_and_400_fallback(tmp_path: Path) -> None:
    """AC-3.2 / AC-3.3 / INV-04 / INV-05 / INV-16 / INV-21 / INV-23: Compact inline buttons <= 64 bytes, /approve & /aprobar command, message chunking > 4000 chars, and HTTP 400 parse_mode fallback."""
    clock = _FakeClock()
    store = MemoryEventStore()
    llm = FakeLLM(
        responses=[
            # Project 1: Long project ID for button length check
            {"spec": _complete_spec_payload("Super Sistema de Facturación y Finanzas Electrónicas"), "questions": []},
            {"approach": "Pure TS", "components": ["src/app.ts"], "tradeoffs": []},
            {"critique": ["None"], "robust_proposal": "Safe", "architectural_safeguards": []},
            _complete_plan_payload("Super Sistema de Facturación"),
            # Project 2: for /approve command tests
            {"spec": _complete_spec_payload("Cronometro Simple"), "questions": []},
            {"approach": "HTML/JS", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["None"], "robust_proposal": "Safe", "architectural_safeguards": []},
            _complete_plan_payload("Cronometro Simple"),
            # Project 3: for /aprobar with prefix test
            {"spec": _complete_spec_payload("Reloj Mundial"), "questions": []},
            {"approach": "HTML/JS", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["None"], "robust_proposal": "Safe", "architectural_safeguards": []},
            _complete_plan_payload("Reloj Mundial"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, clock=clock)
    client = FakeTelegramClient()
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    # 1. Start long-name project -> transitions to WAITING_APPROVAL on /new directly
    new_out = bot.handle_update(
        TelegramUpdate(
            update_id=1,
            chat_id=1001,
            user_id=2001,
            text="/new Super Sistema de Facturación y Finanzas Electrónicas",
        )
    )
    assert new_out.action == "plan_ready_for_approval"
    pid_1 = new_out.project_id
    assert pid_1 is not None
    assert len(pid_1) == 50

    # Verify buttons are compact: len(callback_data.encode("utf-8")) <= 64
    plan_msg = client.sent_messages[-1]
    assert len(plan_msg.buttons) == 1
    row = plan_msg.buttons[0]
    assert len(row) == 2
    approve_btn, adjust_btn = row[0], row[1]
    assert approve_btn.text == "Aprobar"
    assert adjust_btn.text == "Ajustar"

    # Strictly verify <= 64 UTF-8 bytes limit
    assert len(approve_btn.callback_data.encode("utf-8")) <= 64
    assert len(adjust_btn.callback_data.encode("utf-8")) <= 64
    assert approve_btn.callback_data.startswith("approve:")
    assert adjust_btn.callback_data.startswith("adjust:")
    hash_prefix = approve_btn.callback_data.split(":")[1]
    assert len(hash_prefix) == 12

    # 2. Compact callback approval succeeds
    cb_approve_out = bot.handle_update(
        TelegramUpdate(
            update_id=2,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-ok",
            callback_data=approve_btn.callback_data,
        )
    )
    assert cb_approve_out.accepted is True
    assert cb_approve_out.action == "approval_processed"
    status_1 = orch.get_status(pid_1)
    assert status_1.state == ProjectState.BUILDING

    # 3. Mismatched compact callback is rejected
    mismatch_cb = bot.handle_update(
        TelegramUpdate(
            update_id=3,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-bad",
            callback_data="approve:000000000000",
        )
    )
    assert mismatch_cb.accepted is True
    assert mismatch_cb.action == "approval_rejected"

    # 4. Project 2: Test /approve command without argument
    out_p2 = bot.handle_update(
        TelegramUpdate(update_id=4, chat_id=1001, user_id=2001, text="/new Cronometro Simple")
    )
    assert out_p2.action == "plan_ready_for_approval"
    status_p2 = orch.get_status("cronometro-simple")
    assert status_p2.state == ProjectState.WAITING_APPROVAL

    # /approve with wrong prefix fails
    bad_cmd_out = bot.handle_update(
        TelegramUpdate(update_id=5, chat_id=1001, user_id=2001, text="/approve wrongprefix")
    )
    assert bad_cmd_out.accepted is True
    assert bad_cmd_out.action == "approval_rejected"
    assert orch.get_status("cronometro-simple").state == ProjectState.WAITING_APPROVAL

    # /approve without argument approves active project
    ok_cmd_out = bot.handle_update(
        TelegramUpdate(update_id=6, chat_id=1001, user_id=2001, text="/approve")
    )
    assert ok_cmd_out.accepted is True
    assert ok_cmd_out.action == "approval_processed"
    assert orch.get_status("cronometro-simple").state == ProjectState.BUILDING

    # /approve on already BUILDING project rejects cleanly
    dup_approve = bot.handle_update(
        TelegramUpdate(update_id=7, chat_id=1001, user_id=2001, text="/approve")
    )
    assert dup_approve.accepted is True
    assert dup_approve.action == "approval_rejected"
    assert "no está pendiente de aprobación" in dup_approve.reply_text

    # 5. Project 3: Test /aprobar (Spanish alias) with prefix
    out_p3 = bot.handle_update(
        TelegramUpdate(update_id=8, chat_id=1001, user_id=2001, text="/new Reloj Mundial")
    )
    assert out_p3.action == "plan_ready_for_approval"
    status_p3 = orch.get_status("reloj-mundial")
    assert status_p3.state == ProjectState.WAITING_APPROVAL
    prefix_p3 = status_p3.plan_hash[:8]

    aprobar_out = bot.handle_update(
        TelegramUpdate(update_id=9, chat_id=1001, user_id=2001, text=f"/aprobar {prefix_p3}")
    )
    assert aprobar_out.accepted is True
    assert aprobar_out.action == "approval_processed"
    assert orch.get_status("reloj-mundial").state == ProjectState.BUILDING

    # 6. /approve in unauthorized chat or when chat has no active project
    empty_chat_out = bot.handle_update(
        TelegramUpdate(update_id=10, chat_id=9999, user_id=2001, text="/approve")
    )
    assert empty_chat_out.accepted is False
    assert empty_chat_out.action == "rejected_unauthorized"

    # Reset active project in test by removing from active_project_by_chat
    bot.active_project_by_chat.pop(1001, None)
    no_act_out = bot.handle_update(
        TelegramUpdate(update_id=11, chat_id=1001, user_id=2001, text="/approve")
    )
    assert no_act_out.action == "ignored"
    assert "No hay un proyecto activo" in no_act_out.reply_text

    # 7. Resilient message chunking (> 4000 chars)
    btn_sample = [[InlineButton(text="Test", callback_data="test:123")]]
    long_text = "\n".join(f"Paragraph line {i} of detailed plan explanation." for i in range(120))
    assert len(long_text) > 5000

    messages_before = len(client.sent_messages)
    sent_res = bot.send_message(chat_id=1001, text=long_text, buttons=btn_sample)
    assert sent_res == long_text

    new_messages = client.sent_messages[messages_before:]
    assert len(new_messages) >= 2
    for msg in new_messages:
        assert len(msg.text) <= 4000
    # First chunk has NO buttons, last chunk HAS buttons
    assert new_messages[0].buttons == []
    assert new_messages[-1].buttons == btn_sample

    # 8. Fallback to parse_mode=None when HTTP 400 Bad Request occurs
    class _FlakyParseModeClient:
        def __init__(self) -> None:
            self.sent_calls: list[dict[str, Any]] = []

        def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[TelegramUpdate]:
            return []

        def send_message(
            self,
            chat_id: int,
            text: str,
            buttons: Sequence[Sequence[InlineButton]] | None = None,
            parse_mode: str | None = None,
        ) -> SentMessage:
            if parse_mode == "Markdown":
                raise TelegramTransportError(
                    "Telegram API HTTP 400 for sendMessage: Bad Request: can't parse entities: Character '*' is reserved"
                )
            self.sent_calls.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
            return SentMessage(message_id=999, chat_id=chat_id, text=text, parse_mode=parse_mode)

        def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
            pass

    flaky_client = _FlakyParseModeClient()
    bot_fallback = TelegramBotService(
        client=flaky_client,
        orchestrator=orch,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    clean_sent = bot_fallback.send_message(
        chat_id=1001,
        text="*Unclosed markdown bold text",
        parse_mode="Markdown",
    )
    assert clean_sent == "*Unclosed markdown bold text"
    assert len(flaky_client.sent_calls) == 1
    assert flaky_client.sent_calls[0]["parse_mode"] is None
    assert flaky_client.sent_calls[0]["text"] == "*Unclosed markdown bold text"
