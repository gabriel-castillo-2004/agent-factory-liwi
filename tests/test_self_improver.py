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
from agent_factory.self_improver import FactoryMemoryStore, FactorySelfImprover
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



