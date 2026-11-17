"""Tests closing remaining AUDIT_REPORT_3_1.md findings (AC-4.5, INV-07, INV-16, INV-19, INV-21, INV-23)."""

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker, JobState, LocalJobQueue
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    FileSpec,
    Plan,
    ProjectLinks,
    ProjectStatus,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.llm import LLMError
from agent_factory.ports.notifier import (
    InlineButton,
    SentMessage,
    TelegramTransportError,
    TelegramUpdate,
)
from agent_factory.ports.publisher import RegistryPublishError
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService
from agent_factory.workspace import WorkspaceManager


def _init_bare_repo(tmp_path: Path) -> Path:
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
    return bare_repo


def test_git_registry_publisher_recovers_from_divergent_rebase_conflict_without_wedging(
    tmp_path: Path,
) -> None:
    """AC-4.5 (H31-02, INV-21, INV-23): Divergent commits touching registry.json abort failed rebase, reset to FETCH_HEAD, preserve index entries, and leave work_dir unwedged."""
    bare_repo = _init_bare_repo(tmp_path)
    runner = SubprocessRunner(default_timeout_seconds=15.0)
    ts = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)

    pub1 = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=tmp_path / "wt1",
    )
    pub2 = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=tmp_path / "wt2",
    )

    pub1.publish_project(
        ProjectStatus(project_id="proj-one", name="One", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    pub2.publish_project(
        ProjectStatus(project_id="proj-two", name="Two", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    # Create unpushed local commit on wt1 that touches registry.json (diverges from pub2 on bare_repo)
    pub1_offline = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=False,
        runner=runner,
        work_dir=tmp_path / "wt1",
    )
    pub1_offline.publish_project(
        ProjectStatus(project_id="proj-three", name="Three", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    # Publishing online from wt1 hits divergent registry.json -> MUST recover automatically and succeed
    pub1.publish_project(
        ProjectStatus(project_id="proj-three", name="Three", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    pub1.publish_project(
        ProjectStatus(project_id="proj-four", name="Four", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    raw_index = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert sorted(json.loads(raw_index)["projects"].keys()) == [
        "proj-four",
        "proj-one",
        "proj-three",
        "proj-two",
    ]


def test_telegram_bot_new_failure_preserves_previous_active_project_and_command_boundaries(
    tmp_path: Path,
) -> None:
    """AC-4.5 (H31-03, INV-16, INV-23): Failed /new does not overwrite previous active chat project in RAM/SQLite, and /newsletter is not parsed as /new."""
    db_path = tmp_path / "bot_preserve.db"
    plan = Plan(
        summary="Good App Plan",
        rationale="Static",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="OK", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Good App",
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
            LLMError("Interviewer failure on broken app"),
        ]
    )

    store1 = SqliteEventStore(db_path=db_path)
    cfg = TelegramBotConfig(allowed_chat_ids=frozenset({1001}), allowed_user_ids=frozenset({2001}))
    bot1 = TelegramBotService(
        client=FakeTelegramClient(),
        orchestrator=Orchestrator(store=store1, llm=llm),
        config=cfg,
    )

    good_out = bot1.handle_update(
        TelegramUpdate(update_id=1, chat_id=1001, user_id=2001, text="/new Good App")
    )
    assert good_out.project_id == "good-app"
    assert bot1.active_project_by_chat[1001] == "good-app"
    assert store1.get_telegram_active_projects()[1001] == "good-app"

    # Negative prefix boundary: /newsletter MUST NOT enter _handle_new
    newsletter_out = bot1.handle_update(
        TelegramUpdate(update_id=2, chat_id=1001, user_id=2001, text="/newsletter should not trigger new")
    )
    assert newsletter_out.command != "/new"
    assert store1.list_project_ids() == ["good-app"]

    # Failed /new MUST NOT overwrite active project ("good-app") in RAM or SQLite
    fail_out = bot1.handle_update(
        TelegramUpdate(update_id=3, chat_id=1001, user_id=2001, text="/new Broken App")
    )
    assert fail_out.accepted is True
    assert bot1.active_project_by_chat[1001] == "good-app"
    assert store1.get_telegram_active_projects()[1001] == "good-app"

    st_same = bot1.handle_update(
        TelegramUpdate(update_id=4, chat_id=1001, user_id=2001, text="/status")
    )
    assert st_same.project_id == "good-app"
    assert "WAITING_APPROVAL" in st_same.reply_text
    store1.close()

    # After restart, active project in SQLite is still "good-app"
    store2 = SqliteEventStore(db_path=db_path)
    try:
        bot2 = TelegramBotService(
            client=FakeTelegramClient(),
            orchestrator=Orchestrator(store=store2, llm=llm),
            config=cfg,
        )
        st_restart = bot2.handle_update(
            TelegramUpdate(update_id=5, chat_id=1001, user_id=2001, text="/status")
        )
        assert st_restart.project_id == "good-app"
        assert "WAITING_APPROVAL" in st_restart.reply_text
    finally:
        store2.close()


def test_redact_secrets_scrubs_sensitive_query_params_without_env_or_extra_secrets() -> None:
    """AC-4.5 (H31-05, INV-07, INV-19, INV-23): Scrubs ?token=, &api_key=, ?access_token=, ?secret=, &password= with env={} and extra_secrets=() while preserving public URLs."""
    raw = (
        "Callback https://example.com/hook?token=SECRET_Q_123&mode=view failed; "
        "alt https://api.service.test/v1?foo=1&api_key=KEY_ABC_999&access_token=ACC_XYZ_111#frag; "
        "auth https://svc.local/login?secret=SEC_1&password=PASS_2"
    )
    cleaned = redact_secrets(raw, extra_secrets=(), env={})
    for leaked in ("SECRET_Q_123", "KEY_ABC_999", "ACC_XYZ_111", "SEC_1", "PASS_2"):
        assert leaked not in cleaned
    assert "mode=view" in cleaned
    assert "foo=1" in cleaned
    assert "#frag" in cleaned
    assert "[REDACTED]" in cleaned

    public_url = "https://example.com:8080/users/@gabriel?tab=1&page=2"
    assert redact_secrets(public_url, extra_secrets=(), env={}) == public_url


def test_git_registry_publisher_preserves_distinct_offline_projects_and_scrubs_error_context(
    tmp_path: Path,
) -> None:
    """H4-06 / H4-10 / M4-15 (INV-07, INV-19, INV-21, INV-23): Rebase recovery preserves other offline projects even when publishing a different project, handles malformed projects list, and clears __context__."""
    bare_repo = _init_bare_repo(tmp_path)
    runner = SubprocessRunner(default_timeout_seconds=15.0)
    ts = datetime(2026, 11, 17, 10, 30, tzinfo=UTC)

    pub1 = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=tmp_path / "wt1",
    )
    pub2 = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=tmp_path / "wt2",
    )

    pub1.publish_project(
        ProjectStatus(project_id="proj-one", name="One", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    pub2.publish_project(
        ProjectStatus(project_id="proj-two", name="Two", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    # wt1 commits proj-three offline (not pushed); wt2 pushes proj-five creating divergence on registry.json
    pub1_offline = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=False,
        runner=runner,
        work_dir=tmp_path / "wt1",
    )
    pub1_offline.publish_project(
        ProjectStatus(project_id="proj-three", name="Three", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    pub2.publish_project(
        ProjectStatus(project_id="proj-five", name="Five", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    # wt1 now publishes DIFFERENT project (proj-four): MUST preserve offline proj-three too (kills M4-15)
    pub1.publish_project(
        ProjectStatus(project_id="proj-four", name="Four", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    raw_index = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert sorted(json.loads(raw_index)["projects"].keys()) == [
        "proj-five",
        "proj-four",
        "proj-one",
        "proj-three",
        "proj-two",
    ]
    proj3_raw = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:projects/proj-three.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert json.loads(proj3_raw)["project_id"] == "proj-three"

    # H4-06: RegistryPublishError must clear __cause__ and __context__ so raw secrets cannot escape
    sec_tok = "TEST_REG_CTX_SECRET_777"
    err_pub = GitRegistryPublisher(
        repo_path=bare_repo,
        runner=FakeRunner(results=[OSError(f"git transport failed {sec_tok}")]),
        secrets=(sec_tok,),
    )
    with pytest.raises(RegistryPublishError) as reg_err:
        err_pub._run_git(["status"], cwd=tmp_path)
    assert sec_tok not in str(reg_err.value)
    assert reg_err.value.__cause__ is None
    assert reg_err.value.__context__ is None


def test_telegram_bot_resume_rejected_cleanly_and_poll_once_isolates_reply_failures(
    tmp_path: Path,
) -> None:
    """H4-11 / M4-21a (INV-04, INV-16, INV-19, INV-23): /resume on COMPLETED job rejects cleanly without invoking BuildWorker (kills M19/M4-21a), and poll_once isolates send_message errors."""
    plan = Plan(
        summary="Delivered App Plan",
        rationale="Static",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="OK", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Delivered App",
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
            {"approach": "Static ES", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )

    store = SqliteEventStore(db_path=tmp_path / "bot_h411.db")
    try:
        orch = Orchestrator(store=store, llm=llm)
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
                impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
            ),
            runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
            queue=LocalJobQueue(),
        )
        cfg = TelegramBotConfig(
            allowed_chat_ids=frozenset({1001}),
            allowed_user_ids=frozenset({2001}),
            secrets_to_redact=("SECRET_REPLY_ERR_999",),
        )
        client = FakeTelegramClient()
        bot = TelegramBotService(client=client, orchestrator=orch, config=cfg, build_worker=worker)

        bot.handle_update(TelegramUpdate(update_id=1, chat_id=1001, user_id=2001, text="/new Delivered App"))
        pid = "delivered-app"
        status_wait = orch.get_status(pid)
        assert status_wait.state == ProjectState.WAITING_APPROVAL
        bot.handle_update(
            TelegramUpdate(
                update_id=2,
                chat_id=1001,
                user_id=2001,
                callback_query_id="cb-deliv",
                callback_data=f"approve:{pid}:{status_wait.plan_hash}",
            )
        )
        assert orch.get_status(pid).state == ProjectState.DELIVERED
        assert worker.queue.get_job(pid) is not None
        assert worker.queue.get_job(pid).state == JobState.COMPLETED

        # Direct handle_update + poll_once on COMPLETED job MUST return action == "resume_rejected" without touching BuildWorker (kills M4-21a / M19)
        direct_out = bot.handle_update(
            TelegramUpdate(update_id=3, chat_id=1001, user_id=2001, text=f"/resume {pid}")
        )
        assert direct_out.action == "resume_rejected"

        client.enqueue_update(
            TelegramUpdate(update_id=4, chat_id=1001, user_id=2001, text=f"/resume {pid}")
        )
        batch_out = bot.poll_once()
        assert len(batch_out) == 1
        assert batch_out[0].action == "resume_rejected"
        assert worker.queue.get_job(pid).state == JobState.COMPLETED
        assert orch.get_status(pid).state == ProjectState.DELIVERED

        # H4-11 (H31-04): poll_once must isolate send_message failures inside an update batch
        class _FlakyReplyTelegramClient:
            def __init__(self) -> None:
                self.send_attempts = 0
                self.sent_texts: list[str] = []

            def get_updates(
                self, offset: int | None = None, timeout: int = 30
            ) -> list[TelegramUpdate]:
                return [
                    TelegramUpdate(update_id=30, chat_id=1001, user_id=2001, text="/status nonexistent-app"),
                    TelegramUpdate(update_id=31, chat_id=1001, user_id=2001, text=f"/status {pid}"),
                ]

            def send_message(
                self,
                chat_id: int,
                text: str,
                buttons: Sequence[Sequence[InlineButton]] | None = None,
            ) -> SentMessage:
                self.send_attempts += 1
                if "DELIVERED" not in text:
                    raise TelegramTransportError(
                        "Connection reset contacting https://api.telegram.org/bot123456:SECRET_REPLY_ERR_999/sendMessage"
                    )
                self.sent_texts.append(text)
                return SentMessage(message_id=self.send_attempts, chat_id=chat_id, text=text)

            def answer_callback_query(
                self, callback_query_id: str, text: str | None = None
            ) -> None:
                return None

        flaky_client = _FlakyReplyTelegramClient()
        flaky_bot = TelegramBotService(
            client=flaky_client,
            orchestrator=orch,
            config=cfg,
            build_worker=worker,
        )
        outcomes = flaky_bot.poll_once()
        assert len(outcomes) == 2
        assert outcomes[0].update_id == 30
        assert outcomes[0].action == "error"
        assert "SECRET_REPLY_ERR_999" not in outcomes[0].reply_text
        assert outcomes[1].update_id == 31
        assert outcomes[1].action == "status_reported"
        assert "DELIVERED" in outcomes[1].reply_text
    finally:
        store.close()


def test_git_registry_publisher_scrubs_secrets_in_registry_index_project_json_and_events(
    tmp_path: Path,
) -> None:
    """H4-10 (INV-07, INV-19, INV-21, INV-23, ADR-004): publish_project and publish redact secrets across registry.json and projects/<id>.json including passed events."""
    bare_repo = _init_bare_repo(tmp_path)
    runner = SubprocessRunner(default_timeout_seconds=15.0)
    ts = datetime(2026, 11, 17, 11, 0, tzinfo=UTC)

    sec_custom = "CUSTOM_REG_SECRET_111"
    url_tok = "URL_REG_TOKEN_222"
    repo_tok = "REPO_REG_API_KEY_333"
    dep_tok = "DEPLOY_REG_ACC_444"
    bear_tok = "BEARER_REG_555"
    ev_tok = "EVENT_REG_SECRET_666"
    pub_tok = "PUBLISH_ALIAS_SECRET_777"

    pub = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=tmp_path / "wt_reg_scrub",
        secrets=(sec_custom, ev_tok, pub_tok),
    )

    status_1 = ProjectStatus(
        project_id="sec-reg-one",
        name=f"App {sec_custom}",
        objective=f"https://example.com/app?token={url_tok}",
        state=ProjectState.FAILED,
        progress_pct=35,
        failure_reason=f"Denied Authorization: Bearer {bear_tok}",
        links=ProjectLinks(
            repo=f"https://github.com/o/r?api_key={repo_tok}",
            deploy=f"https://o.github.io/app?access_token={dep_tok}",
        ),
        updated_at=ts,
    )
    events_1 = [
        Event(
            id="evt-reg-1",
            project_id="sec-reg-one",
            type=EventType.PROJECT_CREATED,
            payload={"name": f"Name {sec_custom}"},
            at=ts,
        ),
        Event(
            id="evt-reg-2",
            project_id="sec-reg-one",
            type=EventType.PROJECT_FAILED,
            payload={"reason": f"Failure {ev_tok}"},
            at=ts,
        ),
    ]

    pub.publish_project(status_1, events=events_1)

    status_2 = ProjectStatus(
        project_id="sec-reg-two",
        name=f"Alias {pub_tok}",
        objective=f"https://svc.local?secret={pub_tok}",
        state=ProjectState.DELIVERED,
        progress_pct=100,
        links=ProjectLinks(
            repo=f"https://github.com/o/r2?password={pub_tok}",
            deploy=f"https://o.github.io/r2?auth={pub_tok}",
        ),
        updated_at=ts,
    )
    pub.publish(status_2)

    reg_raw = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    proj1_raw = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:projects/sec-reg-one.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    proj2_raw = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:projects/sec-reg-two.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    for leaked in (sec_custom, url_tok, repo_tok, dep_tok, bear_tok, ev_tok, pub_tok):
        assert leaked not in reg_raw
        assert leaked not in proj1_raw
        assert leaked not in proj2_raw

    assert "[REDACTED]" in reg_raw
    assert "[REDACTED]" in proj1_raw
    assert "[REDACTED]" in proj2_raw

    proj1_data = json.loads(proj1_raw)
    assert isinstance(proj1_data.get("events"), list)
    assert len(proj1_data["events"]) == 2
    assert proj1_data["events"][0]["type"] == "PROJECT_CREATED"
    assert proj1_data["events"][1]["type"] == "PROJECT_FAILED"


def test_git_registry_publisher_recovers_from_active_conflicted_rebase_and_wires_events_and_bot_registry(
    tmp_path: Path,
) -> None:
    """H42-04 / M42-12 (INV-07, INV-11, INV-19, INV-21, INV-23): Recovers from active unresolved rebase conflict (REBASE_HEAD), passes BuildWorker store events to publish_project, and uses TelegramBotService.registry_publisher."""
    bare_repo = _init_bare_repo(tmp_path)
    runner = SubprocessRunner(default_timeout_seconds=15.0)
    ts = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

    wt_conflict = tmp_path / "wt_conflict"
    wt_peer = tmp_path / "wt_peer"

    pub_conflict = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=wt_conflict,
    )
    pub_peer = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=runner,
        work_dir=wt_peer,
    )

    pub_conflict.publish_project(
        ProjectStatus(project_id="proj-base", name="Base", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    pub_peer.publish_project(
        ProjectStatus(project_id="proj-peer-init", name="PeerInit", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    # Create diverged local commit + peer push on registry.json, then leave wt_conflict in active conflicted rebase
    pub_conflict_offline = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=False,
        runner=runner,
        work_dir=wt_conflict,
    )
    pub_conflict_offline.publish_project(
        ProjectStatus(project_id="proj-offline-cached", name="OfflineCached", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    pub_peer.publish_project(
        ProjectStatus(project_id="proj-peer-next", name="PeerNext", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )

    subprocess.run(["git", "-C", str(wt_conflict), "fetch", "origin", "registry"], check=True, capture_output=True)
    reb = subprocess.run(
        ["git", "-C", str(wt_conflict), "rebase", "origin/registry"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert reb.returncode != 0
    assert (wt_conflict / ".git" / "rebase-merge").exists() or (wt_conflict / ".git" / "rebase-apply").exists()
    assert (wt_conflict / ".git" / "REBASE_HEAD").exists()

    # publish_project MUST abort the stuck conflicted rebase first and recover cleanly
    pub_conflict.publish_project(
        ProjectStatus(project_id="proj-after-conflict", name="Recovered", state=ProjectState.DELIVERED, progress_pct=100, updated_at=ts)
    )
    assert not (wt_conflict / ".git" / "rebase-merge").exists()
    assert not (wt_conflict / ".git" / "REBASE_HEAD").exists()

    reg_after = json.loads(
        subprocess.run(
            ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert "proj-after-conflict" in reg_after["projects"]
    assert "proj-offline-cached" in reg_after["projects"]
    assert "proj-peer-next" in reg_after["projects"]

    # Part B: BuildWorker passes store events to publish_project AND TelegramBotService uses registry_publisher (kills M42-12)
    class _RecordingRegistryWrapper:
        def __init__(self, inner: GitRegistryPublisher) -> None:
            self.inner = inner
            self.publish_calls: list[tuple[ProjectStatus, list[Event]]] = []

        def publish_project(self, status: ProjectStatus, events: Sequence[Event] = ()):
            self.publish_calls.append((status, list(events)))
            return self.inner.publish_project(status, events=events)

        def load_index(self):
            return self.inner.load_index()

    event_secret = "TEST_H4204_EVT_SECRET_8899"
    plan = Plan(
        summary="Nota Secreta",
        rationale="Static ES",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="OK", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "App Nota",
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
            {"approach": "Static", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )

    store = SqliteEventStore(db_path=tmp_path / "h4204.db")
    try:
        orch = Orchestrator(store=store, llm=llm)
        rec_reg = _RecordingRegistryWrapper(
            GitRegistryPublisher(
                repo_path=bare_repo,
                branch="registry",
                push_to_remote=True,
                runner=runner,
                work_dir=tmp_path / "wt_worker",
                secrets=(event_secret,),
            )
        )
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_worker"),
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
                impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
            ),
            runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary=f"CI ok GITHUB_TOKEN={event_secret}")]),
            queue=LocalJobQueue(),
            registry_publisher=rec_reg,
            secrets_to_redact=(event_secret,),
        )
        tg_client = FakeTelegramClient()
        bot = TelegramBotService(
            client=tg_client,
            orchestrator=orch,
            config=TelegramBotConfig(allowed_chat_ids=frozenset({1001}), allowed_user_ids=frozenset({2001})),
            build_worker=worker,
            registry_publisher=rec_reg,
        )

        # /new -> WAITING_APPROVAL: TelegramBotService MUST publish to registry_publisher BEFORE BuildWorker runs (kills M42-12)
        new_out = bot.handle_update(
            TelegramUpdate(update_id=101, chat_id=1001, user_id=2001, text="/new Nota App")
        )
        pid = new_out.project_id
        assert pid == "nota-app"
        assert len(rec_reg.publish_calls) >= 1
        assert any(st.project_id == pid and st.state == ProjectState.WAITING_APPROVAL for st, _ in rec_reg.publish_calls)

        waiting_hash = orch.get_status(pid).plan_hash
        assert waiting_hash is not None
        calls_before_approve = len(rec_reg.publish_calls)

        # approve -> BuildWorker MUST pass store events to registry_publisher.publish_project(...)
        bot.handle_update(
            TelegramUpdate(
                update_id=102,
                chat_id=1001,
                user_id=2001,
                callback_query_id="cb-nota",
                callback_data=f"approve:{pid}:{waiting_hash}",
            )
        )
        assert len(rec_reg.publish_calls) > calls_before_approve
        worker_status, worker_events = rec_reg.publish_calls[-1]
        assert worker_status.state == ProjectState.DELIVERED
        expected_events = store.get_events(pid)
        assert len(worker_events) == len(expected_events)
        assert len(worker_events) >= 4

        proj_json_raw = subprocess.run(
            ["git", "-C", str(bare_repo), "show", f"refs/heads/registry:projects/{pid}.json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert event_secret not in proj_json_raw
        proj_json = json.loads(proj_json_raw)
        assert proj_json["state"] == "DELIVERED"
        assert isinstance(proj_json.get("events"), list)
        assert len(proj_json["events"]) == len(expected_events)
    finally:
        store.close()
