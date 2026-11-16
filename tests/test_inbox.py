"""Tests for input event idempotency in Inbox and Orchestrator (AC-1.9, INV-16, F-08)."""

from pathlib import Path

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.sqlite_store import SqliteEventStore
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
