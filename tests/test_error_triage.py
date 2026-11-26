"""TDD tests for Auto-Triage Step 1: triage_solutions persistence and error_triage worker."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_factory import error_triage
from agent_factory.error_triage import (
    _analyze_error_with_agy,
    compute_error_signature,
    report_factory_error,
    wait_for_triage_tasks,
)
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.telemetry import (
    get_triage_solution,
    init_telemetry_db,
    list_triage_solutions,
    update_triage_solution,
    upsert_triage_error,
)


class _FakeRunner(Runner):
    """In-memory fake Runner for testing read-only agy.EXE invocation without spawning processes."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
        input_str: str | None = None,
    ) -> CommandResult:
        self.calls.append(
            {
                "command": list(command),
                "cwd": Path(cwd),
                "timeout_seconds": timeout_seconds,
                "env": dict(env) if env is not None else None,
                "input_str": input_str,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return CommandResult(
            command=tuple(command),
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_triage_solutions_schema_and_unique_signature_constraint(tmp_path: Path) -> None:
    """init_telemetry_db creates triage_solutions with exact columns and UNIQUE error_signature."""
    db_path = tmp_path / "telemetry.db"
    assert init_telemetry_db(db_path=db_path) is True

    conn = sqlite3.connect(str(db_path))
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(triage_solutions);").fetchall()]
        assert cols == [
            "id",
            "created_at",
            "error_signature",
            "scope",
            "title",
            "raw_error",
            "root_cause",
            "proposed_fix",
            "technical_prompt",
            "status",
            "occurrences",
        ]

        conn.execute(
            """
            INSERT INTO triage_solutions (
                created_at, error_signature, scope, raw_error
            ) VALUES ('2026-11-26T12:00:00+00:00', 'sig-unique-001', 'factory', 'KeyError: missing')
            """
        )
        conn.commit()

        row = conn.execute(
            "SELECT status, occurrences, title, root_cause, proposed_fix, technical_prompt "
            "FROM triage_solutions WHERE error_signature = 'sig-unique-001'"
        ).fetchone()
        assert row == ("analizando", 1, "", "", "", "")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO triage_solutions (
                    created_at, error_signature, scope, raw_error
                ) VALUES ('2026-11-26T12:01:00+00:00', 'sig-unique-001', 'factory', 'KeyError: duplicate')
                """
            )
            conn.commit()
    finally:
        conn.close()


def test_telemetry_triage_helpers_upsert_update_list_and_get(tmp_path: Path) -> None:
    """Testable telemetry helpers handle upsert, increment, status updates, listing, and lookup."""
    db_path = tmp_path / "telemetry.db"

    created, is_new = upsert_triage_error(
        "sig-helper-1",
        "AttributeError: 'NoneType' object has no attribute 'get'",
        scope="factory",
        db_path=db_path,
    )
    assert is_new is True
    assert created is not None
    assert created["id"] == 1
    assert created["error_signature"] == "sig-helper-1"
    assert created["scope"] == "factory"
    assert created["status"] == "analizando"
    assert created["occurrences"] == 1

    duplicate, is_new_dup = upsert_triage_error(
        "sig-helper-1",
        "AttributeError: 'NoneType' object has no attribute 'get'",
        scope="factory",
        db_path=db_path,
    )
    assert is_new_dup is False
    assert duplicate is not None
    assert duplicate["id"] == 1
    assert duplicate["occurrences"] == 2

    updated = update_triage_solution(
        1,
        title="Acceso a atributo sobre None en dashboard",
        root_cause="El payload llega como None cuando no hay estado.",
        proposed_fix="Validar que el payload sea dict antes de llamar .get().",
        technical_prompt="Agrega prueba TDD para payload None y valida tipo dict.",
        status="lista",
        db_path=db_path,
    )
    assert updated is not None
    assert updated["title"] == "Acceso a atributo sobre None en dashboard"
    assert updated["root_cause"] == "El payload llega como None cuando no hay estado."
    assert updated["proposed_fix"] == "Validar que el payload sea dict antes de llamar .get()."
    assert updated["technical_prompt"] == "Agrega prueba TDD para payload None y valida tipo dict."
    assert updated["status"] == "lista"
    assert updated["occurrences"] == 2

    fetched = get_triage_solution(1, db_path=db_path)
    assert fetched == updated
    assert get_triage_solution(999, db_path=db_path) is None

    all_items = list_triage_solutions(db_path=db_path)
    assert len(all_items) == 1
    assert all_items[0]["id"] == 1

    with pytest.raises(ValueError):
        update_triage_solution(1, status="estado_invalido", db_path=db_path)


def test_compute_error_signature_normalizes_whitespace_paths_and_numbers() -> None:
    """Equivalent errors with different paths, line numbers, hex addresses, or spaces share a signature."""
    sig_a = compute_error_signature(
        'File "C:\\Users\\Alice\\repo\\src\\agent_factory\\builder.py", line 104, in run\n'
        "  KeyError: 'status' at 0x7ff012ab34 (pid 4120)",
        source_file="C:\\Users\\Alice\\repo\\src\\agent_factory\\builder.py",
    )
    sig_b = compute_error_signature(
        'File "/opt/ci/work/src/agent_factory/builder.py", line 289, in run   '
        "KeyError: 'status' at 0x1000deadbeef (pid 9876)",
        source_file="src/agent_factory/builder.py",
    )
    sig_other_err = compute_error_signature(
        "ValueError: invalid json payload",
        source_file="src/agent_factory/builder.py",
    )
    sig_other_file = compute_error_signature(
        'File "C:\\Users\\Alice\\repo\\src\\agent_factory\\dashboard.py", line 104, in run\n'
        "  KeyError: 'status' at 0x7ff012ab34 (pid 4120)",
        source_file="src/agent_factory/dashboard.py",
    )

    assert len(sig_a) == 64
    assert sig_a == sig_b
    assert sig_a != sig_other_err
    assert sig_a != sig_other_file


def test_report_factory_error_deduplicates_and_analyzes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reporting the same error multiple times increments occurrences and invokes analyzer only once."""
    db_path = tmp_path / "telemetry.db"
    calls: list[tuple[str, str | None]] = []

    def fake_analyze(raw_error: str, source_file: str | None = None) -> dict[str, str]:
        calls.append((raw_error, source_file))
        return {
            "title": "Fallo de clave faltante en builder",
            "root_cause": "La clave 'plan' no estaba inicializada en el diccionario.",
            "proposed_fix": "Usar .get('plan') con valor por defecto seguro.",
            "technical_prompt": "Escribe prueba TDD en tests/test_builder_loop.py y valida clave 'plan'.",
        }

    monkeypatch.setattr(error_triage, "_analyze_error_with_agy", fake_analyze)

    first = report_factory_error(
        'File "C:\\dev\\src\\agent_factory\\builder.py", line 42\nKeyError: \'plan\' (pid 1001)',
        scope="factory",
        source_file="src/agent_factory/builder.py",
        db_path=db_path,
    )
    second = report_factory_error(
        'File "D:\\ci\\src\\agent_factory\\builder.py", line 99\nKeyError: \'plan\' (pid 2002)',
        scope="factory",
        source_file="src/agent_factory/builder.py",
        db_path=db_path,
    )
    third = report_factory_error(
        'File "/tmp/x/src/agent_factory/builder.py", line 101   KeyError: \'plan\' (pid 3003)',
        scope="factory",
        source_file="src/agent_factory/builder.py",
        db_path=db_path,
    )

    assert wait_for_triage_tasks(timeout=5.0) is True
    assert first is not None and second is not None and third is not None
    assert first["id"] == second["id"] == third["id"]
    assert len(calls) == 1

    stored = get_triage_solution(first["id"], db_path=db_path)
    assert stored is not None
    assert stored["occurrences"] == 3
    assert stored["status"] == "lista"
    assert stored["title"] == "Fallo de clave faltante en builder"
    assert stored["root_cause"] == "La clave 'plan' no estaba inicializada en el diccionario."
    assert stored["proposed_fix"] == "Usar .get('plan') con valor por defecto seguro."
    assert stored["technical_prompt"] == "Escribe prueba TDD en tests/test_builder_loop.py y valida clave 'plan'."


def test_report_factory_error_scope_filtering_workspace_vs_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope='workspace' never invokes agy.EXE; scope='factory' analyzes and marks status='lista'."""
    db_path = tmp_path / "telemetry.db"
    analyzer_calls: list[str] = []

    def fake_analyze(raw_error: str, source_file: str | None = None) -> dict[str, str]:
        analyzer_calls.append(raw_error)
        return {
            "title": "Error interno en orchestrator",
            "root_cause": "Estado nulo al calcular progreso.",
            "proposed_fix": "Validar evento vacío antes de proyectar estado.",
            "technical_prompt": "Añade test TDD en tests/test_orchestrator.py para evento vacío.",
        }

    monkeypatch.setattr(error_triage, "_analyze_error_with_agy", fake_analyze)

    ws_rec = report_factory_error(
        "ModuleNotFoundError: No module named 'flask'",
        scope="workspace",
        source_file="workspaces/demo/app.py",
        db_path=db_path,
    )
    assert wait_for_triage_tasks(timeout=5.0) is True
    assert ws_rec is not None
    assert ws_rec["scope"] == "workspace"
    assert ws_rec["status"] == "descartada"
    assert analyzer_calls == []

    fac_rec = report_factory_error(
        "RuntimeError: unexpected empty projection in orchestrator",
        scope="factory",
        source_file="src/agent_factory/orchestrator.py",
        db_path=db_path,
    )
    assert wait_for_triage_tasks(timeout=5.0) is True
    assert fac_rec is not None
    assert len(analyzer_calls) == 1

    stored_fac = get_triage_solution(fac_rec["id"], db_path=db_path)
    assert stored_fac is not None
    assert stored_fac["status"] == "lista"
    assert stored_fac["title"] == "Error interno en orchestrator"
    assert stored_fac["root_cause"] == "Estado nulo al calcular progreso."
    assert stored_fac["proposed_fix"] == "Validar evento vacío antes de proyectar estado."
    assert stored_fac["technical_prompt"] == "Añade test TDD en tests/test_orchestrator.py para evento vacío."


def test_single_worker_queue_executes_distinct_errors_sequentially(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple distinct factory errors execute with max concurrency == 1 in the background worker."""
    db_path = tmp_path / "telemetry.db"
    state_lock = threading.Lock()
    active_workers = 0
    max_observed_concurrency = 0
    processed_order: list[str] = []

    def slow_fake_analyze(raw_error: str, source_file: str | None = None) -> dict[str, str]:
        nonlocal active_workers, max_observed_concurrency
        with state_lock:
            active_workers += 1
            max_observed_concurrency = max(max_observed_concurrency, active_workers)
        try:
            time.sleep(0.05)
            processed_order.append(raw_error)
            return {
                "title": f"Diagnostico: {raw_error}",
                "root_cause": f"Causa de {raw_error}",
                "proposed_fix": f"Fix para {raw_error}",
                "technical_prompt": f"Prompt TDD para {raw_error}",
            }
        finally:
            with state_lock:
                active_workers -= 1

    monkeypatch.setattr(error_triage, "_analyze_error_with_agy", slow_fake_analyze)

    distinct_errors = [
        "ZeroDivisionError: division by zero in metrics",
        "IndexError: list index out of range in queue",
        "TypeError: unsupported operand type in planner",
        "KeyError: missing project_id in event store",
    ]
    ids: list[int] = []
    for err in distinct_errors:
        rec = report_factory_error(
            err,
            scope="factory",
            source_file="src/agent_factory/orchestrator.py",
            db_path=db_path,
        )
        assert rec is not None
        ids.append(rec["id"])

    assert wait_for_triage_tasks(timeout=10.0) is True
    assert max_observed_concurrency == 1
    assert processed_order == distinct_errors

    for sol_id in ids:
        sol = get_triage_solution(sol_id, db_path=db_path)
        assert sol is not None
        assert sol["status"] == "lista"
        assert sol["title"] != ""
        assert sol["root_cause"] != ""
        assert sol["proposed_fix"] != ""
        assert sol["technical_prompt"] != ""


def test_analyze_error_with_agy_readonly_command_json_parsing_and_timeout_resilience(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_analyze_error_with_agy runs read-only CLI flags, parses JSON, and handles timeouts safely."""
    json_payload = json.dumps(
        {
            "title": "Fallo al leer snapshot SQLite",
            "root_cause": "Conexión cerrada antes del commit.",
            "proposed_fix": "Asegurar uso de context manager en transacción.",
            "technical_prompt": "Añade test TDD para transacción cerrada en telemetry.py.",
        }
    )
    ok_runner = _FakeRunner(stdout=f"```json\n{json_payload}\n```", exit_code=0)

    result = _analyze_error_with_agy(
        "sqlite3.ProgrammingError: Cannot operate on a closed database.",
        source_file="src/agent_factory/telemetry.py",
        runner=ok_runner,
        agy_command=("agy.EXE",),
        timeout_seconds=30.0,
    )
    assert result == {
        "title": "Fallo al leer snapshot SQLite",
        "root_cause": "Conexión cerrada antes del commit.",
        "proposed_fix": "Asegurar uso de context manager en transacción.",
        "technical_prompt": "Añade test TDD para transacción cerrada en telemetry.py.",
    }
    assert len(ok_runner.calls) == 1
    invoked_cmd = ok_runner.calls[0]["command"]
    assert "-p" in invoked_cmd
    assert "--dangerously-skip-permissions" not in invoked_cmd
    assert "--mode" not in invoked_cmd
    prompt_arg = invoked_cmd[invoked_cmd.index("-p") + 1]
    assert "SOLO LECTURA" in prompt_arg.upper()
    assert "src/agent_factory/telemetry.py" in prompt_arg

    timeout_runner = _FakeRunner(raise_exc=CommandTimeoutError("agy timed out after 30s"))
    assert (
        _analyze_error_with_agy(
            "TimeoutError: slow call",
            source_file="src/agent_factory/builder.py",
            runner=timeout_runner,
            agy_command=("agy.EXE",),
            timeout_seconds=1.0,
        )
        is None
    )

    # Worker thread survives an exception in analyzer and keeps processing later errors.
    db_path = tmp_path / "telemetry.db"
    attempt = {"count": 0}

    def flaky_analyze(raw_error: str, source_file: str | None = None) -> dict[str, str]:
        attempt["count"] += 1
        if attempt["count"] == 1:
            raise RuntimeError("simulated worker boom")
        return {
            "title": "Recuperado tras fallo previo",
            "root_cause": "Causa del segundo error.",
            "proposed_fix": "Fix del segundo error.",
            "technical_prompt": "Prompt del segundo error.",
        }

    monkeypatch.setattr(error_triage, "_analyze_error_with_agy", flaky_analyze)

    first = report_factory_error("First exploding error", scope="factory", db_path=db_path)
    second = report_factory_error("Second healthy error", scope="factory", db_path=db_path)
    assert wait_for_triage_tasks(timeout=5.0) is True
    assert first is not None and second is not None

    first_row = get_triage_solution(first["id"], db_path=db_path)
    second_row = get_triage_solution(second["id"], db_path=db_path)
    assert first_row is not None and first_row["status"] == "analizando"
    assert second_row is not None and second_row["status"] == "lista"
    assert second_row["title"] == "Recuperado tras fallo previo"


def test_report_factory_error_never_propagates_unhandled_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report_factory_error swallows unexpected internal failures and returns None."""

    def broken_upsert(*args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, bool]:
        raise RuntimeError("unexpected sqlite catastrophe")

    monkeypatch.setattr(error_triage, "upsert_triage_error", broken_upsert)

    result = report_factory_error(
        "ValueError: boom",
        scope="factory",
        source_file="src/agent_factory/dashboard.py",
        db_path=tmp_path / "telemetry.db",
    )
    assert result is None


def test_build_actionable_fix_ticket_locates_suspect_file_deduplicates_and_updates_status(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry_tickets.db"
    t1 = error_triage.build_actionable_fix_ticket(
        "browser_js",
        "TypeError: Cannot read properties of null at src/agent_factory/dashboard.py:1234",
        context={"url": "http://127.0.0.1:8000/"},
        db_path=db_path,
    )
    assert t1["status"] == "pendiente"
    assert t1["occurrences"] == 1
    assert t1["deduplicated"] is False
    assert t1["affected_file"] == "src/agent_factory/dashboard.py"
    assert t1["affected_lines"] == "1234"
    assert "interfaz del navegador" in t1["simple_summary"]
    assert "src/agent_factory/dashboard.py:1234" in t1["repair_prompt"]
    assert "ruff check src/agent_factory/dashboard.py" in t1["repair_prompt"]

    # Repeated error deduplicates and increments occurrences
    t2 = error_triage.build_actionable_fix_ticket(
        "browser_js",
        "TypeError: Cannot read properties of null at src/agent_factory/dashboard.py:1234",
        db_path=db_path,
    )
    assert t2["id"] == t1["id"]
    assert t2["occurrences"] == 2
    assert t2["deduplicated"] is True
    assert len(error_triage.list_triage_tickets(status="pendiente", db_path=db_path)) == 1

    # Resolving hides from pending queue
    resolved = error_triage.update_triage_ticket_status(t1["id"], "resuelto", db_path=db_path)
    assert resolved["status"] == "resuelto"
    assert error_triage.list_triage_tickets(status="pendiente", db_path=db_path) == []


def test_supervisor_startup_watchdog_rolls_back_bad_commit_and_records_ticket(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    from agent_factory.supervisor import RESTART_EXIT_CODE, _run_child_with_watchdog

    repo = tmp_path / "repo_watchdog"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("OK = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "good commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    good_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    (repo / "app.py").write_text("BROKEN\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "bad commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    db_path = tmp_path / "watchdog_telemetry.db"
    exit_code, did_rollback = _run_child_with_watchdog(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('SyntaxError in src/agent_factory/dashboard.py:99\\n'); sys.exit(1)",
        ],
        health_url="http://127.0.0.1:8000/api/autonomous/status",
        startup_timeout_seconds=0.8,
        health_check_fn=lambda _u: False,
        sleep_fn=lambda _s: None,
        repo_root=repo,
        db_path=db_path,
        allow_rollback=True,
    )
    assert did_rollback is True
    assert exit_code == RESTART_EXIT_CODE
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head_after == good_sha

    tickets = error_triage.list_triage_tickets(status="pendiente", db_path=db_path)
    assert len(tickets) == 1
    assert tickets[0]["source"] == "startup_watchdog"
    assert tickets[0]["affected_file"] == "src/agent_factory/dashboard.py"


def test_error_triage_solution_listing_and_decision_helpers(tmp_path: Path) -> None:
    """error_triage exposes listing and approve/dismiss decision helpers with strict validation."""
    db_path = tmp_path / "telemetry_solutions.db"
    first, _ = upsert_triage_error(
        "sig-sol-1",
        "KeyError: 'plan' in builder.py",
        scope="factory",
        db_path=db_path,
    )
    second, _ = upsert_triage_error(
        "sig-sol-2",
        "ValueError: invalid json in dashboard.py",
        scope="factory",
        db_path=db_path,
    )
    assert first is not None and second is not None
    update_triage_solution(
        first["id"],
        title="Validar clave plan en builder",
        proposed_fix="Usar .get('plan') con valor por defecto.",
        status="lista",
        db_path=db_path,
    )
    update_triage_solution(
        second["id"],
        title="Validar JSON en dashboard",
        proposed_fix="Capturar JSONDecodeError de forma explícita.",
        status="lista",
        db_path=db_path,
    )

    ready = error_triage.list_triage_solutions(status="lista", db_path=db_path)
    assert len(ready) == 2
    assert {item["id"] for item in ready} == {first["id"], second["id"]}

    approved = error_triage.decide_triage_solution(first["id"], "aprobada", db_path=db_path)
    assert approved["id"] == first["id"]
    assert approved["status"] == "encolada"

    dismissed = error_triage.decide_triage_solution(second["id"], "descartada", db_path=db_path)
    assert dismissed["id"] == second["id"]
    assert dismissed["status"] == "descartada"

    assert error_triage.list_triage_solutions(status="lista", db_path=db_path) == []
    assert len(error_triage.list_triage_solutions(status="encolada", db_path=db_path)) == 1
    assert len(error_triage.list_triage_solutions(status="descartada", db_path=db_path)) == 1

    with pytest.raises(KeyError):
        error_triage.decide_triage_solution(99999, "aprobada", db_path=db_path)

    with pytest.raises(ValueError):
        error_triage.decide_triage_solution(first["id"], "accion_invalida", db_path=db_path)


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_triage_solutions_endpoints_and_ui_block(tmp_path: Path) -> None:
    """Dashboard exposes GET /api/triage/solutions, POST decision (200/400/404), and compact UI block."""
    import urllib.error
    import urllib.request

    from agent_factory.adapters.fake_llm import FakeLLM
    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.dashboard import start_dashboard_server
    from agent_factory.orchestrator import Orchestrator

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _http_json(
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    store = SqliteEventStore(db_path=tmp_path / "dash_triage.db")
    telemetry_db = tmp_path / "telemetry.db"
    sol_a, _ = upsert_triage_error(
        "sig-http-a",
        "RuntimeError: socket reset in orchestrator",
        scope="factory",
        db_path=telemetry_db,
    )
    sol_b, _ = upsert_triage_error(
        "sig-http-b",
        "KeyError: missing stage in dashboard",
        scope="factory",
        db_path=telemetry_db,
    )
    sol_c, _ = upsert_triage_error(
        "sig-http-c",
        "TimeoutError: pending analysis",
        scope="factory",
        db_path=telemetry_db,
    )
    assert sol_a is not None and sol_b is not None and sol_c is not None
    update_triage_solution(
        sol_a["id"],
        title="Manejar reset de socket en orchestrator",
        proposed_fix="Reintentar conexión con backoff acotado.",
        status="lista",
        db_path=telemetry_db,
    )
    update_triage_solution(
        sol_b["id"],
        title="Validar clave stage en dashboard",
        proposed_fix="Usar .get('stage') seguro.",
        status="lista",
        db_path=telemetry_db,
    )

    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        # 1) Unfiltered list returns all 3 rows and pending_count == 2 ('lista')
        code, payload = _http_json(f"{base_url}/api/triage/solutions")
        assert code == 200
        assert payload["ok"] is True
        assert payload["pending_count"] == 2
        assert len(payload["solutions"]) == 3

        # 2) Filtered list by status='lista' returns only the 2 ready solutions
        code_f, payload_f = _http_json(f"{base_url}/api/triage/solutions?status=lista")
        assert code_f == 200
        assert payload_f["status_filter"] == "lista"
        assert payload_f["pending_count"] == 2
        assert {item["id"] for item in payload_f["solutions"]} == {sol_a["id"], sol_b["id"]}

        # 3) Invalid status filter returns 400
        code_bad_st, _ = _http_json(f"{base_url}/api/triage/solutions?status=estado_desconocido")
        assert code_bad_st == 400

        # 4) Approve solution A -> status becomes 'encolada', pending_count drops to 1
        code_ok, res_ok = _http_json(
            f"{base_url}/api/triage/solutions/{sol_a['id']}/decision",
            method="POST",
            body={"decision": "aprobada"},
        )
        assert code_ok == 200
        assert res_ok["ok"] is True
        assert res_ok["status"] == "encolada"
        assert res_ok["solution"]["status"] == "encolada"
        assert res_ok["pending_count"] == 1

        # 5) Dismiss solution B -> status becomes 'descartada', pending_count drops to 0
        code_dis, res_dis = _http_json(
            f"{base_url}/api/triage/solutions/{sol_b['id']}/decision",
            method="POST",
            body={"decision": "descartada"},
        )
        assert code_dis == 200
        assert res_dis["ok"] is True
        assert res_dis["status"] == "descartada"
        assert res_dis["solution"]["status"] == "descartada"
        assert res_dis["pending_count"] == 0

        # Verify persisted list filtered by status='lista' is now empty
        code_empty, payload_empty = _http_json(f"{base_url}/api/triage/solutions?status=lista")
        assert code_empty == 200
        assert payload_empty["solutions"] == []
        assert payload_empty["pending_count"] == 0

        # 6) Nonexistent ID returns 404
        code_404, err_404 = _http_json(
            f"{base_url}/api/triage/solutions/999999/decision",
            method="POST",
            body={"decision": "aprobada"},
        )
        assert code_404 == 404
        assert "error" in err_404

        # 7) Invalid decision or invalid ID returns 400
        code_400_dec, _ = _http_json(
            f"{base_url}/api/triage/solutions/{sol_a['id']}/decision",
            method="POST",
            body={"decision": "invalida"},
        )
        assert code_400_dec == 400

        code_400_id, _ = _http_json(
            f"{base_url}/api/triage/solutions/not-an-int/decision",
            method="POST",
            body={"decision": "aprobada"},
        )
        assert code_400_id == 400

        # 8) Dashboard HTML includes compact Auto-Triage solutions block & counter
        with opener.open(f"{base_url}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html_text = html_resp.read().decode("utf-8")
        assert 'id="triage-solutions-banner"' in html_text
        assert 'id="triage-solutions-count"' in html_text
        assert "/api/triage/solutions" in html_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        store.close()


