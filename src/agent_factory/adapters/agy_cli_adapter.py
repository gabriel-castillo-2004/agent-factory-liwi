"""Real CLI code agent adapter for local execution inside workspaces/<project_id>/ (INV-06, INV-09, INV-18, INV-19, ADR-002)."""

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from agent_factory.adapters.model_resolver import (
    ModelResolver,
    is_model_selection_error,
    is_quota_error,
)
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import AcceptanceTest, Plan
from agent_factory.ports.code_agent import (
    CodeAgent,
    CodeAgentError,
    CodeAgentResult,
    QuotaExhaustedError,
)
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import (
    SandboxViolationError,
    WorkspaceManager,
    is_python_cache_path,
)

logger = logging.getLogger(__name__)

ALLOWED_SYSTEM_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
    }
)

FORBIDDEN_SECRET_ENV_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "API_KEY",
    "PASSWORD",
    "CREDENTIAL",
    "PRIVATE_KEY",
)


def build_sanitized_cli_env(
    allowed_cli_env_keys: Sequence[str] = ("AGY_HOME", "AGY_CONFIG_DIR"),
    host_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct a sanitized environment keeping only whitelisted variables and stripping host secrets (INV-19)."""
    source = os.environ if host_env is None else host_env
    clean_env: dict[str, str] = {}
    allowed_sys_upper = {k.upper() for k in ALLOWED_SYSTEM_ENV_VARS}
    allowed_cli_upper = {k.upper() for k in allowed_cli_env_keys}

    for k, v in source.items():
        k_upper = k.upper()
        if any(sub in k_upper for sub in FORBIDDEN_SECRET_ENV_SUBSTRINGS):
            continue
        if k_upper in allowed_sys_upper:
            canonical_k = "PATH" if k_upper == "PATH" else k
            clean_env[canonical_k] = v
        elif k_upper in allowed_cli_upper or k in allowed_cli_env_keys:
            clean_env[k] = v
    return clean_env


_FENCED_BLOCK_RE = re.compile(r"```([^\r\n`]*)\r?\n(.*?)\r?\n?```", re.DOTALL)
_JSON_FENCE_OR_TAIL_RE = re.compile(r"```json\b[\s\S]*?(?:```|$)", re.IGNORECASE)
_EMBEDDED_JSON_SUMMARY_RE = re.compile(
    r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)', re.IGNORECASE
)
_RESIDUAL_AGENT_JSON_RE = re.compile(
    r'\{\s*"(?:files_written|summary)"\s*:[\s\S]*$', re.IGNORECASE
)
_ATTR_PATH_RE = re.compile(
    r"""(?:file|path|title)\s*=\s*(?:["'`]([^"'`]+)["'`]|(\S+))""",
    re.IGNORECASE,
)
_LANG_PREFIXES: frozenset[str] = frozenset(
    {
        "python",
        "py",
        "javascript",
        "js",
        "typescript",
        "ts",
        "bash",
        "sh",
        "json",
        "html",
        "css",
        "text",
        "txt",
        "plaintext",
    }
)
_MAX_CLI_PROMPT_CHARS = 12000
DEFAULT_AGY_CLI_TIMEOUT_SECONDS = 300.0


def _looks_like_file_path(token: str) -> bool:
    cleaned = token.strip().strip("`\"',:")
    if not cleaned:
        return False
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        return True
    if re.match(r"^[A-Za-z]:", cleaned):
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", cleaned))


def _extract_path_from_fence(info: str, body: str) -> str | None:
    stripped_info = info.strip()
    if stripped_info:
        attr_match = _ATTR_PATH_RE.search(stripped_info)
        if attr_match:
            val = attr_match.group(1) or attr_match.group(2) or ""
            return val.strip().strip("`\"',:")

        # Handle `python:tests/test_app.py` without breaking Windows `C:\...`
        if ":" in stripped_info and not re.match(r"^[A-Za-z]:", stripped_info):
            prefix, rest = stripped_info.split(":", 1)
            if prefix.strip().lower() in _LANG_PREFIXES and _looks_like_file_path(rest):
                return rest.strip().strip("`\"',:")

        parts = stripped_info.split(None, 1)
        if parts[0].strip().lower() in _LANG_PREFIXES:
            if len(parts) > 1 and _looks_like_file_path(parts[1]):
                return parts[1].strip().strip("`\"',:")
        elif _looks_like_file_path(stripped_info):
            return stripped_info.strip("`\"',:")

    first_lines = body.splitlines()[:1]
    if first_lines:
        line0 = first_lines[0].strip()
        for comment_prefix in ("# file:", "#file:", "// file:", "//file:", "#", "//"):
            if line0.lower().startswith(comment_prefix):
                candidate = line0[len(comment_prefix) :].strip().strip("`\"',:")
                if _looks_like_file_path(candidate) and (
                    "/" in candidate or "\\" in candidate or ".." in candidate or candidate.startswith("tests")
                ):
                    return candidate
                break
    return None


def _resolve_safe_test_path(ws_path: Path, raw_path: str) -> Path | None:
    """Validate candidate path stays strictly inside <ws_path>/tests/ (INV-22)."""
    cleaned = raw_path.strip().strip("`\"'")
    if not cleaned or "\x00" in cleaned:
        raise SandboxViolationError(f"Invalid extracted test file path: {raw_path!r}")

    norm = cleaned.replace("\\", "/").removeprefix("./")

    if re.match(r"^[A-Za-z]:", norm) or norm.startswith("/"):
        raise SandboxViolationError(
            f"Absolute path outside workspace tests/ rejected (INV-22): {raw_path!r}"
        )

    parts = PurePosixPath(norm).parts
    if any(part == ".." for part in parts):
        raise SandboxViolationError(
            f"Path traversal outside workspace tests/ rejected (INV-22): {raw_path!r}"
        )

    if not parts or parts[0] != "tests" or len(parts) < 2:
        # Non-traversal paths outside tests/ (e.g. src/ notes) are never written in generate_tests
        return None

    tests_root = (ws_path / "tests").resolve()
    target = (ws_path / PurePosixPath(*parts)).resolve()
    if not target.is_relative_to(tests_root) or target == tests_root:
        raise SandboxViolationError(
            f"Resolved path '{target}' escapes '{tests_root}' (INV-22)"
        )
    return target


def _extract_json_payload_dict(raw_text: str) -> tuple[dict[str, Any] | None, str]:
    """Extract a structured JSON dict (whole text, fenced ```json```, or embedded JSON object) and remaining prose."""
    text = raw_text.strip()
    if not text:
        return None, ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, ""
        return None, text
    except (json.JSONDecodeError, ValueError):
        pass

    matches = list(_FENCED_BLOCK_RE.finditer(text))
    for match in reversed(matches):
        info = match.group(1).strip().lower()
        if info and info != "json":
            continue
        try:
            candidate = json.loads(match.group(2).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict) and (
            "files_written" in candidate or "summary" in candidate
        ):
            prose = (text[: match.start()] + text[match.end() :]).strip()
            return candidate, prose

    decoder = json.JSONDecoder()
    for pos in reversed([idx for idx, ch in enumerate(text) if ch == "{"]):
        try:
            obj, end_offset = decoder.raw_decode(text[pos:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        is_known = "files_written" in obj or "summary" in obj
        at_tail = end_offset == len(text) - pos and (pos == 0 or text[pos - 1] in "\r\n")
        if is_known or at_tail:
            prose = (text[:pos] + text[pos + end_offset :]).strip()
            return obj, prose
    return None, text


def sanitize_agent_summary(raw_summary: object) -> str:
    """Strip fenced ```json``` blocks and residual agent JSON wrappers while preserving plain text."""
    if raw_summary is None:
        return ""
    text = str(raw_summary).strip()
    if not text:
        return ""

    data, prose = _extract_json_payload_dict(text)
    if isinstance(data, dict) and (
        "files_written" in data or "summary" in data or "debug" in data
    ):
        inner = data.get("summary")
        if isinstance(inner, str) and inner.strip():
            text = inner.strip()
        elif prose:
            text = prose
        else:
            text = ""

    extracted_partial = ""
    for fence_m in _JSON_FENCE_OR_TAIL_RE.finditer(text):
        m_sum = _EMBEDDED_JSON_SUMMARY_RE.search(fence_m.group(0))
        if m_sum and m_sum.group(1).strip():
            extracted_partial = m_sum.group(1).strip()
    cleaned = _JSON_FENCE_OR_TAIL_RE.sub("", text).strip()

    res_m = _RESIDUAL_AGENT_JSON_RE.search(cleaned)
    if res_m:
        if not extracted_partial:
            m_sum = _EMBEDDED_JSON_SUMMARY_RE.search(res_m.group(0))
            if m_sum and m_sum.group(1).strip():
                extracted_partial = m_sum.group(1).strip()
        cleaned = _RESIDUAL_AGENT_JSON_RE.sub("", cleaned).strip()

    if not cleaned and extracted_partial:
        cleaned = extracted_partial
    if "```" in cleaned:
        cleaned = cleaned.replace("```", "").strip()
    return cleaned


def _parse_cli_output(output_text: str) -> tuple[list[str], str]:
    """Parse files_written and clean human-readable summary from CLI stdout."""
    data, prose = _extract_json_payload_dict(output_text)
    if not isinstance(data, dict):
        return [], sanitize_agent_summary(output_text)

    raw_files = data.get("files_written", [])
    files_written = (
        [str(p).strip() for p in raw_files if isinstance(p, (str, Path)) and str(p).strip()]
        if isinstance(raw_files, list)
        else []
    )
    raw_summary = data.get("summary", "")
    if isinstance(raw_summary, str):
        summary = sanitize_agent_summary(raw_summary)
    elif prose:
        summary = sanitize_agent_summary(prose)
    else:
        summary = ""
    return files_written, summary


def _extract_and_write_test_files(ws_path: Path, output_text: str) -> list[str]:
    """Extract labeled test code blocks from CLI output and write safely under workspace/tests/."""
    if not output_text or not output_text.strip():
        return []

    texts_to_scan = [output_text]
    parsed, _ = _extract_json_payload_dict(output_text)
    if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
        texts_to_scan.append(parsed["summary"])

    written_paths: list[str] = []
    seen_targets: set[Path] = set()
    for text in texts_to_scan:
        for info, body in _FENCED_BLOCK_RE.findall(text):
            raw_path = _extract_path_from_fence(info, body)
            if raw_path is None:
                continue
            target = _resolve_safe_test_path(ws_path, raw_path)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = body if body.endswith("\n") else f"{body}\n"
            target.write_text(content, encoding="utf-8")
            if target not in seen_targets:
                seen_targets.add(target)
                written_paths.append(target.relative_to(ws_path).as_posix())
    return written_paths


def _list_workspace_test_files(ws_path: Path) -> set[str]:
    tests_dir = ws_path / "tests"
    if not tests_dir.is_dir():
        return set()
    return {
        p.relative_to(ws_path).as_posix()
        for p in tests_dir.rglob("*")
        if p.is_file() and not is_python_cache_path(p.relative_to(ws_path))
    }


def _merge_unique_paths(*groups: Sequence[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for grp in groups:
        for item in grp:
            norm = str(item).strip().replace("\\", "/")
            if norm and norm not in seen:
                seen.add(norm)
                merged.append(norm)
    return merged


class AgyCliAdapter(CodeAgent):
    """Executes the local CLI code agent inside an isolated workspace with a clean environment."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        runner: Runner | None = None,
        cli_command: Sequence[str] = ("agy",),
        timeout_seconds: float = DEFAULT_AGY_CLI_TIMEOUT_SECONDS,
        allowed_cli_env_keys: Sequence[str] = ("AGY_HOME", "AGY_CONFIG_DIR"),
        default_retry_after_seconds: float = 5.0,
        model: str | None = None,
        model_resolver: ModelResolver | None = None,
        secrets: Sequence[str] = (),
        retry_if_no_tests_written: bool | None = None,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.cli_command: tuple[str, ...] = tuple(cli_command)
        self.timeout_seconds = timeout_seconds
        self.allowed_cli_env_keys: frozenset[str] = frozenset(allowed_cli_env_keys)
        self.default_retry_after_seconds = default_retry_after_seconds
        self.model = model
        self.secrets: tuple[str, ...] = tuple(secrets)
        if retry_if_no_tests_written is None:
            self.retry_if_no_tests_written = self.runner.__class__.__name__ != "FakeRunner"
        else:
            self.retry_if_no_tests_written = retry_if_no_tests_written
        self.model_resolver: ModelResolver = (
            model_resolver
            if model_resolver is not None
            else ModelResolver(
                runner=self.runner,
                cli_command=self.cli_command,
                allowed_cli_env_keys=tuple(allowed_cli_env_keys),
                secrets=self.secrets,
            )
        )

    def validate_workspace_cwd(self, workspace_dir: Path) -> Path:
        """Verify workspace_dir is a valid isolated workspace directory under workspace_manager.base_dir (INV-18, INV-22)."""
        ws_path = Path(workspace_dir).resolve()
        base_dir = self.workspace_manager.base_dir.resolve()
        if not ws_path.is_relative_to(base_dir) or ws_path == base_dir:
            raise SandboxViolationError(
                f"Workspace cwd '{workspace_dir}' is not inside workspace base '{base_dir}'"
            )
        if ws_path.parent != base_dir:
            raise SandboxViolationError(
                f"Workspace cwd '{workspace_dir}' is a nested subdirectory under '{base_dir}'"
            )
        project_id = ws_path.name
        try:
            self.workspace_manager.get_workspace_dir(project_id)
        except (ValueError, SandboxViolationError) as err:
            raise SandboxViolationError(
                f"Invalid workspace directory name '{project_id}': {err}"
            ) from err
        return ws_path

    def build_clean_env(
        self,
        host_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Construct a sanitized environment dictionary keeping only whitelisted variables and stripping host secrets (INV-19)."""
        return build_sanitized_cli_env(self.allowed_cli_env_keys, host_env=host_env)

    def build_test_generation_payload(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> dict[str, Any]:
        """Build isolated payload for generate_tests containing only acceptance_tests without Plan or src/ (A2a-5, A2b-4)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        src_dir = ws_path / "src"
        if src_dir.is_dir() and any(p.is_file() for p in src_dir.rglob("*")):
            raise SandboxViolationError(
                f"Production files already exist in src/ before test generation: {ws_path}"
            )
        return {
            "mode": "generate_tests",
            "workspace_dir": str(ws_path),
            "acceptance_tests": [
                {
                    "id": at.id,
                    "description": at.description,
                    "type": at.type.value if hasattr(at.type, "value") else str(at.type),
                }
                for at in acceptance_tests
            ],
        }

    def build_implementation_payload(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> dict[str, Any]:
        """Build payload for implement_or_fix containing Plan, failure_output, and attempt number (A2b-4)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        return {
            "mode": "implement_or_fix",
            "workspace_dir": str(ws_path),
            "attempt": attempt,
            "failure_output": failure_output,
            "plan": plan.model_dump(mode="json"),
        }

    def _build_cli_command(
        self,
        ws_path: Path,
        prompt: str,
        mode_arg: str,
        active_model: str | None,
    ) -> list[str]:
        is_real_agy = Path(self.cli_command[0]).stem.lower() == "agy"
        cmd = list(self.cli_command) + [
            "-p",
            prompt,
            "--add-dir",
            str(ws_path),
            "--dangerously-skip-permissions",
        ]
        if active_model:
            cmd.extend(["--model", active_model])
        cmd.extend(["--mode", "auto" if is_real_agy else mode_arg])
        return cmd

    def _build_prompt(
        self,
        ws_path: Path,
        mode_arg: str,
        payload: dict[str, Any],
        *,
        extra_instruction: str = "",
    ) -> str:
        ws_str = str(ws_path)
        if mode_arg == "generate-tests":
            tests_list = payload.get("acceptance_tests") or []
            tests_lines = [
                f"- {t.get('id', 'AT')}: [{t.get('type', 'unit')}] {t.get('description', '')}"
                for t in tests_list
                if isinstance(t, dict)
            ]
            tests_block = "\n".join(tests_lines)
            prompt = (
                f"Workspace directory (cwd): {ws_str}\n"
                "Task: Generate real, executable acceptance tests inside the workspace directory.\n"
                "Requirements:\n"
                "1. Write test file(s) directly to the workspace filesystem under tests/ (for example tests/test_app.py).\n"
                "2. Do NOT read or create files under src/.\n"
                "3. Respond with a JSON object containing files_written (e.g. [\"tests/test_app.py\"]) and a short plain-text summary (no nested markdown or JSON).\n"
                "4. If direct file tool writes are unavailable, output each file inside a fenced code block labeled with its relative path (e.g. ```python tests/test_app.py).\n"
                "5. Self-contained execution: tests in tests/ MUST run using ONLY the Python standard library and pytest already installed in this environment "
                "(e.g. inspect static HTML/CSS/JS with pathlib, html.parser, re, ast, json). "
                "NEVER import external packages or browser automation runners such as playwright, selenium, or puppeteer unless already declared in the workspace project.\n"
                f"Acceptance tests (also passed via stdin JSON):\n{tests_block}"
            )
        else:
            plan = payload.get("plan") or {}
            plan_files = plan.get("files") or []
            file_paths = [
                str(f.get("path", "")).strip()
                for f in plan_files
                if isinstance(f, dict) and str(f.get("path", "")).strip()
            ]
            target_paths_str = ", ".join(file_paths) if file_paths else "(see plan files)"
            failure_tail = str(payload.get("failure_output") or "")[-4000:]
            contracts_lines = [
                f"- {f.get('path')}: {f.get('purpose', '')} | Contract: {f.get('contract', '')}"
                for f in plan_files
                if isinstance(f, dict) and f.get("path")
            ]
            contracts_block = "\n".join(contracts_lines)
            prompt = (
                f"Workspace directory (cwd): {ws_str}\n"
                f"Task: Implement or fix code directly on disk inside {ws_str} (attempt {payload.get('attempt', 1)}).\n"
                f"Target files to create/update: {target_paths_str}\n"
                "Requirements:\n"
                "1. Before changing code or concluding no changes are needed, READ the test files under tests/ (strictly read-only) together with the pytest failure output to align your implementation with the exact selectors, DOM attributes, exports, and assertions expected.\n"
                "2. Create and edit the target application files directly on the filesystem.\n"
                "3. NEVER create, modify, or delete tests/, pyproject.toml, setup.cfg, conftest.py, or package.json (INV-13).\n"
                "4. Respond with a JSON object containing files_written (only return [] after verifying every assertion in tests/ already holds) and a short plain-text summary (no nested markdown or JSON; full payload also passed via stdin).\n"
                f"Plan summary: {plan.get('summary', '')}\n"
                f"File specifications:\n{contracts_block}\n"
                + (f"Latest failure output:\n{failure_tail}" if failure_tail else "")
            )

        if extra_instruction:
            prompt = f"{prompt}\n\nIMPORTANT RETRY INSTRUCTION:\n{extra_instruction}"

        if len(prompt) > _MAX_CLI_PROMPT_CHARS:
            prompt = prompt[: _MAX_CLI_PROMPT_CHARS - 24] + "\n...[truncated; see stdin]"
        return prompt

    def _run_cli_with_output(
        self,
        ws_path: Path,
        mode_arg: str,
        payload: dict[str, Any],
        *,
        prompt: str | None = None,
    ) -> tuple[CodeAgentResult, str]:
        stage = "BUILDING"
        effective_prompt = (
            prompt
            if prompt is not None
            else self._build_prompt(ws_path, mode_arg, payload)
        )

        payload_str = json.dumps(payload, ensure_ascii=False)
        clean_env = self.build_clean_env()
        active_model = self.model_resolver.resolve_model_for_role(stage, self.model)
        tried_models: set[str] = set()
        max_attempts = 3
        res: CommandResult | None = None

        for attempt_idx in range(1, max_attempts + 1):
            self.model_resolver.record_active_usage(active_model, stage)
            logger.info(
                "AgyCliAdapter (%s) invoking stage %s with model %r (attempt %d/%d)",
                mode_arg,
                stage,
                redact_secrets(str(active_model or "<default>"), extra_secrets=self.secrets),
                attempt_idx,
                max_attempts,
            )
            cmd = self._build_cli_command(ws_path, effective_prompt, mode_arg, active_model)
            try:
                try:
                    res = self.runner.run(
                        command=cmd,
                        cwd=ws_path,
                        timeout_seconds=self.timeout_seconds,
                        env=clean_env,
                        input=payload_str,
                    )
                except TypeError as te:
                    if "input" in str(te):
                        res = self.runner.run(
                            command=cmd,
                            cwd=ws_path,
                            timeout_seconds=self.timeout_seconds,
                            env=clean_env,
                        )
                    else:
                        raise
            except CommandTimeoutError as exc:
                clean_timeout = redact_secrets(str(exc), extra_secrets=self.secrets)
                raise CommandTimeoutError(clean_timeout) from None

            combined_output = f"{res.stdout}\n{res.stderr}".strip()
            if res.exit_code == 0:
                break

            is_quota = is_quota_error(res.exit_code, combined_output)
            is_model_err = is_model_selection_error(combined_output)

            if (is_quota or is_model_err) and attempt_idx < max_attempts:
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
                        "AgyCliAdapter (%s) switching model from %r to fallback %r (attempt %d/%d)",
                        mode_arg,
                        redact_secrets(
                            str(active_model or "<default>"), extra_secrets=self.secrets
                        ),
                        redact_secrets(candidate, extra_secrets=self.secrets),
                        attempt_idx,
                        max_attempts,
                    )
                    active_model = candidate
                    continue

            if is_quota:
                retry_match = re.search(
                    r"retry_after[ =:]+([0-9.]+)", combined_output, re.IGNORECASE
                )
                retry_after = (
                    float(retry_match.group(1))
                    if retry_match
                    else self.default_retry_after_seconds
                )
                raise QuotaExhaustedError(
                    message=redact_secrets(combined_output, extra_secrets=self.secrets)
                    or "CLI quota exhausted",
                    retry_after_seconds=retry_after,
                )

            raise CodeAgentError(
                redact_secrets(
                    f"CLI execution failed with exit code {res.exit_code}: {combined_output}",
                    extra_secrets=self.secrets,
                )
            )

        assert res is not None

        # Parse success output using shared fenced/inline JSON & summary cleaner
        files_written: list[str] = []
        summary = ""
        output_text = res.stdout.strip()
        if output_text:
            files_written, summary = _parse_cli_output(output_text)

        resolved_model = (
            redact_secrets(active_model, extra_secrets=self.secrets)
            if active_model
            else None
        )
        result = CodeAgentResult(
            files_written=files_written,
            summary=summary,
            model=resolved_model,
        )
        return result, res.stdout

    def _run_cli(
        self,
        ws_path: Path,
        mode_arg: str,
        payload: dict[str, Any],
    ) -> CodeAgentResult:
        result, _ = self._run_cli_with_output(ws_path, mode_arg, payload)
        return result

    @staticmethod
    def _collect_or_extract_test_files(
        ws_path: Path,
        before_files: set[str],
        raw_stdout: str,
    ) -> tuple[list[str], list[str]]:
        new_files = sorted(_list_workspace_test_files(ws_path) - before_files)
        extracted: list[str] = []
        if not new_files:
            extracted = _extract_and_write_test_files(ws_path, raw_stdout)
            new_files = sorted(_list_workspace_test_files(ws_path) - before_files)
        return new_files, extracted

    def generate_tests(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> CodeAgentResult:
        """Execute CLI in generate-tests mode inside workspace_dir using only acceptance_tests (INV-13, A2b-4)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        payload = self.build_test_generation_payload(ws_path, acceptance_tests)
        before_files = _list_workspace_test_files(ws_path)

        result, raw_stdout = self._run_cli_with_output(ws_path, "generate-tests", payload)
        new_files, extracted = self._collect_or_extract_test_files(
            ws_path, before_files, raw_stdout
        )

        if not _list_workspace_test_files(ws_path) and self.retry_if_no_tests_written:
            logger.warning(
                "AgyCliAdapter (generate-tests) produced zero files in tests/; retrying explicitly"
            )
            retry_prompt = self._build_prompt(
                ws_path,
                "generate-tests",
                payload,
                extra_instruction=(
                    f"No files were created inside '{ws_path / 'tests'}' on the previous call. "
                    "You MUST write real executable test file(s) directly inside tests/ (e.g. tests/test_app.py) "
                    "now, or output fenced code blocks labeled ```python tests/test_app.py."
                ),
            )
            retry_payload = dict(payload)
            retry_payload["retry_reason"] = "zero_test_files_created_in_tests"
            result, retry_stdout = self._run_cli_with_output(
                ws_path,
                "generate-tests",
                retry_payload,
                prompt=retry_prompt,
            )
            new_files, extracted = self._collect_or_extract_test_files(
                ws_path, before_files, retry_stdout
            )

        merged_files = [
            p
            for p in _merge_unique_paths(result.files_written, new_files, extracted)
            if not is_python_cache_path(p)
        ]
        if merged_files != list(result.files_written) or new_files or extracted:
            result = result.model_copy(update={"files_written": merged_files})
        return result

    def implement_or_fix(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> CodeAgentResult:
        """Execute CLI in implement mode inside workspace_dir with Plan and failure report (A2b-4, A2b-5)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        payload = self.build_implementation_payload(
            ws_path, plan, failure_output=failure_output, attempt=attempt
        )
        return self._run_cli(ws_path, "implement", payload)
