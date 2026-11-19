"""Dynamic model discovery, stage-based preference resolution, and runtime fallback for `agy` CLI."""

from __future__ import annotations

import logging
import re
import threading
import tomllib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        "Gemini 4 Hydrogen (Medium)",
        "Gemini 4 Hydrogen (High)",
        "Gemini 4 Hydrogen (Low)",
        "Gemini 3.8 Flash (High)",
        "Gemini 3.8 Flash (Medium)",
    ),
    "fast": (
        "Gemini 3.8 Flash (Low)",
        "Gemini 3.8 Flash (Medium)",
        "Gemini 3.8 Flash (High)",
        "Gemini 4 Hydrogen (Low)",
    ),
}

_VERSION_NUM_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)*)\b")

ACTIVITY_MESSAGES: dict[str, str] = {
    "INTERVIEW": "Analizando tu idea y preparando preguntas...",
    "DEBATE": "2 especialistas están evaluando la mejor forma de construirlo...",
    "MEDIATION": "Unificando propuestas en un plan paso a paso...",
    "BUILDING_TESTS": "Creando las pruebas para asegurar que todo funcione...",
    "BUILDING_CODE": "Escribiendo el código de tu aplicación...",
    "VERIFYING": "Probando que la aplicación funcione sin errores...",
    "PUBLISHING": "Guardando tu proyecto terminado en GitHub...",
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


def _parse_flash_version(candidate: str) -> tuple[int, ...] | None:
    """Extract numeric version tuple (normalized to >=2 parts) from a Flash model name."""
    match = _VERSION_NUM_RE.search(candidate)
    if match is None:
        return None
    parts = [int(p) for p in match.group(1).split(".") if p.isdigit()]
    if not parts:
        return None
    if len(parts) == 1:
        parts.append(0)
    return tuple(parts)


def _effort_rank(candidate_lower: str) -> int:
    if "(low)" in candidate_lower or re.search(r"\blow\b", candidate_lower):
        return 0
    if "(medium)" in candidate_lower or re.search(r"\bmedium\b", candidate_lower):
        return 1
    if "(high)" in candidate_lower or re.search(r"\bhigh\b", candidate_lower):
        return 2
    return 3


def _find_preferred_match(
    preferences: Sequence[str],
    available: Sequence[str],
) -> str | None:
    """Return the first candidate in available matching preferences in priority order, or None."""
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
    return None


def _pick_preferred_model(
    preferences: Sequence[str],
    available: Sequence[str],
) -> str:
    """Select the highest-priority model matching preference hints, or the first available model."""
    matched = _find_preferred_match(preferences, available)
    return matched if matched is not None else available[0]


def _pick_fast_default_model(available: Sequence[str]) -> str:
    """Prioritize newest Flash (>=3.8: Low -> Medium -> High), then Hydrogen Low."""
    flash_candidates: list[tuple[tuple[int, ...], int, int, str]] = []
    for idx, candidate in enumerate(available):
        low = candidate.lower()
        if "flash" not in low:
            continue
        ver = _parse_flash_version(candidate)
        if ver is not None and ver < (3, 8):
            continue
        # Higher version first; unversioned Flash sits below versioned >=3.8
        rank_ver = ver if ver is not None else (0, 0)
        flash_candidates.append((rank_ver, -_effort_rank(low), -idx, candidate))

    if flash_candidates:
        flash_candidates.sort(reverse=True)
        return flash_candidates[0][3]

    matched = _find_preferred_match(
        ("Gemini 4 Hydrogen (Low)", "Hydrogen", "Flash"),
        available,
    )
    return matched if matched is not None else available[0]


def _pick_strong_default_model(available: Sequence[str]) -> str:
    """Prioritize Hydrogen Medium/High/Low, then Gemini 3.8 Flash High/Medium."""
    matched = _find_preferred_match(DEFAULT_STAGE_PREFERENCES["strong"], available)
    if matched is not None:
        return matched
    fallback_efficient = _find_preferred_match(("Hydrogen", "Flash"), available)
    return fallback_efficient if fallback_efficient is not None else available[0]


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
        self._active_usage: dict[str, str | None] = {
            "model": None,
            "stage": None,
            "updated_at": None,
        }
        self._active_activity: dict[str, Any] = {
            "activity_summary": None,
            "activity_detail": None,
            "agents_count": None,
            "step_started_at": None,
            "stage": None,
            "tests_passed": None,
            "tests_total": None,
            "build_round": None,
            "max_rounds": None,
        }

    def record_active_usage(
        self,
        model_name: str | None,
        role_or_stage: str,
    ) -> dict[str, str | None]:
        """Record active or most recently used model and stage cleanly under lock."""
        clean_model: str | None = None
        if model_name is not None:
            trimmed = str(model_name).strip()
            if trimmed:
                clean_model = redact_secrets(trimmed, extra_secrets=self.secrets)
        clean_stage = str(role_or_stage or "").strip().upper() or None
        now_iso = datetime.now(UTC).isoformat()
        usage: dict[str, str | None] = {
            "model": clean_model,
            "stage": clean_stage,
            "updated_at": now_iso,
        }
        with self._lock:
            self._active_usage = usage
            if self._active_activity["activity_summary"] is None and clean_stage:
                default_msg = ACTIVITY_MESSAGES.get(
                    clean_stage,
                    ACTIVITY_MESSAGES["BUILDING_CODE"] if clean_stage == "BUILDING" else None,
                )
                if default_msg is not None:
                    self._active_activity = {
                        "activity_summary": default_msg,
                        "agents_count": 2 if clean_stage == "DEBATE" else 1,
                        "step_started_at": now_iso,
                        "stage": clean_stage,
                    }
            return dict(self._active_usage)

    def get_active_usage(self) -> dict[str, str | None]:
        """Return a thread-safe snapshot of the last recorded active model usage."""
        with self._lock:
            return dict(self._active_usage)

    def record_activity(
        self,
        activity_summary: str | None,
        *,
        agents_count: int = 1,
        stage: str | None = None,
        started_at: str | None = None,
        detail: str | None = None,
        tests_passed: int | None = None,
        tests_total: int | None = None,
        build_round: int | None = None,
        max_rounds: int | None = None,
    ) -> dict[str, Any]:
        """Record active plain-Spanish sub-step summary, detail, counts, and start time."""
        clean_summary = (
            redact_secrets(str(activity_summary).strip(), extra_secrets=self.secrets)
            if activity_summary and str(activity_summary).strip()
            else None
        )
        clean_detail = (
            redact_secrets(str(detail).strip(), extra_secrets=self.secrets)
            if detail and str(detail).strip()
            else None
        )
        clean_stage = str(stage).strip().upper() if stage and str(stage).strip() else None
        count = max(1, int(agents_count))
        with self._lock:
            prev_summary = self._active_activity.get("activity_summary")
            prev_detail = self._active_activity.get("activity_detail")
            prev_started = self._active_activity.get("step_started_at")
            if started_at is not None:
                effective_started: str | None = started_at
            elif clean_summary is None:
                effective_started = None
            elif clean_summary != prev_summary or clean_detail != prev_detail or not prev_started:
                effective_started = datetime.now(UTC).isoformat()
            else:
                effective_started = str(prev_started)

            self._active_activity = {
                "activity_summary": clean_summary,
                "activity_detail": clean_detail,
                "agents_count": count if clean_summary is not None else None,
                "step_started_at": effective_started,
                "stage": clean_stage or self._active_usage.get("stage"),
                "tests_passed": int(tests_passed) if tests_passed is not None else None,
                "tests_total": int(tests_total) if tests_total is not None else None,
                "build_round": int(build_round) if build_round is not None else None,
                "max_rounds": int(max_rounds) if max_rounds is not None else None,
            }
            return dict(self._active_activity)

    def get_active_activity(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of active sub-step description, detail, counts, and start timestamp."""
        with self._lock:
            return dict(self._active_activity)

    def get_live_status(self) -> dict[str, Any]:
        """Return combined snapshot of active model usage and current sub-step activity."""
        with self._lock:
            combined: dict[str, Any] = dict(self._active_usage)
            combined.update(
                {
                    "activity_summary": self._active_activity.get("activity_summary"),
                    "activity_detail": self._active_activity.get("activity_detail"),
                    "agents_count": self._active_activity.get("agents_count"),
                    "step_started_at": self._active_activity.get("step_started_at"),
                    "tests_passed": self._active_activity.get("tests_passed"),
                    "tests_total": self._active_activity.get("tests_total"),
                    "build_round": self._active_activity.get("build_round"),
                    "max_rounds": self._active_activity.get("max_rounds"),
                }
            )
            if not combined.get("stage"):
                combined["stage"] = self._active_activity.get("stage")
            return combined

    def clear_active_activity(self) -> None:
        """Clear sub-step activity tracking when background execution completes."""
        with self._lock:
            self._active_activity = {
                "activity_summary": None,
                "activity_detail": None,
                "agents_count": None,
                "step_started_at": None,
                "stage": None,
                "tests_passed": None,
                "tests_total": None,
                "build_round": None,
                "max_rounds": None,
            }

    def _build_clean_env(self) -> dict[str, str]:
        if self._env_builder is not None:
            return self._env_builder()
        from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env

        return build_sanitized_cli_env(self.allowed_cli_env_keys)

    def _select_preferred_model(
        self,
        role_or_stage: str,
        available: Sequence[str],
    ) -> str:
        stage_upper = (role_or_stage or "").strip().upper()
        if stage_upper in self._custom_preferences:
            return _pick_preferred_model(self._custom_preferences[stage_upper], available)
        tier = "STRONG" if stage_upper in _STRONG_STAGES else "FAST"
        if tier in self._custom_preferences:
            return _pick_preferred_model(self._custom_preferences[tier], available)
        if tier == "STRONG":
            return _pick_strong_default_model(available)
        return _pick_fast_default_model(available)

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

            selected = self._select_preferred_model(role_or_stage, available)
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

            selected = self._select_preferred_model(role_or_stage, available)
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
