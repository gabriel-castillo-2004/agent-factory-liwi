"""Secret redaction utilities for logs, events, reports, and error messages (INV-07, INV-19, AC-3.5)."""

from collections.abc import Mapping, Sequence

DEFAULT_SECRET_ENV_KEYS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "GITHUB_TOKEN",
    "VERCEL_TOKEN",
)


def redact_secrets(
    text: str,
    extra_secrets: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> str:
    """Remove explicit tokens and credential patterns from text, replacing them with [REDACTED]."""
    raise NotImplementedError
