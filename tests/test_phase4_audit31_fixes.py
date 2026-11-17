"""Tests closing remaining AUDIT_REPORT_3_1.md findings (AC-4.5, INV-07, INV-16, INV-19, INV-21, INV-23)."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_telegram import FakeTelegramClient
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    FileSpec,
    Plan,
    ProjectStatus,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.llm import LLMError
from agent_factory.ports.notifier import TelegramUpdate
from agent_factory.secrets import redact_secrets
from agent_factory.telegram_bot import TelegramBotConfig, TelegramBotService


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
