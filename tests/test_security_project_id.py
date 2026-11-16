"""Tests for project_id validation and path traversal prevention (AC-1.12, INV-22, F-05)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.file_publisher import FileStatusPublisher
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.models import ApprovalEvent, ProjectStatus
from agent_factory.orchestrator import Orchestrator

INVALID_PROJECT_IDS: tuple[str, ...] = (
    "../escaped",
    "..\\escaped",
    "a/../../escaped",
    "/abs/path",
    "C:\\abs\\path",
    "",
    " ",
    "-starts-with-dash",
    "UPPERCASE-ID",
    "has_underscore",
    "has space",
    "a" * 64,
)


@pytest.mark.parametrize("invalid_id", INVALID_PROJECT_IDS)
def test_file_publisher_rejects_invalid_project_id_and_prevents_traversal(
    tmp_path: Path,
    invalid_id: str,
) -> None:
    """AC-1.12 / F-05 (INV-22): FileStatusPublisher rejects invalid project_id and never writes outside base_dir."""
    base_dir = tmp_path / "published_statuses"
    publisher = FileStatusPublisher(base_dir=base_dir)
    sentinel_outside = tmp_path / "escaped.json"
    sentinel_outside.write_text('{"keep": true}', encoding="utf-8")

    with pytest.raises(ValueError):
        status = ProjectStatus(project_id=invalid_id)
        publisher.publish(status)

    with pytest.raises(ValueError):
        publisher.load(invalid_id)

    with pytest.raises(ValueError):
        publisher.delete(invalid_id)

    # Sentinel file outside base_dir must remain untouched and no new files outside base_dir may exist
    assert sentinel_outside.is_file()
    assert sentinel_outside.read_text(encoding="utf-8") == '{"keep": true}'
    assert set(tmp_path.iterdir()) == {base_dir, sentinel_outside}


@pytest.mark.parametrize("invalid_id", INVALID_PROJECT_IDS)
def test_orchestrator_public_apis_reject_invalid_project_id(invalid_id: str) -> None:
    """AC-1.12 / F-05 (INV-22): Every public Orchestrator method rejects an invalid project_id."""
    store = MemoryEventStore()
    fake_llm = FakeLLM()
    orchestrator = Orchestrator(store=store, llm=fake_llm)

    with pytest.raises(ValueError):
        orchestrator.create_project(project_id=invalid_id, name="Bad ID", objective="Test")

    with pytest.raises(ValueError):
        orchestrator.handle_interview_message(
            project_id=invalid_id,
            event_id="evt-1",
            user_message="Hello",
        )

    with pytest.raises(ValueError):
        orchestrator.run_debate(project_id=invalid_id)

    with pytest.raises(ValueError):
        orchestrator.submit_approval(
            project_id=invalid_id,
            approval=ApprovalEvent(
                plan_hash="a" * 64,
                timestamp=datetime(2026, 11, 16, 15, 0, tzinfo=UTC),
                decision="approved",
            ),
        )

    with pytest.raises(ValueError):
        orchestrator.get_status(project_id=invalid_id)
