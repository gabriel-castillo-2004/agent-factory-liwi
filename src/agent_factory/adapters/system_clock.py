"""System clock adapter implementing the Clock port (INV-02, INV-09)."""

from datetime import datetime


class SystemClock:
    """Real system clock adapter for UTC time and backoff sleep."""

    def now(self) -> datetime:
        """Return current UTC datetime."""
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        """Sleep for the given duration in seconds (INV-09)."""
        raise NotImplementedError
