"""Mediator agent and fixed 3-step debate pipeline (Section 4, Section 7.4, AC-1.3, AC-1.4)."""

from collections.abc import Set as AbstractSet

from pydantic import BaseModel

from agent_factory.agents.pragmatist import Pragmatist, PragmatistProposal
from agent_factory.agents.structuralist import Structuralist, StructuralistCritique
from agent_factory.domain.models import VALID_TEMPLATES, Plan, Spec
from agent_factory.domain.states import ProjectState
from agent_factory.ports.llm import LLM, InvalidLLMResponseError


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
    ) -> None:
        self.llm = llm
        self.max_retries = max_retries
        self.valid_templates = valid_templates

    def mediate(
        self,
        spec: Spec,
        proposal: PragmatistProposal,
        critique: StructuralistCritique,
    ) -> Plan:
        """Produce and validate the Master Plan with bounded retries on invalid JSON."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Mediator, the final debate synthesis agent. Produce a validated Master Plan "
                    "reconciling Pragmatist and Structuralist positions. You must specify exact file contracts, "
                    "non-empty acceptance tests, and use an existing template_id."
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

        attempts = 0
        last_err: Exception | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                data = self.llm.complete(messages=messages, schema=None)
                plan = Plan.model_validate(data)
                return validate_plan(plan, self.valid_templates)
            except PlanValidationError:
                # Direct validation errors must propagate or fail
                raise
            except (InvalidLLMResponseError, ValueError) as err:
                last_err = err

        assert last_err is not None
        raise last_err


def run_debate_pipeline(
    spec: Spec,
    llm: LLM,
    max_retries: int = 2,
    valid_templates: AbstractSet[str] = VALID_TEMPLATES,
) -> DebatePipelineResult:
    """Execute the fixed 3-call pipeline: Pragmatist -> Structuralist -> Mediator (AC-1.3)."""
    pragmatist = Pragmatist(llm=llm, max_retries=max_retries)
    structuralist = Structuralist(llm=llm, max_retries=max_retries)
    mediator = Mediator(llm=llm, max_retries=max_retries, valid_templates=valid_templates)

    start_calls = len(getattr(llm, "calls", []))

    try:
        proposal = pragmatist.propose(spec)
        critique = structuralist.critique(spec, proposal)
        plan = mediator.mediate(spec, proposal, critique)

        total_calls = len(getattr(llm, "calls", [])) - start_calls
        return DebatePipelineResult(
            state=ProjectState.WAITING_APPROVAL,
            proposal=proposal,
            critique=critique,
            plan=plan,
            failure_cause=None,
            llm_calls=total_calls,
        )
    except (
        InvalidLLMResponseError,
        PlanValidationError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
    ) as err:
        total_calls = len(getattr(llm, "calls", [])) - start_calls
        return DebatePipelineResult(
            state=ProjectState.FAILED,
            proposal=None,
            critique=None,
            plan=None,
            failure_cause=str(err),
            llm_calls=total_calls,
        )
