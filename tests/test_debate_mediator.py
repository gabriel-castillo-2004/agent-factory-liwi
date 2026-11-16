"""Tests for Pragmatist, Structuralist, and Mediator pipeline (AC-1.3, AC-1.4, F-12)."""

from typing import Any

import pytest
from pydantic import BaseModel

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.agents.mediator import (
    Mediator,
    PlanValidationError,
    run_debate_pipeline,
    validate_plan,
)
from agent_factory.agents.pragmatist import PragmatistProposal
from agent_factory.agents.structuralist import StructuralistCritique
from agent_factory.domain.models import Plan, Spec
from agent_factory.domain.states import ProjectState


class _MinimalStatelessLLM:
    """LLM implementation without a .calls attribute to verify F-12 (INV-02)."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._queue = list(responses)

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = (messages, schema)
        return self._queue.pop(0)


def _sample_complete_spec() -> Spec:
    return Spec(
        objective="Markdown note viewer",
        users=["Gabriel"],
        features=["Render markdown", "Local storage persistence"],
        business_rules=["Sanitize rendered HTML"],
        edge_cases=["Empty input", "Large 1MB markdown document"],
        constraints=["Static web app"],
        success_criteria=["Instant preview under 50ms"],
    )


def _valid_pragmatist_dict() -> dict[str, Any]:
    return {
        "approach": "Single-page vanilla TypeScript app using browser localStorage",
        "components": ["editor.ts", "renderer.ts", "storage.ts"],
        "tradeoffs": ["Limited to single device storage"],
    }


def _valid_structuralist_dict() -> dict[str, Any]:
    return {
        "critique": ["Direct innerHTML rendering risks XSS", "localStorage quota may overflow"],
        "robust_proposal": "Strict sanitizer module and storage quota wrapper with error state",
        "architectural_safeguards": ["HTML sanitization contract", "Storage try/catch boundary"],
    }


def _valid_plan_dict() -> dict[str, Any]:
    return {
        "summary": "Static Markdown note viewer with sanitized rendering and storage wrapper",
        "rationale": "Combines simple vanilla modules with strict XSS and storage boundaries",
        "template_id": "static-app",
        "files": [
            {
                "path": "src/storage.ts",
                "purpose": "Persist notes in localStorage safely",
                "contract": "Exports saveNote(text: string): boolean and loadNote(): string",
                "depends_on": [],
            },
            {
                "path": "src/app.ts",
                "purpose": "Bind editor input to sanitized preview",
                "contract": "Exports initApp(root: HTMLElement): void",
                "depends_on": ["src/storage.ts"],
            },
        ],
        "acceptance_tests": [
            {
                "id": "AT-01",
                "description": "Saving a note persists it to localStorage and restores on reload",
                "type": "unit",
            },
            {
                "id": "AT-02",
                "description": "Rendering markdown updates preview without console.error",
                "type": "e2e",
            },
        ],
        "risks": ["Browser localStorage cleared by user"],
        "deployment_target": "github-pages",
    }


def test_debate_happy_path_makes_exactly_three_llm_calls() -> None:
    """AC-1.3: Debate pipeline makes exactly 3 LLM calls on the happy path."""
    spec = _sample_complete_spec()
    fake_llm = FakeLLM(
        responses=[
            _valid_pragmatist_dict(),
            _valid_structuralist_dict(),
            _valid_plan_dict(),
        ]
    )

    result = run_debate_pipeline(spec=spec, llm=fake_llm)

    assert len(fake_llm.calls) == 3
    assert result.llm_calls == 3
    assert result.state == ProjectState.WAITING_APPROVAL
    assert result.plan is not None
    assert result.plan.template_id == "static-app"
    assert len(result.plan.acceptance_tests) == 2
    assert result.failure_cause is None


def test_debate_pipeline_counts_llm_calls_without_fake_llm_calls_attribute() -> None:
    """AC-1.3 / F-12 (INV-02): Pipeline counts LLM calls without relying on FakeLLM.calls."""
    spec = _sample_complete_spec()
    custom_llm = _MinimalStatelessLLM(
        responses=[
            _valid_pragmatist_dict(),
            _valid_structuralist_dict(),
            _valid_plan_dict(),
        ]
    )

    result = run_debate_pipeline(spec=spec, llm=custom_llm)

    assert result.state == ProjectState.WAITING_APPROVAL
    assert result.llm_calls == 3


def test_debate_invalid_json_retries_and_recovers_within_limit() -> None:
    """AC-1.3: Invalid JSON triggers up to 2 retries and succeeds if valid JSON is returned."""
    spec = _sample_complete_spec()
    fake_llm = FakeLLM(
        responses=[
            "<<<not-valid-json-1>>>",
            "<<<not-valid-json-2>>>",
            _valid_pragmatist_dict(),
            _valid_structuralist_dict(),
            _valid_plan_dict(),
        ]
    )

    result = run_debate_pipeline(spec=spec, llm=fake_llm, max_retries=2)

    assert len(fake_llm.calls) == 5
    assert result.state == ProjectState.WAITING_APPROVAL
    assert result.plan is not None
    assert result.failure_cause is None


def test_debate_invalid_json_exhausts_retries_and_ends_in_failed_with_cause() -> None:
    """AC-1.3: Invalid JSON exhausting 2 retries transitions to FAILED with cause."""
    spec = _sample_complete_spec()
    fake_llm = FakeLLM(
        responses=[
            "{malformed json attempt 1",
            "{malformed json attempt 2",
            "{malformed json attempt 3",
            _valid_structuralist_dict(),
            _valid_plan_dict(),
        ]
    )

    result = run_debate_pipeline(spec=spec, llm=fake_llm, max_retries=2)

    assert len(fake_llm.calls) == 3
    assert result.state == ProjectState.FAILED
    assert result.plan is None
    assert result.failure_cause is not None
    assert len(result.failure_cause.strip()) > 0


def test_mediator_rejects_plan_with_empty_acceptance_tests() -> None:
    """AC-1.4: Mediator rejects a Plan whose acceptance_tests list is empty."""
    spec = _sample_complete_spec()
    proposal = PragmatistProposal.model_validate(_valid_pragmatist_dict())
    critique = StructuralistCritique.model_validate(_valid_structuralist_dict())

    invalid_plan_payload = _valid_plan_dict()
    invalid_plan_payload["acceptance_tests"] = []

    fake_llm = FakeLLM(responses=[invalid_plan_payload])
    mediator = Mediator(llm=fake_llm, max_retries=0)

    with pytest.raises(PlanValidationError):
        mediator.mediate(spec=spec, proposal=proposal, critique=critique)

    with pytest.raises(PlanValidationError):
        validate_plan(Plan.model_validate(invalid_plan_payload))


def test_mediator_rejects_plan_with_file_missing_contract() -> None:
    """AC-1.4: Mediator rejects a Plan when any FileSpec has an empty contract."""
    spec = _sample_complete_spec()
    proposal = PragmatistProposal.model_validate(_valid_pragmatist_dict())
    critique = StructuralistCritique.model_validate(_valid_structuralist_dict())

    invalid_plan_payload = _valid_plan_dict()
    invalid_plan_payload["files"][0]["contract"] = "   "

    fake_llm = FakeLLM(responses=[invalid_plan_payload])
    mediator = Mediator(llm=fake_llm, max_retries=0)

    with pytest.raises(PlanValidationError):
        mediator.mediate(spec=spec, proposal=proposal, critique=critique)

    with pytest.raises(PlanValidationError):
        validate_plan(Plan.model_validate(invalid_plan_payload))


def test_mediator_rejects_plan_with_nonexistent_template_id() -> None:
    """AC-1.4 / M8: Mediator rejects a Plan when template_id does not exist in catalog."""
    spec = _sample_complete_spec()
    proposal = PragmatistProposal.model_validate(_valid_pragmatist_dict())
    critique = StructuralistCritique.model_validate(_valid_structuralist_dict())

    invalid_plan_payload = _valid_plan_dict()
    invalid_plan_payload["template_id"] = "nonexistent-nextjs-backend-template"

    fake_llm = FakeLLM(responses=[invalid_plan_payload])
    mediator = Mediator(llm=fake_llm, max_retries=0)

    with pytest.raises(PlanValidationError):
        mediator.mediate(spec=spec, proposal=proposal, critique=critique)

    with pytest.raises(PlanValidationError):
        validate_plan(Plan.model_validate(invalid_plan_payload))
