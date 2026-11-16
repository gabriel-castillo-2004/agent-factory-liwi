"""Structuralist agent: step 2 of the fixed 3-step debate pipeline (Section 4, Section 7.4)."""

from pydantic import BaseModel, Field

from agent_factory.agents.pragmatist import PragmatistProposal
from agent_factory.domain.models import Spec
from agent_factory.ports.llm import LLM, InvalidLLMResponseError


class StructuralistCritique(BaseModel):
    """Critique of PragmatistProposal and robust counter-proposal."""

    critique: list[str] = Field(default_factory=list)
    robust_proposal: str
    architectural_safeguards: list[str] = Field(default_factory=list)


class Structuralist:
    """Critiques Pragmatist proposal and proposes robust safeguards."""

    def __init__(self, llm: LLM, max_retries: int = 2) -> None:
        self.llm = llm
        self.max_retries = max_retries

    def critique(self, spec: Spec, proposal: PragmatistProposal) -> StructuralistCritique:
        """Critique proposal and suggest robust architecture with bounded retries."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Structuralist, an architectural agent. Critique the pragmatic proposal, "
                    "identify failure modes, and propose robust architectural safeguards."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Specification:\n{spec.model_dump_json()}\n\n"
                    f"Pragmatist Proposal:\n{proposal.model_dump_json()}"
                ),
            },
        ]

        last_err: Exception | None = None
        for _ in range(self.max_retries + 1):
            try:
                data = self.llm.complete(messages=messages, schema=StructuralistCritique)
                return StructuralistCritique.model_validate(data)
            except (InvalidLLMResponseError, ValueError) as err:
                last_err = err

        assert last_err is not None
        raise last_err
