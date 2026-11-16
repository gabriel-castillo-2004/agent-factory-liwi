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
from agent_factory.orchestrator import ApprovalError, Orchestrator

# Hand-written expected SHA-256 for _scripted_pipeline_responses()[3] Plan (INV-23)
EXPECTED_CONVERTER_PLAN_SHA256 = (
    "0766899eb4b9a9b56eb193446a2011898c663b2239c2a24683a23c7701982e12"
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
    """AC-1.5 / F-11 / M6: Plan.compute_hash produces distinct SHA-256 hashes for different plans."""
    plan_v1 = Plan(
        summary="Plan V1",
        rationale="Rationale V1",
        template_id="static-app",
        files=[
            FileSpec(
                path="src/index.ts",
                purpose="Entry",
                contract="export function main(): void",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(id="AT-1", description="Test 1", type=AcceptanceTestType.UNIT)
        ],
        risks=["None"],
        deployment_target="github-pages",
    )
    plan_v2 = Plan(
        summary="Plan V2 - modified summary",
        rationale="Rationale V1",
        template_id="static-app",
        files=plan_v1.files,
        acceptance_tests=plan_v1.acceptance_tests,
        risks=["None"],
        deployment_target="github-pages",
    )

    hash_v1 = plan_v1.compute_hash()
    hash_v2 = plan_v2.compute_hash()

    assert len(hash_v1) == 64
    assert len(hash_v2) == 64
    assert hash_v1 != hash_v2
    assert hash_v1 != "static-hash"


def test_submit_approval_rejected_when_project_not_in_waiting_approval() -> None:
    """AC-1.5 / F-11 / M7: submit_approval raises ApprovalError when project is not in WAITING_APPROVAL."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(responses=_scripted_pipeline_responses())
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-wrong-state-approval"

    orchestrator.create_project(
        project_id=project_id,
        name="Wrong State",
        objective="Test state gate",
    )
    premature_approval = ApprovalEvent(
        plan_hash="a" * 64,
        timestamp=datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
        decision="approved",
    )

    # Attempt approval while still in INTERVIEW
    with pytest.raises(ApprovalError):
        orchestrator.submit_approval(project_id=project_id, approval=premature_approval)

    assert orchestrator.get_status(project_id).state == ProjectState.INTERVIEW


def test_stale_approval_rejected_and_mediation_state_recorded_on_redebate() -> None:
    """AC-1.5 / F-11 / F-19 / M6: Re-debate records MEDIATION transition and invalidates previous plan_hash."""
    store = MemoryEventStore()
    responses_v1 = _scripted_pipeline_responses(summary="Plan version 1")
    responses_v2 = _scripted_pipeline_responses(summary="Plan version 2")[1:]
    fake_llm = FakeLLM(responses=[*responses_v1, *responses_v2])
    orchestrator = Orchestrator(store=store, llm=fake_llm)
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
