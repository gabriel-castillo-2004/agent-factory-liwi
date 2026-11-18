"""LLM adapters using local `agy` CLI and automatic API-to-agy fallback (INV-02, INV-09, INV-18, INV-19)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from pydantic import BaseModel

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.ports.llm import LLM, InvalidLLMResponseError, LLMError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\r?\n?(.*?)\r?\n?```", re.DOTALL | re.IGNORECASE)


def _extract_schema_dict(schema: type[BaseModel] | dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return dict(schema)
    return None


def _build_combined_prompt(
    messages: list[dict[str, str]],
    schema: type[BaseModel] | dict[str, Any] | None = None,
) -> str:
    """Format system/user messages and strict JSON instruction into a single prompt."""
    sections: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).strip().upper() or "USER"
        content = str(msg.get("content", ""))
        sections.append(f"[{role}]\n{content}")

    if schema is not None:
        schema_dict = _extract_schema_dict(schema)
        schema_block = (
            f"\nJSON Schema:\n{json.dumps(schema_dict, ensure_ascii=False)}"
            if schema_dict
            else ""
        )
        sections.append(
            "[INSTRUCTIONS]\n"
            "Respond ONLY with a valid JSON object matching the requested schema. "
            "Do not include explanations or text outside the JSON object."
            + schema_block
        )
    else:
        sections.append(
            "[INSTRUCTIONS]\n"
            "Respond ONLY with a valid JSON object. "
            "Do not include explanations or text outside the JSON object."
        )

    return "\n\n".join(sections)


class AgyLLMAdapter:
    """LLM adapter executing local `agy` CLI via Runner with sanitized environment."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        cli_command: Sequence[str] = ("agy",),
        cwd: Path | None = None,
        timeout_seconds: float = 60.0,
        allowed_cli_env_keys: Sequence[str] = ("AGY_HOME", "AGY_CONFIG_DIR"),
        secrets: Sequence[str] = (),
    ) -> None:
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.cli_command: tuple[str, ...] = tuple(cli_command)
        self.cwd: Path = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
        self.timeout_seconds = timeout_seconds
        self.allowed_cli_env_keys: tuple[str, ...] = tuple(allowed_cli_env_keys)
        self.secrets: tuple[str, ...] = tuple(secrets)

    def build_clean_env(
        self,
        host_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Construct a sanitized environment stripping host secrets (INV-19)."""
        return build_sanitized_cli_env(self.allowed_cli_env_keys, host_env=host_env)

    def _raise_error(self, message: str, *, invalid_response: bool = False) -> NoReturn:
        clean_msg = redact_secrets(message, extra_secrets=self.secrets)
        err: LLMError = (
            InvalidLLMResponseError(clean_msg) if invalid_response else LLMError(clean_msg)
        )
        err.__cause__ = None
        err.__context__ = None
        raise err

    def _parse_json_dict(self, stdout: str) -> dict[str, Any]:
        stripped = stdout.strip()
        if not stripped:
            self._raise_error("agy CLI returned empty output", invalid_response=True)

        candidates = [stripped] + [match.strip() for match in _JSON_FENCE_RE.findall(stripped)]
        saw_non_dict_json = False
        for candidate in candidates:
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
            saw_non_dict_json = True

        if saw_non_dict_json:
            self._raise_error(
                "Expected JSON object dictionary from agy CLI, got non-dict JSON",
                invalid_response=True,
            )
        self._raise_error(
            f"Failed to parse JSON dictionary from agy CLI output: {stripped[:200]}",
            invalid_response=True,
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke `agy` CLI with combined prompt and return parsed JSON dict (INV-02, INV-18)."""
        prompt = _build_combined_prompt(messages, schema=schema)
        command = list(self.cli_command) + ["-p", prompt]
        clean_env = self.build_clean_env()

        res: CommandResult | None = None
        run_error: str | None = None
        try:
            res = self.runner.run(
                command=command,
                cwd=self.cwd,
                timeout_seconds=self.timeout_seconds,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            run_error = f"agy CLI execution failed: {exc}"

        if run_error is not None:
            self._raise_error(run_error)

        assert res is not None
        if res.timed_out:
            detail = (res.stderr or res.stdout).strip()
            self._raise_error(
                f"agy CLI timed out: {detail}" if detail else "agy CLI timed out"
            )

        if res.exit_code != 0:
            detail = (res.stderr or res.stdout).strip()
            self._raise_error(
                f"agy CLI exited with code {res.exit_code}: {detail}"
                if detail
                else f"agy CLI exited with code {res.exit_code}"
            )

        return self._parse_json_dict(res.stdout)


class FallbackLLMAdapter:
    """Composite LLM adapter trying primary first and switching to fallback on LLMError."""

    def __init__(self, primary: LLM, fallback: LLM) -> None:
        self.primary = primary
        self.fallback = fallback

    @property
    def transport(self) -> Any:
        """Expose primary HTTP transport when available."""
        return getattr(self.primary, "transport", None)

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call primary.complete(...) and delegate to fallback.complete(...) on LLMError."""
        primary_failed = False
        try:
            return self.primary.complete(messages, schema=schema)
        except LLMError:
            primary_failed = True

        if primary_failed:
            return self.fallback.complete(messages, schema=schema)
        return {}
