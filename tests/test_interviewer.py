"""Tests for Interviewer agent and Orchestrator interview state (AC-1.2, F-17, F-18, N-08)."""

from pathlib import Path

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.agents.interviewer import MAX_QUESTIONS_PER_ROUND, Interviewer
from agent_factory.domain.models import Spec
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import Orchestrator


def test_interviewer_caps_questions_at_three_per_round() -> None:
    """AC-1.2 / M4: Interviewer enforces the maximum of 3 questions per round."""
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Personal expense tracker PWA",
                    "users": ["Gabriel"],
                    "features": ["Add expense"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": [
                    "What categories do you need?",
                    "Should expenses support multiple currencies?",
                    "What happens when offline?",
                    "Are there budget alerts?",
                    "How do you measure success?",
                ],
            }
        ]
    )
    interviewer = Interviewer(llm=fake_llm, max_rounds=6)

    result = interviewer.run_round(
        current_spec=Spec(),
        user_message="Quiero una app para registrar mis gastos diarios.",
        round_number=1,
    )

    assert len(result.questions) == MAX_QUESTIONS_PER_ROUND
    assert len(result.questions) <= 3
    assert result.is_complete is False
    assert result.terminated_by_limit is False
    assert result.spec.objective == "Personal expense tracker PWA"


def test_interviewer_finishes_when_spec_checklist_complete() -> None:
    """AC-1.2: Interviewer finishes immediately when Spec checklist is complete."""
    complete_spec_payload = {
        "spec": {
            "objective": "Pomodoro timer static app",
            "users": ["Individual remote worker"],
            "features": ["25-minute work timer", "5-minute break timer", "Session counter"],
            "business_rules": ["Break unlocks only after work timer completes or resets"],
            "edge_cases": ["Tab backgrounded or browser refreshed mid-session"],
            "constraints": ["Static frontend only, zero backend cost"],
            "success_criteria": ["Timer accuracy within 1 second over 25 minutes"],
            "assumptions": [],
        },
        "questions": ["Optional extra question that should be ignored when complete"],
    }
    fake_llm = FakeLLM(
        responses=[
            complete_spec_payload,
            complete_spec_payload,
        ]
    )
    interviewer = Interviewer(llm=fake_llm, max_rounds=6)

    result = interviewer.conduct_interview(
        initial_message="Quiero un reloj Pomodoro completo.",
        user_replies=["Respuesta extra que no debería usarse"],
    )

    assert result.is_complete is True
    assert result.terminated_by_limit is False
    assert result.questions == []
    assert result.rounds_used == 1
    assert len(fake_llm.calls) == 1
    assert result.spec.is_complete() is True
    assert result.spec.missing_fields() == []


def test_interviewer_finishes_on_round_limit_marking_explicit_assumptions() -> None:
    """AC-1.2: Interviewer stops at max_rounds and marks missing Spec fields as assumptions."""
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Habit tracker",
                    "users": ["Single user"],
                    "features": ["Daily check-in"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["What are the business rules?", "What are the edge cases?"],
            },
            {
                "spec": {
                    "objective": "Habit tracker",
                    "users": ["Single user"],
                    "features": ["Daily check-in"],
                    "business_rules": ["One check-in per habit per calendar day"],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["What are the edge cases?", "What are the constraints?"],
            },
        ]
    )
    interviewer = Interviewer(llm=fake_llm, max_rounds=2)

    result = interviewer.conduct_interview(
        initial_message="Quiero un tracker de hábitos.",
        user_replies=["Solo un check-in por día por hábito."],
    )

    assert result.is_complete is True
    assert result.terminated_by_limit is True
    assert result.rounds_used == 2
    assert result.questions == []
    assert len(result.spec.assumptions) >= 3
    assert any("edge_cases" in assumption for assumption in result.spec.assumptions)
    assert any("constraints" in assumption for assumption in result.spec.assumptions)
    assert any("success_criteria" in assumption for assumption in result.spec.assumptions)
    assert result.spec.is_complete() is True


def test_orchestrator_reconstructs_spec_from_event_log_across_restart() -> None:
    """F-17 (INV-11): New Orchestrator instance reconstructs Spec from event log during interview."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Offline Expense Tracker",
                    "users": ["GabrielUniqueUserMarker"],
                    "features": ["CSV export"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["What are the business rules?"],
            },
            {
                "spec": {
                    "objective": "Offline Expense Tracker",
                    "users": ["GabrielUniqueUserMarker"],
                    "features": ["CSV export"],
                    "business_rules": ["No negative totals"],
                    "edge_cases": ["Empty month"],
                    "constraints": ["Local storage only"],
                    "success_criteria": ["Sub-second export"],
                    "assumptions": [],
                },
                "questions": [],
            },
        ]
    )
    project_id = "proj-spec-restart"

    orchestrator_1 = Orchestrator(store=store, llm=fake_llm)
    orchestrator_1.create_project(
        project_id=project_id,
        name="Expense Tracker",
        objective="Offline Expense Tracker",
    )
    orchestrator_1.handle_interview_message(
        project_id=project_id,
        event_id="evt-round-1",
        user_message="Soy Gabriel y quiero exportar gastos a CSV.",
    )

    # Simulate restart with a new Orchestrator instance sharing the same EventStore
    orchestrator_2 = Orchestrator(store=store, llm=fake_llm)
    orchestrator_2.handle_interview_message(
        project_id=project_id,
        event_id="evt-round-2",
        user_message="Sin totales negativos.",
    )

    second_call_prompt = str(fake_llm.calls[1]["messages"])
    assert "GabrielUniqueUserMarker" in second_call_prompt


def test_orchestrator_handle_interview_message_rejects_nonexistent_project() -> None:
    """F-18 (INV-04): Sending an interview message to a nonexistent project raises an error."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Ghost",
                    "users": [],
                    "features": [],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["Why does this project not exist?"],
            }
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)

    with pytest.raises((IllegalTransition, ValueError)):
        orchestrator.handle_interview_message(
            project_id="proj-ghost-never-created",
            event_id="evt-ghost-1",
            user_message="Hola",
        )


def test_two_orchestrators_sharing_sqlite_store_preserve_objective_and_sync_spec(
    tmp_path: Path,
) -> None:
    """N-08 (INV-11): Two Orchestrators sharing a SqliteEventStore preserve initial objective and reconstruct the same Spec."""
    db_file = tmp_path / "shared_orchestrators.db"
    store = SqliteEventStore(db_path=db_file)
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "PreservedInitialObjectiveMarker",
                    "users": ["Gabriel"],
                    "features": ["Offline sync"],
                    "business_rules": ["Deterministic state"],
                    "edge_cases": ["Process restart"],
                    "constraints": ["Zero backend"],
                    "success_criteria": ["Consistent spec across instances"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {
                "approach": "Simple static SPA",
                "components": ["app.ts"],
                "tradeoffs": ["Local only"],
            },
            {
                "critique": ["State sync"],
                "robust_proposal": "Event log projection",
                "architectural_safeguards": ["Strict schema"],
            },
            {
                "summary": "Shared SQLite Spec App",
                "rationale": "Event-sourced spec reconstruction",
                "template_id": "static-app",
                "files": [
                    {
                        "path": "src/app.ts",
                        "purpose": "Main logic",
                        "contract": "export function init(): void",
                        "depends_on": [],
                    }
                ],
                "acceptance_tests": [
                    {
                        "id": "AT-01",
                        "description": "Reconstructs state accurately",
                        "type": "unit",
                    }
                ],
                "risks": ["None"],
                "deployment_target": "github-pages",
            },
        ]
    )
    project_id = "proj-shared-sqlite-spec"

    orch_1 = Orchestrator(store=store, llm=fake_llm)
    orch_2 = Orchestrator(store=store, llm=fake_llm)

    orch_1.create_project(
        project_id=project_id,
        name="Shared Spec App",
        objective="PreservedInitialObjectiveMarker",
    )

    # orch_2 (which did not call create_project) handles round 1 and must see initial objective from store
    orch_2.handle_interview_message(
        project_id=project_id,
        event_id="evt-shared-1",
        user_message="Completa la especificación.",
    )
    first_call_prompt = str(fake_llm.calls[0]["messages"])
    assert "PreservedInitialObjectiveMarker" in first_call_prompt

    # orch_1 (which cached initial incomplete Spec in create_project) must see the updated complete Spec from store
    debate_status = orch_1.run_debate(project_id)
    store.close()

    assert debate_status.state == ProjectState.WAITING_APPROVAL
