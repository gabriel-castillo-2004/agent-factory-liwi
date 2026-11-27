"""Tests for Supervisor auto-restart (exit 42), FactorySelfImprover memory/consult/apply-rollback, and Dashboard UI (INV-03, INV-19, INV-22, INV-23)."""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.dashboard import start_dashboard_server
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.runner import CommandResult
from agent_factory.self_improver import (
    FactoryMemoryStore,
    FactorySelfImprover,
    select_targeted_test_paths,
)
from agent_factory.supervisor import RESTART_EXIT_CODE, run_supervisor

# Valid 1x1 transparent PNG bytes for screenshot upload tests
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _http_call(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def _init_temp_factory_git_repo(repo_dir: Path, bare_remote: Path | None = None) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=repo_dir, check=True)
    (repo_dir / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "agent_factory" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo_dir / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo_dir / "MASTER_PROMPT.md").write_text("# Immutable\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: initial snapshot"], cwd=repo_dir, check=True, capture_output=True)
    if bare_remote is not None:
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=repo_dir, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_supervisor_restarts_on_exit_42_after_1500ms_and_stops_on_other_codes() -> None:
    """supervisor.py loops only on RESTART_EXIT_CODE=42 with 1.5s sleep and exits cleanly for any other exit code."""
    assert RESTART_EXIT_CODE == 42

    spawned_cmds: list[list[str]] = []
    sleeps: list[float] = []
    exit_codes = [42, 42, 0]

    def _fake_spawn(cmd: Sequence[str]) -> Any:
        spawned_cmds.append(list(cmd))
        return SimpleNamespace(returncode=exit_codes.pop(0))

    final_ok = run_supervisor(
        ["--dashboard-port", "8000"],
        spawn_fn=_fake_spawn,
        sleep_fn=sleeps.append,
        executable="/fake/python",
    )
    assert final_ok == 0
    assert len(spawned_cmds) == 3
    for cmd in spawned_cmds:
        assert cmd[:3] == ["/fake/python", "-m", "agent_factory"]
        assert "--dashboard-port" in cmd
    assert sleeps == [1.5, 1.5]

    # Non-42 failure exit code terminates immediately without restarting
    err_sleeps: list[float] = []
    final_err = run_supervisor(
        [],
        spawn_fn=lambda _cmd: SimpleNamespace(returncode=2),
        sleep_fn=err_sleeps.append,
        executable="/fake/python",
    )
    assert final_err == 2
    assert err_sleeps == []


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_factory_assistant_consult_mode_memory_rules_screenshot_upload_and_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite factory_chat_messages & factory_memory_rules persist consult turns, read-only agy receives files/rules/history/screenshots, saves new_memory_rules & actionable_proposal, and redacts secrets."""
    secret_tok = "SELF_IMPROVE_SECRET_777"
    monkeypatch.setenv("GITHUB_TOKEN", secret_tok)

    db_path = tmp_path / "factory_self_improve.db"
    uploads_dir = tmp_path / "factory_chat_uploads"
    repo_root = tmp_path / "fake_repo"
    _init_temp_factory_git_repo(repo_root)

    store = SqliteEventStore(db_path=db_path)
    recorded_cli: list[dict[str, Any]] = []

    class _ConsultSpyRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            recorded_cli.append(
                {"command": list(command), "cwd": Path(cwd), "timeout": timeout_seconds, "env": dict(env or {})}
            )
            return CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "reply": f"Entendido. Propongo añadir un filtro rápido en dashboard.py (token {secret_tok}).",
                        "new_memory_rules": [
                            "Usar confirmaciones claras en español antes de acciones destructivas."
                        ],
                        "actionable_proposal": {
                            "title": "Añadir filtro rápido en barra superior",
                            "prompt": "En src/agent_factory/dashboard.py añade filtro rápido con prueba TDD.",
                        },
                    }
                ),
            )

    try:
        # Verify exact SQLite table schemas in factory DB
        msg_cols = [r[1] for r in store._conn.execute("PRAGMA table_info(factory_chat_messages)").fetchall()]
        rule_cols = [r[1] for r in store._conn.execute("PRAGMA table_info(factory_memory_rules)").fetchall()]
        assert msg_cols == [
            "id",
            "role",
            "content",
            "image_paths_json",
            "proposal_title",
            "proposal_prompt",
            "status",
            "created_at",
        ]
        assert rule_cols == ["id", "rule_text", "created_at"]

        mem_store = FactoryMemoryStore(sqlite_store=store)
        mem_store.add_rule("Preferir botones compactos en móviles.")

        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=uploads_dir,
            runner=_ConsultSpyRunner(),
            secrets_to_redact=(secret_tok,),
        )
        orch = Orchestrator(store=store, llm=FakeLLM())
        server, thread, base_url = start_dashboard_server(
            orchestrator=orch,
            port=0,
            self_improver=improver,
            secrets_to_redact=(secret_tok,),
        )
        try:
            # 1. GET history returns pre-existing memory rule
            c_hist, r_hist = _http_call(f"{base_url}/api/factory/assistant/history")
            assert c_hist == 200
            assert len(r_hist["memory_rules"]) == 1
            assert r_hist["memory_count"] == 1

            # 2. POST consult message with screenshot attachment (rejects path traversal filename -> sanitized safely inside uploads_dir)
            c_msg, r_msg = _http_call(
                f"{base_url}/api/factory/assistant/message",
                method="POST",
                body={
                    "message": "Revisa esta captura y recuerda usar confirmaciones claras en español.",
                    "images": [{"filename": "../../evil_clip.png", "data_base64": _TINY_PNG_B64}],
                },
            )
            assert c_msg == 200
            assert r_msg["ok"] is True
            assert secret_tok not in r_msg["reply"]
            assert "[REDACTED]" in r_msg["reply"]
            assert r_msg["actionable_proposal"]["title"] == "Añadir filtro rápido en barra superior"
            assert "dashboard.py" in r_msg["actionable_proposal"]["prompt"]
            assert len(r_msg["new_memory_rules"]) == 1
            assert r_msg["memory_count"] == 2

            # Verify uploaded image saved strictly inside uploads_dir (INV-22)
            saved_imgs = list(uploads_dir.iterdir())
            assert len(saved_imgs) == 1
            assert saved_imgs[0].resolve().is_relative_to(uploads_dir.resolve())
            assert saved_imgs[0].read_bytes() == base64.b64decode(_TINY_PNG_B64)

            # Verify consult agy ran strictly read-only without --dangerously-skip-permissions or host secrets
            assert len(recorded_cli) == 1
            cmd = recorded_cli[0]["command"]
            assert "--dangerously-skip-permissions" not in cmd
            assert "GITHUB_TOKEN" not in recorded_cli[0]["env"]
            prompt_sent = cmd[cmd.index("-p") + 1]
            assert "src/agent_factory/sample.py" in prompt_sent
            assert "Preferir botones compactos en móviles." in prompt_sent
            assert str(saved_imgs[0].resolve()) in prompt_sent

            # 3. GET history includes both messages + 2 memory rules
            c_hist2, r_hist2 = _http_call(f"{base_url}/api/factory/assistant/history")
            assert c_hist2 == 200
            assert len(r_hist2["messages"]) == 2
            assert r_hist2["memory_count"] == 2

            # 4. DELETE invalid & valid rule
            c_bad_del, _ = _http_call(f"{base_url}/api/factory/assistant/memory/bad..id", method="DELETE")
            assert c_bad_del == 400
            target_rule_id = r_hist2["memory_rules"][0]["id"]
            c_del, r_del = _http_call(
                f"{base_url}/api/factory/assistant/memory/{target_rule_id}",
                method="DELETE",
            )
            assert c_del == 200
            assert r_del["memory_count"] == 1
            assert _http_call(
                f"{base_url}/api/factory/assistant/memory/{target_rule_id}",
                method="DELETE",
            )[0] == 404
        finally:
            server.shutdown()
            server.server_close()
            server.controller.wait_for_idle(timeout=5.0)
            if thread.is_alive():
                thread.join(timeout=2.0)
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_factory_assistant_apply_rolls_back_git_and_skips_restart_when_pytest_fails(
    tmp_path: Path,
) -> None:
    """When ruff/pytest fails during apply, FactorySelfImprover executes git reset --hard pre-self-improve + git clean -fd src tests, saves error in chat, and NEVER schedules exit 42."""
    repo_root = tmp_path / "repo_rb"
    orig_head = _init_temp_factory_git_repo(repo_root)
    store = SqliteEventStore(db_path=tmp_path / "rb_factory.db")
    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    recorded_cmds: list[list[str]] = []
    restart_codes: list[int] = []

    class _FailingApplyRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            recorded_cmds.append(list(command))
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                # Simulate agy editing tracked file and creating untracked broken file under src/
                (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 'broken'\n", encoding="utf-8")
                (repo_root / "src" / "agent_factory" / "broken_extra.py").write_text("oops\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="Applied broken edit")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                return CommandResult(exit_code=1, stdout="FAILED tests/test_sample.py::test_ok\n1 failed")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads",
            runner=_FailingApplyRunner(),
            restart_fn=restart_codes.append,
            restart_delay_seconds=0.05,
        )
        orch = Orchestrator(store=store, llm=FakeLLM())
        server, thread, base_url = start_dashboard_server(
            orchestrator=orch,
            port=0,
            self_improver=improver,
        )
        try:
            c_app, r_app = _http_call(
                f"{base_url}/api/factory/assistant/apply",
                method="POST",
                body={
                    "proposal_title": "Cambio roto",
                    "proposal_prompt": "Modifica sample.py provocando fallo en pytest",
                },
            )
            assert c_app == 200
            assert r_app["ok"] is False
            assert r_app["applied"] is False
            assert r_app["rolled_back"] is True
            assert r_app["restart_scheduled"] is False
            assert "1 failed" in r_app["error"]
            assert restart_codes == []

            # Verify rollback restored tracked files and removed untracked files in src/tests
            assert (repo_root / "src" / "agent_factory" / "sample.py").read_text(encoding="utf-8") == "VALUE = 1\n"
            assert not (repo_root / "src" / "agent_factory" / "broken_extra.py").exists()
            head_after = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
            ).stdout.strip()
            assert head_after == orig_head

            # Verify exact rollback commands ran
            cmd_strs = [" ".join(c) for c in recorded_cmds]
            assert "git tag -f pre-self-improve HEAD" in cmd_strs
            assert "git reset --hard pre-self-improve" in cmd_strs
            assert "git clean -fd src tests" in cmd_strs

            # Error saved cleanly in factory_chat_messages
            msgs = FactoryMemoryStore(sqlite_store=store).list_messages()
            assert any(m["status"] == "rolled_back" and "1 failed" in m["content"] for m in msgs)
        finally:
            server.shutdown()
            server.server_close()
            server.controller.wait_for_idle(timeout=5.0)
            if thread.is_alive():
                thread.join(timeout=2.0)
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_factory_assistant_apply_commits_pushes_and_triggers_exit_42_when_checks_pass(
    tmp_path: Path,
) -> None:
    """When agy edits src/ and ruff+pytest pass, apply commits on main, pushes origin main, returns restart_scheduled=True, and calls restart_fn(42) after delay."""
    bare_remote = tmp_path / "factory_bare.git"
    repo_root = tmp_path / "repo_ok"
    orig_head = _init_temp_factory_git_repo(repo_root, bare_remote=bare_remote)
    store = SqliteEventStore(db_path=tmp_path / "ok_factory.db")
    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    recorded: list[tuple[list[str], float]] = []
    restart_event = threading.Event()
    restart_codes: list[int] = []

    def _capture_restart(code: int) -> None:
        restart_codes.append(code)
        restart_event.set()

    class _GreenApplyRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            recorded.append((list(command), timeout_seconds))
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="Implemented improvement")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                return CommandResult(exit_code=0, stdout="1 passed")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_ok",
            runner=_GreenApplyRunner(),
            restart_fn=_capture_restart,
            restart_delay_seconds=0.05,
        )
        orch = Orchestrator(store=store, llm=FakeLLM())
        server, thread, base_url = start_dashboard_server(
            orchestrator=orch,
            port=0,
            self_improver=improver,
        )
        try:
            c_app, r_app = _http_call(
                f"{base_url}/api/factory/assistant/apply",
                method="POST",
                body={
                    "proposal_title": "Incrementa VALUE a 2",
                    "proposal_prompt": "Actualiza VALUE = 2 en src/agent_factory/sample.py",
                },
            )
            assert c_app == 200
            assert r_app["ok"] is True
            assert r_app["applied"] is True
            assert r_app["rolled_back"] is False
            assert r_app["restart_scheduled"] is True
            assert r_app["exit_code"] == 42

            assert restart_event.wait(timeout=3.0) is True
            assert restart_codes == [42]

            new_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
            ).stdout.strip()
            remote_head = subprocess.run(
                ["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert new_head != orig_head
            assert new_head == remote_head

            # Verify 900s timeouts on agy and verification commands + targeted-tests instruction
            agy_cmds = [cmd for cmd, _t in recorded if Path(cmd[0]).stem.lower() == "agy"]
            agy_timeouts = [t for cmd, t in recorded if Path(cmd[0]).stem.lower() == "agy"]
            ruff_timeouts = [t for cmd, t in recorded if Path(cmd[0]).stem.lower() == "ruff"]
            pytest_timeouts = [t for cmd, t in recorded if Path(cmd[0]).stem.lower() == "pytest"]
            assert agy_timeouts == [900.0]
            assert ruff_timeouts == [900.0]
            assert pytest_timeouts == [900.0]
            agy_prompt_sent = agy_cmds[0][agy_cmds[0].index("-p") + 1]
            assert "pruebas dirigidas" in agy_prompt_sent.lower()
            assert "suite completa" in agy_prompt_sent.lower()

            # Verify header UI elements and Doctor Agent shortcut present in Dashboard HTML
            with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
                html = html_resp.read().decode("utf-8")
            assert "🏭 Mejorar Fábrica" in html
            assert "improve-factory-btn" in html
            assert "🧠 Memoria de Liwi" in html
            assert "✨ Aplicar mejora a Liwi" in html
            assert "factory-restart-banner" in html
            assert "data-open-factory-improve" in html
            assert "/api/factory/assistant/message" in html
            assert "/api/factory/assistant/apply" in html
        finally:
            server.shutdown()
            server.server_close()
            server.controller.wait_for_idle(timeout=5.0)
            if thread.is_alive():
                thread.join(timeout=2.0)
    finally:
        store.close()


def test_consult_message_invokes_agy_with_at_least_180s_timeout_and_dashboard_client_waits_185s(
    tmp_path: Path,
) -> None:
    """consult_message passes >= 180.0s timeout to agy and dashboard HTML sets 185000 ms for /api/factory/assistant/message."""
    from agent_factory.dashboard import DASHBOARD_HTML

    recorded_timeouts: list[float] = []

    class _TimeoutSpyRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            del command, cwd, env
            recorded_timeouts.append(timeout_seconds)
            return CommandResult(
                exit_code=0,
                stdout=json.dumps({"reply": "Listo", "new_memory_rules": [], "actionable_proposal": None}),
            )

    repo_root = tmp_path / "repo_timeout"
    (repo_root / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    improver = FactorySelfImprover(
        db_path=tmp_path / "timeout.db",
        repo_root=repo_root,
        uploads_dir=tmp_path / "uploads_to",
        runner=_TimeoutSpyRunner(),
    )
    res = improver.consult_message("Analiza posibles mejoras de rendimiento.")
    assert res["ok"] is True
    assert len(recorded_timeouts) == 1
    assert recorded_timeouts[0] >= 180.0
    assert "185000" in DASHBOARD_HTML
    assert "/api/factory/assistant/message" in DASHBOARD_HTML


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_factory_assistant_status_endpoint_live_progress_apply_900s_and_targeted_tests(
    tmp_path: Path,
) -> None:
    """GET /api/factory/assistant/status tracks steps 1/3 -> 2/3 -> 3/3 in real time, resets running=False on finish/rollback, consult+apply instruct targeted tests, and UI contains live progress markers."""
    from concurrent.futures import ThreadPoolExecutor

    repo_root = tmp_path / "repo_status"
    _init_temp_factory_git_repo(repo_root)
    store = SqliteEventStore(db_path=tmp_path / "status_factory.db")
    real_runner = SubprocessRunner(default_timeout_seconds=15.0)

    step1_entered = threading.Event()
    step1_release = threading.Event()
    step2_entered = threading.Event()
    step2_release = threading.Event()
    step3_entered = threading.Event()
    step3_release = threading.Event()
    recorded_consult_prompts: list[str] = []
    recorded_apply_timeouts: list[float] = []

    class _StepSynchronizedRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                prompt = command[command.index("-p") + 1]
                if "--dangerously-skip-permissions" not in command:
                    recorded_consult_prompts.append(prompt)
                    return CommandResult(
                        exit_code=0,
                        stdout=json.dumps(
                            {
                                "reply": "Propuesta lista",
                                "new_memory_rules": [],
                                "actionable_proposal": {
                                    "title": "Optimizar timeout",
                                    "prompt": "Ejecuta solo pytest -q --no-cov tests/test_dashboard.py",
                                },
                            }
                        ),
                    )
                recorded_apply_timeouts.append(timeout_seconds)
                step1_entered.set()
                assert step1_release.wait(timeout=8.0)
                (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 3\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="Edited cleanly")
            if exe_stem == "ruff":
                step2_entered.set()
                assert step2_release.wait(timeout=8.0)
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                return CommandResult(exit_code=0, stdout="1 passed")
            if exe_stem == "git" and len(command) >= 2 and command[1] == "commit":
                step3_entered.set()
                assert step3_release.wait(timeout=8.0)
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_st",
            runner=_StepSynchronizedRunner(),
            restart_fn=lambda _code: None,
            restart_delay_seconds=0.01,
        )
        orch = Orchestrator(store=store, llm=FakeLLM())
        server, thread, base_url = start_dashboard_server(
            orchestrator=orch,
            port=0,
            self_improver=improver,
        )
        try:
            # 1. Idle status at rest
            c_idle, r_idle = _http_call(f"{base_url}/api/factory/assistant/status")
            assert c_idle == 200
            assert r_idle["running"] is False
            assert r_idle["step"] == 0
            assert r_idle["total_steps"] == 3
            assert r_idle["stage"] == ""
            assert r_idle["detail"] == ""
            assert r_idle["elapsed_seconds"] == 0.0

            # 2. Consult mode instructs targeted tests only
            c_cons, _r_cons = _http_call(
                f"{base_url}/api/factory/assistant/message",
                method="POST",
                body={"message": "Optimiza dashboard.py"},
            )
            assert c_cons == 200
            assert len(recorded_consult_prompts) == 1
            assert "únicamente" in recorded_consult_prompts[0].lower()
            assert "tests/test_dashboard.py" in recorded_consult_prompts[0]

            # 3. Live apply updates GET /api/factory/assistant/status across all 3 steps
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(
                    _http_call,
                    f"{base_url}/api/factory/assistant/apply",
                    method="POST",
                    body={
                        "proposal_title": "Optimizar timeout",
                        "proposal_prompt": "Edita sample.py y corre pruebas dirigidas.",
                    },
                    timeout=15.0,
                )

                assert step1_entered.wait(timeout=8.0)
                c_s1, r_s1 = _http_call(f"{base_url}/api/factory/assistant/status")
                assert c_s1 == 200
                assert r_s1["running"] is True
                assert r_s1["step"] == 1
                assert r_s1["total_steps"] == 3
                assert r_s1["stage"] == "Editando código de Liwi con IA"
                assert "Analizando archivos" in r_s1["detail"]
                assert r_s1["title"] == "Optimizar timeout"
                assert r_s1["elapsed_seconds"] >= 0.0
                step1_release.set()

                assert step2_entered.wait(timeout=8.0)
                c_s2, r_s2 = _http_call(f"{base_url}/api/factory/assistant/status")
                assert c_s2 == 200
                assert r_s2["running"] is True
                assert r_s2["step"] == 2
                assert r_s2["stage"] == "Verificando calidad y pruebas"
                assert "suite completa" in r_s2["detail"]
                step2_release.set()

                assert step3_entered.wait(timeout=8.0)
                c_s3, r_s3 = _http_call(f"{base_url}/api/factory/assistant/status")
                assert c_s3 == 200
                assert r_s3["running"] is True
                assert r_s3["step"] == 3
                assert r_s3["stage"] == "Guardando commit y reiniciando"
                assert "commit en main" in r_s3["detail"]
                step3_release.set()

                c_done, r_done = fut.result(timeout=10.0)
                assert c_done == 200
                assert r_done["applied"] is True

            assert recorded_apply_timeouts == [900.0]

            # 4. Back to idle after completion
            c_end, r_end = _http_call(f"{base_url}/api/factory/assistant/status")
            assert c_end == 200
            assert r_end["running"] is False

            # 5. UI contains global live banner, modal progress card, double-click lock & status polling markers
            with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
                html = html_resp.read().decode("utf-8")
            assert "/api/factory/assistant/status" in html
            assert "factory-build-banner" in html
            assert "factory-progress-card" in html
            assert "Construyendo mejora" in html
            assert "Mejora aplicada y Liwi actualizado" in html
        finally:
            step1_release.set()
            step2_release.set()
            step3_release.set()
            server.shutdown()
            server.server_close()
            server.controller.wait_for_idle(timeout=5.0)
            if thread.is_alive():
                thread.join(timeout=2.0)
    finally:
        store.close()


def test_factory_assistant_apply_selects_targeted_tests_for_modified_dashboard_file(
    tmp_path: Path,
) -> None:
    """When agy modifies src/agent_factory/dashboard.py, verification runs pytest only on tests/test_self_improver.py and tests/test_dashboard.py (not unrelated heavy tests) with 900s timeout, and falls back to full suite if no matching test exists."""
    repo_root = tmp_path / "repo_targeted"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=repo_root, check=True)
    (repo_root / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "agent_factory" / "dashboard.py").write_text("X = 1\n", encoding="utf-8")
    (repo_root / "src" / "agent_factory" / "orphan_mod.py").write_text("Y = 1\n", encoding="utf-8")
    (repo_root / "tests" / "test_self_improver.py").write_text("def test_si(): assert True\n", encoding="utf-8")
    (repo_root / "tests" / "test_dashboard.py").write_text("def test_dash(): assert True\n", encoding="utf-8")
    (repo_root / "tests" / "test_recovery_agent.py").write_text("def test_rec(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "chore: seed repo"], cwd=repo_root, check=True, capture_output=True)

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    recorded_pytest_cmds: list[tuple[list[str], float]] = []
    edit_target = ["src/agent_factory/dashboard.py"]

    class _TargetedRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                target = repo_root / edit_target[0]
                target.write_text("VALUE_UPDATED = True\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="ok")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                recorded_pytest_cmds.append((list(command), timeout_seconds))
                return CommandResult(exit_code=0, stdout="2 passed")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    store = SqliteEventStore(db_path=tmp_path / "targeted.db")
    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_tgt",
            runner=_TargetedRunner(),
            restart_fn=lambda _code: None,
            restart_delay_seconds=0.01,
        )
        res_dash = improver.apply_proposal("Actualiza dashboard.py", proposal_title="Dashboard fix")
        assert res_dash["applied"] is True
        assert len(recorded_pytest_cmds) == 1
        cmd_dash, timeout_dash = recorded_pytest_cmds[0]
        assert timeout_dash >= 900.0
        assert "tests/test_self_improver.py" in cmd_dash
        assert "tests/test_dashboard.py" in cmd_dash
        assert "-k" in cmd_dash
        assert "autonomous or ideas or radar or four_tabs or staging" in cmd_dash
        assert "tests/test_recovery_agent.py" not in cmd_dash

        # Unmatched module with no test_*<module>*.py triggers full suite fallback
        recorded_pytest_cmds.clear()
        edit_target[0] = "src/agent_factory/orphan_mod.py"
        res_orphan = improver.apply_proposal("Actualiza orphan_mod.py", proposal_title="Orphan fix")
        assert res_orphan["applied"] is True
        assert len(recorded_pytest_cmds) == 1
        cmd_orphan, timeout_orphan = recorded_pytest_cmds[0]
        assert timeout_orphan >= 900.0
        assert Path(cmd_orphan[0]).stem.lower() == "pytest"
        assert cmd_orphan[1:] == ["-q", "--no-cov"]
    finally:
        store.close()


def test_factory_assistant_apply_targets_self_improver_tests_only_and_auto_repairs_once_on_failure(
    tmp_path: Path,
) -> None:
    """Editing src/agent_factory/self_improver.py targets only tests/test_self_improver.py (no full-suite fallback), consult prompts enforce atomic Paso 1, and failed verification triggers 1 auto-repair round (<=180s) updating live status."""
    repo_root = tmp_path / "repo_repair"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=repo_root, check=True)
    (repo_root / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "agent_factory" / "self_improver.py").write_text("X = 1\n", encoding="utf-8")
    (repo_root / "tests" / "test_self_improver.py").write_text("def test_si(): assert True\n", encoding="utf-8")
    (repo_root / "tests" / "test_unrelated.py").write_text("def test_other(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "chore: seed repair repo"], cwd=repo_root, check=True, capture_output=True)

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    agy_calls: list[dict[str, Any]] = []
    pytest_cmds: list[list[str]] = []
    repair_stages_seen: list[str] = []
    consult_prompts: list[str] = []
    pytest_attempt = [0]
    improver_holder: list[FactorySelfImprover] = []

    class _RepairSpyRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                prompt = command[command.index("-p") + 1]
                if "--dangerously-skip-permissions" not in command:
                    consult_prompts.append(prompt)
                    return CommandResult(
                        exit_code=0,
                        stdout=json.dumps({"reply": "Dividido", "new_memory_rules": [], "actionable_proposal": None}),
                    )
                agy_calls.append({"prompt": prompt, "timeout": timeout_seconds})
                if len(agy_calls) == 1:
                    (repo_root / "src" / "agent_factory" / "self_improver.py").write_text("X = 'bug'\n", encoding="utf-8")
                    return CommandResult(exit_code=0, stdout="initial edit")
                repair_stages_seen.append(improver_holder[0].get_status()["stage"])
                (repo_root / "src" / "agent_factory" / "self_improver.py").write_text("X = 'fixed'\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="repaired edit")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                pytest_attempt[0] += 1
                pytest_cmds.append(list(command))
                if pytest_attempt[0] == 1:
                    return CommandResult(exit_code=1, stdout="FAILED tests/test_self_improver.py - AssertionError: boom_repair")
                return CommandResult(exit_code=0, stdout="1 passed")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    store = SqliteEventStore(db_path=tmp_path / "repair.db")
    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_rep",
            runner=_RepairSpyRunner(),
            restart_fn=lambda _code: None,
            restart_delay_seconds=0.01,
        )
        improver_holder.append(improver)

        # Direct selector check: self_improver.py targets only tests/test_self_improver.py
        assert improver._select_targeted_pytest_targets(["src/agent_factory/self_improver.py"]) == (
            ["tests/test_self_improver.py"],
            True,
        )

        # Consult prompt enforces atomic Paso 1 splitting
        improver.consult_message("Haz muchos cambios en dashboard.py")
        assert len(consult_prompts) == 1
        assert "Paso 1" in consult_prompts[0]

        # Apply fails first pytest -> 1 repair round (300s) -> passes without rollback
        res = improver.apply_proposal("Actualiza self_improver.py", proposal_title="Auto-repair test")
        assert res["ok"] is True
        assert res["applied"] is True
        assert res["rolled_back"] is False
        assert len(agy_calls) == 2
        assert agy_calls[1]["timeout"] == 300.0
        assert "AssertionError: boom_repair" in agy_calls[1]["prompt"]
        assert repair_stages_seen == ["Auto-reparando fallo detectado..."]
        assert len(pytest_cmds) == 2
        for p_cmd in pytest_cmds:
            assert "tests/test_self_improver.py" in p_cmd
            assert "tests/test_unrelated.py" not in p_cmd
        assert (repo_root / "src" / "agent_factory" / "self_improver.py").read_text(encoding="utf-8") == "X = 'fixed'\n"
    finally:
        store.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_factory_evolution_history_git_versions_redaction_limit_and_http_endpoint(
    tmp_path: Path,
) -> None:
    """FactorySelfImprover.get_evolution_history and GET /api/factory/assistant/versions return real global v1.N versions on main, shortstat, secret redaction, ?limit=N, and 400 on invalid limit."""
    secret_tok = "EVO_SECRET_TOKEN_999"
    repo_root = tmp_path / "repo_evo"
    _init_temp_factory_git_repo(repo_root)

    # Commit 2: manual commit with body containing secret
    (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "feat(ui): agregar panel base",
            "-m",
            f"Detalle manual con secreto {secret_tok}",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # Commit 3: self-improve commit modifying 2 files with secret in subject and body
    (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 20\n", encoding="utf-8")
    (repo_root / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 20 == 20\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"chore(self-improve): Mejorar velocidad del panel {secret_tok}",
            "-m",
            "Optimiza consultas SQLite y evita re-render innecesario.",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # Commit 4: latest self-improve commit without body
    (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 30\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "chore(self-improve): Agregar historial de versiones",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    head_full = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head_short = subprocess.run(
        ["git", "rev-parse", "--short", "main"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    store = SqliteEventStore(db_path=tmp_path / "evo.db")
    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_evo",
            runner=SubprocessRunner(default_timeout_seconds=15.0),
            secrets_to_redact=(secret_tok,),
        )

        # Direct method check
        evo = improver.get_evolution_history()
        assert evo["current_version"] == "v1.4"
        assert evo["total_versions"] == 4
        versions = evo["versions"]
        assert len(versions) == 4
        assert [v["version"] for v in versions] == ["v1.4", "v1.3", "v1.2", "v1.1"]

        # Newest commit (v1.4 - self_improve, no body)
        v4 = versions[0]
        assert v4["version"] == "v1.4"
        assert v4["commit_hash"] == head_full
        assert v4["short_hash"] == head_short
        assert v4["committed_at"]
        assert v4["title"] == "Agregar historial de versiones"
        assert v4["raw_subject"] == "chore(self-improve): Agregar historial de versiones"
        assert v4["summary"] == ""
        assert v4["files_changed"] == 1
        assert "1 file changed" in v4["stats"]
        assert v4["is_self_improve"] is True
        assert v4["source"] == "self_improve"
        assert v4["source_label"] == "Auto-mejora"

        # Commit 3 (v1.3 - self_improve, 2 files changed, secret redacted)
        v3 = versions[1]
        assert v3["version"] == "v1.3"
        assert secret_tok not in v3["title"]
        assert secret_tok not in v3["raw_subject"]
        assert "[REDACTED]" in v3["title"]
        assert not v3["title"].lower().startswith("chore(self-improve):")
        assert v3["summary"] == "Optimiza consultas SQLite y evita re-render innecesario."
        assert v3["files_changed"] == 2
        assert "2 files changed" in v3["stats"]
        assert v3["is_self_improve"] is True
        assert v3["source"] == "self_improve"
        assert v3["source_label"] == "Auto-mejora"

        # Commit 2 (v1.2 - manual, secret redacted in summary)
        v2 = versions[2]
        assert v2["version"] == "v1.2"
        assert v2["title"] == "feat(ui): agregar panel base"
        assert v2["raw_subject"] == "feat(ui): agregar panel base"
        assert secret_tok not in v2["summary"]
        assert "[REDACTED]" in v2["summary"]
        assert v2["files_changed"] == 1
        assert v2["is_self_improve"] is False
        assert v2["source"] == "manual"
        assert v2["source_label"] == "Cambio manual"

        # Commit 1 (v1.1 - manual initial snapshot, 3 files changed)
        v1 = versions[3]
        assert v1["version"] == "v1.1"
        assert v1["title"] == "chore: initial snapshot"
        assert v1["files_changed"] == 3
        assert v1["is_self_improve"] is False
        assert v1["source"] == "manual"
        assert v1["source_label"] == "Cambio manual"

        # HTTP endpoint integration
        orch = Orchestrator(store=store, llm=FakeLLM())
        server, thread, base_url = start_dashboard_server(
            orchestrator=orch,
            port=0,
            self_improver=improver,
            secrets_to_redact=(secret_tok,),
        )
        try:
            c_all, r_all = _http_call(f"{base_url}/api/factory/assistant/versions")
            assert c_all == 200
            assert r_all["current_version"] == "v1.4"
            assert r_all["total_versions"] == 4
            assert [v["version"] for v in r_all["versions"]] == ["v1.4", "v1.3", "v1.2", "v1.1"]
            assert secret_tok not in json.dumps(r_all)

            c_lim, r_lim = _http_call(f"{base_url}/api/factory/assistant/versions?limit=2")
            assert c_lim == 200
            assert r_lim["current_version"] == "v1.4"
            assert r_lim["total_versions"] == 4
            assert len(r_lim["versions"]) == 2
            assert [v["version"] for v in r_lim["versions"]] == ["v1.4", "v1.3"]

            c_bad, r_bad = _http_call(f"{base_url}/api/factory/assistant/versions?limit=abc")
            assert c_bad == 400
            assert "error" in r_bad
        finally:
            server.shutdown()
            server.server_close()
            server.controller.wait_for_idle(timeout=5.0)
            if thread.is_alive():
                thread.join(timeout=2.0)
    finally:
        store.close()


def test_factory_evolution_history_degrades_safely_without_git_and_falls_back_to_head(
    tmp_path: Path,
) -> None:
    """get_evolution_history returns v1.0/empty on non-git dirs or empty repos, clamps limit, and falls back to HEAD when main does not exist."""
    no_git_dir = tmp_path / "not_a_git_repo"
    no_git_dir.mkdir(parents=True, exist_ok=True)
    improver_no_git = FactorySelfImprover(
        db_path=tmp_path / "nogit.db",
        repo_root=no_git_dir,
        uploads_dir=tmp_path / "uploads_nogit",
        runner=SubprocessRunner(default_timeout_seconds=15.0),
    )
    assert improver_no_git.get_evolution_history() == {
        "current_version": "v1.0",
        "total_versions": 0,
        "versions": [],
    }

    # Repo without 'main' branch (uses 'develop' -> fallback to HEAD)
    dev_repo = tmp_path / "dev_repo"
    dev_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "develop"], cwd=dev_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=dev_repo, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=dev_repo, check=True)

    # Empty repo (0 commits) degrades safely
    improver_dev = FactorySelfImprover(
        db_path=tmp_path / "dev.db",
        repo_root=dev_repo,
        uploads_dir=tmp_path / "uploads_dev",
        runner=SubprocessRunner(default_timeout_seconds=15.0),
    )
    assert improver_dev.get_evolution_history() == {
        "current_version": "v1.0",
        "total_versions": 0,
        "versions": [],
    }

    # Add 2 commits on develop
    (dev_repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=dev_repo, check=True)
    subprocess.run(["git", "commit", "-m", "chore: first"], cwd=dev_repo, check=True, capture_output=True)
    (dev_repo / "a.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=dev_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore(self-improve):"],
        cwd=dev_repo,
        check=True,
        capture_output=True,
    )

    res = improver_dev.get_evolution_history(limit=0)
    assert res["current_version"] == "v1.2"
    assert res["total_versions"] == 2
    assert len(res["versions"]) == 1
    assert res["versions"][0]["version"] == "v1.2"
    # Empty suffix after chore(self-improve): falls back to raw subject
    assert res["versions"][0]["title"] == "chore(self-improve):"
    assert res["versions"][0]["is_self_improve"] is True


def test_factory_assistant_auto_repair_prompt_compacts_long_pytest_trace_and_suggests_failed_nodes(
    tmp_path: Path,
) -> None:
    """AUTO-REPARACIÓN OBLIGATORIA compacts long pytest output and suggests re-running deduplicated failed node IDs first."""
    repo_root = tmp_path / "repo_compact_repair"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=repo_root, check=True)
    (repo_root / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "agent_factory" / "self_improver.py").write_text("X = 1\n", encoding="utf-8")
    (repo_root / "tests" / "test_self_improver.py").write_text("def test_si(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "chore: seed"], cwd=repo_root, check=True, capture_output=True)

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    repair_prompts: list[str] = []
    pytest_calls = [0]

    long_pytest_fail = (
        "\n".join(f"DEBUG SPAM_NOISE_{i}: filler output abcdefghijklmnopqrstuvwxyz" for i in range(180))
        + "\n"
        + "=================================== FAILURES ===================================\n"
        + ">       assert actual == 'expected_compacted_trace'\n"
        + "E       AssertionError: expected_compacted_trace\n"
        + "tests/test_self_improver.py:77: AssertionError\n"
        + "\n".join(f"DEBUG MID_SPAM_{i}: more filler 0123456789" for i in range(120))
        + "\n"
        + "=========================== short test summary info ============================\n"
        + "FAILED tests/test_self_improver.py::test_case_a - AssertionError: expected_compacted_trace\n"
        + "FAILED tests/test_self_improver.py::test_case_b - ValueError: bad\n"
        + "FAILED tests/test_self_improver.py::test_case_a - AssertionError: duplicate\n"
        + "2 failed, 10 passed in 3.50s\n"
    )

    class _CompactRepairRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                prompt = command[command.index("-p") + 1]
                repair_prompts.append(prompt)
                if len(repair_prompts) == 1:
                    (repo_root / "src" / "agent_factory" / "self_improver.py").write_text("X = 'bad'\n", encoding="utf-8")
                    return CommandResult(exit_code=0, stdout="initial")
                (repo_root / "src" / "agent_factory" / "self_improver.py").write_text("X = 'good'\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="repaired")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                pytest_calls[0] += 1
                if pytest_calls[0] == 1:
                    return CommandResult(exit_code=1, stdout=long_pytest_fail)
                return CommandResult(exit_code=0, stdout="2 passed")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    store = SqliteEventStore(db_path=tmp_path / "compact_si.db")
    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_cr",
            runner=_CompactRepairRunner(),
            restart_fn=lambda _code: None,
            restart_delay_seconds=0.01,
        )
        res = improver.apply_proposal("Actualiza self_improver.py", proposal_title="Compact trace repair")
        assert res["applied"] is True
        assert len(repair_prompts) == 2
        rep_prompt = repair_prompts[1]
        assert "AUTO-REPARACIÓN OBLIGATORIA" in rep_prompt
        assert (
            "pytest -q --no-cov tests/test_self_improver.py::test_case_a tests/test_self_improver.py::test_case_b"
            in rep_prompt
        )
        assert "AssertionError: expected_compacted_trace" in rep_prompt
        assert "2 failed, 10 passed" in rep_prompt
        assert "truncad" in rep_prompt.lower()
        assert "SPAM_NOISE_120" not in rep_prompt
        assert "MID_SPAM_60" not in rep_prompt
    finally:
        store.close()


def test_select_targeted_test_paths_root_subpackages_direct_unmatched_and_dedup() -> None:
    """select_targeted_test_paths maps root, adapters/agents/domain subpackages, and direct test edits deterministically with deduplication and empty fallback."""
    available_tests = [
        "tests/test_telemetry.py",
        "tests/test_orchestrator_state_machine.py",
        "tests/test_orchestrator_approval.py",
        "tests/test_agy_cli_adapter.py",
        "tests/test_model_resolver.py",
        "tests/test_interviewer.py",
        "tests/test_debate_mediator.py",
        "tests/test_states.py",
        "tests/test_progress_and_projection.py",
        "tests/adapters/test_sqlite_store.py",
        "tests/test_dashboard.py",
        "tests/conftest.py",
    ]

    # 1. Root package module (single and multiple matching test files, sorted)
    assert select_targeted_test_paths(
        ["src/agent_factory/telemetry.py"],
        available_tests,
    ) == ["tests/test_telemetry.py"]
    assert select_targeted_test_paths(
        ["src/agent_factory/orchestrator.py"],
        available_tests,
    ) == [
        "tests/test_orchestrator_approval.py",
        "tests/test_orchestrator_state_machine.py",
    ]

    # 2. Subpackage modules: adapters/, agents/, domain/
    assert select_targeted_test_paths(
        ["src/agent_factory/adapters/agy_cli_adapter.py"],
        available_tests,
    ) == ["tests/test_agy_cli_adapter.py"]
    assert select_targeted_test_paths(
        ["src/agent_factory/adapters/sqlite_store.py"],
        available_tests,
    ) == ["tests/adapters/test_sqlite_store.py"]
    assert select_targeted_test_paths(
        ["src/agent_factory/agents/interviewer.py"],
        available_tests,
    ) == ["tests/test_interviewer.py"]
    assert select_targeted_test_paths(
        ["src/agent_factory/agents/mediator.py"],
        available_tests,
    ) == ["tests/test_debate_mediator.py"]
    assert select_targeted_test_paths(
        ["src/agent_factory/domain/states.py"],
        available_tests,
    ) == ["tests/test_states.py"]
    assert select_targeted_test_paths(
        ["src/agent_factory/domain/progress.py"],
        available_tests,
    ) == ["tests/test_progress_and_projection.py"]

    # 3. Direct change in a test file (existing kept, missing ignored)
    assert select_targeted_test_paths(
        ["tests/test_dashboard.py"],
        available_tests,
    ) == ["tests/test_dashboard.py"]
    assert select_targeted_test_paths(
        ["tests/test_nonexistent.py"],
        available_tests,
    ) == []

    # 4. Files without associated test return empty list safely (and no substring false positives)
    assert select_targeted_test_paths(
        [
            "src/agent_factory/orphan_module.py",
            "src/agent_factory/__init__.py",
            "src/agent_factory/domain/models.py",
            "README.md",
            "../tests/test_dashboard.py",
        ],
        available_tests,
    ) == []
    assert select_targeted_test_paths([], available_tests) == []
    assert select_targeted_test_paths(["src/agent_factory/telemetry.py"], []) == []

    # 5. Deduplication, Windows backslash & ./ normalization, and deterministic sorting
    assert select_targeted_test_paths(
        [
            "src\\agent_factory\\domain\\states.py",
            "./src/agent_factory/domain/states.py",
            "tests/test_states.py",
            "src/agent_factory/adapters/agy_cli_adapter.py",
            "tests\\test_agy_cli_adapter.py",
            "src/agent_factory/agents/interviewer.py",
        ],
        [
            "tests\\test_states.py",
            "tests/test_states.py",
            "./tests/test_agy_cli_adapter.py",
            "tests/test_interviewer.py",
            "tests/test_dashboard.py",
        ],
    ) == [
        "tests/test_agy_cli_adapter.py",
        "tests/test_interviewer.py",
        "tests/test_states.py",
    ]


def test_factory_assistant_apply_uses_diff_selector_for_subpackages_and_respects_explicit_tests(
    tmp_path: Path,
) -> None:
    """Validation gate builds pytest -q --no-cov with diff-selected subpackage tests when no explicit list is provided, and honors explicit_tests when specified."""
    repo_root = tmp_path / "repo_subpkg_selector"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=repo_root, check=True)
    (repo_root / "src" / "agent_factory" / "domain").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "agent_factory" / "adapters").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "agent_factory" / "domain" / "states.py").write_text("S = 1\n", encoding="utf-8")
    (repo_root / "src" / "agent_factory" / "adapters" / "orphan_adapter.py").write_text("A = 1\n", encoding="utf-8")
    (repo_root / "tests" / "test_states.py").write_text("def test_s(): assert True\n", encoding="utf-8")
    (repo_root / "tests" / "test_unrelated.py").write_text("def test_u(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "chore: seed subpkg repo"], cwd=repo_root, check=True, capture_output=True)

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    recorded_pytest_cmds: list[list[str]] = []
    edit_target = ["src/agent_factory/domain/states.py"]

    class _SubpkgRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                (repo_root / edit_target[0]).write_text("UPDATED = True\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="ok")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                recorded_pytest_cmds.append(list(command))
                return CommandResult(exit_code=0, stdout="1 passed")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    store = SqliteEventStore(db_path=tmp_path / "subpkg.db")
    try:
        improver = FactorySelfImprover(
            sqlite_store=store,
            repo_root=repo_root,
            uploads_dir=tmp_path / "uploads_subpkg",
            runner=_SubpkgRunner(),
            restart_fn=lambda _code: None,
            restart_delay_seconds=0.01,
        )

        # 1. No explicit test list -> diff selector maps domain/states.py -> tests/test_states.py only
        res_diff = improver.apply_proposal("Actualiza states.py", proposal_title="States update")
        assert res_diff["applied"] is True
        assert len(recorded_pytest_cmds) == 1
        assert recorded_pytest_cmds[0][1:] == ["-q", "--no-cov", "tests/test_states.py"]

        # 2. Explicit test list specified -> honors explicit list even for orphan adapter
        recorded_pytest_cmds.clear()
        edit_target[0] = "src/agent_factory/adapters/orphan_adapter.py"
        res_explicit = improver.apply_proposal(
            "Actualiza orphan_adapter.py",
            proposal_title="Explicit test list",
            explicit_tests=["tests/test_unrelated.py"],
        )
        assert res_explicit["applied"] is True
        assert len(recorded_pytest_cmds) == 1
        assert recorded_pytest_cmds[0][1:] == ["-q", "--no-cov", "tests/test_unrelated.py"]
    finally:
        store.close()


class _FakeQuotaClock:
    """Deterministic Clock double for quota probe tests without real time.sleep."""

    def __init__(self, initial: Any) -> None:
        self._now = initial
        self.sleeps: list[float] = []

    def now(self) -> Any:
        return self._now

    def advance(self, seconds: float) -> Any:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=float(seconds))
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.advance(seconds)


def test_quota_meter_parse_reset_delay_and_state_transitions(tmp_path: Path) -> None:
    """QuotaMeter parses 'Resets in...', reports cooldown/window expiration, and resets pause restoring 100% balance."""
    from datetime import UTC, datetime

    from agent_factory.quota_meter import QuotaMeter, parse_reset_delay_seconds

    assert parse_reset_delay_seconds("HTTP 429 RESOURCE_EXHAUSTED Resets in 14m32s") == 872
    assert parse_reset_delay_seconds("Resets in 1h 20m 5s") == 4805
    assert parse_reset_delay_seconds("Reset in 2 hours 10 minutes") == 7800
    assert parse_reset_delay_seconds("HTTP 429 Too Many Requests") == 1800

    t0 = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)
    clock = _FakeQuotaClock(t0)
    meter = QuotaMeter(db_path=tmp_path / "telemetry_qm.db", clock=clock)

    initial = meter.peek_status()
    assert initial["is_paused"] is False
    assert initial["quota_remaining_pct"] == 100
    assert meter.is_paused() is False
    assert meter.is_window_expired() is False

    # Record 429 with explicit Resets in 20m0s -> paused until 12:20:00 UTC
    paused = meter.record_429_pause("HTTP 429 RESOURCE_EXHAUSTED Resets in 20m0s")
    assert paused["is_paused"] is True
    assert paused["quota_remaining_pct"] == 0
    assert paused["quota_paused_until"] == "2026-11-26T12:20:00+00:00"
    assert paused["reset_seconds"] == 1200
    assert meter.is_paused() is True
    assert meter.is_cooldown_active() is True
    assert meter.is_window_expired() is False

    # Update pause timer after another 429 at 12:10:00 with Resets in 45m30s -> 12:55:30 UTC
    clock.advance(600)
    updated = meter.record_429_pause("429 rate limit exceeded. Resets in 45m30s")
    assert updated["is_paused"] is True
    assert updated["quota_paused_until"] == "2026-11-26T12:55:30+00:00"
    assert updated["reset_seconds"] == 2730

    # Immediate reset clears cooldown and restores 100% cycle availability
    restored = meter.reset_pause()
    assert restored["is_paused"] is False
    assert restored["quota_paused_until"] is None
    assert restored["quota_remaining_pct"] == 100
    assert restored["window_tokens_used"] == 0
    assert restored["turns_used"] == 0
    assert meter.is_paused() is False


def test_quota_probe_recovery_retries_on_429_updates_timer_and_resumes_queue_on_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Periodic 600s quota probe updates timer & sleeps 600s on 429, then clears pause, logs exact bitacora message, and triggers queued idea immediately on success."""
    import logging
    import sqlite3
    from datetime import UTC, datetime

    from agent_factory import idea_engine, self_improver
    from agent_factory.quota_meter import QuotaMeter

    assert self_improver.DEFAULT_QUOTA_PROBE_INTERVAL_SECONDS == 600.0
    expected_log = "Cuota recuperada automáticamente; reanudando ciclo autónomo"

    db_path = tmp_path / "telemetry_probe.db"
    t0 = datetime(2026, 11, 26, 20, 0, 0, tzinfo=UTC)
    clock = _FakeQuotaClock(t0)
    meter = QuotaMeter(db_path=db_path, clock=clock)

    # Enable autonomous mode and seed a queued idea waiting for quota recovery
    idea_engine.set_autonomous_config(autonomous_enabled=True, db_path=db_path)
    saved = idea_engine.save_ideas(
        [
            {
                "title": "⚡ Reanudar cola tras cuota",
                "category": "Estabilidad",
                "impact": "Alto",
                "simple_description": "Prueba de reactivación automática de cuota.",
                "technical_prompt": "Verifica recuperación automática de cuota.",
            }
        ],
        source="auto",
        db_path=db_path,
    )
    idea_id = int(saved[0]["id"])
    idea_engine.update_idea_status(idea_id, "queued", db_path=db_path)

    # Enter active 429 quota pause (Resets in 20m0s -> 20:20:00 UTC)
    meter.record_429_pause("HTTP 429 RESOURCE_EXHAUSTED Resets in 20m0s", now=t0)
    assert meter.is_paused(now=t0) is True

    probe_cmds: list[list[str]] = []
    probe_responses = [
        CommandResult(
            exit_code=1,
            stderr="HTTP 429 RESOURCE_EXHAUSTED: Quota exceeded. Resets in 45m30s",
        ),
        CommandResult(exit_code=0, stdout="OK"),
    ]

    class _QuotaProbeRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            del cwd, timeout_seconds, env
            probe_cmds.append(list(command))
            return probe_responses.pop(0)

    executed_ideas: list[int] = []
    executed_event = threading.Event()
    recorded_waits: list[float] = []
    first_probe_timer_seen: list[str | None] = []

    def _fake_executor(idea: dict[str, Any]) -> dict[str, Any]:
        executed_ideas.append(int(idea["id"]))
        executed_event.set()
        return {"ok": True, "applied": True}

    def _fake_wait(seconds: float) -> bool:
        recorded_waits.append(float(seconds))
        if len(recorded_waits) == 2:
            # Verify state right after attempt 1 failed with 429 Resets in 45m30s at 20:10:00
            snap = meter.peek_status(now=clock.now())
            first_probe_timer_seen.append(snap["quota_paused_until"])
            assert snap["is_paused"] is True
            assert executed_ideas == []
        clock.advance(seconds)
        return False

    improver = FactorySelfImprover(
        db_path=tmp_path / "factory_probe.db",
        telemetry_db_path=db_path,
        repo_root=tmp_path,
        uploads_dir=tmp_path / "uploads_probe",
        runner=_QuotaProbeRunner(),
        clock=clock,
        quota_meter=meter,
        quota_probe_interval_seconds=600.0,
    )

    worker = self_improver.IdeaQueueWorker(
        db_path=db_path,
        executor=_fake_executor,
        improver=improver,
        quota_meter=meter,
        quota_probe_interval_seconds=600.0,
        poll_interval_seconds=0.01,
        clock=clock,
        wait_fn=_fake_wait,
    )

    with caplog.at_level(logging.INFO):
        worker.start()
        try:
            assert executed_event.wait(timeout=5.0) is True
            assert worker.wait_until_idle(timeout=5.0) is True
        finally:
            worker.stop(timeout=2.0)

    # Attempt 1 ran at 20:10:00 (after 600s) -> updated Resets in 45m30s -> 20:55:30 UTC and slept another 600s
    assert recorded_waits == [600.0, 600.0]
    assert first_probe_timer_seen == ["2026-11-26T20:55:30+00:00"]
    assert len(probe_cmds) == 2
    for cmd in probe_cmds:
        assert "--dangerously-skip-permissions" not in cmd

    # Attempt 2 succeeded at 20:20:00 (before 20:55:30!) -> cleared pause, logged exact message, ran queued idea
    assert meter.is_paused(now=clock.now()) is False
    final_status = meter.peek_status(now=clock.now())
    assert final_status["quota_paused_until"] is None
    assert final_status["quota_remaining_pct"] == 100
    assert final_status["window_tokens_used"] == 0
    assert executed_ideas == [idea_id]
    assert idea_engine.get_idea(idea_id, db_path=db_path)["status"] == "aplicada"  # type: ignore[index]

    assert expected_log in caplog.text
    chat_msgs = improver.memory_store.list_messages()
    assert any(m["content"] == expected_log for m in chat_msgs)

    conn = sqlite3.connect(str(db_path))
    try:
        ev_rows = conn.execute(
            "SELECT stage, command FROM execution_events WHERE stage = 'quota_recovery';"
        ).fetchall()
    finally:
        conn.close()
    assert any(row[1] == expected_log for row in ev_rows)


def test_quota_probe_recovery_resets_automatically_when_rolling_window_expires(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the quota meter's rolling window expires, probe_quota_recovery resets pause without calling the AI adapter, logs exact message, and triggers the queue."""
    import logging
    from datetime import UTC, datetime

    from agent_factory import idea_engine, self_improver
    from agent_factory.quota_meter import DEFAULT_QUOTA_WINDOW_SECONDS, QuotaMeter

    expected_log = "Cuota recuperada automáticamente; reanudando ciclo autónomo"
    db_path = tmp_path / "telemetry_window.db"
    t0 = datetime(2026, 11, 26, 10, 0, 0, tzinfo=UTC)
    clock = _FakeQuotaClock(t0)
    meter = QuotaMeter(db_path=db_path, clock=clock)

    # Exhaust rolling window to 0% remaining without an explicit 429 timestamp
    meter.record_usage(90000, 90000, now=t0)
    assert meter.peek_status(now=t0)["quota_remaining_pct"] == 0
    assert meter.is_paused(now=t0) is True
    assert meter.is_window_expired(now=t0) is False

    # Seed a queued idea waiting to run
    saved = idea_engine.save_ideas(
        [
            {
                "title": "🔄 Recuperar al expirar ventana móvil",
                "category": "Estabilidad",
                "impact": "Alto",
                "simple_description": "Prueba expiración de ventana móvil de 5h.",
                "technical_prompt": "Verifica recuperación al expirar ventana.",
            }
        ],
        source="auto",
        db_path=db_path,
    )
    idea_id = int(saved[0]["id"])
    idea_engine.update_idea_status(idea_id, "queued", db_path=db_path)

    ai_calls: list[list[str]] = []

    class _NeverCalledRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            del cwd, timeout_seconds, env
            ai_calls.append(list(command))
            raise AssertionError("AI probe should not be called when rolling window already expired")

    executed_ideas: list[int] = []
    executed_event = threading.Event()

    def _fake_executor(idea: dict[str, Any]) -> dict[str, Any]:
        executed_ideas.append(int(idea["id"]))
        executed_event.set()
        return {"ok": True, "applied": True}

    improver = FactorySelfImprover(
        db_path=tmp_path / "factory_window.db",
        telemetry_db_path=db_path,
        repo_root=tmp_path,
        uploads_dir=tmp_path / "uploads_window",
        runner=_NeverCalledRunner(),
        clock=clock,
        quota_meter=meter,
    )

    # Advance clock past 5h rolling window (18000s + 1s -> 15:00:01 UTC)
    clock.advance(DEFAULT_QUOTA_WINDOW_SECONDS + 1)
    assert meter.is_window_expired(now=clock.now()) is True

    try:
        with caplog.at_level(logging.INFO):
            res = improver.probe_quota_recovery(executor=_fake_executor)

        assert res["ok"] is True
        assert res["recovered"] is True
        assert res["reason"] == "window_expired"
        assert res["quota_paused"] is False
        assert res["queue_triggered"] is True
        assert res["log_message"] == expected_log
        assert ai_calls == []

        restored = meter.peek_status(now=clock.now())
        assert restored["is_paused"] is False
        assert restored["quota_remaining_pct"] == 100
        assert restored["window_tokens_used"] == 0
        assert expected_log in caplog.text

        assert executed_event.wait(timeout=5.0) is True
        worker = self_improver.get_idea_queue_worker()
        assert worker is not None
        assert worker.wait_until_idle(timeout=5.0) is True
        assert executed_ideas == [idea_id]
        assert idea_engine.get_idea(idea_id, db_path=db_path)["status"] == "aplicada"  # type: ignore[index]
    finally:
        self_improver.stop_idea_queue_worker(timeout=2.0)


def test_quota_probe_recovery_keeps_pause_on_transient_errors_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    """Non-quota transient probe errors do not falsely clear quota pause or crash the worker loop, and stop signal cancels cleanly."""
    from datetime import UTC, datetime

    from agent_factory import self_improver
    from agent_factory.quota_meter import QuotaMeter

    db_path = tmp_path / "telemetry_transient.db"
    t0 = datetime(2026, 11, 26, 22, 0, 0, tzinfo=UTC)
    clock = _FakeQuotaClock(t0)
    meter = QuotaMeter(db_path=db_path, clock=clock)
    meter.record_429_pause("HTTP 429 RESOURCE_EXHAUSTED Resets in 30m0s", now=t0)

    class _TransientErrorRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            del command, cwd, timeout_seconds, env
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Temporary network connection reset")
            return CommandResult(exit_code=1, stderr="503 Service Temporarily Unavailable")

    runner = _TransientErrorRunner()
    improver = FactorySelfImprover(
        db_path=tmp_path / "factory_transient.db",
        telemetry_db_path=db_path,
        repo_root=tmp_path,
        uploads_dir=tmp_path / "uploads_transient",
        runner=runner,
        clock=clock,
        quota_meter=meter,
        quota_probe_interval_seconds=600.0,
    )

    # Direct probe: raised exception -> keeps pause intact, does not crash
    res1 = improver.probe_quota_recovery()
    assert res1["ok"] is False
    assert res1["recovered"] is False
    assert res1["status"] == "transient_error"
    assert res1["quota_paused"] is True
    assert res1["next_probe_seconds"] == 600.0
    assert meter.is_paused(now=t0) is True
    assert meter.peek_status(now=t0)["quota_paused_until"] == "2026-11-26T22:30:00+00:00"

    # Second transient error inside IdeaQueueWorker loop -> loop survives and stops cleanly on stop()
    waits: list[float] = []
    two_waits_seen = threading.Event()
    worker_ref: list[self_improver.IdeaQueueWorker] = []

    def _wait_then_stop(seconds: float) -> bool:
        waits.append(float(seconds))
        if len(waits) >= 2:
            two_waits_seen.set()
            worker_ref[0]._stop_event.set()
            return True
        return False

    worker = self_improver.IdeaQueueWorker(
        db_path=db_path,
        improver=improver,
        quota_meter=meter,
        quota_probe_interval_seconds=600.0,
        poll_interval_seconds=0.01,
        clock=clock,
        wait_fn=_wait_then_stop,
    )
    worker_ref.append(worker)
    worker.start()
    try:
        assert two_waits_seen.wait(timeout=5.0) is True
    finally:
        worker.stop(timeout=2.0)

    assert worker.is_alive is False
    assert waits == [600.0, 600.0]
    assert runner.calls == 2
    assert meter.is_paused(now=t0) is True
    assert meter.peek_status(now=t0)["quota_paused_until"] == "2026-11-26T22:30:00+00:00"





