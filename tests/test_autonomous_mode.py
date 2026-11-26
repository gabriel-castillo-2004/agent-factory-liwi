"""TDD tests for 24/7 Autonomous Mode persistence and AutonomousEngine in idea_engine.py."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_factory import idea_engine
from agent_factory.ports.runner import CommandResult
from agent_factory.telemetry import update_triage_solution, upsert_triage_error


class FakeRunner:
    """Deterministic Runner test double for git revert and verification commands."""

    def __init__(self, responses: Sequence[CommandResult | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,
    ) -> CommandResult:
        self.calls.append(
            {
                "command": list(command),
                "cwd": Path(cwd),
                "timeout_seconds": timeout_seconds,
                "env": dict(env) if env is not None else None,
                "input": input,
            }
        )
        if not self._responses:
            raise AssertionError(f"FakeRunner received unexpected command: {list(command)}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeStagingManager:
    """Minimal StagingManager test double exposing get_staging_status."""

    def __init__(
        self,
        *,
        is_building: bool = False,
        queue_size: int = 0,
        pending_count: int = 0,
    ) -> None:
        self.is_building = is_building
        self.queue_size = queue_size
        self.pending_count = pending_count

    def get_staging_status(self) -> dict[str, Any]:
        return {
            "pending_count": self.pending_count,
            "commits": [{"hash": "abc1234", "title": "pending"}] * self.pending_count,
            "is_building": self.is_building,
            "queue_size": self.queue_size,
            "last_error": None,
            "current_task": "build-task" if self.is_building else None,
        }


def _seed_idea(
    db_path: Path,
    *,
    title: str,
    impact: str = "Alto",
    simple_description: str = "Mejora automática de prueba.",
    technical_prompt: str = "Implementa mejora atómica con pruebas dirigidas.",
) -> dict[str, Any]:
    created = idea_engine.save_ideas(
        [
            {
                "title": title,
                "category": "Calidad",
                "impact": impact,
                "simple_description": simple_description,
                "technical_prompt": technical_prompt,
            }
        ],
        source="manual",
        db_path=db_path,
    )
    assert len(created) == 1
    return created[0]


# ---------------------------------------------------------------------------
# (a) SQLite persistence and default values
# ---------------------------------------------------------------------------


def test_autonomous_state_defaults_and_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    state = idea_engine.get_autonomous_state(db_path=db_path)
    assert state["autonomous_enabled"] is False
    assert state["cooldown_minutes"] == 25
    assert state["consecutive_failures"] == 0
    assert isinstance(state["updated_at"], str) and state["updated_at"]

    conn = sqlite3.connect(str(db_path))
    try:
        state_cols = [row[1] for row in conn.execute("PRAGMA table_info(autonomous_state);").fetchall()]
        assert state_cols == [
            "id",
            "autonomous_enabled",
            "cooldown_minutes",
            "consecutive_failures",
            "updated_at",
        ]
        hist_cols = [row[1] for row in conn.execute("PRAGMA table_info(autonomous_history);").fetchall()]
        assert hist_cols == [
            "id",
            "applied_at",
            "idea_id",
            "title",
            "simple_description",
            "commit_hash",
            "status",
        ]
        # Singleton row constraint id = 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO autonomous_state (id, autonomous_enabled, cooldown_minutes, consecutive_failures, updated_at) "
                "VALUES (2, 0, 25, 0, '2026-01-01T00:00:00+00:00');"
            )
    finally:
        conn.close()

    updated = idea_engine.set_autonomous_config(
        autonomous_enabled=True,
        cooldown_minutes=15,
        consecutive_failures=1,
        db_path=db_path,
    )
    assert updated["autonomous_enabled"] is True
    assert updated["cooldown_minutes"] == 15
    assert updated["consecutive_failures"] == 1

    reloaded = idea_engine.get_autonomous_state(db_path=db_path)
    assert reloaded["autonomous_enabled"] is True
    assert reloaded["cooldown_minutes"] == 15
    assert reloaded["consecutive_failures"] == 1

    # Partial update preserves untouched fields
    partial = idea_engine.set_autonomous_config(cooldown_minutes=40, db_path=db_path)
    assert partial["autonomous_enabled"] is True
    assert partial["cooldown_minutes"] == 40
    assert partial["consecutive_failures"] == 1


def test_autonomous_config_validation_rejects_invalid_values(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    with pytest.raises(ValueError):
        idea_engine.set_autonomous_config(cooldown_minutes=0, db_path=db_path)
    with pytest.raises(ValueError):
        idea_engine.set_autonomous_config(cooldown_minutes=-5, db_path=db_path)
    with pytest.raises(TypeError):
        idea_engine.set_autonomous_config(cooldown_minutes=True, db_path=db_path)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        idea_engine.set_autonomous_config(autonomous_enabled="yes", db_path=db_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        idea_engine.set_autonomous_config(consecutive_failures=-1, db_path=db_path)


def test_autonomous_history_record_list_and_mark_reverted_discards_idea(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    idea = _seed_idea(db_path, title="⚡ Optimizar caché local", impact="Alto")

    entry = idea_engine.record_autonomous_application(
        title=idea["title"],
        commit_hash="a1b2c3d4",
        simple_description=idea["simple_description"],
        idea_id=idea["id"],
        db_path=db_path,
    )
    assert entry["id"] > 0
    assert entry["idea_id"] == idea["id"]
    assert entry["title"] == idea["title"]
    assert entry["simple_description"] == idea["simple_description"]
    assert entry["commit_hash"] == "a1b2c3d4"
    assert entry["status"] == "activa"
    assert isinstance(entry["applied_at"], str) and entry["applied_at"]

    history = idea_engine.list_autonomous_history(db_path=db_path)
    assert len(history) == 1
    assert history[0]["id"] == entry["id"]
    assert history[0]["status"] == "activa"

    reverted = idea_engine.mark_autonomous_reverted(entry["id"], db_path=db_path)
    assert reverted is not None
    assert reverted["status"] == "revertida"

    active_only = idea_engine.list_autonomous_history(status="activa", db_path=db_path)
    assert active_only == []
    reverted_only = idea_engine.list_autonomous_history(status="revertida", db_path=db_path)
    assert len(reverted_only) == 1
    assert reverted_only[0]["id"] == entry["id"]

    # Associated idea must now be 'descartada' so it will never be picked again
    reloaded_idea = idea_engine.get_idea(idea["id"], db_path=db_path)
    assert reloaded_idea is not None
    assert reloaded_idea["status"] == "descartada"
    assert idea_engine.select_next_autonomous_idea(db_path=db_path) is None


# ---------------------------------------------------------------------------
# (b) Immediate start/stop & Brake 2 (park in .liwi-staging when disabled mid-build)
# ---------------------------------------------------------------------------


def test_brake2_disable_during_staging_build_parks_result_without_promoting(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    idea = _seed_idea(db_path, title="🛡️ Blindar timeouts", impact="Alto")
    idea_engine.set_autonomous_config(autonomous_enabled=True, cooldown_minutes=25, db_path=db_path)

    build_started = threading.Event()
    allow_build_finish = threading.Event()
    promoted_calls: list[dict[str, Any]] = []

    def fake_staging_executor(work_item: dict[str, Any]) -> dict[str, Any]:
        build_started.set()
        assert allow_build_finish.wait(timeout=5.0)
        return {"ok": True, "worktree": ".liwi-staging", "title": work_item["title"]}

    def fake_promoter(work_item: dict[str, Any]) -> str:
        promoted_calls.append(work_item)
        return "deadbeef"

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        activity_checker=lambda: None,
        triage_provider=lambda: None,
        staging_executor=fake_staging_executor,
        main_promoter=fake_promoter,
        cooldown_unit_seconds=60.0,
        idle_poll_seconds=0.05,
    )

    engine.start()
    try:
        assert build_started.wait(timeout=3.0), "Daemon did not start staging build promptly"

        # User disables autonomous mode mid-build (Freno 2)
        idea_engine.set_autonomous_config(autonomous_enabled=False, db_path=db_path)
        engine.notify_config_changed()

        # Let the staging build complete
        allow_build_finish.set()
        # Wait briefly for cycle to finish cleanly
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and engine.last_cycle_result is None:
            time.sleep(0.01)

        assert engine.last_cycle_result is not None
        assert engine.last_cycle_result["status"] == "parked_in_staging"
        assert engine.last_cycle_result["promoted"] is False
        assert promoted_calls == []
        assert idea_engine.list_autonomous_history(db_path=db_path) == []
        # Parking due to manual shutoff must NOT count as a failure
        state = idea_engine.get_autonomous_state(db_path=db_path)
        assert state["autonomous_enabled"] is False
        assert state["consecutive_failures"] == 0
        # Idea remains intact since it wasn't promoted
        assert idea_engine.get_idea(idea["id"], db_path=db_path)["status"] == "nueva"  # type: ignore[index]
    finally:
        start_stop = time.monotonic()
        engine.stop(timeout=2.0)
        assert time.monotonic() - start_stop < 1.5
        assert engine.is_running is False


def test_daemon_wakes_immediately_on_stop_during_long_cooldown(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    idea_engine.set_autonomous_config(autonomous_enabled=True, cooldown_minutes=25, db_path=db_path)

    cycle_done = threading.Event()
    calls = 0

    def fake_selector() -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        cycle_done.set()
        return None

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        activity_checker=lambda: None,
        triage_provider=lambda: None,
        idea_selector=fake_selector,
        cooldown_unit_seconds=60.0,  # 25 * 60 = 1500s real cooldown
    )

    engine.start()
    try:
        assert engine.is_running is True
        assert cycle_done.wait(timeout=2.0)
        assert calls == 1
    finally:
        t0 = time.monotonic()
        engine.stop(timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert engine.is_running is False


# ---------------------------------------------------------------------------
# (c) Brake 1: Absolute user priority (busy_reason checks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_fragment"),
    [
        ({"has_active_builds": lambda: True}, "build"),
        ({"has_running_local_apps": lambda: True}, "app"),
        ({"staging_manager": FakeStagingManager(is_building=True)}, "staging"),
        ({"staging_manager": FakeStagingManager(queue_size=2)}, "staging"),
        ({"staging_manager": FakeStagingManager(pending_count=1)}, "staging"),
        ({"activity_checker": lambda: "usuario editando proyecto"}, "usuario"),
    ],
)
def test_brake1_yields_when_factory_or_staging_is_busy_without_counting_failure(
    tmp_path: Path,
    kwargs: dict[str, Any],
    expected_fragment: str,
) -> None:
    db_path = tmp_path / "telemetry.db"
    _seed_idea(db_path, title="🔧 Mejora que debe esperar", impact="Alto")
    idea_engine.set_autonomous_config(autonomous_enabled=True, cooldown_minutes=25, db_path=db_path)

    executed: list[dict[str, Any]] = []
    promoted: list[dict[str, Any]] = []

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        triage_provider=lambda: None,
        staging_executor=lambda item: executed.append(item) or {"ok": True},
        main_promoter=lambda item: promoted.append(item) or "abcdef12",
        **kwargs,
    )

    busy_reason = engine.get_busy_reason()
    assert busy_reason is not None
    assert expected_fragment in busy_reason.lower()

    result = engine.run_cycle()
    assert result["status"] == "skipped_busy"
    assert result["executed"] is False
    assert result["busy_reason"] == busy_reason
    assert executed == []
    assert promoted == []

    state = idea_engine.get_autonomous_state(db_path=db_path)
    assert state["consecutive_failures"] == 0
    assert state["autonomous_enabled"] is True


# ---------------------------------------------------------------------------
# (d) Priority of error_triage over ideas & automatic commit_hash recording
# ---------------------------------------------------------------------------


def test_triage_solution_takes_priority_over_ideas_and_records_commit_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    # Seed both a Medium idea, a High idea, and a ready error_triage solution
    med_idea = _seed_idea(db_path, title="📝 Limpieza menor", impact="Medio")
    high_idea = _seed_idea(db_path, title="🚀 Acelerar arranque", impact="Alto")

    rec, is_new = upsert_triage_error(
        "sig-triage-ready-1",
        "ZeroDivisionError in dashboard metrics",
        scope="factory",
        status="analizando",
        db_path=db_path,
    )
    assert is_new is True and rec is not None
    update_triage_solution(
        rec["id"],
        title="🩹 Evitar división por cero en métricas",
        root_cause="Denominador cero sin guarda.",
        proposed_fix="Validar denominador > 0 antes de dividir.",
        technical_prompt="Añade guarda para denominador cero y prueba dirigida.",
        status="lista",
        db_path=db_path,
    )

    # Start with 1 prior failure to verify success resets consecutive_failures to 0
    idea_engine.set_autonomous_config(
        autonomous_enabled=True,
        cooldown_minutes=25,
        consecutive_failures=1,
        db_path=db_path,
    )

    built_items: list[dict[str, Any]] = []
    commits = ["1111aaaa", "2222bbbb"]

    def fake_build(item: dict[str, Any]) -> dict[str, Any]:
        built_items.append(item)
        return {"ok": True}

    def fake_promote(_item: dict[str, Any]) -> str:
        return commits.pop(0)

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        activity_checker=lambda: None,
        staging_executor=fake_build,
        main_promoter=fake_promote,
    )

    # Cycle 1: must pick error_triage solution first
    res1 = engine.run_cycle()
    assert res1["status"] == "applied"
    assert res1["promoted"] is True
    assert res1["commit_hash"] == "1111aaaa"
    assert built_items[0]["source"] == "error_triage"
    assert built_items[0]["title"] == "🩹 Evitar división por cero en métricas"
    assert idea_engine.get_autonomous_state(db_path=db_path)["consecutive_failures"] == 0

    # Cycle 2: no more ready triage -> must pick High impact idea before Medium impact idea
    res2 = engine.run_cycle()
    assert res2["status"] == "applied"
    assert res2["commit_hash"] == "2222bbbb"
    assert built_items[1]["source"] == "idea_engine"
    assert built_items[1]["idea_id"] == high_idea["id"]
    assert built_items[1]["title"] == high_idea["title"]

    # Verify history recorded both commit hashes cleanly
    history = idea_engine.list_autonomous_history(db_path=db_path)
    assert len(history) == 2
    hashes = {item["commit_hash"] for item in history}
    assert hashes == {"1111aaaa", "2222bbbb"}
    assert all(item["status"] == "activa" for item in history)

    # Medium idea is still 'nueva', High idea is now 'encolada'
    assert idea_engine.get_idea(high_idea["id"], db_path=db_path)["status"] == "encolada"  # type: ignore[index]
    assert idea_engine.get_idea(med_idea["id"], db_path=db_path)["status"] == "nueva"  # type: ignore[index]


# ---------------------------------------------------------------------------
# (e) Brake 3: Circuit breaker trips after 2 consecutive failures
# ---------------------------------------------------------------------------


def test_brake3_circuit_breaker_disables_autonomous_mode_after_two_consecutive_failures(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    _seed_idea(db_path, title="🔥 Idea inestable", impact="Alto")
    idea_engine.set_autonomous_config(autonomous_enabled=True, cooldown_minutes=25, db_path=db_path)

    promote_calls: list[dict[str, Any]] = []

    def failing_build(_item: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "pytest failed in .liwi-staging"}

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        activity_checker=lambda: None,
        triage_provider=lambda: None,
        staging_executor=failing_build,
        main_promoter=lambda item: promote_calls.append(item) or "9999ffff",
    )

    # Failure 1: increments consecutive_failures to 1, remains enabled
    res1 = engine.run_cycle()
    assert res1["status"] == "failed"
    assert res1["consecutive_failures"] == 1
    assert res1["circuit_breaker_tripped"] is False
    state1 = idea_engine.get_autonomous_state(db_path=db_path)
    assert state1["consecutive_failures"] == 1
    assert state1["autonomous_enabled"] is True

    # Failure 2: increments consecutive_failures to 2 and auto-disables in SQLite
    res2 = engine.run_cycle()
    assert res2["status"] == "failed"
    assert res2["consecutive_failures"] == 2
    assert res2["circuit_breaker_tripped"] is True
    state2 = idea_engine.get_autonomous_state(db_path=db_path)
    assert state2["consecutive_failures"] == 2
    assert state2["autonomous_enabled"] is False

    # Cycle 3: disabled -> does not execute anything
    res3 = engine.run_cycle()
    assert res3["status"] == "disabled"
    assert res3["executed"] is False
    assert promote_calls == []
    assert idea_engine.list_autonomous_history(db_path=db_path) == []


# ---------------------------------------------------------------------------
# (f) Reversion: git revert --no-edit <commit_hash>, verify, mark revertida, discard idea
# ---------------------------------------------------------------------------


def test_revert_autonomous_improvement_runs_git_revert_verifies_and_discards_idea(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    idea = _seed_idea(db_path, title="🧩 Nueva heurística", impact="Alto")
    entry = idea_engine.record_autonomous_application(
        title=idea["title"],
        commit_hash="c0ffee42",
        simple_description=idea["simple_description"],
        idea_id=idea["id"],
        db_path=db_path,
    )

    fake_runner = FakeRunner(
        [
            # 1. git rev-parse --abbrev-ref HEAD -> main
            CommandResult(exit_code=0, stdout="main\n", stderr=""),
            # 2. git show --pretty= --name-only c0ffee42
            CommandResult(
                exit_code=0,
                stdout="src/agent_factory/idea_engine.py\ntests/test_autonomous_mode.py\n",
                stderr="",
            ),
            # 3. git revert --no-edit c0ffee42
            CommandResult(exit_code=0, stdout="[main 1234abcd] Revert ...\n", stderr=""),
            # 4. ruff check ...
            CommandResult(exit_code=0, stdout="All checks passed!\n", stderr=""),
            # 5. pytest -q --no-cov ...
            CommandResult(exit_code=0, stdout="1 passed\n", stderr=""),
        ]
    )

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        command_runner=fake_runner,
    )

    result = engine.revert_autonomous_improvement(entry["id"])
    assert result["ok"] is True
    assert result["commit_hash"] == "c0ffee42"
    assert result["entry"]["status"] == "revertida"
    assert result["discarded_idea_id"] == idea["id"]

    commands = [call["command"] for call in fake_runner.calls]
    assert ["git", "revert", "--no-edit", "c0ffee42"] in commands
    assert any(cmd[0] == "ruff" and "check" in cmd for cmd in commands)
    assert any("pytest" in " ".join(cmd) and "--no-cov" in cmd for cmd in commands)

    # DB reflects 'revertida' and associated idea is 'descartada'
    reloaded_entry = idea_engine.get_autonomous_history_entry(entry["id"], db_path=db_path)
    assert reloaded_entry is not None
    assert reloaded_entry["status"] == "revertida"
    reloaded_idea = idea_engine.get_idea(idea["id"], db_path=db_path)
    assert reloaded_idea is not None
    assert reloaded_idea["status"] == "descartada"

    # Re-reverting an already reverted entry fails cleanly
    with pytest.raises(ValueError):
        engine.revert_autonomous_improvement(entry["id"])


def test_revert_autonomous_improvement_does_not_mark_reverted_when_verification_fails(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    idea = _seed_idea(db_path, title="🧪 Cambio con fallo al verificar revert", impact="Alto")
    entry = idea_engine.record_autonomous_application(
        title=idea["title"],
        commit_hash="beef1234",
        simple_description=idea["simple_description"],
        idea_id=idea["id"],
        db_path=db_path,
    )

    revert_calls: list[str] = []
    verify_calls: list[str] = []

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        revert_runner=lambda commit_hash: revert_calls.append(commit_hash) or {"ok": True},
        verify_runner=lambda commit_hash: verify_calls.append(commit_hash) or {"ok": False, "error": "ruff failed"},
    )

    with pytest.raises(RuntimeError, match="ruff failed"):
        engine.revert_autonomous_improvement(entry["id"])

    assert revert_calls == ["beef1234"]
    assert verify_calls == ["beef1234"]
    # Entry must remain 'activa' and idea not discarded because verification failed
    reloaded_entry = idea_engine.get_autonomous_history_entry(entry["id"], db_path=db_path)
    assert reloaded_entry is not None
    assert reloaded_entry["status"] == "activa"
    assert idea_engine.get_idea(idea["id"], db_path=db_path)["status"] == "nueva"  # type: ignore[index]


def test_quota_meter_reserve_threshold_and_429_auto_calibration_without_circuit_breaker(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    db_path = tmp_path / "telemetry.db"
    _seed_idea(db_path, title="⚡ Mejorar caché", impact="Alto")
    idea_engine.set_autonomous_config(autonomous_enabled=True, cooldown_minutes=20, db_path=db_path)

    initial_q = idea_engine.get_quota_status(db_path=db_path)
    assert initial_q["estimated_window_capacity"] == 180000
    assert initial_q["reserve_threshold_pct"] == 30
    assert initial_q["quota_remaining_pct"] == 100

    # Accumulate 130,000 units (<30% left -> ~28% <= 30% threshold)
    after_usage = idea_engine.record_agy_usage(65000, 65000, db_path=db_path)
    assert after_usage is not None
    assert after_usage["quota_remaining_pct"] == 28
    assert after_usage["reserve_reached"] is True

    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        activity_checker=lambda: None,
        triage_provider=lambda: None,
        staging_executor=lambda _w: (_ for _ in ()).throw(AssertionError("should yield at reserve threshold")),
    )
    cycle_reserve = engine.run_cycle()
    assert cycle_reserve["status"] == "paused_quota"
    assert engine.get_state()["state"] == "pausado_cuota"

    # Lower reserve threshold to 5% and raise 429 during staging execution -> auto-calibrates without circuit breaker
    idea_engine.set_quota_config(reserve_threshold_pct=5, db_path=db_path)
    engine2 = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        activity_checker=lambda: None,
        triage_provider=lambda: None,
        staging_executor=lambda _w: {"ok": False, "error": "RESOURCE_EXHAUSTED 429 Resets in 20m0s"},
    )
    cycle_429 = engine2.run_cycle()
    assert cycle_429["status"] == "paused_quota"
    assert cycle_429["quota"]["is_paused"] is True
    assert cycle_429["quota"]["quota_remaining_pct"] == 0
    assert cycle_429["quota"]["estimated_window_capacity"] == 130000
    st_after = idea_engine.get_autonomous_state(db_path=db_path)
    assert st_after["consecutive_failures"] == 0
    assert st_after["autonomous_enabled"] is True

    # Once pause expires in future clock check, quota resets cleanly to 100%
    future = datetime.now(UTC) + timedelta(minutes=35)
    recovered_q = idea_engine.get_quota_status(db_path=db_path, now=future)
    assert recovered_q["is_paused"] is False
    assert recovered_q["quota_remaining_pct"] == 100


def test_autonomous_engine_get_status_snapshot_tracks_cediendo_paso_and_apagado(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    engine = idea_engine.AutonomousEngine(
        db_path=db_path,
        repo_root=tmp_path,
        has_running_local_apps=lambda: True,
    )
    snap_off = engine.get_status_snapshot()
    assert snap_off["state"] == "apagado"
    assert snap_off["quota"]["quota_remaining_pct"] == 100

    idea_engine.set_autonomous_config(autonomous_enabled=True, db_path=db_path)
    snap_yield = engine.get_status_snapshot()
    assert snap_yield["state"] == "cediendo_paso"
    assert snap_yield["busy_reason"] == "app local encendida"

