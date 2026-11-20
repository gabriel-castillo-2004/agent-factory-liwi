"""Integration tests for the local loopback HTTP dashboard and CLI integration (INV-03, INV-07, INV-19, INV-22)."""

import http.client
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory import cli
from agent_factory.adapters.agy_llm_adapter import AgyLLMAdapter
from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.model_resolver import ModelResolver
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.telegram_client import TelegramHttpClient
from agent_factory.builder import BuildWorker, LocalJobQueue
from agent_factory.dashboard import DashboardController, DashboardHTTPServer, start_dashboard_server
from agent_factory.domain.events import EventType, create_event
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.llm import LLMError
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.scaffold import DEFAULT_TEMPLATES_ROOT, TemplateScaffold
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService
from agent_factory.workspace import SandboxViolationError, WorkspaceManager


def _spec_payload() -> dict[str, Any]:
    return {
        "spec": {
            "objective": "Pomodoro timer static app",
            "users": ["Gabriel"],
            "features": ["25 min countdown", "Start/Pause controls"],
            "business_rules": ["Never go negative"],
            "edge_cases": ["Double click start"],
            "constraints": ["Zero external backend"],
            "success_criteria": ["Zero console errors"],
            "assumptions": [],
        },
        "questions": [],
    }


def _pragmatist_payload() -> dict[str, Any]:
    return {
        "approach": "ES module timer loop",
        "components": ["src/app.js", "index.html"],
        "tradeoffs": ["In-memory state only"],
    }


def _structuralist_payload() -> dict[str, Any]:
    return {
        "critique": ["Clear existing interval on reset"],
        "robust_proposal": "Single interval guard + explicit DOM binding",
        "architectural_safeguards": ["Idempotent start button"],
    }


def _plan_payload(summary: str = "Pomodoro timer static web plan") -> dict[str, Any]:
    return Plan(
        summary=summary,
        rationale="Clean static-web architecture with safe interval handling",
        template_id="static-web",
        files=[
            FileSpec(
                path="src/app.js",
                purpose="Timer logic",
                contract="export function formatTime(sec) { return String(sec); }",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT-01",
                description="Formats countdown seconds correctly",
                type=AcceptanceTestType.UNIT,
            )
        ],
        risks=["Timer drift in background tabs"],
        deployment_target="github-pages",
    ).model_dump(mode="json")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_rejects_non_loopback_host(tmp_path: Path) -> None:
    """DashboardHTTPServer refuses binding to any non-127.0.0.1 address."""
    store = SqliteEventStore(db_path=tmp_path / "guard.db")
    try:
        orch = Orchestrator(store=store, llm=FakeLLM())
        with pytest.raises(ValueError, match="127.0.0.1"):
            DashboardHTTPServer(
                ("0.0.0.0", 0),  # type: ignore[arg-type]
                controller=None,  # type: ignore[arg-type]
            )
        _ = orch
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_http_endpoints_ui_approve_retry_and_delete(tmp_path: Path) -> None:
    """Dashboard serves self-contained UI + JSON APIs supporting inspection, retry, approve, and safe deletion."""
    secret = "DASHBOARD_TEST_SECRET_8899"
    store = SqliteEventStore(db_path=tmp_path / "dash.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    llm = FakeLLM(
        responses=[
            # 1. proj-wait -> complete pipeline to WAITING_APPROVAL
            _spec_payload(),
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("Waiting Approval Pomodoro"),
            # 2. proj-failed-debate -> interview succeeds, debate fails (3 calls)
            _spec_payload(),
            LLMError(f"Transient LLM failure token={secret}"),
            LLMError(f"Transient LLM failure token={secret}"),
            LLMError(f"Transient LLM failure token={secret}"),
            # 3. proj-failed-plan -> reaches WAITING_APPROVAL, then gets failed event
            _spec_payload(),
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("Plan Before Failure"),
            # 4. proj-failed-debate retry -> succeeds cleanly via dashboard POST /retry!
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("Recovered Debate Plan"),
        ]
    )

    orch = Orchestrator(store=store, llm=llm)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_timer.py": "def test_at_01(): assert True\n"}],
            impl_steps=[{"src/app.js": "export function formatTime(s) { return String(s); }\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(
            ci_results=[CIRunResult(passed=True, summary="CI passed")],
            repo_base_url="https://github.com/gabriel-test",
            deploy_base_url="https://gabriel-test.github.io",
        ),
        queue=LocalJobQueue(),
        secrets_to_redact=(secret,),
    )

    # Setup 4 projects
    orch.create_project("proj-wait", "Pomodoro Wait", "Waiting approval")
    orch.handle_interview_message("proj-wait", "evt-w1", "Full spec")
    wait_status = orch.run_debate("proj-wait")
    assert wait_status.state == ProjectState.WAITING_APPROVAL

    orch.create_project("proj-failed-debate", "Failed Debate", "Should retry debate")
    orch.handle_interview_message("proj-failed-debate", "evt-fd1", "Full spec")
    fd_status = orch.run_debate("proj-failed-debate")
    assert fd_status.state == ProjectState.FAILED

    orch.create_project("proj-failed-plan", "Failed With Plan", "Should retry to WAITING_APPROVAL")
    orch.handle_interview_message("proj-failed-plan", "evt-fp1", "Full spec")
    fp_wait = orch.run_debate("proj-failed-plan")
    assert fp_wait.state == ProjectState.WAITING_APPROVAL
    orch.record_phase_failure(
        "proj-failed-plan",
        failed_state=ProjectState.WAITING_APPROVAL,
        error=RuntimeError("Simulated failure after plan"),
        agent="test",
    )
    assert orch.get_status("proj-failed-plan").state == ProjectState.FAILED

    # Delivered project with workspace directory on disk
    orch.create_project("proj-done", "Delivered App", "Has repo and workspace")
    ws_done = ws_mgr.create_workspace("proj-done")
    (ws_done / "marker.txt").write_text("cleanup me", encoding="utf-8")
    store.append(
        create_event(
            "proj-done",
            EventType.STATE_TRANSITIONED,
            {"from": ProjectState.INTERVIEW, "to": ProjectState.DEBATE},
            at=datetime(2026, 11, 18, 10, 0, tzinfo=UTC),
        )
    )
    store.append(
        create_event(
            "proj-done",
            EventType.STATE_TRANSITIONED,
            {"from": ProjectState.DEBATE, "to": ProjectState.MEDIATION},
            at=datetime(2026, 11, 18, 10, 1, tzinfo=UTC),
        )
    )
    store.append(
        create_event(
            "proj-done",
            EventType.PLAN_CREATED,
            {"plan_hash": "hashdone", "to": ProjectState.WAITING_APPROVAL},
            at=datetime(2026, 11, 18, 10, 2, tzinfo=UTC),
        )
    )
    store.append(
        create_event(
            "proj-done",
            EventType.APPROVAL_RECORDED,
            {"plan_hash": "hashdone", "decision": "approved", "to": ProjectState.BUILDING},
            at=datetime(2026, 11, 18, 10, 3, tzinfo=UTC),
        )
    )
    store.append(
        create_event(
            "proj-done",
            EventType.STATE_TRANSITIONED,
            {"from": ProjectState.BUILDING, "to": ProjectState.VERIFYING},
            at=datetime(2026, 11, 18, 10, 4, tzinfo=UTC),
        )
    )
    store.append(
        create_event(
            "proj-done",
            EventType.PROJECT_DELIVERED,
            {
                "repo_url": "https://github.com/gabriel-test/proj-done",
                "deploy_url": "https://gabriel-test.github.io/proj-done",
            },
            at=datetime(2026, 11, 18, 10, 5, tzinfo=UTC),
        )
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
        secrets_to_redact=(secret,),
    )
    port = int(server.server_address[1])
    assert base_url == f"http://127.0.0.1:{port}"
    try:
        # 1. GET / -> self-contained HTML UI (no external CDNs, dark/light theme, auto-refresh, action labels)
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")
        assert "Fábrica de Proyectos" in html
        assert "Aprobar y construir" in html
        assert "Volver a intentar" in html
        assert "Reanudar proyecto" in html
        assert "El proceso se detuvo al reiniciar la aplicación." in html
        assert "Ver datos técnicos" in html
        assert "Eliminar proyecto" in html
        assert "formatFriendlyTime" in html
        assert "toLocaleTimeString" in html
        assert "es-MX" in html
        assert "Ayer," in html
        assert "Hoy," in html
        assert "5000" in html
        assert "data-theme" in html
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # 2. GET /api/projects -> summary list with state, progress, error, links, zero leaked secrets
        code, list_data = _http_json(f"{base_url}/api/projects")
        assert code == 200
        by_id = {p["project_id"]: p for p in list_data["projects"]}
        assert set(by_id) == {"proj-wait", "proj-failed-debate", "proj-failed-plan", "proj-done"}
        assert by_id["proj-wait"]["state"] == "WAITING_APPROVAL"
        assert by_id["proj-wait"]["progress_pct"] == 35
        assert by_id["proj-wait"]["is_running"] is False
        assert by_id["proj-wait"]["last_event_at"] is not None
        assert by_id["proj-done"]["state"] == "DELIVERED"
        assert by_id["proj-done"]["links"]["repo"] == "https://github.com/gabriel-test/proj-done"
        assert by_id["proj-failed-debate"]["state"] == "FAILED"
        assert by_id["proj-failed-debate"]["last_error"] is not None
        assert secret not in json.dumps(list_data)

        # 3. GET /api/projects/<id> -> detail with status, spec, plan, events
        d_code, detail = _http_json(f"{base_url}/api/projects/proj-wait")
        assert d_code == 200
        assert detail["is_running"] is False
        assert detail["last_event_at"] is not None
        assert detail["status"]["is_running"] is False
        assert detail["status"]["last_event_at"] is not None
        assert detail["spec"]["objective"] == "Pomodoro timer static app"
        assert detail["plan"]["summary"] == "Waiting Approval Pomodoro"
        assert detail["plan"]["acceptance_tests"][0]["id"] == "AT-01"
        assert len(detail["events"]) >= 4

        # Validation & 404 guards (INV-22)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            conn.request("GET", "/api/projects/..%2Fetc")
            bad_resp = conn.getresponse()
            bad_resp.read()
            assert bad_resp.status == 400

            conn.request("GET", "/api/projects/INVALID_UPPERCASE")
            bad_resp2 = conn.getresponse()
            bad_resp2.read()
            assert bad_resp2.status == 400
        finally:
            conn.close()

        nf_code, _ = _http_json(f"{base_url}/api/projects/does-not-exist")
        assert nf_code == 404

        # 4. POST /api/projects/proj-failed-plan/retry -> WAITING_APPROVAL (no background needed)
        r_plan_code, r_plan_data = _http_json(
            f"{base_url}/api/projects/proj-failed-plan/retry",
            method="POST",
        )
        assert r_plan_code == 200
        assert r_plan_data["status"]["state"] == "WAITING_APPROVAL"
        assert r_plan_data["background_started"] is False

        # 5. POST /api/projects/proj-failed-debate/retry -> triggers debate pipeline in background -> WAITING_APPROVAL
        r_deb_code, r_deb_data = _http_json(
            f"{base_url}/api/projects/proj-failed-debate/retry",
            method="POST",
        )
        assert r_deb_code == 200
        assert r_deb_data["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True
        _, fd_after = _http_json(f"{base_url}/api/projects/proj-failed-debate")
        assert fd_after["status"]["state"] == "WAITING_APPROVAL"
        assert fd_after["plan"]["summary"] == "Recovered Debate Plan"

        # 6. POST /api/projects/proj-wait/approve -> scaffolds + builds in background -> DELIVERED
        app_code, app_data = _http_json(
            f"{base_url}/api/projects/proj-wait/approve",
            method="POST",
        )
        assert app_code == 200
        assert app_data["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True
        _, wait_after = _http_json(f"{base_url}/api/projects/proj-wait")
        assert wait_after["status"]["state"] == "DELIVERED"
        assert wait_after["status"]["progress_pct"] == 100
        assert (ws_mgr.get_workspace_dir("proj-wait") / "package.json").is_file()

        # Re-approving non-WAITING_APPROVAL returns 409 Conflict
        conf_code, _ = _http_json(f"{base_url}/api/projects/proj-wait/approve", method="POST")
        assert conf_code == 409

        # 7. DELETE /api/projects/proj-done -> deletes from SQLite & removes workspaces/proj-done without touching proj-wait
        assert ws_done.exists()
        del_code, del_data = _http_json(f"{base_url}/api/projects/proj-done", method="DELETE")
        assert del_code == 200
        assert del_data["deleted"] == "proj-done"
        assert not ws_done.exists()
        assert ws_mgr.get_workspace_dir("proj-wait").exists()
        gone_code, _ = _http_json(f"{base_url}/api/projects/proj-done")
        assert gone_code == 404
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_cli_main_starts_dashboard_server_on_127_0_0_1_and_prints_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli.main starts DashboardHTTPServer on 127.0.0.1:<DASHBOARD_PORT> and shuts down cleanly."""
    models_toml = tmp_path / "models.toml"
    models_toml.write_text(
        '[models]\nfast = "acme-fast"\nstrong = "acme-strong"\n',
        encoding="utf-8",
    )
    limits_toml = tmp_path / "limits.toml"
    limits_toml.write_text(
        "INTERVIEW = 6\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )

    stop_ev = threading.Event()
    observed_ports: list[int] = []

    def _offline_get_updates(
        self: TelegramHttpClient,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[Any]:
        _ = (self, offset, timeout)
        captured_out = capsys.readouterr().out
        for line in captured_out.splitlines():
            if "http://127.0.0.1:" in line:
                url = line.split("http://127.0.0.1:", 1)[1].strip()
                port = int(url.split("/", 1)[0])
                observed_ports.append(port)
                status, data = _http_json(f"http://127.0.0.1:{port}/api/projects")
                assert status == 200
                assert data == {"projects": []}
                break
        stop_ev.set()
        return []

    monkeypatch.setattr(TelegramHttpClient, "get_updates", _offline_get_updates)

    env = {
        "TELEGRAM_BOT_TOKEN": "TEST_DASH_CLI_TOK_1122",
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
        "LLM_API_KEY": "TEST_DASH_LLM_KEY_3344",
        "AGENT_FACTORY_DB_PATH": str(tmp_path / "cli_dash.db"),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws"),
        "AGENT_FACTORY_REGISTRY_REPO": str(tmp_path / "origin.git"),
        "AGENT_FACTORY_MODELS_CONFIG": str(models_toml),
        "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
        "DASHBOARD_PORT": "0",
    }

    exit_code = cli.main([], env=env, stop_event=stop_ev)
    assert exit_code == 0
    assert len(observed_ports) == 1
    assert observed_ports[0] > 0


def test_dashboard_port_config_validation_and_secret_redaction() -> None:
    """DASHBOARD_PORT defaults to 8000, accepts 0..65535, and fails closed without leaking secrets."""
    tok = "TEST_PORT_SECRET_5566"
    base_env = {
        "TELEGRAM_BOT_TOKEN": tok,
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
    }
    assert cli.load_cli_config(base_env).dashboard_port == 8000
    assert cli.load_cli_config({**base_env, "DASHBOARD_PORT": "0"}).dashboard_port == 0
    assert cli.load_cli_config({**base_env, "DASHBOARD_PORT": "8080"}).dashboard_port == 8080

    for bad in ("-1", "70000", f"bad_{tok}"):
        with pytest.raises(cli.CliConfigError) as exc_info:
            cli.load_cli_config({**base_env, "DASHBOARD_PORT": bad})
        assert tok not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_error_routes_and_background_scaffold_failure_never_stalls(
    tmp_path: Path,
) -> None:
    """Dashboard returns proper 400/404/409 responses and records PHASE_ERROR+FAILED if background scaffold errors."""
    store = SqliteEventStore(db_path=tmp_path / "dash_err.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_err")
    llm = FakeLLM(
        responses=[
            _spec_payload(),
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("Scaffold Fail Plan"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(),
        runner=FakeRunner(),
        vcs=FakeVCS(),
        queue=LocalJobQueue(),
    )

    class _FailingScaffold(TemplateScaffold):
        def scaffold(self, project_id: str, template_id: str) -> Path:
            raise OSError("Simulated disk failure during scaffold")

    failing_scaffold = _FailingScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    orch.create_project("proj-scaf-fail", "Scaf Fail", "Obj")
    orch.handle_interview_message("proj-scaf-fail", "evt-sf1", "Spec")
    wait = orch.run_debate("proj-scaf-fail")
    assert wait.state == ProjectState.WAITING_APPROVAL

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=failing_scaffold,
        workspace_manager=ws_mgr,
    )
    try:
        # 404 unknown endpoints / projects
        c1, _ = _http_json(f"{base_url}/api/unknown")
        assert c1 == 404
        c2, _ = _http_json(f"{base_url}/api/projects/missing-id/approve", method="POST")
        assert c2 == 404
        c3, _ = _http_json(f"{base_url}/api/projects/missing-id/retry", method="POST")
        assert c3 == 404
        c4, _ = _http_json(f"{base_url}/api/projects/missing-id", method="DELETE")
        assert c4 == 404

        # 400 invalid project_id on POST / DELETE
        c5, _ = _http_json(f"{base_url}/api/projects/BAD_ID/approve", method="POST")
        assert c5 == 400
        c6, _ = _http_json(f"{base_url}/api/projects/BAD_ID", method="DELETE")
        assert c6 == 400

        # 409 retry on active non-FAILED project
        c7, _ = _http_json(f"{base_url}/api/projects/proj-scaf-fail/retry", method="POST")
        assert c7 == 409

        # Approve triggers background scaffold error -> cleanly records PHASE_ERROR & transitions to FAILED
        ok_code, ok_data = _http_json(f"{base_url}/api/projects/proj-scaf-fail/approve", method="POST")
        assert ok_code == 200
        assert ok_data["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        st_after = orch.get_status("proj-scaf-fail")
        assert st_after.state == ProjectState.FAILED
        assert "Simulated disk failure" in (st_after.failure_reason or "")
        ev_types = [e.type for e in store.get_events("proj-scaf-fail")]
        assert EventType.PHASE_ERROR in ev_types
        assert EventType.PROJECT_FAILED in ev_types
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_notifies_telegram_checks_busy_conflicts_and_corrupted_events(
    tmp_path: Path,
) -> None:
    """Dashboard controller notifies mapped Telegram chat, blocks busy mutations with 409, and tolerates corrupted events."""
    store = SqliteEventStore(db_path=tmp_path / "dash_tg.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_tg")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)
    llm = FakeLLM(
        responses=[
            _spec_payload(),
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("TG Notify Plan"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_timer.py": "def test_at_01(): assert True\n"}],
            impl_steps=[{"src/app.js": "export function formatTime(s) { return String(s); }\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(
            ci_results=[CIRunResult(passed=True, summary="CI ok")],
            repo_base_url="https://github.com/gabriel-test",
            deploy_base_url="https://gabriel-test.github.io",
        ),
        queue=LocalJobQueue(),
    )
    tg_client = FakeTelegramClient()
    bot = TelegramBotService(
        client=tg_client,
        orchestrator=orch,
        config=TelegramBotConfig(allowed_chat_ids=frozenset({1001}), allowed_user_ids=frozenset({2001})),
        build_worker=worker,
        template_scaffold=scaffold,
    )

    orch.create_project("proj-tg-notify", "Notify App", "Obj")
    bot._set_active_project(1001, "proj-tg-notify")
    orch.handle_interview_message("proj-tg-notify", "evt-t1", "Spec")
    wait = orch.run_debate("proj-tg-notify")
    assert wait.state == ProjectState.WAITING_APPROVAL

    # Corrupted event stream on a separate project should degrade safely in list_projects
    store.append(
        create_event(
            "proj-corrupt",
            EventType.AGENT_INVOKED,
            {"agent": "bad"},
            at=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
        )
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
        bot_service=bot,
    )
    try:
        code, list_res = _http_json(f"{base_url}/api/projects")
        assert code == 200
        corrupt = next(p for p in list_res["projects"] if p["project_id"] == "proj-corrupt")
        assert corrupt["state"] == "ERROR"

        # Simulate active lock on proj-tg-notify -> approve/retry/delete return 409 Conflict
        with server.controller._lock:
            server.controller._active_projects.add("proj-tg-notify")
        try:
            c_app, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify/approve", method="POST")
            c_ret, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify/retry", method="POST")
            c_del, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify", method="DELETE")
            assert (c_app, c_ret, c_del) == (409, 409, 409)
        finally:
            with server.controller._lock:
                server.controller._active_projects.discard("proj-tg-notify")

        # Unknown POST/DELETE endpoints -> 404
        c_p404, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify/unknown", method="POST")
        c_d404, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify/approve", method="DELETE")
        assert (c_p404, c_d404) == (404, 404)

        # Real approve notifies Telegram chat 1001 upon completion
        ok_code, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify/approve", method="POST")
        assert ok_code == 200
        assert server.controller.wait_for_idle(timeout=5.0) is True
        assert any("entregado con éxito" in m.text for m in tg_client.sent_messages)

        # Delete cleans up active_project_by_chat in bot_service
        d_code, _ = _http_json(f"{base_url}/api/projects/proj-tg-notify", method="DELETE")
        assert d_code == 200
        assert 1001 not in bot.active_project_by_chat
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()
    _ = DashboardController


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_orphaned_building_project_resumed_and_delivered(tmp_path: Path) -> None:
    """An orphaned project in BUILDING (from server restart) shows is_running=False and resumes cleanly."""
    store = SqliteEventStore(str(tmp_path / "orphaned_dash.db"))
    pid = "proj-orphan-bld"
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    # Simulate events leading up to BUILDING before restart
    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {"project_id": pid, "name": "Orphan App", "objective": "Obj"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.STATE_TRANSITIONED,
            {"from": "INTERVIEW", "to": "DEBATE"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.STATE_TRANSITIONED,
            {"from": "DEBATE", "to": "MEDIATION"},
            at=now,
        )
    )
    plan = Plan(
        summary="Orphan Plan",
        rationale="Testing restart",
        template_id="static-web",
        deployment_target="static",
        files=[FileSpec(path="index.html", purpose="UI", contract="Contract")],
        acceptance_tests=[AcceptanceTest(id="AT-01", type=AcceptanceTestType.UNIT, description="Pass")],
    )
    plan_dict = plan.model_dump(mode="json")
    plan_hash = plan.compute_hash()
    store.append(
        create_event(
            pid,
            EventType.PLAN_CREATED,
            {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.APPROVAL_RECORDED,
            {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"},
            at=now,
        )
    )

    orch = Orchestrator(store=store, llm=FakeLLM())
    assert orch.get_status(pid).state == ProjectState.BUILDING

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_orphan")
    runner = FakeRunner(
        results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
    )
    vcs = FakeVCS()
    code_agent = FakeCodeAgent(
        test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
        impl_steps=[{"index.html": "<!DOCTYPE html><html><body><h1>Done</h1></body></html>\n"}],
    )
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=code_agent,
        runner=runner,
        vcs=vcs,
    )
    scaffold = TemplateScaffold(
        workspace_manager=ws_mgr,
        templates_root=DEFAULT_TEMPLATES_ROOT,
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
    )
    try:
        # 1. Check GET /api/projects and GET /api/projects/<id>
        _, list_res = _http_json(f"{base_url}/api/projects")
        proj_item = next(p for p in list_res["projects"] if p["project_id"] == pid)
        assert proj_item["state"] == "BUILDING"
        assert proj_item["is_running"] is False
        assert proj_item["last_event_at"] is not None

        _, detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert detail["status"]["state"] == "BUILDING"
        assert detail["is_running"] is False
        assert detail["last_event_at"] is not None

        # 2. POST /api/projects/<id>/retry -> resumes to WAITING_APPROVAL
        ret_code, ret_res = _http_json(f"{base_url}/api/projects/{pid}/retry", method="POST")
        assert ret_code == 200
        assert ret_res["status"]["state"] == "WAITING_APPROVAL"
        assert ret_res["background_started"] is False

        # 3. POST /api/projects/<id>/approve -> starts background build and delivers project
        app_code, app_res = _http_json(f"{base_url}/api/projects/{pid}/approve", method="POST")
        assert app_code == 200
        assert app_res["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, final_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert final_detail["status"]["state"] == "DELIVERED"
        assert final_detail["status"]["progress_pct"] == 100
        assert final_detail["is_running"] is False
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_create_project_interview_message_and_plan_adjustment(tmp_path: Path) -> None:
    """Dashboard UI guards working banner strictly by is_running, creates projects, processes INTERVIEW turns to WAITING_APPROVAL, and handles plan adjustments."""
    store = SqliteEventStore(str(tmp_path / "interactive_dash.db"))

    partial_spec = {
        "spec": {
            "objective": "Calculadora de presupuestos",
            "users": ["Usuario final"],
            "features": [],
            "business_rules": [],
            "edge_cases": [],
            "constraints": [],
            "success_criteria": [],
            "assumptions": [],
        },
        "questions": ["¿Qué funciones principales necesitas?"],
    }

    round1_started = threading.Event()
    round1_release = threading.Event()

    class _InteractiveLLM(FakeLLM):
        def __init__(self, responses: list[Any]) -> None:
            super().__init__(responses=responses)
            self._call_idx = 0

        def complete(
            self,
            messages: list[dict[str, str]],
            schema: Any = None,
        ) -> dict[str, Any]:
            self._call_idx += 1
            if self._call_idx == 1:
                round1_started.set()
                assert round1_release.wait(timeout=5.0) is True
            return super().complete(messages=messages, schema=schema)

    llm = _InteractiveLLM(
        responses=[
            # 1. POST /api/projects -> round 1 interview asks 1 question
            partial_spec,
            # 2. POST /api/projects/<pid>/message -> completes spec & chains DEBATE -> WAITING_APPROVAL
            _spec_payload(),
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("Plan Web Interactivo v1"),
            # 3. POST /api/projects/<pid>/message while WAITING_APPROVAL -> adjusts plan & regenerates
            _spec_payload(),
            _pragmatist_payload(),
            _structuralist_payload(),
            _plan_payload("Plan Web Interactivo v2 ajustado"),
        ]
    )

    orch = Orchestrator(store=store, llm=llm)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
    )
    try:
        # 1. UI HTML sanity and strict honesty guards
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        for expected in (
            "+ Nuevo proyecto",
            "¿Qué quieres construir?",
            "Comenzar",
            "Esperando tus respuestas",
            "Enviar respuesta",
            "Pedir un ajuste al plan",
            "Enviar ajuste",
            "Proceso pausado",
            "Trabajando automáticamente",
            "Sin actividad desde hace varias horas.",
        ):
            assert expected in html

        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        fn_body = html[fn_start:fn_end]
        running_block, non_running_body = fn_body.split("if (st.state === 'INTERVIEW')", 1)
        assert "if (isRunning)" in running_block
        assert "Trabajando automáticamente" in running_block
        assert "Trabajando automáticamente" not in non_running_body

        interview_block, after_interview = non_running_body.split(
            "if (st.state === 'WAITING_APPROVAL')", 1
        )
        assert "Esperando tus respuestas" in interview_block
        assert "Enviar respuesta" in interview_block
        assert "Trabajando automáticamente" not in interview_block
        assert "Pedir un ajuste al plan" in after_interview
        assert "Enviar ajuste" in after_interview
        assert "st.state === 'VERIFYING'" in after_interview
        assert "Proceso pausado" in after_interview

        # 2. Invalid POST /api/projects payloads -> 400
        bad_code, _ = _http_json(f"{base_url}/api/projects", method="POST", body={"prompt": "   "})
        assert bad_code == 400
        bad_code2, _ = _http_json(f"{base_url}/api/projects", method="POST", body={})
        assert bad_code2 == 400

        # 3. POST /api/projects creates project, runs round 1 in background with is_running=True
        create_code, create_res = _http_json(
            f"{base_url}/api/projects",
            method="POST",
            body={"prompt": "Calculadora de presupuestos"},
        )
        assert create_code == 200
        pid = create_res["project_id"]
        assert pid == "calculadora-de-presupuestos"
        assert create_res["background_started"] is True

        assert round1_started.wait(timeout=5.0) is True
        _, running_list = _http_json(f"{base_url}/api/projects")
        running_entry = next(p for p in running_list["projects"] if p["project_id"] == pid)
        assert running_entry["is_running"] is True
        _, running_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert running_detail["is_running"] is True

        # Active operation rejects concurrent message with 409 Conflict
        busy_code, _ = _http_json(
            f"{base_url}/api/projects/{pid}/message",
            method="POST",
            body={"message": "Concurrent"},
        )
        assert busy_code == 409

        round1_release.set()
        assert server.controller.wait_for_idle(timeout=5.0) is True

        # 4. After round 1 completes -> INTERVIEW with is_running=False and assistant question saved
        _, int_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert int_detail["status"]["state"] == "INTERVIEW"
        assert int_detail["is_running"] is False
        assert int_detail["status"]["is_running"] is False
        last_round = next(
            ev for ev in reversed(int_detail["events"]) if ev["type"] == EventType.INTERVIEW_ROUND
        )
        assert last_round["payload"]["questions"] == ["¿Qué funciones principales necesitas?"]

        # 5. Message validation & missing project guards
        empty_msg_code, _ = _http_json(
            f"{base_url}/api/projects/{pid}/message",
            method="POST",
            body={"message": "   "},
        )
        assert empty_msg_code == 400
        missing_code, _ = _http_json(
            f"{base_url}/api/projects/no-existe/message",
            method="POST",
            body={"message": "Hola"},
        )
        assert missing_code == 404

        # 6. POST /api/projects/<pid>/message in INTERVIEW -> completes Spec & advances to WAITING_APPROVAL
        msg_code, msg_res = _http_json(
            f"{base_url}/api/projects/{pid}/message",
            method="POST",
            body={"message": "Calcula ingresos y gastos mensuales con gráficas"},
        )
        assert msg_code == 200
        assert msg_res["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, wait_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert wait_detail["status"]["state"] == "WAITING_APPROVAL"
        assert wait_detail["is_running"] is False
        assert wait_detail["plan"]["summary"] == "Plan Web Interactivo v1"
        first_plan_hash = wait_detail["status"]["plan_hash"]
        assert first_plan_hash is not None

        # 7. POST /api/projects/<pid>/message in WAITING_APPROVAL -> adjusts plan & regenerates mediation
        adj_code, adj_res = _http_json(
            f"{base_url}/api/projects/{pid}/message",
            method="POST",
            body={"message": "Agrega exportación a CSV"},
        )
        assert adj_code == 200
        assert adj_res["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, adj_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert adj_detail["status"]["state"] == "WAITING_APPROVAL"
        assert adj_detail["is_running"] is False
        assert adj_detail["plan"]["summary"] == "Plan Web Interactivo v2 ajustado"
        assert adj_detail["status"]["plan_hash"] != first_plan_hash

        # 8. Approve transitions to BUILDING; POST /message in BUILDING returns 409 Conflict
        app_code, _ = _http_json(f"{base_url}/api/projects/{pid}/approve", method="POST")
        assert app_code == 200
        conf_msg_code, _ = _http_json(
            f"{base_url}/api/projects/{pid}/message",
            method="POST",
            body={"message": "No permitido en BUILDING"},
        )
        assert conf_msg_code == 409
    finally:
        round1_release.set()
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_exposes_active_model_while_running_and_renders_history_model(
    tmp_path: Path,
) -> None:
    """Expose active_model and active_stage when is_running is true, update live in UI banner, and render model alongside Agente invocado."""
    store = SqliteEventStore(str(tmp_path / "live_model.db"))

    started_evt = threading.Event()
    release_evt = threading.Event()

    class _SlowAgyRunner(FakeRunner):
        def run(self, **kwargs: Any) -> CommandResult:
            started_evt.set()
            assert release_evt.wait(timeout=5.0) is True
            return super().run(**kwargs)

    probe_stderr = (
        "Available models:\n"
        "  - Gemini 4 Neon (High)\n"
        "  - Gemini 4 Hydrogen (Medium)\n"
        "  - Gemini 3.8 Flash (Low)\n"
        "  - Gemini 3.8 Flash (High)\n"
    )
    runner = _SlowAgyRunner(
        results=[
            CommandResult(exit_code=0, stdout=json.dumps(_spec_payload())),
            CommandResult(exit_code=0, stdout=json.dumps(_pragmatist_payload())),
            CommandResult(exit_code=0, stdout=json.dumps(_structuralist_payload())),
            CommandResult(exit_code=0, stdout=json.dumps(_plan_payload("Plan Modelo Vivo"))),
        ]
    )
    resolver = ModelResolver(runner=runner, auto_probe=False)
    resolver._discovered_models = [
        "Gemini 4 Neon (High)",
        "Gemini 4 Hydrogen (Medium)",
        "Gemini 3.8 Flash (Low)",
        "Gemini 3.8 Flash (High)",
    ]
    assert probe_stderr

    llm = AgyLLMAdapter(
        runner=runner,
        cwd=tmp_path,
        model="gemini-2.5-pro",
        model_resolver=resolver,
    )
    orch = Orchestrator(store=store, llm=llm)

    # Idle project with pre-existing AGENT_INVOKED carrying a model in payload
    orch.create_project("proj-idle-hist", "Proyecto Inactivo", "Objetivo inactivo")
    store.append(
        create_event(
            "proj-idle-hist",
            EventType.AGENT_INVOKED,
            {"state": "BUILDING", "model": "Gemini 4 Hydrogen (Medium)"},
            at=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        )
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        model_resolver=resolver,
    )
    try:
        # HTML checks for running-banner model pill and history model badge
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")

        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        running_block = html[fn_start:fn_end].split("if (st.state === 'INTERVIEW')", 1)[0]
        assert "Modelo:" in running_block
        assert "active_model" in running_block
        assert "activity_summary" in running_block
        assert "agents_count" in running_block
        assert "step_started_at" in running_block
        assert "Tiempo en este paso:" in running_block
        assert "1 agente trabajando" in running_block
        assert "2 agentes colaborando" in running_block
        assert "live-step-timer" in running_block
        assert "model-pill" in running_block
        assert "activity_detail" in running_block
        assert "pruebas aprobadas" in running_block
        assert "Es normal que tome varias rondas" in running_block
        assert "Tardó " in html

        # 5-phase grouped timeline, 5 segments, rounds, file chips, prior attempts, and tech mode
        for expected_phase in (
            "1. Idea y requisitos",
            "2. Diseño de la solución",
            "3. Aprobación del plan",
            "4. Construcción y pruebas",
            "5. Entrega final",
            "Pendiente",
            "Completado en ",
            "opacity: 0.55",
            "✓",
            "Pruebas automáticas",
            "Propuesta del especialista",
            "Crítica de estructura",
            "Etapa ",
            " de 5",
            "4000",
            "stage-segments",
            "Modo técnico",
            "af_tech_mode",
            "Ver intentos anteriores",
            "Copiar diagnóstico",
            "file-chip",
            "round-card",
        ):
            assert expected_phase in html
        assert "Siguiente paso:" not in html
        assert "progress_pct || 0) + '%)'" not in html

        # Trigger live background run -> Orchestrator + AgyLLMAdapter record activity and active_model right before runner
        code, res = _http_json(
            f"{base_url}/api/projects",
            method="POST",
            body={"prompt": "Sitio de prueba modelo"},
        )
        assert code == 200
        pid = res["project_id"]
        assert started_evt.wait(timeout=5.0) is True

        # Wait briefly until runner call triggers record_active_usage -> API reflects live activity & model
        _, proj_list = _http_json(f"{base_url}/api/projects")
        by_id = {p["project_id"]: p for p in proj_list["projects"]}
        assert by_id[pid]["is_running"] is True
        assert by_id[pid]["active_model"] == "Gemini 3.8 Flash (Low)"
        assert by_id[pid]["active_stage"] == "INTERVIEW"
        assert (
            by_id[pid]["activity_summary"]
            == "Analizando tu idea y preparando preguntas..."
        )
        assert by_id[pid]["agents_count"] == 1
        assert isinstance(by_id[pid]["step_started_at"], str)
        assert "T" in by_id[pid]["step_started_at"]

        assert by_id["proj-idle-hist"]["is_running"] is False
        assert by_id["proj-idle-hist"]["active_model"] is None
        assert by_id["proj-idle-hist"]["active_stage"] is None
        assert by_id["proj-idle-hist"]["activity_summary"] is None
        assert by_id["proj-idle-hist"]["agents_count"] is None
        assert by_id["proj-idle-hist"]["step_started_at"] is None

        _, detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert detail["is_running"] is True
        assert detail["active_model"] == "Gemini 3.8 Flash (Low)"
        assert detail["active_stage"] == "INTERVIEW"
        assert (
            detail["activity_summary"]
            == "Analizando tu idea y preparando preguntas..."
        )
        assert detail["agents_count"] == 1
        assert detail["step_started_at"] == by_id[pid]["step_started_at"]
        assert detail["status"]["active_model"] == "Gemini 3.8 Flash (Low)"
        assert detail["status"]["agents_count"] == 1

        # Live step switch while still running updates activity_summary, agents_count=2, and step_started_at
        first_started_at = detail["step_started_at"]
        resolver.record_active_usage("Gemini 4 Hydrogen (Medium)", "DEBATE")
        resolver.record_activity(
            "2 especialistas están evaluando la mejor forma de construirlo...",
            agents_count=2,
            stage="DEBATE",
        )
        _, updated_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert updated_detail["active_model"] == "Gemini 4 Hydrogen (Medium)"
        assert updated_detail["active_stage"] == "DEBATE"
        assert (
            updated_detail["activity_summary"]
            == "2 especialistas están evaluando la mejor forma de construirlo..."
        )
        assert updated_detail["agents_count"] == 2
        assert updated_detail["step_started_at"] is not None

        # Repeating exact same activity phrase keeps original step_started_at
        deb_started_at = updated_detail["step_started_at"]
        resolver.record_activity(
            "2 especialistas están evaluando la mejor forma de construirlo...",
            agents_count=2,
            stage="DEBATE",
        )
        assert resolver.get_active_activity()["step_started_at"] == deb_started_at
        assert first_started_at != "" or deb_started_at != ""

        release_evt.set()
        assert server.controller.wait_for_idle(timeout=5.0) is True

        # Once stopped (is_running == False), live fields become None and history contains duration_seconds
        _, stopped_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert stopped_detail["is_running"] is False
        assert stopped_detail["active_model"] is None
        assert stopped_detail["active_stage"] is None
        assert stopped_detail["activity_summary"] is None
        assert stopped_detail["agents_count"] is None
        assert stopped_detail["step_started_at"] is None
        interview_ev = next(
            ev
            for ev in stopped_detail["events"]
            if ev["type"] == EventType.INTERVIEW_ROUND
        )
        assert isinstance(interview_ev["payload"].get("duration_seconds"), (int, float))
        plan_ev = next(
            ev for ev in stopped_detail["events"] if ev["type"] == EventType.PLAN_CREATED
        )
        assert isinstance(plan_ev["payload"].get("duration_seconds"), (int, float))

        # Idle project retains model metadata inside AGENT_INVOKED history payload
        _, hist_detail = _http_json(f"{base_url}/api/projects/proj-idle-hist")
        invoked_ev = next(
            ev for ev in hist_detail["events"] if ev["type"] == EventType.AGENT_INVOKED
        )
        assert invoked_ev["payload"]["model"] == "Gemini 4 Hydrogen (Medium)"
    finally:
        release_evt.set()
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_retry_cleans_partial_workspace_safely_and_prevents_test_collision(
    tmp_path: Path,
) -> None:
    """Retrying FAILED/BUILDING project safely cleans partial workspace/<id> (including readonly .git files), enforces INV-22, and allows clean rebuild without tests/ collision."""
    store = SqliteEventStore(str(tmp_path / "retry_clean_ws.db"))
    pid = "proj-partial-retry"
    now = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)

    plan_obj = Plan(
        summary="Retry Clean Plan",
        rationale="Simple static site",
        template_id="static-web",
        files=[FileSpec(path="index.html", purpose="UI", contract="HTML5")],
        acceptance_tests=[
            AcceptanceTest(id="AT-01", description="Renders", type=AcceptanceTestType.UNIT)
        ],
        risks=[],
        deployment_target="github-pages",
    )
    plan_hash = plan_obj.compute_hash()
    plan_dict = plan_obj.model_dump(mode="json")

    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {"name": "Partial Retry", "objective": "App", "state": "INTERVIEW"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.STATE_TRANSITIONED,
            {"from": "INTERVIEW", "to": "DEBATE"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.STATE_TRANSITIONED,
            {"from": "DEBATE", "to": "MEDIATION"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.PLAN_CREATED,
            {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.APPROVAL_RECORDED,
            {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"},
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.PROJECT_FAILED,
            {"failed_from": "BUILDING", "reason": "BuildWorker timed out"},
            at=now,
        )
    )

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)
    # Materialize partial workspace WITH existing tests/ file AND readonly .git pack object
    partial_ws = scaffold.scaffold(pid, "static-web")
    partial_test = partial_ws / "tests" / "test_stale.py"
    partial_test.parent.mkdir(parents=True, exist_ok=True)
    partial_test.write_text("def test_stale(): assert False\n", encoding="utf-8")
    git_obj = partial_ws / ".git" / "objects" / "ab" / "123456"
    git_obj.parent.mkdir(parents=True, exist_ok=True)
    git_obj.write_bytes(b"readonly-git-object")
    os.chmod(git_obj, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    # Confirm TemplateScaffold still rejects non-empty tests/ directly (INV-13 intact)
    with pytest.raises(SandboxViolationError, match="already contains files in tests/"):
        scaffold.scaffold(pid, "static-web")

    outside_dir = tmp_path / "outside_untouched"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "keep.txt"
    outside_file.write_text("must stay", encoding="utf-8")

    # INV-22 traversal guard on WorkspaceManager.remove_workspace
    with pytest.raises(SandboxViolationError):
        ws_mgr.remove_workspace("../outside_untouched")
    assert outside_file.read_text(encoding="utf-8") == "must stay"

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_at_01(): assert True\n"}],
            impl_steps=[{"index.html": "<!DOCTYPE html><html><body><h1>OK</h1></body></html>\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(),
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
    )
    try:
        # 1. Traversal via HTTP POST /retry is rejected with 400 and leaves outside files intact
        bad_code, _ = _http_json(
            f"{base_url}/api/projects/%2E%2E%2Foutside_untouched/retry",
            method="POST",
        )
        assert bad_code == 400
        assert outside_file.read_text(encoding="utf-8") == "must stay"

        # 2. Explicit clean_workspace=True cleans partial workspace (incl. readonly .git) and enters WAITING_APPROVAL
        ret_code, ret_res = _http_json(
            f"{base_url}/api/projects/{pid}/retry",
            method="POST",
            body={"clean_workspace": True},
        )
        assert ret_code == 200
        assert ret_res["status"]["state"] == "WAITING_APPROVAL"
        assert not partial_ws.exists()

        _, retried_detail = _http_json(f"{base_url}/api/projects/{pid}")
        retried_ev = next(
            ev for ev in reversed(retried_detail["events"]) if ev["type"] == EventType.PROJECT_RETRIED
        )
        assert retried_ev["payload"]["workspace_cleaned"] is True

        # 3. Approving after retry scaffolds cleanly and finishes in DELIVERED without tests/ collision
        app_code, app_res = _http_json(f"{base_url}/api/projects/{pid}/approve", method="POST")
        assert app_code == 200
        assert app_res["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, delivered_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert delivered_detail["status"]["state"] == "DELIVERED"
        assert delivered_detail["status"]["progress_pct"] == 100
        delivered_ws = ws_mgr.get_workspace_dir(pid)
        assert delivered_ws.is_dir()

        # 4. Retry on DELIVERED project returns 409 Conflict and NEVER removes delivered workspace
        fin_ret_code, _ = _http_json(f"{base_url}/api/projects/{pid}/retry", method="POST")
        assert fin_ret_code == 409
        assert delivered_ws.is_dir()
    finally:
        # Make git file writable before pytest cleanup if test failed halfway
        if git_obj.exists():
            os.chmod(git_obj, stat.S_IWRITE | stat.S_IREAD)
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_phases_abc_rounds_file_chips_prior_attempts_and_tech_mode(
    tmp_path: Path,
) -> None:
    """Verify Phases A+B+C: rich AGENT_INVOKED round/file/tamper metadata, redacted summaries (INV-19), strict tests/ isolation (INV-13), live detail & test counts, and INV-11 legacy compat."""
    store = SqliteEventStore(str(tmp_path / "phases_abc.db"))
    secret_val = "SECRET_PHASE_B_LEAK_GUARD_42"
    pid = "proj-phases-abc"
    now = datetime(2026, 11, 19, 16, 0, tzinfo=UTC)

    plan_obj = Plan(
        summary="Timer Web App",
        rationale="Pure ES modules",
        template_id="static-web",
        files=[
            FileSpec(path="index.html", purpose="Layout", contract="HTML5"),
            FileSpec(path="src/app.js", purpose="Timer logic", contract="export function tick() {}"),
        ],
        acceptance_tests=[
            AcceptanceTest(id="AT-01", description="Renders page", type=AcceptanceTestType.UNIT),
            AcceptanceTest(id="AT-02", description="Starts timer", type=AcceptanceTestType.E2E),
        ],
        risks=[],
        deployment_target="github-pages",
    )
    plan_hash = plan_obj.compute_hash()
    plan_dict = plan_obj.model_dump(mode="json")

    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Timer", "objective": "Timer", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
    ):
        store.append(ev)

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_abc")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)
    scaffold.scaffold(pid, "static-web")

    runner = FakeRunner(
        results=[
            # Round 1: AT-01 passes, AT-02 fails
            CommandResult(
                exit_code=1,
                stdout="1 passed, 1 failed",
                test_results={"AT-01": True, "AT-02": False},
            ),
            # Round 2 is tampered -> runner is NOT invoked!
            # Round 3: both pass
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            ),
        ]
    )
    resolver = ModelResolver(runner=runner, auto_probe=False, secrets=(secret_val,))
    resolver.record_active_usage("Gemini 4 Hydrogen (Medium)", "BUILDING")

    code_agent = FakeCodeAgent(
        test_gen_steps=[
            lambda ws, _ats: (
                (ws / "tests").mkdir(parents=True, exist_ok=True),
                (ws / "tests" / "test_timer.py").write_text("def test_at_01(): assert True\n", encoding="utf-8"),
                type(
                    "Res",
                    (),
                    {
                        "files_written": ["tests/test_timer.py", "src/should_be_ignored.js"],
                        "summary": f"Generated tests with {secret_val}",
                        "model": "Gemini 4 Hydrogen (Medium)",
                    },
                )(),
            )[-1],
        ],
        impl_steps=[
            lambda ws, _p, _f, _a: (
                (ws / "src").mkdir(parents=True, exist_ok=True),
                (ws / "src" / "app.js").write_text("// v1\n", encoding="utf-8"),
                type(
                    "Res",
                    (),
                    {
                        "files_written": ["index.html", "src/app.js"],
                        "summary": f"First build with token={secret_val}",
                        "model": "Gemini 4 Hydrogen (Medium)",
                    },
                )(),
            )[-1],
            # Attempt 2 tampers tests/ -> must be reverted and flagged outcome=reverted_tamper
            lambda ws, _p, _f, _a: (
                (ws / "tests" / "test_timer.py").write_text("def test_at_01(): pass\n", encoding="utf-8"),
                type(
                    "Res",
                    (),
                    {
                        "files_written": ["tests/test_timer.py"],
                        "summary": "Tried weakening tests",
                        "model": "Gemini 4 Hydrogen (Medium)",
                    },
                )(),
            )[-1],
            lambda ws, _p, _f, _a: (
                (ws / "src" / "app.js").write_text("export function tick() { return 1; }\n", encoding="utf-8"),
                type(
                    "Res",
                    (),
                    {
                        "files_written": ["src/app.js"],
                        "summary": "Fixed timer start button",
                        "model": "Gemini 4 Hydrogen (Medium)",
                    },
                )(),
            )[-1],
        ],
    )
    # Attach model_resolver so BuildWorker records live activity & tests counts on it
    code_agent.model_resolver = resolver  # type: ignore[attr-defined]

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=code_agent,
        runner=runner,
        vcs=FakeVCS(),
        secrets_to_redact=(secret_val,),
    )
    report = worker.run_job(pid)
    assert report.failure_cause is None
    assert orch.get_status(pid).state == ProjectState.DELIVERED

    invoked_events = [
        ev for ev in store.get_events(pid) if ev.type == EventType.AGENT_INVOKED
    ]
    assert len(invoked_events) == 4  # 1 gen_tests + 3 impl rounds

    # gen_tests files_written MUST strictly obey INV-13 (only tests/ paths, redacted)
    gen_pl = invoked_events[0].payload
    assert gen_pl["agent"] == "generate_tests"
    assert gen_pl["files_written"] == ["tests/test_timer.py"]
    assert secret_val not in str(gen_pl)

    # Round 1: round=1, files_written, redacted summary_short, no failing_tests yet
    r1_pl = invoked_events[1].payload
    assert r1_pl["agent"] == "implement_or_fix"
    assert r1_pl["round"] == 1
    assert r1_pl["max_rounds"] >= 14
    assert r1_pl["files_written"] == ["index.html", "src/app.js"]
    assert secret_val not in r1_pl["summary_short"]
    assert r1_pl["outcome"] == "ok"

    # Round 2 (tampered): round=2, failing_tests recorded from round 1, outcome="reverted_tamper"
    r2_pl = invoked_events[2].payload
    assert r2_pl["round"] == 2
    assert r2_pl["failing_tests"] == ["AT-02"]
    assert r2_pl["outcome"] == "reverted_tamper"

    # Round 3: round=3, outcome="ok"
    r3_pl = invoked_events[3].payload
    assert r3_pl["round"] == 3
    assert r3_pl["outcome"] == "ok"

    # Verify ModelResolver live detail + test progress API exposure when running
    resolver.record_activity(
        "Corrigiendo 1 prueba pendiente...",
        agents_count=1,
        stage="BUILDING",
        detail="Archivos: src/app.js · Ronda 3 de hasta 14",
        tests_passed=1,
        tests_total=2,
        build_round=3,
        max_rounds=14,
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
        model_resolver=resolver,
        secrets_to_redact=(secret_val,),
    )
    try:
        # Mark running temporarily to verify /api/projects/<pid> exposes live Phase C fields
        server.controller.mark_running(pid)
        _, live_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert live_detail["activity_detail"] == "Archivos: src/app.js · Ronda 3 de hasta 14"
        assert live_detail["tests_passed"] == 1
        assert live_detail["tests_total"] == 2
        assert live_detail["build_round"] == 3
        assert live_detail["max_rounds"] == 14
        server.controller.mark_stopped(pid)

        _, idle_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert idle_detail["activity_detail"] is None
        assert idle_detail["tests_passed"] is None
        assert idle_detail["tests_total"] is None
        assert secret_val not in json.dumps(idle_detail)

        # Verify UI embeds sanitizeDisplaySummary defense and failed round reason in Spanish
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            html_body = resp.read().decode("utf-8")
        assert "sanitizeDisplaySummary" in html_body
        assert "Falló la verificación de pruebas" in html_body
        assert "ModuleNotFoundError" in html_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_incremental_retry_preserves_workspace_and_supports_clean_rebuild(
    tmp_path: Path,
) -> None:
    """Default retry (clean_workspace=False) preserves tests/ and prior src/, reuses existing tests without calling generate_tests, and Dashboard offers 'Reconstruir desde cero'."""
    store = SqliteEventStore(str(tmp_path / "incr_retry.db"))
    pid = "proj-incr-dash"
    now = datetime(2026, 11, 19, 17, 15, tzinfo=UTC)
    plan_dict = _plan_payload("Incremental App")
    plan_obj = Plan.model_validate(plan_dict)
    plan_hash = plan_obj.compute_hash()

    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Incr", "objective": "App", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.PROJECT_FAILED, {"failed_from": "BUILDING", "reason": "Timeout midway"}, at=now),
    ):
        store.append(ev)

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_incr_dash")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)
    ws_dir = scaffold.scaffold(pid, "static-web")
    existing_test = "def test_at_01(): assert True\n"
    (ws_dir / "tests").mkdir(parents=True, exist_ok=True)
    (ws_dir / "tests" / "test_app.py").write_text(existing_test, encoding="utf-8")
    (ws_dir / "src").mkdir(parents=True, exist_ok=True)
    (ws_dir / "src" / "app.js").write_text("// partial progress\n", encoding="utf-8")

    seen_prior_js: list[str] = []

    def _resume_impl(ws: Path, _p, _f, _a):
        seen_prior_js.append((ws / "src" / "app.js").read_text(encoding="utf-8"))
        (ws / "src" / "app.js").write_text("// completed after resume\n", encoding="utf-8")
        from agent_factory.ports.code_agent import CodeAgentResult

        return CodeAgentResult(files_written=["src/app.js"], summary="Resumed cleanly")

    code_agent = FakeCodeAgent(
        test_gen_steps=[{"tests/should_not_run.py": "def test_no(): pass\n"}],
        impl_steps=[_resume_impl],
    )
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=code_agent,
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(),
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
    )
    try:
        # 1. HTML contains secondary clean rebuild button and client sends clean_workspace flag
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            html = resp.read().decode("utf-8")
        assert "Reconstruir desde cero" in html
        assert "clean_workspace" in html
        assert "retry-clean" in html

        # 2. Default POST /retry (no body -> clean_workspace=False) preserves tests/ and src/
        ret_code, ret_res = _http_json(f"{base_url}/api/projects/{pid}/retry", method="POST")
        assert ret_code == 200
        assert ret_res["status"]["state"] == "WAITING_APPROVAL"
        assert (ws_dir / "tests" / "test_app.py").read_text(encoding="utf-8") == existing_test
        assert (ws_dir / "src" / "app.js").read_text(encoding="utf-8") == "// partial progress\n"

        _, retried_detail = _http_json(f"{base_url}/api/projects/{pid}")
        retried_ev = next(
            ev for ev in reversed(retried_detail["events"]) if ev["type"] == EventType.PROJECT_RETRIED
        )
        assert retried_ev["payload"]["workspace_cleaned"] is False

        # 3. Approve -> resumes incrementally without failing scaffold or calling generate_tests
        app_code, app_res = _http_json(f"{base_url}/api/projects/{pid}/approve", method="POST")
        assert app_code == 200
        assert app_res["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, final_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert final_detail["status"]["state"] == "DELIVERED"
        assert len(code_agent.test_gen_calls) == 0
        assert seen_prior_js == ["// partial progress\n"]
        assert any(ev["type"] == EventType.TESTS_REUSED for ev in final_detail["events"])
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_smart_title_generation_and_manual_rename_endpoint(tmp_path: Path) -> None:
    """create_project synthesizes clean <=55-char titles and clean slugs, preserves existing SQLite titles (INV-11), and PATCH /title validates, sanitizes, and renames projects."""
    store = SqliteEventStore(str(tmp_path / "smart_title.db"))
    now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    # Pre-existing project in SQLite MUST remain untouched (INV-11)
    legacy_pid = "legacy-old-proj"
    legacy_name = "Nombre legacy muy largo guardado antes en SQLite sin alteraciones automáticas"
    store.append(
        create_event(
            legacy_pid,
            EventType.PROJECT_CREATED,
            {"name": legacy_name, "objective": "Legacy prompt", "state": "INTERVIEW"},
            at=now,
        )
    )

    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Gestor de gastos diarios",
                    "users": ["Gabriel"],
                    "features": ["Registro"],
                    "business_rules": ["Montos positivos"],
                    "edge_cases": ["Campo vacío"],
                    "constraints": ["Web estática"],
                    "success_criteria": ["0 errores"],
                    "assumptions": [],
                },
                "questions": ["¿Quieres categorías personalizadas?"],
            }
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        # Existing project stays unchanged
        _, list_before = _http_json(f"{base_url}/api/projects")
        legacy_entry = next(p for p in list_before["projects"] if p["project_id"] == legacy_pid)
        assert legacy_entry["name"] == legacy_name

        # New project synthesizes short <=55 title without filler prefix & clean slug
        verbose_prompt = (
            "Quiero una aplicación web para gestionar gastos diarios del hogar "
            "con gráficas mensuales y exportación de reportes en CSV"
        )
        c_code, c_res = _http_json(
            f"{base_url}/api/projects",
            method="POST",
            body={"prompt": verbose_prompt},
        )
        assert c_code == 200
        pid = c_res["project_id"]
        short_name = c_res["status"]["name"]
        assert 5 <= len(short_name) <= 55
        assert "quiero" not in short_name.lower()
        assert not pid.startswith("quiero-una-aplicacion")
        assert c_res["status"]["objective"] == verbose_prompt
        assert server.controller.wait_for_idle(timeout=5.0) is True

        # Validation guards on PATCH /api/projects/<id>/title
        bad_empty, _ = _http_json(
            f"{base_url}/api/projects/{pid}/title",
            method="PATCH",
            body={"title": "   "},
        )
        assert bad_empty == 400
        bad_long, _ = _http_json(
            f"{base_url}/api/projects/{pid}/title",
            method="PATCH",
            body={"title": "X" * 81},
        )
        assert bad_long == 400
        missing_code, _ = _http_json(
            f"{base_url}/api/projects/no-existe-tit/title",
            method="PATCH",
            body={"title": "Nuevo título"},
        )
        assert missing_code == 404

        # Valid rename persists via PROJECT_RENAMED event while preserving project_id & objective
        ren_code, ren_res = _http_json(
            f"{base_url}/api/projects/{pid}/title",
            method="PATCH",
            body={"title": "  Control de\x00 Gastos <2026>  "},
        )
        assert ren_code == 200
        assert ren_res["status"]["name"] == "Control de Gastos 2026"
        assert ren_res["project_id"] == pid

        _, detail_after = _http_json(f"{base_url}/api/projects/{pid}")
        assert detail_after["status"]["name"] == "Control de Gastos 2026"
        assert detail_after["status"]["objective"] == verbose_prompt
        assert any(ev["type"] == EventType.PROJECT_RENAMED for ev in detail_after["events"])

        # UI contains inline title editor hook & PATCH endpoint
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        assert "edit-title-btn" in html
        assert "/title" in html
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_local_app_runner_start_stop_for_static_and_python_apps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LocalAppRunner detects static (preferring built src/ over empty template root) and python_local (PORT/HOST, clean env), exposes local_app state and /start+/stop endpoints, and releases ports cleanly."""
    from agent_factory.runtime_runner import LocalAppRunner, detect_workspace_app

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST_LEAK_GUARD_SECRET_888")

    store = SqliteEventStore(str(tmp_path / "local_runner.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_local")
    now = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)

    def _seed_delivered(pid: str, name: str, deploy_url: str | None) -> None:
        pdict = _plan_payload(name)
        phash = Plan.model_validate(pdict).compute_hash()
        for ev in (
            create_event(pid, EventType.PROJECT_CREATED, {"name": name, "objective": name, "state": "INTERVIEW"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
            create_event(pid, EventType.PLAN_CREATED, {"plan": pdict, "plan_hash": phash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
            create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": phash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
            create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": f"https://github.com/acme/{pid}", "deploy_url": deploy_url}, at=now),
        ):
            store.append(ev)

    # 1. Static app whose root index.html is empty template and built app lives in src/
    pid_static = "app-static-src"
    _seed_delivered(pid_static, "App Estática", f"https://acme.github.io/{pid_static}")
    ws_static = ws_mgr.create_workspace(pid_static)
    (ws_static / "index.html").write_text(
        '<!DOCTYPE html>\n<html lang="es">\n  <head>\n    <title>Static Web App</title>\n'
        '    <link rel="stylesheet" href="./styles.css" />\n  </head>\n'
        '  <body>\n    <main id="app" aria-live="polite"></main>\n'
        '    <script type="module" src="./src/app.js"></script>\n  </body>\n</html>\n',
        encoding="utf-8",
    )
    (ws_static / "src").mkdir(parents=True)
    (ws_static / "src" / "index.html").write_text(
        "<!doctype html><html><body><h1>Real Built Static App</h1><script src='./app.js'></script></body></html>\n",
        encoding="utf-8",
    )
    (ws_static / "src" / "app.js").write_text("console.log('ok');\n", encoding="utf-8")

    # 2. Local Python server app with app.py using PORT and HOST from clean subprocess env
    pid_py = "app-python-srv"
    _seed_delivered(pid_py, "Backend Local Python", "")
    ws_py = ws_mgr.create_workspace(pid_py)
    (ws_py / "app.py").write_text(
        "import http.server, json, os\n"
        "host = os.environ['HOST']\n"
        "port = int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'ok': True, 'has_tg': 'TELEGRAM_BOT_TOKEN' in os.environ}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "http.server.ThreadingHTTPServer((host, port), H).serve_forever()\n",
        encoding="utf-8",
    )

    static_info = detect_workspace_app(ws_static)
    assert static_info.app_type == "static"
    assert static_info.serve_dir is not None
    assert static_info.serve_dir.resolve() == (ws_static / "src").resolve()

    py_info = detect_workspace_app(ws_py)
    assert py_info.app_type == "python_local"

    local_runner = LocalAppRunner(workspace_manager=ws_mgr)
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        local_runner=local_runner,
    )
    try:
        # UI exposes local controls & conditional GitHub Pages button
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        for marker in ("Encender en local", "Apagar", "Abrir app en local", "local_app"):
            assert marker in html

        # GET /api/projects exposes local_app state initially stopped
        _, list_res = _http_json(f"{base_url}/api/projects")
        st_entry = next(p for p in list_res["projects"] if p["project_id"] == pid_static)
        assert st_entry["local_app"] == {"running": False, "url": None, "app_type": "static"}
        py_entry = next(p for p in list_res["projects"] if p["project_id"] == pid_py)
        assert py_entry["local_app"] == {"running": False, "url": None, "app_type": "python_local"}

        # Start static app -> serves real src/index.html on 127.0.0.1:8081..8199
        s_code, s_res = _http_json(f"{base_url}/api/projects/{pid_static}/start", method="POST")
        assert s_code == 200
        static_url = s_res["local_app"]["url"]
        assert s_res["local_app"]["running"] is True
        assert s_res["local_app"]["app_type"] == "static"
        assert static_url.startswith("http://127.0.0.1:")
        port_s = int(static_url.rsplit(":", 1)[1])
        assert 8081 <= port_s <= 8199

        with urllib.request.urlopen(static_url, timeout=5.0) as app_resp:
            assert app_resp.status == 200
            body_html = app_resp.read().decode("utf-8")
        assert "Real Built Static App" in body_html
        assert "Static Web App" not in body_html

        # Start python_local app -> receives PORT/HOST and clean env without TELEGRAM_BOT_TOKEN
        p_code, p_res = _http_json(f"{base_url}/api/projects/{pid_py}/start", method="POST")
        assert p_code == 200
        py_url = p_res["local_app"]["url"]
        assert p_res["local_app"]["running"] is True
        assert p_res["local_app"]["app_type"] == "python_local"
        assert py_url != static_url

        _, py_json = _http_json(py_url)
        assert py_json["ok"] is True
        assert py_json["has_tg"] is False

        # Detail endpoint reflects running local_app
        _, py_detail = _http_json(f"{base_url}/api/projects/{pid_py}")
        assert py_detail["local_app"]["running"] is True
        assert py_detail["status"]["local_app"]["url"] == py_url

        # Stop both apps and verify ports are released
        stop_s_code, stop_s = _http_json(f"{base_url}/api/projects/{pid_static}/stop", method="POST")
        assert stop_s_code == 200
        assert stop_s["local_app"]["running"] is False
        assert stop_s["local_app"]["url"] is None
        with pytest.raises((urllib.error.URLError, OSError)):
            urllib.request.urlopen(static_url, timeout=1.0)

        stop_p_code, stop_p = _http_json(f"{base_url}/api/projects/{pid_py}/stop", method="POST")
        assert stop_p_code == 200
        assert stop_p["local_app"]["running"] is False
        with pytest.raises((urllib.error.URLError, OSError)):
            urllib.request.urlopen(py_url, timeout=1.0)
    finally:
        local_runner.stop_all()
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_post_delivery_modify_workflow_preserves_workspace_and_updates_project(
    tmp_path: Path,
) -> None:
    """POST /api/projects/<id>/modify transitions DELIVERED -> CHANGE_REQUEST -> BUILDING, preserves existing workspace, extends tests before sealing INV-13 lock, runs Smoke Test, pushes updates, and delivers cleanly."""
    from agent_factory.runtime_runner import LocalAppRunner

    store = SqliteEventStore(str(tmp_path / "modify_post.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_mod")
    pid = "proj-post-modify"
    now = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Post-Delivery App v1")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()

    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "App Notas", "objective": "App de notas rápida", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": f"https://github.com/acme/{pid}", "deploy_url": f"https://acme.github.io/{pid}"}, at=now),
    ):
        store.append(ev)

    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text(
        "<!doctype html><html><body><h1>Notas</h1><script src='./app.js'></script></body></html>\n",
        encoding="utf-8",
    )
    (ws_dir / "app.js").write_text("window.appReady = true;\n", encoding="utf-8")
    (ws_dir / "custom-user-keep.txt").write_text("preserve-me\n", encoding="utf-8")
    (ws_dir / "tests").mkdir(parents=True)
    (ws_dir / "tests" / "test_app.py").write_text("def test_at_01(): assert True\n", encoding="utf-8")

    # Another non-delivered project to assert 409 state guard
    store.append(
        create_event(
            "proj-not-delivered",
            EventType.PROJECT_CREATED,
            {"name": "En Entrevista", "objective": "Obj", "state": "INTERVIEW"},
            at=now,
        )
    )

    seen_modify_prompts: list[str] = []

    def _mod_test_step(ws: Path, _tests: Any) -> Any:
        # Existing workspace artifacts must still exist during test extension
        assert (ws / "custom-user-keep.txt").is_file()
        (ws / "tests" / "test_modify_theme.py").write_text(
            "def test_theme_toggle(): assert 'theme-toggle' in (Path if False else open('index.html', encoding='utf-8').read())\n",
            encoding="utf-8",
        )
        from agent_factory.ports.code_agent import CodeAgentResult

        return CodeAgentResult(
            files_written=["tests/test_modify_theme.py"],
            summary="Added modification test",
        )

    def _mod_impl_step(ws: Path, plan_obj: Any, failure_out: str, _attempt: int) -> Any:
        seen_modify_prompts.append(f"{plan_obj.summary}\n{failure_out}")
        assert (ws / "custom-user-keep.txt").read_text(encoding="utf-8") == "preserve-me\n"
        # Tampering with existing tests MUST fail INV-13 lock after test extension
        (ws / "index.html").write_text(
            "<!doctype html><html><body><h1>Notas</h1><button id='theme-toggle'>Tema</button><script src='./app.js'></script></body></html>\n",
            encoding="utf-8",
        )
        from agent_factory.ports.code_agent import CodeAgentResult

        return CodeAgentResult(files_written=["index.html"], summary="Added theme toggle button")

    code_agent = FakeCodeAgent(test_gen_steps=[_mod_test_step], impl_steps=[_mod_impl_step])
    vcs = FakeVCS(
        ci_results=[CIRunResult(passed=True, summary="CI updated green")],
        repo_base_url="https://github.com/acme",
        deploy_base_url="https://acme.github.io",
    )
    local_runner = LocalAppRunner(workspace_manager=ws_mgr)
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=code_agent,
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=0,
                    stdout="2 passed",
                    test_results={"AT-01": True, "MOD-02": True},
                )
            ]
        ),
        vcs=vcs,
        local_app_runner=local_runner,
    )
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        template_scaffold=scaffold,
        workspace_manager=ws_mgr,
        local_runner=local_runner,
    )
    try:
        # UI contains post-delivery modification panel
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        assert "Pedir cambios o mejoras" in html
        assert "Aplicar cambios" in html
        assert "/modify" in html

        # Validation guards
        assert _http_json(f"{base_url}/api/projects/{pid}/modify", method="POST", body={"instructions": "  "})[0] == 400
        assert _http_json(f"{base_url}/api/projects/no-existe-mod/modify", method="POST", body={"instructions": "Hola"})[0] == 404
        assert _http_json(f"{base_url}/api/projects/proj-not-delivered/modify", method="POST", body={"instructions": "Cambiar"})[0] == 409

        # Execute post-delivery modify
        mod_instr = "Agrega un botón de tema oscuro en la cabecera"
        m_code, m_res = _http_json(
            f"{base_url}/api/projects/{pid}/modify",
            method="POST",
            body={"instructions": mod_instr},
        )
        assert m_code == 200
        assert m_res["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, updated_detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert updated_detail["status"]["state"] == "DELIVERED"
        assert (ws_dir / "custom-user-keep.txt").read_text(encoding="utf-8") == "preserve-me\n"
        assert (ws_dir / "tests" / "test_modify_theme.py").is_file()
        assert "theme-toggle" in (ws_dir / "index.html").read_text(encoding="utf-8")
        assert any(mod_instr in txt for txt in seen_modify_prompts)
        assert any(mod_instr in f for f in updated_detail["spec"]["features"])
        assert len(vcs.pushes) == 1
        assert len(vcs.merges) == 1
        assert local_runner.get_status(pid).running is False
    finally:
        local_runner.stop_all()
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_suggest_title_analyzes_repo_and_ui_hook(tmp_path: Path) -> None:
    """POST /api/projects/<id>/suggest-title inspects real workspace files, emits creative 'Nombre - Descripción' <=65 chars, redacts secrets (INV-19), and wires UI button."""
    store = SqliteEventStore(str(tmp_path / "suggest_title.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_suggest")
    pid = "musica-repo-test"
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    secret_tok = "TEST_SUGGEST_SECRET_777"

    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {
                "name": "Aplicación de música",
                "objective": "App local para descargar y escuchar canciones favoritas",
                "state": "INTERVIEW",
            },
            at=now,
        )
    )
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "README.md").write_text(
        "# Musiquita\n\nApp web para descargar y escuchar música sin anuncios.\n",
        encoding="utf-8",
    )
    (ws_dir / "src").mkdir()
    (ws_dir / "src" / "app.py").write_text(
        f'"""Musiquita server token={secret_tok}."""\nfrom http.server import ThreadingHTTPServer\n',
        encoding="utf-8",
    )

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        secrets_to_redact=(secret_tok,),
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        assert "Sugerir analizando repo" in html
        assert "/suggest-title" in html

        assert _http_json(f"{base_url}/api/projects/no-existe-sug/suggest-title", method="POST")[0] == 404
        assert _http_json(f"{base_url}/api/projects/Bad%20ID!/suggest-title", method="POST")[0] == 400

        code, res = _http_json(f"{base_url}/api/projects/{pid}/suggest-title", method="POST")
        assert code == 200
        assert res["ok"] is True
        suggested = res["suggested_title"]
        assert " - " in suggested
        short_name, short_desc = suggested.split(" - ", 1)
        assert len(short_name.strip()) >= 2
        assert len(short_desc.strip()) >= 5
        assert len(suggested) <= 65
        assert "Musiquita" in short_name
        assert secret_tok not in suggested
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_logo_auto_svg_and_custom_upload_sanitization(tmp_path: Path) -> None:
    """GET /logo serves auto-generated domain-adapted 64x64 SVG by default; POST /logo validates magic bytes/size <=512KB, blocks SVG XSS (<script>, onload=, javascript:), persists valid SVG/PNG in logos_dir & workspace, and UI exposes Cambiar logo."""
    import base64

    store = SqliteEventStore(str(tmp_path / "logos_dash.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_logos")
    logos_dir = tmp_path / "custom_logos"
    now = datetime(2026, 11, 20, 12, 5, tzinfo=UTC)

    pid_music = "logo-musica"
    store.append(
        create_event(
            pid_music,
            EventType.PROJECT_CREATED,
            {
                "name": "Musiquita - Reproductor de audio",
                "objective": "Escuchar música MP3 y gestionar playlists",
                "state": "INTERVIEW",
            },
            at=now,
        )
    )
    ws_music = ws_mgr.create_workspace(pid_music)
    (ws_music / "index.html").write_text("<h1>Hola</h1>\n", encoding="utf-8")

    pid_timer = "logo-timer"
    store.append(
        create_event(
            pid_timer,
            EventType.PROJECT_CREATED,
            {
                "name": "Pomodoro Foco",
                "objective": "Temporizador de intervalos de estudio",
                "state": "INTERVIEW",
            },
            at=now,
        )
    )

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        logos_dir=logos_dir,
    )
    try:
        # 1. UI exposes logo thumbnail hooks + Cambiar logo uploader
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        assert "Cambiar logo" in html
        assert "logo-file-input" in html
        assert "/logo" in html

        # 2. Default GET /logo returns clean 64x64 domain-adapted SVG (distinct per domain)
        with urllib.request.urlopen(f"{base_url}/api/projects/{pid_music}/logo", timeout=5.0) as r1:
            assert r1.status == 200
            assert "image/svg+xml" in r1.headers.get("Content-Type", "")
            svg_music = r1.read().decode("utf-8")
        assert "<svg" in svg_music
        assert 'viewBox="0 0 64 64"' in svg_music
        assert "<script" not in svg_music.lower()

        with urllib.request.urlopen(f"{base_url}/api/projects/{pid_timer}/logo", timeout=5.0) as r2:
            assert r2.status == 200
            svg_timer = r2.read().decode("utf-8")
        assert "<svg" in svg_timer
        assert svg_music != svg_timer

        # 3. Security validations on POST /logo
        for bad_svg in (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle r="10"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)">x</a></svg>',
        ):
            bad_code, _ = _http_json(
                f"{base_url}/api/projects/{pid_music}/logo",
                method="POST",
                body={
                    "filename": "evil.svg",
                    "data_base64": base64.b64encode(bad_svg).decode("ascii"),
                },
            )
            assert bad_code == 400

        # Reject oversized payload > 512 KB
        too_big = b"\x89PNG\r\n\x1a\n" + (b"0" * (512 * 1024 + 1))
        big_code, _ = _http_json(
            f"{base_url}/api/projects/{pid_music}/logo",
            method="POST",
            body={
                "filename": "big.png",
                "data_base64": base64.b64encode(too_big).decode("ascii"),
            },
        )
        assert big_code == 400

        # Reject spoofed magic header
        fake_code, _ = _http_json(
            f"{base_url}/api/projects/{pid_music}/logo",
            method="POST",
            body={
                "filename": "fake.png",
                "data_base64": base64.b64encode(b"NOT_A_PNG_FILE").decode("ascii"),
            },
        )
        assert fake_code == 400

        # 4. Upload valid clean SVG -> saved in logos_dir and copied as workspace favicon.svg
        clean_svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="24" fill="#38bdf8"/></svg>'
        up_svg_code, up_svg_res = _http_json(
            f"{base_url}/api/projects/{pid_music}/logo",
            method="POST",
            body={
                "filename": "brand.svg",
                "data_base64": base64.b64encode(clean_svg).decode("ascii"),
            },
        )
        assert up_svg_code == 200
        assert up_svg_res["ok"] is True
        assert (logos_dir / f"{pid_music}.svg").read_bytes() == clean_svg
        assert (ws_music / "favicon.svg").read_bytes() == clean_svg

        # 5. Upload valid PNG -> replaces old SVG custom logo, served with image/png & copied to workspace
        valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        up_png_code, _ = _http_json(
            f"{base_url}/api/projects/{pid_music}/logo",
            method="POST",
            body={
                "filename": "icon.png",
                "data_base64": base64.b64encode(valid_png).decode("ascii"),
            },
        )
        assert up_png_code == 200
        assert not (logos_dir / f"{pid_music}.svg").exists()
        assert (logos_dir / f"{pid_music}.png").read_bytes() == valid_png
        assert (ws_music / "favicon.png").read_bytes() == valid_png

        with urllib.request.urlopen(f"{base_url}/api/projects/{pid_music}/logo", timeout=5.0) as r_png:
            assert r_png.status == 200
            assert r_png.headers.get("Content-Type", "").startswith("image/png")
            assert r_png.read() == valid_png

        # 6. Upload valid WebP via multipart/form-data -> accepted and served with image/webp
        valid_webp = b"RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        boundary = "----AgentFactoryBoundary987"
        mp_body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="logo"; filename="custom.webp"\r\n'
                f"Content-Type: image/webp\r\n\r\n"
            ).encode()
            + valid_webp
            + f"\r\n--{boundary}--\r\n".encode()
        )
        mp_req = urllib.request.Request(
            f"{base_url}/api/projects/{pid_music}/logo",
            data=mp_body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(mp_req, timeout=5.0) as mp_resp:
            assert mp_resp.status == 200
            mp_json = json.loads(mp_resp.read().decode("utf-8"))
        assert mp_json["mime_type"] == "image/webp"
        assert (logos_dir / f"{pid_music}.webp").read_bytes() == valid_webp
        assert not (logos_dir / f"{pid_music}.png").exists()

        # Summary list/detail expose logo_url
        _, list_data = _http_json(f"{base_url}/api/projects")
        music_item = next(p for p in list_data["projects"] if p["project_id"] == pid_music)
        assert music_item["logo_url"].startswith(f"/api/projects/{pid_music}/logo")
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_suggest_title_from_repo_filters_template_text_and_inspects_src_templates(
    tmp_path: Path,
) -> None:
    """suggest_title_from_repo ignores base template placeholders in README/root index/package.json and extracts real app title & Spanish description from src/ HTML, docstrings/comments, and objective."""
    from agent_factory.orchestrator import suggest_title_from_repo

    ws_dir = tmp_path / "ws_yt_dl"
    ws_dir.mkdir(parents=True)
    (ws_dir / "README.md").write_text(
        "# Agent Factory\n\n100% static web application base template for Agent Factory.\n",
        encoding="utf-8",
    )
    (ws_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "agent-factory-static-web-template",
                "description": "100% static web application base template for Agent Factory (offline-ready, zero backend)",
            }
        ),
        encoding="utf-8",
    )
    (ws_dir / "index.html").write_text(
        "<!DOCTYPE html><html><head><title>Static Web App</title></head>"
        "<body><main id='app'></main></body></html>\n",
        encoding="utf-8",
    )

    tpl_dir = ws_dir / "src" / "yt_music_dl" / "templates"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "index.html").write_text(
        "<!DOCTYPE html><html lang='es'><head><title>YT Music DL</title></head>"
        "<body><h1>YT Music DL - Descargador local de música de YouTube</h1></body></html>\n",
        encoding="utf-8",
    )
    (ws_dir / "src" / "yt_music_dl" / "__init__.py").write_text(
        "# YT Music DL: aplicación web local en español\n",
        encoding="utf-8",
    )
    (ws_dir / "src" / "yt_music_dl" / "app.py").write_text(
        '"""Flask application factory, local security middleware, and Waitress entrypoint."""\n'
        "from flask import Flask\n",
        encoding="utf-8",
    )

    suggested = suggest_title_from_repo(
        ws_dir,
        current_name="Aplicación web local en Python",
        objective="Quiero una aplicación web local en Python con interfaz para descargar música de YouTube con yt-dlp",
        plan_summary="App web local con Flask y yt-dlp para bajar audio de YouTube.",
    )
    low = suggested.lower()
    for banned in (
        "agent factory",
        "agent-factory",
        "static web app",
        "static web application",
        "base template",
        "100% static web",
        "flask application factory",
    ):
        assert banned not in low, f"Found unexpected noise '{banned}' in '{suggested}'"
    assert " - " in suggested
    assert len(suggested) <= 65
    short_name, short_desc = suggested.split(" - ", 1)
    assert short_name == "YT Music DL"
    assert len(short_desc.strip()) >= 8

    # Variant B: HTML inside src/ only carries <h1>YT Music DL</h1>, leading Python comment + objective provide Spanish summary
    ws_minimal = tmp_path / "ws_yt_minimal"
    ws_minimal.mkdir(parents=True)
    (ws_minimal / "README.md").write_text(
        "# Static Web Application\n\nBase template for Agent Factory.\n",
        encoding="utf-8",
    )
    (ws_minimal / "index.html").write_text("<title>Static Web App</title>\n", encoding="utf-8")
    min_tpl = ws_minimal / "src" / "yt_music_dl" / "templates"
    min_tpl.mkdir(parents=True)
    (min_tpl / "home.html").write_text(
        "<html><head><title>Static Web App</title></head><body><h1>YT Music DL</h1></body></html>\n",
        encoding="utf-8",
    )
    (ws_minimal / "src" / "yt_music_dl" / "main.py").write_text(
        "# Descargador local de música de YouTube\n",
        encoding="utf-8",
    )
    suggested_b = suggest_title_from_repo(
        ws_minimal,
        current_name="App música",
        objective="Descargar canciones de YouTube en formato MP3 localmente",
    )
    low_b = suggested_b.lower()
    assert "agent factory" not in low_b
    assert "static web" not in low_b
    assert "base template" not in low_b
    assert " - " in suggested_b
    assert len(suggested_b) <= 65
    name_b, desc_b = suggested_b.split(" - ", 1)
    assert name_b == "YT Music DL"
    assert "música" in desc_b.lower() or "youtube" in desc_b.lower() or "mp3" in desc_b.lower()



