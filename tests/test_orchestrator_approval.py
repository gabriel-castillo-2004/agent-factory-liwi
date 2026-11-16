"""Tests for Orchestrator approval gate (AC-1.5, INV-05, Section 7.9)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.models import ApprovalEvent
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import ApprovalError, Orchestrator


def _scripted_pipeline_responses() -> list[dict[str, Any]]:
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
            "summary": "Unit converter static web application",
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
    """AC-1.5: Attempting to transition to BUILDING without an ApprovalEvent fails (INV-05)."""
    orchestrator, project_id = _setup_orchestrator_at_waiting_approval()

    with pytest.raises(ApprovalError):
        orchestrator.submit_approval(project_id=project_id, approval=None)

    current_status = orchestrator.get_status(project_id)
    assert current_status.state == ProjectState.WAITING_APPROVAL


def test_enter_building_with_mismatched_plan_hash_fails() -> None:
    """AC-1.5: Attempting to transition to BUILDING with a different plan_hash fails (INV-05)."""
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
