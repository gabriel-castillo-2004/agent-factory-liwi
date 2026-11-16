"""Scripted fake LLM adapter for offline deterministic testing (Section 7.2)."""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel


class FakeLLM:
    """Fake LLM that returns scripted responses in FIFO order."""

    def __init__(
        self,
        responses: Sequence[dict[str, Any] | str | Exception] | None = None,
    ) -> None:
        self._responses: list[dict[str, Any] | str | Exception] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, response: dict[str, Any] | str | Exception) -> None:
        """Add an additional scripted response to the queue."""
        raise NotImplementedError

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume next scripted response and validate against schema."""
        raise NotImplementedError
