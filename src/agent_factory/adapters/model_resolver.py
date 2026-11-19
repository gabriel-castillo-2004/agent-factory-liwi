"""Dynamic model discovery, stage-based preference resolution, and runtime fallback for `agy` CLI."""

from __future__ import annotations

import logging
import re
import threading
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

logger = logging.getLogger(__name__)

_AVAILABLE_MODELS_HEADER_RE = re.compile(
    r"Available\s+models\s*:(.*)",
    re.IGNORECASE | re.DOTALL,
)
_SECTION_HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _/-]{1,40}:\s*$")
_LIST_MARKER_RE = re.compile(r"^(?:[-*•]+|\d+[.)])\s*")
_PROSE_TERMINATOR_RE = re.compile(
    r"^(?:use|run|see|for\s+more|error:|note:|invalid\s+model)\b",
    re.IGNORECASE,
)

_STRONG_STAGES: frozenset[str] = frozenset(
    {
        "BUILDING",
        "BUILD",
        "GENERATE_TESTS",
        "GENERATE-TESTS",
        "IMPLEMENT",
        "IMPLEMENT_OR_FIX",
        "MEDIATION",
        "DEBATE",
        "STRONG",
    }
)

DEFAULT_STAGE_PREFERENCES: dict[str, tuple[str, ...]] = {
    "strong": (
        "Neon (High)",
        "Argon (High)",
        "Claude Sonnet",
        "Gemini 3.8 Flash (High)",
    ),
    "fast": (
        "Gemini 3.8 Flash",
        "Hydrogen",
        "Argon",
    ),
}

_MODEL_ERROR_PHRASES: tuple[str, ...] = (
    "invalid model selection",
    "not recognized as a known model",
    "unknown model",
    "invalid model",
    "model not found",
    "unsupported model",
    "available models:",
)

_QUOTA_ERROR_PHRASES: tuple[str, ...] = (
    "too many requests",
    "quota exhausted",
    "quota exceeded",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "insufficient quota",
    "exhausted your capacity",
    "capacity exhausted",
)

_HTTP_429_RE = re.compile(r"\b(?:http|status|error|code)\s*[:=]?\s*429\b", re.IGNORECASE)


def _clean_configured_model(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() == "CONFIGURE_ME":
        return None
    return s


def parse_available_models(output: str) -> list[str]:
    """Extract ordered, deduplicated model names listed under `Available models:` in CLI output."""
    if not output:
        return []
    match = _AVAILABLE_MODELS_HEADER_RE.search(output)
    if match is None:
        return []

    tail = match.group(1)
    lines = tail.splitlines()
    models: list[str] = []
    seen_lower: set[str] = set()

    def _add(raw_item: str) -> None:
        candidate = _LIST_MARKER_RE.sub("", raw_item.strip()).strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in ('"', "'"):
            candidate = candidate[1:-1].strip()
        if not candidate:
            return
        key = candidate.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            models.append(candidate)

    if lines:
        inline = lines[0].strip()
        if inline:
            if "," in inline:
                for part in inline.split(","):
                    _add(part)
            else:
                _add(inline)

    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            if models:
                break
            continue
        if _SECTION_HEADER_RE.match(stripped):
            break
        if (
            models
            and not raw_line.startswith((" ", "\t", "-", "*", "•"))
            and _PROSE_TERMINATOR_RE.match(stripped)
        ):
            break
        _add(stripped)

    return models


def is_model_selection_error(output: str) -> bool:
    """Return True if CLI output indicates an unknown or invalid model name."""
    if not output:
        return False
    lowered = output.lower()
    return any(phrase in lowered for phrase in _MODEL_ERROR_PHRASES)


def is_quota_error(exit_code: int, output: str) -> bool:
    """Return True if exit code or output indicates quota/rate-limit exhaustion."""
    if exit_code in {42, 429}:
        return True
    if not output:
        return False
    lowered = output.lower()
    if any(phrase in lowered for phrase in _QUOTA_ERROR_PHRASES):
        return True
    return bool(_HTTP_429_RE.search(output))


def is_retryable_model_or_quota_error(exit_code: int, output: str) -> bool:
    """Return True when CLI failure warrants trying another fallback model."""
    return is_model_selection_error(output) or is_quota_error(exit_code, output)


def read_configured_model_for_stage(
    models_config_path: Path | None,
    role_or_stage: str,
) -> str | None:
    """Read configured model for a lifecycle stage from models.toml without placeholders."""
    if models_config_path is None or not models_config_path.is_file():
        return None
    try:
        with models_config_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    stage_upper = (role_or_stage or "").strip().upper()
    stage_lower = stage_upper.lower()
    is_strong = stage_upper in _STRONG_STAGES

    for k, v in data.items():
        if isinstance(v, str) and k.strip().upper() == stage_upper:
            cleaned = _clean_configured_model(v)
            if cleaned is not None:
                return cleaned

    models_sec = data.get("models")
    if isinstance(models_sec, dict):
        for k, v in models_sec.items():
            if k.strip().lower() == stage_lower:
                cleaned = _clean_configured_model(v)
                if cleaned is not None:
                    return cleaned
        tier_key = "strong" if is_strong else "fast"
        cleaned = _clean_configured_model(models_sec.get(tier_key))
        if cleaned is not None:
            return cleaned

    tier_key = "strong" if is_strong else "fast"
    return _clean_configured_model(data.get(tier_key))


def _load_preferences_from_toml(models_config_path: Path | None) -> dict[str, tuple[str, ...]]:
    if models_config_path is None or not models_config_path.is_file():
        return {}
    try:
        with models_config_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    prefs: dict[str, tuple[str, ...]] = {}
    for section_key in ("fallbacks", "preferences"):
        sec = data.get(section_key)
        if isinstance(sec, dict):
            for k, v in sec.items():
                if isinstance(v, list):
                    cleaned = tuple(str(x).strip() for x in v if str(x).strip())
                    if cleaned:
                        prefs[str(k).strip().upper()] = cleaned
    return prefs


def _pick_preferred_model(
    preferences: Sequence[str],
    available: Sequence[str],
) -> str:
    """Select the highest-priority model matching preference hints, or the first available model."""
    for pattern in preferences:
        pat_low = pattern.strip().lower()
        if not pat_low:
            continue
        for candidate in available:
            if pat_low in candidate.lower():
                return candidate
        tokens = [tok for tok in re.split(r"[\s()]+", pat_low) if tok]
        if len(tokens) > 1:
            for candidate in available:
                cand_low = candidate.lower()
                if all(tok in cand_low for tok in tokens):
                    return candidate
    return available[0]


class ModelResolver:
    """Thread-safe model discovery cache, stage preference resolver, and runtime fallback tracker."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        cli_command: Sequence[str] = ("agy",),
        cwd: Path | None = None,
        allowed_cli_env_keys: Sequence[str] = ("AGY_HOME", "AGY_CONFIG_DIR"),
        models_config_path: Path | None = None,
        preference_patterns: Mapping[str, Sequence[str]] | None = None,
        secrets: Sequence[str] = (),
        probe_timeout_seconds: float = 10.0,
        auto_probe: bool | None = None,
        env_builder: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        if runner is None:
            from agent_factory.adapters.subprocess_runner import SubprocessRunner

            self.runner: Runner = SubprocessRunner(default_timeout_seconds=probe_timeout_seconds)
        else:
            self.runner = runner
        self.cli_command: tuple[str, ...] = tuple(cli_command)
        self.cwd: Path = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
        self.allowed_cli_env_keys: tuple[str, ...] = tuple(allowed_cli_env_keys)
        self.models_config_path = models_config_path
        self.secrets: tuple[str, ...] = tuple(secrets)
        self.probe_timeout_seconds = probe_timeout_seconds
        self._env_builder = env_builder

        is_real_agy = bool(self.cli_command) and Path(self.cli_command[0]).stem.lower() == "agy"
        if auto_probe is None:
            self.auto_probe = (
                self.runner.__class__.__name__ == "SubprocessRunner" and is_real_agy
            )
        else:
            self.auto_probe = auto_probe

        self._custom_preferences: dict[str, tuple[str, ...]] = _load_preferences_from_toml(
            models_config_path
        )
        if preference_patterns is not None:
            for k, v in preference_patterns.items():
                cleaned = tuple(str(x).strip() for x in v if str(x).strip())
                if cleaned:
                    self._custom_preferences[str(k).strip().upper()] = cleaned

        self._discovered_models: list[str] | None = None
        self._failed_models: set[str] = set()

    def _build_clean_env(self) -> dict[str, str]:
        if self._env_builder is not None:
            return self._env_builder()
        from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env

        return build_sanitized_cli_env(self.allowed_cli_env_keys)

    def _preferences_for_stage(self, role_or_stage: str) -> tuple[str, ...]:
        stage_upper = (role_or_stage or "").strip().upper()
        if stage_upper in self._custom_preferences:
            return self._custom_preferences[stage_upper]
        tier = "STRONG" if stage_upper in _STRONG_STAGES else "FAST"
        if tier in self._custom_preferences:
            return self._custom_preferences[tier]
        return DEFAULT_STAGE_PREFERENCES[tier.lower()]

    def discover_available_models(self, force_refresh: bool = False) -> list[str]:
        """Probe `agy` CLI and cache available models listed under `Available models:`."""
        with self._lock:
            if not force_refresh and self._discovered_models is not None:
                return list(self._discovered_models)

            cmd = list(self.cli_command) + ["-p", "ping", "--model", "__probe__"]
            clean_env = self._build_clean_env()
            output_text = ""
            try:
                try:
                    res = self.runner.run(
                        command=cmd,
                        cwd=self.cwd,
                        timeout_seconds=self.probe_timeout_seconds,
                        env=clean_env,
                        input="",
                    )
                except TypeError as exc:
                    if "input" in str(exc):
                        res = self.runner.run(
                            command=cmd,
                            cwd=self.cwd,
                            timeout_seconds=self.probe_timeout_seconds,
                            env=clean_env,
                        )
                    else:
                        raise
                output_text = f"{res.stdout}\n{res.stderr}".strip()
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
                clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
                logger.warning("Failed to probe agy available models: %s", clean_err)
                if self._discovered_models is None:
                    self._discovered_models = []
                return list(self._discovered_models)

            parsed = parse_available_models(output_text)
            self._discovered_models = parsed
            if parsed:
                logger.info(
                    "Discovered %d available agy models: %s",
                    len(parsed),
                    redact_secrets(", ".join(parsed), extra_secrets=self.secrets),
                )
            else:
                logger.debug(
                    "agy probe returned no 'Available models:' list; keeping configured defaults."
                )
            return list(parsed)

    def resolve_model_for_role(
        self,
        role_or_stage: str,
        configured_model: str | None = None,
    ) -> str | None:
        """Resolve an active model for role_or_stage, substituting unavailable configured models."""
        configured = _clean_configured_model(configured_model)
        with self._lock:
            if self._discovered_models is None and self.auto_probe:
                self.discover_available_models()

            discovered = list(self._discovered_models or [])
            if not discovered:
                if configured is not None:
                    logger.debug(
                        "No discovered agy models; using configured model %r for stage %s",
                        redact_secrets(configured, extra_secrets=self.secrets),
                        role_or_stage,
                    )
                return configured

            available = [m for m in discovered if m.lower() not in self._failed_models]
            if not available:
                logger.warning(
                    "All %d discovered agy models are marked failed for stage %s",
                    len(discovered),
                    role_or_stage,
                )
                return None

            if configured is not None:
                for candidate in available:
                    if candidate == configured or candidate.lower() == configured.lower():
                        logger.info(
                            "Validated configured model %r for stage %s",
                            redact_secrets(candidate, extra_secrets=self.secrets),
                            role_or_stage,
                        )
                        return candidate

            prefs = self._preferences_for_stage(role_or_stage)
            selected = _pick_preferred_model(prefs, available)
            if configured is not None:
                logger.warning(
                    "Configured model %r for stage %s is unavailable; using fallback model %r",
                    redact_secrets(configured, extra_secrets=self.secrets),
                    role_or_stage,
                    redact_secrets(selected, extra_secrets=self.secrets),
                )
            else:
                logger.info(
                    "Resolved model %r for stage %s from discovered agy models",
                    redact_secrets(selected, extra_secrets=self.secrets),
                    role_or_stage,
                )
            return selected

    def next_fallback_model(
        self,
        role_or_stage: str,
        failed_model: str | None,
        *,
        error_output: str = "",
    ) -> str | None:
        """Mark failed_model unavailable, refresh discovered models if needed, and return next candidate."""
        with self._lock:
            if failed_model is not None:
                cleaned_failed = failed_model.strip()
                if cleaned_failed:
                    self._failed_models.add(cleaned_failed.lower())

            parsed_from_err = parse_available_models(error_output) if error_output else []
            if parsed_from_err:
                self._discovered_models = parsed_from_err
                logger.info(
                    "Updated available agy models from CLI error output (%d candidates)",
                    len(parsed_from_err),
                )
            elif self.auto_probe and not self._discovered_models:
                self.discover_available_models(force_refresh=True)

            discovered = list(self._discovered_models or [])
            if not discovered:
                return None

            failed_lower = (failed_model or "").strip().lower()
            available = [
                m
                for m in discovered
                if m.lower() not in self._failed_models and (not failed_lower or m.lower() != failed_lower)
            ]
            if not available and self.auto_probe and not parsed_from_err:
                refreshed = self.discover_available_models(force_refresh=True)
                available = [
                    m
                    for m in refreshed
                    if m.lower() not in self._failed_models
                    and (not failed_lower or m.lower() != failed_lower)
                ]

            if not available:
                logger.error(
                    "No remaining fallback models for stage %s after failure of %r",
                    role_or_stage,
                    redact_secrets(str(failed_model or "<default>"), extra_secrets=self.secrets),
                )
                return None

            prefs = self._preferences_for_stage(role_or_stage)
            selected = _pick_preferred_model(prefs, available)
            logger.warning(
                "Substituted failed model %r with fallback model %r for stage %s",
                redact_secrets(str(failed_model or "<default>"), extra_secrets=self.secrets),
                redact_secrets(selected, extra_secrets=self.secrets),
                role_or_stage,
            )
            return selected

    def clear_failed_models(self) -> None:
        """Clear temporarily marked failed models (e.g., on quota reset)."""
        with self._lock:
            self._failed_models.clear()
