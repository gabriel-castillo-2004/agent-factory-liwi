"""Tests for self-healing RecoveryAgent, error_lessons memory, Builder/Orchestrator interception, and Dashboard visibility (INV-03, INV-11, INV-19, INV-22, INV-23)."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.github_vcs_adapter import VcsError
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker
from agent_factory.dashboard import start_dashboard_server
from agent_factory.domain.events import EventType, create_event
from agent_factory.domain.models import ApprovalEvent, Plan
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.runner import CommandResult
from agent_factory.recovery_agent import (
    ErrorLessonStore,
    RecoveryAgent,
    compact_pytest_failure_output,
    extract_failed_pytest_nodes,
)
from agent_factory.workspace import WorkspaceManager


def _make_approved_project(
    store: Any,
    project_id: str = "proj-doc-01",
    *,
    num_tests: int = 1,
) -> Orchestrator:
    orch = Orchestrator(store=store, llm=FakeLLM())
    orch.create_project(project_id, f"App {project_id}", "Build resilient app")
    now = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
    store.append(
        create_event(
            project_id,
            EventType.STATE_TRANSITIONED,
            {"from": ProjectState.INTERVIEW.value, "to": ProjectState.DEBATE.value},
            at=now,
        )
    )
    store.append(
        create_event(
            project_id,
            EventType.STATE_TRANSITIONED,
            {"from": ProjectState.DEBATE.value, "to": ProjectState.MEDIATION.value},
            at=now,
        )
    )
    plan = Plan.model_validate(
        {
            "summary": "Resilient web app",
            "rationale": "Static modular app",
            "template_id": "static-app",
            "files": [
                {
                    "path": "src/app.js",
                    "purpose": "Main logic",
                    "contract": "export function run(): boolean",
                    "depends_on": [],
                }
            ],
            "acceptance_tests": [
                {
                    "id": f"AT-0{i}",
                    "description": f"Criterion {i}",
                    "type": "unit",
                }
                for i in range(1, num_tests + 1)
            ],
            "risks": ["None"],
            "deployment_target": "github-pages",
        }
    )
    plan_hash = plan.compute_hash()
    store.append(
        create_event(
            project_id,
            EventType.PLAN_CREATED,
            {
                "plan": plan.model_dump(mode="json"),
                "plan_hash": plan_hash,
                "total_tests": num_tests,
                "to": ProjectState.WAITING_APPROVAL.value,
            },
            at=now,
        )
    )
    orch.submit_approval(
        project_id,
        ApprovalEvent(plan_hash=plan_hash, timestamp=now, decision="approved"),
    )
    return orch


def test_sqlite_error_lessons_table_schema_and_seeded_playbooks(tmp_path: Path) -> None:
    """SqliteEventStore initializes error_lessons with expected schema and preloads git_sync_main & pip_install playbooks."""
    db_path = tmp_path / "factory.db"
    store = SqliteEventStore(db_path=db_path)
    try:
        cols = [
            row[1]
            for row in store._conn.execute("PRAGMA table_info(error_lessons)").fetchall()
        ]
        assert cols == [
            "id",
            "error_pattern",
            "stage",
            "scope",
            "root_cause",
            "resolution_type",
            "resolution_payload",
            "success_count",
            "updated_at",
        ]

        lesson_store = ErrorLessonStore(sqlite_store=store)
        pr_match = lesson_store.find_matching_lesson(
            "VcsError: Merge failed (HTTP 405): Pull Request has merge conflicts",
            stage="PUBLISHING",
            scope="workspace",
        )
        assert pr_match is not None
        assert pr_match.resolution_type == "git_sync_main"
        assert pr_match.scope == "workspace"
        assert pr_match.success_count >= 1

        pip_match = lesson_store.find_matching_lesson(
            "ModuleNotFoundError: No module named 'yaml'",
            stage="VERIFYING",
            scope="workspace",
        )
        assert pip_match is not None
        assert pip_match.resolution_type == "pip_install"
        assert pip_match.scope == "workspace"
        assert pip_match.success_count >= 1
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_recovery_agent_matches_git_sync_main_playbook_on_pr_conflict_and_recovers_build(
    tmp_path: Path,
) -> None:
    """Paso A (git_sync_main): When merge_and_deploy raises HTTP 405 PR merge conflict, Doctor Agent executes git fetch origin, git merge -s ours origin/main --no-edit, git push --force-with-lease -u origin work, increments success_count, and retries merge cleanly."""
    bare_remote = tmp_path / "pr_conflict_origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)

    # Seed main with initial commit + conflicting upstream commit on main
    seed_dir = tmp_path / "seed_main"
    seed_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seed"], cwd=seed_dir, check=True)
    subprocess.run(["git", "config", "user.email", "seed@local"], cwd=seed_dir, check=True)
    (seed_dir / "index.html").write_text("<h1>Base v1</h1>\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed_dir, check=True)
    subprocess.run(["git", "commit", "-m", "base v1"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", str(bare_remote), "main:main"], cwd=seed_dir, check=True, capture_output=True)

    # Advance main with a conflicting edit on bare_remote
    (seed_dir / "index.html").write_text("<h1>Main Upstream Edit</h1>\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed_dir, check=True)
    subprocess.run(["git", "commit", "-m", "main upstream"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", str(bare_remote), "main:main"], cwd=seed_dir, check=True, capture_output=True)
    main_head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=seed_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    db_path = tmp_path / "factory_git_sync.db"
    store = SqliteEventStore(db_path=db_path)
    try:
        pid = "proj-git-conflict"
        orch = _make_approved_project(store, project_id=pid, num_tests=1)
        ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
        ws_dir = ws_mgr.create_workspace(pid)
        subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=ws_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Worker"], cwd=ws_dir, check=True)
        subprocess.run(["git", "config", "user.email", "worker@local"], cwd=ws_dir, check=True)
        (ws_dir / "index.html").write_text("<h1>Base v1</h1>\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True)
        subprocess.run(["git", "commit", "-m", "local base"], cwd=ws_dir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "work"], cwd=ws_dir, check=True, capture_output=True)
        (ws_dir / "index.html").write_text("<h1>Tested Work App</h1>\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True)
        subprocess.run(["git", "commit", "-m", "work impl"], cwd=ws_dir, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", str(bare_remote), "work:work"], cwd=ws_dir, check=True, capture_output=True)

        real_runner = SubprocessRunner(default_timeout_seconds=15.0)
        recorded_cmds: list[list[str]] = []

        class _SpyGitRunner(SubprocessRunner):
            def run(
                self,
                command: Sequence[str],
                cwd: Path,
                timeout_seconds: float = 30.0,
                env: Mapping[str, str] | None = None,
            ) -> CommandResult:
                recorded_cmds.append(list(command))
                if command and Path(command[0]).name.lower().startswith("pytest"):
                    return CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

        spy_runner = _SpyGitRunner()

        class _ConflictThenGreenVCS(FakeVCS):
            def merge_and_deploy(self, project_id: str, branch: str = "work") -> tuple[str, str]:
                # Fail HTTP 405 until origin/main is an ancestor of origin/work
                anc = subprocess.run(
                    ["git", "-C", str(bare_remote), "merge-base", "--is-ancestor", main_head_sha, f"refs/heads/{branch}"],
                    check=False,
                    capture_output=True,
                )
                if anc.returncode != 0:
                    raise VcsError("Merge failed (HTTP 405): Pull Request has merge conflicts")
                return super().merge_and_deploy(project_id, branch=branch)

        recovery_agent = RecoveryAgent(
            sqlite_store=store,
            workspace_manager=ws_mgr,
            runner=spy_runner,
            agy_command=["__disabled_in_test__"],
        )
        pr_lesson = ErrorLessonStore(sqlite_store=store).get_lesson("lesson-pr-merge-conflicts")
        assert pr_lesson is not None
        before_count = pr_lesson.success_count

        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_at01(): assert True\n"}],
                impl_steps=[{"src/app.js": "export const ok = true;\n"}],
            ),
            runner=spy_runner,
            vcs=_ConflictThenGreenVCS(),
            recovery_agent=recovery_agent,
        )
        report = worker.run_job(pid)

        assert orch.get_status(pid).state == ProjectState.DELIVERED
        assert report.failure_cause is None

        # Verify exact git sync playbook commands ran in order
        cmd_strs = [" ".join(c) for c in recorded_cmds]
        assert "git fetch origin" in cmd_strs
        assert any("git merge -s ours origin/main --no-edit" in s for s in cmd_strs)
        assert "git push --force-with-lease -u origin work" in cmd_strs
        assert (ws_dir / "index.html").read_text(encoding="utf-8") == "<h1>Tested Work App</h1>\n"

        after_lesson = ErrorLessonStore(sqlite_store=store).get_lesson("lesson-pr-merge-conflicts")
        assert after_lesson is not None
        assert after_lesson.success_count == before_count + 1

        doc_events = [e for e in store.get_events(pid) if e.type == EventType.DOCTOR_RECOVERY]
        assert len(doc_events) == 1
        rec = doc_events[0].payload
        assert rec["recovered"] is True
        assert rec["used_lesson"] is True
        assert rec["lesson_applied_type"] == "git_sync_main"
        assert rec["attempts"] == 1
    finally:
        store.close()


def test_recovery_agent_learns_new_lesson_from_simulated_agy_and_reuses_it_later(
    tmp_path: Path,
) -> None:
    """Paso B -> Paso A: Unknown workspace error invokes agy with --add-dir <workspace>, repairs workspace, learns new lesson in SQLite, and future occurrences match stored lesson without agy."""
    db_path = tmp_path / "learned_lessons.db"
    store = SqliteEventStore(db_path=db_path)
    try:
        ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_learn")
        ws_dir = ws_mgr.create_workspace("proj-learn-1")
        (ws_dir / "config.json").write_text('{"mode": "broken"}\n', encoding="utf-8")

        agy_script = tmp_path / "sim_doctor_agy.py"
        agy_script.write_text(
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "ws = pathlib.Path(args[args.index('--add-dir') + 1])\n"
            "(ws / 'config.json').write_text('{\"mode\": \"healthy\"}\\n', encoding='utf-8')\n"
            "print(json.dumps({'files_written': ['config.json'], 'summary': 'Fixed missing healthy mode in config.json'}))\n",
            encoding="utf-8",
        )

        spy_calls: list[list[str]] = []
        real_runner = SubprocessRunner(default_timeout_seconds=15.0)

        class _RecordingRunner(SubprocessRunner):
            def run(
                self,
                command: Sequence[str],
                cwd: Path,
                timeout_seconds: float = 30.0,
                env: Mapping[str, str] | None = None,
            ) -> CommandResult:
                spy_calls.append(list(command))
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

        doctor = RecoveryAgent(
            sqlite_store=store,
            workspace_manager=ws_mgr,
            runner=_RecordingRunner(),
            agy_command=[sys.executable, str(agy_script)],
        )

        def _check_config() -> str:
            data = json.loads((ws_dir / "config.json").read_text(encoding="utf-8"))
            if data.get("mode") != "healthy":
                raise RuntimeError("CustomConfigMismatchError: expected healthy mode in config.json")
            return "ok"

        with pytest.raises(RuntimeError, match="CustomConfigMismatchError") as exc_info:
            _check_config()

        res_first = doctor.attempt_recovery(
            project_id="proj-learn-1",
            stage="VERIFYING",
            error=exc_info.value,
            workspace_dir=ws_dir,
            retry_fn=_check_config,
            recent_logs="stderr: validating config.json failed\n",
        )

        assert res_first.recovered is True
        assert res_first.attempts == 1
        assert res_first.scope == "workspace"
        assert res_first.learned_new_lesson is True
        assert res_first.used_lesson is False
        assert res_first.lesson_applied_type == "agy_workspace_fix"
        assert res_first.lesson_id is not None

        # Verify agy received --add-dir <workspace> plus error and recent logs
        agy_cmds = [c for c in spy_calls if str(agy_script) in " ".join(c)]
        assert len(agy_cmds) == 1
        assert "--add-dir" in agy_cmds[0]
        assert str(ws_dir.resolve()) == agy_cmds[0][agy_cmds[0].index("--add-dir") + 1]
        prompt_text = agy_cmds[0][agy_cmds[0].index("-p") + 1]
        assert "CustomConfigMismatchError" in prompt_text
        assert "validating config.json failed" in prompt_text

        # Lesson persisted in SQLite error_lessons table
        row = store._conn.execute(
            "SELECT error_pattern, stage, scope, resolution_type, success_count FROM error_lessons WHERE id = ?",
            (res_first.lesson_id,),
        ).fetchone()
        assert row is not None
        assert row[2] == "workspace"
        assert row[3] == "agy_workspace_fix"
        assert row[4] == 1

        # Re-open lesson store / simulate fresh error in second workspace -> matches learned lesson (Paso A)
        ws_dir_2 = ws_mgr.create_workspace("proj-learn-2")
        (ws_dir_2 / "config.json").write_text('{"mode": "broken"}\n', encoding="utf-8")
        matched = ErrorLessonStore(sqlite_store=store).find_matching_lesson(
            "CustomConfigMismatchError: expected healthy mode in config.json",
            stage="VERIFYING",
            scope="workspace",
        )
        assert matched is not None
        assert matched.id == res_first.lesson_id

        res_second = doctor.attempt_recovery(
            project_id="proj-learn-2",
            stage="VERIFYING",
            error=RuntimeError("CustomConfigMismatchError: expected healthy mode in config.json"),
            workspace_dir=ws_dir_2,
            retry_fn=lambda: (
                "ok"
                if json.loads((ws_dir_2 / "config.json").read_text(encoding="utf-8")).get("mode") == "healthy"
                else (_ for _ in ()).throw(RuntimeError("CustomConfigMismatchError: expected healthy mode in config.json"))
            ),
        )
        assert res_second.recovered is True
        assert res_second.used_lesson is True
        assert res_second.learned_new_lesson is False
        assert res_second.lesson_id == res_first.lesson_id
    finally:
        store.close()


def test_recovery_agent_enforces_max_two_attempts_limit_before_failing(
    tmp_path: Path,
) -> None:
    """Doctor Agent stops strictly at max 2 attempts when workspace errors persist and transitions cleanly to FAILED with incident metadata."""
    store = SqliteEventStore(db_path=tmp_path / "max_attempts.db")
    try:
        pid = "proj-max-2"
        orch = _make_approved_project(store, project_id=pid, num_tests=1)
        ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_max2")
        ws_mgr.create_workspace(pid)

        fake_runner = FakeRunner(
            results=[
                CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                # Attempt 1: agy runs, succeeds exit 0
                CommandResult(exit_code=0, stdout=json.dumps({"files_written": [], "summary": "Tried fix 1"})),
                # Attempt 2: agy runs, succeeds exit 0
                CommandResult(exit_code=0, stdout=json.dumps({"files_written": [], "summary": "Tried fix 2"})),
                # Extra result should NEVER be consumed
                CommandResult(exit_code=0, stdout="MUST_NOT_RUN"),
            ]
        )

        class _AlwaysFailMergeVCS(FakeVCS):
            def merge_and_deploy(self, project_id: str, branch: str = "work") -> tuple[str, str]:
                raise VcsError("Permanent remote repository protection failure 403")

        doctor = RecoveryAgent(
            sqlite_store=store,
            workspace_manager=ws_mgr,
            runner=fake_runner,
        )
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_at01(): assert True\n"}],
                impl_steps=[{"src/app.js": "export const ok = true;\n"}],
            ),
            runner=fake_runner,
            vcs=_AlwaysFailMergeVCS(),
            recovery_agent=doctor,
        )
        report = worker.run_job(pid)

        status = orch.get_status(pid)
        assert status.state == ProjectState.FAILED
        assert report.failure_cause is not None
        assert "403" in report.failure_cause

        # 1 initial pytest + strictly 2 agy recovery attempts = 3 runner calls total (never a 3rd recovery attempt)
        assert len(fake_runner.calls) == 3

        phase_err = next(e for e in store.get_events(pid) if e.type == EventType.PHASE_ERROR)
        rec_meta = phase_err.payload["recovery"]
        assert rec_meta["recovered"] is False
        assert rec_meta["attempts"] == 2
        assert rec_meta["max_attempts"] == 2
        assert rec_meta["used_lesson"] is False
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_recovery_agent_isolates_factory_traceback_readonly_diagnosis_and_exposes_in_dashboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paso C + Dashboard (INV-19, INV-22, INV-23): When traceback originates in src/agent_factory/, never pass --dangerously-skip-permissions or modify factory code, record diagnosis & suggested prompt, and display them in Dashboard."""
    sec_tok = "FACTORY_DOCTOR_SECRET_999"
    monkeypatch.setenv("GITHUB_TOKEN", sec_tok)

    store = SqliteEventStore(db_path=tmp_path / "factory_diag.db")
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_factory")
    pid = "proj-factory-err"
    orch = _make_approved_project(store, project_id=pid, num_tests=1)

    spy_calls: list[dict[str, Any]] = []

    class _ReadOnlyDiagRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            spy_calls.append({"command": list(command), "cwd": Path(cwd), "env": dict(env or {})})
            return CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "diagnosis": "KeyError en orchestrator.py al leer metadata faltante en proyección.",
                        "suggested_prompt": "En src/agent_factory/orchestrator.py, valida con .get() antes de indexar el payload y añade prueba unitaria.",
                    }
                ),
            )

    doctor = RecoveryAgent(
        sqlite_store=store,
        workspace_manager=ws_mgr,
        runner=_ReadOnlyDiagRunner(),
        secrets_to_redact=(sec_tok,),
    )
    orch.recovery_agent = doctor

    factory_tb = (
        "Traceback (most recent call last):\n"
        '  File "C:/repo/src/agent_factory/orchestrator.py", line 1420, in run_pipeline\n'
        '    val = metadata["missing_key"]\n'
        f"KeyError: 'missing_key' token={sec_tok}\n"
    )
    res = doctor.attempt_recovery(
        project_id=pid,
        stage="BUILDING",
        error=KeyError(f"missing_key token={sec_tok}"),
        workspace_dir=ws_mgr.create_workspace(pid),
        traceback_text=factory_tb,
        retry_fn=lambda: (_ for _ in ()).throw(AssertionError("Must never retry/mutate factory bugs")),
        extra_secrets=(sec_tok,),
    )

    assert res.recovered is False
    assert res.scope == "factory"
    assert res.attempts == 0
    assert res.lesson_applied_type == "factory_diagnostic"
    assert "orchestrator.py" in res.diagnosis
    assert "src/agent_factory/orchestrator.py" in res.suggested_prompt
    assert sec_tok not in res.diagnosis
    assert sec_tok not in res.suggested_prompt
    assert sec_tok not in res.last_error

    assert len(spy_calls) == 1
    ro_cmd = spy_calls[0]["command"]
    assert "--dangerously-skip-permissions" not in ro_cmd
    assert "--add-dir" not in ro_cmd
    assert sec_tok not in " ".join(ro_cmd)
    assert "GITHUB_TOKEN" not in spy_calls[0]["env"]

    orch.record_phase_failure(
        pid,
        failed_state=ProjectState.BUILDING,
        error=KeyError(f"missing_key token={sec_tok}"),
        agent="orchestrator",
        extra_secrets=(sec_tok,),
        recovery=res,
    )

    server, thread, base_url = start_dashboard_server(
        orchestrator=orch,
        port=0,
        workspace_manager=ws_mgr,
        secrets_to_redact=(sec_tok,),
    )
    try:
        with urllib.request.urlopen(f"{base_url}/api/projects/{pid}") as resp:
            detail = json.loads(resp.read().decode("utf-8"))
        with urllib.request.urlopen(f"{base_url}/") as html_resp:
            html_body = html_resp.read().decode("utf-8")

        last_rec = detail["status"]["last_recovery"]
        assert last_rec is not None
        assert last_rec["scope"] == "factory"
        assert last_rec["lesson_applied_type"] == "factory_diagnostic"
        assert "orchestrator.py" in last_rec["diagnosis"]
        assert "src/agent_factory/orchestrator.py" in last_rec["suggested_prompt"]
        assert sec_tok not in json.dumps(detail)

        assert "Agente Doctor" in html_body
        assert "Copiar prompt sugerido" in html_body
        assert "DOCTOR_RECOVERY" in html_body
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_recovery_agent_matches_pip_install_playbook_on_module_not_found(
    tmp_path: Path,
) -> None:
    """Paso A (pip_install): ModuleNotFoundError triggers isolated workspace pip install with mapped package name and rejects unsafe module tokens (INV-22)."""
    store = SqliteEventStore(db_path=tmp_path / "pip_lessons.db")
    try:
        ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_pip")
        ws_dir = ws_mgr.create_workspace("proj-pip-1")
        # Simulate existing workspace venv python cleanly
        venv_py = (
            ws_dir / ".venv" / "Scripts" / "python.exe"
            if (sys.platform == "win32")
            else ws_dir / ".venv" / "bin" / "python"
        )
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("", encoding="utf-8")

        fake_runner = FakeRunner(
            results=[
                CommandResult(exit_code=0, stdout="Successfully installed PyYAML"),
            ]
        )
        doctor = RecoveryAgent(
            sqlite_store=store,
            workspace_manager=ws_mgr,
            runner=fake_runner,
        )
        installed = {"ok": False}

        def _retry_pytest() -> str:
            if not installed["ok"]:
                installed["ok"] = True
                raise RuntimeError("ModuleNotFoundError: No module named 'yaml'")
            return "1 passed"

        with pytest.raises(RuntimeError, match="No module named") as exc_info:
            _retry_pytest()

        res = doctor.attempt_recovery(
            project_id="proj-pip-1",
            stage="VERIFYING",
            error=exc_info.value,
            workspace_dir=ws_dir,
            retry_fn=_retry_pytest,
        )
        assert res.recovered is True
        assert res.used_lesson is True
        assert res.lesson_applied_type == "pip_install"
        assert res.recovered_value == "1 passed"
        assert len(fake_runner.calls) == 1
        pip_cmd = fake_runner.calls[0]["command"]
        assert Path(pip_cmd[0]).resolve() == venv_py.resolve()
        assert pip_cmd[1:] == ["-m", "pip", "install", "PyYAML"]
        assert "GITHUB_TOKEN" not in (fake_runner.calls[0]["env"] or {})

        # Negative test: unsafe flag-like module name must never execute pip install
        bad_runner = FakeRunner()
        bad_doctor = RecoveryAgent(
            sqlite_store=store,
            workspace_manager=ws_mgr,
            runner=bad_runner,
        )
        res_bad = bad_doctor.attempt_recovery(
            project_id="proj-pip-1",
            stage="VERIFYING",
            error=RuntimeError("No module named '--index-url=http://evil'"),
            workspace_dir=ws_dir,
            retry_fn=None,
        )
        assert res_bad.recovered is False
        assert not any("pip" in c["command"] for c in bad_runner.calls)
    finally:
        store.close()


def test_extract_failed_pytest_nodes_deduplicates_and_handles_empty_output() -> None:
    """extract_failed_pytest_nodes returns unique ordered 'tests/...' node IDs and empty list for clean/empty output."""
    assert extract_failed_pytest_nodes("") == []
    assert extract_failed_pytest_nodes("   \n\t  ") == []
    assert extract_failed_pytest_nodes("12 passed in 0.42s\n") == []

    raw = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_alpha.py::test_one - AssertionError: boom\n"
        "FAILED tests/test_beta.py::TestSuite::test_two[case-1] - ValueError: bad\n"
        "FAILED tests/test_alpha.py::test_one - AssertionError: duplicate\n"
        "  FAILED tests\\test_gamma.py::test_win\n"
        "FAILED tests/test_delta.py - RuntimeError: import failed\n"
        "FAILED other_dir/not_a_test.py::test_skip - AssertionError: ignored\n"
    )
    assert extract_failed_pytest_nodes(raw) == [
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::TestSuite::test_two[case-1]",
        "tests/test_gamma.py::test_win",
        "tests/test_delta.py",
    ]


def test_compact_pytest_failure_output_strict_max_chars_preserves_summary_assertions_and_handles_empty() -> None:
    """compact_pytest_failure_output keeps failure summary and latest assertions/tracebacks within strict <= max_chars."""
    assert compact_pytest_failure_output("") == ""
    assert compact_pytest_failure_output("   \n\t ") == ""
    assert compact_pytest_failure_output("FAILED tests/test_x.py::test_a", max_chars=0) == ""
    assert compact_pytest_failure_output("FAILED tests/test_x.py::test_a", max_chars=-10) == ""

    short_out = "FAILED tests/test_x.py::test_a - AssertionError: 1 != 2\n1 failed in 0.12s"
    assert compact_pytest_failure_output(short_out, max_chars=2500) == short_out

    leading_noise = "\n".join(f"PASSED tests/test_noise.py::test_ok_{i} [ 10%]" for i in range(80))
    traceback_block = (
        "=================================== FAILURES ===================================\n"
        "__________________________________ test_total __________________________________\n"
        "    def test_total():\n"
        "        total = compute_total()\n"
        ">       assert total == 42\n"
        "E       AssertionError: assert 7 == 42\n"
        "E        +  where 7 = compute_total()\n"
        "tests/test_calc.py:42: AssertionError\n"
    )
    middle_noise = "\n".join(f"DEBUG NOISY_INTERMEDIATE_LINE_{i}: filler payload 0123456789" for i in range(180))
    summary_block = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_calc.py::test_total - AssertionError: assert 7 == 42\n"
        "========================= 1 failed, 90 passed in 4.10s ========================="
    )
    long_output = f"{leading_noise}\n{traceback_block}\n{middle_noise}\n{summary_block}"
    assert len(long_output) > 5000

    compacted = compact_pytest_failure_output(long_output, max_chars=700)
    assert len(compacted) <= 700
    assert "truncad" in compacted.lower()
    assert "FAILED tests/test_calc.py::test_total" in compacted
    assert "1 failed, 90 passed" in compacted
    assert "AssertionError: assert 7 == 42" in compacted
    assert ">       assert total == 42" in compacted
    assert "NOISY_INTERMEDIATE_LINE_120" not in compacted

    # Very tight budgets must still strictly respect <= max_chars
    for tight_cap in (120, 40, 15, 3):
        tight = compact_pytest_failure_output(long_output, max_chars=tight_cap)
        assert len(tight) <= tight_cap

    # Single huge line without newlines
    single_line = "X" * 4000
    compact_single = compact_pytest_failure_output(single_line, max_chars=250)
    assert len(compact_single) <= 250
    assert "truncad" in compact_single.lower()


def test_recovery_agent_workspace_fix_prompt_compacts_pytest_output_and_suggests_failed_nodes(
    tmp_path: Path,
) -> None:
    """RecoveryAgent compacts verbose pytest logs and suggests re-running deduplicated failed node IDs first."""
    store = SqliteEventStore(db_path=tmp_path / "compact_ws.db")
    try:
        ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_compact")
        ws_dir = ws_mgr.create_workspace("proj-compact-1")
        fake_runner = FakeRunner(
            results=[
                CommandResult(exit_code=0, stdout=json.dumps({"files_written": ["app.py"], "summary": "Fixed edge"})),
            ]
        )
        doctor = RecoveryAgent(
            sqlite_store=store,
            workspace_manager=ws_mgr,
            runner=fake_runner,
        )
        noisy_logs = (
            "\n".join(f"INFO NOISE_LOG_LINE_{i}: verbose setup output 1234567890" for i in range(160))
            + "\n"
            + ">       assert compute() == 'expected_ok'\n"
            + "E       AssertionError: expected_ok\n"
            + "tests/test_mod.py:18: AssertionError\n"
            + "=========================== short test summary info ============================\n"
            + "FAILED tests/test_mod.py::test_edge - AssertionError: expected_ok\n"
            + "FAILED tests/test_mod.py::test_edge - AssertionError: expected_ok\n"
            + "1 failed, 20 passed in 2.00s\n"
        )
        res = doctor.attempt_recovery(
            project_id="proj-compact-1",
            stage="VERIFYING",
            error=RuntimeError("pytest failed with AssertionError: expected_ok"),
            workspace_dir=ws_dir,
            recent_logs=noisy_logs,
            retry_fn=lambda: "green",
        )
        assert res.recovered is True
        assert len(fake_runner.calls) == 1
        cmd = fake_runner.calls[0]["command"]
        prompt = cmd[cmd.index("-p") + 1]
        assert "pytest -q --no-cov tests/test_mod.py::test_edge" in prompt
        assert "AssertionError: expected_ok" in prompt
        assert "truncad" in prompt.lower()
        assert "NOISE_LOG_LINE_100" not in prompt
    finally:
        store.close()
