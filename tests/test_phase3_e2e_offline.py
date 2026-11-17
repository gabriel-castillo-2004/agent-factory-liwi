"""End-to-end offline Phase 3 integration tests: Telegram -> Interview -> Debate -> Scaffold -> BuildWorker(AgyCliAdapter) -> GitRegistryPublisher -> Telegram delivery (AC-3.6, INV-03, INV-13, INV-19, INV-21, INV-23)."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker, LocalJobQueue
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.notifier import TelegramUpdate
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.scaffold import DEFAULT_TEMPLATES_ROOT, TemplateScaffold
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService
from agent_factory.workspace import WorkspaceManager


class _FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 16, 22, 30, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        return None


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
            "user.email=seed@example.local",
            "commit",
            "-m",
            "chore: init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "-C", str(seed_repo), "push", "-u", "origin", "main"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(bare_repo), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    )
    main_sha = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bare_repo, main_sha


def _sample_countdown_plan() -> dict[str, Any]:
    plan = Plan(
        summary="Temporizador visual estático",
        rationale="Módulo ES sin dependencias externas sobre static-web",
        template_id="static-web",
        files=[
            FileSpec(
                path="src/app.js",
                purpose="Temporizador regresivo",
                contract="export function formatSeconds(s) { return String(s); }",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT-01",
                description="formatSeconds(5) devuelve '5'",
                type=AcceptanceTestType.UNIT,
            ),
            AcceptanceTest(
                id="AT-02",
                description="Carga index.html sin console.error ni pageerror",
                type=AcceptanceTestType.E2E,
            ),
        ],
        risks=["None"],
        deployment_target="github-pages",
    )
    return plan.model_dump(mode="json")


def test_end_to_end_offline_telegram_interview_build_registry_and_final_notification(
    tmp_path: Path,
) -> None:
    """AC-3.6 positive (INV-03, INV-13, INV-20, INV-21, INV-23): /new -> Q&A -> debate -> Aprobar -> TemplateScaffold + BuildWorker(AgyCliAdapter) -> registry -> Telegram links."""
    bare_repo, main_sha_before = _init_bare_repo(tmp_path)
    store = MemoryEventStore()
    clock = _FakeClock()

    llm = FakeLLM(
        responses=[
            # Round 1: 1 clarifying question
            {
                "spec": {
                    "objective": "Temporizador visual",
                    "users": [],
                    "features": ["Cuenta regresiva"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["¿Quién usará el temporizador y qué duración máxima necesita?"],
            },
            # Round 2: spec complete
            {
                "spec": {
                    "objective": "Temporizador visual",
                    "users": ["Gabriel"],
                    "features": ["Cuenta regresiva"],
                    "business_rules": ["Máximo 60 minutos"],
                    "edge_cases": ["Valor cero"],
                    "constraints": ["100% estático"],
                    "success_criteria": ["Zero errores de consola"],
                    "assumptions": [],
                },
                "questions": [],
            },
            # Debate 3 calls
            {"approach": "ES module timer", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["Negative input"], "robust_proposal": "Clamp 0", "architectural_safeguards": []},
            _sample_countdown_plan(),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, clock=clock)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    cli_script = tmp_path / "sim_agy_e2e_p3.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "mode = args[args.index('--mode') + 1]\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
        "    tdir = ws / 'tests'\n"
        "    tdir.mkdir(parents=True, exist_ok=True)\n"
        "    (tdir / 'test_timer.py').write_text('def test_ok(): assert True\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_timer.py'], 'summary': 'Tests ok'}))\n"
        "else:\n"
        "    sdir = ws / 'src'\n"
        "    sdir.mkdir(parents=True, exist_ok=True)\n"
        "    (sdir / 'app.js').write_text('export function formatSeconds(s) { return String(s); }\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'Timer ready'}))\n",
        encoding="utf-8",
    )
    code_agent = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(cli_script)],
    )
    test_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            )
        ]
    )
    vcs = FakeVCS(
        ci_results=[CIRunResult(passed=True, summary="CI green")],
        repo_base_url="https://github.com/gabriel-test",
        deploy_base_url="https://gabriel-test.github.io",
    )
    reg_publisher = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=SubprocessRunner(default_timeout_seconds=15.0),
        work_dir=tmp_path / "reg_work",
    )
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=code_agent,
        runner=test_runner,
        vcs=vcs,
        queue=LocalJobQueue(),
        clock=clock,
        registry_publisher=reg_publisher,
    )

    client = FakeTelegramClient(
        updates=[
            # Unauthorized user first -> ignored
            TelegramUpdate(update_id=1, chat_id=999, user_id=999, text="/new Intruder"),
            # Authorized /new
            TelegramUpdate(update_id=2, chat_id=1001, user_id=2001, text="/new Temporizador visual"),
        ]
    )
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        build_worker=worker,
        template_scaffold=scaffold,
        registry_publisher=reg_publisher,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
        ),
    )

    poll_1 = bot.poll_once()
    assert len(poll_1) == 2
    assert poll_1[0].accepted is False
    assert poll_1[1].accepted is True
    pid = poll_1[1].project_id
    assert pid == "temporizador-visual"
    assert orch.get_status(pid).state == ProjectState.INTERVIEW

    # Reply answers clarifying question -> debate -> plan sent with Aprobar button
    bot.handle_update(
        TelegramUpdate(
            update_id=3,
            chat_id=1001,
            user_id=2001,
            text="Gabriel, máximo 60 minutos, 100% estático",
        )
    )
    waiting_status = orch.get_status(pid)
    assert waiting_status.state == ProjectState.WAITING_APPROVAL
    assert waiting_status.plan_hash is not None

    # Click Aprobar -> full scaffold + AgyCliAdapter + BuildWorker + registry push + final Telegram link message
    approve_outcome = bot.handle_update(
        TelegramUpdate(
            update_id=4,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-final-1",
            callback_data=f"approve:{pid}:{waiting_status.plan_hash}",
        )
    )
    assert approve_outcome.accepted is True

    final_status = orch.get_status(pid)
    assert final_status.state == ProjectState.DELIVERED
    assert final_status.progress_pct == 100
    assert final_status.links.repo == "https://github.com/gabriel-test/temporizador-visual"
    assert final_status.links.deploy == "https://gabriel-test.github.io/temporizador-visual"

    # Final Telegram notification includes repo and deploy URLs
    final_msg = client.sent_messages[-1].text
    assert "https://github.com/gabriel-test/temporizador-visual" in final_msg
    assert "https://gabriel-test.github.io/temporizador-visual" in final_msg

    # Verify registry branch updated and main untouched
    main_sha_after = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_sha_after == main_sha_before

    raw_index = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    index_data = json.loads(raw_index)
    assert index_data["projects"][pid]["state"] == "DELIVERED"
    assert (
        index_data["projects"][pid]["deploy_url"]
        == "https://gabriel-test.github.io/temporizador-visual"
    )

    # /status reports delivered links
    st_outcome = bot.handle_update(
        TelegramUpdate(update_id=5, chat_id=1001, user_id=2001, text=f"/status {pid}")
    )
    assert "DELIVERED" in st_outcome.reply_text
    assert "100%" in st_outcome.reply_text
    assert "https://gabriel-test.github.io/temporizador-visual" in st_outcome.reply_text


def test_end_to_end_offline_telegram_build_handles_vcs_failure_without_leaking_secrets(
    tmp_path: Path,
) -> None:
    """AC-3.6 negative (INV-19, INV-23): When delivery VCS fails during E2E flow with embedded tokens, project moves to FAILED cleanly and never leaks secrets to Telegram or registry."""
    bare_repo, _ = _init_bare_repo(tmp_path)
    secret_tok = "TEST_E2E_GH_SECRET_4455"
    store = MemoryEventStore()
    clock = _FakeClock()

    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "App fallida",
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
            _sample_countdown_plan(),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, clock=clock)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    class _ExplodingVCS(FakeVCS):
        def merge_and_deploy(self, project_id: str, branch: str = "work") -> tuple[str, str]:
            raise RuntimeError(f"GitHub remote error using GITHUB_TOKEN={secret_tok}")

    reg_publisher = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=SubprocessRunner(default_timeout_seconds=15.0),
        work_dir=tmp_path / "reg_work",
        secrets=(secret_tok,),
    )
    cli_script = tmp_path / "sim_agy_e2e_fail.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "mode = args[args.index('--mode') + 1]\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
        "    (ws / 'tests').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'tests' / 'test_t.py').write_text('def test_ok(): assert True\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_t.py'], 'summary': 'ok'}))\n"
        "else:\n"
        "    (ws / 'src').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'src' / 'app.js').write_text('export const a = 1;\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'ok'}))\n",
        encoding="utf-8",
    )
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=AgyCliAdapter(
            workspace_manager=ws_mgr,
            runner=SubprocessRunner(default_timeout_seconds=10.0),
            cli_command=[sys.executable, str(cli_script)],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "AT-02": True})]
        ),
        vcs=_ExplodingVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        queue=LocalJobQueue(),
        clock=clock,
        registry_publisher=reg_publisher,
        secrets_to_redact=(secret_tok,),
    )
    client = FakeTelegramClient()
    bot = TelegramBotService(
        client=client,
        orchestrator=orch,
        build_worker=worker,
        template_scaffold=scaffold,
        registry_publisher=reg_publisher,
        config=TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
            secrets_to_redact=(secret_tok,),
        ),
    )

    new_out = bot.handle_update(
        TelegramUpdate(update_id=10, chat_id=1001, user_id=2001, text="/new App fallida")
    )
    pid = new_out.project_id
    assert pid is not None
    plan_hash = orch.get_status(pid).plan_hash
    assert plan_hash is not None

    fail_out = bot.handle_update(
        TelegramUpdate(
            update_id=11,
            chat_id=1001,
            user_id=2001,
            callback_query_id="cb-fail",
            callback_data=f"approve:{pid}:{plan_hash}",
        )
    )
    assert orch.get_status(pid).state == ProjectState.FAILED
    assert secret_tok not in fail_out.reply_text
    assert secret_tok not in client.sent_messages[-1].text
    assert secret_tok not in "\n".join(e.model_dump_json() for e in store.get_events(pid))

    # Registry branch was not created/populated for a failed build
    refs_out = subprocess.run(
        ["git", "-C", str(bare_repo), "show-ref"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "refs/heads/registry" not in refs_out
