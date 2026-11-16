"""Port definition for deterministic clock access (INV-02, Section 6)."""

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Protocol for retrieving current time."""

    def now(self) -> datetime:
        """Return current UTC datetime."""
        raise NotImplementedError
