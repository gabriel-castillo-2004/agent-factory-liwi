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


def test_format_agent_error_handles_quota_429_and_collapses_technical_detail() -> None:
    from datetime import UTC, datetime

    from agent_factory.error_triage import format_agent_error

    fixed_now = datetime(2026, 11, 26, 10, 0, 0, tzinfo=UTC)
    res_timed = format_agent_error(
        "Error: RESOURCE_EXHAUSTED (HTTP 429). Resets in 15m30s at upstream.",
        now=fixed_now,
    )
    assert res_timed["is_quota_429"] is True
    assert res_timed["reset_seconds"] == 930
    assert "⏳ Sin cuota disponible por ahora." in res_timed["message"]
    assert "continuará automáticamente a las " in res_timed["message"]
    assert "<details><summary>Ver detalle técnico</summary>" in res_timed["details_html"]
    assert "RESOURCE_EXHAUSTED" in res_timed["details_html"]

    # Missing explicit countdown falls back to 30m (1800s)
    res_fallback = format_agent_error("HTTP 429 Too Many Requests", now=fixed_now)
    assert res_fallback["is_quota_429"] is True
    assert res_fallback["reset_seconds"] == 1800


def test_project_ideas_crud_dedup_error_and_generate_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import os

    from agent_factory.ports.runner import CommandResult

    db_path = tmp_path / "telemetry.db"
    ws_dir = tmp_path / "workspaces" / "mi-app"
    ws_dir.mkdir(parents=True)
    (ws_dir / "README.md").write_text("# Mi App\nDemo local.\n", encoding="utf-8")

    # Runtime error deduplication
    err1 = idea_engine.create_project_error_idea(
        "mi-app",
        "Port 9001 already in use when booting server",
        db_path=db_path,
    )
    err2 = idea_engine.create_project_error_idea(
        "mi-app",
        "Port 9001 already in use when booting server",
        db_path=db_path,
    )
    assert err1 is not None and err2 is not None
    assert err1["id"] == err2["id"]
    assert err1["source"] == "runtime_error"

    fake_runner_calls: list[list[str]] = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / ("agy.EXE" if os.name == "nt" else "agy")
    fake_agy.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    class _FakeProjRunner:
        def run(self, command, *, cwd, timeout_seconds=30.0, env=None):
            fake_runner_calls.append(list(command))
            payload = [
                {
                    "title": "🎨 Mejorar contraste",
                    "category": "UX",
                    "impact": "Alto",
                    "simple_description": "Ajusta contraste en botones.",
                    "change_prompt": "Actualiza CSS de botones para cumplir contraste AA.",
                }
            ]
            return CommandResult(exit_code=0, stdout=json.dumps(payload), stderr="")

    created = idea_engine.generate_project_ideas(
        "mi-app",
        ws_dir,
        count=2,
        db_path=db_path,
        runner=_FakeProjRunner(),
        agy_command=[str(fake_agy)],
    )
    assert len(created) == 1
    assert created[0]["project_slug"] == "mi-app"
    assert "--mode" not in fake_runner_calls[0]

    updated = idea_engine.update_project_idea_status(
        created[0]["id"],
        "implementando",
        project_slug="mi-app",
        db_path=db_path,
    )
    assert updated["status"] == "implementando"
    assert len(idea_engine.list_project_ideas("mi-app", db_path=db_path)) == 2


def test_list_ideas_filters_by_canonical_status_and_returns_exact_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"

    saved = idea_engine.save_ideas(
        [
            _sample_idea(title="💡 Pendiente A"),
            _sample_idea(title="💡 Pendiente B"),
            _sample_idea(title="📥 Encolada C"),
            _sample_idea(title="🔧 Implementando D"),
            _sample_idea(title="✅ Aplicada E"),
            _sample_idea(title="🗑️ Descartada F"),
        ],
        source="manual",
        db_path=db_path,
    )
    assert len(saved) == 6
    by_title = {item["title"]: item["id"] for item in saved}

    idea_engine.update_idea_status(by_title["📥 Encolada C"], "queued", db_path=db_path)
    idea_engine.update_idea_status(by_title["🔧 Implementando D"], "implementing", db_path=db_path)
    idea_engine.update_idea_status(by_title["✅ Aplicada E"], "implemented", db_path=db_path)
    idea_engine.dismiss_idea(by_title["🗑️ Descartada F"], db_path=db_path)

    expected_counts = {
        "pending": 2,
        "queued": 1,
        "implementing": 1,
        "implemented": 1,
        "dismissed": 1,
        "all": 6,
    }

    expected_by_filter = {
        "pending": {"💡 Pendiente A", "💡 Pendiente B"},
        "queued": {"📥 Encolada C"},
        "implementing": {"🔧 Implementando D"},
        "implemented": {"✅ Aplicada E"},
        "dismissed": {"🗑️ Descartada F"},
        "all": {
            "💡 Pendiente A",
            "💡 Pendiente B",
            "📥 Encolada C",
            "🔧 Implementando D",
            "✅ Aplicada E",
            "🗑️ Descartada F",
        },
    }

    for status_filter, expected_titles in expected_by_filter.items():
        res = idea_engine.list_ideas(
            status=status_filter,
            include_counts=True,
            db_path=db_path,
        )
        assert isinstance(res, dict)
        assert res["status"] == status_filter
        assert {item["title"] for item in res["ideas"]} == expected_titles
        assert res["counts"] == expected_counts

    # Case/whitespace normalization and independence of counts from limit
    trimmed = idea_engine.list_ideas(
        status="  PENDING  ",
        limit=1,
        include_counts=True,
        db_path=db_path,
    )
    assert trimmed["status"] == "pending"
    assert len(trimmed["ideas"]) == 1
    assert trimmed["counts"] == expected_counts

    # Backward-compatible list return also exposes .counts and defaults to active ideas
    default_list = idea_engine.list_ideas(db_path=db_path)
    assert isinstance(default_list, list)
    assert {item["title"] for item in default_list} == {
        "💡 Pendiente A",
        "💡 Pendiente B",
        "📥 Encolada C",
        "🔧 Implementando D",
    }
    assert default_list.counts == expected_counts

    # Empty DB returns all-zero counts and equals []
    empty_db = tmp_path / "empty.db"
    empty_list = idea_engine.list_ideas(db_path=empty_db)
    assert empty_list == []
    assert empty_list.counts == {
        "pending": 0,
        "queued": 0,
        "implementing": 0,
        "implemented": 0,
        "dismissed": 0,
        "all": 0,
    }

    # Invalid status values fail safely with ValueError / TypeError
    for bad_status in ("desconocido", "", "   "):
        with pytest.raises(ValueError):
            idea_engine.list_ideas(status=bad_status, db_path=db_path)

    with pytest.raises(TypeError):
        idea_engine.list_ideas(status=123, db_path=db_path)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        idea_engine.list_ideas(statuses=("nueva",), status="pending", db_path=db_path)


def test_list_project_ideas_filters_by_canonical_status_and_returns_exact_counts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "telemetry.db"

    saved = idea_engine.save_project_ideas(
        "mi-app",
        [
            {
                "title": "🎨 Pendiente Uno",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Desc 1",
                "change_prompt": "Prompt 1",
            },
            {
                "title": "🎨 Pendiente Dos",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Desc 2",
                "change_prompt": "Prompt 2",
            },
            {
                "title": "📥 Encolada Tres",
                "category": "Estabilidad",
                "impact": "Alto",
                "simple_description": "Desc 3",
                "change_prompt": "Prompt 3",
            },
            {
                "title": "🔧 Implementando Cuatro",
                "category": "Rendimiento",
                "impact": "Alto",
                "simple_description": "Desc 4",
                "change_prompt": "Prompt 4",
            },
            {
                "title": "✅ Aplicada Cinco",
                "category": "Seguridad",
                "impact": "Alto",
                "simple_description": "Desc 5",
                "change_prompt": "Prompt 5",
            },
            {
                "title": "🗑️ Descartada Seis",
                "category": "Producto",
                "impact": "Medio",
                "simple_description": "Desc 6",
                "change_prompt": "Prompt 6",
            },
        ],
        source="manual",
        db_path=db_path,
    )
    assert len(saved) == 6
    by_title = {item["title"]: item["id"] for item in saved}

    # Seed another project to guarantee slug isolation in counts and filtering
    idea_engine.save_project_ideas(
        "otro-app",
        [
            {
                "title": "🔒 Idea de otro proyecto",
                "category": "Seguridad",
                "impact": "Alto",
                "simple_description": "Otra app",
                "change_prompt": "Prompt otra app",
            }
        ],
        source="manual",
        db_path=db_path,
    )

    idea_engine.update_project_idea_status(
        by_title["📥 Encolada Tres"], "queued", project_slug="mi-app", db_path=db_path
    )
    idea_engine.update_project_idea_status(
        by_title["🔧 Implementando Cuatro"], "implementing", project_slug="mi-app", db_path=db_path
    )
    idea_engine.update_project_idea_status(
        by_title["✅ Aplicada Cinco"], "implemented", project_slug="mi-app", db_path=db_path
    )
    idea_engine.update_project_idea_status(
        by_title["🗑️ Descartada Seis"], "dismissed", project_slug="mi-app", db_path=db_path
    )

    expected_counts = {
        "pending": 2,
        "queued": 1,
        "implementing": 1,
        "implemented": 1,
        "dismissed": 1,
        "all": 6,
    }

    expected_by_filter = {
        "pending": {"🎨 Pendiente Uno", "🎨 Pendiente Dos"},
        "queued": {"📥 Encolada Tres"},
        "implementing": {"🔧 Implementando Cuatro"},
        "implemented": {"✅ Aplicada Cinco"},
        "dismissed": {"🗑️ Descartada Seis"},
        "all": {
            "🎨 Pendiente Uno",
            "🎨 Pendiente Dos",
            "📥 Encolada Tres",
            "🔧 Implementando Cuatro",
            "✅ Aplicada Cinco",
            "🗑️ Descartada Seis",
        },
    }

    for status_filter, expected_titles in expected_by_filter.items():
        res = idea_engine.list_project_ideas(
            "mi-app",
            status=status_filter,
            include_counts=True,
            db_path=db_path,
        )
        assert isinstance(res, dict)
        assert res["status"] == status_filter
        assert {item["title"] for item in res["ideas"]} == expected_titles
        assert res["counts"] == expected_counts

    # Spanish status alias still works and limit does not alter counts
    es_res = idea_engine.list_project_ideas(
        "mi-app",
        status="nueva",
        limit=1,
        include_counts=True,
        db_path=db_path,
    )
    assert es_res["status"] == "pending"
    assert len(es_res["ideas"]) == 1
    assert es_res["counts"] == expected_counts

    # Backward-compatible list return (default status=None returns all for slug) with .counts
    default_items = idea_engine.list_project_ideas("mi-app", db_path=db_path)
    assert isinstance(default_items, list)
    assert len(default_items) == 6
    assert default_items.counts == expected_counts

    # Unknown/empty project returns empty list and zeroed counts
    empty_res = idea_engine.list_project_ideas(
        "sin-ideas",
        include_counts=True,
        db_path=db_path,
    )
    assert empty_res == {
        "ideas": [],
        "counts": {
            "pending": 0,
            "queued": 0,
            "implementing": 0,
            "implemented": 0,
            "dismissed": 0,
            "all": 0,
        },
        "status": "all",
    }

    # Invalid statuses raise ValueError / TypeError
    for bad_status in ("invalido", "", "   "):
        with pytest.raises(ValueError):
            idea_engine.list_project_ideas(
                "mi-app",
                status=bad_status,
                db_path=db_path,
            )

    with pytest.raises(TypeError):
        idea_engine.list_project_ideas(
            "mi-app",
            status=42,  # type: ignore[arg-type]
            db_path=db_path,
        )


def test_legacy_tables_migrate_to_support_all_five_idea_statuses(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_statuses.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE improvement_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                impact TEXT NOT NULL,
                simple_description TEXT NOT NULL,
                technical_prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'nueva'
                    CHECK(status IN ('nueva', 'encolada', 'descartada')),
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK(source IN ('manual', 'auto', 'visual_qa'))
            );
            """
        )
        conn.execute(
            """
            INSERT INTO improvement_ideas (
                created_at, title, category, impact,
                simple_description, technical_prompt, status, source
            ) VALUES (
                '2026-01-01T00:00:00Z', 'Idea Legacy Factory', 'UX', 'Alto',
                'Desc', 'Prompt', 'nueva', 'manual'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE project_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                project_slug TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                impact TEXT NOT NULL,
                simple_description TEXT NOT NULL,
                change_prompt TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK(source IN ('manual', 'runtime_error', 'audit')),
                status TEXT NOT NULL DEFAULT 'nueva'
                    CHECK(status IN ('nueva', 'implementando', 'aplicada', 'descartada'))
            );
            """
        )
        conn.execute(
            """
            INSERT INTO project_ideas (
                created_at, project_slug, title, category, impact,
                simple_description, change_prompt, source, status
            ) VALUES (
                '2026-01-01T00:00:00Z', 'mi-app', 'Idea Legacy Proyecto', 'UX', 'Alto',
                'Desc', 'Prompt', 'manual', 'nueva'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    updated_factory = idea_engine.update_idea_status(1, "implemented", db_path=db_path)
    assert updated_factory["title"] == "Idea Legacy Factory"
    assert updated_factory["status"] == "aplicada"

    updated_proj = idea_engine.update_project_idea_status(
        1, "queued", project_slug="mi-app", db_path=db_path
    )
    assert updated_proj["title"] == "Idea Legacy Proyecto"
    assert updated_proj["status"] == "encolada"


def test_get_recent_commits_reads_git_log_and_handles_missing_non_git_or_failures(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    git_runner = FakeRunner(
        [
            CommandResult(
                exit_code=0,
                stdout=(
                    "a1b2c3d feat(radar): add telemetry cache\n"
                    "e4f5a6b fix(dashboard): prevent modal flicker\n"
                ),
            )
        ]
    )
    commits = idea_engine._get_recent_commits(repo_dir, limit=60, runner=git_runner)
    assert commits == [
        "a1b2c3d feat(radar): add telemetry cache",
        "e4f5a6b fix(dashboard): prevent modal flicker",
    ]
    assert len(git_runner.calls) == 1
    assert git_runner.calls[0]["command"] == ["git", "log", "--oneline", "-n", "60"]
    assert git_runner.calls[0]["cwd"] == repo_dir.resolve()

    # Non-existent folder returns [] without invoking runner
    unused_runner = FakeRunner([])
    assert idea_engine._get_recent_commits(tmp_path / "does-not-exist", runner=unused_runner) == []
    assert unused_runner.calls == []

    # Existing folder without .git returns [] cleanly with default runner
    plain_dir = tmp_path / "plain_folder"
    plain_dir.mkdir()
    assert idea_engine._get_recent_commits(plain_dir) == []

    # Non-zero exit code, timeout, and OSError all return [] without raising
    error_runner = FakeRunner(
        [
            CommandResult(exit_code=128, stdout="", stderr="not a git repository"),
            CommandTimeoutError("git log timed out"),
            OSError("git executable missing"),
        ]
    )
    assert idea_engine._get_recent_commits(repo_dir, runner=error_runner) == []
    assert idea_engine._get_recent_commits(repo_dir, runner=error_runner) == []
    assert idea_engine._get_recent_commits(repo_dir, runner=error_runner) == []


def test_is_duplicate_idea_blocks_paraphrases_all_statuses_and_commits_and_accepts_new() -> None:
    existing_items = [
        {
            "title": "⚡ Caché de Telemetría en SQLite",
            "simple_description": "Guarda resúmenes recientes de consultas SQLite para acelerar el radar.",
            "status": "pending",
        },
        {
            "title": "🔔 Alertas de cuota agotada",
            "simple_description": "Notifica en el panel cuando la cuota está agotada.",
            "status": "queued",
        },
        {
            "title": "📊 Exportar métricas a CSV",
            "simple_description": "Permite descargar métricas históricas en formato CSV.",
            "status": "implementing",
        },
        {
            "title": "🌙 Modo Oscuro en el Dashboard",
            "simple_description": "Activa un tema oscuro en la interfaz del dashboard.",
            "status": "implemented",
        },
        {
            "title": "🛡️ Rate limiting middleware for API",
            "simple_description": "Protects API endpoints with per-client request rate limiting.",
            "status": "applied",
        },
        {
            "title": "🧹 Limpieza automática de worktrees huérfanos",
            "simple_description": "Elimina worktrees de git abandonados al iniciar.",
            "status": "dismissed",
        },
    ]
    recent_commits = [
        "a1b2c3d feat(auth): add oauth token rotation for webhooks",
        "9f8e7d6 fix(idea_engine): agregar filtro de ideas duplicadas",
        "1112223 fix: dashboard",
    ]

    # Spanish paraphrases with different emojis/accents/wording across all statuses
    dup_es, reason_es = idea_engine.is_duplicate_idea(
        "🚀 Implementar cache para consultas de telemetria SQLite",
        "Acelera el radar con caché local sobre consultas de telemetría.",
        existing_items,
        recent_commits,
    )
    assert dup_es is True
    assert reason_es

    dup_implemented, reason_impl = idea_engine.is_duplicate_idea(
        "🎨 Agregar soporte de modo oscuro al dashboard",
        "Interruptor para tema oscuro en el dashboard.",
        existing_items,
        recent_commits,
    )
    assert dup_implemented is True
    assert reason_impl

    dup_dismissed, reason_dis = idea_engine.is_duplicate_idea(
        "Limpieza de worktrees huerfanos de git",
        "Borra worktrees abandonados automáticamente.",
        existing_items,
        recent_commits,
    )
    assert dup_dismissed is True
    assert reason_dis

    # English paraphrase against 'applied' idea
    dup_en, reason_en = idea_engine.is_duplicate_idea(
        "Implement API rate limit middleware",
        "Adds request rate limiting protection to API endpoints.",
        existing_items,
        recent_commits,
    )
    assert dup_en is True
    assert reason_en

    # Match against git commit messages (ignoring leading commit hash)
    dup_commit_en, reason_commit_en = idea_engine.is_duplicate_idea(
        "🔐 OAuth token rotation for webhooks",
        "Rotates OAuth tokens automatically for outbound webhooks.",
        existing_items,
        recent_commits,
    )
    assert dup_commit_en is True
    assert reason_commit_en

    dup_commit_es, reason_commit_es = idea_engine.is_duplicate_idea(
        "🛡️ Filtro de ideas duplicadas",
        "Evita repetir sugerencias en idea_engine.",
        existing_items,
        recent_commits,
    )
    assert dup_commit_es is True
    assert reason_commit_es

    # Genuinely new ideas must be accepted (even if sharing one generic word with a short commit)
    is_dup_new_1, reason_new_1 = idea_engine.is_duplicate_idea(
        "📈 Gráfica de latencia p95 en el dashboard",
        "Muestra percentiles de respuesta por etapa para detectar regresiones.",
        existing_items,
        recent_commits,
    )
    assert is_dup_new_1 is False
    assert reason_new_1 == ""

    is_dup_new_2, reason_new_2 = idea_engine.is_duplicate_idea(
        "🧪 Pruebas de contrato para adaptador Telegram",
        "Valida reintentos y formato Markdown del cliente de notificaciones.",
        existing_items,
        recent_commits,
    )
    assert is_dup_new_2 is False
    assert reason_new_2 == ""


def test_generate_ideas_injects_history_commits_filters_paraphrases_and_completes_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "telemetry.db"
    repo_root = tmp_path / "repo"
    (repo_root / "src" / "agent_factory").mkdir(parents=True)
    (repo_root / "src" / "agent_factory" / "idea_engine.py").write_text("", encoding="utf-8")

    # Seed historical ideas across all statuses
    seeded = idea_engine.save_ideas(
        [
            _sample_idea(title="💡 Idea Pendiente Historial", simple_description="Mejora pendiente."),
            _sample_idea(title="📥 Idea Encolada Historial", simple_description="Mejora en cola."),
            _sample_idea(title="🔧 Idea Implementando Historial", simple_description="En progreso."),
            _sample_idea(
                title="🌙 Modo Oscuro en el Dashboard",
                simple_description="Activa un tema oscuro persistente en el panel.",
            ),
            _sample_idea(
                title="🧹 Limpieza automática de worktrees huérfanos",
                simple_description="Limpia worktrees abandonados al arrancar.",
            ),
        ],
        source="manual",
        db_path=db_path,
    )
    by_title = {item["title"]: item["id"] for item in seeded}
    idea_engine.update_idea_status(by_title["📥 Idea Encolada Historial"], "queued", db_path=db_path)
    idea_engine.update_idea_status(
        by_title["🔧 Idea Implementando Historial"], "implementing", db_path=db_path
    )
    idea_engine.update_idea_status(
        by_title["🌙 Modo Oscuro en el Dashboard"], "implemented", db_path=db_path
    )
    idea_engine.dismiss_idea(
        by_title["🧹 Limpieza automática de worktrees huérfanos"], db_path=db_path
    )

    commit_calls: list[tuple[Path, int]] = []

    def _fake_get_commits(repo_path: Path | str, limit: int = 60) -> list[str]:
        commit_calls.append((Path(repo_path), limit))
        return [
            "a1b2c3d feat(auth): add oauth token rotation for webhooks",
            "b2c3d4e fix(staging): prune stale temp files",
        ]

    monkeypatch.setattr(idea_engine, "_get_recent_commits", _fake_get_commits)

    round1_payload = {
        "ideas": [
            # Paraphrase of dismissed idea -> must be rejected
            _sample_idea(
                title="Limpieza de worktrees huerfanos",
                simple_description="Borra worktrees abandonados automáticamente.",
            ),
            # Paraphrase of recent commit -> must be rejected
            _sample_idea(
                title="🔐 OAuth token rotation for webhooks",
                simple_description="Rotates OAuth tokens automatically for webhooks.",
            ),
            # Genuinely new idea #1 -> accepted
            _sample_idea(
                title="📈 Percentiles p95 de latencia",
                simple_description="Calcula percentiles de tiempo por etapa en telemetría.",
            ),
            # Intra-batch paraphrase of new idea #1 -> rejected
            _sample_idea(
                title="📊 Latencia p95 y percentiles por etapa",
                simple_description="Mide percentiles p95 de latencia en telemetría.",
            ),
        ]
    }
    round2_payload = {
        "ideas": [
            _sample_idea(
                title="🧭 Mapa de dependencias internas",
                simple_description="Genera un grafo ligero de módulos importados para detectar ciclos.",
            ),
            _sample_idea(
                title="🔔 Resumen semanal de incidentes",
                simple_description="Agrupa fallos recurrentes por semana en un reporte compacto.",
            ),
        ]
    }
    agy_runner = FakeRunner(
        [
            CommandResult(exit_code=0, stdout=json.dumps(round1_payload)),
            CommandResult(exit_code=0, stdout=json.dumps(round2_payload)),
        ]
    )

    assert idea_engine.generate_ideas is idea_engine.generate_new_ideas
    created = idea_engine.generate_ideas(
        source="manual",
        count=3,
        db_path=db_path,
        runner=agy_runner,
        repo_root=repo_root,
    )

    assert len(commit_calls) == 1
    assert commit_calls[0][1] == 60

    # Round 1 + 1 complementary round
    assert len(agy_runner.calls) == 2
    prompt_round_1 = agy_runner.calls[0]["command"][-1]
    prompt_round_2 = agy_runner.calls[1]["command"][-1]

    # Round 1 prompt injects ALL historical ideas (every status) and commits
    assert "💡 Idea Pendiente Historial" in prompt_round_1
    assert "📥 Idea Encolada Historial" in prompt_round_1
    assert "🔧 Idea Implementando Historial" in prompt_round_1
    assert "🌙 Modo Oscuro en el Dashboard" in prompt_round_1
    assert "🧹 Limpieza automática de worktrees huérfanos" in prompt_round_1
    assert "a1b2c3d feat(auth): add oauth token rotation for webhooks" in prompt_round_1

    # Round 2 prompt excludes rejected and already-accepted topics
    assert "Limpieza de worktrees huerfanos" in prompt_round_2
    assert "OAuth token rotation for webhooks" in prompt_round_2
    assert "📈 Percentiles p95 de latencia" in prompt_round_2

    assert [item["title"] for item in created] == [
        "📈 Percentiles p95 de latencia",
        "🧭 Mapa de dependencias internas",
        "🔔 Resumen semanal de incidentes",
    ]


def test_generate_ideas_limits_complementary_round_to_at_most_one_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "telemetry.db"
    idea_engine.save_ideas(
        [
            _sample_idea(
                title="🌙 Modo Oscuro en el Dashboard",
                simple_description="Activa tema oscuro en el panel.",
            )
        ],
        db_path=db_path,
    )
    monkeypatch.setattr(idea_engine, "_get_recent_commits", lambda repo_path, limit=60: [])

    # Both rounds return duplicates + only 1 new idea in round 2 -> must stop after 1 retry
    round1_payload = {
        "ideas": [
            _sample_idea(
                title="🎨 Agregar modo oscuro al dashboard",
                simple_description="Soporte de tema oscuro en el panel.",
            )
        ]
    }
    round2_payload = {
        "ideas": [
            _sample_idea(
                title="🎨 Tema oscuro para el dashboard",
                simple_description="Activa modo oscuro en el panel.",
            ),
            _sample_idea(
                title="🧪 Pruebas de contratos HTTP",
                simple_description="Verifica códigos de estado y esquemas JSON en rutas públicas.",
            ),
        ]
    }
    agy_runner = FakeRunner(
        [
            CommandResult(exit_code=0, stdout=json.dumps(round1_payload)),
            CommandResult(exit_code=0, stdout=json.dumps(round2_payload)),
        ]
    )

    created = idea_engine.generate_new_ideas(
        source="manual",
        count=3,
        db_path=db_path,
        runner=agy_runner,
        repo_root=tmp_path,
    )

    assert len(agy_runner.calls) == 2
    assert [item["title"] for item in created] == ["🧪 Pruebas de contratos HTTP"]


def test_generate_project_ideas_injects_history_commits_filters_and_completes_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "telemetry.db"
    ws_dir = tmp_path / "workspaces" / "mi-app"
    ws_dir.mkdir(parents=True)
    (ws_dir / "README.md").write_text("# Mi App\n", encoding="utf-8")

    # Seed project ideas across all statuses for 'mi-app'
    seeded = idea_engine.save_project_ideas(
        "mi-app",
        [
            {
                "title": "💡 Filtro de búsqueda pendiente",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Agrega caja de búsqueda rápida.",
                "change_prompt": "Implementa filtro.",
            },
            {
                "title": "📥 Paginación en tabla",
                "category": "Rendimiento",
                "impact": "Medio",
                "simple_description": "Pagina resultados en bloques de 20.",
                "change_prompt": "Agrega paginación.",
            },
            {
                "title": "🔧 Exportador PDF",
                "category": "Producto",
                "impact": "Alto",
                "simple_description": "Exporta facturas a PDF.",
                "change_prompt": "Agrega exportador.",
            },
            {
                "title": "🌙 Modo Oscuro en la App",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Permite alternar entre tema claro y oscuro.",
                "change_prompt": "Agrega tema oscuro.",
            },
            {
                "title": "🗑️ Sonidos en botones",
                "category": "UX",
                "impact": "Medio",
                "simple_description": "Reproduce efectos de sonido al pulsar botones.",
                "change_prompt": "Agrega audio.",
            },
        ],
        source="manual",
        db_path=db_path,
    )
    by_title = {item["title"]: item["id"] for item in seeded}
    idea_engine.update_project_idea_status(
        by_title["📥 Paginación en tabla"], "queued", project_slug="mi-app", db_path=db_path
    )
    idea_engine.update_project_idea_status(
        by_title["🔧 Exportador PDF"], "implementing", project_slug="mi-app", db_path=db_path
    )
    idea_engine.update_project_idea_status(
        by_title["🌙 Modo Oscuro en la App"], "implemented", project_slug="mi-app", db_path=db_path
    )
    idea_engine.update_project_idea_status(
        by_title["🗑️ Sonidos en botones"], "dismissed", project_slug="mi-app", db_path=db_path
    )

    # Seed another project to guarantee isolation
    idea_engine.save_project_ideas(
        "otra-app",
        [
            {
                "title": "🔒 Secreto de Otra App",
                "category": "Seguridad",
                "impact": "Alto",
                "simple_description": "No debe aparecer en el prompt de mi-app.",
                "change_prompt": "Prompt otra app.",
            }
        ],
        source="manual",
        db_path=db_path,
    )

    commit_calls: list[tuple[Path, int]] = []

    def _fake_get_commits(repo_path: Path | str, limit: int = 60) -> list[str]:
        commit_calls.append((Path(repo_path), limit))
        return [
            "c0ffee1 feat(ui): add keyboard shortcuts for modal navigation",
        ]

    monkeypatch.setattr(idea_engine, "_get_recent_commits", _fake_get_commits)

    round1_payload = [
        # Paraphrase of implemented idea -> rejected
        {
            "title": "🎨 Agregar modo oscuro para la app",
            "category": "UX",
            "impact": "Alto",
            "simple_description": "Alterna entre tema claro y oscuro en la aplicación.",
            "change_prompt": "Implementa variables CSS de tema oscuro.",
        },
        # Paraphrase of project commit -> rejected
        {
            "title": "⌨️ Keyboard shortcuts for modal navigation",
            "category": "UX",
            "impact": "Medio",
            "simple_description": "Supports Escape and arrow keyboard shortcuts in modals.",
            "change_prompt": "Add keydown listener for modal.",
        },
        # Genuinely new idea #1 -> accepted
        {
            "title": "📦 Caché offline con Service Worker",
            "category": "Rendimiento",
            "impact": "Alto",
            "simple_description": "Permite consultar el catálogo sin conexión a internet.",
            "change_prompt": "Registra service worker ligero para assets estáticos.",
        },
    ]
    round2_payload = [
        {
            "title": "♿ Etiquetas ARIA en formularios",
            "category": "UX",
            "impact": "Medio",
            "simple_description": "Mejora compatibilidad con lectores de pantalla en todos los inputs.",
            "change_prompt": "Añade atributos aria-label y aria-describedby en formularios.",
        }
    ]
    agy_runner = FakeRunner(
        [
            CommandResult(exit_code=0, stdout=json.dumps(round1_payload)),
            CommandResult(exit_code=0, stdout=json.dumps(round2_payload)),
        ]
    )

    created = idea_engine.generate_project_ideas(
        "mi-app",
        ws_dir,
        count=2,
        db_path=db_path,
        runner=agy_runner,
        agy_command=["agy"],
    )

    assert len(commit_calls) == 1
    assert commit_calls[0] == (ws_dir.resolve(), 60)

    assert len(agy_runner.calls) == 2
    prompt_round_1 = agy_runner.calls[0]["command"][-1]
    prompt_round_2 = agy_runner.calls[1]["command"][-1]

    # Round 1 prompt includes all 'mi-app' historical ideas + commits, and excludes 'otra-app'
    assert "💡 Filtro de búsqueda pendiente" in prompt_round_1
    assert "📥 Paginación en tabla" in prompt_round_1
    assert "🔧 Exportador PDF" in prompt_round_1
    assert "🌙 Modo Oscuro en la App" in prompt_round_1
    assert "🗑️ Sonidos en botones" in prompt_round_1
    assert "c0ffee1 feat(ui): add keyboard shortcuts for modal navigation" in prompt_round_1
    assert "Secreto de Otra App" not in prompt_round_1

    # Round 2 prompt excludes rejected and accepted ideas
    assert "Agregar modo oscuro para la app" in prompt_round_2
    assert "Keyboard shortcuts for modal navigation" in prompt_round_2
    assert "📦 Caché offline con Service Worker" in prompt_round_2

    assert [item["title"] for item in created] == [
        "📦 Caché offline con Service Worker",
        "♿ Etiquetas ARIA en formularios",
    ]



