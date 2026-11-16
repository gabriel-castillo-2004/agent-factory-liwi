"""Port definition for LLM interactions (INV-02, Section 7.2)."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class LLMError(Exception):
    """Base exception for LLM adapter and transport failures (INV-09, AC-1.13)."""


class InvalidLLMResponseError(LLMError):
    """Raised when an LLM returns malformed JSON or violates the requested schema."""


@runtime_checkable
class LLM(Protocol):
    """Protocol for language model adapters."""

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a completion request and return a parsed JSON dictionary."""
        raise NotImplementedError
