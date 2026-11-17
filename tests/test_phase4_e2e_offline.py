"""End-to-end offline operational tests from CLI runtime assembly through Registry, PWA Panel bundle, and Telegram delivery (AC-4.6, INV-03, INV-16, INV-19, INV-21, INV-23)."""

import json
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.cli import (
    build_runtime,
    load_cli_config,
    main,
    run_polling_loop,
)
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import ProjectState
from agent_factory.ports.notifier import HttpResponse
from agent_factory.ports.runner import CommandResult


class _FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 11, 17, 10, 15, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class _ScriptedTelegramTransport:
    """Offline scripted HttpTransport answering getUpdates / sendMessage / answerCallbackQuery."""

    def __init__(self, update_batches: list[list[dict[str, Any]] | Exception]) -> None:
        self._batches = list(update_batches)
        self.sent_messages: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self._msg_id = 1

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
    ) -> HttpResponse:
        if url.endswith("/getUpdates"):
            if not self._batches:
                return HttpResponse(status_code=200, body=json.dumps({"ok": True, "result": []}))
            nxt = self._batches.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return HttpResponse(status_code=200, body=json.dumps({"ok": True, "result": nxt}))
        if url.endswith("/sendMessage"):
            self._msg_id += 1
            self.sent_messages.append(dict(payload))
            return HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "ok": True,
                        "result": {"message_id": self._msg_id, "chat": {"id": payload["chat_id"]}},
                    }
                ),
            )
        if url.endswith("/answerCallbackQuery"):
            self.answered_callbacks.append(dict(payload))
            return HttpResponse(status_code=200, body=json.dumps({"ok": True, "result": True}))
        return HttpResponse(status_code=404, body=json.dumps({"ok": False, "description": "Unknown method"}))


def _init_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    bare_repo = tmp_path / "origin.git"
    seed_repo = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(bare_repo), str(seed_repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(seed_repo), "checkout", "-b", "main"], check=True, capture_output=True, text=True)
    (seed_repo / "README.md").write_text("# Root\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed_repo),
            "-c",
            "user.name=Seed",
            "-c",
            "user.email=seed@local",
            "commit",
            "-m",
            "chore: init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "-C", str(seed_repo), "push", "-u", "origin", "main"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(bare_repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True, text=True)
    main_sha = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bare_repo, main_sha


def test_phase4_e2e_cli_runtime_to_registry_pwa_panel_and_telegram_notification(
    tmp_path: Path,
) -> None:
    """AC-4.6 positive (INV-03, INV-16, INV-19, INV-21, INV-23): cli config -> build_runtime -> run_polling_loop -> /new -> approve -> Agy + VCS + registry -> PWA panel bundle -> Telegram delivery."""
    bare_repo, main_sha_before = _init_bare_repo(tmp_path)
    tg_token = "TEST_E2E_P4_TG_TOK_112233"
    gh_token = "TEST_E2E_P4_GH_TOK_445566"
    db_path = tmp_path / "agent_factory.db"

    env = {
        "TELEGRAM_BOT_TOKEN": tg_token,
        "ALLOWED_CHAT_IDS": "1001",
        "ALLOWED_USER_IDS": "2001",
        "AGENT_FACTORY_DB_PATH": str(db_path),
        "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "workspaces"),
        "AGENT_FACTORY_REGISTRY_REPO": str(bare_repo),
        "AGENT_FACTORY_REGISTRY_BRANCH": "registry",
        "GITHUB_OWNER": "gabriel-test",
        "GITHUB_TOKEN": gh_token,
    }
    config = load_cli_config(env)

    plan = Plan(
        summary="Panel Clima Estático",
        rationale="ES module on static-web",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="Clima", contract="export {};", depends_on=[])],
        acceptance_tests=[
            AcceptanceTest(id="AT-01", description="Unit", type=AcceptanceTestType.UNIT),
            AcceptanceTest(id="AT-02", description="E2E", type=AcceptanceTestType.E2E),
        ],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "App Clima",
                    "users": ["Gabriel"],
                    "features": ["Pronóstico"],
                    "business_rules": ["Sin backend"],
                    "edge_cases": ["Offline"],
                    "constraints": ["Estático"],
                    "success_criteria": ["0 errores"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "Static ES", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Timeout"], "robust_proposal": "Fallback", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    cli_script = tmp_path / "sim_agy_p4.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "mode = args[args.index('--mode') + 1]\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
        "    (ws / 'tests').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'tests' / 'test_clima.py').write_text('def test_ok(): assert True\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_clima.py'], 'summary': 'Tests ok'}))\n"
        "else:\n"
        "    (ws / 'src').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'src' / 'app.js').write_text('export const ok = 1;\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'App ok'}))\n",
        encoding="utf-8",
    )

    class _HybridOfflineRunner:
        """Executes real git for registry bare repo / local script for agy, and fakes pytest + GitHub CLI checks offline."""

        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            if env is not None:
                assert "TELEGRAM_BOT_TOKEN" not in env
            cmd0 = Path(command[0]).name.lower()
            if cmd0.startswith("python"):
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)
            if command[0] == "pytest":
                return CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "AT-02": True})
            if command[0] == "git" and (
                "af_registry" in str(cwd)
                or "reg_work" in str(cwd)
                or any(str(bare_repo) in c for c in command)
                or "registry" in " ".join(command)
            ):
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)
            if command[0] == "gh" and ("run" in command or "workflow" in command or "status" in " ".join(command)):
                return CommandResult(exit_code=0, stdout=json.dumps({"passed": True, "summary": "CI passed", "failed_checks": []}))
            return CommandResult(exit_code=0, stdout="ok")

    hybrid_runner = _HybridOfflineRunner()
    transport = _ScriptedTelegramTransport(
        update_batches=[
            [
                {
                    "update_id": 10,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 2001, "username": "gabriel"},
                        "chat": {"id": 1001},
                        "text": "/new App Clima",
                    },
                }
            ],
            # Delay approve callback dynamically after plan_hash exists -> handled below
        ]
    )
    clock = _FakeClock()

    from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
    from agent_factory.workspace import WorkspaceManager

    ws_mgr = WorkspaceManager(base_dir=config.workspaces_dir)
    agy = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=hybrid_runner,
        cli_command=[sys.executable, str(cli_script)],
    )
    vcs = GitHubVCSAdapter(
        runner=hybrid_runner,
        owner=config.github_owner,
        github_token=gh_token,
        secrets=(gh_token,),
    )

    runtime = build_runtime(
        config,
        llm=llm,
        runner=hybrid_runner,
        transport=transport,
        vcs=vcs,
        code_agent=agy,
        clock=clock,
    )

    stop_event = threading.Event()
    # Poll 1: /new App Clima -> WAITING_APPROVAL with inline Aprobar button
    polls_1 = run_polling_loop(
        runtime.bot_service,
        clock=clock,
        stop_event=stop_event,
        max_iterations=1,
    )
    assert polls_1 == 1
    pid = "app-clima"
    status_waiting = runtime.orchestrator.get_status(pid)
    assert status_waiting.state == ProjectState.WAITING_APPROVAL
    assert status_waiting.plan_hash is not None

    # Enqueue approval callback using real generated plan_hash and poll 2
    transport._batches.append(
        [
            {
                "update_id": 11,
                "callback_query": {
                    "id": "cb-approve-clima",
                    "from": {"id": 2001, "username": "gabriel"},
                    "message": {"message_id": 2, "chat": {"id": 1001}},
                    "data": f"approve:{pid}:{status_waiting.plan_hash}",
                },
            }
        ]
    )
    polls_2 = run_polling_loop(
        runtime.bot_service,
        clock=clock,
        stop_event=stop_event,
        max_iterations=1,
    )
    assert polls_2 == 1

    final_status = runtime.orchestrator.get_status(pid)
    assert final_status.state == ProjectState.DELIVERED
    assert final_status.progress_pct == 100
    assert final_status.links.repo == "https://github.com/gabriel-test/app-clima"
    assert final_status.links.deploy == "https://gabriel-test.github.io/app-clima"

    # Final Telegram notification has links and never leaks tokens
    final_tg_text = transport.sent_messages[-1]["text"]
    assert "https://github.com/gabriel-test/app-clima" in final_tg_text
    assert "https://gabriel-test.github.io/app-clima" in final_tg_text
    assert tg_token not in final_tg_text
    assert gh_token not in final_tg_text

    # Export PWA Panel bundle from delivered status & event store -> consumed by panel/
    bundle_dir = StaticDeployPublisher(runner=hybrid_runner).export_panel_bundle(
        tmp_path / "panel_dist",
        statuses=[final_status],
        events_by_project={pid: runtime.store.get_events(pid)},
        panel_source_dir=Path(__file__).resolve().parent.parent / "panel",
    )
    assert (bundle_dir / "index.html").is_file()
    assert (bundle_dir / "manifest.webmanifest").is_file()
    panel_proj = json.loads((bundle_dir / "projects" / f"{pid}.json").read_text(encoding="utf-8"))
    assert panel_proj["state"] == "DELIVERED"
    assert panel_proj["progress_pct"] == 100
    assert len(panel_proj["events"]) >= 3

    reg_raw = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    reg_index = json.loads(reg_raw)
    assert reg_index["projects"][pid]["state"] == "DELIVERED"

    main_sha_after = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_sha_after == main_sha_before


def test_phase4_e2e_negative_invalid_config_and_transient_backoff(tmp_path: Path) -> None:
    """AC-4.6 negative (INV-19, INV-21, INV-23): Invalid CLI config fails cleanly without leaking tokens, and polling loop recovers from transient network failure via backoff."""
    tok = "TEST_NEG_E2E_TOK_999"
    exit_code = main([], env={"TELEGRAM_BOT_TOKEN": tok})
    assert exit_code != 0

    bare_repo, _ = _init_bare_repo(tmp_path)
    cfg = load_cli_config(
        {
            "TELEGRAM_BOT_TOKEN": tok,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "AGENT_FACTORY_DB_PATH": str(tmp_path / "neg.db"),
            "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "ws"),
            "AGENT_FACTORY_REGISTRY_REPO": str(bare_repo),
        }
    )
    clock = _FakeClock()
    transport = _ScriptedTelegramTransport(
        update_batches=[
            OSError(f"Connection reset contacting https://api.telegram.org/bot{tok}/getUpdates"),
            [],
        ]
    )
    runtime = build_runtime(
        cfg,
        llm=FakeLLM(),
        transport=transport,
        clock=clock,
    )
    stop_event = threading.Event()
    errors: list[str] = []
    iterations = run_polling_loop(
        runtime.bot_service,
        clock=clock,
        stop_event=stop_event,
        initial_backoff_seconds=1.5,
        max_backoff_seconds=6.0,
        max_iterations=2,
        on_error=lambda err, _delay: errors.append(str(err)),
    )
    assert iterations == 2
    assert clock.sleeps == [1.5]
    assert len(errors) == 1
    assert tok not in errors[0]


def test_phase4_e2e_bot_and_build_worker_stream_state_and_sanitized_events_to_registry(
    tmp_path: Path,
) -> None:
    """H42-04 / M42-12 (INV-11, INV-19, INV-21, INV-23): build_runtime runtime updates registry from TelegramBotService on intermediate transitions (/new, adjust) and persists BuildWorker sanitized events into projects/<id>.json."""
    bare_repo, _ = _init_bare_repo(tmp_path)
    tg_token = "TEST_E2E_H4204_TG_TOK_111"
    gh_token = "TEST_E2E_H4204_GH_TOK_222"
    llm_key = "TEST_E2E_H4204_LLM_KEY_333"

    models_toml = tmp_path / "models_e2e.toml"
    models_toml.write_text(
        "[models]\n"
        'fast = "e2e-fast-model"\n'
        'strong = "e2e-strong-model"\n',
        encoding="utf-8",
    )
    limits_toml = tmp_path / "limits_e2e.toml"
    limits_toml.write_text(
        "INTERVIEW = 6\nDEBATE = 3\nMEDIATION = 2\nBUILDING = 15\nci_rounds = 3\n",
        encoding="utf-8",
    )

    cfg = load_cli_config(
        {
            "TELEGRAM_BOT_TOKEN": tg_token,
            "ALLOWED_CHAT_IDS": "1001",
            "ALLOWED_USER_IDS": "2001",
            "LLM_API_KEY": llm_key,
            "AGENT_FACTORY_DB_PATH": str(tmp_path / "e2e_stream.db"),
            "AGENT_FACTORY_WORKSPACES_DIR": str(tmp_path / "workspaces"),
            "AGENT_FACTORY_REGISTRY_REPO": str(bare_repo),
            "AGENT_FACTORY_REGISTRY_BRANCH": "registry",
            "AGENT_FACTORY_MODELS_CONFIG": str(models_toml),
            "AGENT_FACTORY_LIMITS_CONFIG": str(limits_toml),
            "GITHUB_OWNER": "gabriel-test",
            "GITHUB_TOKEN": gh_token,
        }
    )

    plan = Plan(
        summary="Clima Viva",
        rationale="ES static",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="Unit", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            # Round 1: /new Clima Viva -> complete spec -> plan 1
            {
                "spec": {
                    "objective": "Clima",
                    "users": ["Gabriel"],
                    "features": ["Tiempo"],
                    "business_rules": ["Static"],
                    "edge_cases": ["Offline"],
                    "constraints": ["Zero cost"],
                    "success_criteria": ["0 errors"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "Static", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Timeout"], "robust_proposal": "Fallback", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
            # Round 2 (after adjust): interview -> debate -> plan 2
            {
                "spec": {
                    "objective": "Clima con alerta",
                    "users": ["Gabriel"],
                    "features": ["Tiempo", "Alerta"],
                    "business_rules": ["Static"],
                    "edge_cases": ["Offline"],
                    "constraints": ["Zero cost"],
                    "success_criteria": ["0 errors"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "Static v2", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Timeout"], "robust_proposal": "Fallback v2", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    cli_script = tmp_path / "sim_agy_stream.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "mode = args[args.index('--mode') + 1]\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
        "    (ws / 'tests').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'tests' / 'test_ok.py').write_text('def test_ok(): assert True\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_ok.py'], 'summary': 'Tests ok'}))\n"
        "else:\n"
        "    (ws / 'src').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'src' / 'app.js').write_text('export const ok = 1;\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'App ok'}))\n",
        encoding="utf-8",
    )

    class _StreamRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            cmd0 = Path(command[0]).name.lower()
            if cmd0.startswith("python"):
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)
            if command[0] == "pytest":
                return CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})
            if command[0] == "git" and (
                "af_registry" in str(cwd)
                or any(str(bare_repo) in c for c in command)
                or "registry" in " ".join(command)
            ):
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)
            if command[0] == "gh" and ("run" in command or "workflow" in command):
                return CommandResult(exit_code=0, stdout=json.dumps([{"status": "completed", "conclusion": "success"}]))
            return CommandResult(exit_code=0, stdout="ok")

    hybrid_runner = _StreamRunner()
    transport = _ScriptedTelegramTransport(
        update_batches=[
            [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 2001, "username": "gabriel"},
                        "chat": {"id": 1001},
                        "text": "/new Clima Viva",
                    },
                }
            ]
        ]
    )
    from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
    from agent_factory.workspace import WorkspaceManager

    ws_mgr = WorkspaceManager(base_dir=cfg.workspaces_dir)
    agy = AgyCliAdapter(workspace_manager=ws_mgr, runner=hybrid_runner, cli_command=[sys.executable, str(cli_script)])
    clock = _FakeClock()
    rt = build_runtime(cfg, llm=llm, runner=hybrid_runner, transport=transport, code_agent=agy, clock=clock)
    try:
        stop_ev = threading.Event()
        # Poll 1: /new -> WAITING_APPROVAL (MUST already be published to registry by TelegramBotService, kills M42-12)
        run_polling_loop(rt.bot_service, clock=clock, stop_event=stop_ev, max_iterations=1)
        pid = "clima-viva"
        proj_waiting_raw = subprocess.run(
            ["git", "-C", str(bare_repo), "show", f"refs/heads/registry:projects/{pid}.json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        proj_waiting = json.loads(proj_waiting_raw)
        assert proj_waiting["state"] == "WAITING_APPROVAL"
        assert proj_waiting["progress_pct"] == 35
        first_hash = proj_waiting["plan_hash"]
        assert first_hash is not None

        # Poll 2: adjust callback -> returns to INTERVIEW and updates registry immediately
        transport._batches.append(
            [
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb-adj",
                        "from": {"id": 2001, "username": "gabriel"},
                        "message": {"message_id": 2, "chat": {"id": 1001}},
                        "data": f"adjust:{pid}:{first_hash}",
                    },
                }
            ]
        )
        run_polling_loop(rt.bot_service, clock=clock, stop_event=stop_ev, max_iterations=1)
        proj_adj = json.loads(
            subprocess.run(
                ["git", "-C", str(bare_repo), "show", f"refs/heads/registry:projects/{pid}.json"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        assert proj_adj["state"] == "INTERVIEW"
        assert proj_adj["progress_pct"] == 10

        # Poll 3: answer -> WAITING_APPROVAL v2, Poll 4: approve -> DELIVERED with full events array
        transport._batches.append(
            [
                {
                    "update_id": 3,
                    "message": {
                        "message_id": 3,
                        "from": {"id": 2001, "username": "gabriel"},
                        "chat": {"id": 1001},
                        "text": "Agrega alerta offline",
                    },
                }
            ]
        )
        run_polling_loop(rt.bot_service, clock=clock, stop_event=stop_ev, max_iterations=1)
        new_hash = rt.orchestrator.get_status(pid).plan_hash
        assert new_hash is not None

        transport._batches.append(
            [
                {
                    "update_id": 4,
                    "callback_query": {
                        "id": "cb-ok",
                        "from": {"id": 2001, "username": "gabriel"},
                        "message": {"message_id": 4, "chat": {"id": 1001}},
                        "data": f"approve:{pid}:{new_hash}",
                    },
                }
            ]
        )
        run_polling_loop(rt.bot_service, clock=clock, stop_event=stop_ev, max_iterations=1)

        proj_delivered_raw = subprocess.run(
            ["git", "-C", str(bare_repo), "show", f"refs/heads/registry:projects/{pid}.json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        proj_delivered = json.loads(proj_delivered_raw)
        assert proj_delivered["state"] == "DELIVERED"
        assert proj_delivered["progress_pct"] == 100
        assert isinstance(proj_delivered.get("events"), list)
        event_types = [ev["type"] for ev in proj_delivered["events"]]
        assert "PROJECT_CREATED" in event_types
        assert "APPROVAL_RECORDED" in event_types
        assert "PROJECT_DELIVERED" in event_types
        assert tg_token not in proj_delivered_raw
        assert gh_token not in proj_delivered_raw
        assert llm_key not in proj_delivered_raw
    finally:
        rt.store.close()
