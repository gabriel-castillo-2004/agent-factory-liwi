"""Self-healing Doctor Agent with SQLite-backed error_lessons memory (INV-02, INV-18, INV-19, INV-22)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.github_vcs_adapter import VcsError, build_git_push_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.domain.models import validate_project_id
from agent_factory.domain.states import IllegalTransition
from agent_factory.ports.clock import Clock
from agent_factory.ports.code_agent import CodeAgentError
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.runtime_runner import LocalAppError
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import (
    SandboxViolationError,
    WorkspaceManager,
    verify_isolated_git_root,
)

if TYPE_CHECKING:
    from agent_factory.adapters.sqlite_store import SqliteEventStore

DEFAULT_MAX_RECOVERY_ATTEMPTS: int = 2
_MAX_INLINE_PROMPT_CHARS: int = 6000
_SAFE_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FACTORY_PATH_MARKER = "src/agent_factory/"
_FACTORY_BUG_TYPES: tuple[type[BaseException], ...] = (
    KeyError,
    AttributeError,
    TypeError,
    IndexError,
    NameError,
    ZeroDivisionError,
    UnboundLocalError,
    AssertionError,
    IllegalTransition,
    sqlite3.Error,
)
_WORKSPACE_EXTERNAL_ERROR_RE = re.compile(
    r"(?:Pull Request (?:has merge conflicts|is not mergeable)|"
    r"HTTP\s*40[345]|merge conflict|"
    r"No module named|"
    r"Git push failed|Merge failed|Smoke Test HTTP|pytest)",
    re.IGNORECASE,
)

_SEEDED_ERROR_LESSONS: tuple[dict[str, Any], ...] = (
    {
        "id": "lesson-pr-merge-conflicts",
        "error_pattern": r"(?:Pull Request (?:has merge conflicts|is not mergeable)|HTTP\s*405|merge conflicts?)",
        "stage": "*",
        "scope": "workspace",
        "root_cause": "La rama work del workspace tiene conflictos de mezcla respecto a origin/main al intentar fusionar el PR.",
        "resolution_type": "git_sync_main",
        "resolution_payload": json.dumps(
            {
                "branch": "work",
                "commands": [
                    ["git", "fetch", "origin"],
                    ["git", "merge", "-s", "ours", "origin/main", "--no-edit", "--allow-unrelated-histories"],
                    ["git", "push", "--force-with-lease", "-u", "origin", "work"],
                ],
            }
        ),
        "success_count": 1,
    },
    {
        "id": "lesson-module-not-found",
        "error_pattern": r"(?:ModuleNotFoundError:\s*)?No module named\s+['\"]?([^\s'\"]+)['\"]?",
        "stage": "*",
        "scope": "workspace",
        "root_cause": "Falta una dependencia o módulo Python en el entorno aislado del workspace.",
        "resolution_type": "pip_install",
        "resolution_payload": json.dumps(
            {
                "module_regex": r"No module named\s+['\"]?([^\s'\"]+)['\"]?",
                "module_map": {
                    "PIL": "Pillow",
                    "cv2": "opencv-python",
                    "yaml": "PyYAML",
                    "bs4": "beautifulsoup4",
                    "sklearn": "scikit-learn",
                    "dotenv": "python-dotenv",
                    "jose": "python-jose",
                    "jwt": "PyJWT",
                },
            }
        ),
        "success_count": 1,
    },
)


class _SystemClock:
    """Default wall clock returning UTC timestamps."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        _ = seconds


class ErrorLesson(BaseModel):
    """Persistent resolution playbook or learned fix stored in SQLite."""

    id: str
    error_pattern: str
    stage: str
    scope: Literal["workspace", "factory"]
    root_cause: str
    resolution_type: Literal["git_sync_main", "pip_install", "agy_workspace_fix", "factory_diagnostic"]
    resolution_payload: str
    success_count: int = 0
    updated_at: str


class RecoveryResult(BaseModel):
    """Structured outcome returned by RecoveryAgent.attempt_recovery (INV-19 safe)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    recovered: bool = False
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS
    scope: Literal["workspace", "factory"] = "workspace"
    used_lesson: bool = False
    lesson_id: str | None = None
    lesson_applied_type: str | None = None
    learned_new_lesson: bool = False
    diagnosis: str = ""
    suggested_prompt: str = ""
    last_error: str = ""
    recovered_value: Any = Field(default=None, exclude=True)

    def to_incident_dict(self, *, extra_secrets: Sequence[str] = ()) -> dict[str, Any]:
        """Serialize secret-redacted recovery metadata for events and dashboard views."""
        return {
            "recovered": bool(self.recovered),
            "attempts": int(self.attempts),
            "max_attempts": int(self.max_attempts),
            "scope": self.scope,
            "used_lesson": bool(self.used_lesson),
            "lesson_id": self.lesson_id,
            "lesson_applied_type": self.lesson_applied_type,
            "learned_new_lesson": bool(self.learned_new_lesson),
            "diagnosis": redact_secrets(self.diagnosis, extra_secrets=extra_secrets),
            "suggested_prompt": redact_secrets(self.suggested_prompt, extra_secrets=extra_secrets),
            "last_error": redact_secrets(self.last_error, extra_secrets=extra_secrets),
        }


def init_error_lessons_table(
    conn: sqlite3.Connection,
    lock: threading.RLock | None = None,
    *,
    now_iso: str | None = None,
) -> None:
    """Create error_lessons table if absent and idempotently seed proven base playbooks."""
    stamp = now_iso or datetime.now(UTC).isoformat()

    def _run() -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS error_lessons (
                    id TEXT PRIMARY KEY,
                    error_pattern TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    resolution_type TEXT NOT NULL,
                    resolution_payload TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for seed in _SEEDED_ERROR_LESSONS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO error_lessons (
                        id, error_pattern, stage, scope, root_cause,
                        resolution_type, resolution_payload, success_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        seed["id"],
                        seed["error_pattern"],
                        seed["stage"],
                        seed["scope"],
                        seed["root_cause"],
                        seed["resolution_type"],
                        seed["resolution_payload"],
                        seed["success_count"],
                        stamp,
                    ),
                )

    if lock is not None:
        with lock:
            _run()
    else:
        _run()


class ErrorLessonStore:
    """Thread-safe SQLite repository for Doctor Agent error_lessons memory."""

    def __init__(
        self,
        *,
        sqlite_store: SqliteEventStore | None = None,
        db_path: str | Path = ":memory:",
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock if clock is not None else _SystemClock()
        if sqlite_store is not None:
            self._owns_conn = False
            self._conn = sqlite_store._conn
            self._lock = sqlite_store._lock
        else:
            self._owns_conn = True
            target = str(db_path)
            if target != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self._conn = sqlite3.connect(target, check_same_thread=False)
        init_error_lessons_table(
            self._conn,
            self._lock,
            now_iso=self.clock.now().isoformat(),
        )

    @staticmethod
    def _row_to_lesson(row: tuple[Any, ...]) -> ErrorLesson:
        return ErrorLesson(
            id=str(row[0]),
            error_pattern=str(row[1]),
            stage=str(row[2]),
            scope=row[3],
            root_cause=str(row[4]),
            resolution_type=row[5],
            resolution_payload=str(row[6]),
            success_count=int(row[7]),
            updated_at=str(row[8]),
        )

    def list_lessons(self, *, scope: str | None = None) -> list[ErrorLesson]:
        """Return persisted lessons ordered by provenance and recency."""
        with self._lock:
            cur = self._conn.cursor()
            if scope is not None:
                cur.execute(
                    "SELECT id, error_pattern, stage, scope, root_cause, resolution_type, resolution_payload, success_count, updated_at "
                    "FROM error_lessons WHERE scope = ? ORDER BY success_count DESC, updated_at DESC",
                    (scope,),
                )
            else:
                cur.execute(
                    "SELECT id, error_pattern, stage, scope, root_cause, resolution_type, resolution_payload, success_count, updated_at "
                    "FROM error_lessons ORDER BY success_count DESC, updated_at DESC"
                )
            return [self._row_to_lesson(row) for row in cur.fetchall()]

    def get_lesson(self, lesson_id: str) -> ErrorLesson | None:
        """Load a single lesson by id, or None if not present."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, error_pattern, stage, scope, root_cause, resolution_type, resolution_payload, success_count, updated_at "
                "FROM error_lessons WHERE id = ?",
                (lesson_id,),
            ).fetchone()
        return self._row_to_lesson(row) if row is not None else None

    def find_matching_lesson(
        self,
        error_text: str,
        *,
        stage: str = "*",
        scope: str = "workspace",
        exclude_ids: Collection[str] = (),
    ) -> ErrorLesson | None:
        """Return highest-scoring proven lesson whose stage and regex pattern match error_text."""
        if not error_text:
            return None
        norm_stage = str(stage or "*").strip().upper()
        candidates = self.list_lessons(scope=scope)
        for lesson in candidates:
            if lesson.id in exclude_ids or lesson.success_count <= 0:
                continue
            lesson_stages = {s.strip().upper() for s in lesson.stage.split(",") if s.strip()}
            if "*" not in lesson_stages and norm_stage != "*" and norm_stage not in lesson_stages:
                continue
            try:
                if re.search(lesson.error_pattern, error_text, re.IGNORECASE):
                    return lesson
            except re.error:
                if lesson.error_pattern.lower() in error_text.lower():
                    return lesson
        return None

    def record_lesson(
        self,
        *,
        error_pattern: str,
        stage: str,
        scope: Literal["workspace", "factory"],
        root_cause: str,
        resolution_type: Literal["git_sync_main", "pip_install", "agy_workspace_fix", "factory_diagnostic"],
        resolution_payload: str,
        lesson_id: str | None = None,
    ) -> ErrorLesson:
        """Persist a newly learned lesson or reinforce an existing identical pattern."""
        now_iso = self.clock.now().isoformat()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id, success_count FROM error_lessons WHERE error_pattern = ? AND stage = ? AND scope = ? AND resolution_type = ?",
                (error_pattern, stage, scope, resolution_type),
            ).fetchone()
            if existing is not None:
                target_id = str(existing[0])
                new_count = int(existing[1]) + 1
                self._conn.execute(
                    "UPDATE error_lessons SET root_cause = ?, resolution_payload = ?, success_count = ?, updated_at = ? WHERE id = ?",
                    (root_cause, resolution_payload, new_count, now_iso, target_id),
                )
            else:
                target_id = lesson_id or f"lesson-{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    """
                    INSERT INTO error_lessons (
                        id, error_pattern, stage, scope, root_cause,
                        resolution_type, resolution_payload, success_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        target_id,
                        error_pattern,
                        stage,
                        scope,
                        root_cause,
                        resolution_type,
                        resolution_payload,
                        now_iso,
                    ),
                )
        saved = self.get_lesson(target_id)
        assert saved is not None
        return saved

    def increment_success(self, lesson_id: str) -> ErrorLesson | None:
        """Increment success_count and update timestamp for an applied lesson."""
        now_iso = self.clock.now().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE error_lessons SET success_count = success_count + 1, updated_at = ? WHERE id = ?",
                (now_iso, lesson_id),
            )
        return self.get_lesson(lesson_id)

    def close(self) -> None:
        """Close owned SQLite connection (leaves shared SqliteEventStore connection open)."""
        if self._owns_conn:
            with self._lock:
                self._conn.close()


def _extract_traceback_text(error: BaseException | str, explicit_tb: str | None = None) -> str:
    """Return explicit traceback or formatted traceback from BaseException."""
    if explicit_tb and explicit_tb.strip():
        return explicit_tb.strip()
    if isinstance(error, BaseException) and error.__traceback__ is not None:
        return "".join(traceback.format_exception(type(error), error, error.__traceback__)).strip()
    return ""


def _classify_error_scope(
    error: BaseException | str,
    traceback_text: str,
    *,
    error_text: str,
) -> Literal["workspace", "factory"]:
    """Determine whether error originates from factory internal code vs generated workspace."""
    tb_norm = traceback_text.replace("\\", "/")
    err_norm = error_text.replace("\\", "/")
    frames = [p.replace("\\", "/") for p in re.findall(r'File\s+"([^"]+)"', traceback_text)]
    has_factory_trace = _FACTORY_PATH_MARKER in tb_norm or _FACTORY_PATH_MARKER in err_norm
    if not has_factory_trace:
        return "workspace"

    if isinstance(error, _FACTORY_BUG_TYPES) or any(
        err_norm.lstrip().startswith(f"{t.__name__}:") for t in _FACTORY_BUG_TYPES
    ):
        return "factory"

    # External/workspace failures raised by adapters carry src/agent_factory/ call stacks but target workspace operations
    if isinstance(error, (VcsError, LocalAppError, CodeAgentError, CommandTimeoutError)):
        return "workspace"
    if _WORKSPACE_EXTERNAL_ERROR_RE.search(error_text):
        return "workspace"

    if frames and _FACTORY_PATH_MARKER in frames[-1]:
        return "factory"
    if _FACTORY_PATH_MARKER in tb_norm:
        return "factory"
    return "workspace"


def _derive_learned_pattern(error_text: str) -> str:
    """Derive a stable escaped regex matching the core diagnostic message across runs."""
    lines = [ln.strip() for ln in error_text.splitlines() if ln.strip()]
    core = lines[-1] if lines else error_text.strip()
    for ln in reversed(lines):
        if any(tok in ln for tok in ("Error", "Exception", "failed", "Failed", "fatal:", "ModuleNotFoundError")):
            core = ln
            break
    core = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", core)
    core = re.sub(r"\b[0-9a-fA-F]{12,}\b", "<ID>", core)
    core = core[:160].strip()
    escaped = re.escape(core)
    escaped = escaped.replace(re.escape("<HEX>"), r"0x[0-9a-fA-F]+").replace(
        re.escape("<ID>"), r"[0-9a-fA-F]+"
    )
    return escaped or re.escape("unknown workspace error")


def _extract_missing_package_name(error_text: str, payload_json: str) -> str | None:
    """Extract and strictly validate a Python package name from ModuleNotFoundError output."""
    try:
        cfg = json.loads(payload_json) if payload_json.strip() else {}
    except json.JSONDecodeError:
        cfg = {}
    regex = str(cfg.get("module_regex") or r"No module named\s+['\"]?([^\s'\"]+)['\"]?")
    match = re.search(regex, error_text, re.IGNORECASE)
    if not match or not match.groups():
        return None
    raw_mod = str(match.group(1) or "").strip()
    top_mod = raw_mod.split(".", 1)[0].strip()
    if not _SAFE_PACKAGE_NAME_RE.fullmatch(top_mod):
        return None
    mod_map = cfg.get("module_map") if isinstance(cfg, dict) else None
    if isinstance(mod_map, dict) and top_mod in mod_map:
        mapped = str(mod_map[top_mod]).strip()
        return mapped if _SAFE_PACKAGE_NAME_RE.fullmatch(mapped) else None
    return top_mod


def _parse_json_dict_loose(text: str) -> dict[str, Any] | None:
    """Extract first JSON object from stdout if CLI returned structured output."""
    clean = str(text or "").strip()
    if not clean:
        return None
    for candidate in (clean,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    fenced = re.search(r"```json\s*([\s\S]*?)```", clean, re.IGNORECASE)
    if fenced:
        try:
            obj = json.loads(fenced.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    start = clean.find("{")
    end = clean.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(clean[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


class RecoveryAgent:
    """Doctor Agent coordinating lesson matching, workspace repair (max 2 attempts), learning, and read-only factory diagnostics."""

    def __init__(
        self,
        *,
        lesson_store: ErrorLessonStore | None = None,
        sqlite_store: SqliteEventStore | None = None,
        db_path: str | Path = ":memory:",
        workspace_manager: WorkspaceManager | None = None,
        runner: Runner | None = None,
        clock: Clock | None = None,
        max_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS,
        agy_command: Sequence[str] = ("agy",),
        model: str | None = None,
        secrets_to_redact: Sequence[str] = (),
        timeout_seconds: float = 120.0,
        notifier: Any | None = None,
    ) -> None:
        self.clock: Clock = clock if clock is not None else _SystemClock()
        self.lesson_store: ErrorLessonStore = (
            lesson_store
            if lesson_store is not None
            else ErrorLessonStore(sqlite_store=sqlite_store, db_path=db_path, clock=self.clock)
        )
        self.workspace_manager = workspace_manager
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.max_attempts = max(1, min(DEFAULT_MAX_RECOVERY_ATTEMPTS, int(max_attempts)))
        self.agy_command: tuple[str, ...] = tuple(agy_command) if agy_command else ("agy",)
        self.model = model.strip() if isinstance(model, str) and model.strip() else None
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self.timeout_seconds = float(timeout_seconds)
        self.notifier = notifier

    def _notify_recovery_outcome(
        self,
        project_id: str,
        stage: str,
        result: RecoveryResult,
    ) -> None:
        """Send best-effort classified notification for recovery outcome when notifier is configured."""
        if self.notifier is None:
            return
        try:
            if result.recovered:
                self.notifier.notify_progress(
                    project_id,
                    state="RECOVERED",
                    summary=result.diagnosis or f"Recuperado automáticamente en {stage}.",
                )
            else:
                self.notifier.notify_needs_attention(
                    project_id,
                    cause=result.diagnosis or result.last_error or "Recuperación automática agotada.",
                    stage=stage,
                )
        except (OSError, RuntimeError, ValueError, TypeError):
            pass

    def _redact(self, text: str, extra_secrets: Sequence[str] = ()) -> str:
        combined = (*self.secrets_to_redact, *extra_secrets)
        return redact_secrets(text, extra_secrets=combined)

    def _resolve_contained_workspace(
        self,
        project_id: str,
        workspace_dir: Path | None,
    ) -> Path:
        validate_project_id(project_id)
        if workspace_dir is None:
            if self.workspace_manager is not None:
                workspace_dir = self.workspace_manager.get_workspace_dir(project_id)
            else:
                raise SandboxViolationError(f"Workspace directory required for recovery of '{project_id}'")
        ws_path = Path(workspace_dir)
        if ws_path.is_symlink():
            raise SandboxViolationError(f"Workspace directory cannot be a symlink: {ws_path} (INV-22)")
        ws_resolved = ws_path.resolve()
        if self.workspace_manager is not None and not self.workspace_manager.is_external_workspace(project_id):
            base = self.workspace_manager.base_dir.resolve()
            if not ws_resolved.is_relative_to(base) or ws_resolved == base:
                raise SandboxViolationError(
                    f"Workspace path '{ws_resolved}' escapes base '{base}' (INV-22)"
                )
        if not ws_resolved.is_dir():
            raise SandboxViolationError(f"Workspace directory '{ws_resolved}' does not exist")
        return ws_resolved

    def _resolve_agy_base_command(self) -> tuple[list[str] | None, bool, str]:
        cmd = list(self.agy_command)
        is_real_agy = bool(cmd) and Path(cmd[0]).stem.lower() == "agy"
        if cmd:
            resolved = resolve_executable(
                cmd[0],
                include_host_fallbacks=isinstance(self.runner, SubprocessRunner),
            )
            if (
                isinstance(self.runner, SubprocessRunner)
                and is_real_agy
                and (os.environ.get("AGY_CLI_DISABLED") == "1" or not resolved)
            ):
                return None, True, "CLI 'agy' no disponible o deshabilitado en este entorno."
            if resolved:
                cmd[0] = resolved
        return cmd, is_real_agy, ""

    def _run_git_sync_main(
        self,
        ws_resolved: Path,
        *,
        github_token: str,
        repo_base_url: str,
        extra_secrets: Sequence[str],
    ) -> None:
        verify_isolated_git_root(ws_resolved, self.runner)
        clean_env = build_sanitized_cli_env()
        push_env, push_secrets = build_git_push_env(
            clean_env,
            github_token,
            repo_base_url=repo_base_url,
        )
        all_sec = (*extra_secrets, *push_secrets)

        name_chk = self.runner.run(["git", "config", "--get", "user.name"], cwd=ws_resolved, env=clean_env)
        email_chk = self.runner.run(["git", "config", "--get", "user.email"], cwd=ws_resolved, env=clean_env)
        if name_chk.exit_code != 0 or not name_chk.stdout.strip():
            self.runner.run(["git", "config", "--local", "user.name", "AgentFactory"], cwd=ws_resolved, env=clean_env)
        if email_chk.exit_code != 0 or not email_chk.stdout.strip():
            self.runner.run(
                ["git", "config", "--local", "user.email", "agent@factory.local"],
                cwd=ws_resolved,
                env=clean_env,
            )

        br_res = self.runner.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=ws_resolved,
            env=clean_env,
        )
        if br_res.exit_code == 0 and br_res.stdout.strip() not in ("", "work"):
            chk_work = self.runner.run(["git", "checkout", "work"], cwd=ws_resolved, env=clean_env)
            if chk_work.exit_code != 0:
                detail = self._redact(chk_work.stderr or chk_work.stdout or "git checkout work failed", all_sec)
                raise RuntimeError(f"Error cambiando a rama work: {detail}")

        commands: tuple[tuple[list[str], str], ...] = (
            (["git", "fetch", "origin"], "Error en git fetch origin"),
            (
                ["git", "merge", "-s", "ours", "origin/main", "--no-edit", "--allow-unrelated-histories"],
                "Error en git merge origin/main",
            ),
            (["git", "push", "--force-with-lease", "-u", "origin", "work"], "Error en git push work"),
        )
        for cmd, ctx in commands:
            res = self.runner.run(
                cmd,
                cwd=ws_resolved,
                timeout_seconds=min(60.0, self.timeout_seconds),
                env=push_env,
            )
            if res.exit_code != 0:
                raw = (res.stderr or res.stdout or f"exit code {res.exit_code}").strip()
                raise RuntimeError(f"{ctx}: {self._redact(raw, all_sec)}")

    def _run_pip_install(
        self,
        lesson: ErrorLesson,
        *,
        error_text: str,
        ws_resolved: Path,
        extra_secrets: Sequence[str],
    ) -> str:
        pkg = _extract_missing_package_name(error_text, lesson.resolution_payload)
        if pkg is None:
            raise ValueError("No se pudo extraer un nombre de paquete válido y seguro para pip install")

        # Never try to PyPI-install a local module living inside the workspace itself
        if (
            (ws_resolved / f"{pkg}.py").is_file()
            or (ws_resolved / pkg).is_dir()
            or (ws_resolved / "src" / f"{pkg}.py").is_file()
            or (ws_resolved / "src" / pkg).is_dir()
        ):
            raise RuntimeError(f"El módulo '{pkg}' existe localmente en el workspace; requiere revisión de imports")

        py_exe = Path(sys.executable)
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
            cand = ws_resolved / rel
            if cand.exists() and not cand.is_symlink():
                resolved_cand = cand.resolve()
                if resolved_cand.is_relative_to(ws_resolved) and resolved_cand.is_file():
                    py_exe = resolved_cand
                    break

        dep_env = build_sanitized_cli_env()
        dep_env["PIP_NO_INPUT"] = "1"
        dep_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

        res = self.runner.run(
            [str(py_exe), "-m", "pip", "install", pkg],
            cwd=ws_resolved,
            timeout_seconds=self.timeout_seconds,
            env=dep_env,
        )
        if res.exit_code != 0:
            raw = (res.stderr or res.stdout or f"exit code {res.exit_code}").strip()
            raise RuntimeError(f"pip install {pkg} falló: {self._redact(raw, extra_secrets)}")
        return pkg

    def _run_agy_cli(
        self,
        *,
        prompt: str,
        cwd: Path,
        add_dir: Path | None,
        allow_writes: bool,
        mode_arg: str,
        extra_secrets: Sequence[str],
    ) -> tuple[bool, str]:
        base_cmd, is_real_agy, unavailable_reason = self._resolve_agy_base_command()
        if base_cmd is None:
            return False, unavailable_reason

        is_batch_shim = bool(base_cmd and base_cmd[0].lower().endswith((".cmd", ".bat")))
        clean_env = build_sanitized_cli_env()
        tmp_prompt_file: Path | None = None
        try:
            if len(prompt) <= _MAX_INLINE_PROMPT_CHARS:
                prompt_arg = re.sub(r"[\r\n]+", " ", prompt).strip() if is_batch_shim else prompt
            else:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".txt",
                    prefix="af_doc_prompt_",
                    delete=False,
                ) as tf:
                    tf.write(prompt)
                    tmp_prompt_file = Path(tf.name).resolve()
                prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt_file}"

            cmd = list(base_cmd) + ["-p", prompt_arg]
            if add_dir is not None:
                cmd.extend(["--add-dir", str(add_dir)])
            if allow_writes:
                cmd.append("--dangerously-skip-permissions")
                cmd.extend(["--mode", "auto" if is_real_agy else mode_arg])
            if self.model:
                cmd.extend(["--model", self.model])

            res = self.runner.run(
                cmd,
                cwd=cwd,
                timeout_seconds=self.timeout_seconds,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return False, self._redact(str(exc) or type(exc).__name__, extra_secrets)
        finally:
            if tmp_prompt_file is not None:
                tmp_prompt_file.unlink(missing_ok=True)

        combined = f"{res.stdout}\n{res.stderr}".strip()
        if res.exit_code != 0:
            return False, self._redact(combined or f"exit code {res.exit_code}", extra_secrets)
        return True, self._redact(res.stdout.strip() or "Reparación aplicada en workspace.", extra_secrets)

    def _run_agy_workspace_fix(
        self,
        ws_resolved: Path,
        *,
        stage: str,
        error_text: str,
        recent_logs: str,
        guidance: str = "",
        extra_secrets: Sequence[str],
    ) -> tuple[bool, str]:
        bounded_logs = self._redact(recent_logs[-2500:], extra_secrets) if recent_logs else "(sin logs previos)"
        hint_block = f"\nLección previa de referencia: {guidance}\n" if guidance else ""
        prompt = (
            f"Agente Doctor (reparación segura limitada al workspace).\n"
            f"Etapa: {stage}\n"
            f"Error detectado:\n{error_text[:1500]}\n"
            f"{hint_block}"
            f"Logs recientes:\n{bounded_logs}\n\n"
            "Instrucciones: Investiga y corrige únicamente los archivos dentro de este workspace para resolver el fallo. "
            'Devuelve al final un JSON con {"files_written": [...], "summary": "<causa raíz y cambio aplicado>"}.'
        )
        return self._run_agy_cli(
            prompt=prompt,
            cwd=ws_resolved,
            add_dir=ws_resolved,
            allow_writes=True,
            mode_arg="repair",
            extra_secrets=extra_secrets,
        )

    def _run_agy_factory_readonly_diagnosis(
        self,
        *,
        stage: str,
        error_text: str,
        traceback_text: str,
        extra_secrets: Sequence[str],
    ) -> tuple[str, str]:
        clean_tb = self._redact(traceback_text[-3500:] if traceback_text else error_text, extra_secrets)
        clean_err = self._redact(error_text[:1200], extra_secrets)
        ro_prompt = (
            "MODO SOLO LECTURA (PROHIBIDO editar o crear archivos).\n"
            f"Fallo interno en la fábrica (etapa {stage}):\n{clean_err}\n\n"
            f"Traceback:\n{clean_tb}\n\n"
            "Analiza la causa raíz sin tocar código de la fábrica y devuelve JSON:\n"
            '{"diagnosis": "<explicación clara de la causa raíz>", '
            '"suggested_prompt": "<prompt accionable y acotado para corregir src/agent_factory/ con TDD>"}'
        )

        with tempfile.TemporaryDirectory(prefix="af_doc_ro_") as tmp_dir:
            ok, out = self._run_agy_cli(
                prompt=ro_prompt,
                cwd=Path(tmp_dir),
                add_dir=None,
                allow_writes=False,
                mode_arg="readonly",
                extra_secrets=extra_secrets,
            )

        if ok and out:
            parsed = _parse_json_dict_loose(out)
            if parsed:
                diag = self._redact(str(parsed.get("diagnosis") or "").strip(), extra_secrets)
                sug = self._redact(str(parsed.get("suggested_prompt") or "").strip(), extra_secrets)
                if diag or sug:
                    return (
                        diag or f"Error interno en fábrica ({stage}): {clean_err}",
                        sug
                        or f"Corrige el fallo interno detectado en src/agent_factory/ ({clean_err}) siguiendo TDD y verifica con pytest.",
                    )
            return (
                self._redact(out[:1200], extra_secrets),
                f"Revisa el módulo señalado en src/agent_factory/ para resolver: {clean_err}. Escribe primero una prueba TDD que reproduzca el traceback y aplica la corrección mínima.",
            )

        frame_matches = re.findall(r'File\s+"([^"]*src[\\/]agent_factory[^"]*)",\s+line\s+(\d+)', clean_tb)
        where = (
            f"{frame_matches[-1][0]}:{frame_matches[-1][1]}"
            if frame_matches
            else "src/agent_factory/"
        )
        diagnosis = f"Error interno de la fábrica en {where} durante {stage}: {clean_err}"
        suggested_prompt = (
            f"Inspecciona {where} y añade una prueba TDD que reproduzca '{clean_err}'. "
            "Corrige el manejo del caso borde sin alterar tests existentes y ejecuta ruff check src tests y pytest -q --no-cov."
        )
        return self._redact(diagnosis, extra_secrets), self._redact(suggested_prompt, extra_secrets)

    def _apply_workspace_lesson(
        self,
        lesson: ErrorLesson,
        *,
        error_text: str,
        recent_logs: str,
        ws_resolved: Path,
        github_token: str,
        repo_base_url: str,
        extra_secrets: Sequence[str],
    ) -> str:
        if lesson.resolution_type == "git_sync_main":
            self._run_git_sync_main(
                ws_resolved,
                github_token=github_token,
                repo_base_url=repo_base_url,
                extra_secrets=extra_secrets,
            )
            return "Sincronización git_sync_main completada sobre la rama work."
        if lesson.resolution_type == "pip_install":
            pkg = self._run_pip_install(
                lesson,
                error_text=error_text,
                ws_resolved=ws_resolved,
                extra_secrets=extra_secrets,
            )
            return f"Dependencia '{pkg}' instalada en el entorno del workspace."
        if lesson.resolution_type == "agy_workspace_fix":
            guidance = lesson.root_cause
            try:
                pl = json.loads(lesson.resolution_payload)
                if isinstance(pl, dict) and pl.get("instructions"):
                    guidance = f"{lesson.root_cause} | Solución previa: {pl['instructions']}"
            except json.JSONDecodeError:
                pass
            ok, summary = self._run_agy_workspace_fix(
                ws_resolved,
                stage=lesson.stage,
                error_text=error_text,
                recent_logs=recent_logs,
                guidance=guidance,
                extra_secrets=extra_secrets,
            )
            if not ok:
                raise RuntimeError(summary or "Falló la aplicación de la lección con agy")
            return summary
        raise ValueError(f"Unsupported workspace resolution_type '{lesson.resolution_type}'")

    def diagnose_factory_error(
        self,
        *,
        stage: str,
        error: BaseException | str,
        traceback_text: str | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> RecoveryResult:
        """Analyze an internal factory traceback in strict read-only mode without modifying factory code."""
        raw_err = str(error).strip() or type(error).__name__
        clean_err = self._redact(raw_err, extra_secrets)
        tb_text = _extract_traceback_text(error, traceback_text)
        diag, sug = self._run_agy_factory_readonly_diagnosis(
            stage=stage,
            error_text=clean_err,
            traceback_text=tb_text,
            extra_secrets=extra_secrets,
        )
        return RecoveryResult(
            recovered=False,
            attempts=0,
            max_attempts=self.max_attempts,
            scope="factory",
            used_lesson=False,
            lesson_applied_type="factory_diagnostic",
            diagnosis=diag,
            suggested_prompt=sug,
            last_error=clean_err,
        )

    def attempt_recovery(
        self,
        *,
        project_id: str,
        stage: str,
        error: BaseException | str,
        workspace_dir: Path | None = None,
        retry_fn: Callable[[], Any] | None = None,
        recent_logs: str = "",
        traceback_text: str | None = None,
        github_token: str = "",
        repo_base_url: str = "https://github.com",
        extra_secrets: Sequence[str] = (),
    ) -> RecoveryResult:
        """Intercept workspace or factory failure prior to NEEDS_ATTENTION/FAILED with at most 2 recovery attempts."""
        result = self._attempt_recovery_inner(
            project_id=project_id,
            stage=stage,
            error=error,
            workspace_dir=workspace_dir,
            retry_fn=retry_fn,
            recent_logs=recent_logs,
            traceback_text=traceback_text,
            github_token=github_token,
            repo_base_url=repo_base_url,
            extra_secrets=extra_secrets,
        )
        self._notify_recovery_outcome(project_id, stage, result)
        return result

    def _attempt_recovery_inner(
        self,
        *,
        project_id: str,
        stage: str,
        error: BaseException | str,
        workspace_dir: Path | None = None,
        retry_fn: Callable[[], Any] | None = None,
        recent_logs: str = "",
        traceback_text: str | None = None,
        github_token: str = "",
        repo_base_url: str = "https://github.com",
        extra_secrets: Sequence[str] = (),
    ) -> RecoveryResult:
        """Core recovery workflow returning structured RecoveryResult."""
        all_secrets = (*self.secrets_to_redact, *extra_secrets)
        if github_token:
            all_secrets = (*all_secrets, github_token)

        raw_err = str(error).strip() or type(error).__name__
        clean_err = self._redact(raw_err, all_secrets)
        tb_text = _extract_traceback_text(error, traceback_text)

        scope = _classify_error_scope(error, tb_text, error_text=clean_err)
        if scope == "factory":
            return self.diagnose_factory_error(
                stage=stage,
                error=error,
                traceback_text=tb_text,
                extra_secrets=all_secrets,
            )

        try:
            ws_resolved = self._resolve_contained_workspace(project_id, workspace_dir)
        except SandboxViolationError as exc:
            err_msg = self._redact(str(exc), all_secrets)
            return RecoveryResult(
                recovered=False,
                attempts=0,
                max_attempts=self.max_attempts,
                scope="workspace",
                diagnosis=err_msg,
                last_error=clean_err,
            )

        attempts = 0
        tried_lesson_ids: set[str] = set()
        current_err: BaseException | str = error
        current_err_text = clean_err
        last_used_lesson = False
        last_lesson_id: str | None = None
        last_applied_type: str | None = None
        last_diagnosis = ""

        while attempts < self.max_attempts:
            matched = self.lesson_store.find_matching_lesson(
                current_err_text,
                stage=stage,
                scope="workspace",
                exclude_ids=tried_lesson_ids,
            )
            attempts += 1

            action_ok = False
            action_note = ""
            if matched is not None:
                tried_lesson_ids.add(matched.id)
                last_used_lesson = True
                last_lesson_id = matched.id
                last_applied_type = matched.resolution_type
                try:
                    action_note = self._apply_workspace_lesson(
                        matched,
                        error_text=current_err_text,
                        recent_logs=recent_logs,
                        ws_resolved=ws_resolved,
                        github_token=github_token,
                        repo_base_url=repo_base_url,
                        extra_secrets=all_secrets,
                    )
                    action_ok = True
                except ValueError as val_exc:
                    # Reject unsafe lesson inputs (e.g. invalid package tokens under INV-22) without falling back to agy
                    action_note = self._redact(str(val_exc) or "Entrada inválida para lección", all_secrets)
                    return RecoveryResult(
                        recovered=False,
                        attempts=attempts,
                        max_attempts=self.max_attempts,
                        scope="workspace",
                        used_lesson=True,
                        lesson_id=matched.id,
                        lesson_applied_type=matched.resolution_type,
                        diagnosis=action_note,
                        last_error=current_err_text,
                    )
                except (CommandTimeoutError, OSError, RuntimeError) as act_exc:
                    action_note = self._redact(str(act_exc) or type(act_exc).__name__, all_secrets)
                    last_diagnosis = action_note
            else:
                last_used_lesson = False
                last_lesson_id = None
                last_applied_type = "agy_workspace_fix"
                action_ok, action_note = self._run_agy_workspace_fix(
                    ws_resolved,
                    stage=stage,
                    error_text=current_err_text,
                    recent_logs=recent_logs,
                    extra_secrets=all_secrets,
                )
                last_diagnosis = action_note

            if not action_ok:
                continue

            if retry_fn is None:
                if matched is not None:
                    self.lesson_store.increment_success(matched.id)
                    return RecoveryResult(
                        recovered=True,
                        attempts=attempts,
                        max_attempts=self.max_attempts,
                        scope="workspace",
                        used_lesson=True,
                        lesson_id=matched.id,
                        lesson_applied_type=matched.resolution_type,
                        diagnosis=action_note,
                        last_error=current_err_text,
                    )
                learned = self.lesson_store.record_lesson(
                    error_pattern=_derive_learned_pattern(current_err_text),
                    stage=stage,
                    scope="workspace",
                    root_cause=action_note[:300] or f"Reparado automáticamente en {stage}",
                    resolution_type="agy_workspace_fix",
                    resolution_payload=json.dumps({"instructions": action_note[:1000]}),
                )
                return RecoveryResult(
                    recovered=True,
                    attempts=attempts,
                    max_attempts=self.max_attempts,
                    scope="workspace",
                    used_lesson=False,
                    lesson_id=learned.id,
                    lesson_applied_type="agy_workspace_fix",
                    learned_new_lesson=True,
                    diagnosis=action_note,
                    last_error=current_err_text,
                )

            try:
                recovered_val = retry_fn()
            except (CommandTimeoutError, OSError, RuntimeError, ValueError, KeyError, TypeError) as retry_exc:
                current_err = retry_exc
                current_err_text = self._redact(
                    str(retry_exc).strip() or type(retry_exc).__name__,
                    all_secrets,
                )
                retry_tb = _extract_traceback_text(retry_exc)
                if _classify_error_scope(retry_exc, retry_tb, error_text=current_err_text) == "factory":
                    diag_res = self.diagnose_factory_error(
                        stage=stage,
                        error=retry_exc,
                        traceback_text=retry_tb,
                        extra_secrets=all_secrets,
                    )
                    diag_res.attempts = attempts
                    return diag_res
                last_diagnosis = f"{action_note} -> Reintento falló: {current_err_text}"
                continue

            # Retry succeeded!
            if matched is not None:
                self.lesson_store.increment_success(matched.id)
                return RecoveryResult(
                    recovered=True,
                    attempts=attempts,
                    max_attempts=self.max_attempts,
                    scope="workspace",
                    used_lesson=True,
                    lesson_id=matched.id,
                    lesson_applied_type=matched.resolution_type,
                    learned_new_lesson=False,
                    diagnosis=action_note,
                    last_error=current_err_text,
                    recovered_value=recovered_val,
                )

            parsed_out = _parse_json_dict_loose(action_note)
            fix_summary = (
                str(parsed_out.get("summary") or "").strip()
                if parsed_out is not None
                else action_note
            ) or f"Reparado automáticamente por agy en {stage}"
            learned = self.lesson_store.record_lesson(
                error_pattern=_derive_learned_pattern(current_err_text),
                stage=stage,
                scope="workspace",
                root_cause=self._redact(fix_summary[:300], all_secrets),
                resolution_type="agy_workspace_fix",
                resolution_payload=json.dumps(
                    {
                        "instructions": self._redact(fix_summary[:1000], all_secrets),
                        "files_written": parsed_out.get("files_written", []) if parsed_out else [],
                    }
                ),
            )
            return RecoveryResult(
                recovered=True,
                attempts=attempts,
                max_attempts=self.max_attempts,
                scope="workspace",
                used_lesson=False,
                lesson_id=learned.id,
                lesson_applied_type="agy_workspace_fix",
                learned_new_lesson=True,
                diagnosis=self._redact(fix_summary, all_secrets),
                last_error=current_err_text,
                recovered_value=recovered_val,
            )

        _ = current_err
        return RecoveryResult(
            recovered=False,
            attempts=attempts,
            max_attempts=self.max_attempts,
            scope="workspace",
            used_lesson=last_used_lesson,
            lesson_id=last_lesson_id,
            lesson_applied_type=last_applied_type,
            learned_new_lesson=False,
            diagnosis=last_diagnosis or f"No se logró recuperar tras {attempts} intento(s).",
            last_error=current_err_text,
        )
