"""Integration tests for the local loopback HTTP dashboard and CLI integration (INV-03, INV-07, INV-19, INV-22)."""

import http.client
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory import cli
from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.fake_vcs import FakeVCS
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
from agent_factory.workspace import WorkspaceManager


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
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, method=method)
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
        assert "Agent Factory" in html
        assert "Aprobar y Construir" in html
        assert "Reintentar" in html
        assert "Eliminar" in html
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
        assert by_id["proj-done"]["state"] == "DELIVERED"
        assert by_id["proj-done"]["links"]["repo"] == "https://github.com/gabriel-test/proj-done"
        assert by_id["proj-failed-debate"]["state"] == "FAILED"
        assert by_id["proj-failed-debate"]["last_error"] is not None
        assert secret not in json.dumps(list_data)

        # 3. GET /api/projects/<id> -> detail with status, spec, plan, events
        d_code, detail = _http_json(f"{base_url}/api/projects/proj-wait")
        assert d_code == 200
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
