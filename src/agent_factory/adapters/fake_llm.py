"""Scripted fake LLM adapter for offline deterministic testing (Section 7.2)."""

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from agent_factory.ports.llm import InvalidLLMResponseError


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
        self._responses.append(response)

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume next scripted response and validate against schema."""
        self.calls.append({"messages": messages, "schema": schema})

        if not self._responses:
            raise RuntimeError("FakeLLM response queue is empty")

        resp = self._responses.pop(0)

        if isinstance(resp, Exception):
            raise resp

        data: dict[str, Any]
        if isinstance(resp, str):
            try:
                parsed = json.loads(resp)
                if not isinstance(parsed, dict):
                    raise InvalidLLMResponseError(f"Expected JSON object, got: {type(parsed)}")
                data = parsed
            except (json.JSONDecodeError, InvalidLLMResponseError) as err:
                raise InvalidLLMResponseError(f"Invalid JSON string: {resp}") from err
        elif isinstance(resp, dict):
            data = resp
        else:
            raise InvalidLLMResponseError(f"Unexpected response type: {type(resp)}")

        if schema is not None and isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
                validated = schema.model_validate(data)
                return validated.model_dump(mode="json")
            except ValidationError as err:
                raise InvalidLLMResponseError(f"Response failed schema validation: {err}") from err

        return data
