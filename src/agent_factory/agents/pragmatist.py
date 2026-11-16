"""Pragmatist agent: step 1 of the fixed 3-step debate pipeline (Section 4, Section 7.4)."""

from pydantic import BaseModel, Field

from agent_factory.domain.models import Spec
from agent_factory.ports.clock import Clock
from agent_factory.ports.llm import LLM, LLMError


class PragmatistProposal(BaseModel):
    """Simple, direct architecture proposal produced by Pragmatist."""

    approach: str
    components: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)


class Pragmatist:
    """Proposes the simplest viable architecture for a Spec."""

    def __init__(self, llm: LLM, max_retries: int = 2, clock: Clock | None = None) -> None:
        self.llm = llm
        self.max_retries = max_retries
        self.clock = clock

    def propose(self, spec: Spec) -> PragmatistProposal:
        """Generate pragmatic proposal with bounded retries on invalid JSON."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Pragmatist, an architectural agent. Propose the simplest, most direct "
                    "implementation route with minimal dependencies and zero unnecessary abstraction."
                ),
            },
            {
                "role": "user",
                "content": f"Specification:\n{spec.model_dump_json()}",
            },
        ]

        attempts = 0
        last_err: Exception | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                data = self.llm.complete(messages=messages, schema=PragmatistProposal)
                return PragmatistProposal.model_validate(data)
            except (LLMError, ValueError, TimeoutError, OSError) as err:
                last_err = err
                if attempts <= self.max_retries and self.clock is not None and hasattr(self.clock, "sleep"):
                    self.clock.sleep(0.1 * (2 ** (attempts - 1)))

        assert last_err is not None
        raise last_err
