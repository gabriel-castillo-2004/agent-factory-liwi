"""Secret redaction utilities for logs, events, reports, and error messages (INV-07, INV-19, AC-3.5)."""

import os
import re
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
    if not text:
        return text

    result = str(text)

    # 1. Redact via URL, header, and token prefix patterns
    # Telegram bot API URL pattern: https://api.telegram.org/bot<token>/...
    result = re.sub(
        r"(https?://api\.telegram\.org/bot)[^/\s\"']+",
        r"\1[REDACTED]",
        result,
    )

    # Telegram bot token outside api.telegram.org URL: bot<digits>:<token>
    result = re.sub(
        r"\bbot\d+:[A-Za-z0-9_-]{8,}",
        "bot[REDACTED]",
        result,
    )

    # HTTP basic auth / userinfo credentials in URL authority only (never cross '/' into path)
    result = re.sub(
        r"(https?://[^/\s:@\"']+:)[^/\s@\"']+(@)",
        r"\1[REDACTED]\2",
        result,
    )

    # URL query parameter credentials (e.g. ?token=..., &api_key=..., ?access_token=..., etc.)
    result = re.sub(
        r"([?&](?:token|api_key|apikey|access_token|secret|password|auth|client_secret)=)[^&\s#\"']+",
        r"\1[REDACTED]",
        result,
        flags=re.IGNORECASE,
    )

    # Bearer tokens in headers or messages
    result = re.sub(
        r"(Bearer\s+)[A-Za-z0-9_.-]+",
        r"\1[REDACTED]",
        result,
        flags=re.IGNORECASE,
    )

    # Well-known provider token prefixes (GitHub classic/fine-grained PATs, sk- API keys)
    result = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{12,})\b",
        "[REDACTED]",
        result,
    )

    # Key=value token assignments for standard secret variables
    result = re.sub(
        r"((?:GITHUB_TOKEN|VERCEL_TOKEN|TELEGRAM_BOT_TOKEN)\s*=\s*)[^\s;&,\"']+",
        r"\1[REDACTED]",
        result,
        flags=re.IGNORECASE,
    )

    # 2. Collect explicit secret strings from environment and parameters
    effective_env = os.environ if env is None else env
    secrets_to_scrub: set[str] = set()

    for sec in extra_secrets:
        if sec and isinstance(sec, str) and sec.strip():
            secrets_to_scrub.add(sec)

    for key, val in effective_env.items():
        if not val or not isinstance(val, str) or len(val.strip()) < 4:
            continue
        key_upper = key.upper()
        if key_upper in DEFAULT_SECRET_ENV_KEYS or any(
            key_upper.endswith(suffix) for suffix in ("_TOKEN", "_SECRET", "_KEY", "_PAT")
        ):
            secrets_to_scrub.add(val)

    # Scrub explicit secrets, longer ones first to avoid partial replacements
    for secret in sorted(secrets_to_scrub, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")

    return result
