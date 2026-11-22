"""LLM adapters using local `agy` CLI and automatic API-to-agy fallback (INV-02, INV-09, INV-18, INV-19)."""

from __future__ import annotations

import inspect
import json
import logging
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from pydantic import BaseModel

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.model_resolver import (
    ModelResolver,
    is_retryable_model_or_quota_error,
    read_configured_model_for_stage,
)
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.ports.llm import LLM, InvalidLLMResponseError, LLMError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\r?\n?(.*?)\r?\n?```", re.DOTALL | re.IGNORECASE)
MAX_CLI_ARG_PROMPT_CHARS = 6000


def _extract_schema_dict(schema: type[BaseModel] | dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return dict(schema)
    return None


def _build_example_skeleton(
    schema: dict[str, Any],
    defs: dict[str, Any] | None = None,
    seen_refs: set[str] | None = None,
) -> Any:
    """Generate a clean JSON example skeleton from a JSON schema with resolved refs and types."""
    if defs is None:
        defs = schema.get("$defs") or schema.get("definitions") or {}
    if seen_refs is None:
        seen_refs = set()

    ref = schema.get("$ref")
    if ref and isinstance(ref, str):
        ref_name = ref.split("/")[-1]
        if ref_name in seen_refs:
            return {}
        target = defs.get(ref_name, {})
        return _build_example_skeleton(target, defs=defs, seen_refs=seen_refs | {ref_name})

    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants and isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict) and variant.get("type") != "null":
                return _build_example_skeleton(variant, defs=defs, seen_refs=seen_refs)

    schema_type = schema.get("type")
    if schema.get("enum"):
        return schema["enum"][0]
    if schema_type == "object" or "properties" in schema:
        res: dict[str, Any] = {}
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for prop, p_schema in props.items():
                if isinstance(p_schema, dict):
                    if prop == "template_id" and p_schema.get("type") == "string" and not p_schema.get("default"):
                        res[prop] = "static-web"
                    elif prop == "id" and p_schema.get("type") == "string" and not p_schema.get("default"):
                        res[prop] = "AT-01"
                    else:
                        res[prop] = _build_example_skeleton(p_schema, defs=defs, seen_refs=seen_refs)
        return res
    if schema_type == "array":
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [_build_example_skeleton(item_schema, defs=defs, seen_refs=seen_refs)]
        return []
    if schema_type == "string":
        if schema.get("default"):
            return schema["default"]
        return "string"
    if schema_type == "integer":
        return schema.get("default", 0)
    if schema_type == "number":
        return schema.get("default", 0.0)
    if schema_type == "boolean":
        return schema.get("default", True)
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
        skeleton = _build_example_skeleton(schema_dict) if schema_dict else None
        skeleton_block = (
            f"\nExample JSON Skeleton:\n{json.dumps(skeleton, ensure_ascii=False, indent=2)}"
            if skeleton
            else ""
        )
        rules: list[str] = []
        if schema_dict and "properties" in schema_dict:
            props = schema_dict["properties"]
            if "template_id" in props:
                rules.append(
                    "- 'template_id' must be exactly one of: 'static-web', 'static-app' (recommended/primary template: 'static-web')"
                )
            if "acceptance_tests" in props:
                rules.append(
                    "- 'acceptance_tests' must be a non-empty list of objects, each containing: "
                    "'id' (string, e.g. 'AT-01'), 'description' (string), and 'type' (must be exactly 'unit', 'integration', or 'e2e')"
                )
            if "files" in props:
                rules.append(
                    "- 'files' must be a list of objects, each containing: "
                    "'path' (string), 'purpose' (string), and non-empty 'contract' (string)"
                )
        rules_block = ("\nSpecific Schema Constraints:\n" + "\n".join(rules)) if rules else ""
        sections.append(
            "[INSTRUCTIONS]\n"
            "Respond ONLY with a valid JSON object matching the requested schema. "
            "Do not include explanations or text outside the JSON object."
            + schema_block
            + skeleton_block
            + rules_block
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
        model: str | None = None,
        models_config_path: Path | None = None,
        model_resolver: ModelResolver | None = None,
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
        self.model = model
        self.models_config_path = models_config_path
        self.model_resolver: ModelResolver = (
            model_resolver
            if model_resolver is not None
            else ModelResolver(
                runner=self.runner,
                cli_command=self.cli_command,
                cwd=self.cwd,
                allowed_cli_env_keys=self.allowed_cli_env_keys,
                models_config_path=models_config_path,
                secrets=self.secrets,
            )
        )

    def build_clean_env(
        self,
        host_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Construct a sanitized environment stripping host secrets (INV-19)."""
        return build_sanitized_cli_env(self.allowed_cli_env_keys, host_env=host_env)

    @staticmethod
    def _infer_stage(
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None,
    ) -> str:
        if schema is not None:
            schema_name = getattr(schema, "__name__", str(schema))
            if "Plan" in schema_name:
                return "MEDIATION"
            if "Pragmatist" in schema_name or "Structuralist" in schema_name:
                return "DEBATE"
            if "Interview" in schema_name:
                return "INTERVIEW"

        for msg in messages:
            content_low = str(msg.get("content", "")).lower()
            if "mediator" in content_low or "mediation" in content_low:
                return "MEDIATION"
            if "pragmatist" in content_low or "structuralist" in content_low:
                return "DEBATE"
            if "interviewer" in content_low:
                return "INTERVIEW"
        return "INTERVIEW"

    def _configured_model_for_stage(self, stage: str) -> str | None:
        if self.model is not None:
            cleaned = self.model.strip()
            if cleaned and cleaned.upper() != "CONFIGURE_ME":
                return cleaned
        return read_configured_model_for_stage(self.models_config_path, stage)

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

    def _invoke_cli_prompt(
        self,
        *,
        prompt: str,
        active_model: str | None,
        clean_env: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute CLI keeping prompts <=6,000 chars inline and using a UTF-8 temp file in -p for longer prompts."""
        eff_timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if len(prompt) <= MAX_CLI_ARG_PROMPT_CHARS:
            command = list(self.cli_command) + ["-p", prompt]
            if active_model:
                command.extend(["--model", active_model])
            return self.runner.run(
                command=command,
                cwd=self.cwd,
                timeout_seconds=eff_timeout,
                env=clean_env,
            )

        tmp_file_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="agy_prompt_",
                delete=False,
            ) as tmp_file:
                tmp_file.write(prompt)
                tmp_file_path = Path(tmp_file.name)
            file_command = list(self.cli_command) + [
                "-p",
                f"Lee y sigue exactamente las instrucciones y el contexto del archivo: {tmp_file_path}",
            ]
            if active_model:
                file_command.extend(["--model", active_model])
            return self.runner.run(
                command=file_command,
                cwd=self.cwd,
                timeout_seconds=eff_timeout,
                env=clean_env,
            )
        finally:
            if tmp_file_path is not None:
                tmp_file_path.unlink(missing_ok=True)

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Invoke `agy` CLI with combined prompt and bounded model fallback retries (INV-02, INV-18)."""
        prompt = _build_combined_prompt(messages, schema=schema)
        stage = self._infer_stage(messages, schema)
        configured_model = self._configured_model_for_stage(stage)
        active_model = self.model_resolver.resolve_model_for_role(stage, configured_model)
        clean_env = self.build_clean_env()

        tried_models: set[str] = set()
        max_attempts = 3

        for attempt_idx in range(1, max_attempts + 1):
            self.model_resolver.record_active_usage(active_model, stage)
            logger.info(
                "AgyLLMAdapter invoking stage %s with model %r (attempt %d/%d)",
                stage,
                redact_secrets(str(active_model or "<default>"), extra_secrets=self.secrets),
                attempt_idx,
                max_attempts,
            )

            res: CommandResult | None = None
            run_error: str | None = None
            try:
                res = self._invoke_cli_prompt(
                    prompt=prompt,
                    active_model=active_model,
                    clean_env=clean_env,
                    timeout_seconds=timeout_seconds,
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

            if res.exit_code == 0:
                return self._parse_json_dict(res.stdout)

            combined_output = f"{res.stdout}\n{res.stderr}".strip()
            detail = (res.stderr or res.stdout).strip()
            if (
                is_retryable_model_or_quota_error(res.exit_code, combined_output)
                and attempt_idx < max_attempts
            ):
                tried_models.add((active_model or "").lower())
                candidate = self.model_resolver.next_fallback_model(
                    stage,
                    active_model,
                    error_output=combined_output,
                )
                while candidate is not None and candidate.lower() in tried_models:
                    candidate = self.model_resolver.next_fallback_model(stage, candidate)
                if candidate is not None:
                    logger.warning(
                        "AgyLLMAdapter retrying stage %s with fallback model %r (after %r failed)",
                        stage,
                        redact_secrets(candidate, extra_secrets=self.secrets),
                        redact_secrets(str(active_model or "<default>"), extra_secrets=self.secrets),
                    )
                    active_model = candidate
                    continue

            self._raise_error(
                f"agy CLI exited with code {res.exit_code}: {detail}"
                if detail
                else f"agy CLI exited with code {res.exit_code}"
            )

        self._raise_error("agy CLI exhausted all model retry attempts")

    def generate(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Execute completion supporting per-call timeout_seconds override."""
        return self.complete(messages, schema=schema, timeout_seconds=timeout_seconds)


class FallbackLLMAdapter:
    """Composite LLM adapter trying primary first and switching to fallback on LLMError."""

    def __init__(self, primary: LLM, fallback: LLM) -> None:
        self.primary = primary
        self.fallback = fallback

    @property
    def transport(self) -> Any:
        """Expose primary HTTP transport when available."""
        return getattr(self.primary, "transport", None)

    @property
    def model_resolver(self) -> Any:
        """Expose primary or fallback ModelResolver when available."""
        return getattr(self.fallback, "model_resolver", None) or getattr(
            self.primary, "model_resolver", None
        )

    @staticmethod
    def _invoke_with_optional_timeout(
        target: LLM,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if timeout_seconds is not None:
            try:
                sig = inspect.signature(target.complete)
                if "timeout_seconds" in sig.parameters:
                    return target.complete(
                        messages,
                        schema=schema,
                        timeout_seconds=timeout_seconds,
                    )
            except (TypeError, ValueError):
                pass
        return target.complete(messages, schema=schema)

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Call primary.complete(...) and delegate to fallback.complete(...) on LLMError."""
        primary_failed = False
        try:
            return self._invoke_with_optional_timeout(
                self.primary,
                messages,
                schema=schema,
                timeout_seconds=timeout_seconds,
            )
        except LLMError:
            primary_failed = True

        if primary_failed:
            return self._invoke_with_optional_timeout(
                self.fallback,
                messages,
                schema=schema,
                timeout_seconds=timeout_seconds,
            )
        return {}

    def generate(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Execute composite completion supporting per-call timeout_seconds override."""
        return self.complete(messages, schema=schema, timeout_seconds=timeout_seconds)
