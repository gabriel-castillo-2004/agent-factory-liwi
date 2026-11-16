"""Mediator agent and fixed 3-step debate pipeline (Section 4, Section 7.4, AC-1.3, AC-1.4)."""

from collections.abc import Set as AbstractSet

from pydantic import BaseModel

from agent_factory.agents.pragmatist import PragmatistProposal
from agent_factory.agents.structuralist import StructuralistCritique
from agent_factory.domain.models import VALID_TEMPLATES, Plan, Spec
from agent_factory.domain.states import ProjectState
from agent_factory.ports.llm import LLM


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
    raise NotImplementedError


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
        raise NotImplementedError


def run_debate_pipeline(
    spec: Spec,
    llm: LLM,
    max_retries: int = 2,
    valid_templates: AbstractSet[str] = VALID_TEMPLATES,
) -> DebatePipelineResult:
    """Execute the fixed 3-call pipeline: Pragmatist -> Structuralist -> Mediator (AC-1.3)."""
    raise NotImplementedError
