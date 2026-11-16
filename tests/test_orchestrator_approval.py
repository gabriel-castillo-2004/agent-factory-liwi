"""Tests for Orchestrator approval gate and plan_hash invariants (AC-1.5, F-01, F-11, F-19)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.events import EventType
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import ApprovalError, CircuitBreaker, Orchestrator

# Hand-written expected SHA-256 for _scripted_pipeline_responses()[3] Plan (INV-23, N-10)
EXPECTED_CONVERTER_PLAN_SHA256 = (
    "07566aa3b1f5145823e388aa12eb4c258b8943d7222c7df4b3ceff73c9a19b7b"
)


def _scripted_pipeline_responses(summary: str = "Unit converter static web application") -> list[dict[str, Any]]:
    return [
        {
            "spec": {
                "objective": "Static unit converter",
                "users": ["Gabriel"],
                "features": ["Convert length and weight"],
                "business_rules": ["Round to 4 decimal places"],
                "edge_cases": ["Negative inputs"],
                "constraints": ["Zero backend"],
                "success_criteria": ["Instant conversion"],
                "assumptions": [],
            },
            "questions": [],
        },
        {
            "approach": "Pure client-side conversion functions",
            "components": ["converter.ts", "ui.ts"],
            "tradeoffs": ["Hardcoded conversion table"],
        },
        {
            "critique": ["Floating point precision issues"],
            "robust_proposal": "Scaled integer math and input validation",
            "architectural_safeguards": ["Strict numeric parser"],
        },
        {
            "summary": summary,
            "rationale": "Simple client-side architecture with numeric validation",
            "template_id": "static-app",
            "files": [
                {
                    "path": "src/converter.ts",
                    "purpose": "Conversion formulas",
                    "contract": "Exports convert(value: number, from: string, to: string): number",
                    "depends_on": [],
                }
            ],
            "acceptance_tests": [
                {
                    "id": "AT-01",
                    "description": "Converts meters to kilometers accurately",
                    "type": "unit",
                }
            ],
            "risks": ["None"],
            "deployment_target": "github-pages",
        },
    ]


def _setup_orchestrator_at_waiting_approval() -> tuple[Orchestrator, str]:
    store = MemoryEventStore()
    fake_llm = FakeLLM(responses=_scripted_pipeline_responses())
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-approval-gate"

    orchestrator.create_project(
        project_id=project_id,
        name="Unit Converter",
        objective="Static unit converter",
    )
    orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="evt-interview-1",
        user_message="Quiero un conversor de unidades completo.",
    )
    status = orchestrator.run_debate(project_id=project_id)
    assert status.state == ProjectState.WAITING_APPROVAL
    assert status.plan_hash is not None
    return orchestrator, project_id


def test_enter_building_without_approval_event_fails() -> None:
    """AC-1.5 / M11: Attempting to transition to BUILDING without an ApprovalEvent fails (INV-05)."""
    orchestrator, project_id = _setup_orchestrator_at_waiting_approval()

    with pytest.raises(ApprovalError):
        orchestrator.submit_approval(project_id=project_id, approval=None)

    current_status = orchestrator.get_status(project_id)
    assert current_status.state == ProjectState.WAITING_APPROVAL


def test_enter_building_with_mismatched_plan_hash_fails() -> None:
    """AC-1.5 / M2: Attempting to transition to BUILDING with a different plan_hash fails (INV-05)."""
    orchestrator, project_id = _setup_orchestrator_at_waiting_approval()

    wrong_approval = ApprovalEvent(
        plan_hash="0000000000000000000000000000000000000000000000000000000000000000",
        timestamp=datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
        decision="approved",
    )

    with pytest.raises(ApprovalError):
        orchestrator.submit_approval(project_id=project_id, approval=wrong_approval)

    current_status = orchestrator.get_status(project_id)
    assert current_status.state == ProjectState.WAITING_APPROVAL


def test_enter_building_with_matching_plan_hash_succeeds() -> None:
    """AC-1.5: Submitting an ApprovalEvent with matching plan_hash transitions to BUILDING."""
    orchestrator, project_id = _setup_orchestrator_at_waiting_approval()
    waiting_status = orchestrator.get_status(project_id)
    assert waiting_status.plan_hash is not None

    valid_approval = ApprovalEvent(
        plan_hash=waiting_status.plan_hash,
        timestamp=datetime(2026, 11, 16, 14, 5, tzinfo=UTC),
        decision="approved",
    )

    building_status = orchestrator.submit_approval(
        project_id=project_id,
        approval=valid_approval,
    )
    assert building_status.state == ProjectState.BUILDING
    assert building_status.plan_hash == waiting_status.plan_hash


@pytest.mark.parametrize(
    "non_approved_decision",
    ["rejected", "adjust_interview", "adjust_debate", "pending", ""],
)
def test_enter_building_with_non_approved_decision_fails(non_approved_decision: str) -> None:
    """AC-1.5 / F-01: ApprovalEvent with decision != 'approved' must never enter BUILDING."""
    orchestrator, project_id = _setup_orchestrator_at_waiting_approval()
    waiting_status = orchestrator.get_status(project_id)
    assert waiting_status.plan_hash is not None

    rejected_event = ApprovalEvent(
        plan_hash=waiting_status.plan_hash,
        timestamp=datetime(2026, 11, 16, 14, 10, tzinfo=UTC),
        decision=non_approved_decision,
    )

    with pytest.raises(ApprovalError):
        orchestrator.submit_approval(project_id=project_id, approval=rejected_event)

    current_status = orchestrator.get_status(project_id)
    assert current_status.state != ProjectState.BUILDING
    assert current_status.state == ProjectState.WAITING_APPROVAL


def test_plan_compute_hash_sensitivity_and_handwritten_digest() -> None:
    """AC-1.5 / F-11 / N-10 / M6 / X1: Plan.compute_hash matches hand-written SHA-256 and is sensitive to all fields."""
    base_plan = Plan.model_validate(_scripted_pipeline_responses()[3])
    base_hash = base_plan.compute_hash()
    assert base_hash == EXPECTED_CONVERTER_PLAN_SHA256

    plan_diff_summary = base_plan.model_copy(update={"summary": "Modified summary"})
    plan_diff_files = base_plan.model_copy(
        update={
            "files": [
                FileSpec(
                    path="src/other.ts",
                    purpose="Different file",
                    contract="export function other(): void",
                    depends_on=[],
                )
            ]
        }
    )
    plan_diff_tests = base_plan.model_copy(
        update={
            "acceptance_tests": [
                AcceptanceTest(
                    id="AT-02",
                    description="Different acceptance test",
                    type=AcceptanceTestType.E2E,
                )
            ]
        }
    )
    plan_diff_risks = base_plan.model_copy(update={"risks": ["Precision overflow risk"]})

    h_summary = plan_diff_summary.compute_hash()
    h_files = plan_diff_files.compute_hash()
    h_tests = plan_diff_tests.compute_hash()
    h_risks = plan_diff_risks.compute_hash()

    assert h_summary != EXPECTED_CONVERTER_PLAN_SHA256
    assert h_files != EXPECTED_CONVERTER_PLAN_SHA256
    assert h_tests != EXPECTED_CONVERTER_PLAN_SHA256
    assert h_risks != EXPECTED_CONVERTER_PLAN_SHA256
    assert len({EXPECTED_CONVERTER_PLAN_SHA256, h_summary, h_files, h_tests, h_risks}) == 5


def test_submit_approval_rejected_when_project_not_in_waiting_approval() -> None:
    """AC-1.5 / F-11 / M7: submit_approval raises ApprovalError in INTERVIEW, BUILDING, and FAILED even with valid plan_hash."""
    # 1. Reject in INTERVIEW
    store_int = MemoryEventStore()
    orchestrator_int = Orchestrator(store=store_int, llm=FakeLLM(responses=_scripted_pipeline_responses()))
    orchestrator_int.create_project(
        project_id="proj-wrong-state-interview",
        name="Wrong State",
        objective="Test state gate",
    )
    premature_approval = ApprovalEvent(
        plan_hash=EXPECTED_CONVERTER_PLAN_SHA256,
        timestamp=datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
        decision="approved",
    )
    with pytest.raises(ApprovalError):
        orchestrator_int.submit_approval(
            project_id="proj-wrong-state-interview",
            approval=premature_approval,
        )
    assert orchestrator_int.get_status("proj-wrong-state-interview").state == ProjectState.INTERVIEW

    # 2. Reject in BUILDING with matching plan_hash and decision="approved" (kills M7)
    orchestrator_bld, project_bld = _setup_orchestrator_at_waiting_approval()
    waiting_status = orchestrator_bld.get_status(project_bld)
    assert waiting_status.plan_hash == EXPECTED_CONVERTER_PLAN_SHA256

    valid_approval = ApprovalEvent(
        plan_hash=EXPECTED_CONVERTER_PLAN_SHA256,
        timestamp=datetime(2026, 11, 16, 14, 5, tzinfo=UTC),
        decision="approved",
    )
    building_status = orchestrator_bld.submit_approval(project_bld, valid_approval)
    assert building_status.state == ProjectState.BUILDING

    with pytest.raises(ApprovalError):
        orchestrator_bld.submit_approval(project_bld, valid_approval)
    assert orchestrator_bld.get_status(project_bld).state == ProjectState.BUILDING

    # 3. Reject in FAILED when plan_hash is already set and matches (kills M7)
    orchestrator_fail, project_fail = _setup_orchestrator_at_waiting_approval()
    assert isinstance(orchestrator_fail.llm, FakeLLM)
    for _ in range(3):
        orchestrator_fail.llm.enqueue(TimeoutError("Simulated timeout to enter FAILED"))
    failed_status = orchestrator_fail.run_debate(project_fail)
    assert failed_status.state == ProjectState.FAILED
    assert failed_status.plan_hash == EXPECTED_CONVERTER_PLAN_SHA256

    with pytest.raises(ApprovalError):
        orchestrator_fail.submit_approval(project_fail, valid_approval)
    assert orchestrator_fail.get_status(project_fail).state == ProjectState.FAILED


def test_stale_approval_rejected_and_mediation_state_recorded_on_redebate() -> None:
    """AC-1.5 / F-11 / F-19 / N-02 / M6: Re-debate with explicit budget records MEDIATION and invalidates previous plan_hash."""
    store = MemoryEventStore()
    responses_v1 = _scripted_pipeline_responses(summary="Plan version 1")
    responses_v2 = _scripted_pipeline_responses(summary="Plan version 2")[1:]
    fake_llm = FakeLLM(responses=[*responses_v1, *responses_v2])
    breaker = CircuitBreaker(
        limits={"INTERVIEW": 6, "DEBATE": 6, "MEDIATION": 4, "BUILDING": 15, "ci_rounds": 3}
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm, circuit_breaker=breaker)
    project_id = "proj-redebate-hash"

    orchestrator.create_project(project_id=project_id, name="Redebate", objective="Converter")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete converter spec")

    status_v1 = orchestrator.run_debate(project_id)
    assert status_v1.state == ProjectState.WAITING_APPROVAL
    hash_v1 = status_v1.plan_hash
    assert hash_v1 is not None

    # Verify F-19: DEBATE -> MEDIATION transition event was recorded in the event log
    events_v1 = store.get_events(project_id)
    transition_targets = [
        e.payload.get("to")
        for e in events_v1
        if e.type in (EventType.STATE_TRANSITIONED, EventType.PLAN_CREATED)
    ]
    assert ProjectState.DEBATE in transition_targets
    assert ProjectState.MEDIATION in transition_targets
    assert ProjectState.WAITING_APPROVAL in transition_targets

    # Re-run debate from WAITING_APPROVAL (adjustment flow)
    status_v2 = orchestrator.run_debate(project_id)
    assert status_v2.state == ProjectState.WAITING_APPROVAL
    hash_v2 = status_v2.plan_hash
    assert hash_v2 is not None
    assert hash_v2 != hash_v1

    # Stale approval for v1 must be rejected
    stale_approval = ApprovalEvent(
        plan_hash=hash_v1,
        timestamp=datetime(2026, 11, 16, 14, 30, tzinfo=UTC),
        decision="approved",
    )
    with pytest.raises(ApprovalError):
        orchestrator.submit_approval(project_id, stale_approval)

    assert orchestrator.get_status(project_id).state == ProjectState.WAITING_APPROVAL
