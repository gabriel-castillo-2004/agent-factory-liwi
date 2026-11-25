"""Centralized runtime settings and workspace pytest isolation helpers (INV-19)."""

from __future__ import annotations

import importlib.util
import math
import os
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field, field_validator

from agent_factory.secrets import (
    DEFAULT_SECRET_ENV_KEYS,
    FORBIDDEN_ENV_SUBSTRINGS,
    redact_secrets,
)

DEFAULT_WORKSPACE_PYTEST_TIMEOUT: float = 180.0
DEFAULT_WORKSPACE_TEST_ITEM_TIMEOUT_SECONDS: int = 15

_WORKSPACE_SECRET_ENV_SUBSTRINGS: tuple[str, ...] = (
    *FORBIDDEN_ENV_SUBSTRINGS,
    "TOKEN",
    "API_KEY",
    "PRIVATE_KEY",
)


def parse_positive_timeout(
    raw: object,
    *,
    env_var_name: str,
    default: float,
    extra_secrets: Sequence[str] = (),
) -> float:
    """Parse a finite positive float timeout string or default without leaking secrets."""
    if raw is None:
        return float(default)
    text = str(raw).strip()
    if not text:
        return float(default)
    try:
        parsed = float(text)
    except ValueError as exc:
        clean_err = redact_secrets(f"Invalid {env_var_name}: {exc}", extra_secrets=extra_secrets)
        raise ValueError(clean_err) from None
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{env_var_name} must be a finite positive number")
    return parsed


def parse_workspace_pytest_timeout(
    env: Mapping[str, str] | None = None,
    *,
    extra_secrets: Sequence[str] = (),
) -> float:
    """Read and validate WORKSPACE_PYTEST_TIMEOUT (defaults to 180.0 seconds)."""
    effective = os.environ if env is None else env
    return parse_positive_timeout(
        effective.get("WORKSPACE_PYTEST_TIMEOUT"),
        env_var_name="WORKSPACE_PYTEST_TIMEOUT",
        default=DEFAULT_WORKSPACE_PYTEST_TIMEOUT,
        extra_secrets=extra_secrets,
    )


class FactorySettings(BaseModel):
    """Runtime settings for Agent Factory workers and workspace test execution."""

    workspace_pytest_timeout: float = Field(default_factory=parse_workspace_pytest_timeout)

    @field_validator("workspace_pytest_timeout")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("workspace_pytest_timeout must be a finite positive number")
        return float(value)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        extra_secrets: Sequence[str] = (),
    ) -> FactorySettings:
        """Build FactorySettings reading WORKSPACE_PYTEST_TIMEOUT from env."""
        return cls(
            workspace_pytest_timeout=parse_workspace_pytest_timeout(
                env, extra_secrets=extra_secrets
            )
        )


def build_workspace_pytest_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build subprocess env from os.environ scrubbing secrets and disabling external agent CLIs."""
    effective = os.environ if base_env is None else base_env
    clean: dict[str, str] = {}
    for key, val in effective.items():
        key_up = key.upper()
        if key_up in DEFAULT_SECRET_ENV_KEYS or any(
            sub in key_up for sub in _WORKSPACE_SECRET_ENV_SUBSTRINGS
        ):
            continue
        clean[key] = val
    clean["AGY_CLI_DISABLED"] = "1"
    clean["AGY_BIN"] = "__disabled_in_pytest__"
    return clean


def workspace_pytest_timeout_args(
    per_test_timeout_seconds: int = DEFAULT_WORKSPACE_TEST_ITEM_TIMEOUT_SECONDS,
) -> list[str]:
    """Return --timeout=N when pytest-timeout plugin is installed in the active environment."""
    if importlib.util.find_spec("pytest_timeout") is not None:
        return [f"--timeout={int(per_test_timeout_seconds)}"]
    return []
