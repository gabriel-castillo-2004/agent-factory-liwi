"""TDD tests for local SQLite WAL telemetry module and GET /api/telemetry/summary."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.dashboard import start_dashboard_server
from agent_factory.orchestrator import Orchestrator
from agent_factory.telemetry import (
    CANONICAL_COMPONENTS,
    classify_error,
    format_telemetry_context,
    get_telemetry_summary,
    init_telemetry_db,
    record_execution_event,
    record_incident,
    record_safe_event,
    record_safe_incident,
)

_LOOPBACK_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, *, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, method="GET")
    try:
        with _LOOPBACK_HTTP_OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_init_telemetry_db_creates_parent_dirs_wal_mode_and_exact_schema(tmp_path: Path) -> None:
    """Creating the telemetry DB initializes missing parent directories, WAL mode, and exact tables/columns."""
    db_path = tmp_path / "nested" / "subdir" / "telemetry.db"
    assert not db_path.parent.exists()

    init_telemetry_db(db_path=db_path)

    assert db_path.is_file()
    conn = sqlite3.connect(str(db_path))
    try:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(journal_mode).lower() == "wal"

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        assert "execution_events" in tables
        assert "incidents" in tables

        exec_cols = [row[1] for row in conn.execute("PRAGMA table_info(execution_events);").fetchall()]
        assert exec_cols == [
            "id",
            "timestamp",
            "project_id",
            "component",
            "stage",
            "command",
            "duration_ms",
            "exit_code",
            "timed_out",
            "error_signature",
        ]

        inc_cols = [row[1] for row in conn.execute("PRAGMA table_info(incidents);").fetchall()]
        assert inc_cols == [
            "id",
            "timestamp",
            "project_id",
            "stage",
            "error_signature",
            "raw_error",
            "root_cause",
            "fix_summary",
            "resolved",
        ]
    finally:
        conn.close()


def test_record_execution_event_and_incident_defaults_and_auto_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording events and incidents populates UTC ISO timestamps, default flags, and auto error_signature."""
    db_path = tmp_path / "telemetry.db"
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(db_path))

    ev_ok_id = record_execution_event(
        component="tests",
        stage="unit_tests",
        command="pytest -q",
        duration_ms=420,
        exit_code=0,
        project_id="proj-alpha",
    )
    assert isinstance(ev_ok_id, int) and ev_ok_id > 0

    ev_timeout_id = record_execution_event(
        component="git",
        stage="git_push",
        command="git push origin main",
        duration_ms=30000,
        exit_code=124,
        timed_out=True,
        project_id="proj-alpha",
    )
    assert isinstance(ev_timeout_id, int) and ev_timeout_id > ev_ok_id

    ev_err_id = record_execution_event(
        component="tests",
        stage="import_check",
        command="pytest tests/test_app.py",
        duration_ms=150,
        exit_code=1,
        error_text="ModuleNotFoundError: No module named 'pydantic_extra'",
        project_id="proj-beta",
    )
    assert isinstance(ev_err_id, int)

    inc_id = record_incident(
        stage="build_verify",
        raw_error="SyntaxError: invalid syntax in src/app.py line 12",
        project_id="proj-beta",
        root_cause="Trailing comma in older syntax",
    )
    assert isinstance(inc_id, int) and inc_id > 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM execution_events ORDER BY id ASC").fetchall()
        assert len(rows) == 3

        # Check UTC ISO timestamp & default timed_out=0, no error_signature on success
        ts_0 = datetime.fromisoformat(rows[0]["timestamp"])
        assert ts_0.tzinfo is not None
        assert rows[0]["timed_out"] == 0
        assert rows[0]["error_signature"] is None
        assert rows[0]["project_id"] == "proj-alpha"

        # Auto-classified git timeout
        assert rows[1]["timed_out"] == 1
        assert rows[1]["error_signature"] == "git_timeout"

        # Auto-classified missing module from error_text
        assert rows[2]["error_signature"] == "missing_module"

        inc_rows = conn.execute("SELECT * FROM incidents ORDER BY id ASC").fetchall()
        assert len(inc_rows) == 1
        inc_ts = datetime.fromisoformat(inc_rows[0]["timestamp"])
        assert inc_ts.tzinfo is not None
        assert inc_rows[0]["resolved"] == 0
        assert inc_rows[0]["error_signature"] == "syntax_or_lint"
        assert inc_rows[0]["root_cause"] == "Trailing comma in older syntax"
        assert inc_rows[0]["fix_summary"] is None
    finally:
        conn.close()


def test_telemetry_is_fault_tolerant_when_db_unwritable_or_sqlite_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry recording and summary never raise when the DB path is unwritable or SQLite errors occur."""
    # Point db_path to an existing directory so SQLite cannot open it as a file
    bad_path = tmp_path / "is_a_directory"
    bad_path.mkdir()

    assert (
        record_execution_event(
            component="orchestrator",
            stage="debate",
            duration_ms=100,
            db_path=bad_path,
        )
        is None
    )
    assert (
        record_incident(
            stage="debate",
            raw_error="boom",
            db_path=bad_path,
        )
        is None
    )
    summary = get_telemetry_summary(db_path=bad_path)
    assert isinstance(summary, dict)
    assert summary["slowest_executions"] == []
    assert summary["recent_incidents"] == []

    # Simulate sqlite3.connect throwing OperationalError
    def _broken_connect(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("simulated disk full")

    monkeypatch.setattr(sqlite3, "connect", _broken_connect)
    assert (
        record_execution_event(
            component="doctor",
            stage="diagnose",
            duration_ms=50,
            db_path=tmp_path / "ok.db",
        )
        is None
    )
    assert (
        record_incident(
            stage="diagnose",
            raw_error="disk error",
            db_path=tmp_path / "ok.db",
        )
        is None
    )
    fallback_summary = get_telemetry_summary(db_path=tmp_path / "ok.db")
    assert fallback_summary["recurring_errors"] == []


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        ("CommandTimeoutError: git push origin main timed out after 30s", "git_timeout"),
        ("git fetch --all: tiempo agotado esperando remoto", "git_timeout"),
        ("TimeoutExpired: pytest -q tests/test_app.py timed out after 60s", "test_timeout"),
        ("vitest run timed out waiting for worker", "test_timeout"),
        ("agy cli timed out after 180s during code_agent invocation", "ag_cli_timeout"),
        ("CommandTimeoutError: antigravity CLI ag-cli time out", "ag_cli_timeout"),
        ("ModuleNotFoundError: No module named 'fastapi'", "missing_module"),
        ("Error [ERR_MODULE_NOT_FOUND]: Cannot find module './helper.js'", "missing_module"),
        ("ImportError: cannot import name 'Foo' from 'bar'", "missing_module"),
        ("SyntaxError: invalid syntax (src/app.py, line 42)", "syntax_or_lint"),
        ("ruff check failed: F401 unused import", "syntax_or_lint"),
        ("IndentationError: unexpected indent", "syntax_or_lint"),
        ("ESLint found 2 lint errors", "syntax_or_lint"),
        ("", "unknown"),
        ("   \n\t  ", "unknown"),
        ("Unexpected 502 Bad Gateway from upstream proxy", "unknown"),
    ],
)
def test_classify_error_all_six_categories(raw_text: str, expected: str) -> None:
    """classify_error maps known failure patterns into the 6 canonical signatures."""
    assert classify_error(raw_text) == expected


def test_get_telemetry_summary_empty_db_and_empty_context(tmp_path: Path) -> None:
    """Empty DB returns clean summary with zero totals, all 5 components, and empty context string."""
    db_path = tmp_path / "empty_telemetry.db"
    summary = get_telemetry_summary(limit=10, db_path=db_path)

    # Must be JSON serializable
    encoded = json.dumps(summary)
    assert isinstance(encoded, str)

    assert summary["limit"] == 10
    assert summary["totals"] == {
        "execution_events": 0,
        "incidents": 0,
        "unresolved_incidents": 0,
    }
    assert summary["slowest_executions"] == []
    assert summary["recurring_errors"] == []
    assert summary["recent_incidents"] == []
    assert set(summary["component_success_rates"].keys()) == set(CANONICAL_COMPONENTS)
    for comp in CANONICAL_COMPONENTS:
        assert summary["component_success_rates"][comp] == {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "timed_out": 0,
            "success_rate": None,
        }

    assert format_telemetry_context(limit=5, db_path=db_path) == ""


def test_get_telemetry_summary_aggregations_ordering_and_limit(tmp_path: Path) -> None:
    """Summary returns slowest executions, recurring errors, per-component rates, and recent incidents accurately."""
    db_path = tmp_path / "agg_telemetry.db"

    # Execution events across all 5 components
    record_execution_event(
        component="orchestrator",
        stage="interview",
        command="llm:interview",
        duration_ms=1200,
        exit_code=0,
        project_id="proj-1",
        timestamp="2026-11-25T10:00:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="orchestrator",
        stage="debate",
        command="llm:debate",
        duration_ms=3500,
        exit_code=1,
        error_signature="ag_cli_timeout",
        timed_out=True,
        project_id="proj-1",
        timestamp="2026-11-25T10:01:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="tests",
        stage="pytest",
        command="pytest -q",
        duration_ms=8000,
        exit_code=124,
        timed_out=True,
        error_signature="test_timeout",
        project_id="proj-1",
        timestamp="2026-11-25T10:02:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="tests",
        stage="pytest",
        command="pytest -q tests/test_fast.py",
        duration_ms=300,
        exit_code=0,
        project_id="proj-1",
        timestamp="2026-11-25T10:03:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="git",
        stage="push",
        command="git push",
        duration_ms=5200,
        exit_code=124,
        timed_out=True,
        error_signature="git_timeout",
        project_id="proj-2",
        timestamp="2026-11-25T10:04:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="doctor",
        stage="diagnose",
        command="doctor:heal",
        duration_ms=900,
        exit_code=0,
        project_id="proj-2",
        timestamp="2026-11-25T10:05:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="self_improver",
        stage="verify",
        command="ruff check .",
        duration_ms=2100,
        exit_code=1,
        error_signature="syntax_or_lint",
        project_id=None,
        timestamp="2026-11-25T10:06:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="self_improver",
        stage="verify",
        command="ruff check .",
        duration_ms=1900,
        exit_code=1,
        error_signature="syntax_or_lint",
        project_id=None,
        timestamp="2026-11-25T10:07:00+00:00",
        db_path=db_path,
    )
    record_execution_event(
        component="self_improver",
        stage="verify",
        command="ruff check .",
        duration_ms=400,
        exit_code=0,
        project_id=None,
        timestamp="2026-11-25T10:08:00+00:00",
        db_path=db_path,
    )

    # Incidents with distinct timestamps
    record_incident(
        stage="verify",
        raw_error="ruff F401",
        error_signature="syntax_or_lint",
        project_id="proj-1",
        root_cause="Unused import",
        fix_summary="Removed import",
        resolved=True,
        timestamp="2026-11-25T10:01:30+00:00",
        db_path=db_path,
    )
    record_incident(
        stage="push",
        raw_error="git push timed out",
        error_signature="git_timeout",
        project_id="proj-2",
        root_cause="Slow network",
        resolved=False,
        timestamp="2026-11-25T10:05:30+00:00",
        db_path=db_path,
    )
    record_incident(
        stage="test",
        raw_error="pytest timed out",
        error_signature="test_timeout",
        project_id="proj-1",
        resolved=False,
        timestamp="2026-11-25T10:09:00+00:00",
        db_path=db_path,
    )

    summary = get_telemetry_summary(limit=2, db_path=db_path)

    assert summary["limit"] == 2
    assert summary["totals"] == {
        "execution_events": 9,
        "incidents": 3,
        "unresolved_incidents": 2,
    }

    # Slowest executions descending, capped at limit=2
    assert [e["duration_ms"] for e in summary["slowest_executions"]] == [8000, 5200]
    assert summary["slowest_executions"][0]["component"] == "tests"
    assert summary["slowest_executions"][0]["timed_out"] is True
    assert summary["slowest_executions"][1]["component"] == "git"

    # Recurring errors descending (syntax_or_lint=3, git_timeout=2, test_timeout=2, ag_cli_timeout=1)
    assert len(summary["recurring_errors"]) == 2
    assert summary["recurring_errors"][0]["error_signature"] == "syntax_or_lint"
    assert summary["recurring_errors"][0]["count"] == 3
    assert summary["recurring_errors"][0]["execution_events"] == 2
    assert summary["recurring_errors"][0]["incidents"] == 1
    assert summary["recurring_errors"][1]["error_signature"] == "git_timeout"
    assert summary["recurring_errors"][1]["count"] == 2

    # Component success rates (hand-computed expectations)
    rates = summary["component_success_rates"]
    assert rates["orchestrator"] == {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "timed_out": 1,
        "success_rate": 0.5,
    }
    assert rates["tests"] == {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "timed_out": 1,
        "success_rate": 0.5,
    }
    assert rates["git"] == {
        "total": 1,
        "succeeded": 0,
        "failed": 1,
        "timed_out": 1,
        "success_rate": 0.0,
    }
    assert rates["doctor"] == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "timed_out": 0,
        "success_rate": 1.0,
    }
    assert rates["self_improver"] == {
        "total": 3,
        "succeeded": 1,
        "failed": 2,
        "timed_out": 0,
        "success_rate": 0.3333,
    }

    # Recent incidents ordered newest to oldest, capped at limit=2
    assert len(summary["recent_incidents"]) == 2
    assert summary["recent_incidents"][0]["timestamp"] == "2026-11-25T10:09:00+00:00"
    assert summary["recent_incidents"][0]["error_signature"] == "test_timeout"
    assert summary["recent_incidents"][0]["resolved"] is False
    assert summary["recent_incidents"][1]["timestamp"] == "2026-11-25T10:05:30+00:00"
    assert summary["recent_incidents"][1]["error_signature"] == "git_timeout"

    # format_telemetry_context produces a non-empty concise text block
    ctx = format_telemetry_context(limit=3, db_path=db_path)
    assert "syntax_or_lint" in ctx
    assert "test_timeout" in ctx


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_dashboard_get_telemetry_summary_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/telemetry/summary returns HTTP 200 JSON matching get_telemetry_summary and validates limit."""
    telemetry_db = tmp_path / "telemetry.db"
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(telemetry_db))

    record_execution_event(
        component="tests",
        stage="unit",
        command="pytest -q",
        duration_ms=1500,
        exit_code=0,
        project_id="proj-dash",
        timestamp="2026-11-25T12:00:00+00:00",
    )
    record_execution_event(
        component="git",
        stage="push",
        command="git push",
        duration_ms=4200,
        exit_code=124,
        timed_out=True,
        project_id="proj-dash",
        timestamp="2026-11-25T12:01:00+00:00",
    )
    record_incident(
        stage="push",
        raw_error="git push timed out after 30s",
        project_id="proj-dash",
        timestamp="2026-11-25T12:01:05+00:00",
    )

    store = SqliteEventStore(db_path=tmp_path / "dash_events.db")
    orch = Orchestrator(store=store, llm=FakeLLM())
    server, thread, base_url = start_dashboard_server(orchestrator=orch, port=0)
    try:
        status, payload = _http_json(f"{base_url}/api/telemetry/summary")
        assert status == 200
        assert payload == get_telemetry_summary(limit=10, db_path=telemetry_db)

        # Custom ?limit=1 query parameter
        status_lim, payload_lim = _http_json(f"{base_url}/api/telemetry/summary?limit=1")
        assert status_lim == 200
        assert payload_lim == get_telemetry_summary(limit=1, db_path=telemetry_db)
        assert len(payload_lim["slowest_executions"]) == 1

        # Out-of-bounds limit is clamped cleanly
        status_low, payload_low = _http_json(f"{base_url}/api/telemetry/summary?limit=0")
        assert status_low == 200
        assert payload_low["limit"] == 1

        status_high, payload_high = _http_json(f"{base_url}/api/telemetry/summary?limit=999")
        assert status_high == 200
        assert payload_high["limit"] == 100

        # Non-integer limit returns HTTP 400
        status_bad, payload_bad = _http_json(f"{base_url}/api/telemetry/summary?limit=abc")
        assert status_bad == 400
        assert "error" in payload_bad
    finally:
        server.shutdown()
        server.server_close()
        server.controller.wait_for_idle(timeout=5.0)
        if thread.is_alive():
            thread.join(timeout=2.0)
        store.close()


def test_record_safe_event_and_incident_persist_and_silence_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """record_safe_event and record_safe_incident persist to temp DB and swallow any SQLite/IO/unexpected error."""
    import agent_factory.telemetry as telemetry_mod

    db_path = tmp_path / "safe_telemetry.db"

    ev_id = record_safe_event(
        component="self_improver",
        stage="apply_start",
        command="Telemetría viva",
        duration_ms=25,
        exit_code=0,
        db_path=db_path,
    )
    inc_id = record_safe_incident(
        stage="apply_proposal",
        raw_error="ruff check failed: F401 unused import",
        db_path=db_path,
    )
    assert isinstance(ev_id, int) and ev_id > 0
    assert isinstance(inc_id, int) and inc_id > 0

    summary = get_telemetry_summary(limit=10, db_path=db_path)
    assert summary["totals"] == {
        "execution_events": 1,
        "incidents": 1,
        "unresolved_incidents": 1,
    }
    assert summary["slowest_executions"][0]["component"] == "self_improver"
    assert summary["slowest_executions"][0]["stage"] == "apply_start"
    assert summary["recent_incidents"][0]["stage"] == "apply_proposal"
    assert summary["recent_incidents"][0]["error_signature"] == "syntax_or_lint"

    # Unwritable directory path never raises
    bad_dir = tmp_path / "unwritable_dir"
    bad_dir.mkdir()
    assert (
        record_safe_event(
            component="self_improver",
            stage="apply_finish",
            duration_ms=10,
            db_path=bad_dir,
        )
        is None
    )
    assert (
        record_safe_incident(
            stage="apply_proposal",
            raw_error="disk error",
            db_path=bad_dir,
        )
        is None
    )

    # Unexpected exceptions raised by underlying functions are completely silenced
    def _boom_event(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.DatabaseError("corrupted wal")

    def _boom_incident(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(telemetry_mod, "record_execution_event", _boom_event)
    monkeypatch.setattr(telemetry_mod, "record_incident", _boom_incident)

    assert record_safe_event(component="self_improver", stage="apply_start", db_path=db_path) is None
    assert record_safe_incident(stage="apply_proposal", raw_error="boom", db_path=db_path) is None


def test_self_improver_records_live_telemetry_and_never_breaks_on_telemetry_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FactorySelfImprover records start/finish/exception telemetry in temp DB and continues cleanly when telemetry fails."""
    import subprocess
    from collections.abc import Mapping, Sequence

    import agent_factory.telemetry as telemetry_mod
    from agent_factory.adapters.subprocess_runner import SubprocessRunner
    from agent_factory.ports.runner import CommandResult
    from agent_factory.self_improver import FactorySelfImprover

    repo_root = tmp_path / "repo_tel"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestFactory"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local"], cwd=repo_root, check=True)
    (repo_root / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo_root / "tests" / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "chore: seed"], cwd=repo_root, check=True, capture_output=True)

    telemetry_db = tmp_path / "live_telemetry.db"
    monkeypatch.setenv("AGENT_FACTORY_TELEMETRY_DB", str(telemetry_db))

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    mode = ["ok"]

    class _TelemetryTestRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            exe_stem = Path(command[0]).stem.lower()
            if exe_stem == "agy":
                if "--dangerously-skip-permissions" not in command:
                    return CommandResult(
                        exit_code=0,
                        stdout=json.dumps(
                            {"reply": "Consulta lista", "new_memory_rules": [], "actionable_proposal": None}
                        ),
                    )
                if mode[0] == "fail_agy":
                    return CommandResult(exit_code=1, stderr="SyntaxError: bad token SECRET_TEL_999")
                (repo_root / "src" / "agent_factory" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
                return CommandResult(exit_code=0, stdout="ok")
            if exe_stem == "ruff":
                return CommandResult(exit_code=0, stdout="All checks passed!")
            if exe_stem == "pytest":
                return CommandResult(exit_code=0, stdout="1 passed")
            if mode[0] == "fail_tag" and exe_stem == "git" and len(command) >= 2 and command[1] == "tag":
                return CommandResult(exit_code=1, stderr="fatal: cannot lock ref pre-self-improve")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    improver = FactorySelfImprover(
        db_path=tmp_path / "factory.db",
        repo_root=repo_root,
        uploads_dir=tmp_path / "uploads",
        runner=_TelemetryTestRunner(),
        secrets_to_redact=("SECRET_TEL_999",),
        restart_fn=lambda _code: None,
        restart_delay_seconds=0.01,
    )

    # 1. Consult success records consult_start and consult_finish
    consult_res = improver.consult_message("Revisa telemetría")
    assert consult_res["ok"] is True

    # 2. Apply success records apply_start and apply_finish (exit_code=0)
    apply_ok = improver.apply_proposal("Actualiza VALUE", proposal_title="Mejora verde")
    assert apply_ok["applied"] is True

    # 3. Apply rollback records incident with secret redacted and failed apply_finish
    mode[0] = "fail_agy"
    apply_rb = improver.apply_proposal("Provoca error", proposal_title="Mejora roja")
    assert apply_rb["rolled_back"] is True

    # 4. Apply exception (git tag failure) records incident + apply_error and still raises
    mode[0] = "fail_tag"
    with pytest.raises(RuntimeError, match="punto de guardado Git"):
        improver.apply_proposal("Fallo en git tag", proposal_title="Mejora excepcion")

    conn = sqlite3.connect(str(telemetry_db))
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute(
            "SELECT component, stage, exit_code, error_signature FROM execution_events ORDER BY id ASC"
        ).fetchall()
        stages = [row["stage"] for row in events]
        assert all(row["component"] == "self_improver" for row in events)
        assert "consult_start" in stages
        assert "consult_finish" in stages
        assert stages.count("apply_start") == 3
        assert stages.count("apply_finish") == 2
        assert "apply_error" in stages

        incidents = conn.execute(
            "SELECT stage, error_signature, raw_error FROM incidents ORDER BY id ASC"
        ).fetchall()
        assert len(incidents) == 2
        assert incidents[0]["stage"] == "apply_proposal"
        assert incidents[0]["error_signature"] == "syntax_or_lint"
        assert "SECRET_TEL_999" not in incidents[0]["raw_error"]
        assert "[REDACTED]" in incidents[0]["raw_error"]
        assert incidents[1]["stage"] == "apply_proposal"
        assert "punto de guardado Git" in incidents[1]["raw_error"]
    finally:
        conn.close()

    # 5. Telemetry failure NEVER breaks FactorySelfImprover caller
    def _broken_record(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("simulated telemetry disk failure")

    monkeypatch.setattr(telemetry_mod, "record_execution_event", _broken_record)
    monkeypatch.setattr(telemetry_mod, "record_incident", _broken_record)
    mode[0] = "ok"
    res_still_ok = improver.consult_message("Funciona aun sin telemetria")
    assert res_still_ok["ok"] is True
    apply_still_ok = improver.apply_proposal("Otro cambio", proposal_title="Sin telemetria")
    assert apply_still_ok["applied"] is True
