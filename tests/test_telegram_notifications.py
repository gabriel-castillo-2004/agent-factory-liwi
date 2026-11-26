"""TDD tests for clean Telegram notifications, notification levels, and DASHBOARD_PUBLIC_URL (Step 1)."""

from __future__ import annotations

import json
import types
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.telegram_client import TelegramHttpClient
from agent_factory.cli import load_cli_config
from agent_factory.config import (
    DEFAULT_DASHBOARD_PUBLIC_URL,
    DEFAULT_TELEGRAM_NOTIFICATION_LEVEL,
    FactorySettings,
    build_dashboard_url,
    normalize_dashboard_public_url,
    normalize_telegram_notification_level,
    parse_dashboard_public_url,
    parse_telegram_notification_level,
)
from agent_factory.domain.events import EventType, create_event
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import ProjectState
from agent_factory.notifications import (
    NotificationDispatcher,
    format_decision_notification,
    format_finished_notification,
    format_needs_attention_notification,
    format_progress_notification,
)
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.notifier import (
    HttpResponse,
    NotificationCategory,
    TelegramUpdate,
    resolve_notification_delivery,
)
from agent_factory.ports.runner import CommandResult
from agent_factory.recovery_agent import RecoveryAgent
from agent_factory.supervisor import RESTART_EXIT_CODE, run_supervisor
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService


class _RecordingHttpTransport:
    """Offline HttpTransport recording outbound payloads without opening sockets."""

    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
        headers: Mapping[str, str] | None = None,
        extra_secrets: tuple[str, ...] = (),
    ) -> HttpResponse:
        _ = (headers, extra_secrets)
        self.calls.append({"url": url, "payload": dict(payload), "timeout": timeout_seconds})
        if self._responses:
            return self._responses.pop(0)
        return HttpResponse(
            status_code=200,
            body=json.dumps({"ok": True, "result": {"message_id": len(self.calls), "chat": {"id": payload.get("chat_id", 0)}}}),
        )


class _FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 26, 10, 0, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        _ = seconds


def _sample_plan_dict(summary: str = "Panel de tareas") -> dict[str, Any]:
    plan = Plan(
        summary=summary,
        rationale="Static ES module app",
        template_id="static-web",
        files=[
            FileSpec(
                path="src/app.js",
                purpose="Core logic",
                contract="export function run() { return true; }",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(id="AT-01", description="Works", type=AcceptanceTestType.UNIT),
        ],
        risks=["None"],
        deployment_target="github-pages",
    )
    return plan.model_dump(mode="json")


def _complete_spec_dict(objective: str = "Panel de tareas") -> dict[str, Any]:
    return {
        "objective": objective,
        "users": ["Equipo"],
        "features": ["Crear tareas"],
        "business_rules": ["Sin duplicados"],
        "edge_cases": ["Lista vacía"],
        "constraints": ["Estático"],
        "success_criteria": ["Sin errores"],
        "assumptions": [],
    }


def test_config_loads_and_normalizes_notification_level_and_dashboard_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config parses TELEGRAM_NOTIFICATION_LEVEL and DASHBOARD_PUBLIC_URL with safe defaults and normalization."""
    assert DEFAULT_TELEGRAM_NOTIFICATION_LEVEL == "esencial"
    assert DEFAULT_DASHBOARD_PUBLIC_URL == "http://127.0.0.1:8000"

    # Notification level defaults, normalization, and invalid fallback
    assert parse_telegram_notification_level({}) == "esencial"
    assert parse_telegram_notification_level({"TELEGRAM_NOTIFICATION_LEVEL": ""}) == "esencial"
    assert parse_telegram_notification_level({"TELEGRAM_NOTIFICATION_LEVEL": "  NORMAL  "}) == "normal"
    assert parse_telegram_notification_level({"TELEGRAM_NOTIFICATION_LEVEL": "Silenciado"}) == "silenciado"
    assert parse_telegram_notification_level({"TELEGRAM_NOTIFICATION_LEVEL": "Esencial"}) == "esencial"
    assert parse_telegram_notification_level({"TELEGRAM_NOTIFICATION_LEVEL": "ruidoso"}) == "esencial"
    assert normalize_telegram_notification_level(None) == "esencial"
    assert normalize_telegram_notification_level("invalid-level") == "esencial"

    # Dashboard public URL defaults, trailing slash stripping, and project link construction
    assert parse_dashboard_public_url({}) == "http://127.0.0.1:8000"
    assert parse_dashboard_public_url({"DASHBOARD_PUBLIC_URL": "   "}) == "http://127.0.0.1:8000"
    assert parse_dashboard_public_url({"DASHBOARD_PUBLIC_URL": "///"}) == "http://127.0.0.1:8000"
    assert (
        parse_dashboard_public_url({"DASHBOARD_PUBLIC_URL": "https://panel.example.ts.net/"})
        == "https://panel.example.ts.net"
    )
    assert normalize_dashboard_public_url("https://panel.example.ts.net///") == "https://panel.example.ts.net"
    assert (
        build_dashboard_url("https://panel.example.ts.net/", project_id="mi-proyecto")
        == "https://panel.example.ts.net/?project=mi-proyecto"
    )
    assert (
        build_dashboard_url("", project_id="mi-proyecto")
        == "http://127.0.0.1:8000/?project=mi-proyecto"
    )

    # FactorySettings.from_env and direct env defaults
    settings = FactorySettings.from_env(
        {
            "TELEGRAM_NOTIFICATION_LEVEL": "  Normal ",
            "DASHBOARD_PUBLIC_URL": "https://factory.example.ts.net/",
        }
    )
    assert settings.telegram_notification_level == "normal"
    assert settings.dashboard_public_url == "https://factory.example.ts.net"

    fallback_settings = FactorySettings.from_env(
        {
            "TELEGRAM_NOTIFICATION_LEVEL": "unknown",
            "DASHBOARD_PUBLIC_URL": "",
        }
    )
    assert fallback_settings.telegram_notification_level == "esencial"
    assert fallback_settings.dashboard_public_url == "http://127.0.0.1:8000"

    monkeypatch.setenv("TELEGRAM_NOTIFICATION_LEVEL", "SILENCIADO")
    monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://env.example.ts.net/")
    env_settings = FactorySettings()
    assert env_settings.telegram_notification_level == "silenciado"
    assert env_settings.dashboard_public_url == "https://env.example.ts.net"


def test_notification_level_policy_for_esencial_normal_and_silenciado() -> None:
    """Notification policy maps (level, category) -> (should_send, disable_notification) accurately."""
    # esencial: skip intermediate, send critical with sound (disable_notification=False)
    assert resolve_notification_delivery("esencial", NotificationCategory.PROGRESS) == (False, True)
    assert resolve_notification_delivery("esencial", NotificationCategory.FINISHED) == (True, False)
    assert resolve_notification_delivery("esencial", NotificationCategory.NEEDS_ATTENTION) == (True, False)
    assert resolve_notification_delivery("esencial", NotificationCategory.DECISION) == (True, False)

    # normal: send intermediate silently (disable_notification=True), critical with sound
    assert resolve_notification_delivery("normal", NotificationCategory.PROGRESS) == (True, True)
    assert resolve_notification_delivery("normal", NotificationCategory.FINISHED) == (True, False)
    assert resolve_notification_delivery("normal", NotificationCategory.NEEDS_ATTENTION) == (True, False)
    assert resolve_notification_delivery("normal", NotificationCategory.DECISION) == (True, False)

    # silenciado: skip all automatic notifications
    for cat in NotificationCategory:
        assert resolve_notification_delivery("silenciado", cat) == (False, True)


def test_telegram_http_client_payload_includes_disable_notification_and_enforces_levels() -> None:
    """TelegramHttpClient sends disable_notification in sendMessage payload and skips HTTP calls when suppressed."""
    transport = _RecordingHttpTransport()
    client = TelegramHttpClient(
        bot_token="TEST_TOKEN_12345",
        transport=transport,
        notification_level="esencial",
    )

    # Direct send_message defaults to disable_notification=False
    msg_direct = client.send_message(chat_id=1001, text="Respuesta directa")
    assert msg_direct.disable_notification is False
    assert transport.calls[-1]["payload"]["disable_notification"] is False

    # Direct send_message with disable_notification=True
    msg_silent = client.send_message(chat_id=1001, text="Aviso silencioso", disable_notification=True)
    assert msg_silent.disable_notification is True
    assert transport.calls[-1]["payload"]["disable_notification"] is True

    calls_before = len(transport.calls)

    # In 'esencial', intermediate progress is suppressed with zero HTTP calls
    skipped_progress = client.send_notification(
        chat_id=1001,
        text="Avance intermedio",
        category=NotificationCategory.PROGRESS,
    )
    assert skipped_progress is None
    assert len(transport.calls) == calls_before

    # In 'esencial', critical categories send with disable_notification=False
    for cat, txt in (
        (NotificationCategory.FINISHED, "¡Terminado!"),
        (NotificationCategory.NEEDS_ATTENTION, "Necesita atención"),
        (NotificationCategory.DECISION, "Decisión requerida"),
    ):
        sent = client.send_notification(chat_id=1001, text=txt, category=cat)
        assert sent is not None
        assert sent.disable_notification is False
        assert transport.calls[-1]["payload"]["disable_notification"] is False

    # In 'normal', progress sends with disable_notification=True
    normal_client = TelegramHttpClient(
        bot_token="TEST_TOKEN_12345",
        transport=transport,
        notification_level="normal",
    )
    sent_progress = normal_client.send_notification(
        chat_id=1001,
        text="Avance normal",
        category=NotificationCategory.PROGRESS,
    )
    assert sent_progress is not None
    assert sent_progress.disable_notification is True
    assert transport.calls[-1]["payload"]["disable_notification"] is True

    # In 'silenciado', even critical notifications trigger zero HTTP calls
    calls_before_muted = len(transport.calls)
    muted_client = TelegramHttpClient(
        bot_token="TEST_TOKEN_12345",
        transport=transport,
        notification_level="silenciado",
    )
    for cat in NotificationCategory:
        assert muted_client.send_notification(chat_id=1001, text="Silenciado", category=cat) is None
    assert len(transport.calls) == calls_before_muted


def test_fake_telegram_client_records_disable_notification_and_filters_levels() -> None:
    """FakeTelegramClient tracks disable_notification and applies level filtering cleanly."""
    fake = FakeTelegramClient(notification_level="esencial")

    direct = fake.send_message(chat_id=1001, text="Directo", disable_notification=True)
    assert direct.disable_notification is True
    assert len(fake.sent_messages) == 1
    assert fake.sent_messages[0].disable_notification is True

    assert fake.send_notification(1001, "Avance", category=NotificationCategory.PROGRESS) is None
    assert len(fake.sent_messages) == 1
    assert len(fake.suppressed_messages) == 1

    crit = fake.send_notification(1001, "¡Terminado!", category=NotificationCategory.FINISHED)
    assert crit is not None
    assert crit.disable_notification is False
    assert len(fake.sent_messages) == 2

    fake_normal = FakeTelegramClient(notification_level="normal")
    prog = fake_normal.send_notification(1001, "Avance", category=NotificationCategory.PROGRESS)
    assert prog is not None
    assert prog.disable_notification is True
    assert len(fake_normal.sent_messages) == 1

    fake_muted = FakeTelegramClient(notification_level="silenciado")
    assert fake_muted.send_notification(1001, "Necesita atención", category=NotificationCategory.NEEDS_ATTENTION) is None
    assert len(fake_muted.sent_messages) == 0


def test_notification_templates_are_clean_concise_and_use_dashboard_public_url() -> None:
    """Notification templates produce concise Spanish messages with normalized DASHBOARD_PUBLIC_URL links and no log dumps."""
    base_with_slash = "https://panel.example.ts.net/"

    finished_msg = format_finished_notification(
        "mi-app",
        summary="Build verificado y desplegado.",
        repo_url="https://github.com/org/mi-app",
        deploy_url="https://org.github.io/mi-app",
        dashboard_url=base_with_slash,
    )
    assert "¡Terminado!" in finished_msg
    assert "mi-app" in finished_msg
    assert "Build verificado y desplegado." in finished_msg
    assert "https://panel.example.ts.net/?project=mi-app" in finished_msg
    assert "https://panel.example.ts.net//?project=" not in finished_msg

    noisy_traceback = (
        "Traceback (most recent call last):\n"
        '  File "/app/src/agent_factory/builder.py", line 123, in run\n'
        "    do_stuff()\n"
        "RuntimeError: Falló la compilación por token GITHUB_TOKEN=ghp_supersecret1234567890 "
        + ("detalle largo " * 40)
    )
    attention_msg = format_needs_attention_notification(
        "mi-app",
        cause=noisy_traceback,
        stage="BUILDING",
        dashboard_url=base_with_slash,
    )
    assert "Necesita atención" in attention_msg
    assert "mi-app" in attention_msg
    assert "Traceback (most recent call last)" not in attention_msg
    assert "ghp_supersecret1234567890" not in attention_msg
    assert "https://panel.example.ts.net/?project=mi-app" in attention_msg
    assert len(attention_msg) < 420

    decision_msg = format_decision_notification(
        "mi-app",
        prompt="Plan listo: App de tareas.",
        action_expected="Aprueba o ajusta el plan.",
        progress_pct=35,
        dashboard_url=base_with_slash,
    )
    assert "mi-app" in decision_msg
    assert "Aprueba o ajusta el plan." in decision_msg
    assert "https://panel.example.ts.net/?project=mi-app" in decision_msg

    progress_msg = format_progress_notification(
        "mi-app",
        state="BUILDING",
        summary="Generando código",
        progress_pct=50,
        dashboard_url=None,
    )
    assert "mi-app" in progress_msg
    assert "BUILDING" in progress_msg
    assert "http://127.0.0.1:8000/?project=mi-app" in progress_msg


def test_orchestrator_notifies_critical_and_intermediate_events_respecting_levels() -> None:
    """Orchestrator emits decision, progress, finished, and needs_attention notifications filtered by level."""
    # 1. Level = esencial: skips intermediate BUILDING, sends DECISION + FINISHED + NEEDS_ATTENTION with sound
    fake_essential = FakeTelegramClient(notification_level="esencial")
    dispatcher_essential = NotificationDispatcher(
        client=fake_essential,
        chat_ids=(1001,),
        notification_level="esencial",
        dashboard_public_url="https://dash.example.ts.net/",
    )
    llm = FakeLLM(
        responses=[
            {"spec": _complete_spec_dict(), "questions": []},
            {"approach": "Simple", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["OK"], "robust_proposal": "Guard", "architectural_safeguards": []},
            _sample_plan_dict("Panel limpio"),
        ]
    )
    store = MemoryEventStore()
    orch = Orchestrator(store=store, llm=llm, clock=_FakeClock(), notifier=dispatcher_essential)

    orch.create_project("proj-notif", "Panel limpio", "Panel de tareas")
    orch.handle_interview_message("proj-notif", "msg-1", "Todo listo")
    waiting = orch.run_debate("proj-notif")
    assert waiting.state == ProjectState.WAITING_APPROVAL
    assert len(fake_essential.sent_messages) == 1
    assert fake_essential.sent_messages[0].disable_notification is False
    assert "https://dash.example.ts.net/?project=proj-notif" in fake_essential.sent_messages[0].text

    # Approval moves to BUILDING (intermediate -> omitted in esencial)
    assert waiting.plan_hash is not None
    orch.submit_approval(
        "proj-notif",
        ApprovalEvent(plan_hash=waiting.plan_hash, timestamp=_FakeClock().now(), decision="approved"),
    )
    assert len(fake_essential.sent_messages) == 1

    # Intermediate transition BUILDING -> VERIFYING -> DELIVERED (only DELIVERED fires in esencial)
    store.append(
        create_event(
            "proj-notif",
            EventType.STATE_TRANSITIONED,
            {"from": ProjectState.BUILDING.value, "to": ProjectState.VERIFYING.value},
            at=_FakeClock().now(),
        )
    )
    store.append(
        create_event(
            "proj-notif",
            EventType.PROJECT_DELIVERED,
            {
                "repo_url": "https://github.com/org/proj-notif",
                "deploy_url": "https://org.github.io/proj-notif",
            },
            at=_FakeClock().now(),
        )
    )
    delivered = orch.get_status("proj-notif")
    assert delivered.state == ProjectState.DELIVERED
    assert len(fake_essential.sent_messages) == 2
    assert "¡Terminado!" in fake_essential.sent_messages[1].text
    assert fake_essential.sent_messages[1].disable_notification is False

    # Failure on a second project emits critical Necesita atención with sound
    orch.create_project("proj-fail", "App Fallida", "Probar fallo")
    orch.record_phase_failure(
        "proj-fail",
        failed_state=ProjectState.BUILDING,
        error=RuntimeError("Fallo crítico de build"),
        agent="builder",
    )
    assert len(fake_essential.sent_messages) == 3
    assert "Necesita atención" in fake_essential.sent_messages[2].text
    assert fake_essential.sent_messages[2].disable_notification is False
    assert "https://dash.example.ts.net/?project=proj-fail" in fake_essential.sent_messages[2].text

    # 2. Level = normal: intermediate BUILDING transition is sent silently (disable_notification=True)
    fake_normal = FakeTelegramClient(notification_level="normal")
    dispatcher_normal = NotificationDispatcher(
        client=fake_normal,
        chat_ids=(1001,),
        notification_level="normal",
        dashboard_public_url="https://dash.example.ts.net/",
    )
    llm_norm = FakeLLM(
        responses=[
            {"spec": _complete_spec_dict(), "questions": []},
            {"approach": "Simple", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["OK"], "robust_proposal": "Guard", "architectural_safeguards": []},
            _sample_plan_dict("Panel normal"),
        ]
    )
    orch_norm = Orchestrator(
        store=MemoryEventStore(), llm=llm_norm, clock=_FakeClock(), notifier=dispatcher_normal
    )
    orch_norm.create_project("proj-norm", "Panel normal", "Objetivo")
    orch_norm.handle_interview_message("proj-norm", "msg-n1", "Listo")
    wait_n = orch_norm.run_debate("proj-norm")
    assert wait_n.plan_hash is not None
    orch_norm.submit_approval(
        "proj-norm",
        ApprovalEvent(plan_hash=wait_n.plan_hash, timestamp=_FakeClock().now(), decision="approved"),
    )
    building_msgs = [m for m in fake_normal.sent_messages if "BUILDING" in m.text]
    assert len(building_msgs) == 1
    assert building_msgs[0].disable_notification is True


def test_recovery_agent_and_supervisor_classify_notifications_with_dashboard_url(
    tmp_path: Path,
) -> None:
    """RecoveryAgent and supervisor send critical Necesita atención with sound and intermediate progress silently in normal."""
    # RecoveryAgent factory diagnostic -> critical Necesita atención even in esencial
    fake_doc = FakeTelegramClient(notification_level="esencial")
    doc_notifier = NotificationDispatcher(
        client=fake_doc,
        chat_ids=(1001,),
        notification_level="esencial",
        dashboard_public_url="https://doc.example.ts.net/",
    )
    agent = RecoveryAgent(
        db_path=tmp_path / "lessons.db",
        runner=FakeRunner(results=[CommandResult(exit_code=1, stderr="cli unavailable")]),
        notifier=doc_notifier,
    )
    res = agent.attempt_recovery(
        project_id="proj-doc",
        stage="BUILDING",
        error=KeyError("missing_key"),
        traceback_text='Traceback (most recent call last):\n  File "src/agent_factory/builder.py", line 42, in run\nKeyError: \'missing_key\'',
    )
    assert res.recovered is False
    assert res.scope == "factory"
    assert len(fake_doc.sent_messages) == 1
    assert "Necesita atención" in fake_doc.sent_messages[0].text
    assert fake_doc.sent_messages[0].disable_notification is False
    assert "https://doc.example.ts.net/?project=proj-doc" in fake_doc.sent_messages[0].text

    # Supervisor in normal mode: exit code 42 -> silent progress; exit code 1 -> critical Necesita atención with sound
    fake_sup = FakeTelegramClient(notification_level="normal")
    sup_notifier = NotificationDispatcher(
        client=fake_sup,
        chat_ids=(1001,),
        notification_level="normal",
        dashboard_public_url="https://sup.example.ts.net/",
    )
    codes = iter([RESTART_EXIT_CODE, 1])

    def _fake_spawn(cmd: Any) -> Any:
        _ = cmd
        return types.SimpleNamespace(returncode=next(codes))

    exit_code = run_supervisor(
        spawn_fn=_fake_spawn,
        sleep_fn=lambda _s: None,
        notifier=sup_notifier,
    )
    assert exit_code == 1
    assert len(fake_sup.sent_messages) == 2
    # First: planned restart (progress, silent)
    assert fake_sup.sent_messages[0].disable_notification is True
    assert "RESTARTING" in fake_sup.sent_messages[0].text
    # Second: unexpected crash (critical, with sound)
    assert fake_sup.sent_messages[1].disable_notification is False
    assert "Necesita atención" in fake_sup.sent_messages[1].text
    assert "https://sup.example.ts.net" in fake_sup.sent_messages[1].text

    # Supervisor in esencial mode: exit code 42 skipped, exit code 2 sends only critical alert
    fake_sup_es = FakeTelegramClient(notification_level="esencial")
    sup_notifier_es = NotificationDispatcher(
        client=fake_sup_es,
        chat_ids=(1001,),
        notification_level="esencial",
        dashboard_public_url="https://sup.example.ts.net/",
    )
    codes_es = iter([RESTART_EXIT_CODE, 2])
    exit_code_es = run_supervisor(
        spawn_fn=lambda _c: types.SimpleNamespace(returncode=next(codes_es)),
        sleep_fn=lambda _s: None,
        notifier=sup_notifier_es,
    )
    assert exit_code_es == 2
    assert len(fake_sup_es.sent_messages) == 1
    assert "Necesita atención" in fake_sup_es.sent_messages[0].text
    assert fake_sup_es.sent_messages[0].disable_notification is False


def test_telegram_bot_and_cli_config_wire_dashboard_url_and_notification_level() -> None:
    """TelegramBotService uses DASHBOARD_PUBLIC_URL in decision notifications and CliConfig loads env settings."""
    cli_cfg = load_cli_config(
        {
            "TELEGRAM_BOT_TOKEN": "12345:TEST_TOKEN_CLI",
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "TELEGRAM_NOTIFICATION_LEVEL": "NORMAL",
            "DASHBOARD_PUBLIC_URL": "https://bot.example.ts.net/",
        }
    )
    assert cli_cfg.telegram_notification_level == "normal"
    assert cli_cfg.dashboard_public_url == "https://bot.example.ts.net"

    llm = FakeLLM(
        responses=[
            {"spec": _complete_spec_dict("App Bot"), "questions": []},
            {"approach": "Static", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["OK"], "robust_proposal": "Guard", "architectural_safeguards": []},
            _sample_plan_dict("App Bot Limpia"),
        ]
    )
    orch = Orchestrator(store=MemoryEventStore(), llm=llm, clock=_FakeClock())
    client = FakeTelegramClient(notification_level="esencial")
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
            notification_level="esencial",
            dashboard_public_url="https://bot.example.ts.net/",
        ),
    )

    out = bot.handle_update(
        TelegramUpdate(update_id=1, chat_id=1001, user_id=2001, text="/new App Bot")
    )
    assert out.action == "plan_ready_for_approval"
    assert "App Bot Limpia" in client.sent_messages[-1].text
    assert "https://bot.example.ts.net/?project=app-bot" in client.sent_messages[-1].text
    assert client.sent_messages[-1].disable_notification is False
