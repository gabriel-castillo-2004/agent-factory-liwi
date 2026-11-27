"""Integration tests for the local loopback HTTP dashboard and CLI integration (INV-03, INV-07, INV-19, INV-22)."""

import http.client
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
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


_LOOPBACK_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _LOOPBACK_HTTP_OPENER.open(req, timeout=timeout) as resp:
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

    import subprocess

    real_run = subprocess.run

    def _fast_venv_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_list = [str(c) for c in cmd]
        if len(cmd_list) >= 4 and cmd_list[1:3] == ["-m", "venv"]:
            return real_run(
                [cmd_list[0], "-m", "venv", "--without-pip", *cmd_list[3:]],
                **kwargs,
            )
        return real_run(cmd_list, **kwargs)

    local_runner = LocalAppRunner(
        workspace_manager=ws_mgr,
        dep_command_runner=_fast_venv_runner,
    )
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
        assert server.controller.wait_for_idle(timeout=25.0) is True

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


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_rename_project_updates_github_repo_remote_and_status(
    tmp_path: Path,
) -> None:
    """PATCH /api/projects/<id>/title renames GitHub repo via PATCH /repos/{owner}/{old_repo}, updates origin remote & projected repo_url/deploy_url while keeping project_id intact, tolerates 422, redacts secrets, and exposes UI checkbox."""
    import subprocess
    from collections.abc import Sequence
    from types import SimpleNamespace

    from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter

    pid = "quiero-una-app-larga"
    gh_tok = "TEST_GH_RENAME_SECRET_456"
    store = SqliteEventStore(str(tmp_path / "rename_gh.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_rename")
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<h1>App</h1>\n", encoding="utf-8")

    # Initialize isolated git repo inside ws_dir pointing to old GitHub remote
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://github.com/gabriel-user/{pid}.git"],
        cwd=ws_dir,
        check=True,
    )

    now = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {"name": "Quiero una app larga", "objective": "App larga", "state": "INTERVIEW"},
            at=now,
        )
    )
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"to": "DEBATE"}, at=now))
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"to": "MEDIATION"}, at=now))
    store.append(create_event(pid, EventType.PLAN_CREATED, {"plan_hash": "h1", "total_tests": 1}, at=now))
    store.append(
        create_event(
            pid,
            EventType.APPROVAL_RECORDED,
            {"decision": "approved", "plan_hash": "h1", "to": "BUILDING"},
            at=now,
        )
    )
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"to": "VERIFYING"}, at=now))
    store.append(
        create_event(
            pid,
            EventType.PROJECT_DELIVERED,
            {
                "repo_url": f"https://github.com/gabriel-user/{pid}",
                "deploy_url": f"https://gabriel-user.github.io/{pid}",
            },
            at=now,
        )
    )

    http_calls: list[tuple[str, str, Any, dict[str, str] | None]] = []
    next_status = 200
    next_body: str = json.dumps(
        {
            "name": "yt-music-dl-descargador",
            "html_url": "https://github.com/gabriel-user/yt-music-dl-descargador",
        }
    )

    class _FakeTransport:
        def request_json(
            self,
            method: str,
            url: str,
            payload: Any = None,
            timeout_seconds: float = 30.0,
            headers: dict[str, str] | None = None,
            extra_secrets: Sequence[str] = (),
            raise_on_http_error: bool = False,
        ) -> Any:
            http_calls.append((method, url, payload, headers))
            return SimpleNamespace(status_code=next_status, body=next_body)

    vcs = GitHubVCSAdapter(
        owner="gabriel-user",
        github_token=gh_tok,
        secrets=(gh_tok,),
        http_transport=_FakeTransport(),
    )
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr, vcs=vcs)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        secrets_to_redact=(gh_tok,),
    )
    try:
        # 1. GET / exposes discrete GitHub rename checkbox inside edit-title-form
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        assert "Renombrar también en GitHub" in html
        assert "rename-github-checkbox" in html

        # 2. PATCH /title with rename_github_repo=True calls PATCH /repos/gabriel-user/quiero-una-app-larga
        code, res = _http_json(
            f"{base_url}/api/projects/{pid}/title",
            method="PATCH",
            body={"title": "YT Music DL — Descargador!", "rename_github_repo": True},
        )
        assert code == 200
        assert res["ok"] is True
        assert res["project_id"] == pid
        assert res["name"] == "YT Music DL — Descargador!"
        assert res["status"]["project_id"] == pid
        assert res["status"]["links"]["repo"] == "https://github.com/gabriel-user/yt-music-dl-descargador"
        assert (
            res["status"]["links"]["deploy"]
            == "https://gabriel-user.github.io/yt-music-dl-descargador/"
        )

        assert len(http_calls) == 1
        m, u, p, hdrs = http_calls[0]
        assert m == "PATCH"
        assert u == f"https://api.github.com/repos/gabriel-user/{pid}"
        assert p == {"name": "yt-music-dl-descargador"}
        assert hdrs is not None and hdrs["Authorization"] == f"Bearer {gh_tok}"

        # Workspace git origin remote updated without embedding token
        origin_url = subprocess.run(
            ["git", "-C", str(ws_dir), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert origin_url == "https://github.com/gabriel-user/yt-music-dl-descargador.git"
        assert gh_tok not in origin_url

        # Event-sourced detail replay keeps project_id & projects new URLs
        _, detail = _http_json(f"{base_url}/api/projects/{pid}")
        assert detail["project_id"] == pid
        assert detail["status"]["name"] == "YT Music DL — Descargador!"
        assert detail["status"]["links"]["repo"] == "https://github.com/gabriel-user/yt-music-dl-descargador"

        # 3. rename_github_repo=False renames only title without calling GitHub API
        code2, res2 = _http_json(
            f"{base_url}/api/projects/{pid}/title",
            method="PATCH",
            body={"title": "Sólo Título Local", "rename_github_repo": False},
        )
        assert code2 == 200
        assert res2["name"] == "Sólo Título Local"
        assert len(http_calls) == 1
        assert res2["status"]["links"]["repo"] == "https://github.com/gabriel-user/yt-music-dl-descargador"

        # 4. Tolerates 422 when GitHub says repo already has that name; redacts secrets on real failure
        next_status = 422
        next_body = json.dumps({"message": "Repository already has that name"})
        r_url, d_url = vcs.rename_repository(
            "https://github.com/gabriel-user/yt-music-dl-descargador",
            "Música & Café — Versión 2",
        )
        assert r_url == "https://github.com/gabriel-user/musica-cafe-version-2"
        assert d_url == "https://gabriel-user.github.io/musica-cafe-version-2/"

        next_status = 403
        next_body = json.dumps({"message": f"Forbidden with token {gh_tok}"})
        err_code, err_res = _http_json(
            f"{base_url}/api/projects/{pid}/title",
            method="PATCH",
            body={"title": "Fallo Con Secreto", "rename_github_repo": True},
        )
        assert err_code == 409
        assert gh_tok not in err_res["error"]
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_project_chat_audit_document_persistence_and_secret_redaction(
    tmp_path: Path,
) -> None:
    """collect_workspace_code_context & POST/GET /api/projects/<id>/chat support chat/audit/document read-only over real workspace code, redact secrets (INV-19), persist in chats_dir, and wire audit/doc/voice UI controls."""
    from agent_factory.workspace import collect_workspace_code_context

    pid = "audit-chat-demo"
    secret_val = "TEST_CHAT_AUDIT_SECRET_999"
    store = SqliteEventStore(str(tmp_path / "chat_dash.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_chat")
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "src").mkdir()
    app_file = ws_dir / "src" / "app.py"
    app_src = f'"""Music app."""\nTOKEN_HINT = "{secret_val}"\ndef route_home(): return "ok"\n'
    app_file.write_text(app_src, encoding="utf-8")
    (ws_dir / ".env").write_text(f"SECRET={secret_val}\n", encoding="utf-8")
    (ws_dir / "node_modules").mkdir()
    (ws_dir / "node_modules" / "ignored.js").write_text("SHOULD_NOT_APPEAR\n", encoding="utf-8")
    (ws_dir / ".venv").mkdir()
    (ws_dir / ".venv" / "lib.py").write_text("VENV_IGNORED\n", encoding="utf-8")
    (ws_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")

    ctx = collect_workspace_code_context(ws_dir, max_chars=8000, extra_secrets=(secret_val,))
    assert "src/app.py" in ctx
    assert "route_home" in ctx
    assert "SHOULD_NOT_APPEAR" not in ctx
    assert "VENV_IGNORED" not in ctx
    assert ".env" not in ctx
    assert secret_val not in ctx
    assert "[REDACTED]" in ctx

    now = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {"name": "Audit Demo", "objective": "App local para pruebas", "state": "INTERVIEW"},
            at=now,
        )
    )
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"to": "DEBATE"}, at=now))
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"to": "MEDIATION"}, at=now))
    store.append(
        create_event(
            pid,
            EventType.PLAN_CREATED,
            {
                "plan_hash": "h-chat",
                "total_tests": 1,
                "plan": {
                    "summary": "App local auditada",
                    "rationale": "Simple",
                    "template_id": "static-web",
                    "files": [],
                    "acceptance_tests": [],
                    "risks": [],
                    "deployment_target": "github-pages",
                },
            },
            at=now,
        )
    )
    store.append(
        create_event(
            pid,
            EventType.APPROVAL_RECORDED,
            {"decision": "approved", "plan_hash": "h-chat", "to": "BUILDING"},
            at=now,
        )
    )
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"to": "VERIFYING"}, at=now))
    store.append(create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": ""}, at=now))

    llm = FakeLLM(
        responses=[
            {"reply": f"La ruta route_home responde ok (sin {secret_val}).", "suggested_changes": ""},
            {
                "reply": f"## Auditoría\n- Revisar token {secret_val}\n- Añadir manejo de errores.",
                "suggested_changes": f"Añadir validación de errores y quitar {secret_val}.",
            },
            {
                "reply": "# Documentación Técnica\nEjecuta `src/app.py`.",
                "document_title": f"{pid}-documentacion.md",
            },
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        secrets_to_redact=(secret_val,),
    )
    try:
        # 1. GET / exposes Audit chat & voice UI controls
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        for needle in (
            "Consultar y Auditar proyecto",
            "Auditar código",
            "Explicar funcionamiento",
            "Generar documento (.md)",
            "Descargar .md",
            "Enviar hallazgos a Aplicar cambios",
            "SpeechRecognition",
            "Leer respuestas en voz alta",
            "speechSynthesis",
        ):
            assert needle in html

        # 2. Mode chat
        c1, r1 = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "¿Qué hace route_home?", "mode": "chat"},
        )
        assert c1 == 200
        assert r1["mode"] == "chat"
        assert "route_home" in r1["reply"]
        assert secret_val not in r1["reply"]
        assert "[REDACTED]" in r1["reply"]

        # Prompt sent to LLM contained real file content (with secret redacted)
        assert any("route_home" in str(m.get("content", "")) for m in llm.calls[0]["messages"])
        assert secret_val not in json.dumps(llm.calls[0]["messages"])

        # 3. Mode audit
        c2, r2 = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "", "mode": "audit"},
        )
        assert c2 == 200
        assert r2["mode"] == "audit"
        assert "Auditoría" in r2["reply"]
        assert secret_val not in r2["suggested_changes"]

        # 4. Mode document
        c3, r3 = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "", "mode": "document"},
        )
        assert c3 == 200
        assert r3["mode"] == "document"
        assert "# Documentación Técnica" in r3["reply"]

        # 5. GET chat reloads persisted history cleanly (secrets redacted) and workspace file stayed untouched
        c_hist, r_hist = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert c_hist == 200
        assert len(r_hist["messages"]) == 6
        assert secret_val not in json.dumps(r_hist)
        assert app_file.read_text(encoding="utf-8") == app_src

        # Invalid mode returns 400
        c_bad, _ = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "hola", "mode": "unknown"},
        )
        assert c_bad == 400
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_import_project_and_lan_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/projects/import registers external directories (with/without manifest) as DELIVERED with external_workspace_path while rejecting symlinks, factory repo & system paths (INV-22); POST /api/settings/lan-mode toggles 0.0.0.0 LAN binding safely."""
    import os

    from agent_factory.runtime_runner import LocalAppRunner

    store = SqliteEventStore(str(tmp_path / "import_lan.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_internal")
    import subprocess

    real_run = subprocess.run

    def _fast_venv_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_list = [str(c) for c in cmd]
        if len(cmd_list) >= 4 and cmd_list[1:3] == ["-m", "venv"]:
            return real_run(
                [cmd_list[0], "-m", "venv", "--without-pip", *cmd_list[3:]],
                **kwargs,
            )
        return real_run(cmd_list, **kwargs)

    local_runner = LocalAppRunner(
        workspace_manager=ws_mgr,
        lan_ip_resolver=lambda: "192.168.1.42",
        dep_command_runner=_fast_venv_runner,
    )
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        local_runner=local_runner,
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        assert "Importar proyecto" in html
        assert "import-project-modal" in html
        assert "Acceso Wi-Fi (celular)" in html
        assert "/api/settings/lan-mode" in html

        # 1. Import project WITH .agent-factory/manifest.json
        ext_a = tmp_path / "external_music_app"
        (ext_a / ".agent-factory").mkdir(parents=True)
        (ext_a / ".agent-factory" / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "project_id": "ext-music-app",
                    "title": "Musiquita Externa",
                    "app_type": "python_local",
                    "objective": "App local importada",
                    "plan_summary": "Servidor HTTP Python",
                    "repo_url": "https://github.com/acme/ext-music-app",
                    "deploy_url": "",
                }
            ),
            encoding="utf-8",
        )
        (ext_a / "app.py").write_text(
            "import http.server, os\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = f'host={os.environ[\"HOST\"]}'.encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    def log_message(self, *a): pass\n"
            "if __name__ == '__main__':\n"
            "    http.server.ThreadingHTTPServer((os.environ['HOST'], int(os.environ['PORT'])), H).serve_forever()\n",
            encoding="utf-8",
        )

        c_imp, r_imp = _http_json(
            f"{base_url}/api/projects/import",
            method="POST",
            body={"local_path": str(ext_a), "title": ""},
        )
        assert c_imp == 200
        assert r_imp["ok"] is True
        pid_a = r_imp["project_id"]
        assert pid_a == "ext-music-app"
        assert r_imp["app_type"] == "python_local"
        assert r_imp["status"]["state"] == "DELIVERED"
        assert r_imp["status"]["name"] == "Musiquita Externa"
        assert r_imp["status"]["workspace_path"] == str(ext_a.resolve())
        assert r_imp["status"]["links"]["repo"] == "https://github.com/acme/ext-music-app"

        # 2. Import project WITHOUT manifest (static web folder)
        ext_b = tmp_path / "external_static_site"
        ext_b.mkdir()
        (ext_b / "index.html").write_text("<h1>Mi Sitio Importado</h1>\n", encoding="utf-8")
        c_imp_b, r_imp_b = _http_json(
            f"{base_url}/api/projects/import",
            method="POST",
            body={"local_path": str(ext_b), "title": "Sitio Local"},
        )
        assert c_imp_b == 200
        assert r_imp_b["app_type"] == "static"
        assert r_imp_b["status"]["state"] == "DELIVERED"
        assert r_imp_b["status"]["name"] == "Sitio Local"

        # 3. INV-22 import path rejections (factory repo, system dirs, nonexistent)
        factory_sub = Path(__file__).resolve().parents[1] / "src"
        c_fac, _ = _http_json(
            f"{base_url}/api/projects/import",
            method="POST",
            body={"local_path": str(factory_sub)},
        )
        assert c_fac == 400

        sys_dir = Path(os.environ.get("SystemRoot", "C:/Windows")) if os.name == "nt" else Path("/etc")
        c_sys, _ = _http_json(
            f"{base_url}/api/projects/import",
            method="POST",
            body={"local_path": str(sys_dir)},
        )
        assert c_sys == 400

        c_missing, _ = _http_json(
            f"{base_url}/api/projects/import",
            method="POST",
            body={"local_path": str(tmp_path / "does_not_exist")},
        )
        assert c_missing == 400

        # 4. LAN mode defaults to False -> local start binds 127.0.0.1 and has no lan_url
        _, r_lan0 = _http_json(f"{base_url}/api/settings/lan-mode")
        assert r_lan0["lan_mode"] is False

        _, s_off = _http_json(f"{base_url}/api/projects/{pid_a}/start", method="POST")
        assert s_off["local_app"]["running"] is True
        assert s_off["local_app"]["url"].startswith("http://127.0.0.1:")
        assert s_off["local_app"].get("lan_url") is None
        with urllib.request.urlopen(s_off["local_app"]["url"], timeout=5.0) as h_off:
            assert h_off.read().decode("utf-8") == "host=127.0.0.1"
        _http_json(f"{base_url}/api/projects/{pid_a}/stop", method="POST")

        # Enable LAN mode -> local start binds 0.0.0.0 and exposes lan_url 192.168.1.42
        c_lan_on, r_lan_on = _http_json(
            f"{base_url}/api/settings/lan-mode",
            method="POST",
            body={"lan_mode": True},
        )
        assert c_lan_on == 200
        assert r_lan_on["lan_mode"] is True

        _, s_on = _http_json(f"{base_url}/api/projects/{pid_a}/start", method="POST")
        assert s_on["local_app"]["running"] is True
        assert s_on["local_app"]["url"].startswith("http://127.0.0.1:")
        assert s_on["local_app"]["lan_url"].startswith("http://192.168.1.42:")
        with urllib.request.urlopen(s_on["local_app"]["url"], timeout=5.0) as h_on:
            assert h_on.read().decode("utf-8") == "host=0.0.0.0"
        _http_json(f"{base_url}/api/projects/{pid_a}/stop", method="POST")

        # Deleting imported project removes registry entry without deleting ext_a on disk!
        c_del, _ = _http_json(f"{base_url}/api/projects/{pid_a}", method="DELETE")
        assert c_del == 200
        assert (ext_a / "app.py").is_file()
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
def test_dashboard_checkpoints_and_rollback_with_inv22_and_verification(
    tmp_path: Path,
) -> None:
    """GET /checkpoints & POST /rollback list delivered commits, validate history (INV-22), stop local app, restore files cleanly via new commit, run pytest+smoke test, push to origin, and append PROJECT_DELIVERED (INV-04, INV-11)."""
    import subprocess

    from agent_factory.runtime_runner import LocalAppRunner

    pid = "proj-rollback-demo"
    store = SqliteEventStore(str(tmp_path / "rb_dash.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_rb")
    ws_dir = ws_mgr.create_workspace(pid)
    bare_remote = tmp_path / "bare_rb_origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=ws_dir, check=True)

    # Commit v1
    (ws_dir / "index.html").write_text("<!doctype html><html><body><h1>Version Uno</h1></body></html>\n", encoding="utf-8")
    (ws_dir / "tests").mkdir()
    (ws_dir / "tests" / "test_app.py").write_text(
        "from pathlib import Path\n"
        "def test_html_nonempty():\n"
        "    assert 'Version' in Path('index.html').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True)
    subprocess.run(["git", "-c", "user.name=TestBot", "-c", "user.email=test@local", "commit", "-m", "chore: v1"], cwd=ws_dir, check=True, capture_output=True)
    sha_v1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ws_dir, check=True, capture_output=True)

    # Commit v2
    (ws_dir / "index.html").write_text("<!doctype html><html><body><h1>Version Dos</h1></body></html>\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True)
    subprocess.run(["git", "-c", "user.name=TestBot", "-c", "user.email=test@local", "commit", "-m", "chore: v2"], cwd=ws_dir, check=True, capture_output=True)
    sha_v2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "origin", "main"], cwd=ws_dir, check=True, capture_output=True)

    now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Rollback Demo")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Rollback App", "objective": "Probar rollback", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": str(bare_remote), "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DELIVERED", "to": "CHANGE_REQUEST", "instructions": "Cambiar a v2"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "CHANGE_REQUEST", "to": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": str(bare_remote), "deploy_url": "", "commit_sha": sha_v2, "version_label": "v2"}, at=now),
    ):
        store.append(ev)

    local_runner = LocalAppRunner(workspace_manager=ws_mgr)
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        local_runner=local_runner,
    )
    try:
        # 1. GET /checkpoints returns v2 & v1
        c_cp, r_cp = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert c_cp == 200
        labels = [cp["version_label"] for cp in r_cp["checkpoints"]]
        assert "v1" in labels and "v2" in labels
        shas = {cp["version_label"]: cp["commit_sha"] for cp in r_cp["checkpoints"]}
        assert shas["v1"] == sha_v1 and shas["v2"] == sha_v2

        # 2. Invalid/unsafe or foreign commit SHA rejected (INV-22)
        assert _http_json(f"{base_url}/api/projects/{pid}/rollback", method="POST", body={"commit_sha": "../bad"})[0] == 400
        assert _http_json(f"{base_url}/api/projects/{pid}/rollback", method="POST", body={"commit_sha": "-flag"})[0] == 400
        assert _http_json(f"{base_url}/api/projects/{pid}/rollback", method="POST", body={"commit_sha": "deadbeef1234"})[0] == 400
        factory_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert _http_json(f"{base_url}/api/projects/{pid}/rollback", method="POST", body={"commit_sha": factory_head})[0] == 400

        # 3. Start local app first, then execute rollback -> stops local app, restores v1 cleanly, commits & pushes, records DELIVERED v3
        c_st, r_st = _http_json(f"{base_url}/api/projects/{pid}/start", method="POST")
        assert c_st == 200 and r_st["local_app"]["running"] is True

        c_rb, r_rb = _http_json(
            f"{base_url}/api/projects/{pid}/rollback",
            method="POST",
            body={"commit_sha": sha_v1},
            timeout=25.0,
        )
        assert c_rb == 200
        assert r_rb["ok"] is True
        assert r_rb["status"]["state"] == "DELIVERED"
        assert local_runner.get_status(pid).running is False
        assert "Version Uno" in (ws_dir / "index.html").read_text(encoding="utf-8")

        new_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True).stdout.strip()
        remote_head = subprocess.run(["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"], check=True, capture_output=True, text=True).stdout.strip()
        assert new_head == remote_head
        assert new_head not in (sha_v1, sha_v2)

        _, r_cp2 = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert len(r_cp2["checkpoints"]) == 3
        assert r_cp2["checkpoints"][0]["version_label"] == "v3"
        assert r_cp2["checkpoints"][0]["commit_sha"] == new_head
        assert r_cp2["checkpoints"][0]["rollback_from"] == sha_v1
        assert r_cp2["checkpoints"][0]["is_current"] is True
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
def test_dashboard_dev_view_files_diff_target_files_and_live_preview(
    tmp_path: Path,
) -> None:
    """GET /files, GET /diff (redacting secrets INV-19), POST /modify target_files validation (INV-13/22), and UI controls for Vista Simple/Desarrollador, Rollback, Diff & Live Preview."""
    import subprocess

    from agent_factory.runtime_runner import LocalAppRunner

    pid = "proj-dev-view"
    secret_val = "TEST_DIFF_SECRET_8899"
    store = SqliteEventStore(str(tmp_path / "dev_dash.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_dev")
    ws_dir = ws_mgr.create_workspace(pid)
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "--local", "user.name", "TestBot"], cwd=ws_dir, check=True)
    subprocess.run(["git", "config", "--local", "user.email", "test@local"], cwd=ws_dir, check=True)

    (ws_dir / "src").mkdir()
    (ws_dir / "index.html").write_text("<h1>Hola</h1>\n", encoding="utf-8")
    (ws_dir / "src" / "app.js").write_text("export const v = 1;\n", encoding="utf-8")
    (ws_dir / ".env").write_text(f"API_KEY={secret_val}\n", encoding="utf-8")
    (ws_dir / "tests").mkdir()
    (ws_dir / "tests" / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "index.html", "src/app.js", "tests/test_app.py"], cwd=ws_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: initial"], cwd=ws_dir, check=True, capture_output=True)

    (ws_dir / "src" / "app.js").write_text(
        f"export const v = 2;\nexport const hint = '{secret_val}';\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/app.js"], cwd=ws_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feat: bump v2"], cwd=ws_dir, check=True, capture_output=True)

    now = datetime(2026, 11, 20, 18, 30, tzinfo=UTC)
    plan_dict = _plan_payload("Dev View App")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Dev View", "objective": "Modo dev", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    seen_impl_failure: list[str] = []

    def _capture_impl(ws: Path, plan_obj: Any, failure_out: str, _attempt: int) -> Any:
        seen_impl_failure.append(f"{plan_obj.summary}\n{failure_out}")
        from agent_factory.ports.code_agent import CodeAgentResult

        return CodeAgentResult(files_written=["src/app.js"], summary="Updated app.js")

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_mod.py": "def test_m(): assert True\n"}],
            impl_steps=[_capture_impl],
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "MOD-02": True})]),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="ok")]),
    )
    local_runner = LocalAppRunner(workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        workspace_manager=ws_mgr,
        local_runner=local_runner,
        secrets_to_redact=(secret_val,),
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")
        for needle in (
            "Vista Simple",
            "Vista Desarrollador",
            "Modo técnico",
            "af_tech_mode",
            "Versiones estables (Rollback)",
            "Restaurar esta versión",
            "Últimos cambios",
            "git diff",
            "modify-target-files",
            "Mostrar vista previa aquí",
            "Recargar vista previa",
            "live-preview-iframe",
        ):
            assert needle in html

        # 1. GET /files hides .env and lists real project files
        c_files, r_files = _http_json(f"{base_url}/api/projects/{pid}/files")
        assert c_files == 200
        assert "src/app.js" in r_files["files"]
        assert "index.html" in r_files["files"]
        assert ".env" not in r_files["files"]

        # 2. GET /diff returns latest commit diff and redacts secrets (INV-19)
        c_diff, r_diff = _http_json(f"{base_url}/api/projects/{pid}/diff")
        assert c_diff == 200
        assert r_diff["has_git"] is True
        assert "export const v = 2;" in r_diff["diff"]
        assert secret_val not in r_diff["diff"]
        assert "[REDACTED]" in r_diff["diff"]

        # 3. target_files validation & forwarding in POST /modify
        assert _http_json(f"{base_url}/api/projects/{pid}/modify", method="POST", body={"instructions": "Fix", "target_files": ["../outside.py"]})[0] == 400
        assert _http_json(f"{base_url}/api/projects/{pid}/modify", method="POST", body={"instructions": "Fix", "target_files": [".env"]})[0] == 400
        assert _http_json(f"{base_url}/api/projects/{pid}/modify", method="POST", body={"instructions": "Fix", "target_files": ["tests/test_app.py"]})[0] == 400

        c_mod, r_mod = _http_json(
            f"{base_url}/api/projects/{pid}/modify",
            method="POST",
            body={"instructions": "Refactor helper", "target_files": ["src/app.js"]},
        )
        assert c_mod == 200 and r_mod["background_started"] is True
        assert server.controller.wait_for_idle(timeout=5.0) is True
        assert any("src/app.js" in txt for txt in seen_impl_failure)
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
def test_dashboard_rollback_resets_cleanly_when_post_rollback_tests_fail(
    tmp_path: Path,
) -> None:
    """When pytest fails during POST /rollback, workspace is reset back to pre-rollback commit cleanly, 409 is returned, and no PROJECT_DELIVERED is recorded."""
    import subprocess

    pid = "proj-rb-fail-reset"
    store = SqliteEventStore(str(tmp_path / "rb_fail.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_rb_fail")
    ws_dir = ws_mgr.create_workspace(pid)
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)

    (ws_dir / "app.txt").write_text("broken-v1\n", encoding="utf-8")
    (ws_dir / "tests").mkdir()
    (ws_dir / "tests" / "test_check.py").write_text(
        "from pathlib import Path\n"
        "def test_ok():\n"
        "    assert Path('app.txt').read_text(encoding='utf-8').strip() == 'healthy-v2'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@l", "commit", "-m", "v1"],
        cwd=ws_dir,
        check=True,
        capture_output=True,
    )
    sha_v1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    (ws_dir / "app.txt").write_text("healthy-v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=ws_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@l", "commit", "-m", "v2"],
        cwd=ws_dir,
        check=True,
        capture_output=True,
    )
    sha_v2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    now = datetime(2026, 11, 20, 19, 0, tzinfo=UTC)
    plan_dict = _plan_payload("RB Fail Plan")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "RB Fail", "objective": "Obj", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": sha_v2, "version_label": "v2"}, at=now),
    ):
        store.append(ev)

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
    )
    try:
        c_rb, r_rb = _http_json(
            f"{base_url}/api/projects/{pid}/rollback",
            method="POST",
            body={"commit_sha": sha_v1},
            timeout=20.0,
        )
        assert c_rb == 409
        assert "fallaron" in r_rb["error"].lower()
        assert (ws_dir / "app.txt").read_text(encoding="utf-8").strip() == "healthy-v2"
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True
        ).stdout.strip()
        assert head_after == sha_v2
        assert len([e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED]) == 1
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_ux_redesign_progress_timer_header_and_four_tabs(tmp_path: Path) -> None:
    """UX redesign exposes live timer banner, heartbeat dot, clean header menu, and 4 unified tabs."""
    store = SqliteEventStore(db_path=tmp_path / "ux_redesign.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_ux")
    pid = "ux-app"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    plan_dict = _plan_payload("UX App Plan")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "UX App", "objective": "Probar UX", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "https://github.com/acme/ux-app", "deploy_url": "", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        # 1. Global live activity banner + timer + 75s stall detection + heartbeat dot
        for marker in (
            'id="live-activity-banner"',
            'id="live-activity-timer"',
            "live-spinner-svg",
            "00:00s",
            "Reintentar / Recargar estado",
            "puede seguir procesando o haberse bloqueado",
            'id="server-health-dot"',
            "Servidor desconectado",
        ):
            assert marker in html

        # 2. Clean minimalist header without duplicate developer label + settings dropdown
        assert "Vista Desarrollador (Vista Desarrollador)" not in html
        for marker in (
            'id="view-mode-segmented"',
            'id="settings-menu-btn"',
            'id="settings-popover"',
            "Ajustes",
        ):
            assert marker in html

        # 3. Unified 4 tabs (Consultar y Auditar, Pedir cambios, Resumen y Plan, Historial)
        for tab_marker in (
            'id="tab-audit-btn"',
            'id="tab-modify-btn"',
            "Consultar y Auditar",
            "Pedir cambios",
            "Resumen y Plan",
            "Historial",
            'id="tab-modify"',
        ):
            assert tab_marker in html

        # Exactly 5 tab buttons in .tabs bar (including Auto-Mejoras)
        tabs_start = html.index('<div class="tabs">')
        tabs_end = html.index("</div>", tabs_start)
        tabs_block = html[tabs_start:tabs_end]
        assert tabs_block.count('class="tab-btn') == 5
        assert 'id="tab-ideas-btn"' in tabs_block
        assert 'data-tab="ideas"' in tabs_block
        assert 'data-tab="audit"' in tabs_block
        assert 'data-tab="modify"' in tabs_block
        assert 'data-tab="spec"' in tabs_block
        assert 'data-tab="events"' in tabs_block
        assert 'data-tab="plan"' not in tabs_block
        assert tabs_block.index("tab-audit-btn") < tabs_block.index("tab-modify-btn")

        # 4. DELIVERED top banner stays compact without modify/rollback forms inline
        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        delivered_block = html[fn_start:fn_end].split("st.state === 'DELIVERED'", 1)[1]
        assert 'id="modify-project-input"' not in delivered_block
        assert 'id="rollback-version-select"' not in delivered_block
        assert 'id="git-diff-details"' not in delivered_block
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_blocks_b0_b1_b2_b3_a11y_responsive_feedback_and_simple_mode(
    tmp_path: Path,
) -> None:
    """Blocks B0-B3: tokens/a11y, responsive breakpoints/switcher, unified feedback/AbortController, Simple mode clarity."""
    store = SqliteEventStore(db_path=tmp_path / "blocks_b0_b3.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_b0_b3")
    pid = "b0-b3-app"
    now = datetime(2026, 4, 21, 9, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Plan B0-B3")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "App B0", "objective": "Probar B0-B3", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": "abc1234", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        # Never introduce external CDNs or https:// links in DASHBOARD_HTML
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # B0: Tokens, focus-visible, reduced motion, contrast, and dead CSS cleanup
        for b0_marker in (
            "--fs-",
            "--sp-",
            ":focus-visible",
            "prefers-reduced-motion",
        ):
            assert b0_marker in html
        assert ".tech-toggle {" not in html
        assert ".tech-mode-hint" not in html
        assert "versions-box" not in html

        # B1: Responsive layout, coarse touch targets, switcher, scrollIntoView, minmax, fullscreen sheet, modal dvh
        for b1_marker in (
            "@media (max-width: 600px)",
            "@media (max-width: 1023px)",
            "@media (max-width: 1279px)",
            "@media (pointer: coarse)",
            "min-height: 44px",
            'id="mobile-project-switcher"',
            "scrollIntoView",
            "minmax(0, 1fr)",
            "100dvh",
        ):
            assert b1_marker in html

        # B2: Unified progress banner, STALL_BG_MINUTES, AbortController timeouts, toast stack, panel signatures
        for b2_marker in (
            'id="toast-stack"',
            "AbortController",
            "STALL_BG_MINUTES = 10",
            "lastListSignature",
            "lastSpecSignature",
            "lastEventsSignature",
            "Reintentar",
        ):
            assert b2_marker in html

        # B3: Clear Simple mode vs tech-only isolation, rollback button + custom modal, examples, optimistic state
        for b3_marker in (
            'id="example-prompts"',
            'id="simple-rollback-btn"',
            'id="confirm-action-modal"',
            "Qué se va a construir",
            "Verificaciones:",
            "simple-only",
            "Respuesta enviada · Analizando",
            "Volver a la versión anterior",
        ):
            assert b3_marker in html

        # Verify Modelo: and activity_detail in buildGuidanceBanner are isolated as tech-only
        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        running_block = html[fn_start:fn_end].split("if (st.state === 'INTERVIEW')", 1)[0]
        assert "model-pill tech-only" in running_block
        assert "banner-subdesc tech-only" in running_block
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_blocks_b4_b5_b6_audit_index_markdown_dev_ergonomics_and_aria(
    tmp_path: Path,
) -> None:
    """Blocks B4-B6: versioned audit chat persistence, audit index + XSS-safe Markdown, dev diff/files/timeline, and WAI-ARIA."""
    store = SqliteEventStore(db_path=tmp_path / "blocks_b4_b6.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_b4_b6")
    pid = "b4-b6-app"
    now = datetime(2026, 4, 21, 11, 0, tzinfo=UTC)
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<h1>Hola</h1>\n", encoding="utf-8")

    sha_v3 = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    plan_dict = _plan_payload("Plan B4-B6")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "App B4-B6", "objective": "Auditar y ARIA", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": sha_v3, "version_label": "v3"}, at=now),
    ):
        store.append(ev)

    chats_dir = tmp_path / "chats"
    chats_dir.mkdir(parents=True)
    # Seed legacy entry without commit_sha / version_label to verify backwards compatibility
    (chats_dir / f"{pid}.json").write_text(
        json.dumps([
            {
                "id": "old-msg-1",
                "role": "assistant",
                "mode": "document",
                "content": "# Doc antiguo\nTexto previo",
                "document_title": "old-doc.md",
                "created_at": now.isoformat(),
            }
        ]),
        encoding="utf-8",
    )

    llm = FakeLLM(
        responses=[
            {
                "reply": "# Informe de Auditoría\n\n<script>alert('xss')</script>\n\n## Hallazgos\n- Revisar **seguridad** en `index.html`.",
                "suggested_changes": "Escapar salidas HTML.",
                "document_title": "b4-b6-audit.md",
            }
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
    )
    try:
        # 1. POST /chat persists commit_sha and version_label and GET /chat returns both + legacy compatibility
        p_code, p_res = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "", "mode": "audit"},
        )
        assert p_code == 200
        assert p_res["commit_sha"] == sha_v3
        assert p_res["version_label"] == "v3"
        assert p_res["history"][-1]["commit_sha"] == sha_v3
        assert p_res["history"][-1]["version_label"] == "v3"

        g_code, g_res = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert g_code == 200
        msgs = g_res["messages"]
        assert len(msgs) == 3
        assert msgs[0]["id"] == "old-msg-1"
        assert msgs[0].get("commit_sha", "") == ""
        assert msgs[1]["commit_sha"] == sha_v3
        assert msgs[1]["version_label"] == "v3"
        assert msgs[2]["commit_sha"] == sha_v3
        assert msgs[2]["version_label"] == "v3"

        # Verify persisted JSON on disk also contains commit_sha and version_label
        saved_json = json.loads((chats_dir / f"{pid}.json").read_text(encoding="utf-8"))
        assert saved_json[-1]["commit_sha"] == sha_v3
        assert saved_json[-1]["version_label"] == "v3"

        # 2. HTML UI checks for B4, B5, B6 (zero external links / CDN)
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # B4: Audit index, sub-views, readonly modify notice, TOC, and XSS-safe renderSafeMarkdown
        for b4_marker in (
            'id="audit-index"',
            "Auditorías y documentos",
            "Preguntas",
            "Volver al índice",
            "Disponible al terminar los cambios",
            "function renderSafeMarkdown(",
        ):
            assert b4_marker in html

        md_fn_start = html.index("function renderSafeMarkdown(")
        md_fn_end = html.index("function renderAuditChatTab(", md_fn_start)
        md_fn_body = html[md_fn_start:md_fn_end]
        assert "escapeHtml(" in md_fn_body

        # B5: Dev ergonomics — searchable file picker, colored per-file diff, vertical versions timeline
        for b5_marker in (
            'id="target-files-search"',
            ".diff-line-add",
            ".diff-line-del",
            ".diff-line-hunk",
            'id="versions-timeline"',
            "Copiar ruta",
            "Copiar hash",
        ):
            assert b5_marker in html

        # B6: WAI-ARIA tablist/tab/tabpanel + arrows, dialog aria-modal + focus trap, clock aria-hidden
        for b6_marker in (
            'role="tablist"',
            'role="tab"',
            'role="tabpanel"',
            "aria-selected",
            "aria-controls",
            "ArrowRight",
            "ArrowLeft",
            'role="dialog"',
            'aria-modal="true"',
            "trapDialogFocus",
        ):
            assert b6_marker in html

        # Timer element must be aria-hidden and NOT inside an aria-live ancestor
        banner_start = html.index('id="live-activity-banner"')
        banner_end = html.index('id="live-activity-stall"', banner_start)
        banner_snippet = html[banner_start:banner_end]
        assert 'id="live-activity-timer"' in banner_snippet
        assert 'aria-hidden="true"' in banner_snippet[banner_snippet.index('id="live-activity-timer"'):]
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_second_audit_fixes_layers_mobile_md_dev_aria(
    tmp_path: Path,
) -> None:
    """Second audit P0-P2 fixes: opaque banner layers, mobile padding/ellipsis, callback MD ($$/$&), B5 signature/diff, and ARIA."""
    store = SqliteEventStore(db_path=tmp_path / "audit2_fixes.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_audit2")
    pid = "audit2-app"
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<h1>Audit2</h1>\n", encoding="utf-8")

    plan_dict = _plan_payload("Plan Audit2")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "App Audit2", "objective": "Validar parches P0-P2", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": "deadbeef1234", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    md_with_dollar_patterns = (
        "#### Nivel 4 con código\n\n"
        "Inline con patrones especiales: `echo $$ y $& sin alterar`.\n\n"
        "```c++   \n"
        'const char* pattern = "$$ y $& intactos";\n'
        "```\n"
    )
    llm = FakeLLM(
        responses=[
            {
                "reply": md_with_dollar_patterns,
                "suggested_changes": "Mantener patrones intactos.",
                "document_title": "audit2-dollar-patterns.md",
            }
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    chats_dir = tmp_path / "chats_audit2"
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
    )
    try:
        # 0. Verify Markdown audit payload containing $$ and $& and #### round-trips cleanly
        p_code, p_res = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "", "mode": "audit"},
        )
        assert p_code == 200
        assert "$$ y $& intactos" in p_res["reply"]
        assert "#### Nivel 4 con código" in p_res["reply"]

        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        # Never introduce external CDNs or https:// links
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # 1. P0 · Opaque layered background & stacking order on sticky banner / header / preview
        hdr_start = html.index("header {")
        hdr_end = html.index("}", hdr_start)
        assert "z-index: 30;" in html[hdr_start:hdr_end]

        shell_start = html.index(".live-activity-shell {")
        shell_end = html.index("}", shell_start)
        shell_css = html[shell_start:shell_end]
        assert "top: 56px;" in shell_css
        assert "z-index: 20;" in shell_css

        banner_css_start = html.index(".live-activity-banner {")
        stall_css_end = html.index("}", html.index(".live-activity-banner.stall {"))
        banner_css = html[banner_css_start:stall_css_end]
        assert "linear-gradient(var(--info-bg), var(--info-bg)), var(--surface)" in banner_css
        assert "linear-gradient(var(--warn-bg), var(--warn-bg)), var(--surface)" in banner_css
        assert "backdrop-filter: blur(10px)" in banner_css
        assert "has-live-banner" in html

        # 2. P0 · Mobile 360px bottom clearance & switcher ellipsis
        mob_start = html.index("@media (max-width: 600px)")
        mob_end = html.index("@media (pointer: coarse)", mob_start)
        mob_css = html[mob_start:mob_end]
        assert "padding-bottom: 92px;" in mob_css

        sw_css_start = html.index("#mobile-project-switcher")
        sw_css_end = html.index("}", sw_css_start)
        sw_css = html[sw_css_start:sw_css_end]
        assert "overflow: hidden;" in sw_css
        assert "text-overflow: ellipsis;" in sw_css
        assert "white-space: nowrap;" in sw_css

        # 3. P1 · B5 syncTargetFilesSelection updates lastModifySignature & parseUnifiedDiffFiles supports quotes/rename/binary
        sync_start = html.index("function syncTargetFilesSelection(")
        sync_end = html.index("function wireChecklistInputs(", sync_start)
        sync_body = html[sync_start:sync_end]
        assert "state.lastModifySignature = computeModifySignature(pid);" in sync_body

        diff_start = html.index("function parseUnifiedDiffFiles(")
        diff_end = html.index("function renderDiffLineHtml(", diff_start)
        diff_body = html[diff_start:diff_end]
        assert "rename from " in diff_body
        assert "rename to " in diff_body
        assert "Binary files" in diff_body
        assert "unquoteGitDiffPath" in diff_body

        # 4. P1 · B4 renderSafeMarkdown & formatSafeInlineMd use callback replacement for $$ / $&, #{1,6}, c++, and renderModifyTab mount
        inline_md_start = html.index("function formatSafeInlineMd(")
        inline_md_end = html.index("function renderSafeMarkdown(", inline_md_start)
        inline_md_body = html[inline_md_start:inline_md_end]
        assert "() => html" in inline_md_body

        md_start = html.index("function renderSafeMarkdown(")
        md_end = html.index("function inferAuditVersionBadge(", md_start)
        md_body = html[md_start:md_end]
        assert "() => blockHtml" in md_body
        assert "#{1,6}" in md_body
        assert "replace('@@AF_FCODE_' + idx + '@@', blockHtml)" not in md_body

        apply_start = html.index('button[data-audit-apply]')
        apply_end = html.index("async function loadProjectChatHistory(", apply_start)
        apply_body = html[apply_start:apply_end]
        assert "renderModifyTab(" in apply_body

        # 5. P2 · ARIA role="presentation" on .tabs & strengthened trapDialogFocus
        assert "document.querySelectorAll('.tabs')" in html
        assert "'role', 'presentation'" in html

        trap_start = html.index("function trapDialogFocus(")
        trap_end = html.index("function askCustomConfirm(", trap_start)
        trap_body = html[trap_start:trap_end]
        assert "!dialogEl.contains(document.activeElement)" in trap_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_audit_lifecycle_endpoints_retry_delete_and_ui_cards(
    tmp_path: Path,
) -> None:
    """Dashboard exposes POST /chat/<id>/retry and DELETE /chat/<id> (INV-22), surfaces running/timed_out/interrupted/failed/completed audit cards with 250s /chat timeout and instant refresh."""
    from datetime import timedelta

    from agent_factory.ports.llm import LLMError

    class _FixedClock:
        def __init__(self, current: datetime) -> None:
            self._current = current

        def now(self) -> datetime:
            return self._current

        def sleep(self, seconds: float) -> None:
            pass

    pid = "audit-ui-life"
    secret_val = "DASH_AUDIT_SECRET_321"
    clock = _FixedClock(datetime(2026, 4, 22, 16, 0, tzinfo=UTC))
    store = SqliteEventStore(db_path=tmp_path / "dash_audit_life.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_dash_life")
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")

    plan_dict = _plan_payload("Plan Audit UI Lifecycle")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Audit UI", "objective": "Probar ciclo persistente", "state": "INTERVIEW"}, at=clock.now()),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=clock.now()),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=clock.now()),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=clock.now()),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=clock.now()),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=clock.now()),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": "beef12345678", "version_label": "v1"}, at=clock.now()),
    ):
        store.append(ev)

    llm = FakeLLM(
        responses=[
            LLMError(f"agy CLI timed out reading code ({secret_val})"),
            {
                "reply": "# Informe Tras Reintento\nTodo verificado tras reintentar.",
                "suggested_changes": "Ninguno.",
                "document_title": "audit-reintentada.md",
            },
        ]
    )
    orch = Orchestrator(store=store, llm=llm, clock=clock, workspace_manager=ws_mgr)
    chats_dir = tmp_path / "chats_dash_life"
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
        secrets_to_redact=(secret_val,),
    )
    try:
        # 1. Failed/timed_out POST /chat returns 409 cleanly while persisting timed_out entry & redacted detail
        c_fail, r_fail = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "", "mode": "audit"},
        )
        assert c_fail == 409
        assert secret_val not in r_fail["error"]

        c_hist1, r_hist1 = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert c_hist1 == 200
        failed_card = r_hist1["messages"][-1]
        entry_id = failed_card["id"]
        assert failed_card["status"] == "timed_out"
        assert failed_card["stage_label"] == "2/3 Analizando código con IA"
        assert secret_val not in failed_card["error_detail"]
        assert "[REDACTED]" in failed_card["error_detail"]

        # 2. Retry via POST /api/projects/<id>/chat/<entry_id>/retry completes existing card
        c_retry, r_retry = _http_json(
            f"{base_url}/api/projects/{pid}/chat/{entry_id}/retry",
            method="POST",
            body={},
        )
        assert c_retry == 200
        assert r_retry["status"] == "completed"
        assert "Informe Tras Reintento" in r_retry["reply"]
        assert r_retry["history"][-1]["id"] == entry_id
        assert r_retry["history"][-1]["status"] == "completed"
        assert r_retry["history"][-1]["stage_label"] == "Completada"

        # 3. Orphan 'running' card returns as 'interrupted' on GET /chat and 409 when retrying an active running one
        chat_file = chats_dir / f"{pid}.json"
        saved = json.loads(chat_file.read_text(encoding="utf-8"))
        saved.append(
            {
                "id": "a-active-run",
                "role": "assistant",
                "mode": "audit",
                "prompt": "Activa",
                "content": "",
                "status": "running",
                "stage_label": "1/3 Leyendo archivos del repositorio",
                "server_boot_id": orch._server_boot_id,
                "created_at": clock.now().isoformat(),
            }
        )
        saved.append(
            {
                "id": "a-stale-run",
                "role": "assistant",
                "mode": "document",
                "prompt": "Vencida",
                "content": "",
                "status": "running",
                "stage_label": "2/3 Analizando código con IA",
                "server_boot_id": orch._server_boot_id,
                "created_at": (clock.now() - timedelta(seconds=280)).isoformat(),
            }
        )
        chat_file.write_text(json.dumps(saved), encoding="utf-8")

        c_active_retry, _ = _http_json(
            f"{base_url}/api/projects/{pid}/chat/a-active-run/retry",
            method="POST",
            body={},
        )
        assert c_active_retry == 409

        c_hist2, r_hist2 = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert c_hist2 == 200
        by_id = {m["id"]: m for m in r_hist2["messages"]}
        assert by_id["a-stale-run"]["status"] == "interrupted"
        assert "2/3 Analizando código con IA" in by_id["a-stale-run"]["error_detail"]

        # 4. DELETE /api/projects/<id>/chat/<entry_id> deletes record, 404 when absent, 400 on bad entry_id
        c_del, r_del = _http_json(
            f"{base_url}/api/projects/{pid}/chat/a-stale-run",
            method="DELETE",
        )
        assert c_del == 200
        assert "a-stale-run" not in [m["id"] for m in r_del["messages"]]

        c_del_missing, _ = _http_json(
            f"{base_url}/api/projects/{pid}/chat/a-stale-run",
            method="DELETE",
        )
        assert c_del_missing == 404

        c_bad_id, _ = _http_json(
            f"{base_url}/api/projects/{pid}/chat/bad%20id!",
            method="DELETE",
        )
        assert c_bad_id == 400

        # 5. HTML UI checks: 250000ms /chat timeout, live status badges, stage diagnostic, retry & delete controls
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        assert "250000" in html
        for marker in (
            "⏳ En curso",
            "✅ Completada",
            "⚠️ Tiempo agotado / Interrumpida",
            "❌ Error",
            "🔄 Reintentar",
            "🗑️ Eliminar",
            "data-audit-retry",
            "data-audit-delete",
            "1/3 Leyendo archivos del repositorio",
        ):
            assert marker in html
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_failed_interview_retry_regenerates_questions_and_empty_interview_banner(
    tmp_path: Path,
) -> None:
    """Retrying a project failed in INTERVIEW or stuck in INTERVIEW without questions regenerates InterviewerAgent questions, and UI suppresses 'Esperando tus respuestas' when questions is empty."""
    pid_fail = "pagina-responsiva"
    store = SqliteEventStore(db_path=tmp_path / "retry_interview.db")

    first_fail_done = threading.Event()

    class _FailFirstInterviewLLM(FakeLLM):
        def complete(
            self,
            messages: list[dict[str, str]],
            schema: Any = None,
        ) -> dict[str, Any]:
            try:
                return super().complete(messages=messages, schema=schema)
            except LLMError:
                first_fail_done.set()
                raise

    llm = _FailFirstInterviewLLM(
        responses=[
            # 1. Create pagina-responsiva -> first interview attempt fails with transport error -> FAILED
            LLMError("Initial agy CLI failure during interview"),
            # 2. Retry pagina-responsiva -> InterviewerAgent succeeds with 2 clarifying questions
            {
                "spec": {
                    "objective": "Pagina responsiva",
                    "users": ["Visitantes móviles"],
                    "features": [],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": [
                    "¿Qué secciones principales tendrá tu página responsiva?",
                    "¿Qué estilo visual o paleta de colores prefieres?",
                ],
            },
            # 3. Retry project already stuck in INTERVIEW without questions -> regenerates questions cleanly
            {
                "spec": {
                    "objective": "Portafolio interactivo",
                    "users": ["Reclutadores"],
                    "features": ["Proyectos"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["¿Deseas incluir formulario de contacto?"],
            },
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        # 1. Verify HTML buildGuidanceBanner behavior when questions is empty vs populated
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        fn_body = html[fn_start:fn_end]
        _, after_int = fn_body.split("if (st.state === 'INTERVIEW')", 1)
        interview_block, _ = after_int.split("if (st.state === 'WAITING_APPROVAL')", 1)
        assert "if (!questions.length)" in interview_block
        _empty_q_branch, populated_q_branch = interview_block.split("if (!questions.length)", 1)
        populated_after_empty = populated_q_branch.split("return '<div class=\"guidance-banner", 2)
        empty_return = populated_after_empty[1]
        assert "Aún no se han generado las preguntas de aclaración" in empty_return
        assert "Generar preguntas ahora" in empty_return
        assert "retry-interview" in empty_return
        assert "Esperando tus respuestas" not in empty_return
        assert '<ol class="question-list">' in populated_after_empty[2]
        assert "Esperando tus respuestas" in populated_after_empty[2]

        # 2. Create project -> first interview fails cleanly into FAILED
        c_code, c_res = _http_json(
            f"{base_url}/api/projects",
            method="POST",
            body={"prompt": "Pagina responsiva"},
        )
        assert c_code == 200
        assert c_res["project_id"] == pid_fail
        assert first_fail_done.wait(timeout=5.0) is True
        assert server.controller.wait_for_idle(timeout=5.0) is True

        _, detail_failed = _http_json(f"{base_url}/api/projects/{pid_fail}")
        assert detail_failed["status"]["state"] == "FAILED"
        assert not any(
            ev["type"] == EventType.INTERVIEW_ROUND and ev["payload"].get("questions")
            for ev in detail_failed["events"]
        )

        # 3. POST /api/projects/<id>/retry -> regenerates InterviewerAgent questions & returns INTERVIEW
        r_code, r_res = _http_json(
            f"{base_url}/api/projects/{pid_fail}/retry",
            method="POST",
            body={"clean_workspace": False},
        )
        assert r_code == 200
        assert r_res["status"]["state"] == "INTERVIEW"

        g_code, g_detail = _http_json(f"{base_url}/api/projects/{pid_fail}")
        assert g_code == 200
        assert g_detail["status"]["state"] == "INTERVIEW"
        round_events = [
            ev for ev in g_detail["events"] if ev["type"] == EventType.INTERVIEW_ROUND
        ]
        assert len(round_events) == 1
        assert round_events[0]["payload"]["questions"] == [
            "¿Qué secciones principales tendrá tu página responsiva?",
            "¿Qué estilo visual o paleta de colores prefieres?",
        ]

        # 4. Project already in INTERVIEW with zero questions -> retry regenerates questions directly
        stuck_pid = "stuck-interview"
        orch.create_project(stuck_pid, "Portafolio", "Portafolio interactivo")
        assert orch.get_status(stuck_pid).state == ProjectState.INTERVIEW

        r2_code, r2_res = _http_json(
            f"{base_url}/api/projects/{stuck_pid}/retry",
            method="POST",
            body={"clean_workspace": False},
        )
        assert r2_code == 200
        assert r2_res["status"]["state"] == "INTERVIEW"

        _, g2_detail = _http_json(f"{base_url}/api/projects/{stuck_pid}")
        round2 = [ev for ev in g2_detail["events"] if ev["type"] == EventType.INTERVIEW_ROUND]
        assert len(round2) == 1
        assert round2[0]["payload"]["questions"] == ["¿Deseas incluir formulario de contacto?"]
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_normalizes_empty_and_null_interview_questions_and_keeps_generate_button_and_textarea(
    tmp_path: Path,
) -> None:
    """Backend/UI strip blank or null INTERVIEW questions ([""]/None -> []), keep #generate-questions-btn above an enabled textarea, and handle aged PROJECT_RETRIED."""
    store = SqliteEventStore(db_path=tmp_path / "clean_questions.db")
    old_at = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)

    # 1. Project in INTERVIEW whose latest INTERVIEW_ROUND has only blank/null entries
    pid_blank = "pagina-responsiva-vacia"
    store.append(
        create_event(
            pid_blank,
            EventType.PROJECT_CREATED,
            {"name": "Pagina responsiva", "objective": "Sitio web responsive", "state": "INTERVIEW"},
            at=old_at,
        )
    )
    store.append(
        create_event(
            pid_blank,
            EventType.INTERVIEW_ROUND,
            {"questions": ["", "   ", None], "is_complete": False, "terminated_by_limit": False},
            at=old_at,
        )
    )

    # 2. Project in INTERVIEW with None questions after a prior failed retry aged several hours
    pid_none_retried = "pagina-responsiva-none"
    store.append(
        create_event(
            pid_none_retried,
            EventType.PROJECT_CREATED,
            {"name": "Pagina responsiva 2", "objective": "Landing responsive", "state": "INTERVIEW"},
            at=old_at,
        )
    )
    store.append(
        create_event(
            pid_none_retried,
            EventType.INTERVIEW_ROUND,
            {"questions": None, "is_complete": False, "terminated_by_limit": False},
            at=old_at,
        )
    )
    store.append(
        create_event(
            pid_none_retried,
            EventType.PROJECT_FAILED,
            {"failed_from": "INTERVIEW", "reason": "Fallo previo"},
            at=old_at,
        )
    )
    store.append(
        create_event(
            pid_none_retried,
            EventType.PROJECT_RETRIED,
            {"from": "FAILED", "to": "INTERVIEW", "failed_from": "INTERVIEW"},
            at=old_at,
        )
    )

    # 3. Project with mixed whitespace/null + real questions keeps only clean trimmed strings
    pid_mixed = "pagina-responsiva-mix"
    store.append(
        create_event(
            pid_mixed,
            EventType.PROJECT_CREATED,
            {"name": "Pagina mix", "objective": "Sitio mixto", "state": "INTERVIEW"},
            at=old_at,
        )
    )
    store.append(
        create_event(
            pid_mixed,
            EventType.INTERVIEW_ROUND,
            {
                "questions": ["", None, "  ¿Qué secciones necesitas?  ", "   "],
                "is_complete": False,
                "terminated_by_limit": False,
            },
            at=old_at,
        )
    )

    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        # Orchestrator.get_project_detail returns normalized clean list[str]
        orch_blank = orch.get_project_detail(pid_blank)
        assert orch_blank["questions"] == []
        orch_none = orch.get_project_detail(pid_none_retried)
        assert orch_none["questions"] == []
        orch_mixed = orch.get_project_detail(pid_mixed)
        assert orch_mixed["questions"] == ["¿Qué secciones necesitas?"]

        # GET /api/projects/<id> mirrors clean questions=[] for [""] and None
        c1, d1 = _http_json(f"{base_url}/api/projects/{pid_blank}")
        assert c1 == 200
        assert d1["questions"] == []
        c2, d2 = _http_json(f"{base_url}/api/projects/{pid_none_retried}")
        assert c2 == 200
        assert d2["questions"] == []
        c3, d3 = _http_json(f"{base_url}/api/projects/{pid_mixed}")
        assert c3 == 200
        assert d3["questions"] == ["¿Qué secciones necesitas?"]

        # GET / UI filters d.questions (even [""] / null), renders #generate-questions-btn above enabled textarea
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        fn_body = html[fn_start:fn_end]
        _, after_int = fn_body.split("if (st.state === 'INTERVIEW')", 1)
        interview_block, _ = after_int.split("if (st.state === 'WAITING_APPROVAL')", 1)

        assert "cleanQuestions" in interview_block
        assert "Array.isArray(d.questions)" in interview_block
        assert ".filter(Boolean)" in interview_block

        _, populated_q_branch = interview_block.split("if (!questions.length)", 1)
        populated_after_empty = populated_q_branch.split("return '<div class=\"guidance-banner", 2)
        empty_return = populated_after_empty[1]
        assert "Aún no se han generado las preguntas de aclaración" in empty_return
        assert 'id="generate-questions-btn"' in empty_return
        assert "Generar preguntas ahora" in empty_return
        assert 'id="interview-reply-input"' in empty_return
        assert 'id="interview-reply-btn"' in empty_return
        assert empty_return.index("generate-questions-btn") < empty_return.index("interview-reply-input")
        assert "Esperando tus respuestas" not in empty_return
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_quality_auditor_adaptive_weights_ui_vs_cli_and_dashboard_renders_scorecard(
    tmp_path: Path,
) -> None:
    """Test 4: Adaptive UI vs pure-CLI weights (ux_ui redistributed equally across robustness/architecture/testing), tolerant fallback + 240s timeout, and Dashboard scorecard rendering."""
    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.domain.models import Spec

    # 1. Workspaces: web frontend UI vs pure Python CLI/API
    ui_ws = tmp_path / "ws_ui"
    ui_ws.mkdir(parents=True)
    (ui_ws / "index.html").write_text("<!doctype html><html><body><h1>Panel</h1></body></html>", encoding="utf-8")
    (ui_ws / "styles.css").write_text("body { margin: 0; }", encoding="utf-8")
    (ui_ws / "tests").mkdir()
    (ui_ws / "tests" / "test_ui.py").write_text("def test_ui(): assert True\n", encoding="utf-8")

    cli_ws = tmp_path / "ws_cli"
    (cli_ws / "src").mkdir(parents=True)
    (cli_ws / "tests").mkdir(parents=True)
    (cli_ws / "src" / "cli.py").write_text(
        "def run_cmd(arg: str) -> str:\n    return arg.strip()\n",
        encoding="utf-8",
    )
    (cli_ws / "requirements.txt").write_text("click==8.1.7\n", encoding="utf-8")
    (cli_ws / "tests" / "test_cli.py").write_text("def test_cli(): assert True\n", encoding="utf-8")

    area_scores_payload = {
        "dimensions": {
            "ux_ui": {"score": 40.0, "findings": ["No UI visual polish"], "improvements": ["Add styling"]},
            "functional_completeness": {"score": 90.0, "findings": [], "improvements": []},
            "robustness": {"score": 88.0, "findings": [], "improvements": []},
            "architecture_quality": {"score": 84.0, "findings": [], "improvements": []},
            "security_hygiene": {"score": 92.0, "findings": [], "improvements": []},
            "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
        }
    }

    seen_timeouts: list[float | None] = []

    class _SpyAuditLLM:
        def __init__(self, payloads: list[object]) -> None:
            self._payloads = list(payloads)

        def generate(self, messages, schema=None, *, timeout_seconds: float | None = None):
            seen_timeouts.append(timeout_seconds)
            nxt = self._payloads.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        def complete(self, messages, schema=None):
            return self.generate(messages, schema=schema)

    spy_llm = _SpyAuditLLM(
        [
            area_scores_payload,
            area_scores_payload,
            LLMError("Non-JSON response triggers deterministic fallback"),
        ]
    )
    auditor = ParentAuditor(llm=spy_llm)
    spec = Spec(objective="Herramienta de cálculo", users=["Operadores"], features=["Calcular"])

    ui_report = auditor.audit_workspace("ui-app", ui_ws, spec=spec)
    assert seen_timeouts[0] == 240.0
    assert ui_report.has_ui is True
    assert ui_report.dimensions["ux_ui"].weight == pytest.approx(0.25)
    assert ui_report.dimensions["functional_completeness"].weight == pytest.approx(0.25)
    assert ui_report.dimensions["robustness"].weight == pytest.approx(0.125)
    assert ui_report.global_score == pytest.approx(76.5, abs=0.05)
    assert ui_report.passed is False
    assert "ux_ui" in ui_report.failed_dimensions

    cli_report = auditor.audit_workspace("cli-app", cli_ws, spec=spec)
    assert seen_timeouts[1] == 240.0
    assert cli_report.has_ui is False
    assert cli_report.dimensions["ux_ui"].active is False
    assert cli_report.dimensions["ux_ui"].weight == 0.0
    redistributed = 0.125 + (0.25 / 3.0)
    assert cli_report.dimensions["robustness"].weight == pytest.approx(redistributed, abs=1e-4)
    assert cli_report.dimensions["architecture_quality"].weight == pytest.approx(redistributed, abs=1e-4)
    assert cli_report.dimensions["testing_runtime"].weight == pytest.approx(redistributed, abs=1e-4)
    assert cli_report.dimensions["security_hygiene"].weight == pytest.approx(0.125, abs=1e-4)
    expected_cli = round(
        0.25 * 90.0 + 0.125 * 92.0 + redistributed * (88.0 + 84.0 + 88.0),
        2,
    )
    assert cli_report.global_score == pytest.approx(expected_cli, abs=0.05)
    assert cli_report.passed is True

    # Deterministic fallback still produces valid 6-dimension scorecard on LLMError
    fb_report = auditor.audit_workspace("cli-app", cli_ws, spec=spec)
    assert fb_report.source == "deterministic_fallback"
    assert len(fb_report.dimensions) == 6
    assert 0.0 <= fb_report.global_score <= 100.0

    # 2. Dashboard renders Boleta de Calidad in audit cards, reader, and API
    store = SqliteEventStore(db_path=tmp_path / "scorecard_ui.db")
    chats_dir = tmp_path / "chats"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_ui_dash")
    pid = "scorecard-app"
    now = datetime(2026, 4, 24, 14, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Scorecard Web")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    sha_v1 = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    scorecard_dict = cli_report.to_scorecard_dict()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Scorecard App", "objective": "Obj", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(
            pid,
            EventType.PROJECT_DELIVERED,
            {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1", "quality_scorecard": scorecard_dict},
            at=now,
        ),
    ):
        store.append(ev)

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    orch.record_automated_quality_audit(
        pid,
        report=cli_report,
        commit_sha=sha_v1,
        version_label="v1",
        chats_dir=chats_dir,
    )
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0, workspace_manager=ws_mgr)
    try:
        chat_code, chat_data = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert chat_code == 200
        assert chat_data["messages"][-1]["quality_scorecard"]["global_score"] == scorecard_dict["global_score"]

        cp_code, cp_data = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert cp_code == 200
        assert cp_data["checkpoints"][0]["quality_scorecard"]["global_score"] == scorecard_dict["global_score"]

        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for expected_ui in (
            "quality-scorecard",
            "quality-bar-track",
            "renderQualityScorecardHtml",
            "Global:",
            "Aprobado",
            "Con observaciones",
            "UX/UI",
            "Funcionalidad",
            "Robustez",
            "Arquitectura",
            "Seguridad",
            "Pruebas",
        ):
            assert expected_ui in html
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_5_themes_selector_clean_sidebar_compact_delivered_and_audit_bar(
    tmp_path: Path,
) -> None:
    """Dashboard exposes 5-theme selector (#themes-menu-btn/#themes-popover), cleaner sidebar, compact DELIVERED card with #project-more-menu & clickable logo, unified audit bar, and hides audit filters for <2 entries."""
    store = SqliteEventStore(db_path=tmp_path / "themes_clean_ui.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_themes_clean")
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # 1. Dedicated 5-theme selector in header + CSS variables + af_theme compatibility with #theme-btn
        for theme_marker in (
            'id="themes-menu-btn"',
            'id="themes-popover"',
            'data-theme="dark"',
            'html[data-theme="light"]',
            'html[data-theme="emerald"]',
            'html[data-theme="sunset"]',
            'html[data-theme="lavender"]',
            'data-theme-choice="dark"',
            'data-theme-choice="light"',
            'data-theme-choice="emerald"',
            'data-theme-choice="sunset"',
            'data-theme-choice="lavender"',
            "Noche Profunda (Oscuro)",
            "Luz Editorial (Claro)",
            "Bosque Esmeralda",
            "Atardecer Terracota",
            "Grafito Lavanda",
            'id="theme-btn"',
            "af_theme",
        ):
            assert theme_marker in html

        # 2. Cleaner sidebar: compact secondary Import in panel-header and full-width primary + Nuevo proyecto above list
        sidebar_start = html.index('id="sidebar-panel"')
        sidebar_end = html.index('id="import-project-modal"', sidebar_start)
        sidebar_html = html[sidebar_start:sidebar_end]
        hdr_start = sidebar_html.index('<div class="panel-header">')
        hdr_end = sidebar_html.index('<div id="sidebar-projects-body">', hdr_start)
        sidebar_hdr = sidebar_html[hdr_start:hdr_end]
        assert 'id="toggle-import-project-btn"' in sidebar_hdr
        assert 'id="toggle-new-project-btn"' not in sidebar_hdr
        body_html = sidebar_html[hdr_end:]
        assert 'id="toggle-new-project-btn"' in body_html
        assert body_html.index('id="toggle-new-project-btn"') < body_html.index('id="project-list"')

        # 3. Clickable logo icon (no noisy standalone >Cambiar logo< button) and compact DELIVERED card with #project-more-menu
        assert ">Cambiar logo<" not in html
        assert 'title="Cambiar logo"' in html
        fn_start = html.index("function buildGuidanceBanner(")
        fn_end = html.index("function renderDetail()", fn_start)
        delivered_block = html[fn_start:fn_end].split("st.state === 'DELIVERED'", 1)[1]
        assert "delivered-compact" in delivered_block
        assert 'id="project-more-menu"' in delivered_block
        assert "Encender en local" in delivered_block
        assert "Abrir repositorio en GitHub" in delivered_block
        assert "Ver despliegue en vivo" in delivered_block
        assert "Eliminar proyecto" in delivered_block

        # Footer #detail-secondary no longer renders bottom "Eliminar proyecto" button
        render_start = html.index("function renderDetail()")
        render_end = html.index("function unquoteGitDiffPath(", render_start)
        render_body = html[render_start:render_end]
        sec_chunk = render_body.split("detail-secondary", 1)[1].split("lastSpecSignature", 1)[0]
        assert "Eliminar proyecto" not in sec_chunk

        # 4. Unified single audit toolbar & conditional filters hidden when reportEntries.length < 2
        audit_fn_start = html.index("function renderAuditChatTab(")
        audit_fn_end = html.index("async function loadProjectChatHistory(", audit_fn_start)
        audit_body = html[audit_fn_start:audit_fn_end]
        assert "audit-unified-bar" in audit_body
        assert 'data-chat-mode="audit"' in audit_body
        assert 'data-chat-mode="chat"' in audit_body
        assert 'data-chat-mode="document"' in audit_body
        assert "audit-more-actions-btn" in audit_body
        assert "reportEntries.length >= 2" in audit_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_quality_endpoints_and_history_versions_timeline(
    tmp_path: Path,
) -> None:
    """POST /quality/evaluate & /quality/improve endpoints validate state/INV-22, update version scorecards, and GET / renders Historial de versiones in Simple & Dev modes."""
    import subprocess

    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.builder import BuildWorker

    store = SqliteEventStore(db_path=tmp_path / "quality_timeline.db")
    chats_dir = tmp_path / "chats_qt"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_qt")
    pid = "quality-timeline-app"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<!doctype html><html><body><h1>Base v1</h1></body></html>\n", encoding="utf-8")
    (ws_dir / "tests").mkdir(exist_ok=True)
    (ws_dir / "tests" / "test_basic.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@local",
            "commit",
            "-m",
            "init v1",
        ],
        cwd=ws_dir,
        check=True,
        capture_output=True,
    )
    sha_v1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    def _audit_json(ux: float, other: float) -> dict[str, object]:
        return {
            "summary": "Auditoría de calidad",
            "dimensions": {
                "ux_ui": {"score": ux, "findings": [] if ux >= 70 else ["Ajustar contraste"], "improvements": [] if ux >= 70 else ["Mejorar UI"]},
                "functional_completeness": {"score": other, "findings": [], "improvements": []},
                "robustness": {"score": other, "findings": [], "improvements": []},
                "architecture_quality": {"score": other, "findings": [], "improvements": []},
                "security_hygiene": {"score": other, "findings": [], "improvements": []},
                "testing_runtime": {"score": other, "findings": [], "improvements": []},
            },
        }

    llm = FakeLLM(
        responses=[
            # 1. POST /quality/evaluate -> Estándar (80%)
            _audit_json(72.0, 82.0),
            # 2. POST /quality/improve initial audit -> Básico (70%)
            _audit_json(64.0, 72.0),
            # 3. POST /quality/improve after round 1 -> Producción (89%)
            _audit_json(90.0, 89.0),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Quality Timeline App")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Timeline QA", "objective": "Sitio web de recetas saludables", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(
            pid,
            EventType.PROJECT_DELIVERED,
            {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"},
            at=now,
        ),
    ):
        store.append(ev)

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            impl_steps=[
                {"index.html": '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><h1>Mejorado v2</h1></body></html>\n'}
            ]
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")]),
        quality_auditor=ParentAuditor(llm=llm),
        chats_dir=chats_dir,
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
    )
    try:
        # 1. HTML includes Historial de versiones (visible in Simple & Dev), tier badges, actions, and endpoints
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        for marker in (
            'id="history-versions-section"',
            "Historial de versiones",
            "⚪ Sin evaluar",
            "🥉 Básico",
            "🥈 Estándar",
            "🥇 Producción",
            "💎 Excelencia",
            "version-quality-bars",
            "↩︎ Volver a esta versión",
            "🛡️ Re-evaluar calidad",
            "✨ Mejorar hasta cumplir estándar",
            'id="evaluate-quality-btn"',
            'id="improve-quality-btn"',
            "/quality/evaluate",
            "/quality/improve",
            "runTrackedOperation",
            "askCustomConfirm",
        ):
            assert marker in html

        # Verify Historial de versiones section is visible in Simple AND Dev modes (only SHA/Copiar hash is tech-only)
        ver_fn_start = html.index("function buildHistoryVersionsSectionHtml(")
        ver_fn_end = html.index("function renderEventsTab(", ver_fn_start)
        ver_fn_body = html[ver_fn_start:ver_fn_end]
        assert 'class="history-versions-section"' in ver_fn_body
        assert 'history-versions-section tech-only' not in ver_fn_body
        assert 'version-sha-wrap tech-only' in ver_fn_body

        # 2. INV-22 / 404 guards on /quality/evaluate & /quality/improve
        assert _http_json(f"{base_url}/api/projects/..%2Fbad/quality/evaluate", method="POST")[0] == 400
        assert _http_json(f"{base_url}/api/projects/Bad%20ID!/quality/improve", method="POST")[0] == 400
        assert _http_json(f"{base_url}/api/projects/no-existe/quality/evaluate", method="POST")[0] == 404
        assert _http_json(f"{base_url}/api/projects/no-existe/quality/improve", method="POST")[0] == 404

        # Project not in DELIVERED state returns 409 Conflict
        pending_pid = "pending-qa-check"
        store.append(create_event(pending_pid, EventType.PROJECT_CREATED, {"name": "Pending", "objective": "Test", "state": "INTERVIEW"}, at=now))
        assert _http_json(f"{base_url}/api/projects/{pending_pid}/quality/evaluate", method="POST")[0] == 409
        assert _http_json(f"{base_url}/api/projects/{pending_pid}/quality/improve", method="POST")[0] == 409

        # 3. Unevaluated v1 exposes summary + quality_scorecard=None in GET /checkpoints
        c_cp0, r_cp0 = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert c_cp0 == 200
        assert r_cp0["checkpoints"][0]["quality_scorecard"] is None
        assert "recetas saludables" in r_cp0["checkpoints"][0]["summary"]

        # 4. POST /quality/evaluate updates v1 scorecard read-only without changing files
        html_before = (ws_dir / "index.html").read_text(encoding="utf-8")
        c_eval, r_eval = _http_json(f"{base_url}/api/projects/{pid}/quality/evaluate", method="POST", timeout=15.0)
        assert c_eval == 200
        assert r_eval["ok"] is True
        assert (ws_dir / "index.html").read_text(encoding="utf-8") == html_before
        assert r_eval["scorecard"]["tier_id"] == "standard"
        assert r_eval["scorecard"]["tier_label"] == "🥈 Estándar"
        assert r_eval["checkpoints"][0]["quality_scorecard"]["tier_id"] == "standard"

        c_chat, r_chat = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert c_chat == 200
        assert any(m.get("mode") == "audit" and m.get("version_label") == "v1" for m in r_chat["messages"])

        # 5. POST /quality/improve runs bounded refinement and delivers v2 Producción
        c_imp, r_imp = _http_json(f"{base_url}/api/projects/{pid}/quality/improve", method="POST", timeout=15.0)
        assert c_imp == 200
        assert r_imp["ok"] is True
        assert server.controller.wait_for_idle(timeout=15.0)

        c_cp1, r_cp1 = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert c_cp1 == 200
        assert len(r_cp1["checkpoints"]) == 2
        v2_cp = r_cp1["checkpoints"][0]
        assert v2_cp["version_label"] == "v2"
        assert v2_cp["is_current"] is True
        assert v2_cp["quality_scorecard"]["tier_id"] == "production"
        assert v2_cp["quality_scorecard"]["tier_label"] == "🥇 Producción"
        assert v2_cp["quality_scorecard"]["global_score"] >= 85.0
        assert "Mejorado v2" in (ws_dir / "index.html").read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_quality_improve_force_polish_endpoint_and_debate_history_ui(
    tmp_path: Path,
) -> None:
    """POST /quality/improve supports force_polish=true on already-passing projects, records debate_rounds/Markdown debate section, and GET / includes #polish-debate-btn + debate badge."""
    import subprocess

    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.builder import BuildWorker

    store = SqliteEventStore(db_path=tmp_path / "polish_debate.db")
    chats_dir = tmp_path / "chats_pd"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_pd")
    pid = "polish-debate-app"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<!doctype html><html><body><h1>Prod v1</h1></body></html>\n", encoding="utf-8")
    (ws_dir / "tests").mkdir(exist_ok=True)
    (ws_dir / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "init v1"],
        cwd=ws_dir,
        check=True,
        capture_output=True,
    )
    sha_v1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    llm = FakeLLM(
        responses=[
            # 1. force_polish initial Critic eval -> 88% Producción
            {
                "summary": "Cumple Producción (88%), pulible con debate.",
                "dimensions": {
                    "ux_ui": {"score": 86.0, "findings": ["Falta aria-label en index.html"], "improvements": ["Añadir aria-label"]},
                    "functional_completeness": {"score": 88.0, "findings": [], "improvements": []},
                    "robustness": {"score": 88.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 90.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 90.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
                },
                "objections": [
                    {
                        "dimension": "ux_ui",
                        "file_path": "index.html",
                        "severity": "medium",
                        "issue": "Falta aria-label en botones de index.html",
                        "concrete_fix": "Agregar aria-label accesible en index.html",
                    }
                ],
            },
            # 2. BuilderResponder response
            {
                "builder_response": "index.html: agregado aria-label y región main accesible.",
                "surgical_instructions": "Actualiza index.html agregando main y aria-label.",
            },
            # 3. Critic diff review -> 95% Excelencia
            {
                "summary": "Pulido verificado con diff (95%).",
                "resolved_count": 1,
                "diff_summary": "Añadido aria-label en index.html",
                "dimensions": {
                    "ux_ui": {"score": 95.0, "findings": [], "improvements": []},
                    "functional_completeness": {"score": 95.0, "findings": [], "improvements": []},
                    "robustness": {"score": 95.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 95.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 95.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 95.0, "findings": [], "improvements": []},
                },
            },
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    now = datetime(2026, 5, 6, 15, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Polish Debate App")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Polish QA", "objective": "Portal accesible", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(
            pid,
            EventType.PROJECT_DELIVERED,
            {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"},
            at=now,
        ),
    ):
        store.append(ev)

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            impl_steps=[
                {"index.html": '<!doctype html><html><body><main aria-label="Portal"><h1>Pulido v2</h1></main></body></html>\n'}
            ]
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        quality_auditor=ParentAuditor(llm=llm),
        chats_dir=chats_dir,
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for marker in (
            'id="polish-debate-btn"',
            "⚔️ Pulir con debate",
            "force_polish",
            "⚔️ Debate 2 agentes",
            "submitPolishWithDebate",
        ):
            assert marker in html

        # Non-boolean force_polish returns 400 (INV-22)
        assert (
            _http_json(
                f"{base_url}/api/projects/{pid}/quality/improve",
                method="POST",
                body={"force_polish": "yes"},
            )[0]
            == 400
        )

        # force_polish=true runs adversarial debate even when starting at 88% Producción
        c_imp, r_imp = _http_json(
            f"{base_url}/api/projects/{pid}/quality/improve",
            method="POST",
            body={"force_polish": True},
            timeout=15.0,
        )
        assert c_imp == 200
        assert r_imp["ok"] is True
        assert server.controller.wait_for_idle(timeout=15.0)

        c_cp, r_cp = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert c_cp == 200
        assert len(r_cp["checkpoints"]) == 2
        v2_cp = r_cp["checkpoints"][0]
        assert v2_cp["version_label"] == "v2"
        sc_v2 = v2_cp["quality_scorecard"]
        assert sc_v2["tier_id"] == "excellence"
        assert sc_v2["global_score"] == pytest.approx(95.0, abs=0.05)
        assert len(sc_v2["debate_rounds"]) == 1
        assert sc_v2["debate_summary"]["resolved_count"] == 1
        assert v2_cp["debate_summary"]["resolved_count"] == 1

        c_chat, r_chat = _http_json(f"{base_url}/api/projects/{pid}/chat")
        assert c_chat == 200
        latest_audit = [m for m in r_chat["messages"] if m.get("mode") == "audit"][-1]
        assert "⚔️ Debate de Calidad (Crítico vs. Constructor)" in latest_audit["content"]
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_polish_conserves_v1_note_busy_delivered_banner_and_completed_audit_hides_retry(
    tmp_path: Path,
) -> None:
    """When quality debate/improvement conserves v1 (new_version_created=False), checkpoints expose last_polish_note; buildHistoryVersionsSectionHtml renders last_polish_note and primary #polish-debate-btn; buildGuidanceBanner shows 'Refinando calidad con debate de 2 agentes...' when DELIVERED is busy; renderAuditChatTab only shows Reintentar on failed/timed_out/interrupted."""
    import subprocess

    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.builder import BuildWorker

    store = SqliteEventStore(db_path=tmp_path / "polish_v1_kept.db")
    chats_dir = tmp_path / "chats_v1_kept"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_v1_kept")
    pid = "conserve-v1-app"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text(
        "<!doctype html><html><body><h1>Estable v1</h1></body></html>\n",
        encoding="utf-8",
    )
    (ws_dir / "tests").mkdir(exist_ok=True)
    (ws_dir / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "init v1"],
        cwd=ws_dir,
        check=True,
        capture_output=True,
    )
    sha_v1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ws_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    llm = FakeLLM(
        responses=[
            # 1. force_polish initial Critic eval -> 89% Producción with 1 objection
            {
                "summary": "Cumple Producción (89%).",
                "dimensions": {
                    "ux_ui": {"score": 88.0, "findings": ["Ajustar botón"], "improvements": ["Mejorar botón"]},
                    "functional_completeness": {"score": 89.0, "findings": [], "improvements": []},
                    "robustness": {"score": 89.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 90.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 90.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
                },
                "objections": [
                    {
                        "dimension": "ux_ui",
                        "file_path": "index.html",
                        "severity": "low",
                        "issue": "Botón sin subtítulo",
                        "concrete_fix": "Agregar subtítulo en index.html",
                    }
                ],
            },
            # 2. BuilderResponder response
            {
                "builder_response": "index.html: intento de ajuste.",
                "surgical_instructions": "Intenta ajustar index.html.",
            },
            # 3. Critic diff review -> lower score (82%), triggers anti-regression discard so v1 is conserved!
            {
                "summary": "El cambio redujo claridad visual (82%).",
                "resolved_count": 0,
                "diff_summary": "Cambio descartado por regresión",
                "dimensions": {
                    "ux_ui": {"score": 78.0, "findings": ["Regresión"], "improvements": []},
                    "functional_completeness": {"score": 83.0, "findings": [], "improvements": []},
                    "robustness": {"score": 83.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 83.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 83.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 82.0, "findings": [], "improvements": []},
                },
            },
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    now = datetime(2026, 5, 8, 10, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Conserve V1 App")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Conserve V1", "objective": "App estable", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(
            pid,
            EventType.PROJECT_DELIVERED,
            {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"},
            at=now,
        ),
    ):
        store.append(ev)

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            impl_steps=[
                {"index.html": "<!doctype html><html><body><h1>Intento Regresivo</h1></body></html>\n"}
            ]
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        quality_auditor=ParentAuditor(llm=llm),
        chats_dir=chats_dir,
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
    )
    try:
        # 1. Inspect HTML functions for the 3 UI improvements
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # 1a. buildHistoryVersionsSectionHtml renders last_polish_note and primary #polish-debate-btn with secondary options in ▾ menu
        hist_start = html.index("function buildHistoryVersionsSectionHtml(")
        hist_end = html.index("function renderEventsTab(", hist_start)
        hist_body = html[hist_start:hist_end]
        assert "last_polish_note" in hist_body
        assert "version-polish-note" in hist_body
        assert 'id="polish-debate-btn" class="btn btn-primary btn-sm"' in hist_body
        assert 'id="quality-more-actions-menu"' in hist_body

        # 1b. buildGuidanceBanner displays "Refinando calidad con debate de 2 agentes..." when DELIVERED is busy
        banner_start = html.index("function buildGuidanceBanner(")
        banner_end = html.index("function splitProjectAttempts(", banner_start)
        banner_body = html[banner_start:banner_end]
        running_branch = banner_body.split("if (st.state === 'INTERVIEW')", 1)[0]
        assert "Refinando calidad con debate de 2 agentes..." in running_branch
        assert "st.state === 'DELIVERED'" in running_branch

        # 1c. renderAuditChatTab shows Reintentar ONLY for failed / timed_out / interrupted (not completed)
        audit_start = html.index("function renderAuditChatTab(")
        audit_end = html.index("async function loadProjectChatHistory(", audit_start)
        audit_body = html[audit_start:audit_end]
        assert "repId && repStatus !== 'running'" not in audit_body
        assert (
            "repId && (repStatus === 'failed' || repStatus === 'timed_out' || repStatus === 'interrupted')"
            in audit_body
        )

        # 2. Verify DELIVERED project marked busy returns "Refinando calidad con debate de 2 agentes..." in status
        server.controller.mark_running(pid)
        try:
            c_det_busy, r_det_busy = _http_json(f"{base_url}/api/projects/{pid}")
            assert c_det_busy == 200
            assert r_det_busy["status"]["is_running"] is True
            assert (
                r_det_busy["status"]["activity_summary"]
                == "Refinando calidad con debate de 2 agentes..."
            )
        finally:
            server.controller.mark_stopped(pid)

        # 3. Run POST /quality/improve with force_polish=True where candidate change is discarded -> v1 conserved + last_polish_note set
        c_imp, r_imp = _http_json(
            f"{base_url}/api/projects/{pid}/quality/improve",
            method="POST",
            body={"force_polish": True},
            timeout=15.0,
        )
        assert c_imp == 200
        assert r_imp["ok"] is True
        assert server.controller.wait_for_idle(timeout=15.0)

        c_cp, r_cp = _http_json(f"{base_url}/api/projects/{pid}/checkpoints")
        assert c_cp == 200
        assert len(r_cp["checkpoints"]) == 1
        cp_v1 = r_cp["checkpoints"][0]
        assert cp_v1["version_label"] == "v1"
        assert cp_v1["is_current"] is True
        assert cp_v1.get("last_polish_note") == (
            "Revisión completada: se conservó v1 sin cambios nuevos en código"
        )
        assert cp_v1["quality_scorecard"].get("last_polish_note") == (
            "Revisión completada: se conservó v1 sin cambios nuevos en código"
        )
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_auto_recovers_orphaned_delivered_ops_manual_recover_and_unifies_timers(
    tmp_path: Path,
) -> None:
    """Auto-recover orphaned DELIVERED background ops (>600s or foreign server_boot_id), support POST /recover without 409 from Reintentar / Recargar estado, allow re-running #polish-debate-btn, and unify banner/card timer origin."""
    import subprocess

    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.builder import BuildWorker

    def _mk_delivered_repo(pid_val: str, db_name: str, ws_sub: str, llm_obj: FakeLLM) -> tuple[SqliteEventStore, WorkspaceManager, Path, Orchestrator, str]:
        st = SqliteEventStore(db_path=tmp_path / db_name)
        wm = WorkspaceManager(base_dir=tmp_path / ws_sub)
        wdir = wm.create_workspace(pid_val)
        (wdir / "index.html").write_text(
            "<!doctype html><html><body><h1>Base v1</h1></body></html>\n",
            encoding="utf-8",
        )
        (wdir / "tests").mkdir(exist_ok=True)
        (wdir / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=wdir, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=wdir, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=wdir, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "init v1"],
            cwd=wdir,
            check=True,
            capture_output=True,
        )
        sha1 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wdir, check=True, capture_output=True, text=True
        ).stdout.strip()
        orch_obj = Orchestrator(store=st, llm=llm_obj, workspace_manager=wm)
        base_t = datetime.now(UTC) - timedelta(minutes=25)
        plan_d = _plan_payload("Orphan Recovery App")
        plan_h = Plan.model_validate(plan_d).compute_hash()
        for ev in (
            create_event(pid_val, EventType.PROJECT_CREATED, {"name": "Orphan Polish", "objective": "App estable", "state": "INTERVIEW"}, at=base_t),
            create_event(pid_val, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=base_t),
            create_event(pid_val, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=base_t),
            create_event(pid_val, EventType.PLAN_CREATED, {"plan": plan_d, "plan_hash": plan_h, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=base_t),
            create_event(pid_val, EventType.APPROVAL_RECORDED, {"plan_hash": plan_h, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=base_t),
            create_event(pid_val, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=base_t),
            create_event(
                pid_val,
                EventType.PROJECT_DELIVERED,
                {"repo_url": "", "deploy_url": "", "commit_sha": sha1, "version_label": "v1"},
                at=base_t,
            ),
        ):
            st.append(ev)
        return st, wm, wdir, orch_obj, sha1

    llm1 = FakeLLM(
        responses=[
            {
                "summary": "Cumple Producción (88%).",
                "dimensions": {
                    "ux_ui": {"score": 88.0, "findings": ["Ajustar header"], "improvements": ["Mejorar header"]},
                    "functional_completeness": {"score": 88.0, "findings": [], "improvements": []},
                    "robustness": {"score": 88.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 88.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 88.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
                },
                "objections": [
                    {
                        "dimension": "ux_ui",
                        "file_path": "index.html",
                        "severity": "low",
                        "issue": "Falta landmark main",
                        "concrete_fix": "Usar main en index.html",
                    }
                ],
            },
            {
                "builder_response": "index.html: agregado landmark main.",
                "surgical_instructions": "Envuelve h1 en main en index.html.",
            },
            {
                "summary": "Pulido a Excelencia (95%).",
                "resolved_count": 1,
                "diff_summary": "Agregado landmark main",
                "dimensions": {
                    "ux_ui": {"score": 95.0, "findings": [], "improvements": []},
                    "functional_completeness": {"score": 95.0, "findings": [], "improvements": []},
                    "robustness": {"score": 95.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 95.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 95.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 95.0, "findings": [], "improvements": []},
                },
            },
        ]
    )
    pid1 = "orphan-boot-polish"
    pid2 = "orphan-timeout-polish"
    store, ws_mgr, _ws_dir, orch, sha_v1 = _mk_delivered_repo(pid1, "orphan_recovery.db", "ws_orphan_rec", llm1)
    chats_dir = tmp_path / "chats_orphan_rec"
    now_utc = datetime.now(UTC)

    # Seed Case 1: unclosed op started 20s ago from another server_boot_id
    store.append(
        create_event(
            pid1,
            EventType.BACKGROUND_OP_STARTED,
            {
                "op_id": "bg-old-boot-1",
                "op_type": "quality_improve",
                "server_boot_id": "prior-boot-xyz",
                "started_at": (now_utc - timedelta(seconds=20)).isoformat(),
                "activity_summary": "Refinando calidad con debate de 2 agentes...",
                "stage": "VERIFYING",
                "agents_count": 2,
            },
            at=now_utc - timedelta(seconds=20),
        )
    )

    # Seed Case 2 in same store: unclosed op started 620s (> 10 min) ago even with CURRENT server_boot_id
    ws_dir2 = ws_mgr.create_workspace(pid2)
    (ws_dir2 / "index.html").write_text("<h1>Timeout test</h1>\n", encoding="utf-8")
    plan_d2 = _plan_payload("Timeout App")
    plan_h2 = Plan.model_validate(plan_d2).compute_hash()
    for ev in (
        create_event(pid2, EventType.PROJECT_CREATED, {"name": "Timeout App", "objective": "Obj", "state": "INTERVIEW"}, at=now_utc - timedelta(minutes=30)),
        create_event(pid2, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now_utc - timedelta(minutes=30)),
        create_event(pid2, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now_utc - timedelta(minutes=30)),
        create_event(pid2, EventType.PLAN_CREATED, {"plan": plan_d2, "plan_hash": plan_h2, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now_utc - timedelta(minutes=30)),
        create_event(pid2, EventType.APPROVAL_RECORDED, {"plan_hash": plan_h2, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now_utc - timedelta(minutes=30)),
        create_event(pid2, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now_utc - timedelta(minutes=30)),
        create_event(pid2, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"}, at=now_utc - timedelta(minutes=30)),
        create_event(
            pid2,
            EventType.BACKGROUND_OP_STARTED,
            {
                "op_id": "bg-expired-620",
                "op_type": "quality_improve",
                "server_boot_id": orch._server_boot_id,
                "started_at": (now_utc - timedelta(seconds=620)).isoformat(),
                "activity_summary": "Refinando calidad con debate de 2 agentes...",
                "stage": "VERIFYING",
                "agents_count": 2,
            },
            at=now_utc - timedelta(seconds=620),
        ),
    ):
        store.append(ev)

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            impl_steps=[
                {"index.html": "<!doctype html><html><body><main><h1>Pulido v2</h1></main></body></html>\n"}
            ]
        ),
        runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        quality_auditor=ParentAuditor(llm=llm1),
        chats_dir=chats_dir,
    )
    resolver = ModelResolver(runner=FakeRunner(), auto_probe=False)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        workspace_manager=ws_mgr,
        model_resolver=resolver,
        chats_dir=chats_dir,
    )
    try:
        # 1. Inspect UI HTML for timer unification and /recover wiring on Reintentar / Recargar estado
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        assert "function resolveOperationStartMs(" in html
        assert "function resolveOperationElapsedSeconds(" in html

        live_fn_start = html.index("function renderLiveActivityBanner(")
        live_fn_end = html.index("async function runTrackedOperation(", live_fn_start)
        live_fn_body = html[live_fn_start:live_fn_end]
        assert "resolveOperationStartMs(" in live_fn_body
        assert "st.last_event_at || st.step_started_at" not in live_fn_body

        guide_fn_start = html.index("function buildGuidanceBanner(")
        guide_fn_end = html.index("if (st.state === 'INTERVIEW')", guide_fn_start)
        guide_running_body = html[guide_fn_start:guide_fn_end]
        assert "resolveOperationElapsedSeconds(" in guide_running_body
        assert "step_started_at" in guide_running_body

        reload_fn_start = html.index("const reloadBtn = document.getElementById('live-activity-reload-btn')")
        reload_fn_end = html.index("function updateLiveTimers(", reload_fn_start)
        reload_body = html[reload_fn_start:reload_fn_end]
        assert "/recover" in reload_body

        # 2. Querying GET /api/projects/<pid1> auto-recovers stale server_boot_id op -> is_running=False & persists BACKGROUND_OP_FINISHED
        c1, d1 = _http_json(f"{base_url}/api/projects/{pid1}")
        assert c1 == 200
        assert d1["status"]["state"] == "DELIVERED"
        assert d1["is_running"] is False
        assert d1["status"]["is_running"] is False
        assert d1["status"]["busy"] is False
        assert d1["step_started_at"] is None
        assert any(
            ev["type"] == EventType.BACKGROUND_OP_FINISHED
            and ev["payload"].get("op_id") == "bg-old-boot-1"
            and ev["payload"].get("status") == "interrupted"
            for ev in d1["events"]
        )

        # 3. Querying GET /api/projects list (_serialize_summary) auto-recovers >600s expired op for pid2
        c_list, r_list = _http_json(f"{base_url}/api/projects")
        assert c_list == 200
        by_pid = {p["project_id"]: p for p in r_list["projects"]}
        assert by_pid[pid2]["state"] == "DELIVERED"
        assert by_pid[pid2]["is_running"] is False
        assert by_pid[pid2]["busy"] is False
        assert any(
            ev.type == EventType.BACKGROUND_OP_FINISHED
            and ev.payload.get("op_id") == "bg-expired-620"
            and ev.payload.get("status") == "interrupted"
            for ev in store.get_events(pid2)
        )

        # 4. Can immediately trigger #polish-debate-btn (POST /quality/improve with force_polish=True) without 409 after auto-recovery
        c_imp, r_imp = _http_json(
            f"{base_url}/api/projects/{pid1}/quality/improve",
            method="POST",
            body={"force_polish": True},
            timeout=15.0,
        )
        assert c_imp == 200
        assert r_imp["ok"] is True
        assert server.controller.wait_for_idle(timeout=15.0)
        _, cps_res = _http_json(f"{base_url}/api/projects/{pid1}/checkpoints")
        assert cps_res["checkpoints"][0]["version_label"] == "v2"

        # 5. Live running DELIVERED project unifies op_started_at & step_started_at; manual unlock via POST /api/projects/<pid2>/recover clears everything without 409
        chats_dir.mkdir(parents=True, exist_ok=True)
        (chats_dir / f"{pid2}.json").write_text(
            json.dumps(
                [
                    {
                        "id": "a-hung-audit",
                        "role": "assistant",
                        "mode": "audit",
                        "prompt": "Pulir",
                        "content": "",
                        "status": "running",
                        "stage_label": "2/3 Analizando código con IA",
                        "server_boot_id": orch._server_boot_id,
                        "created_at": now_utc.isoformat(),
                    }
                ]
            ),
            encoding="utf-8",
        )
        resolver.record_activity(
            "Refinando calidad con debate de 2 agentes...",
            agents_count=2,
            stage="VERIFYING",
        )
        server.controller.mark_running(pid2)
        store.append(
            create_event(
                pid2,
                EventType.BACKGROUND_OP_STARTED,
                {
                    "op_id": "bg-manual-stuck",
                    "op_type": "quality_improve",
                    "server_boot_id": orch._server_boot_id,
                    "started_at": now_utc.isoformat(),
                },
                at=now_utc,
            )
        )
        _, live_busy = _http_json(f"{base_url}/api/projects/{pid2}")
        assert live_busy["is_running"] is True
        assert live_busy["op_started_at"] == live_busy["status"]["op_started_at"]
        assert live_busy["op_started_at"] is not None
        assert live_busy["step_started_at"] is not None

        # INV-22 & 404 guards on /recover
        assert _http_json(f"{base_url}/api/projects/..%2Fbad/recover", method="POST")[0] == 400
        assert _http_json(f"{base_url}/api/projects/no-existe-rec/recover", method="POST")[0] == 404
        # Clean /retry on DELIVERED still enforces 409 when not using /recover
        assert _http_json(f"{base_url}/api/projects/{pid1}/retry", method="POST")[0] == 409

        c_rec, r_rec = _http_json(
            f"{base_url}/api/projects/{pid2}/recover",
            method="POST",
            body={},
        )
        assert c_rec == 200
        assert r_rec["ok"] is True
        assert r_rec["status"]["state"] == "DELIVERED"
        assert r_rec["status"]["is_running"] is False
        assert r_rec["status"]["busy"] is False
        assert r_rec["status"]["op_started_at"] is None
        assert r_rec["status"]["step_started_at"] is None
        assert server.controller.is_running(pid2) is False
        assert resolver.get_active_activity()["activity_summary"] is None
        assert any(
            ev.type == EventType.BACKGROUND_OP_FINISHED
            and ev.payload.get("op_id") == "bg-manual-stuck"
            and ev.payload.get("status") == "interrupted"
            for ev in store.get_events(pid2)
        )
        c_chat2, r_chat2 = _http_json(f"{base_url}/api/projects/{pid2}/chat")
        assert c_chat2 == 200
        assert r_chat2["messages"][-1]["status"] == "interrupted"
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_dashboard_expires_stale_in_memory_active_ops_over_600s(tmp_path: Path) -> None:
    """Expire stale in-memory active_ops entries (>600s) without requiring a restart."""
    from agent_factory.dashboard import DashboardController

    store = SqliteEventStore(str(tmp_path / "stale_mem_op.db"))
    orch = Orchestrator(store=store, llm=FakeLLM(responses=[]))
    pid = "stale-mem-op-proj"
    base_t = datetime.now(UTC) - timedelta(minutes=20)
    plan_d = _plan_payload("Stale Memory App")
    plan_h = Plan.model_validate(plan_d).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Stale Mem", "objective": "Test stale mem", "state": "INTERVIEW"}, at=base_t),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=base_t),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=base_t),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_d, "plan_hash": plan_h, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=base_t),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_h, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=base_t),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=base_t),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": "abc123", "version_label": "v1"}, at=base_t),
    ):
        store.append(ev)

    ctrl = DashboardController(orchestrator=orch)
    ctrl.mark_running(pid, op_type="quality_improve", activity_summary="Pulido activo")
    assert ctrl.is_running(pid) is True

    # Simulate an active in-memory operation stuck longer than 600s
    stale_dt = datetime.now(UTC) - timedelta(seconds=615)
    ctrl._active_ops[pid]["started_at"] = stale_dt.isoformat()
    ctrl._active_ops[pid]["started_at_dt"] = stale_dt

    detail = ctrl.get_project_detail(pid)
    assert detail["is_running"] is False
    assert detail["op_started_at"] is None
    assert detail["status"]["busy"] is False
    assert ctrl.is_running(pid) is False


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_runtime_start_timeout_180s_and_swallows_client_disconnects(
    tmp_path: Path,
) -> None:
    """JS client configures 180000ms timeout for /start and /runtime/start, backend aliases /runtime/start with INV-22 checks, and _send_json/_send_html swallow Windows ConnectionAbortedError cleanly."""
    import io
    from types import SimpleNamespace

    from agent_factory.dashboard import DashboardController, _DashboardRequestHandler
    from agent_factory.runtime_runner import LocalAppRunner

    store = SqliteEventStore(str(tmp_path / "rt_timeout_dash.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_rt_timeout")
    pid = "proj-rt-180"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<h1>Runtime 180s</h1>\n", encoding="utf-8")

    now = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    pdict = _plan_payload("Runtime 180 App")
    phash = Plan.model_validate(pdict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "RT 180", "objective": "App runtime", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": pdict, "plan_hash": phash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": phash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": ""}, at=now),
    ):
        store.append(ev)

    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    local_runner = LocalAppRunner(workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        local_runner=local_runner,
    )
    try:
        # 1. JS resolveFetchTimeoutMs & local-start configure 180000 ms for /start and /runtime/start
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html = html_resp.read().decode("utf-8")

        fn_start = html.index("function resolveFetchTimeoutMs(")
        fn_end = html.index("async function apiFetch(", fn_start)
        fn_body = html[fn_start:fn_end]
        assert "180000" in fn_body
        assert "runtime" in fn_body
        assert "start" in fn_body
        assert "timeoutMs: 180000" in html

        # 2. /runtime/start route alias enforces INV-22 and starts local app cleanly
        assert _http_json(f"{base_url}/api/projects/..%2Fbad/runtime/start", method="POST")[0] == 400
        code_rt, res_rt = _http_json(f"{base_url}/api/projects/{pid}/runtime/start", method="POST")
        assert code_rt == 200
        assert res_rt["local_app"]["running"] is True
        code_stp, res_stp = _http_json(f"{base_url}/api/projects/{pid}/runtime/stop", method="POST")
        assert code_stp == 200
        assert res_stp["local_app"]["running"] is False

        # 3. _send_json and _send_html safely swallow client disconnect socket errors (WinError 10053)
        ctrl = DashboardController(orchestrator=orch, secrets_to_redact=("TOP_SECRET_10053",))
        for exc_cls in (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            class _AbortedWriter:
                def __init__(self, err_type: type[OSError]) -> None:
                    self._err_type = err_type

                def write(self, _data: bytes) -> int:
                    raise self._err_type(10053, "WinError 10053 aborted")

            handler = object.__new__(_DashboardRequestHandler)
            handler.server = SimpleNamespace(controller=ctrl)  # type: ignore[assignment]
            handler.wfile = _AbortedWriter(exc_cls)  # type: ignore[assignment]
            handler._headers_buffer = []
            handler.close_connection = False
            handler.send_response = lambda *_a, **_kw: None  # type: ignore[method-assign]
            handler.send_header = lambda *_a, **_kw: None  # type: ignore[method-assign]
            handler.end_headers = lambda *_a, **_kw: None  # type: ignore[method-assign]
            handler.rfile = io.BytesIO(b"")  # type: ignore[assignment]

            # Must not raise!
            handler._send_json(200, {"ok": True, "secret": "TOP_SECRET_10053"})
            handler._send_html(200, "<html><body>ok</body></html>")
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
def test_dashboard_questions_chat_clean_layout_collapse_relative_paths_and_noise_filtering(
    tmp_path: Path,
) -> None:
    """Questions tab uses single-scroll flex #audit-chat-panel (max-height 68vh, 16px padding, 14px gap, sticky composer, 78% user right / 90% assistant left with 1.6 line-height), collapses prior auditor replies >420 chars or >8 lines while keeping latest open, converts file:///.../workspaces/<slug>/<path> to clean relative paths, filters audit/empty entries, and places subtle footer actions only on non-empty auditor replies."""
    store = SqliteEventStore(str(tmp_path / "chat_ux_redesign.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_chat_ux")
    pid = "chat-ux-demo"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "src" / "yt_music_dl").mkdir(parents=True)
    (ws_dir / "src" / "yt_music_dl" / "app.py").write_text("def main(): return True\n", encoding="utf-8")

    now = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    pdict = _plan_payload("Chat UX Redesign App")
    phash = Plan.model_validate(pdict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Chat UX App", "objective": "Chat limpio", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": pdict, "plan_hash": phash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": phash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    long_md_reply = (
        "El punto de entrada está en "
        "[app.py](file:///c:/Users/Angel%20laptopJaz/Documents/projects/agent-factory-liwi/workspaces/chat-ux-demo/src/yt_music_dl/app.py#L1-L10) "
        "y también consulta file:///C:/Users/Angel%20laptopJaz/workspaces/chat-ux-demo/README.md.\n"
        + "\n".join(f"- Línea de detalle arquitectónico número {i} con explicación clara." for i in range(1, 11))
    )
    llm = FakeLLM(
        responses=[
            {
                "reply": long_md_reply,
                "suggested_changes": "Extraer configuración de rutas.",
                "document_title": "consulta-arquitectura.md",
            }
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        chats_dir=tmp_path / "chats_ux",
    )
    try:
        # Round-trip a chat question returning file:/// workspace links and >420 chars / >8 lines
        c_chat, r_chat = _http_json(
            f"{base_url}/api/projects/{pid}/chat",
            method="POST",
            body={"message": "¿Dónde vive el punto de entrada?", "mode": "chat"},
        )
        assert c_chat == 200
        assert "src/yt_music_dl/app.py" in r_chat["reply"]

        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # 1. Clean single-scroll flex layout & role bubbles CSS
        panel_css_start = html.index("#audit-chat-panel {")
        panel_css_end = html.index("}", panel_css_start)
        panel_css = html[panel_css_start:panel_css_end]
        assert "display: flex;" in panel_css
        assert "flex-direction: column;" in panel_css
        assert "max-height: 68vh;" in panel_css
        assert "padding: 16px;" in panel_css

        assert "gap: 14px;" in html
        assert "max-width: 78%;" in html
        assert "max-width: 90%;" in html
        assert "line-height: 1.6;" in html
        assert 'id="audit-chat-panel"' in html
        assert "audit-chat-composer" in html

        # 2. Auto-collapse (>420 chars or >8 lines) keeping latest assistant reply open
        assert "function isLongAuditorReply(" in html
        long_fn_start = html.index("function isLongAuditorReply(")
        long_fn_end = html.index("function isQuestionsTabEntry(", long_fn_start)
        long_fn_body = html[long_fn_start:long_fn_end]
        assert "420" in long_fn_body
        assert "> 8" in long_fn_body

        assert "Ver respuesta completa" in html
        assert "Colapsar" in html
        assert "lastAssistantChatIdx" in html
        assert "data-chat-toggle" in html
        assert "chat-msg-fade" in html

        # 3. Clean workspace relative paths without broken file:/// URLs in renderSafeMarkdown
        assert "function extractCleanProjectPathFromFileUrl(" in html
        path_fn_start = html.index("function extractCleanProjectPathFromFileUrl(")
        path_fn_end = html.index("function formatSafeInlineMd(", path_fn_start)
        path_fn_body = html[path_fn_start:path_fn_end]
        assert "/workspaces/" in path_fn_body
        assert "decodeURI" in path_fn_body

        md_start = html.index("function renderSafeMarkdown(")
        md_end = html.index("function inferAuditVersionBadge(", md_start)
        md_body = html[md_start:md_end]
        assert "@@AF_FPATH_" in md_body
        assert "md-file-ref" in md_body
        assert 'href="file://' not in md_body

        # 4. Noise filtering & discreet footer actions in Preguntas
        q_fn_start = html.index("function isQuestionsTabEntry(")
        q_fn_end = html.index("function renderAuditChatTab(", q_fn_start)
        q_fn_body = html[q_fn_start:q_fn_end]
        assert "kind === 'audit'" in q_fn_body
        assert "Audita el proyecto completo" in q_fn_body or "audita el proyecto completo" in q_fn_body
        assert "if (!text) return false" in q_fn_body

        audit_fn_start = html.index("function renderAuditChatTab(")
        audit_fn_end = html.index("async function loadProjectChatHistory(", audit_fn_start)
        audit_fn_body = html[audit_fn_start:audit_fn_end]
        assert "m.role === 'user' && (m.mode === 'audit' || m.mode === 'document')" not in audit_fn_body
        assert "isQuestionsTabEntry(" in audit_fn_body
        assert "chat-msg-footer" in audit_fn_body
        assert "!isUser && hasRealContent" in audit_fn_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_liwi_chat_minimalist_redesign_and_contract_compatibility(
    tmp_path: Path,
) -> None:
    """Liwi chat modal uses minimalist full-height 800px centered layout, clean header, borderless stream, and capsule composer while preserving all IDs/endpoints."""
    import re

    store = SqliteEventStore(str(tmp_path / "liwi_minimal_ui.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # 1. All existing HTML IDs, data-* hooks, and API endpoints remain intact
        required_ids = [
            "improve-factory-btn",
            "factory-build-banner",
            "factory-build-banner-title",
            "factory-build-banner-detail",
            "factory-build-banner-timer",
            "factory-restart-banner",
            "factory-improve-modal",
            "factory-improve-title",
            "factory-memory-toggle",
            "factory-memory-panel",
            "close-factory-improve-btn",
            "factory-progress-card",
            "factory-progress-title",
            "factory-progress-timer",
            "factory-progress-detail",
            "factory-chat-stream",
            "factory-paste-previews",
            "factory-chat-form",
            "factory-chat-input",
            "factory-image-input",
            "factory-attach-btn",
            "factory-inline-status",
            "factory-chat-send-btn",
        ]
        for dom_id in required_ids:
            assert f'id="{dom_id}"' in html, f"Missing required DOM id: {dom_id}"

        required_endpoints = [
            "/api/factory/assistant/history",
            "/api/factory/assistant/status",
            "/api/factory/assistant/message",
            "/api/factory/assistant/apply",
            "/api/factory/assistant/memory/",
        ]
        for ep in required_endpoints:
            assert ep in html, f"Missing required endpoint: {ep}"

        assert "data-apply-factory-proposal" in html
        assert "data-open-factory-improve" in html
        assert "addEventListener('paste'" in html

        # 2. Expanded near-fullscreen container + centered 800px column + clean top-right close button
        assert "#factory-improve-modal .factory-chat-modal" in html
        modal_css_start = html.index("#factory-improve-modal .factory-chat-modal")
        modal_css_end = html.index("</style>", modal_css_start)
        liwi_css = html[modal_css_start:modal_css_end]

        assert "max-width: 800px;" in liwi_css
        assert "width: 100%;" in liwi_css
        assert "margin: 0 auto;" in liwi_css
        assert "94dvh" in liwi_css or "94vh" in liwi_css or "96vh" in liwi_css
        assert "overflow-x: hidden;" in liwi_css

        # 3. Clean header: ONLY title "Liwi", collapsible "🧠 Memoria de Liwi" pill, and top-right Close/Back button (no filler/subtitles)
        modal_html_start = html.index('<div id="factory-improve-modal"')
        modal_html_end = html.index('<section class="panel" id="detail-panel">', modal_html_start)
        modal_html = html[modal_html_start:modal_html_end]

        head_match = re.search(
            r'<div class="factory-chat-head">(.*?)</div>\s*<div class="factory-chat-meta-col"',
            modal_html,
            re.DOTALL,
        )
        assert head_match is not None, "Expected clean .factory-chat-head block"
        head_html = head_match.group(1)

        title_match = re.search(
            r'<h3 id="factory-improve-title"[^>]*>(.*?)</h3>',
            head_html,
            re.DOTALL,
        )
        assert title_match is not None
        assert title_match.group(1).strip() == "Liwi"
        assert 'id="factory-memory-toggle"' in head_html
        assert "🧠 Memoria de Liwi" in head_html
        assert 'id="close-factory-improve-btn"' in head_html
        assert "Cerrar" in head_html or "Volver" in head_html
        assert "<p" not in head_html
        assert "<small" not in head_html
        assert "Mejorar Fábrica (Liwi)" not in head_html

        # 4. Borderless history stream, right-aligned subtle user bubble, clean Liwi replies with comfortable typography & light proposal cards
        stream_css_match = re.search(r"\.factory-chat-stream\s*\{([^}]*)\}", liwi_css)
        assert stream_css_match is not None
        stream_css = stream_css_match.group(1)
        assert "border: none;" in stream_css
        assert "background: transparent;" in stream_css
        assert "max-width: 800px;" in stream_css

        user_msg_match = re.search(r"\.factory-msg-user\s*\{([^}]*)\}", liwi_css)
        assert user_msg_match is not None
        user_msg_css = user_msg_match.group(1)
        assert "align-self: flex-end;" in user_msg_css
        assert "border-radius:" in user_msg_css

        assistant_msg_match = re.search(r"\.factory-msg-assistant\s*\{([^}]*)\}", liwi_css)
        assert assistant_msg_match is not None
        assistant_msg_css = assistant_msg_match.group(1)
        assert "background: transparent;" in assistant_msg_css
        assert "border: none;" in assistant_msg_css
        assert "line-height: 1.65;" in assistant_msg_css
        assert "font-size: 15.5px;" in assistant_msg_css or "font-size: 15px;" in assistant_msg_css or "font-size: 16px;" in assistant_msg_css

        assert ".factory-proposal-card" in liwi_css
        assert "✨ Aplicar mejora a Liwi" in html

        # 5. Single rounded capsule input bar: previews on top (hidden when empty), 📎 left, borderless auto-resizing textarea center, circular ↑ right, Enter/Shift+Enter handling
        form_match = re.search(
            r'<form id="factory-chat-form"[^>]*>(.*?)</form>',
            modal_html,
            re.DOTALL,
        )
        assert form_match is not None
        form_html = form_match.group(1)

        previews_pos = form_html.find('id="factory-paste-previews"')
        attach_pos = form_html.find('id="factory-attach-btn"')
        input_pos = form_html.find('id="factory-chat-input"')
        send_pos = form_html.find('id="factory-chat-send-btn"')
        assert previews_pos >= 0, "Expected #factory-paste-previews integrated at top of capsule form"
        assert attach_pos > previews_pos
        assert input_pos > attach_pos
        assert send_pos > input_pos

        assert 'id="factory-paste-previews" class="factory-paste-previews" style="display:none"' in form_html
        attach_btn_match = re.search(
            r'<button id="factory-attach-btn"[^>]*>(.*?)</button>',
            form_html,
            re.DOTALL,
        )
        assert attach_btn_match is not None
        assert attach_btn_match.group(1).strip() == "📎"

        send_btn_match = re.search(
            r'<button id="factory-chat-send-btn"[^>]*>(.*?)</button>',
            form_html,
            re.DOTALL,
        )
        assert send_btn_match is not None
        assert send_btn_match.group(1).strip() == "↑"

        assert 'rows="1"' in form_html
        assert ".factory-composer-capsule" in liwi_css
        assert "border-radius: 26px;" in liwi_css or "border-radius: 24px;" in liwi_css
        assert ".factory-composer-textarea" in liwi_css
        assert ".factory-send-circle-btn" in liwi_css
        assert "border-radius: 9999px;" in liwi_css or "border-radius: 50%;" in liwi_css

        init_js_start = html.index("(function initFactoryImproverModal()")
        init_js_end = html.index("setInterval(updateLiveTimers", init_js_start)
        init_js = html[init_js_start:init_js_end]

        assert "autoResizeFactoryInput" in init_js
        assert "inputEl.scrollHeight" in init_js
        assert "ev.key === 'Enter'" in init_js
        assert "!ev.shiftKey" in init_js
        assert "!ev.isComposing" in init_js
        assert "previewsEl.style.display = pendingImages.length ? 'flex' : 'none'" in init_js
        assert "sendBtn.textContent = '↑'" in init_js
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_dashboard_liwi_fullscreen_overlay_optimistic_chat_markdown_newlines_and_button_spinners() -> None:
    """DASHBOARD_HTML includes fullscreen update overlay (#factory-fullscreen-build-overlay), optimistic Liwi chat bubble, literal newline normalization in renderSafeMarkdown, and live spinners/clocks on slow actions."""
    from agent_factory.dashboard import DASHBOARD_HTML

    assert 'id="factory-fullscreen-build-overlay"' in DASHBOARD_HTML
    assert 'id="factory-fs-minimize-btn"' in DASHBOARD_HTML
    assert "backdrop-filter: blur(12px);" in DASHBOARD_HTML
    assert "¡Liwi actualizado! Recargando..." in DASHBOARD_HTML
    assert "window.location.reload()" in DASHBOARD_HTML
    assert "1/3 Editando código con IA" in DASHBOARD_HTML
    assert "2/3 Verificando pruebas" in DASHBOARD_HTML
    assert "3/3 Guardando y reiniciando" in DASHBOARD_HTML

    assert 'id="factory-thinking-bubble"' in DASHBOARD_HTML or "factory-thinking-bubble" in DASHBOARD_HTML
    assert "Liwi está pensando…" in DASHBOARD_HTML
    assert "consultInFlight = true" in DASHBOARD_HTML
    assert "allFactoryCodeBlocks" in DASHBOARD_HTML
    assert "setButtonLoading(" in DASHBOARD_HTML
    assert ".btn-spinner {" in DASHBOARD_HTML
    assert ".live-op-clock" in DASHBOARD_HTML
    assert ".card-live-timer" in DASHBOARD_HTML
    assert 'id="audit-toolbar-status"' in DASHBOARD_HTML

    md_start = DASHBOARD_HTML.index("function renderSafeMarkdown(mdText)")
    md_end = DASHBOARD_HTML.index("const codeBlocks = [];", md_start)
    md_head = DASHBOARD_HTML[md_start:md_end]
    assert "replace(" in md_head
    assert "split(" in md_head


def _extract_css_block(css: str, header_prefix: str) -> str:
    """Return the brace-balanced inner body of the first CSS block matching header_prefix."""
    start = css.find(header_prefix)
    if start == -1:
        return ""
    open_brace = css.find("{", start)
    if open_brace == -1:
        return ""
    depth = 0
    for idx in range(open_brace, len(css)):
        ch = css[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace + 1 : idx]
    return ""


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_mobile_responsive_step1_header_and_project_selector(tmp_path: Path) -> None:
    """Step 1 mobile CSS lives inside @media (max-width: 768px) for anti-overflow, scrollable header buttons, and single-line project selector + Nuevo without altering desktop or CDNs."""
    store = SqliteEventStore(str(tmp_path / "mobile_step1.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # 1. Zero external CDNs and all existing header + project-selector IDs preserved
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        required_ids = [
            "server-health-dot",
            "server-health-label",
            "status-note",
            "view-mode-segmented",
            "view-mode-simple",
            "view-mode-dev",
            "tech-mode-toggle",
            "themes-menu-btn",
            "themes-popover",
            "improve-factory-btn",
            "settings-menu-btn",
            "settings-popover",
            "sidebar-panel",
            "mobile-project-switcher",
            "mobile-new-project-shortcut",
            "toggle-import-project-btn",
            "toggle-new-project-btn",
        ]
        for dom_id in required_ids:
            assert f'id="{dom_id}"' in html, f"Missing existing HTML id: {dom_id}"

        # 2. Extract <style> and the @media (max-width: 768px) block
        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768, "Expected @media (max-width: 768px) block in dashboard CSS"

        # 3. Anti-overflow base rules inside @media (max-width: 768px)
        assert "overflow-x: hidden;" in media_768
        assert "max-width: 100%;" in media_768
        assert "box-sizing: border-box;" in media_768

        # 4. Top header scrollable controls bar and non-wrapping 38px buttons
        controls_rule = _extract_css_block(media_768, "header .controls")
        assert controls_rule, "Expected header .controls rule inside @media (max-width: 768px)"
        assert "display: flex;" in controls_rule
        assert "flex-wrap: nowrap;" in controls_rule
        assert "overflow-x: auto;" in controls_rule
        assert "-webkit-overflow-scrolling: touch;" in controls_rule
        assert "max-width: 100%;" in controls_rule

        for header_btn_sel in (
            "#view-mode-simple",
            "#view-mode-dev",
            "#themes-menu-btn",
            "#improve-factory-btn",
            "#settings-menu-btn",
        ):
            assert header_btn_sel in media_768
        header_btns_rule = _extract_css_block(media_768, "header .controls .btn")
        assert header_btns_rule, "Expected header button rules inside @media (max-width: 768px)"
        assert "white-space: nowrap;" in header_btns_rule
        assert "flex-shrink: 0;" in header_btns_rule
        assert "min-height: 38px;" in header_btns_rule

        # 5. Project selector row + selector control + '+ Nuevo' button
        switcher_row_rule = _extract_css_block(media_768, ".mobile-switcher-row {")
        assert switcher_row_rule, "Expected .mobile-switcher-row rule inside @media (max-width: 768px)"
        assert "display: flex;" in switcher_row_rule
        assert "flex-wrap: nowrap;" in switcher_row_rule
        assert "align-items: center;" in switcher_row_rule
        assert "width: 100%;" in switcher_row_rule
        assert "min-width: 0;" in switcher_row_rule

        selector_rule = _extract_css_block(media_768, ".mobile-switcher-row .mobile-switcher-btn")
        assert selector_rule, "Expected project selector rule inside @media (max-width: 768px)"
        assert ".mobile-switcher-row #mobile-project-switcher" in media_768
        assert ".mobile-switcher-row select" in media_768
        assert "flex: 1;" in selector_rule
        assert "min-width: 0;" in selector_rule
        assert "min-height: 38px;" in selector_rule

        new_btn_rule = _extract_css_block(media_768, "#mobile-new-project-shortcut")
        assert new_btn_rule, "Expected #mobile-new-project-shortcut rule inside @media (max-width: 768px)"
        assert "white-space: nowrap;" in new_btn_rule
        assert "flex-shrink: 0;" in new_btn_rule
        assert "min-height: 38px;" in new_btn_rule

        # 6. Desktop rules outside @media remain untouched
        assert ".controls { display: flex; align-items: center; gap: 10px; }" in css
        desktop_switcher = _extract_css_block(css, ".mobile-switcher-row {")
        assert "display: none;" in desktop_switcher
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_dashboard_mobile_responsive_header_and_project_selector() -> None:
    """Verify @media (max-width: 768px) enforces body anti-overflow, scrollable top-bar buttons, and flex project selector + Nuevo row without CDNs or ID changes."""
    from agent_factory.dashboard import DASHBOARD_HTML

    # Zero external CDNs and expected Spanish labels + HTML IDs preserved
    assert "cdn." not in DASHBOARD_HTML.lower()
    assert "https://" not in DASHBOARD_HTML.lower()
    for expected_markup in (
        'id="view-mode-simple"',
        ">Simple</button>",
        'id="view-mode-dev"',
        ">Desarrollador</button>",
        'id="themes-menu-btn"',
        "Temas</button>",
        'id="improve-factory-btn"',
        "Mejorar Fábrica</button>",
        'id="settings-menu-btn"',
        "Ajustes</button>",
        'id="mobile-project-switcher"',
        'id="mobile-new-project-shortcut"',
        ">+ Nuevo</button>",
    ):
        assert expected_markup in DASHBOARD_HTML, f"Missing expected dashboard markup: {expected_markup}"

    style_start = DASHBOARD_HTML.index("<style>") + len("<style>")
    style_end = DASHBOARD_HTML.index("</style>", style_start)
    css = DASHBOARD_HTML[style_start:style_end]

    media_768 = _extract_css_block(css, "@media (max-width: 768px)")
    assert media_768, "Missing @media (max-width: 768px) block"

    # Body anti-overflow inside @media (max-width: 768px)
    body_rule = _extract_css_block(media_768, "html,\n      body") or _extract_css_block(media_768, "body {")
    assert body_rule, "Expected html, body or body rule inside @media (max-width: 768px)"
    assert "overflow-x: hidden;" in body_rule

    # Top bar scrollable container and non-wrapping accessible buttons
    controls_rule = _extract_css_block(media_768, "header .controls {")
    assert controls_rule, "Expected header .controls rule inside @media (max-width: 768px)"
    assert "overflow-x: auto;" in controls_rule
    assert "max-width: 100%;" in controls_rule

    header_btns_rule = _extract_css_block(media_768, "header .controls .btn")
    assert header_btns_rule, "Expected header button rule inside @media (max-width: 768px)"
    for btn_id in (
        "#view-mode-simple",
        "#view-mode-dev",
        "#themes-menu-btn",
        "#improve-factory-btn",
        "#settings-menu-btn",
    ):
        assert btn_id in media_768
    assert "white-space: nowrap;" in header_btns_rule
    assert "min-height: 38px;" in header_btns_rule
    assert "display: inline-flex;" in header_btns_rule
    assert "align-items: center;" in header_btns_rule

    # Project selector + '+ Nuevo' row rules
    switcher_row_rule = _extract_css_block(media_768, ".mobile-switcher-row {")
    assert switcher_row_rule, "Expected .mobile-switcher-row rule inside @media (max-width: 768px)"
    assert "display: flex;" in switcher_row_rule
    assert "align-items: center;" in switcher_row_rule
    assert "max-width: 100%;" in switcher_row_rule

    selector_rule = _extract_css_block(media_768, ".mobile-switcher-row .mobile-switcher-btn")
    assert selector_rule, "Expected project selector rule inside @media (max-width: 768px)"
    assert ".mobile-switcher-row select" in media_768
    assert "flex: 1;" in selector_rule
    assert "min-width: 0;" in selector_rule
    assert "max-width: 100%;" in selector_rule

    new_btn_rule = _extract_css_block(media_768, "#mobile-new-project-shortcut {")
    assert new_btn_rule, "Expected #mobile-new-project-shortcut rule inside @media (max-width: 768px)"
    assert "white-space: nowrap;" in new_btn_rule
    assert "flex-shrink: 0;" in new_btn_rule
    assert "min-height: 38px;" in new_btn_rule

    # Desktop CSS outside @media (max-width: 768px) untouched
    assert ".controls { display: flex; align-items: center; gap: 10px; }" in css
    desktop_switcher = _extract_css_block(css, ".mobile-switcher-row {")
    assert "display: none;" in desktop_switcher


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_factory_chat_includes_copy_button_and_clipboard_fallback(
    tmp_path: Path,
) -> None:
    """Dashboard HTML includes discreet .copy-msg-btn styles (desktop + mobile), Copiar/Copiado labels, and clipboard + execCommand fallback for #factory-chat-stream."""
    store = SqliteEventStore(str(tmp_path / "factory_chat_copy.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # Zero external CDNs and existing Liwi chat container intact
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        assert 'id="factory-chat-stream"' in html

        # 1. CSS rules for .copy-msg-btn on desktop and touch-friendly @media (max-width: 768px)
        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        assert ".copy-msg-btn" in css
        desktop_copy_rule = _extract_css_block(css, ".copy-msg-btn {")
        assert desktop_copy_rule, "Expected desktop .copy-msg-btn CSS rule"
        assert "cursor: pointer;" in desktop_copy_rule

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768, "Expected @media (max-width: 768px) block"
        assert ".copy-msg-btn" in media_768
        assert "touch-action: manipulation;" in media_768
        assert "min-height: 36px;" in media_768 or "min-height: 34px;" in media_768 or "min-height: 32px;" in media_768

        # 2. JS helpers inside factory chat modal: labels, clipboard API + execCommand fallback, and clean extraction
        init_js_start = html.index("(function initFactoryImproverModal()")
        init_js_end = html.index("setInterval(updateLiveTimers", init_js_start)
        init_js = html[init_js_start:init_js_end]

        assert "copy-msg-btn" in init_js
        assert "Copiar" in init_js
        assert "Copiado" in init_js
        assert "navigator.clipboard.writeText" in init_js
        assert "execCommand('copy')" in init_js or 'execCommand("copy")' in init_js
        assert "function copyTextToClipboard(" in init_js
        assert "function createCopyButton(" in init_js
        assert "1500" in init_js
        assert "setSelectionRange" in init_js
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_mobile_responsive_topbar_and_project_card_refinements(
    tmp_path: Path,
) -> None:
    """Step 1 mobile CSS inside @media (max-width: 768px) compacts top-bar buttons, shrinks project title, aligns edit-title with updated date, and prevents overflow on card actions/tabs/audit split without altering desktop."""
    store = SqliteEventStore(str(tmp_path / "mobile_step1_cards.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # 1. Zero external CDNs and all relevant existing HTML IDs preserved
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for dom_id in (
            "view-mode-simple",
            "view-mode-dev",
            "themes-menu-btn",
            "improve-factory-btn",
            "settings-menu-btn",
            "detail-title",
            "edit-title-btn",
            "detail-time",
            "tab-audit-btn",
            "tab-modify-btn",
            "tab-spec-btn",
            "tab-events-btn",
            "project-more-btn",
            "audit-quick-audit-btn",
            "audit-more-actions-btn",
        ):
            assert f'id="{dom_id}"' in html, f"Missing existing HTML id: {dom_id}"

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        media_768_marker = "@media (max-width: 768px)"
        media_768 = _extract_css_block(css, media_768_marker)
        assert media_768, "Expected @media (max-width: 768px) block in dashboard CSS"

        # 2. Compact padding and font-size for top-bar buttons in narrow viewports (360px-412px)
        header_btns_rule = _extract_css_block(media_768, "header .controls .btn")
        assert header_btns_rule, "Expected header button rules inside @media (max-width: 768px)"
        assert "padding: 5px 9px;" in header_btns_rule
        assert "font-size: 0.78rem;" in header_btns_rule
        assert "white-space: nowrap;" in header_btns_rule
        assert "flex-shrink: 0;" in header_btns_rule

        # 3. Project card main title reduced to 1.25rem with line-height: 1.25
        title_rule = _extract_css_block(media_768, "#detail-title")
        assert title_rule, "Expected #detail-title / .detail-title rule inside @media (max-width: 768px)"
        assert ".detail-title" in media_768
        assert "font-size: 1.25rem;" in title_rule
        assert "line-height: 1.25;" in title_rule

        # 4. Single-row flex alignment eliminating empty line between edit-title button and updated date
        header_group_rule = _extract_css_block(media_768, ".detail-header-group {")
        assert header_group_rule, "Expected .detail-header-group rule inside @media (max-width: 768px)"
        assert "display: flex;" in header_group_rule
        assert "align-items: center;" in header_group_rule
        assert "justify-content: space-between;" in header_group_rule

        edit_btn_rule = _extract_css_block(media_768, "#edit-title-btn {")
        assert edit_btn_rule, "Expected #edit-title-btn rule inside @media (max-width: 768px)"
        assert "order:" in edit_btn_rule

        detail_time_rule = _extract_css_block(media_768, "#detail-time {")
        assert detail_time_rule, "Expected #detail-time rule inside @media (max-width: 768px)"
        assert "order:" in detail_time_rule
        assert "margin:" in detail_time_rule

        # 5. Card action buttons (Encender en local, Más), tabs, and Auditar código + ▾ split group:
        #    white-space: nowrap, uniform height, and zero overflow containment
        card_actions_wrap_rule = _extract_css_block(media_768, ".delivered-compact-actions {")
        assert card_actions_wrap_rule, "Expected .delivered-compact-actions rule inside @media (max-width: 768px)"
        assert "flex-wrap: wrap;" in card_actions_wrap_rule

        card_action_btns_rule = _extract_css_block(media_768, ".delivered-compact-actions .btn")
        assert card_action_btns_rule, "Expected card action button rules inside @media (max-width: 768px)"
        assert ".local-controls-row .btn" in media_768
        assert "#project-more-btn" in media_768
        assert "white-space: nowrap;" in card_action_btns_rule
        assert "min-height: 38px;" in card_action_btns_rule
        assert "max-width: 100%;" in card_action_btns_rule

        tabs_rule = _extract_css_block(media_768, ".tabs {")
        assert tabs_rule, "Expected .tabs rule inside @media (max-width: 768px)"
        assert "max-width: 100%;" in tabs_rule
        assert "min-width: 0;" in tabs_rule
        assert "overflow-x: auto;" in tabs_rule
        assert "white-space: nowrap;" in tabs_rule

        tab_btn_rule = _extract_css_block(media_768, ".tab-btn {")
        assert tab_btn_rule, "Expected .tab-btn rule inside @media (max-width: 768px)"
        assert "white-space: nowrap;" in tab_btn_rule
        assert "min-height: 38px;" in tab_btn_rule

        audit_bar_rule = _extract_css_block(media_768, ".audit-unified-bar {")
        assert audit_bar_rule, "Expected .audit-unified-bar rule inside @media (max-width: 768px)"
        assert "max-width: 100%;" in audit_bar_rule
        assert "min-width: 0;" in audit_bar_rule

        audit_split_rule = _extract_css_block(media_768, ".audit-primary-split {")
        assert audit_split_rule, "Expected .audit-primary-split rule inside @media (max-width: 768px)"
        assert "max-width: 100%;" in audit_split_rule
        assert "min-width: 0;" in audit_split_rule

        audit_btns_rule = _extract_css_block(media_768, ".audit-primary-split .btn")
        assert audit_btns_rule, "Expected .audit-primary-split button rules inside @media (max-width: 768px)"
        assert "#audit-quick-audit-btn" in media_768
        assert "#audit-more-actions-btn" in media_768
        assert "white-space: nowrap;" in audit_btns_rule
        assert "min-height: 38px;" in audit_btns_rule
        assert "max-width: 100%;" in audit_btns_rule

        # 6. Desktop styles outside @media (max-width: 768px) remain untouched
        desktop_css = css.replace(media_768, "")
        assert ".detail-header-group" not in desktop_css
        desktop_title_rule = _extract_css_block(desktop_css, ".detail-title {")
        assert "font-size: 1.35rem;" in desktop_title_rule
        assert "line-height: 1.3;" in desktop_title_rule
        assert ".controls { display: flex; align-items: center; gap: 10px; }" in desktop_css
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_mobile_responsive_overflow_containment_and_header(
    tmp_path: Path,
) -> None:
    """Step 1 mobile audit: global anti-overflow containment, long-path/code wrapping, top-bar scroll, and single-line project selector live strictly inside @media (max-width: 768px)."""
    store = SqliteEventStore(str(tmp_path / "mobile_overflow_step1.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # 1. Zero external CDNs and all key HTML IDs intact
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for dom_id in (
            "view-mode-simple",
            "view-mode-dev",
            "themes-menu-btn",
            "improve-factory-btn",
            "settings-menu-btn",
            "sidebar-panel",
            "detail-panel",
            "mobile-project-switcher",
            "mobile-new-project-shortcut",
        ):
            assert f'id="{dom_id}"' in html, f"Missing existing HTML id: {dom_id}"

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768, "Expected @media (max-width: 768px) block in dashboard CSS"

        # 2. Global box-sizing and 100vw / overflow-x: hidden containment
        universal_box_rule = _extract_css_block(media_768, "*,\n      *::before,\n      *::after")
        assert universal_box_rule, "Expected *, *::before, *::after rule inside @media (max-width: 768px)"
        assert "box-sizing: border-box;" in universal_box_rule

        html_body_rule = _extract_css_block(media_768, "html,\n      body")
        assert html_body_rule, "Expected html, body rule inside @media (max-width: 768px)"
        assert "max-width: 100vw;" in html_body_rule
        assert "overflow-x: hidden;" in html_body_rule

        # 3. Main containers, cards, panels, and flex/grid rows enforce min-width: 0 and max-width: 100%
        containment_rule = _extract_css_block(media_768, "main.layout")
        assert containment_rule, "Expected containers/cards/panels/flex/grid containment rule inside @media (max-width: 768px)"
        assert "min-width: 0;" in containment_rule
        assert "max-width: 100%;" in containment_rule
        for container_sel in (
            ".panel",
            ".project-card",
            ".phase-card",
            ".audit-card",
            ".version-card",
            ".doctor-recovery-card",
            ".live-activity-banner",
            ".live-activity-main",
            ".quality-bars-grid",
            ".factory-steps-bar",
            ".versions-row",
            ".audit-toolbar",
            ".modal-card",
        ):
            assert container_sel in media_768, f"Expected {container_sel} contained inside @media (max-width: 768px)"

        # 4. Long text, Windows paths, logs, diagnostics, errors, <code>, and <pre> wrapping + scroll
        wrap_rule = _extract_css_block(media_768, "code,\n      .md-body code")
        assert wrap_rule, "Expected code/logs/diagnostics wrapping rule inside @media (max-width: 768px)"
        assert "white-space: pre-wrap;" in wrap_rule
        assert "overflow-wrap: anywhere;" in wrap_rule
        assert "word-break: break-word;" in wrap_rule
        assert "max-width: 100%;" in wrap_rule
        for diag_sel in (
            "details.tech-details pre",
            ".doctor-prompt-pre",
            ".diff-pre",
            ".audit-card-diag",
            ".inline-error-box",
            ".file-path",
            ".md-file-ref",
        ):
            assert diag_sel in media_768, f"Expected {diag_sel} in mobile wrap rules"

        pre_scroll_rule = _extract_css_block(media_768, "pre,\n      details.tech-details pre")
        assert pre_scroll_rule, "Expected <pre> wrapping and overflow-x: auto rule inside @media (max-width: 768px)"
        assert "white-space: pre-wrap;" in pre_scroll_rule
        assert "overflow-wrap: anywhere;" in pre_scroll_rule
        assert "word-break: break-word;" in pre_scroll_rule
        assert "max-width: 100%;" in pre_scroll_rule
        assert "overflow-x: auto;" in pre_scroll_rule

        # 5. Top bar scrollable button strip and touch-friendly button sizing
        controls_rule = _extract_css_block(media_768, "header .controls {")
        assert controls_rule, "Expected header .controls rule inside @media (max-width: 768px)"
        assert "display: flex;" in controls_rule
        assert "align-items: center;" in controls_rule
        assert "gap: 6px;" in controls_rule
        assert "overflow-x: auto;" in controls_rule
        assert "-webkit-overflow-scrolling: touch;" in controls_rule
        assert "max-width: 100%;" in controls_rule

        header_touch_rule = _extract_css_block(media_768, "header .controls > button")
        assert header_touch_rule, "Expected top-bar button base touch rule inside @media (max-width: 768px)"
        assert "min-height: 38px;" in header_touch_rule
        assert "white-space: nowrap;" in header_touch_rule
        assert "flex-shrink: 0;" in header_touch_rule
        assert "font-size: 0.84rem;" in header_touch_rule
        assert "padding: 6px 10px;" in header_touch_rule

        # 6. Project selector + '+ Nuevo' single-line row
        switcher_row_rule = _extract_css_block(media_768, ".mobile-switcher-row {")
        assert switcher_row_rule, "Expected .mobile-switcher-row rule inside @media (max-width: 768px)"
        assert "display: flex;" in switcher_row_rule
        assert "align-items: center;" in switcher_row_rule
        assert "gap: 8px;" in switcher_row_rule
        assert "max-width: 100%;" in switcher_row_rule

        selector_rule = _extract_css_block(media_768, ".mobile-switcher-row .mobile-switcher-btn")
        assert selector_rule, "Expected project selector rule inside @media (max-width: 768px)"
        assert ".mobile-switcher-row select" in media_768
        assert "flex: 1;" in selector_rule
        assert "min-width: 0;" in selector_rule
        assert "text-overflow: ellipsis;" in selector_rule

        new_btn_rule = _extract_css_block(media_768, "#mobile-new-project-shortcut {")
        assert new_btn_rule, "Expected #mobile-new-project-shortcut rule inside @media (max-width: 768px)"
        assert "white-space: nowrap;" in new_btn_rule
        assert "flex-shrink: 0;" in new_btn_rule
        assert "min-height: 38px;" in new_btn_rule

        # 7. Desktop CSS outside @media (max-width: 768px) remains completely untouched
        desktop_css = css.replace(media_768, "")
        desktop_body = _extract_css_block(desktop_css, "body {")
        assert "max-width: 100vw;" not in desktop_body
        assert ".controls { display: flex; align-items: center; gap: 10px; }" in desktop_css
        desktop_switcher = _extract_css_block(desktop_css, ".mobile-switcher-row {")
        assert "display: none;" in desktop_switcher
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_mobile_responsive_step2a_cards_doctor_tabs_overflow_and_antizoom(
    tmp_path: Path,
) -> None:
    """Step 2A mobile audit: project cards, Doctor alert/pills containment, action buttons/tabs/audit split, and 16px anti-zoom inputs strictly inside @media (max-width: 768px)."""
    store = SqliteEventStore(str(tmp_path / "mobile_step2a.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # e) Zero external CDNs and existing HTML IDs/labels preserved
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for dom_id in (
            "detail-title",
            "edit-title-btn",
            "detail-time",
            "detail-badge",
            "tab-audit-btn",
            "tab-modify-btn",
            "tab-spec-btn",
            "tab-events-btn",
            "audit-quick-audit-btn",
            "audit-more-actions-btn",
            "new-project-input",
            "edit-title-input",
        ):
            assert f'id="{dom_id}"' in html, f"Missing existing HTML id: {dom_id}"

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768, "Expected @media (max-width: 768px) block in dashboard CSS"

        # a) Project cards: 1.2rem title with line-height 1.25 + overflow-wrap: anywhere,
        #    flex row for edit button & Actualizado date with gap 8px and no empty break,
        #    and badge/status rows wrapping with gap 6px.
        step2a_title_rule = _extract_css_block(media_768, "#detail-panel #detail-title")
        assert step2a_title_rule, "Expected Step 2A project title rule inside @media (max-width: 768px)"
        assert ".detail-header-group .detail-title" in media_768
        assert ".project-card .card-title" in media_768
        assert "font-size: 1.2rem;" in step2a_title_rule
        assert "line-height: 1.25;" in step2a_title_rule
        assert "overflow-wrap: anywhere;" in step2a_title_rule

        header_group_rule = _extract_css_block(media_768, ".detail-header-group {")
        assert header_group_rule, "Expected .detail-header-group rule inside @media (max-width: 768px)"
        assert "display: flex;" in header_group_rule
        assert "align-items: center;" in header_group_rule
        assert "justify-content: space-between;" in header_group_rule
        assert "gap: 8px;" in header_group_rule

        edit_btn_rule = _extract_css_block(media_768, "#edit-title-btn {")
        assert edit_btn_rule
        assert "margin: 0;" in edit_btn_rule

        detail_time_rule = _extract_css_block(media_768, "#detail-time {")
        assert detail_time_rule
        assert "margin: 0 0 0 auto !important;" in detail_time_rule

        badge_rows_rule = _extract_css_block(media_768, ".project-card .card-header")
        assert badge_rows_rule, "Expected badge/status rows rule inside @media (max-width: 768px)"
        for badge_row_sel in (
            ".detail-header-status",
            ".model-usage-row",
            ".plan-meta",
            ".round-meta",
            ".timeline-title-group",
        ):
            assert badge_row_sel in media_768
        assert "flex-wrap: wrap;" in badge_rows_rule
        assert "gap: 6px;" in badge_rows_rule

        # b) 'Se requiere atención' alert and 'Agente Doctor / Diagnóstico' pills + text containment
        doctor_badges_rule = _extract_css_block(media_768, ".doctor-recovery-badges {")
        assert doctor_badges_rule, "Expected .doctor-recovery-badges rule inside @media (max-width: 768px)"
        assert "flex-wrap: wrap;" in doctor_badges_rule
        assert "gap: 6px;" in doctor_badges_rule
        assert "max-width: 100%;" in doctor_badges_rule
        assert "min-width: 0;" in doctor_badges_rule

        doctor_pills_rule = _extract_css_block(media_768, ".doctor-recovery-badges .model-pill")
        assert doctor_pills_rule, "Expected Doctor pills wrap rule inside @media (max-width: 768px)"
        assert ".doctor-recovery-badges .test-badge" in media_768
        assert "max-width: 100%;" in doctor_pills_rule
        assert "overflow-wrap: anywhere;" in doctor_pills_rule

        doctor_diag_rule = _extract_css_block(media_768, ".guidance-banner.banner-err,")
        assert doctor_diag_rule, "Expected alert & Doctor diagnostic containment rule inside @media (max-width: 768px)"
        for diag_sel in (
            ".guidance-banner.banner-err .banner-desc",
            ".doctor-recovery-card",
            ".doctor-recovery-card .round-summary",
            ".doctor-prompt-box",
        ):
            assert diag_sel in media_768
        assert "max-width: 100%;" in doctor_diag_rule
        assert "min-width: 0;" in doctor_diag_rule
        assert "overflow-wrap: anywhere;" in doctor_diag_rule
        assert "word-break: break-word;" in doctor_diag_rule

        # c) Action buttons, tabs, audit subtabs, and 'Auditar código' + '▾' split group
        banner_actions_rule = _extract_css_block(media_768, ".banner-actions {")
        assert banner_actions_rule, "Expected .banner-actions wrap rule inside @media (max-width: 768px)"
        assert "flex-wrap: wrap;" in banner_actions_rule

        card_action_btns_rule = _extract_css_block(media_768, ".delivered-compact-actions .btn")
        assert card_action_btns_rule
        assert ".banner-actions .btn" in media_768
        assert ".doctor-prompt-box .btn" in media_768
        assert "min-height: 38px;" in card_action_btns_rule
        assert "white-space: nowrap;" in card_action_btns_rule

        tabs_rule = _extract_css_block(media_768, ".tabs {")
        assert "overflow-x: auto;" in tabs_rule
        assert "max-width: 100%;" in tabs_rule

        tab_btn_rule = _extract_css_block(media_768, ".tab-btn {")
        assert "min-height: 38px;" in tab_btn_rule
        assert "white-space: nowrap;" in tab_btn_rule

        audit_subtabs_rule = _extract_css_block(media_768, ".audit-subtabs {")
        assert "overflow-x: auto;" in audit_subtabs_rule
        assert "max-width: 100%;" in audit_subtabs_rule

        audit_subtab_rule = _extract_css_block(media_768, ".audit-subtab {")
        assert "min-height: 38px;" in audit_subtab_rule
        assert "white-space: nowrap;" in audit_subtab_rule

        audit_split_rule = _extract_css_block(media_768, ".audit-primary-split {")
        assert "flex-wrap: wrap;" in audit_split_rule
        assert "max-width: 100%;" in audit_split_rule
        assert "min-width: 0;" in audit_split_rule

        audit_btns_rule = _extract_css_block(media_768, ".audit-primary-split .btn")
        assert "min-height: 38px;" in audit_btns_rule
        assert "white-space: nowrap;" in audit_btns_rule

        # d) Anti-zoom 16px for <input>, <textarea>, <select> strictly inside @media (max-width: 768px)
        antizoom_rule = _extract_css_block(media_768, "input,\n      textarea,\n      select {")
        assert antizoom_rule, "Expected input, textarea, select anti-zoom rule inside @media (max-width: 768px)"
        assert "font-size: 16px;" in antizoom_rule

        antizoom_fields_rule = _extract_css_block(media_768, "input.input-field,")
        assert antizoom_fields_rule, "Expected .input-field/.input-inline anti-zoom override inside @media (max-width: 768px)"
        assert "textarea.input-field" in media_768
        assert "select.input-field" in media_768
        assert "input.input-inline" in media_768
        assert "font-size: 16px;" in antizoom_fields_rule

        # e) Desktop (> 768px) CSS remains completely untouched
        desktop_css = css.replace(media_768, "")
        assert "font-size: 16px;" not in desktop_css
        desktop_title = _extract_css_block(desktop_css, ".detail-title {")
        assert "font-size: 1.35rem;" in desktop_title
        assert "line-height: 1.3;" in desktop_title
        desktop_input = _extract_css_block(desktop_css, ".input-field {")
        assert "font-size: 0.92rem;" in desktop_input
        desktop_doctor_badges = _extract_css_block(desktop_css, ".doctor-recovery-badges {")
        assert "gap: 8px;" in desktop_doctor_badges
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_self_improve_staging_lazy_init_executes_zero_git_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instantiating DashboardController/DashboardHTTPServer and querying staging/promote lazily executes zero git commands."""
    import subprocess

    git_calls: list[list[str]] = []
    real_run = subprocess.run

    def _forbid_git(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).lower().endswith("git"):
            git_calls.append([str(c) for c in cmd])
            raise AssertionError(f"Unexpected git execution during lazy dashboard init: {cmd}")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _forbid_git)

    store = SqliteEventStore(str(tmp_path / "staging_lazy.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    ctrl = DashboardController(orchestrator=orch)
    assert ctrl._staging_manager is None

    http_srv = DashboardHTTPServer(("127.0.0.1", 0), ctrl)
    http_srv.server_close()
    assert ctrl._staging_manager is None
    assert git_calls == []

    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        assert server.controller._staging_manager is None

        status_code, staging_payload = _http_json(f"{base_url}/api/self-improve/staging")
        assert status_code == 200
        assert staging_payload["pending_count"] == 0
        assert staging_payload["commits"] == []
        assert staging_payload["is_building"] is False
        assert staging_payload["queue_size"] == 0
        assert staging_payload["last_error"] is None
        assert staging_payload["current_task"] is None
        assert staging_payload["initialized"] is False
        assert server.controller._staging_manager is None

        promote_code, promote_payload = _http_json(
            f"{base_url}/api/self-improve/promote",
            method="POST",
            body={},
        )
        assert promote_code == 200
        assert promote_payload["ok"] is True
        assert promote_payload["promoted"] is False
        assert promote_payload["merged_commits"] == 0
        assert promote_payload["commits"] == []
        assert promote_payload["restart_scheduled"] is False
        assert server.controller._staging_manager is None
        assert git_calls == []
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_self_improve_apply_enqueues_in_staging_and_delegates_to_controller_self_improver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/factory/assistant/apply enqueues background staging work without restarting main and resolves controller.self_improver dynamically."""
    from agent_factory.staging_manager import StagingManager

    class _FakeStagingManager:
        def __init__(self) -> None:
            self.enqueued: list[dict[str, Any]] = []
            self.shutdown_called = False

        def enqueue_improvement(
            self,
            title: str,
            prompt: str,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.enqueued.append(
                {
                    "title": title,
                    "prompt": prompt,
                    "metadata": dict(metadata) if metadata is not None else {},
                }
            )
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": True,
                "queue_size": len(self.enqueued),
                "last_error": None,
                "current_task": title,
            }

        def get_staging_status(self) -> dict[str, Any]:
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": bool(self.enqueued),
                "queue_size": len(self.enqueued),
                "last_error": None,
                "current_task": self.enqueued[-1]["title"] if self.enqueued else None,
            }

        def shutdown(self, timeout: float = 5.0) -> None:
            del timeout
            self.shutdown_called = True

    store = SqliteEventStore(str(tmp_path / "staging_apply.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    fake_staging = _FakeStagingManager()
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=fake_staging,  # type: ignore[arg-type]
        secrets_to_redact=("SECRET_STAGE_APPLY_99",),
    )
    try:
        code, payload = _http_json(
            f"{base_url}/api/factory/assistant/apply",
            method="POST",
            body={
                "proposal_title": "Mejora staging SECRET_STAGE_APPLY_99",
                "proposal_prompt": "Implementa mejora en segundo plano",
                "message_id": "fmsg-101",
            },
        )
        assert code == 200
        assert payload["ok"] is True
        assert payload["queued"] is True
        assert payload["applied"] is False
        assert payload["rolled_back"] is False
        assert payload["restart_scheduled"] is False
        assert "SECRET_STAGE_APPLY_99" not in json.dumps(payload)
        assert payload["staging"]["queue_size"] == 1
        assert len(fake_staging.enqueued) == 1
        assert fake_staging.enqueued[0]["prompt"] == "Implementa mejora en segundo plano"
        assert fake_staging.enqueued[0]["metadata"]["message_id"] == "fmsg-101"
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)

    assert fake_staging.shutdown_called is True

    # Part B: Dynamic resolution of controller.self_improver at worker execution time (no git / no agy.EXE)
    class _SpySelfImprover:
        def __init__(self, *, should_fail: bool = False) -> None:
            self.should_fail = should_fail
            self.calls: list[dict[str, Any]] = []
            self.repo_root = tmp_path / "fake_repo"

        def apply_proposal(
            self,
            proposal_prompt: str,
            *,
            proposal_title: str = "",
            message_id: str | None = None,
            github_token: str = "",
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "proposal_prompt": proposal_prompt,
                    "proposal_title": proposal_title,
                    "message_id": message_id,
                    "github_token": github_token,
                }
            )
            if self.should_fail:
                return {"ok": False, "error": "fallo controlado en staging"}
            return {"ok": True, "applied": True, "restart_scheduled": False}

    worktree_dir = (tmp_path / "fake_repo" / ".liwi-staging").resolve()
    worktree_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(StagingManager, "ensure_worktree", lambda self: worktree_dir)
    monkeypatch.setattr(StagingManager, "_list_pending_commits", lambda self: [])
    monkeypatch.setattr(StagingManager, "_cleanup_worktree", lambda self: None)

    ctrl = DashboardController(orchestrator=orch)
    stale_double = _SpySelfImprover()
    active_double = _SpySelfImprover()
    failing_double = _SpySelfImprover(should_fail=True)
    ctrl.self_improver = stale_double  # type: ignore[assignment]
    # Trigger lazy manager creation first, then swap self_improver to prove dynamic resolution
    mgr = ctrl._get_staging_manager()
    ctrl.self_improver = active_double  # type: ignore[assignment]
    try:
        res_ok = ctrl.apply_factory_assistant_proposal(
            "Mejora dinámica 1",
            proposal_title="Dinámica 1",
            message_id="fmsg-202",
        )
        assert res_ok["ok"] is True
        assert res_ok["queued"] is True
        assert res_ok["restart_scheduled"] is False
        assert mgr.wait_until_idle(timeout=5.0) is True
        assert stale_double.calls == []
        assert active_double.calls == [
            {
                "proposal_prompt": "Mejora dinámica 1",
                "proposal_title": "Dinámica 1",
                "message_id": "fmsg-202",
                "github_token": "",
            }
        ]
        assert mgr.get_staging_status()["last_error"] is None

        # Swap again to failing double and verify error is captured cleanly in staging status
        ctrl.self_improver = failing_double  # type: ignore[assignment]
        ctrl.apply_factory_assistant_proposal("Rompe staging", proposal_title="Fallo 1")
        assert mgr.wait_until_idle(timeout=5.0) is True
        assert len(failing_double.calls) == 1
        st_after_err = ctrl.get_self_improve_staging_status()
        assert st_after_err["last_error"] == "fallo controlado en staging"
    finally:
        ctrl.shutdown_staging(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_self_improve_staging_get_endpoint_returns_status_without_mutating_git(
    tmp_path: Path,
) -> None:
    """GET /api/self-improve/staging returns worker state, pending commits, and redacted last_error without mutating Git."""

    class _ReadOnlyStagingSpy:
        def __init__(self) -> None:
            self.status_calls = 0
            self.mutated = False

        def ensure_worktree(self) -> Path:
            self.mutated = True
            raise AssertionError("ensure_worktree must not be called by GET /api/self-improve/staging")

        def get_staging_status(self) -> dict[str, Any]:
            self.status_calls += 1
            return {
                "pending_count": 2,
                "commits": [
                    {"hash": "abc1234", "title": "feat: mejora uno"},
                    {"hash": "def5678", "title": "feat: mejora dos"},
                ],
                "is_building": True,
                "queue_size": 1,
                "last_error": "error previo con SECRET_STG_TOKEN",
                "current_task": "feat: mejora tres",
            }

        def shutdown(self, timeout: float = 5.0) -> None:
            del timeout

    store = SqliteEventStore(str(tmp_path / "staging_status.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    spy = _ReadOnlyStagingSpy()
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=spy,  # type: ignore[arg-type]
        secrets_to_redact=("SECRET_STG_TOKEN",),
    )
    try:
        code, payload = _http_json(f"{base_url}/api/self-improve/staging")
        assert code == 200
        assert spy.status_calls == 1
        assert spy.mutated is False
        assert payload["pending_count"] == 2
        assert payload["commits"] == [
            {"hash": "abc1234", "title": "feat: mejora uno"},
            {"hash": "def5678", "title": "feat: mejora dos"},
        ]
        assert payload["is_building"] is True
        assert payload["queue_size"] == 1
        assert payload["current_task"] == "feat: mejora tres"
        assert payload["initialized"] is True
        assert "SECRET_STG_TOKEN" not in str(payload["last_error"])
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_self_improve_promote_endpoint_handles_success_empty_busy_and_error(
    tmp_path: Path,
) -> None:
    """POST /api/self-improve/promote delegates to staging_manager and cleanly handles empty, busy (409), error (400), and success without restarting pytest."""
    from agent_factory.self_improver import FactorySelfImprover

    class _ControllableStagingManager:
        def __init__(self) -> None:
            self.mode = "empty"
            self.promote_calls: list[bool] = []
            self.on_restart_calls = 0

        def promote_staging_to_main(self, trigger_restart: bool = True) -> dict[str, Any]:
            self.promote_calls.append(bool(trigger_restart))
            if self.mode == "empty":
                return {"promoted": False, "merged_commits": 0, "commits": []}
            if self.mode == "busy":
                raise RuntimeError(
                    "No se puede promover staging a main mientras hay mejoras en construcción o en cola."
                )
            if self.mode == "error":
                raise RuntimeError("Falló el merge fast-forward de 'liwi-staging' en 'main': conflict")
            if trigger_restart:
                self.on_restart_calls += 1
            return {
                "promoted": True,
                "merged_commits": 2,
                "commits": [
                    {"hash": "111aaaa", "title": "feat: alpha"},
                    {"hash": "222bbbb", "title": "feat: beta"},
                ],
            }

        def shutdown(self, timeout: float = 5.0) -> None:
            del timeout

    store = SqliteEventStore(str(tmp_path / "staging_promote.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    fake_mgr = _ControllableStagingManager()
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=fake_mgr,  # type: ignore[arg-type]
    )
    try:
        # 1. Empty staging (no pending commits)
        fake_mgr.mode = "empty"
        c_empty, r_empty = _http_json(
            f"{base_url}/api/self-improve/promote",
            method="POST",
            body={},
        )
        assert c_empty == 200
        assert r_empty["ok"] is True
        assert r_empty["promoted"] is False
        assert r_empty["merged_commits"] == 0
        assert r_empty["commits"] == []
        assert r_empty["restart_scheduled"] is False
        assert fake_mgr.on_restart_calls == 0

        # 2. Busy queue / worker building -> 409 Conflict
        fake_mgr.mode = "busy"
        c_busy, r_busy = _http_json(
            f"{base_url}/api/self-improve/promote",
            method="POST",
            body={},
        )
        assert c_busy == 409
        assert "construcción" in r_busy["error"] or "cola" in r_busy["error"]
        assert fake_mgr.on_restart_calls == 0

        # 3. Git merge / branch error -> 400 Bad Request
        fake_mgr.mode = "error"
        c_err, r_err = _http_json(
            f"{base_url}/api/self-improve/promote",
            method="POST",
            body={},
        )
        assert c_err == 400
        assert "fast-forward" in r_err["error"]
        assert fake_mgr.on_restart_calls == 0

        # 4. Success under pytest defaults trigger_restart=False so pytest never restarts
        fake_mgr.mode = "success"
        c_ok, r_ok = _http_json(
            f"{base_url}/api/self-improve/promote",
            method="POST",
            body={},
        )
        assert c_ok == 200
        assert r_ok["ok"] is True
        assert r_ok["promoted"] is True
        assert r_ok["merged_commits"] == 2
        assert len(r_ok["commits"]) == 2
        assert r_ok["restart_scheduled"] is False
        assert fake_mgr.promote_calls[-1] is False
        assert fake_mgr.on_restart_calls == 0

        # 5. Controller's _trigger_staging_restart guards os._exit under pytest and supports test restart_fn
        restart_event = threading.Event()
        restart_codes: list[int] = []

        def _capture_exit(code: int) -> None:
            restart_codes.append(code)
            restart_event.set()

        # Default os._exit must be a safe no-op under pytest
        safe_improver = FactorySelfImprover(sqlite_store=store, repo_root=tmp_path)
        server.controller.self_improver = safe_improver
        server.controller._trigger_staging_restart()

        # Injected non-os._exit restart_fn schedules exit code 42
        spy_improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=tmp_path,
            restart_fn=_capture_exit,
            restart_delay_seconds=0.01,
        )
        server.controller.self_improver = spy_improver
        server.controller._trigger_staging_restart()
        assert restart_event.wait(timeout=2.0) is True
        assert restart_codes == [42]
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_staging_banner_ui_and_promote_flow_html_contract(tmp_path: Path) -> None:
    """GET / serves staging header banner controls, /api/self-improve/staging + promote JS, non-blocking staging apply confirmation, and preserves existing IDs and zero-CDN policy."""
    store = SqliteEventStore(str(tmp_path / "staging_ui_contract.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # Zero external CDNs
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # Existing header & Liwi chat IDs preserved
        for existing_id in (
            "server-health-dot",
            "server-health-label",
            "status-note",
            "view-mode-segmented",
            "view-mode-simple",
            "view-mode-dev",
            "tech-mode-toggle",
            "themes-menu-btn",
            "themes-popover",
            "improve-factory-btn",
            "settings-menu-btn",
            "settings-popover",
            "factory-improve-modal",
            "factory-chat-stream",
            "factory-chat-form",
            "factory-chat-input",
            "factory-chat-send-btn",
            "factory-inline-status",
        ):
            assert f'id="{existing_id}"' in html, f"Missing existing ID: {existing_id}"

        # (a) Header staging banner controls: pill, dropdown list of titles, and 'Actualizar ahora' button
        header_start = html.index("<header>")
        header_end = html.index("</header>", header_start)
        header_html = html[header_start:header_end]

        for staging_id in (
            "staging-banner-wrap",
            "staging-banner-btn",
            "staging-banner-spinner",
            "staging-banner-label",
            "staging-popover",
            "staging-commits-list",
            "staging-popover-error",
            "staging-promote-btn",
            "staging-promote-status",
            "staging-promote-clock",
        ):
            assert f'id="{staging_id}"' in header_html, f"Missing staging header ID: {staging_id}"
        assert "Actualizar ahora" in header_html

        # (b) JS integrates GET /api/self-improve/staging and POST /api/self-improve/promote with real backend fields
        script_start = html.index("<script>")
        script_js = html[script_start:]
        assert "/api/self-improve/staging" in script_js
        assert "/api/self-improve/promote" in script_js
        for backend_field in (
            "pending_count",
            "commits",
            "is_building",
            "queue_size",
            "last_error",
            "current_task",
            "promoted",
            "restart_scheduled",
        ):
            assert backend_field in script_js, f"Missing backend JSON field usage: {backend_field}"

        assert "⏳ Preparando mejora en segundo plano..." in script_js
        assert "1 mejora lista" in script_js
        assert "mejoras listas" in script_js
        assert "Aplicando actualizaciones y reiniciando..." in script_js
        assert "window.location.reload()" in script_js
        assert "escapeHtml(" in script_js

        # (c) Liwi chat apply flow handles queued staging response non-blockingly and triggers staging refresh
        assert "✓ Mejora encolada en segundo plano (staging)" in script_js
        assert "res.queued" in script_js
        assert "refreshSelfImproveStaging" in script_js
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_staging_banner_mobile_responsive_css_inside_media_768(tmp_path: Path) -> None:
    """Mobile CSS adjustments for the staging banner and popover live strictly inside @media (max-width: 768px) without altering desktop header layout."""
    store = SqliteEventStore(str(tmp_path / "staging_mobile_css.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768, "Expected @media (max-width: 768px) block"

        assert "header .controls .staging-banner-wrap" in media_768
        assert "#staging-banner-btn" in media_768
        assert "header .staging-popover" in media_768
        assert "#staging-promote-btn" in media_768

        staging_pop_mobile = _extract_css_block(media_768, "header .staging-popover")
        assert staging_pop_mobile
        assert "position: fixed;" in staging_pop_mobile
        assert "max-width: calc(100vw - 24px);" in staging_pop_mobile

        staging_btn_mobile = _extract_css_block(media_768, "#staging-banner-btn")
        assert staging_btn_mobile
        assert "min-height: 38px;" in staging_btn_mobile
        assert "white-space: nowrap;" in staging_btn_mobile

        # Desktop header rule remains intact outside @media (max-width: 768px)
        desktop_header = _extract_css_block(css, "header {")
        assert "display: flex;" in desktop_header
        assert "justify-content: space-between;" in desktop_header
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_staging_chat_stays_non_blocking_and_promote_polling_is_guarded(
    tmp_path: Path,
) -> None:
    """While staging builds in background, Liwi chat remains non-blocking, tracks queued proposals across refreshes, and guards promote reload probes."""
    store = SqliteEventStore(str(tmp_path / "staging_non_blocking.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        script_js = html[html.index("<script>") :]

        # Chat blocking checks must be gated by isBlockingApplyActive() so staging never locks chat
        assert "function isBlockingApplyActive()" in script_js
        assert "activeStatus.running && !window.__liwiStagingActive" in script_js
        assert "if (consultInFlight || isBlockingApplyActive()) return;" in script_js
        assert "if (!isBlockingApplyActive()) {" in script_js
        assert "const isLocked = isBlockingApplyActive();" in script_js

        # Reload race guard: staging banner clears legacy blocking overlay/controls when active
        assert "window.clearFactoryBlockingForStaging" in script_js

        # Double-click and mid-refresh protection for queued proposals
        assert "locallyQueuedProposalIds" in script_js
        assert "b.getAttribute('data-enqueuing') === '1'" in script_js

        # Promote reload polling prevents overlapping concurrent probes
        promote_fn_start = script_js.index("function startPromoteReloadPolling(")
        promote_fn_end = script_js.index("btn.addEventListener('click'", promote_fn_start)
        promote_fn_body = script_js[promote_fn_start:promote_fn_end]
        assert "probeInFlight" in promote_fn_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_staging_apply_handles_ok_false_and_promote_probe_uses_timeout(
    tmp_path: Path,
) -> None:
    """Apply flow treats ok=false responses as errors restoring the button, and promote reload probes bound hung sockets with AbortController."""
    store = SqliteEventStore(str(tmp_path / "staging_resilience.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        script_js = html[html.index("<script>") :]

        apply_start = script_js.index("apiFetch('/api/factory/assistant/apply'")
        apply_end = script_js.index("async function refreshHistory()", apply_start)
        apply_body = script_js[apply_start:apply_end]
        assert "if (res && res.ok === false)" in apply_body

        promote_start = script_js.index("function startPromoteReloadPolling(")
        promote_end = script_js.index("btn.addEventListener('click'", promote_start)
        promote_body = script_js[promote_start:promote_end]
        assert "AbortController" in promote_body
        assert ".abort()" in promote_body
        assert "clearTimeout(" in promote_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_renders_radar_de_auto_mejoras_ui(tmp_path: Path) -> None:
    """Dashboard HTML renders the '💡 Radar de Auto-Mejoras' UI inside the Liwi modal with zero external CDNs."""
    store = SqliteEventStore(str(tmp_path / "radar_ui.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # Zero external CDNs
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # Existing Liwi modal IDs remain intact
        for existing_id in (
            "factory-improve-modal",
            "factory-improve-title",
            "factory-memory-toggle",
            "factory-memory-panel",
            "close-factory-improve-btn",
            "factory-chat-stream",
            "factory-chat-form",
            "factory-chat-input",
            "factory-chat-send-btn",
        ):
            assert f'id="{existing_id}"' in html, f"Missing existing ID: {existing_id}"

        # Radar section strictly inside Liwi modal (#factory-improve-modal)
        modal_start = html.index('<div id="factory-improve-modal"')
        modal_end = html.index('<div id="factory-fullscreen-build-overlay"', modal_start)
        modal_html = html[modal_start:modal_end]

        assert "💡 Radar de Auto-Mejoras" in modal_html
        for radar_id in (
            "liwi-radar-section",
            "liwi-radar-title",
            "liwi-radar-frequency",
            "liwi-radar-generate-btn",
            "liwi-radar-live-status",
            "liwi-radar-spinner",
            "liwi-radar-status-text",
            "liwi-radar-clock",
            "liwi-radar-grid",
            "liwi-radar-empty",
        ):
            assert f'id="{radar_id}"' in modal_html, f"Missing radar ID in Liwi modal: {radar_id}"

        assert "💡 Generar ideas" in modal_html
        assert ">Manual<" in modal_html
        assert ">Cada 24h<" in modal_html
        assert ">Cada 3 días<" in modal_html

        # Inline JS wires /api/ideas, card expansion, live timers, implement/dismiss actions, and XSS escaping
        script_js = html[html.index("<script>") :]
        assert "/api/ideas" in script_js
        assert "/api/ideas/generate" in script_js
        assert "/api/ideas/settings" in script_js
        assert "/implement" in script_js
        assert "/dismiss" in script_js
        assert "simple_description" in script_js
        assert "+ Encolar" in script_js
        assert "Descartar" in script_js
        assert "stopPropagation()" in script_js
        assert "setInterval(" in script_js
        assert "escapeHtml(" in script_js
        assert "auto_interval_hours" in script_js
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_radar_ideas_responsive_and_endpoints(tmp_path: Path) -> None:
    """Radar CSS uses 2 columns on desktop and 1 column inside @media (max-width: 768px), and /api/ideas endpoints work end-to-end."""
    db_path = tmp_path / "radar_endpoints.db"
    store = SqliteEventStore(str(db_path))
    orch = Orchestrator(store=store, llm=FakeLLM())

    enqueued_calls: list[dict[str, Any]] = []

    class _StubStagingManager:
        def enqueue_improvement(
            self,
            *,
            title: str,
            prompt: str,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            enqueued_calls.append({"title": title, "prompt": prompt, "metadata": metadata or {}})
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": True,
                "queue_size": len(enqueued_calls),
                "last_error": None,
                "current_task": title,
                "initialized": True,
            }

        def get_staging_status(self) -> dict[str, Any]:
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": bool(enqueued_calls),
                "queue_size": len(enqueued_calls),
                "last_error": None,
                "current_task": enqueued_calls[-1]["title"] if enqueued_calls else None,
                "initialized": True,
            }

        def shutdown(self, timeout: float = 5.0) -> None:
            _ = timeout

    generated_payload = json.dumps(
        {
            "ideas": [
                {
                    "title": "⚡ Caché de estado en dashboard",
                    "category": "Rendimiento",
                    "impact": "Alto",
                    "simple_description": "Reduce lecturas repetidas en disco para acelerar la carga.",
                    "technical_prompt": "Agrega caché corta en dashboard.py y verifica con pytest -q --no-cov tests/test_dashboard.py.",
                },
                {
                    "title": "🎨 Atajos de teclado en modal",
                    "category": "UX",
                    "impact": "Medio",
                    "simple_description": "Permite cerrar y navegar tarjetas del radar con teclado.",
                    "technical_prompt": "Agrega soporte de Escape en dashboard.py y verifica con pytest -q --no-cov tests/test_dashboard.py.",
                },
            ]
        }
    )
    fake_runner = FakeRunner(
        results=[
            # 1) git log in build_ideation_context
            CommandResult(exit_code=0, stdout="abc1234 feat(radar): add idea engine\n"),
            # 2) agy read-only ideation output
            CommandResult(exit_code=0, stdout=generated_payload),
        ]
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        runner=fake_runner,
        staging_manager=_StubStagingManager(),  # type: ignore[arg-type]
    )
    try:
        # 1. Responsive CSS check: 2 columns desktop, 1 column strictly inside @media (max-width: 768px)
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]

        desktop_grid = _extract_css_block(css, ".liwi-radar-grid {")
        assert desktop_grid, "Expected desktop .liwi-radar-grid rule"
        assert "display: grid;" in desktop_grid
        assert "repeat(2, minmax(0, 1fr))" in desktop_grid

        desktop_details = _extract_css_block(css, ".liwi-radar-card-details {")
        assert desktop_details, "Expected collapsed .liwi-radar-card-details rule"
        assert "display: none;" in desktop_details

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768, "Expected @media (max-width: 768px) block"
        mobile_grid = _extract_css_block(media_768, ".liwi-radar-grid {")
        assert mobile_grid, "Expected .liwi-radar-grid override inside @media (max-width: 768px)"
        assert "grid-template-columns: 1fr;" in mobile_grid
        assert ".liwi-radar-head" in media_768
        assert ".liwi-radar-controls" in media_768

        # 2. GET /api/ideas initially empty with default auto_interval_hours=0.0
        code, initial = _http_json(f"{base_url}/api/ideas")
        assert code == 200
        assert initial["ideas"] == []
        assert initial["auto_interval_hours"] == 0.0

        # 3. POST /api/ideas/settings persists frequency and rejects invalid input
        s_code, s_data = _http_json(
            f"{base_url}/api/ideas/settings",
            method="POST",
            body={"auto_interval_hours": 24},
        )
        assert s_code == 200
        assert s_data["ok"] is True
        assert s_data["auto_interval_hours"] == 24.0

        bad_s_code, _ = _http_json(
            f"{base_url}/api/ideas/settings",
            method="POST",
            body={"auto_interval_hours": -5},
        )
        assert bad_s_code == 400

        code_after_setting, list_after_setting = _http_json(f"{base_url}/api/ideas")
        assert code_after_setting == 200
        assert list_after_setting["auto_interval_hours"] == 24.0

        # 4. POST /api/ideas/generate creates and returns ideas
        g_code, g_data = _http_json(
            f"{base_url}/api/ideas/generate",
            method="POST",
            body={"count": 2},
        )
        assert g_code == 200
        assert g_data["ok"] is True
        assert g_data["created_count"] == 2
        assert len(g_data["ideas"]) == 2
        by_title = {item["title"]: item for item in g_data["ideas"]}
        first_idea = by_title["⚡ Caché de estado en dashboard"]
        second_idea = by_title["🎨 Atajos de teclado en modal"]
        assert first_idea["impact"] == "Alto"
        assert first_idea["status"] == "nueva"
        assert first_idea["simple_description"] == "Reduce lecturas repetidas en disco para acelerar la carga."

        # 5. POST /api/ideas/<id>/implement enqueues into staging and marks status='encolada'
        imp_code, imp_data = _http_json(
            f"{base_url}/api/ideas/{first_idea['id']}/implement",
            method="POST",
        )
        assert imp_code == 200
        assert imp_data["ok"] is True
        assert imp_data["idea"]["status"] == "encolada"
        assert len(enqueued_calls) == 1
        assert enqueued_calls[0]["title"] == "⚡ Caché de estado en dashboard"
        assert "Agrega caché corta en dashboard.py" in enqueued_calls[0]["prompt"]

        # Duplicate implement returns 409; invalid ID returns 400; missing ID returns 404
        dup_code, _ = _http_json(
            f"{base_url}/api/ideas/{first_idea['id']}/implement",
            method="POST",
        )
        assert dup_code == 409
        bad_id_code, _ = _http_json(f"{base_url}/api/ideas/not-an-int/implement", method="POST")
        assert bad_id_code == 400
        missing_code, _ = _http_json(f"{base_url}/api/ideas/99999/implement", method="POST")
        assert missing_code == 404

        # 6. POST /api/ideas/<id>/dismiss marks 'descartada' and removes from default GET /api/ideas
        dis_code, dis_data = _http_json(
            f"{base_url}/api/ideas/{second_idea['id']}/dismiss",
            method="POST",
        )
        assert dis_code == 200
        assert dis_data["ok"] is True
        assert dis_data["idea"]["status"] == "descartada"

        code_final, final_list = _http_json(f"{base_url}/api/ideas")
        assert code_final == 200
        assert [item["id"] for item in final_list["ideas"]] == [first_idea["id"]]
        assert final_list["ideas"][0]["status"] == "encolada"
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_ideas_api_generate_frequency_and_404_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/api/ideas endpoints use telemetry SQLite DB, support auto_interval_hours on /generate, and return 404 on missing IDs."""
    from agent_factory import idea_engine

    telemetry_db = tmp_path / "telemetry.db"
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(telemetry_db))

    store = SqliteEventStore(str(tmp_path / "events.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())

    generate_calls: list[dict[str, Any]] = []
    enqueued_calls: list[dict[str, Any]] = []

    def _fake_generate_new_ideas(
        *,
        source: str = "manual",
        count: int = 3,
        db_path: Path | str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        generate_calls.append({"source": source, "count": count, "db_path": Path(db_path) if db_path else None})
        idx = len(generate_calls)
        return idea_engine.save_ideas(
            [
                {
                    "title": f"💡 Mejora Radar #{idx}",
                    "category": "UX",
                    "impact": "Alto",
                    "simple_description": f"Descripción simple #{idx}",
                    "technical_prompt": f"Prompt técnico TDD #{idx}",
                }
            ],
            source=source,
            db_path=db_path,
            max_items=count,
        )

    class _FakeStagingManager:
        def enqueue_improvement(
            self,
            *,
            title: str,
            prompt: str,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            enqueued_calls.append({"title": title, "prompt": prompt, "metadata": metadata or {}})
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": True,
                "queue_size": len(enqueued_calls),
                "last_error": None,
                "current_task": title,
                "initialized": True,
            }

        def get_staging_status(self) -> dict[str, Any]:
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": bool(enqueued_calls),
                "queue_size": len(enqueued_calls),
                "last_error": None,
                "current_task": enqueued_calls[-1]["title"] if enqueued_calls else None,
                "initialized": True,
            }

        def shutdown(self, timeout: float = 5.0) -> None:
            _ = timeout

    monkeypatch.setattr(idea_engine, "generate_new_ideas", _fake_generate_new_ideas)

    # Seed one pre-existing idea directly in telemetry_db to prove GET /api/ideas reads it
    seeded = idea_engine.save_ideas(
        [
            {
                "title": "🛡️ Validar entradas en API",
                "category": "Seguridad",
                "impact": "Alto",
                "simple_description": "Asegura validación estricta en endpoints.",
                "technical_prompt": "Valida IDs numéricos en dashboard.py con pruebas dirigidas.",
            }
        ],
        source="auto",
        db_path=telemetry_db,
    )
    seeded_id = seeded[0]["id"]

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=_FakeStagingManager(),  # type: ignore[arg-type]
    )
    try:
        # 1. GET /api/ideas returns seeded idea from telemetry_db and default auto_interval_hours=0.0
        code, initial = _http_json(f"{base_url}/api/ideas")
        assert code == 200
        assert initial["auto_interval_hours"] == 0.0
        assert [it["id"] for it in initial["ideas"]] == [seeded_id]
        assert initial["ideas"][0]["title"] == "🛡️ Validar entradas en API"

        # 2. POST /api/ideas/generate with only auto_interval_hours saves frequency without invoking generator
        freq_code, freq_data = _http_json(
            f"{base_url}/api/ideas/generate",
            method="POST",
            body={"auto_interval_hours": 72},
        )
        assert freq_code == 200
        assert freq_data["ok"] is True
        assert freq_data["auto_interval_hours"] == 72.0
        assert freq_data["created_count"] == 0
        assert freq_data["created"] == []
        assert [it["id"] for it in freq_data["ideas"]] == [seeded_id]
        assert len(generate_calls) == 0
        assert idea_engine.get_auto_interval_hours(db_path=telemetry_db) == 72.0

        # Invalid auto_interval_hours rejected with 400 and does not invoke generator
        bad_freq_code, _ = _http_json(
            f"{base_url}/api/ideas/generate",
            method="POST",
            body={"auto_interval_hours": -12},
        )
        assert bad_freq_code == 400
        assert len(generate_calls) == 0

        # 3. POST /api/ideas/generate with auto_interval_hours + count updates frequency AND generates
        gen_code, gen_data = _http_json(
            f"{base_url}/api/ideas/generate",
            method="POST",
            body={"auto_interval_hours": 24, "count": 2},
        )
        assert gen_code == 200
        assert gen_data["ok"] is True
        assert gen_data["auto_interval_hours"] == 24.0
        assert gen_data["created_count"] == 1
        assert len(gen_data["ideas"]) == 2
        assert len(generate_calls) == 1
        assert generate_calls[0]["db_path"] == telemetry_db.resolve()
        assert idea_engine.get_auto_interval_hours(db_path=telemetry_db) == 24.0
        generated_id = gen_data["created"][0]["id"]

        # 4. POST /api/ideas/<id>/implement enqueues technical_prompt and marks 'encolada'; 404 for unknown ID
        imp_code, imp_data = _http_json(
            f"{base_url}/api/ideas/{generated_id}/implement",
            method="POST",
        )
        assert imp_code == 200
        assert imp_data["ok"] is True
        assert imp_data["idea"]["status"] == "encolada"
        assert len(enqueued_calls) == 1
        assert enqueued_calls[0]["prompt"] == "Prompt técnico TDD #1"
        assert idea_engine.get_idea(generated_id, db_path=telemetry_db)["status"] == "encolada"

        missing_imp_code, _ = _http_json(f"{base_url}/api/ideas/999999/implement", method="POST")
        assert missing_imp_code == 404

        # 5. POST /api/ideas/<id>/dismiss marks 'descartada'; 404 for unknown ID; 400 for invalid ID
        dis_code, dis_data = _http_json(
            f"{base_url}/api/ideas/{seeded_id}/dismiss",
            method="POST",
        )
        assert dis_code == 200
        assert dis_data["ok"] is True
        assert dis_data["idea"]["status"] == "descartada"
        assert idea_engine.get_idea(seeded_id, db_path=telemetry_db)["status"] == "descartada"

        missing_dis_code, _ = _http_json(f"{base_url}/api/ideas/999999/dismiss", method="POST")
        assert missing_dis_code == 404

        bad_dis_code, _ = _http_json(f"{base_url}/api/ideas/not-valid/dismiss", method="POST")
        assert bad_dis_code == 400

        # Final GET /api/ideas reflects persisted state and frequency
        final_code, final_data = _http_json(f"{base_url}/api/ideas")
        assert final_code == 200
        assert final_data["auto_interval_hours"] == 24.0
        assert [it["id"] for it in final_data["ideas"]] == [generated_id]
        assert final_data["ideas"][0]["status"] == "encolada"
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_dashboard_radar_idea_db_path_resolves_to_telemetry_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DashboardController resolves idea DB to data/telemetry.db in production and respects AGENT_FACTORY_TELEMETRY_DB."""
    monkeypatch.delenv("AGENT_FACTORY_TELEMETRY_DB", raising=False)

    # When store DB is in a temp directory, ideas DB stays isolated in <tmp>/telemetry.db
    store = SqliteEventStore(str(tmp_path / "agent_factory.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    try:
        ctrl = DashboardController(orchestrator=orch)
        assert ctrl._resolve_idea_db_path() == (tmp_path / "telemetry.db").resolve()

        # When AGENT_FACTORY_TELEMETRY_DB is set, it takes precedence
        override_db = tmp_path / "custom_telemetry.db"
        monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(override_db))
        assert ctrl._resolve_idea_db_path() == override_db.resolve()
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_auto_ideas_header_button_scrolls_to_radar_and_auto_loads_ideas(
    tmp_path: Path,
) -> None:
    """Header top bar exposes visible '💡 Auto-Mejoras' button with badge next to '#improve-factory-btn' (desktop + mobile), scrolls to '#liwi-radar-section', prevents modal meta clipping, and auto-loads '/api/ideas' on startup."""
    store = SqliteEventStore(str(tmp_path / "auto_ideas_header.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        header_start = html.index("<header>")
        header_end = html.index("</header>", header_start)
        header_html = html[header_start:header_end]

        assert 'id="auto-ideas-btn"' in header_html
        assert 'id="auto-ideas-count"' in header_html
        assert "💡 Auto-Mejoras" in header_html
        assert 'id="improve-factory-btn"' in header_html
        assert header_html.index('id="improve-factory-btn"') != header_html.index('id="auto-ideas-btn"')

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]
        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert "#auto-ideas-btn" in media_768

        meta_col_rule = _extract_css_block(css, ".factory-chat-meta-col {")
        assert "overflow-y" not in meta_col_rule
        assert "max-height" not in meta_col_rule
        radar_modal_rule = _extract_css_block(
            css,
            '#factory-improve-modal[data-view="radar"] .factory-chat-modal {',
        )
        assert "overflow-y: auto;" in radar_modal_rule

        script_js = html[html.index("<script>") :]
        assert "auto-ideas-btn" in script_js
        assert "focusRadar" in script_js
        assert "setFactoryModalView" in script_js
        assert "auto-ideas-count" in script_js
        radar_init = script_js[script_js.index("(function initLiwiIdeaRadar()") :]
        assert "\n      loadIdeas();" in radar_init
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_header_popover_controller_staging_autoclose_and_mutual_exclusion(
    tmp_path: Path,
) -> None:
    """Header popovers (staging & settings) use a central JS controller with outside-click, Escape, mutual exclusion, and non-invasive staging close/positioning."""
    store = SqliteEventStore(str(tmp_path / "popover_ctrl_staging.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        header_start = html.index("<header>")
        header_end = html.index("</header>", header_start)
        header_html = html[header_start:header_end]

        # Staging popover has an explicit non-invasive close button inside its header
        assert 'id="staging-popover-close"' in header_html
        assert 'id="staging-popover"' in header_html
        assert header_html.index('id="staging-popover"') < header_html.index('id="staging-popover-close"')

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]
        staging_pop_desktop = _extract_css_block(css, ".staging-popover {")
        assert staging_pop_desktop
        assert "max-height:" in staging_pop_desktop
        assert "overflow-y: auto;" in staging_pop_desktop

        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768
        staging_pop_mobile = _extract_css_block(media_768, "header .staging-popover")
        assert staging_pop_mobile
        assert "max-height:" in staging_pop_mobile
        assert "overflow-y: auto;" in staging_pop_mobile

        script_js = html[html.index("<script>") :]
        assert "headerPopoverController" in script_js
        assert "createHeaderPopoverController" in script_js
        assert "handleOutsideClick" in script_js
        assert "handleKeydown" in script_js
        assert "exceptId" in script_js
        assert "document.addEventListener('click'" in script_js
        assert "document.addEventListener('keydown'" in script_js
        assert "ev.key === 'Escape'" in script_js
        assert "headerPopoverController.register('settings'" in script_js
        assert "headerPopoverController.register('staging'" in script_js
        assert "headerPopoverController.toggle('settings')" in script_js
        assert "headerPopoverController.toggle('staging')" in script_js
        assert "headerPopoverController.close('staging'" in script_js
        assert "getBoundingClientRect()" in script_js
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_compact_header_groups_themes_inside_settings_menu(
    tmp_path: Path,
) -> None:
    """Compact header groups Temas inside the single Ajustes dropdown while preserving all existing HTML IDs, 5 themes, zero CDNs, and desktop/mobile CSS boundaries."""
    store = SqliteEventStore(str(tmp_path / "compact_header_themes.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        header_start = html.index("<header>")
        header_end = html.index("</header>", header_start)
        header_html = html[header_start:header_end]

        # All existing header IDs preserved
        for dom_id in (
            "server-health-dot",
            "server-health-label",
            "status-note",
            "view-mode-segmented",
            "view-mode-simple",
            "view-mode-dev",
            "tech-mode-toggle",
            "staging-banner-wrap",
            "staging-banner-btn",
            "staging-popover",
            "auto-ideas-btn",
            "auto-ideas-count",
            "improve-factory-btn",
            "settings-menu-btn",
            "settings-popover",
            "lan-mode-toggle",
            "voice-tts-toggle",
            "theme-btn",
            "themes-menu-btn",
            "themes-popover",
            "settings-feedback",
        ):
            assert f'id="{dom_id}"' in header_html, f"Missing preserved header ID: {dom_id}"

        # Temas selector is grouped inside #settings-popover instead of adding a standalone top-bar button
        settings_pop_idx = header_html.index('id="settings-popover"')
        themes_btn_idx = header_html.index('id="themes-menu-btn"')
        themes_pop_idx = header_html.index('id="themes-popover"')
        settings_fb_idx = header_html.index('id="settings-feedback"')
        assert settings_pop_idx < themes_btn_idx < themes_pop_idx < settings_fb_idx

        # All 5 theme choices remain intact inside #themes-popover
        themes_section = header_html[themes_pop_idx:settings_fb_idx]
        for choice in ("dark", "light", "emerald", "sunset", "lavender"):
            assert f'data-theme-choice="{choice}"' in themes_section

        # Mobile responsive rules for the compact grouped settings/themes dropdown live inside @media (max-width: 768px)
        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]
        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert media_768
        assert "header .settings-popover .themes-popover" in media_768
        assert "header .settings-popover .themes-menu-wrap" in media_768

        # Desktop header and controls rules remain untouched outside @media (max-width: 768px)
        desktop_css = css.replace(media_768, "")
        desktop_header = _extract_css_block(desktop_css, "header {")
        assert "display: flex;" in desktop_header
        assert "justify-content: space-between;" in desktop_header
        assert ".controls { display: flex; align-items: center; gap: 10px; }" in desktop_css
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_autonomous_quota_visual_audit_and_project_ideas_endpoints_and_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import subprocess

    from agent_factory import idea_engine, visual_qa_agent
    from agent_factory.builder import BuildWorker

    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(tmp_path / "telemetry_dash.db"))
    store = SqliteEventStore(db_path=tmp_path / "dash_auto.db")
    chats_dir = tmp_path / "chats_auto"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_auto")
    pid = "auto-proj-app"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "README.md").write_text("# Auto Project\n", encoding="utf-8")
    (ws_dir / "index.html").write_text("<html><body><h1>Auto</h1></body></html>\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "init"],
        cwd=ws_dir,
        check=True,
        capture_output=True,
    )

    llm = FakeLLM(responses=[])
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    plan_dict = _plan_payload("Auto Project App")
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Auto Project", "objective": "App", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": "abc1234", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    idea_json = json.dumps(
        [
            {
                "title": "✨ Mejorar accesibilidad",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Agrega etiquetas ARIA claras.",
                "change_prompt": "Añade atributos aria-label al formulario principal.",
            }
        ]
    )
    fake_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout=idea_json, stderr="")])
    fake_agy = tmp_path / ("agy.EXE" if os.name == "nt" else "agy")
    fake_agy.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(impl_steps=[{"index.html": "<html><body><main>Updated</main></body></html>\n"}]),
        runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="ok")]),
        chats_dir=chats_dir,
    )

    monkeypatch.setattr(
        visual_qa_agent,
        "run_synthetic_user_audit",
        lambda **_kwargs: {
            "ok": True,
            "findings_count": 1,
            "ideas_created_count": 1,
            "findings": ["Botón sin contraste en móvil"],
            "created": [],
        },
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        build_worker=worker,
        workspace_manager=ws_mgr,
        chats_dir=chats_dir,
        runner=fake_runner,
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for marker in (
            'id="autonomous-toggle-btn"',
            'id="quota-meter-pill"',
            'id="liwi-reserve-threshold"',
            'id="liwi-autonomous-cooldown"',
            'id="liwi-visual-audit-btn"',
            'id="liwi-subtab-history-btn"',
            'id="liwi-autonomous-history-panel"',
            'id="tab-ideas-btn"',
            'id="tab-ideas"',
            'id="factory-modal-switch-view-btn"',
            'id="liwi-pilot-settings-btn"',
            'id="liwi-pilot-settings-panel"',
            "setFactoryModalView",
            'data-view="chat"',
            "liwi-radar-card-preview",
            "-webkit-line-clamp: 2",
            "No se pudieron cargar los proyectos:",
            "function clearFactoryBlockingForStaging()",
            "/api/autonomous/status",
            "/api/autonomous/toggle",
            "/api/autonomous/revert/",
            "/api/ideas/visual-audit",
        ):
            assert marker in html

        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        media_768 = _extract_css_block(html[style_start:style_end], "@media (max-width: 768px)")
        assert ".autonomous-controls-wrap" in media_768
        assert ".proj-ideas-grid" in media_768

        # 1. Autonomous status & toggle
        c_st, r_st = _http_json(f"{base_url}/api/autonomous/status")
        assert c_st == 200
        assert r_st["state"] == "apagado"
        assert "quota" in r_st

        c_tog, r_tog = _http_json(
            f"{base_url}/api/autonomous/toggle",
            method="POST",
            body={"autonomous_enabled": False, "cooldown_minutes": 20, "reserve_threshold_pct": 40},
        )
        assert c_tog == 200
        assert r_tog["cooldown_minutes"] == 20
        assert r_tog["quota"]["reserve_threshold_pct"] == 40

        # 2. Visual audit endpoint
        c_va, r_va = _http_json(f"{base_url}/api/ideas/visual-audit", method="POST", body={})
        assert c_va == 200
        assert r_va["findings_count"] == 1
        assert r_va["ideas_created_count"] == 1

        # 3. Per-project ideas endpoints
        c_gen, r_gen = _http_json(
            f"{base_url}/api/projects/{pid}/ideas/generate",
            method="POST",
            body={"count": 1},
        )
        assert c_gen == 200
        assert r_gen["created_count"] == 1
        proj_idea_id = r_gen["created"][0]["id"]

        c_list, r_list = _http_json(f"{base_url}/api/projects/{pid}/ideas")
        assert c_list == 200
        assert len(r_list["ideas"]) == 1

        c_app, r_app = _http_json(
            f"{base_url}/api/projects/{pid}/ideas/{proj_idea_id}/approve",
            method="POST",
            body={},
        )
        assert c_app == 200
        assert r_app["idea"]["status"] == "implementando"
        assert server.controller.wait_for_idle(timeout=10.0)

        c_dis, r_dis = _http_json(
            f"{base_url}/api/projects/{pid}/ideas/{proj_idea_id}/dismiss",
            method="POST",
            body={},
        )
        assert c_dis == 200
        assert r_dis["idea"]["status"] == "descartada"

        # 4. start_local_app failure creates runtime_error idea
        monkeypatch.setattr(
            server.controller.local_runner,
            "start",
            lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("Fallo simulado al arrancar puerto")),
        )
        c_fail, _ = _http_json(f"{base_url}/api/projects/{pid}/start", method="POST", body={})
        assert c_fail == 409
        after_err_ideas = idea_engine.list_project_ideas(pid, db_path=tmp_path / "telemetry_dash.db")
        assert any(it["source"] == "runtime_error" for it in after_err_ideas)
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_triage_client_error_tickets_and_banner_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(tmp_path / "telemetry_triage.db"))
    store = SqliteEventStore(db_path=tmp_path / "dash_triage.db")
    orch = Orchestrator(store=store, llm=FakeLLM())

    enqueued_repairs: list[dict[str, Any]] = []

    class _SpyStaging:
        def enqueue_improvement(self, *, title: str, prompt: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
            enqueued_repairs.append({"title": title, "prompt": prompt, "metadata": metadata or {}})
            return {"pending_count": 0, "is_building": True, "queue_size": 1, "commits": []}

        def shutdown(self, timeout: float = 2.0) -> None:
            _ = timeout

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=_SpyStaging(),  # type: ignore[arg-type]
    )
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")

        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()
        for marker in (
            'id="triage-alert-banner"',
            'id="triage-repair-btn"',
            'id="triage-copy-prompt-btn"',
            'id="triage-dismiss-btn"',
            "ANTI_SPAM_MS = 15000",
            "return 8000;",
            'id="retry-load-projects-btn"',
            "🔄 Reintentar",
            "res.status !== 502 && res.status !== 503 && res.status !== 504",
            "/api/telemetry/client-error",
            "/api/triage/tickets",
        ):
            assert marker in html

        # 1. Report JS error -> creates ticket with simple_summary + repair_prompt
        c_post, r_post = _http_json(
            f"{base_url}/api/telemetry/client-error",
            method="POST",
            body={
                "source": "window.onerror",
                "error_text": "ReferenceError: foo is not defined at src/agent_factory/dashboard.py:7001",
                "context": {"lineno": 7001},
            },
        )
        assert c_post == 200
        assert r_post["ok"] is True
        tid = r_post["ticket"]["id"]
        assert "src/agent_factory/dashboard.py:7001" in r_post["ticket"]["repair_prompt"]

        # 2. List pending tickets
        c_list, r_list = _http_json(f"{base_url}/api/triage/tickets")
        assert c_list == 200
        assert len(r_list["tickets"]) == 1
        assert r_list["tickets"][0]["id"] == tid

        # 3. Repair enqueues repair_prompt into .liwi-staging
        c_rep, r_rep = _http_json(f"{base_url}/api/triage/tickets/{tid}/repair", method="POST", body={})
        assert c_rep == 200
        assert r_rep["queued"] is True
        assert r_rep["ticket"]["status"] == "reparando"
        assert len(enqueued_repairs) == 1
        assert "src/agent_factory/dashboard.py:7001" in enqueued_repairs[0]["prompt"]

        # 4. Dismiss hides ticket from pending list
        c_dis, r_dis = _http_json(f"{base_url}/api/triage/tickets/{tid}/dismiss", method="POST", body={})
        assert c_dis == 200
        assert r_dis["ticket"]["status"] == "descartado"
        _, r_after = _http_json(f"{base_url}/api/triage/tickets")
        assert r_after["tickets"] == []
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_autonomous_status_watchdog_nonblocking_and_fault_tolerant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/autonomous/status stays fast (no git publish or workspace scans), non-blocking at boot, and 200 OK on DB errors."""
    import sqlite3
    import time

    from agent_factory import idea_engine, runtime_runner

    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(tmp_path / "telemetry_watchdog.db"))
    store = SqliteEventStore(db_path=tmp_path / "dash_watchdog.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_watchdog")
    pid = "watchdog-proj"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<html><body><h1>Watchdog</h1></body></html>\n", encoding="utf-8")

    publish_calls: list[str] = []

    class _SpyPublisher:
        def publish(self, status: Any) -> None:
            publish_calls.append(str(status.project_id))

    orch = Orchestrator(
        store=store,
        llm=FakeLLM(),
        publisher=_SpyPublisher(),  # type: ignore[arg-type]
        workspace_manager=ws_mgr,
    )
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {"name": "Watchdog Proj", "objective": "Fast healthcheck", "state": "INTERVIEW"},
            at=now,
        )
    )

    detect_calls: list[Path] = []
    orig_detect = runtime_runner.detect_workspace_app

    def _spy_detect(path: Path) -> Any:
        detect_calls.append(path)
        return orig_detect(path)

    monkeypatch.setattr(runtime_runner, "detect_workspace_app", _spy_detect)

    # 1. Non-blocking cold boot: even if autonomous auto-start blocks on SQLite, HTTP server binds immediately.
    autostart_gate = threading.Event()
    autostart_entered = threading.Event()
    real_get_auto_state = idea_engine.get_autonomous_state

    def _slow_first_autostart(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if not autostart_entered.is_set():
            autostart_entered.set()
            autostart_gate.wait(timeout=3.0)
        return real_get_auto_state(*args, **kwargs)

    monkeypatch.setattr(idea_engine, "get_autonomous_state", _slow_first_autostart)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    t0 = time.monotonic()
    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        secrets_to_redact=("SUPER_SECRET_BOOT_999",),
    )
    boot_elapsed = time.monotonic() - t0
    try:
        assert boot_elapsed < 1.0, f"start_dashboard_server blocked {boot_elapsed:.2f}s on autonomous autostart"

        # Restore normal get_autonomous_state for the HTTP probe while autostart thread is still waiting
        monkeypatch.setattr(idea_engine, "get_autonomous_state", real_get_auto_state)

        # 2. GET /api/autonomous/status must NOT trigger publisher.publish() or detect_workspace_app() scans
        code, payload = _http_json(f"{base_url}/api/autonomous/status", timeout=2.0)
        assert code == 200
        assert payload["state"] == "apagado"
        assert "quota" in payload
        assert publish_calls == []
        assert detect_calls == []

        # 3. Transient SQLite lock inside get_status_snapshot must degrade to 200 OK (never drop connection / 500)
        eng = server.controller._get_autonomous_engine()
        monkeypatch.setattr(
            eng,
            "get_status_snapshot",
            lambda **_kw: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked SUPER_SECRET_BOOT_999")
            ),
        )
        deg_code, deg_payload = _http_json(f"{base_url}/api/autonomous/status", timeout=2.0)
        assert deg_code == 200
        assert deg_payload["degraded"] is True
        assert deg_payload["state"] == "apagado"
        assert deg_payload["history"] == []
        assert "quota" in deg_payload
        assert "SUPER_SECRET_BOOT_999" not in json.dumps(deg_payload)
    finally:
        autostart_gate.set()
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_projects_list_fault_tolerance_and_triage_ignores_transient_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/projects never drops connections (502) on bad/locked store rows, avoids sync publish, and client-error triage ignores transient 502/503/504."""
    import sqlite3

    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(tmp_path / "telemetry_502.db"))
    store = SqliteEventStore(db_path=tmp_path / "dash_502.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_502")
    secret = "SECRET_PROJECTS_502_TOKEN"

    publish_calls: list[str] = []

    class _SpyPublisher:
        def publish(self, status: Any) -> None:
            publish_calls.append(str(status.project_id))

    orch = Orchestrator(
        store=store,
        llm=FakeLLM(),
        publisher=_SpyPublisher(),  # type: ignore[arg-type]
        workspace_manager=ws_mgr,
    )
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

    # 1. Healthy project
    store.append(
        create_event(
            "healthy-proj",
            EventType.PROJECT_CREATED,
            {"name": "Healthy App", "objective": "Works cleanly", "state": "INTERVIEW"},
            at=now,
        )
    )
    # 2. Corrupted/invalid project_id stored in SQLite (previously double-raised ValueError inside is_running)
    store.append(
        create_event(
            "INVALID_PID_ROW",
            EventType.PROJECT_CREATED,
            {"name": "Broken ID", "objective": "Bad id", "state": "INTERVIEW"},
            at=now,
        )
    )
    # 3. Imported project whose external folder no longer exists on disk
    missing_ext = str((tmp_path / "deleted-external-dir").resolve())
    store.append(
        create_event(
            "missing-ext-proj",
            EventType.PROJECT_CREATED,
            {
                "name": "Moved External App",
                "objective": "External folder removed",
                "state": "INTERVIEW",
                "external_workspace_path": missing_ext,
            },
            at=now,
        )
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        secrets_to_redact=(secret,),
    )
    try:
        # GET /api/projects succeeds with 200, degrades invalid row cleanly, survives missing external dir, and never invokes publisher.publish()
        code, payload = _http_json(f"{base_url}/api/projects", timeout=3.0)
        assert code == 200
        by_id = {p["project_id"]: p for p in payload["projects"]}
        assert by_id["healthy-proj"]["state"] == "INTERVIEW"
        assert by_id["INVALID_PID_ROW"]["state"] == "ERROR"
        assert by_id["missing-ext-proj"]["state"] == "INTERVIEW"
        assert by_id["missing-ext-proj"]["local_app"] == {
            "running": False,
            "url": None,
            "app_type": "unknown",
        }
        assert publish_calls == []

        # Transient SQLite lock on list_project_ids returns clean redacted 503 JSON (never drops connection / 502)
        monkeypatch.setattr(
            store,
            "list_project_ids",
            lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError(f"database is locked {secret}")
            ),
        )
        err_code, err_payload = _http_json(f"{base_url}/api/projects", timeout=3.0)
        assert err_code == 503
        assert err_payload["degraded"] is True
        assert "error" in err_payload
        assert secret not in json.dumps(err_payload)

        # POST /api/telemetry/client-error ignores transient fetch_500 gateway statuses (502/503/504) without creating tickets
        c_502, r_502 = _http_json(
            f"{base_url}/api/telemetry/client-error",
            method="POST",
            body={
                "source": "fetch_500",
                "error_text": "HTTP 502 en /api/projects: Error interno",
                "context": {"url": "/api/projects", "status": 502},
            },
        )
        assert c_502 == 200
        assert r_502["ok"] is True
        assert r_502["ignored"] is True
        assert r_502["ticket"] is None

        c_tickets_empty, r_tickets_empty = _http_json(f"{base_url}/api/triage/tickets")
        assert c_tickets_empty == 200
        assert r_tickets_empty["tickets"] == []

        # Genuine HTTP 500 still creates a triage ticket
        c_500, r_500 = _http_json(
            f"{base_url}/api/telemetry/client-error",
            method="POST",
            body={
                "source": "fetch_500",
                "error_text": "HTTP 500 en /api/projects: Fallo real",
                "context": {"url": "/api/projects", "status": 500},
            },
        )
        assert c_500 == 200
        assert r_500["ok"] is True
        assert r_500["ticket"] is not None
        assert r_500["ticket"]["source"] == "fetch_500"
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_ideas_and_project_ideas_status_filters_and_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/ideas and GET /api/projects/<slug>/ideas support ?status= filtering and always expose exact counts."""
    from agent_factory import idea_engine

    telemetry_db = tmp_path / "telemetry_filters.db"
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(telemetry_db))

    store = SqliteEventStore(db_path=tmp_path / "dash_filters.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_filters")
    orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
    pid = "filter-demo"
    ws_mgr.create_workspace(pid)
    orch.create_project(pid, "Filter Demo", "Demo project for idea filters")

    # Seed factory ideas across all 5 canonical statuses
    factory_saved = idea_engine.save_ideas(
        [
            {
                "title": "💡 Factory Pending",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Pending desc",
                "technical_prompt": "Pending prompt",
            },
            {
                "title": "📥 Factory Queued",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Queued desc",
                "technical_prompt": "Queued prompt",
            },
            {
                "title": "🔧 Factory Implementing",
                "category": "Estabilidad",
                "impact": "Alto",
                "simple_description": "Impl desc",
                "technical_prompt": "Impl prompt",
            },
            {
                "title": "✅ Factory Implemented",
                "category": "Rendimiento",
                "impact": "Alto",
                "simple_description": "Done desc",
                "technical_prompt": "Done prompt",
            },
            {
                "title": "🗑️ Factory Dismissed",
                "category": "Producto",
                "impact": "Medio",
                "simple_description": "Dismissed desc",
                "technical_prompt": "Dismissed prompt",
            },
        ],
        source="manual",
        db_path=telemetry_db,
    )
    f_ids = {item["title"]: item["id"] for item in factory_saved}
    idea_engine.update_idea_status(f_ids["📥 Factory Queued"], "queued", db_path=telemetry_db)
    idea_engine.update_idea_status(f_ids["🔧 Factory Implementing"], "implementing", db_path=telemetry_db)
    idea_engine.update_idea_status(f_ids["✅ Factory Implemented"], "implemented", db_path=telemetry_db)
    idea_engine.dismiss_idea(f_ids["🗑️ Factory Dismissed"], db_path=telemetry_db)

    # Seed project ideas across all 5 canonical statuses
    proj_saved = idea_engine.save_project_ideas(
        pid,
        [
            {
                "title": "💡 Proj Pending",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Pending",
                "change_prompt": "Prompt 1",
            },
            {
                "title": "📥 Proj Queued",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Queued",
                "change_prompt": "Prompt 2",
            },
            {
                "title": "🔧 Proj Implementing",
                "category": "Estabilidad",
                "impact": "Alto",
                "simple_description": "Implementing",
                "change_prompt": "Prompt 3",
            },
            {
                "title": "✅ Proj Implemented",
                "category": "Rendimiento",
                "impact": "Alto",
                "simple_description": "Implemented",
                "change_prompt": "Prompt 4",
            },
            {
                "title": "🗑️ Proj Dismissed",
                "category": "Producto",
                "impact": "Medio",
                "simple_description": "Dismissed",
                "change_prompt": "Prompt 5",
            },
        ],
        source="manual",
        db_path=telemetry_db,
    )
    p_ids = {item["title"]: item["id"] for item in proj_saved}
    idea_engine.update_project_idea_status(p_ids["📥 Proj Queued"], "queued", project_slug=pid, db_path=telemetry_db)
    idea_engine.update_project_idea_status(p_ids["🔧 Proj Implementing"], "implementing", project_slug=pid, db_path=telemetry_db)
    idea_engine.update_project_idea_status(p_ids["✅ Proj Implemented"], "implemented", project_slug=pid, db_path=telemetry_db)
    idea_engine.update_project_idea_status(p_ids["🗑️ Proj Dismissed"], "dismissed", project_slug=pid, db_path=telemetry_db)

    expected_counts = {
        "pending": 1,
        "queued": 1,
        "implementing": 1,
        "implemented": 1,
        "dismissed": 1,
        "all": 5,
    }

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
    )
    try:
        # 1. Default GET /api/ideas returns active ideas + full counts
        c_def, r_def = _http_json(f"{base_url}/api/ideas")
        assert c_def == 200
        assert {it["title"] for it in r_def["ideas"]} == {
            "💡 Factory Pending",
            "📥 Factory Queued",
            "🔧 Factory Implementing",
        }
        assert r_def["counts"] == expected_counts
        assert r_def["auto_interval_hours"] == 0.0

        # 2. Filtered GET /api/ideas?status=...
        for st, expected_titles in (
            ("pending", {"💡 Factory Pending"}),
            ("queued", {"📥 Factory Queued"}),
            ("implementing", {"🔧 Factory Implementing"}),
            ("implemented", {"✅ Factory Implemented"}),
            ("dismissed", {"🗑️ Factory Dismissed"}),
            (
                "all",
                {
                    "💡 Factory Pending",
                    "📥 Factory Queued",
                    "🔧 Factory Implementing",
                    "✅ Factory Implemented",
                    "🗑️ Factory Dismissed",
                },
            ),
        ):
            code, data = _http_json(f"{base_url}/api/ideas?status={st}")
            assert code == 200
            assert data["status"] == st
            assert {it["title"] for it in data["ideas"]} == expected_titles
            assert data["counts"] == expected_counts

        c_bad, _ = _http_json(f"{base_url}/api/ideas?status=invalido")
        assert c_bad == 400

        # 3. Default GET /api/projects/<slug>/ideas returns all ideas + full counts
        cp_def, rp_def = _http_json(f"{base_url}/api/projects/{pid}/ideas")
        assert cp_def == 200
        assert rp_def["ok"] is True
        assert len(rp_def["ideas"]) == 5
        assert rp_def["counts"] == expected_counts

        # 4. Filtered GET /api/projects/<slug>/ideas?status=...
        for st, expected_titles in (
            ("pending", {"💡 Proj Pending"}),
            ("queued", {"📥 Proj Queued"}),
            ("implementing", {"🔧 Proj Implementing"}),
            ("implemented", {"✅ Proj Implemented"}),
            ("dismissed", {"🗑️ Proj Dismissed"}),
            (
                "all",
                {
                    "💡 Proj Pending",
                    "📥 Proj Queued",
                    "🔧 Proj Implementing",
                    "✅ Proj Implemented",
                    "🗑️ Proj Dismissed",
                },
            ),
        ):
            code, data = _http_json(f"{base_url}/api/projects/{pid}/ideas?status={st}")
            assert code == 200
            assert data["status"] == st
            assert {it["title"] for it in data["ideas"]} == expected_titles
            assert data["counts"] == expected_counts

        cp_bad, _ = _http_json(f"{base_url}/api/projects/{pid}/ideas?status=invalido")
        assert cp_bad == 400
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_radar_filters_run_now_and_exclusive_accordion_ui(tmp_path: Path) -> None:
    """Radar exposes 4 status filters with counters (pending default), replaces queued disabled button with run-now + dismiss, enforces exclusive accordion, preserves all IDs, and uses zero external CDNs."""
    store = SqliteEventStore(str(tmp_path / "radar_step1_ui.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")

        # Zero external CDNs
        assert "cdn." not in html.lower()
        assert "https://" not in html.lower()

        # Existing HTML IDs preserved intact (including #liwi-radar-section and untouched #tab-ideas / #tab-ideas-btn)
        for preserved_id in (
            "liwi-radar-section",
            "liwi-radar-title",
            "liwi-radar-frequency",
            "liwi-radar-generate-btn",
            "liwi-radar-live-status",
            "liwi-radar-spinner",
            "liwi-radar-status-text",
            "liwi-radar-clock",
            "liwi-radar-grid",
            "liwi-radar-empty",
            "liwi-subtab-ideas-btn",
            "liwi-subtab-history-btn",
            "liwi-pilot-settings-btn",
            "liwi-pilot-settings-panel",
            "liwi-autonomous-history-panel",
            "liwi-visual-audit-btn",
            "tab-ideas",
            "tab-ideas-btn",
        ):
            assert f'id="{preserved_id}"' in html, f"Missing preserved ID: {preserved_id}"

        # Extract #liwi-radar-section HTML
        radar_start = html.index('<section id="liwi-radar-section"')
        radar_end = html.index("</section>", radar_start) + len("</section>")
        radar_html = html[radar_start:radar_end]

        # 3-lane filter buttons with counters inside #liwi-radar-section, with 'pending' selected by default
        assert 'id="liwi-radar-filters"' in radar_html
        for filter_key, label in (
            ("pending", "✨ Nuevas"),
            ("queued", "⏳ En cola"),
            ("implemented", "✅ Implementadas"),
        ):
            assert f'data-radar-filter="{filter_key}"' in radar_html
            assert label in radar_html
            assert f'data-radar-filter-count="{filter_key}"' in radar_html

        pending_btn_start = radar_html.index('data-radar-filter="pending"')
        pending_btn_tag = radar_html[radar_html.rfind("<button", 0, pending_btn_start) : radar_html.index(">", pending_btn_start) + 1]
        assert "active" in pending_btn_tag
        assert 'aria-selected="true"' in pending_btn_tag

        for other_key in ("queued", "implemented"):
            other_start = radar_html.index(f'data-radar-filter="{other_key}"')
            other_tag = radar_html[radar_html.rfind("<button", 0, other_start) : radar_html.index(">", other_start) + 1]
            assert 'aria-selected="false"' in other_tag

        # Responsive CSS rules: mobile adjustments strictly inside @media (max-width: 768px)
        style_start = html.index("<style>") + len("<style>")
        style_end = html.index("</style>", style_start)
        css = html[style_start:style_end]
        media_768 = _extract_css_block(css, "@media (max-width: 768px)")
        assert ".liwi-radar-filters" in css
        assert ".liwi-radar-filter-btn" in css
        assert ".liwi-radar-filters" in media_768
        assert ".liwi-radar-filter-btn" in media_768
        desktop_css = css.replace(media_768, "")
        desktop_grid = _extract_css_block(desktop_css, ".liwi-radar-grid {")
        assert "repeat(2, minmax(0, 1fr))" in desktop_grid

        # Inspect initLiwiIdeaRadar JS logic
        script_js = html[html.index("<script>") :]
        radar_js_start = script_js.index("(function initLiwiIdeaRadar()")
        radar_js_end = script_js.index("(function initAutonomousAndProjectIdeas()", radar_js_start)
        radar_js = script_js[radar_js_start:radar_js_end]

        # 1) 3-lane filter state (pending default), backend counts sync, active filter preserved
        assert "let activeFilter = 'pending';" in radar_js
        assert "counts.queued" in radar_js
        assert "counts.pending" in radar_js
        assert "counts.implemented" in radar_js
        assert "counts.failed" in radar_js
        assert "/api/ideas?status=" in radar_js

        # 2) Queued cards expose '▶️ Ejecutar ya' (/run-now) + 'Quitar' (/remove)
        assert "✓ Encolada en staging" not in radar_js
        assert "▶️ Ejecutar ya" in radar_js
        assert "data-radar-run-now" in radar_js
        assert "/run-now" in radar_js
        assert "data-radar-remove" in radar_js
        assert "/remove" in radar_js
        assert "actionInFlightIds" in radar_js
        assert "Error al ejecutar:" in radar_js

        # 3) Exclusive accordion inside #liwi-radar-section (single expanded card at a time)
        assert "expandedIdeaIds" not in radar_js
        assert "let expandedIdeaId = null;" in radar_js
        assert "gridEl.querySelectorAll('.liwi-radar-card').forEach((otherEl)" in radar_js
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_radar_ideas_run_now_endpoint_and_status_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/ideas/<id>/run-now enqueues queued idea immediately, updates status to implementando and counts, and guards invalid/conflicting states."""
    from agent_factory import idea_engine

    telemetry_db = tmp_path / "telemetry_run_now.db"
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(telemetry_db))

    store = SqliteEventStore(db_path=tmp_path / "dash_run_now.db")
    orch = Orchestrator(store=store, llm=FakeLLM())

    enqueued_calls: list[dict[str, Any]] = []

    class _StubStagingManager:
        def enqueue_improvement(
            self,
            *,
            title: str,
            prompt: str,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            enqueued_calls.append({"title": title, "prompt": prompt, "metadata": metadata or {}})
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": True,
                "queue_size": len(enqueued_calls),
                "last_error": None,
                "current_task": title,
                "initialized": True,
            }

        def get_staging_status(self) -> dict[str, Any]:
            return {
                "pending_count": 0,
                "commits": [],
                "is_building": bool(enqueued_calls),
                "queue_size": len(enqueued_calls),
                "last_error": None,
                "current_task": enqueued_calls[-1]["title"] if enqueued_calls else None,
                "initialized": True,
            }

        def shutdown(self, timeout: float = 5.0) -> None:
            _ = timeout

    saved = idea_engine.save_ideas(
        [
            {
                "title": "💡 Pending Idea",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Pending",
                "technical_prompt": "Prompt pending",
            },
            {
                "title": "📥 Queued Idea One",
                "category": "Rendimiento",
                "impact": "Alto",
                "simple_description": "Queued one",
                "technical_prompt": "Prompt queued one",
            },
            {
                "title": "📥 Queued Idea Two",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Queued two",
                "technical_prompt": "Prompt queued two",
            },
            {
                "title": "✅ Implemented Idea",
                "category": "Calidad",
                "impact": "Medio",
                "simple_description": "Implemented",
                "technical_prompt": "Prompt implemented",
            },
            {
                "title": "🚫 Dismissed Idea",
                "category": "Seguridad",
                "impact": "Medio",
                "simple_description": "Dismissed",
                "technical_prompt": "Prompt dismissed",
            },
        ],
        source="manual",
        db_path=telemetry_db,
    )
    by_title = {item["title"]: item["id"] for item in saved}
    queued_one_id = by_title["📥 Queued Idea One"]
    queued_two_id = by_title["📥 Queued Idea Two"]
    implemented_id = by_title["✅ Implemented Idea"]
    dismissed_id = by_title["🚫 Dismissed Idea"]

    idea_engine.update_idea_status(queued_one_id, "queued", db_path=telemetry_db)
    idea_engine.update_idea_status(queued_two_id, "queued", db_path=telemetry_db)
    idea_engine.update_idea_status(implemented_id, "implemented", db_path=telemetry_db)
    idea_engine.dismiss_idea(dismissed_id, db_path=telemetry_db)

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=_StubStagingManager(),  # type: ignore[arg-type]
    )
    try:
        # 1. Run-now on queued idea -> 200, enqueued with run_now=True, status='implementando', counts updated
        code, data = _http_json(f"{base_url}/api/ideas/{queued_one_id}/run-now", method="POST")
        assert code == 200
        assert data["ok"] is True
        assert data["idea"]["id"] == queued_one_id
        assert data["idea"]["status"] == "implementando"
        assert data["counts"] == {
            "pending": 1,
            "queued": 1,
            "implementing": 1,
            "implemented": 1,
            "dismissed": 1,
            "all": 5,
        }
        assert len(enqueued_calls) == 1
        assert enqueued_calls[0]["title"] == "📥 Queued Idea One"
        assert enqueued_calls[0]["prompt"] == "Prompt queued one"
        assert enqueued_calls[0]["metadata"]["idea_id"] == queued_one_id
        assert enqueued_calls[0]["metadata"]["run_now"] is True

        # 2. Conflict / Not Found / Bad Request guards
        dup_code, _ = _http_json(f"{base_url}/api/ideas/{queued_one_id}/run-now", method="POST")
        assert dup_code == 409
        impl_code, _ = _http_json(f"{base_url}/api/ideas/{implemented_id}/run-now", method="POST")
        assert impl_code == 409
        dis_code, _ = _http_json(f"{base_url}/api/ideas/{dismissed_id}/run-now", method="POST")
        assert dis_code == 409
        nf_code, _ = _http_json(f"{base_url}/api/ideas/999999/run-now", method="POST")
        assert nf_code == 404
        bad_code, _ = _http_json(f"{base_url}/api/ideas/not-an-int/run-now", method="POST")
        assert bad_code == 400

        # 3. Dismissing a queued idea updates counts cleanly (queued -> 0, dismissed -> 2)
        d_code, d_data = _http_json(f"{base_url}/api/ideas/{queued_two_id}/dismiss", method="POST")
        assert d_code == 200
        assert d_data["idea"]["status"] == "descartada"

        q_code, q_list = _http_json(f"{base_url}/api/ideas?status=queued")
        assert q_code == 200
        assert q_list["ideas"] == []
        assert q_list["counts"] == {
            "pending": 1,
            "queued": 0,
            "implementing": 1,
            "implemented": 1,
            "dismissed": 2,
            "all": 5,
        }
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_centro_automejora_three_lanes_and_queue_endpoints(tmp_path: Path) -> None:
    from agent_factory import idea_engine
    from agent_factory.dashboard import DASHBOARD_HTML, start_dashboard_server
    from agent_factory.orchestrator import Orchestrator

    # 1. 3-lane Centro de Auto-Mejora UI markers & applyTechMode polling isolation
    for marker in (
        "Centro de Auto-Mejora",
        'id="liwi-hero-active-card"',
        'id="liwi-queue-reconcile-btn"',
        'id="liwi-pilot-state-pill"',
        "✨ Nuevas",
        "⏳ En cola",
        "✅ Implementadas",
    ):
        assert marker in DASHBOARD_HTML
    tech_fn_start = DASHBOARD_HTML.index("function applyTechMode(")
    tech_fn_block = DASHBOARD_HTML[tech_fn_start : tech_fn_start + 900]
    assert "setInterval" not in tech_fn_block
    assert "clearInterval" not in tech_fn_block

    # 2. /api/ideas/<id>/remove and /api/ideas/queue-reconcile endpoints
    store = SqliteEventStore(str(tmp_path / "factory.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    telemetry_db = tmp_path / "telemetry.db"
    created = idea_engine.save_ideas(
        [
            {
                "title": "Mejora de cola",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Prueba de quitar de cola y reconciliar.",
                "technical_prompt": "Ajuste menor",
                "source": "manual",
            }
        ],
        db_path=telemetry_db,
    )
    idea_id = int(created[0]["id"])
    idea_engine.update_idea_status(idea_id, "encolada", db_path=telemetry_db)

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
    )
    server.controller._resolve_idea_db_path = lambda: telemetry_db  # type: ignore[method-assign]
    try:
        rm_code, rm_data = _http_json(f"{base_url}/api/ideas/{idea_id}/remove", method="POST")
        assert rm_code == 200
        assert rm_data["ok"] is True
        assert rm_data["idea"]["status"] == "nueva"
        assert rm_data["idea"]["queued_at"] is None

        rec_code, rec_data = _http_json(f"{base_url}/api/ideas/queue-reconcile", method="POST")
        assert rec_code == 200
        assert rec_data["ok"] is True
        assert "report" in rec_data
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_drain_queue_endpoint_and_autonomous_history_merge(tmp_path: Path) -> None:
    from agent_factory import idea_engine
    from agent_factory.dashboard import DASHBOARD_HTML, start_dashboard_server
    from agent_factory.orchestrator import Orchestrator

    assert 'id="liwi-drain-queue-btn"' in DASHBOARD_HTML
    assert "Procesar toda la cola" in DASHBOARD_HTML

    store = SqliteEventStore(str(tmp_path / "factory.db"))
    orch = Orchestrator(store=store, llm=FakeLLM())
    telemetry_db = tmp_path / "telemetry.db"
    idea_engine.set_autonomous_config(autonomous_enabled=False, db_path=telemetry_db)
    created = idea_engine.save_ideas(
        [
            {
                "title": "Cola Uno",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Primera mejora en cola.",
                "technical_prompt": "Prompt 1",
                "source": "manual",
            },
            {
                "title": "Cola Dos",
                "category": "Rendimiento",
                "impact": "Alto",
                "simple_description": "Segunda mejora en cola.",
                "technical_prompt": "Prompt 2",
                "source": "manual",
            },
        ],
        db_path=telemetry_db,
    )
    for item in created:
        idea_engine.update_idea_status(int(item["id"]), "encolada", db_path=telemetry_db)

    idea_engine.record_autonomous_application(
        title="Historial Previo Sin Idea",
        commit_hash="abc123456789",
        simple_description="Mejora aplicada previamente.",
        idea_id=None,
        db_path=telemetry_db,
    )

    enqueued_calls: list[dict[str, object]] = []

    class _FakeStaging:
        def __init__(self) -> None:
            self._queue: list[dict[str, object]] = []

        def enqueue_improvement(self, title: str, prompt: str, metadata: dict | None = None) -> dict:
            enqueued_calls.append({"title": title, "prompt": prompt, "metadata": metadata or {}})
            return {"is_building": True, "queue_size": len(enqueued_calls)}

        def get_staging_status(self) -> dict:
            return {"is_building": bool(enqueued_calls), "queue_size": len(enqueued_calls)}

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        staging_manager=_FakeStaging(),  # type: ignore[arg-type]
    )
    server.controller._resolve_idea_db_path = lambda: telemetry_db  # type: ignore[method-assign]
    try:
        d_code, d_data = _http_json(f"{base_url}/api/ideas/drain-queue", method="POST")
        assert d_code == 200
        assert d_data["ok"] is True
        assert d_data["enqueued_count"] == 2
        assert len(enqueued_calls) == 2
        assert idea_engine.get_autonomous_state(db_path=telemetry_db)["autonomous_enabled"] is False

        i_code, i_data = _http_json(f"{base_url}/api/ideas?status=implemented")
        assert i_code == 200
        titles = [it["title"] for it in i_data["ideas"]]
        assert "Historial Previo Sin Idea" in titles
        assert "staging_queue" in i_data
        assert i_data["staging_queue"]["active"] is True
        assert i_data["staging_queue"]["pending_count"] == 3

        st_code, st_data = _http_json(f"{base_url}/api/autonomous/status")
        assert st_code == 200
        assert st_data["staging_queue"]["active"] is True
        assert "⚙️ Procesando cola" in DASHBOARD_HTML
        assert "🔄 En staging" in DASHBOARD_HTML
        assert "⏳ Cola en ejecución en staging..." in DASHBOARD_HTML
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()




