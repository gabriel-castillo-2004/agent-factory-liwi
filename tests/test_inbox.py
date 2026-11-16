"""Tests for input event idempotency in Inbox and Orchestrator (AC-1.9, INV-16, F-08, N-09)."""

from pathlib import Path

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.domain.states import ProjectState
from agent_factory.inbox import Inbox
from agent_factory.orchestrator import Orchestrator


def test_same_event_id_processed_twice_makes_zero_llm_calls_second_time() -> None:
    """AC-1.9 / M9: Processing the same incoming event ID twice makes 0 LLM calls on the second run."""
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Recipe manager",
                    "users": ["Gabriel"],
                    "features": ["Search by ingredient"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["Any dietary filters?"],
            },
            {
                "spec": {
                    "objective": "Should never be reached",
                    "users": ["Nobody"],
                    "features": [],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": [],
            },
        ]
    )
    store = MemoryEventStore()
    inbox = Inbox()
    orchestrator = Orchestrator(store=store, llm=fake_llm, inbox=inbox)
    project_id = "proj-inbox-01"

    orchestrator.create_project(project_id=project_id, name="Recipes", objective="Recipe manager")

    first_status = orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-9001",
        user_message="Quiero un recetario por ingredientes.",
    )
    assert len(fake_llm.calls) == 1
    assert inbox.is_processed("telegram-update-9001") is True

    second_status = orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-9001",
        user_message="Quiero un recetario por ingredientes.",
    )
    assert len(fake_llm.calls) == 1
    assert second_status == first_status


def test_inbox_registers_event_before_llm_invocation_even_when_llm_fails() -> None:
    """AC-1.9 / INV-16 / M9: Incoming event_id is marked processed BEFORE invoking the LLM."""
    inbox = Inbox()
    observed_during_call: list[bool] = []

    def failing_llm_invocation() -> str:
        observed_during_call.append(inbox.is_processed("telegram-update-9002"))
        raise RuntimeError("Simulated LLM failure")

    with pytest.raises(RuntimeError, match="Simulated LLM failure"):
        inbox.process_once("telegram-update-9002", failing_llm_invocation)

    assert observed_during_call == [True]
    assert inbox.is_processed("telegram-update-9002") is True

    retry_invoked = False

    def retry_handler() -> str:
        nonlocal retry_invoked
        retry_invoked = True
        return "should-not-run"

    result = inbox.process_once("telegram-update-9002", retry_handler)
    assert result is None
    assert retry_invoked is False


def test_same_event_id_after_closing_and_reopening_sqlite_store_makes_zero_llm_calls(
    tmp_path: Path,
) -> None:
    """AC-1.9 / F-08 (INV-16): Idempotency persists across closing and reopening SQLite store."""
    db_file = tmp_path / "persistent_inbox.db"
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Persistent recipe manager",
                    "users": ["Gabriel"],
                    "features": ["Search by ingredient"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["Any dietary filters?"],
            },
            {
                "spec": {
                    "objective": "Duplicate call after restart",
                    "users": ["Nobody"],
                    "features": [],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": [],
            },
        ]
    )
    project_id = "proj-persist-inbox"

    store_1 = SqliteEventStore(db_path=db_file)
    orchestrator_1 = Orchestrator(store=store_1, llm=fake_llm)
    orchestrator_1.create_project(
        project_id=project_id,
        name="Persistent Recipes",
        objective="Persistent recipe manager",
    )
    status_1 = orchestrator_1.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-persist-501",
        user_message="Quiero un recetario persistente.",
    )
    store_1.close()

    assert len(fake_llm.calls) == 1

    # Simulate process restart: reopen SQLite store and create a fresh Orchestrator (new Inbox)
    store_2 = SqliteEventStore(db_path=db_file)
    orchestrator_2 = Orchestrator(store=store_2, llm=fake_llm)
    status_2 = orchestrator_2.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-persist-501",
        user_message="Quiero un recetario persistente.",
    )
    store_2.close()

    assert len(fake_llm.calls) == 1
    assert status_2 == status_1


def test_duplicate_event_id_after_leaving_interview_is_ignored_without_error() -> None:
    """N-09 (INV-16): A duplicate event_id arriving after project left INTERVIEW is ignored without raising IllegalTransition."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Static timer",
                    "users": ["Gabriel"],
                    "features": ["Countdown"],
                    "business_rules": ["Positive seconds"],
                    "edge_cases": ["Zero"],
                    "constraints": ["Static web"],
                    "success_criteria": ["Works offline"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {
                "approach": "Vanilla JS timer",
                "components": ["timer.ts"],
                "tradeoffs": ["Simple"],
            },
            {
                "critique": ["Tab drift"],
                "robust_proposal": "Monotonic timestamp delta",
                "architectural_safeguards": ["Visibility API sync"],
            },
            {
                "summary": "Timer app",
                "rationale": "Timestamp delta",
                "template_id": "static-app",
                "files": [
                    {
                        "path": "src/timer.ts",
                        "purpose": "Timer logic",
                        "contract": "export function startTimer(sec: number): void",
                        "depends_on": [],
                    }
                ],
                "acceptance_tests": [
                    {
                        "id": "AT-01",
                        "description": "Counts down to zero",
                        "type": "unit",
                    }
                ],
                "risks": ["None"],
                "deployment_target": "github-pages",
            },
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-dup-after-interview"

    orchestrator.create_project(project_id=project_id, name="Timer", objective="Static timer")
    orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-late-dup-77",
        user_message="Quiero un temporizador completo.",
    )
    waiting_status = orchestrator.run_debate(project_id=project_id)
    assert waiting_status.state == ProjectState.WAITING_APPROVAL
    assert len(fake_llm.calls) == 4

    # Redelivery of already-processed event_id after leaving INTERVIEW must be ignored (no IllegalTransition)
    dup_status = orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-late-dup-77",
        user_message="Quiero un temporizador completo.",
    )
    assert dup_status.state == ProjectState.WAITING_APPROVAL
    assert dup_status == waiting_status
    assert len(fake_llm.calls) == 4

    # Same after simulated restart with a new Orchestrator instance
    restarted_orchestrator = Orchestrator(store=store, llm=fake_llm)
    dup_after_restart = restarted_orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="telegram-update-late-dup-77",
        user_message="Quiero un temporizador completo.",
    )
    assert dup_after_restart.state == ProjectState.WAITING_APPROVAL
    assert len(fake_llm.calls) == 4
