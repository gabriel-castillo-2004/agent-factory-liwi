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

            # Verify 600s timeouts on agy and verification commands
            agy_timeouts = [t for cmd, t in recorded if Path(cmd[0]).stem.lower() == "agy"]
            ruff_timeouts = [t for cmd, t in recorded if Path(cmd[0]).stem.lower() == "ruff"]
            pytest_timeouts = [t for cmd, t in recorded if Path(cmd[0]).stem.lower() == "pytest"]
            assert agy_timeouts == [600.0]
            assert ruff_timeouts == [600.0]
            assert pytest_timeouts == [600.0]

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
