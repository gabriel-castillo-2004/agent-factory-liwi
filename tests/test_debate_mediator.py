"""Tests for Pragmatist, Structuralist, and Mediator pipeline (AC-1.3, AC-1.4, F-12, N-05)."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.system_clock import SystemClock
from agent_factory.agents.mediator import (
    Mediator,
    PlanValidationError,
    run_debate_pipeline,
    validate_plan,
)
from agent_factory.agents.pragmatist import Pragmatist, PragmatistProposal
from agent_factory.agents.structuralist import Structuralist, StructuralistCritique
from agent_factory.domain.models import Plan, Spec
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.clock import Clock
from agent_factory.ports.llm import InvalidLLMResponseError


class _FakeClock:
    """Deterministic test clock recording sleep durations (INV-02, INV-09, N-05)."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


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


def test_pragmatist_structuralist_and_mediator_execute_backoff_via_clock_sleep() -> None:
    """N-05 / X7 / X16 / X17 (INV-09): Pragmatist, Structuralist, and Mediator execute exponential backoff via clock.sleep."""
    spec = _sample_complete_spec()

    # 1. Pragmatist backoff (kills X7)
    p_clock = _FakeClock()
    p_llm = FakeLLM(
        responses=[
            "<<<bad-pragmatist-1>>>",
            TimeoutError("Transient timeout in Pragmatist"),
            _valid_pragmatist_dict(),
        ]
    )
    pragmatist = Pragmatist(llm=p_llm, max_retries=2, clock=p_clock)
    proposal = pragmatist.propose(spec)
    assert p_clock.sleeps == [0.1, 0.2]

    # 2. Structuralist backoff on recovery and exhaustion (kills X16)
    s_clock = _FakeClock()
    s_llm = FakeLLM(
        responses=[
            "<<<bad-structuralist-1>>>",
            TimeoutError("Transient timeout in Structuralist"),
            _valid_structuralist_dict(),
        ]
    )
    structuralist = Structuralist(llm=s_llm, max_retries=2, clock=s_clock)
    critique = structuralist.critique(spec, proposal)
    assert s_clock.sleeps == [0.1, 0.2]

    s_exhaust_clock = _FakeClock()
    s_exhaust_llm = FakeLLM(
        responses=[
            "<<<bad-s-1>>>",
            "<<<bad-s-2>>>",
            "<<<bad-s-3>>>",
        ]
    )
    with pytest.raises(InvalidLLMResponseError):
        Structuralist(llm=s_exhaust_llm, max_retries=2, clock=s_exhaust_clock).critique(
            spec, proposal
        )
    assert s_exhaust_clock.sleeps == [0.1, 0.2]

    # 3. Mediator backoff (kills X17)
    m_clock = _FakeClock()
    m_llm = FakeLLM(
        responses=[
            "<<<bad-mediator-1>>>",
            TimeoutError("Transient timeout in Mediator"),
            _valid_plan_dict(),
        ]
    )
    mediator = Mediator(llm=m_llm, max_retries=2, clock=m_clock)
    plan = mediator.mediate(spec, proposal, critique)
    assert plan.template_id == "static-app"
    assert m_clock.sleeps == [0.1, 0.2]


def test_system_clock_implements_clock_port_and_is_default_in_orchestrator() -> None:
    """N-05 (INV-02, INV-09): SystemClock implements Clock port and is used by default in Orchestrator."""
    sys_clock = SystemClock()
    assert isinstance(sys_clock, Clock)

    now = sys_clock.now()
    assert now.tzinfo is not None
    sys_clock.sleep(0.0)

    orchestrator = Orchestrator(store=MemoryEventStore(), llm=FakeLLM())
    assert isinstance(orchestrator.clock, SystemClock)


def test_mediator_retries_with_validation_error_feedback_and_warning_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mediator logs warning and includes exact ValidationError in correction message on retry."""
    spec = _sample_complete_spec()
    proposal = PragmatistProposal.model_validate(_valid_pragmatist_dict())
    critique = StructuralistCritique.model_validate(_valid_structuralist_dict())

    # Attempt 1 returns dict missing required 'rationale' and 'template_id'
    bad_plan_payload = {
        "summary": "Incomplete plan",
    }
    good_plan_payload = _valid_plan_dict()

    fake_llm = FakeLLM(responses=[bad_plan_payload, good_plan_payload])
    mediator = Mediator(llm=fake_llm, max_retries=1)

    with caplog.at_level(logging.WARNING):
        plan = mediator.mediate(spec, proposal, critique)

    assert plan.template_id == "static-app"
    assert len(fake_llm.calls) == 2

    # Verify logging.warning was recorded
    assert "failed Plan validation" in caplog.text

    # Verify attempt 2 received correction message containing exact ValidationError details
    second_call_messages = fake_llm.calls[1]["messages"]
    assert len(second_call_messages) == 3
    retry_prompt = second_call_messages[-1]["content"]
    assert "Previous attempt failed Plan validation" in retry_prompt
    assert "rationale" in retry_prompt
    assert "template_id" in retry_prompt


def test_mediator_system_prompt_specifies_allowed_templates_and_test_types() -> None:
    """Mediator prompt explicitly defines allowed template_id catalog and test types."""
    spec = _sample_complete_spec()
    proposal = PragmatistProposal.model_validate(_valid_pragmatist_dict())
    critique = StructuralistCritique.model_validate(_valid_structuralist_dict())

    fake_llm = FakeLLM(responses=[_valid_plan_dict()])
    mediator = Mediator(llm=fake_llm, max_retries=0)
    mediator.mediate(spec, proposal, critique)

    first_call_messages = fake_llm.calls[0]["messages"]
    system_prompt = first_call_messages[0]["content"]

    assert "template_id must be exactly one of:" in system_prompt
    assert "'static-app'" in system_prompt
    assert "'static-web'" in system_prompt
    assert "unit" in system_prompt
    assert "integration" in system_prompt
    assert "e2e" in system_prompt
    assert "contract" in system_prompt


def test_mediator_retries_with_validation_error_when_adapter_returns_raw_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mediator catches direct ValidationError from Plan.model_validate and adds correction message."""
    spec = _sample_complete_spec()
    proposal = PragmatistProposal.model_validate(_valid_pragmatist_dict())
    critique = StructuralistCritique.model_validate(_valid_structuralist_dict())

    class _RawDictLLM:
        def __init__(self, responses: list[dict[str, Any]]) -> None:
            self.responses = list(responses)
            self.calls: list[dict[str, Any]] = []

        def complete(self, messages: list[dict[str, str]], schema: Any = None) -> dict[str, Any]:
            self.calls.append({"messages": messages, "schema": schema})
            return self.responses.pop(0)

    llm = _RawDictLLM(
        responses=[
            {"summary": "Missing fields"},
            _valid_plan_dict(),
        ]
    )
    mediator = Mediator(llm=llm, max_retries=1)

    with caplog.at_level(logging.WARNING):
        plan = mediator.mediate(spec, proposal, critique)

    assert plan.template_id == "static-app"
    assert len(llm.calls) == 2
    assert "failed Plan validation" in caplog.text
    second_prompt = str(llm.calls[1]["messages"])
    assert "Previous attempt failed Plan validation" in second_prompt
    assert "rationale" in second_prompt
