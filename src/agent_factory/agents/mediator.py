"""Mediator agent and fixed 3-step debate pipeline (Section 4, Section 7.4, AC-1.3, AC-1.4)."""

import logging
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from typing import Any

from pydantic import BaseModel, ValidationError

from agent_factory.agents.pragmatist import Pragmatist, PragmatistProposal
from agent_factory.agents.structuralist import Structuralist, StructuralistCritique
from agent_factory.domain.models import VALID_TEMPLATES, Plan, Spec
from agent_factory.domain.states import ProjectState
from agent_factory.ports.clock import Clock
from agent_factory.ports.llm import LLM, LLMError

logger = logging.getLogger(__name__)


class PlanValidationError(ValueError):
    """Raised when a Plan violates Mediator invariants (AC-1.4)."""


class DebatePipelineResult(BaseModel):
    """Outcome of running the 3-step Debate + Mediation pipeline."""

    state: ProjectState
    proposal: PragmatistProposal | None = None
    critique: StructuralistCritique | None = None
    plan: Plan | None = None
    failure_cause: str | None = None
    llm_calls: int = 0


def validate_plan(
    plan: Plan,
    valid_templates: AbstractSet[str] = VALID_TEMPLATES,
) -> Plan:
    """Validate that plan has non-empty acceptance_tests, file contracts, and valid template_id."""
    if not plan.acceptance_tests:
        raise PlanValidationError("Mediator rejects plan: acceptance_tests must not be empty")

    for file_spec in plan.files:
        if not file_spec.contract or not file_spec.contract.strip():
            raise PlanValidationError(
                f"Mediator rejects plan: file '{file_spec.path}' missing required contract"
            )

    if plan.template_id not in valid_templates:
        raise PlanValidationError(
            f"Mediator rejects plan: template_id '{plan.template_id}' does not exist in catalog"
        )

    return plan


class Mediator:
    """Synthesizes Pragmatist and Structuralist outputs into a validated Plan (AC-1.4)."""

    def __init__(
        self,
        llm: LLM,
        max_retries: int = 2,
        valid_templates: AbstractSet[str] = VALID_TEMPLATES,
        clock: Clock | None = None,
    ) -> None:
        self.llm = llm
        self.max_retries = max_retries
        self.valid_templates = valid_templates
        self.clock = clock

    def mediate(
        self,
        spec: Spec,
        proposal: PragmatistProposal,
        critique: StructuralistCritique,
    ) -> Plan:
        """Produce and validate the Master Plan with bounded retries on invalid JSON."""
        templates_str = ", ".join(f"'{t}'" for t in sorted(self.valid_templates))
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Mediator, the final debate synthesis agent. Produce a validated Master Plan "
                    "reconciling Pragmatist and Structuralist positions. "
                    f"template_id must be exactly one of: {templates_str} (recommended/primary template: 'static-web'). "
                    "acceptance_tests must be non-empty, each containing 'id', 'description', and 'type' (allowed types: 'unit', 'integration', 'e2e'). "
                    "files must contain 'path', 'purpose', and a non-empty 'contract' for each file."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Spec:\n{spec.model_dump_json()}\n\n"
                    f"Pragmatist Proposal:\n{proposal.model_dump_json()}\n\n"
                    f"Structuralist Critique:\n{critique.model_dump_json()}"
                ),
            },
        ]

        attempt_messages = list(messages)
        attempts = 0
        last_err: Exception | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                data = self.llm.complete(messages=attempt_messages, schema=Plan)
                plan = Plan.model_validate(data)
                return validate_plan(plan, self.valid_templates)
            except PlanValidationError:
                # Direct validation errors must propagate or fail
                raise
            except (LLMError, ValueError, TimeoutError, OSError) as err:
                last_err = err
                validation_err = (
                    err
                    if isinstance(err, ValidationError)
                    else (
                        err.__cause__
                        if isinstance(getattr(err, "__cause__", None), ValidationError)
                        else None
                    )
                )
                if validation_err is not None:
                    logger.warning(
                        "Mediator attempt %d failed Plan validation: %s",
                        attempts,
                        validation_err,
                    )
                    attempt_messages = list(messages) + [
                        {
                            "role": "user",
                            "content": (
                                f"Previous attempt failed Plan validation: {validation_err}. "
                                "Please correct the output and respond ONLY with a valid JSON object matching the Plan schema. "
                                f"template_id must be one of: {templates_str} (recommended/primary template: 'static-web'). "
                                "acceptance_tests must be non-empty with types 'unit', 'integration', or 'e2e'. "
                                "Every file must specify a non-empty contract."
                            ),
                        }
                    ]
                elif isinstance(err, LLMError):
                    logger.warning(
                        "Mediator attempt %d failed with LLM error: %s",
                        attempts,
                        err,
                    )
                    attempt_messages = list(messages) + [
                        {
                            "role": "user",
                            "content": (
                                f"Previous attempt failed: {err}. "
                                "Please correct the output and respond ONLY with a valid JSON object matching the Plan schema. "
                                f"template_id must be one of: {templates_str} (recommended/primary template: 'static-web'). "
                                "acceptance_tests must be non-empty with types 'unit', 'integration', or 'e2e'. "
                                "Every file must specify a non-empty contract."
                            ),
                        }
                    ]
                if attempts <= self.max_retries and self.clock is not None and hasattr(self.clock, "sleep"):
                    self.clock.sleep(0.1 * (2 ** (attempts - 1)))

        assert last_err is not None
        raise last_err


class _PipelineLLMWrapper:
    """Counting and breaker-aware LLM wrapper for debate pipeline (F-06, F-12)."""

    def __init__(
        self,
        inner: LLM,
        on_call: Callable[[ProjectState], None] | None = None,
    ) -> None:
        self.inner = inner
        self.on_call = on_call
        self.current_state = ProjectState.DEBATE
        self.call_count = 0

    def complete(self, messages: list[dict[str, str]], schema: type[Any] | None = None) -> Any:
        if self.on_call is not None:
            self.on_call(self.current_state)
        self.call_count += 1
        return self.inner.complete(messages=messages, schema=schema)


def run_debate_pipeline(
    spec: Spec,
    llm: LLM,
    max_retries: int = 2,
    valid_templates: AbstractSet[str] = VALID_TEMPLATES,
    clock: Clock | None = None,
    on_llm_call: Callable[[ProjectState], None] | None = None,
    on_transition: Callable[[ProjectState, ProjectState], None] | None = None,
) -> DebatePipelineResult:
    """Execute the fixed 3-call pipeline: Pragmatist -> Structuralist -> Mediator (AC-1.3)."""
    wrapper = _PipelineLLMWrapper(inner=llm, on_call=on_llm_call)
    pragmatist = Pragmatist(llm=wrapper, max_retries=max_retries, clock=clock)
    structuralist = Structuralist(llm=wrapper, max_retries=max_retries, clock=clock)
    mediator = Mediator(
        llm=wrapper,
        max_retries=max_retries,
        valid_templates=valid_templates,
        clock=clock,
    )

    try:
        proposal = pragmatist.propose(spec)
        critique = structuralist.critique(spec, proposal)

        if on_transition is not None:
            on_transition(ProjectState.DEBATE, ProjectState.MEDIATION)
        wrapper.current_state = ProjectState.MEDIATION

        plan = mediator.mediate(spec, proposal, critique)

        return DebatePipelineResult(
            state=ProjectState.WAITING_APPROVAL,
            proposal=proposal,
            critique=critique,
            plan=plan,
            failure_cause=None,
            llm_calls=wrapper.call_count,
        )
    except (
        LLMError,
        PlanValidationError,
        ValueError,
        TimeoutError,
        OSError,
        RuntimeError,
    ) as err:
        return DebatePipelineResult(
            state=ProjectState.FAILED,
            proposal=None,
            critique=None,
            plan=None,
            failure_cause=str(err),
            llm_calls=wrapper.call_count,
        )
