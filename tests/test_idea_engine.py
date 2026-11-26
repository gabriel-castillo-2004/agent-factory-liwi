"""Unit tests for the Self-Improvement Idea Radar engine (idea_engine.py)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_factory import idea_engine
from agent_factory.ports.runner import CommandResult, CommandTimeoutError
from agent_factory.telemetry import record_execution_event, record_incident


class FakeRunner:
    """Deterministic test double implementing the Runner protocol."""

    def __init__(
        self,
        responses: Sequence[CommandResult | Exception],
    ) -> None:
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
            raise AssertionError("FakeRunner received more calls than configured responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeStagingManager:
    """Minimal test double for StagingManager.enqueue_improvement."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.enqueued: list[dict[str, Any]] = []

    def enqueue_improvement(
        self,
        title: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.enqueued.append(
            {
                "title": title,
                "prompt": prompt,
                "metadata": dict(metadata) if metadata is not None else {},
            }
        )
        return {
            "pending_count": 0,
            "commits": [],
            "is_building": False,
            "queue_size": len(self.enqueued),
            "last_error": None,
            "current_task": None,
        }


def _sample_idea(
    title: str = "🚀 Caché de Telemetría",
    category: str = "Rendimiento",
    impact: str = "Alto",
    simple_description: str = "Acelera la carga del resumen evitando consultas repetidas.",
    technical_prompt: str = "Implementa caché con TTL en telemetry.py y corre pytest -q --no-cov tests/test_telemetry.py.",
) -> dict[str, str]:
    return {
        "title": title,
        "category": category,
        "impact": impact,
        "simple_description": simple_description,
        "technical_prompt": technical_prompt,
    }


def test_init_idea_db_creates_schema_and_check_constraints_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    assert idea_engine.init_idea_db(db_path=db_path) is True
    assert idea_engine.init_idea_db(db_path=db_path) is True

    conn = sqlite3.connect(str(db_path))
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(improvement_ideas);").fetchall()]
        assert cols == [
            "id",
            "created_at",
            "title",
            "category",
            "impact",
            "simple_description",
            "technical_prompt",
            "status",
            "source",
        ]

        settings_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(idea_radar_settings);").fetchall()
        ]
        assert "key" in settings_cols
        assert "value" in settings_cols

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO improvement_ideas (
                    created_at, title, category, impact,
                    simple_description, technical_prompt, status, source
                ) VALUES ('2026-01-01T00:00:00Z', 'T', 'Cat', 'Alto', 'Desc', 'Prompt', 'invalido', 'manual')
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO improvement_ideas (
                    created_at, title, category, impact,
                    simple_description, technical_prompt, status, source
                ) VALUES ('2026-01-01T00:00:00Z', 'T', 'Cat', 'Alto', 'Desc', 'Prompt', 'nueva', 'cron')
                """
            )
    finally:
        conn.close()


def test_auto_interval_hours_persistence_and_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    assert idea_engine.get_auto_interval_hours(db_path=db_path) == 0.0

    assert idea_engine.set_auto_interval_hours(12, db_path=db_path) == 12.0
    assert idea_engine.get_auto_interval_hours(db_path=db_path) == 12.0

    assert idea_engine.set_auto_interval_hours(2.5, db_path=db_path) == 2.5
    assert idea_engine.get_auto_interval_hours(db_path=db_path) == 2.5

    assert idea_engine.set_auto_interval_hours(0, db_path=db_path) == 0.0
    assert idea_engine.get_auto_interval_hours(db_path=db_path) == 0.0

    for invalid_val in (-1, -0.5, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            idea_engine.set_auto_interval_hours(invalid_val, db_path=db_path)

    for invalid_type in (True, False):
        with pytest.raises(TypeError):
            idea_engine.set_auto_interval_hours(invalid_type, db_path=db_path)


def test_crud_list_by_status_get_and_dismiss(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    saved_manual = idea_engine.save_ideas(
        [
            _sample_idea(title="🚀 Idea Uno", impact="Alto"),
            _sample_idea(title="🛡️ Idea Dos", impact="Medio"),
        ],
        source="manual",
        db_path=db_path,
    )
    saved_auto = idea_engine.save_ideas(
        [_sample_idea(title="⚙️ Idea Tres", impact="Alto")],
        source="auto",
        db_path=db_path,
    )

    assert len(saved_manual) == 2
    assert len(saved_auto) == 1
    assert saved_manual[0]["status"] == "nueva"
    assert saved_manual[0]["source"] == "manual"
    assert saved_auto[0]["source"] == "auto"

    fetched = idea_engine.get_idea(saved_manual[0]["id"], db_path=db_path)
    assert fetched is not None
    assert fetched["title"] == "🚀 Idea Uno"
    assert idea_engine.get_idea(99999, db_path=db_path) is None
    assert idea_engine.get_idea(0, db_path=db_path) is None

    dismissed = idea_engine.dismiss_idea(saved_manual[1]["id"], db_path=db_path)
    assert dismissed is not None
    assert dismissed["status"] == "descartada"
    assert idea_engine.dismiss_idea(99999, db_path=db_path) is None

    default_list = idea_engine.list_ideas(db_path=db_path)
    assert {item["title"] for item in default_list} == {"🚀 Idea Uno", "⚙️ Idea Tres"}

    dismissed_list = idea_engine.list_ideas(statuses=("descartada",), db_path=db_path)
    assert [item["title"] for item in dismissed_list] == ["🛡️ Idea Dos"]

    all_titles = idea_engine.list_all_idea_titles(db_path=db_path)
    assert set(all_titles) == {"🚀 Idea Uno", "🛡️ Idea Dos", "⚙️ Idea Tres"}

    with pytest.raises(ValueError):
        idea_engine.list_ideas(statuses=("desconocido",), db_path=db_path)


def test_save_ideas_deduplicates_normalized_titles_including_dismissed(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    initial = idea_engine.save_ideas(
        [
            _sample_idea(title="🚀 Caché de Telemetría"),
            _sample_idea(title="✨ Panel de Alertas"),
        ],
        source="manual",
        db_path=db_path,
    )
    idea_engine.dismiss_idea(initial[0]["id"], db_path=db_path)

    second_batch = idea_engine.save_ideas(
        [
            _sample_idea(title="  caché   de telemetría! "),
            _sample_idea(title="🔥 Panel de Alertas"),
            _sample_idea(title="🧪 Pruebas de Carga"),
            _sample_idea(title="🧪  pruebas de carga"),
        ],
        source="manual",
        db_path=db_path,
    )

    assert [item["title"] for item in second_batch] == ["🧪 Pruebas de Carga"]
    assert set(idea_engine.list_all_idea_titles(db_path=db_path)) == {
        "🚀 Caché de Telemetría",
        "✨ Panel de Alertas",
        "🧪 Pruebas de Carga",
    }


def test_implement_idea_enqueues_technical_prompt_and_marks_encolada(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    saved = idea_engine.save_ideas(
        [
            _sample_idea(
                title="⚡ Optimizar Consultas",
                technical_prompt="Agrega índice en SQLite y corre pytest -q --no-cov tests/test_idea_engine.py.",
            ),
            _sample_idea(title="🧹 Idea Descartada"),
        ],
        source="manual",
        db_path=db_path,
    )
    active_id = saved[0]["id"]
    dismissed_id = saved[1]["id"]
    idea_engine.dismiss_idea(dismissed_id, db_path=db_path)

    staging = FakeStagingManager()
    result = idea_engine.implement_idea(active_id, staging, db_path=db_path)

    assert result["ok"] is True
    assert result["idea"]["status"] == "encolada"
    assert result["staging_status"]["queue_size"] == 1
    assert len(staging.enqueued) == 1
    assert staging.enqueued[0]["title"] == "⚡ Optimizar Consultas"
    assert (
        staging.enqueued[0]["prompt"]
        == "Agrega índice en SQLite y corre pytest -q --no-cov tests/test_idea_engine.py."
    )
    assert staging.enqueued[0]["metadata"]["idea_id"] == active_id

    persisted = idea_engine.get_idea(active_id, db_path=db_path)
    assert persisted is not None
    assert persisted["status"] == "encolada"

    with pytest.raises(ValueError):
        idea_engine.implement_idea(active_id, staging, db_path=db_path)

    with pytest.raises(ValueError):
        idea_engine.implement_idea(dismissed_id, staging, db_path=db_path)

    with pytest.raises(KeyError):
        idea_engine.implement_idea(99999, staging, db_path=db_path)


def test_implement_idea_keeps_status_nueva_if_enqueue_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    saved = idea_engine.save_ideas([_sample_idea(title="🔧 Idea Con Error")], db_path=db_path)
    idea_id = saved[0]["id"]

    failing_staging = FakeStagingManager(fail_with=RuntimeError("staging detenido"))
    with pytest.raises(RuntimeError, match="staging detenido"):
        idea_engine.implement_idea(idea_id, failing_staging, db_path=db_path)

    persisted = idea_engine.get_idea(idea_id, db_path=db_path)
    assert persisted is not None
    assert persisted["status"] == "nueva"


def test_build_ideation_context_combines_telemetry_git_py_files_and_all_titles(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    repo_root = tmp_path / "repo"
    pkg_dir = repo_root / "src" / "agent_factory"
    sub_dir = pkg_dir / "adapters"
    sub_dir.mkdir(parents=True)
    (pkg_dir / "alpha.py").write_text("x = 1\n", encoding="utf-8")
    (sub_dir / "beta.py").write_text("y = 2\n", encoding="utf-8")
    (pkg_dir / "README.txt").write_text("ignore me\n", encoding="utf-8")

    record_execution_event(
        component="tests",
        stage="pytest",
        duration_ms=4200,
        exit_code=1,
        error_signature="test_timeout",
        db_path=db_path,
    )
    record_incident(
        stage="self_improve",
        raw_error="pytest timed out after 900s",
        error_signature="test_timeout",
        db_path=db_path,
    )

    saved = idea_engine.save_ideas(
        [
            _sample_idea(title="🚀 Idea Activa"),
            _sample_idea(title="🗑️ Idea Ya Descartada"),
        ],
        db_path=db_path,
    )
    idea_engine.dismiss_idea(saved[1]["id"], db_path=db_path)

    git_runner = FakeRunner(
        [
            CommandResult(
                exit_code=0,
                stdout="abc1234 feat(radar): add telemetry summary\ndef5678 fix(staging): prune worktree\n",
            )
        ]
    )

    context = idea_engine.build_ideation_context(
        db_path=db_path,
        repo_root=repo_root,
        runner=git_runner,
    )

    assert len(git_runner.calls) == 1
    assert git_runner.calls[0]["command"] == ["git", "log", "--oneline", "-n", "15"]
    assert "test_timeout" in context
    assert "abc1234 feat(radar): add telemetry summary" in context
    assert "src/agent_factory/alpha.py" in context
    assert "src/agent_factory/adapters/beta.py" in context
    assert "README.txt" not in context
    assert "🚀 Idea Activa" in context
    assert "🗑️ Idea Ya Descartada" in context


def test_build_ideation_context_handles_git_unavailable_safely(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    repo_root = tmp_path / "repo"
    (repo_root / "src" / "agent_factory").mkdir(parents=True)
    (repo_root / "src" / "agent_factory" / "core.py").write_text("", encoding="utf-8")

    failing_git_runner = FakeRunner([OSError("git not found")])
    context = idea_engine.build_ideation_context(
        db_path=db_path,
        repo_root=repo_root,
        runner=failing_git_runner,
    )

    assert "git no disponible" in context.lower()
    assert "src/agent_factory/core.py" in context


def test_generate_new_ideas_parses_clean_json_and_invokes_agy_readonly(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    repo_root = tmp_path / "repo"
    (repo_root / "src" / "agent_factory").mkdir(parents=True)
    (repo_root / "src" / "agent_factory" / "telemetry.py").write_text("", encoding="utf-8")

    payload = [
        {
            "title": "⚡ Caché de Consultas SQLite",
            "category": "Rendimiento",
            "impact": "high",
            "simple_description": "Hace que el panel abra mucho más rápido reutilizando consultas recientes.",
            "technical_prompt": "Añade caché ligero y ejecuta pytest -q --no-cov tests/test_telemetry.py.",
        },
        {
            "title": "🔔 Resumen Diario de Errores",
            "category": "Observabilidad",
            "impact": "medium",
            "simple_description": "Agrupa errores repetidos en un resumen fácil de leer.",
            "technical_prompt": "Agrega agregador diario y ejecuta pytest -q --no-cov tests/test_telemetry.py.",
        },
    ]

    agy_runner = FakeRunner([CommandResult(exit_code=0, stdout=json.dumps(payload))])
    git_runner = FakeRunner([CommandResult(exit_code=0, stdout="111aaaa chore: init\n")])

    created = idea_engine.generate_new_ideas(
        source="manual",
        count=3,
        db_path=db_path,
        runner=agy_runner,
        repo_root=repo_root,
        git_runner=git_runner,
    )

    assert len(agy_runner.calls) == 1
    invoked_cmd = agy_runner.calls[0]["command"]
    assert Path(invoked_cmd[0]).stem.lower() == "agy"
    assert "-p" in invoked_cmd
    assert "--dangerously-skip-permissions" not in invoked_cmd
    assert "--mode" not in invoked_cmd

    assert len(created) == 2
    assert created[0]["title"] == "⚡ Caché de Consultas SQLite"
    assert created[0]["impact"] == "Alto"
    assert created[0]["source"] == "manual"
    assert created[0]["status"] == "nueva"
    assert created[1]["impact"] == "Medio"


def test_generate_new_ideas_parses_markdown_fenced_json_deduplicates_and_filters_invalid(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    existing = idea_engine.save_ideas(
        [_sample_idea(title="🛡️ Guardián de Puertos")],
        source="manual",
        db_path=db_path,
    )
    idea_engine.dismiss_idea(existing[0]["id"], db_path=db_path)

    wrapped_stdout = """
Aquí tienes las mejoras sugeridas para la fábrica:

```json
{
  "ideas": [
    {
      "title": "guardian de puertos",
      "category": "Estabilidad",
      "impact": "Alto",
      "simple_description": "Duplicado de una idea descartada.",
      "technical_prompt": "No debería guardarse."
    },
    {
      "title": "⚠️ Idea Incompleta",
      "category": "Calidad",
      "impact": "Medio",
      "simple_description": "",
      "technical_prompt": "Falta descripción."
    },
    {
      "title": "📊 Métricas de Latencia",
      "category": "Observabilidad",
      "impact": "Alto",
      "simple_description": "Muestra cuánto tarda cada paso para detectar cuellos de botella.",
      "technical_prompt": "Implementa percentiles p95 y corre pytest -q --no-cov tests/test_telemetry.py."
    }
  ]
}
```
Espero que te sirvan.
"""

    agy_runner = FakeRunner([CommandResult(exit_code=0, stdout=wrapped_stdout)])
    git_runner = FakeRunner([CommandResult(exit_code=0, stdout="")])

    created = idea_engine.generate_new_ideas(
        source="auto",
        count=3,
        db_path=db_path,
        runner=agy_runner,
        repo_root=tmp_path,
        git_runner=git_runner,
    )

    assert len(created) == 1
    assert created[0]["title"] == "📊 Métricas de Latencia"
    assert created[0]["source"] == "auto"


def test_generate_new_ideas_handles_invalid_json_and_runner_failures(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    git_runner = FakeRunner([CommandResult(exit_code=0, stdout="")] * 3)

    bad_json_runner = FakeRunner([CommandResult(exit_code=0, stdout="No pude generar JSON hoy.")])
    assert (
        idea_engine.generate_new_ideas(
            db_path=db_path,
            runner=bad_json_runner,
            repo_root=tmp_path,
            git_runner=git_runner,
        )
        == []
    )

    nonzero_runner = FakeRunner([CommandResult(exit_code=1, stdout="", stderr="cli error")])
    assert (
        idea_engine.generate_new_ideas(
            db_path=db_path,
            runner=nonzero_runner,
            repo_root=tmp_path,
            git_runner=git_runner,
        )
        == []
    )

    timeout_runner = FakeRunner([CommandTimeoutError("timed out")])
    assert (
        idea_engine.generate_new_ideas(
            db_path=db_path,
            runner=timeout_runner,
            repo_root=tmp_path,
            git_runner=git_runner,
        )
        == []
    )

    assert idea_engine.list_ideas(db_path=db_path) == []


def test_auto_idea_scheduler_skips_when_disabled_or_busy_and_runs_when_idle(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    calls: list[dict[str, Any]] = []
    now_box = [1000.0]

    def fake_generator(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return [_sample_idea(title="🤖 Idea Automática")]

    factory_idle = [True]
    staging_idle = [True]

    scheduler = idea_engine.AutoIdeaScheduler(
        db_path=db_path,
        is_factory_idle=lambda: factory_idle[0],
        is_staging_idle=lambda: staging_idle[0],
        generator=fake_generator,
        clock=lambda: now_box[0],
    )

    # Disabled when auto_interval_hours == 0
    now_box[0] += 7200.0
    assert scheduler.run_once_if_due() == []
    assert calls == []

    # Enable 1-hour interval, but factory is busy -> must skip
    idea_engine.set_auto_interval_hours(1.0, db_path=db_path)
    factory_idle[0] = False
    staging_idle[0] = True
    assert scheduler.run_once_if_due() == []
    assert calls == []

    # Factory idle, but staging is busy -> must skip
    factory_idle[0] = True
    staging_idle[0] = False
    assert scheduler.run_once_if_due() == []
    assert calls == []

    # Both idle and interval elapsed -> must execute with source='auto'
    factory_idle[0] = True
    staging_idle[0] = True
    generated = scheduler.run_once_if_due()
    assert len(generated) == 1
    assert len(calls) == 1
    assert calls[0]["source"] == "auto"
    assert calls[0]["db_path"] == db_path

    # Immediately afterward, interval has not elapsed -> must skip
    assert scheduler.run_once_if_due() == []
    assert len(calls) == 1

    # Advance 1 hour -> executes again
    now_box[0] += 3600.0
    assert len(scheduler.run_once_if_due()) == 1
    assert len(calls) == 2


def test_start_auto_idea_worker_daemon_respects_busy_state_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"
    idea_engine.set_auto_interval_hours(0.00001, db_path=db_path)

    triggered = threading.Event()
    calls: list[str] = []
    factory_idle = [False]

    def fake_generator(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(str(kwargs.get("source")))
        triggered.set()
        return []

    worker = idea_engine.start_auto_idea_worker(
        db_path=db_path,
        is_factory_idle=lambda: factory_idle[0],
        is_staging_idle=lambda: True,
        generator=fake_generator,
        poll_interval_seconds=0.01,
    )
    try:
        assert worker.is_running is True
        assert triggered.wait(timeout=0.08) is False
        assert calls == []

        factory_idle[0] = True
        assert triggered.wait(timeout=1.0) is True
        assert calls[0] == "auto"
    finally:
        worker.stop(timeout=2.0)

    assert worker.is_running is False
