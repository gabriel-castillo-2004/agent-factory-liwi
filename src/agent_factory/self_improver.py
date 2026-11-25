"""Self-improvement Parent Assistant (Liwi) with SQLite memory, safe apply gate, git rollback, and restart signaling (INV-03, INV-18, INV-19, INV-22)."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.github_vcs_adapter import build_git_push_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.config import build_workspace_pytest_env
from agent_factory.ports.clock import Clock
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.recovery_agent import _parse_json_dict_loose
from agent_factory.secrets import redact_secrets
from agent_factory.supervisor import RESTART_EXIT_CODE

if TYPE_CHECKING:
    from agent_factory.adapters.sqlite_store import SqliteEventStore

DEFAULT_SELF_IMPROVE_CONSULT_TIMEOUT_SECONDS: float = 180.0
DEFAULT_SELF_IMPROVE_TIMEOUT_SECONDS: float = 600.0
DEFAULT_RESTART_DELAY_SECONDS: float = 1.0
MAX_UPLOAD_IMAGE_BYTES: int = 5 * 1024 * 1024
MAX_UPLOAD_IMAGES_PER_MESSAGE: int = 4
_MAX_INLINE_PROMPT_CHARS: int = 6000
_SAFE_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_FORBIDDEN_SELF_IMPROVE_PREFIXES: tuple[str, ...] = (
    ".env",
    "MASTER_PROMPT.md",
    "AGENTS.md",
    "config/models.toml",
    "data",
    "workspaces",
)


class _WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def init_factory_self_improver_tables(
    conn: sqlite3.Connection,
    lock: threading.RLock | None = None,
) -> None:
    """Create factory_chat_messages and factory_memory_rules tables idempotently."""

    def _run() -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factory_chat_messages (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    image_paths_json TEXT NOT NULL,
                    proposal_title TEXT,
                    proposal_prompt TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factory_memory_rules (
                    id TEXT PRIMARY KEY,
                    rule_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    if lock is not None:
        with lock:
            _run()
    else:
        _run()


class FactoryMemoryStore:
    """Thread-safe SQLite repository for Liwi self-improvement chat history and preference rules."""

    def __init__(
        self,
        *,
        sqlite_store: SqliteEventStore | None = None,
        db_path: str | Path = ":memory:",
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock if clock is not None else _WallClock()
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
        init_factory_self_improver_tables(self._conn, self._lock)

    @staticmethod
    def _row_to_message(row: tuple[Any, ...]) -> dict[str, Any]:
        raw_paths = str(row[3] or "[]")
        try:
            paths = json.loads(raw_paths)
            if not isinstance(paths, list):
                paths = []
        except json.JSONDecodeError:
            paths = []
        filenames = [Path(str(p)).name for p in paths if str(p).strip()]
        return {
            "id": str(row[0]),
            "role": str(row[1]),
            "content": str(row[2]),
            "image_paths": [str(p) for p in paths],
            "image_urls": [f"/api/factory/assistant/uploads/{fn}" for fn in filenames],
            "proposal_title": str(row[4]) if row[4] is not None else None,
            "proposal_prompt": str(row[5]) if row[5] is not None else None,
            "status": str(row[6]),
            "created_at": str(row[7]),
        }

    def list_messages(self, *, limit: int = 80) -> list[dict[str, Any]]:
        """Return chat messages ordered oldest-first (capped to latest `limit`)."""
        cap = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, image_paths_json, proposal_title, proposal_prompt, status, created_at "
                "FROM factory_chat_messages ORDER BY rowid DESC LIMIT ?",
                (cap,),
            ).fetchall()
        rows.reverse()
        return [self._row_to_message(r) for r in rows]

    def add_message(
        self,
        *,
        role: str,
        content: str,
        image_paths: Sequence[str] = (),
        proposal_title: str | None = None,
        proposal_prompt: str | None = None,
        status: str = "ok",
    ) -> dict[str, Any]:
        """Insert and return a persisted factory assistant chat message."""
        msg_id = f"fmsg-{uuid.uuid4().hex[:12]}"
        created_at = self.clock.now().isoformat()
        paths_json = json.dumps([str(p) for p in image_paths])
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO factory_chat_messages (
                    id, role, content, image_paths_json, proposal_title,
                    proposal_prompt, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    role,
                    content,
                    paths_json,
                    proposal_title,
                    proposal_prompt,
                    status,
                    created_at,
                ),
            )
            row = self._conn.execute(
                "SELECT id, role, content, image_paths_json, proposal_title, proposal_prompt, status, created_at "
                "FROM factory_chat_messages WHERE id = ?",
                (msg_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_message(row)

    def update_message_status(self, message_id: str, status: str) -> None:
        """Update proposal status for message_id when present."""
        clean_id = str(message_id or "").strip()
        if not clean_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE factory_chat_messages SET status = ? WHERE id = ?",
                (status, clean_id),
            )

    def list_rules(self) -> list[dict[str, Any]]:
        """Return all active persistent memory rules ordered oldest-first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, rule_text, created_at FROM factory_memory_rules ORDER BY rowid ASC"
            ).fetchall()
        return [
            {"id": str(r[0]), "rule_text": str(r[1]), "created_at": str(r[2])}
            for r in rows
        ]

    def add_rule(self, rule_text: str) -> dict[str, Any] | None:
        """Persist a new preference/memory rule unless an identical rule already exists."""
        clean = " ".join(str(rule_text or "").split()).strip()
        if not clean:
            return None
        norm = clean.lower()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, rule_text, created_at FROM factory_memory_rules"
            ).fetchall()
            for r in existing:
                if " ".join(str(r[1]).split()).strip().lower() == norm:
                    return None
            rule_id = f"frule-{uuid.uuid4().hex[:12]}"
            created_at = self.clock.now().isoformat()
            with self._conn:
                self._conn.execute(
                    "INSERT INTO factory_memory_rules (id, rule_text, created_at) VALUES (?, ?, ?)",
                    (rule_id, clean[:500], created_at),
                )
        return {"id": rule_id, "rule_text": clean[:500], "created_at": created_at}

    def delete_rule(self, rule_id: str) -> bool:
        """Validate rule_id (INV-22) and delete rule from factory_memory_rules."""
        clean = str(rule_id or "").strip()
        if not _SAFE_RULE_ID_RE.fullmatch(clean):
            raise ValueError(f"Invalid memory rule id '{rule_id}'")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM factory_memory_rules WHERE id = ?",
                (clean,),
            )
            return cur.rowcount > 0


def _detect_image_ext_and_mime(data: bytes) -> tuple[str, str] | None:
    """Identify safe raster screenshot format strictly via magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _list_factory_source_files(repo_root: Path) -> list[str]:
    """Return sorted relative POSIX paths under src/agent_factory/ avoiding secrets/caches."""
    src_pkg = (repo_root / "src" / "agent_factory").resolve()
    if not src_pkg.is_dir():
        return []
    files: list[str] = []
    for item in sorted(src_pkg.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        if any(p in {"__pycache__", ".pytest_cache", ".git", ".venv"} for p in item.parts):
            continue
        if item.name.startswith(".env"):
            continue
        try:
            rel = item.resolve().relative_to(repo_root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        files.append(rel)
        if len(files) >= 200:
            break
    return files


class FactorySelfImprover:
    """Orchestrates Liwi parent consult chat, SQLite memory, safe apply+test gate, rollback, and exit 42 restart."""

    def __init__(
        self,
        *,
        sqlite_store: SqliteEventStore | None = None,
        db_path: str | Path = ":memory:",
        repo_root: Path | None = None,
        uploads_dir: Path | None = None,
        runner: Runner | None = None,
        clock: Clock | None = None,
        agy_command: Sequence[str] = ("agy",),
        model: str | None = None,
        secrets_to_redact: Sequence[str] = (),
        consult_timeout_seconds: float = DEFAULT_SELF_IMPROVE_CONSULT_TIMEOUT_SECONDS,
        apply_timeout_seconds: float = DEFAULT_SELF_IMPROVE_TIMEOUT_SECONDS,
        restart_fn: Callable[[int], Any] | None = None,
        restart_delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS,
        pre_exit_hook: Callable[[], Any] | None = None,
    ) -> None:
        self.clock: Clock = clock if clock is not None else _WallClock()
        self.memory_store = FactoryMemoryStore(
            sqlite_store=sqlite_store,
            db_path=db_path,
            clock=self.clock,
        )
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root is not None
            else Path(__file__).resolve().parents[2]
        )
        if uploads_dir is not None:
            self.uploads_dir = Path(uploads_dir).resolve()
        else:
            raw_db = getattr(sqlite_store, "db_path", db_path) if sqlite_store is not None else db_path
            if raw_db and str(raw_db) != ":memory:":
                db_parent = Path(str(raw_db)).resolve().parent
                repo_data_dir = Path("data").resolve()
                self.uploads_dir = (
                    (repo_data_dir / "factory_chat_uploads").resolve()
                    if db_parent == repo_data_dir
                    else (db_parent / "factory_chat_uploads").resolve()
                )
            else:
                self.uploads_dir = Path("data/factory_chat_uploads").resolve()
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=consult_timeout_seconds)
        )
        self.agy_command: tuple[str, ...] = tuple(agy_command) if agy_command else ("agy",)
        self.model = model.strip() if isinstance(model, str) and model.strip() else None
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self.consult_timeout_seconds = float(consult_timeout_seconds)
        self.apply_timeout_seconds = float(apply_timeout_seconds)
        self.restart_fn: Callable[[int], Any] = restart_fn if restart_fn is not None else os._exit
        self.restart_delay_seconds = float(restart_delay_seconds)
        self.pre_exit_hook = pre_exit_hook
        self._apply_lock = threading.Lock()

    def _redact(self, text: str, extra_secrets: Sequence[str] = ()) -> str:
        return redact_secrets(text, extra_secrets=(*self.secrets_to_redact, *extra_secrets))

    def _save_uploaded_images(self, images: Sequence[Mapping[str, Any]]) -> list[str]:
        if not images:
            return []
        if len(images) > MAX_UPLOAD_IMAGES_PER_MESSAGE:
            raise ValueError(f"Máximo {MAX_UPLOAD_IMAGES_PER_MESSAGE} capturas por mensaje.")
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        base_resolved = self.uploads_dir.resolve()
        saved_paths: list[str] = []

        for idx, item in enumerate(images, start=1):
            if not isinstance(item, Mapping):
                raise TypeError("Cada elemento de 'images' debe ser un objeto.")
            raw_b64 = item.get("data_base64")
            if not isinstance(raw_b64, str) or not raw_b64.strip():
                raise ValueError("Cada captura adjunta requiere 'data_base64'.")
            b64_clean = raw_b64.strip()
            if "base64," in b64_clean[:64]:
                b64_clean = b64_clean.split("base64,", 1)[1].strip()
            try:
                raw_bytes = base64.b64decode(b64_clean, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("Codificación base64 inválida en la captura adjunta.") from exc
            if not raw_bytes:
                raise ValueError("La captura adjunta está vacía.")
            if len(raw_bytes) > MAX_UPLOAD_IMAGE_BYTES:
                raise ValueError("Cada captura debe pesar máximo 5 MB.")
            fmt = _detect_image_ext_and_mime(raw_bytes)
            if fmt is None:
                raise ValueError("Formato de captura no permitido (usa PNG, JPG, GIF o WebP).")
            ext, _mime = fmt
            raw_name = str(item.get("filename") or f"clip_{idx}")
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(raw_name).stem).strip("_-")[:32] or "clip"
            safe_filename = f"{stem}_{uuid.uuid4().hex[:10]}{ext}"
            target = (base_resolved / safe_filename).resolve()
            if not target.is_relative_to(base_resolved) or target.parent != base_resolved:
                raise ValueError("Ruta inválida para captura de pantalla (INV-22).")
            target.write_bytes(raw_bytes)
            saved_paths.append(str(target))
        return saved_paths

    def get_uploaded_image(self, filename: str) -> tuple[str, bytes]:
        """Safely read an uploaded screenshot strictly contained inside self.uploads_dir (INV-22)."""
        clean = str(filename or "").strip()
        if not _SAFE_UPLOAD_NAME_RE.fullmatch(clean):
            raise ValueError(f"Invalid upload filename '{filename}'")
        base_resolved = self.uploads_dir.resolve()
        candidate = base_resolved / clean
        if candidate.is_symlink():
            raise ValueError("Upload symlink rejected (INV-22)")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base_resolved) or not resolved.is_file():
            raise FileNotFoundError(clean)
        data = resolved.read_bytes()
        fmt = _detect_image_ext_and_mime(data)
        return (fmt[1] if fmt else "application/octet-stream"), data

    def get_history(self) -> dict[str, Any]:
        """Return persisted chat history and memory rules with secrets scrubbed."""
        messages = self.memory_store.list_messages()
        rules = self.memory_store.list_rules()
        return {
            "messages": messages,
            "memory_rules": rules,
            "memory_count": len(rules),
        }

    def delete_memory_rule(self, rule_id: str) -> dict[str, Any]:
        """Delete a rule from factory_memory_rules or raise KeyError if missing."""
        deleted = self.memory_store.delete_rule(rule_id)
        if not deleted:
            raise KeyError(rule_id)
        rules = self.memory_store.list_rules()
        return {
            "ok": True,
            "deleted": True,
            "rule_id": rule_id,
            "memory_rules": rules,
            "memory_count": len(rules),
        }

    def _resolve_agy_cmd(self) -> tuple[list[str] | None, bool, str]:
        cmd = list(self.agy_command)
        is_real_agy = bool(cmd) and Path(cmd[0]).stem.lower() == "agy"
        is_exact_subprocess = type(self.runner) is SubprocessRunner
        if cmd:
            resolved = resolve_executable(
                cmd[0],
                include_host_fallbacks=is_exact_subprocess,
            )
            if (
                is_exact_subprocess
                and is_real_agy
                and (os.environ.get("AGY_CLI_DISABLED") == "1" or not resolved)
            ):
                return None, True, "CLI 'agy' no disponible en este entorno."
            if resolved and is_exact_subprocess:
                cmd[0] = resolved
        return cmd, is_real_agy, ""

    def _run_agy(
        self,
        *,
        prompt: str,
        cwd: Path,
        allow_writes: bool,
        timeout_seconds: float,
        extra_add_dirs: Sequence[Path] = (),
        extra_secrets: Sequence[str] = (),
    ) -> tuple[bool, str]:
        base_cmd, is_real_agy, err_reason = self._resolve_agy_cmd()
        if base_cmd is None:
            return False, err_reason
        is_batch = bool(base_cmd and base_cmd[0].lower().endswith((".cmd", ".bat")))
        clean_env = build_sanitized_cli_env()
        tmp_prompt: Path | None = None
        try:
            if len(prompt) <= _MAX_INLINE_PROMPT_CHARS:
                prompt_arg = re.sub(r"[\r\n]+", " ", prompt).strip() if is_batch else prompt
            else:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".txt",
                    prefix="af_liwi_",
                    delete=False,
                ) as tf:
                    tf.write(prompt)
                    tmp_prompt = Path(tf.name).resolve()
                prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

            cmd = list(base_cmd) + ["-p", prompt_arg]
            for d in extra_add_dirs:
                cmd.extend(["--add-dir", str(d)])
            if allow_writes:
                cmd.append("--dangerously-skip-permissions")
                if is_real_agy:
                    cmd.extend(["--mode", "auto"])
            if self.model:
                cmd.extend(["--model", self.model])

            res = self.runner.run(
                cmd,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return False, self._redact(str(exc) or type(exc).__name__, extra_secrets)
        finally:
            if tmp_prompt is not None:
                tmp_prompt.unlink(missing_ok=True)

        if res.exit_code != 0:
            raw = (res.stderr or res.stdout or f"exit code {res.exit_code}").strip()
            return False, self._redact(raw, extra_secrets)
        return True, self._redact(res.stdout.strip(), extra_secrets)

    def consult_message(
        self,
        message: str,
        *,
        images: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Consult Liwi assistant in strict read-only mode, persist turns/rules, and extract proposal."""
        raw_msg = str(message or "").strip()
        if not raw_msg and not images:
            raise ValueError("El mensaje o una captura adjunta es obligatorio.")
        eff_msg = raw_msg or "Analiza la captura adjunta y sugiere mejoras."
        saved_images = self._save_uploaded_images(images)

        user_rec = self.memory_store.add_message(
            role="user",
            content=self._redact(eff_msg),
            image_paths=saved_images,
            status="ok",
        )

        src_files = _list_factory_source_files(self.repo_root)
        rules = self.memory_store.list_rules()
        history = self.memory_store.list_messages(limit=14)[:-1]

        rules_block = "\n".join(f"- [{r['id']}] {r['rule_text']}" for r in rules) or "(ninguna)"
        hist_block = (
            "\n".join(f"{m['role']}: {m['content'][:500]}" for m in history[-8:])
            or "(inicio de conversación)"
        )
        files_block = "\n".join(src_files[:120]) or "(sin archivos)"
        img_block = "\n".join(saved_images) if saved_images else "(sin imágenes adjuntas)"

        consult_prompt = (
            "MODO CONSULTA DE SOLO LECTURA (PROHIBIDO editar archivos o ejecutar comandos mutantes).\n"
            "Eres Liwi, el asistente de auto-mejora de Agent Factory. Responde siempre en español claro.\n\n"
            f"Archivos en src/agent_factory/:\n{files_block}\n\n"
            f"Reglas persistentes en Memoria de Liwi:\n{rules_block}\n\n"
            f"Historial reciente:\n{hist_block}\n\n"
            f"Imágenes adjuntas (rutas locales):\n{img_block}\n\n"
            f"Solicitud del usuario:\n{self._redact(eff_msg)}\n\n"
            "Devuelve ÚNICAMENTE un JSON válido con esta estructura exacta:\n"
            '{"reply": "<respuesta en español claro>", '
            '"new_memory_rules": ["<regla o preferencia detectada, si aplica>"], '
            '"actionable_proposal": {"title": "<título corto>", "prompt": "<instrucciones técnicas detalladas TDD>"} | null}'
        )

        with tempfile.TemporaryDirectory(prefix="af_liwi_consult_") as ro_cwd:
            ok, out = self._run_agy(
                prompt=consult_prompt,
                cwd=Path(ro_cwd),
                allow_writes=False,
                timeout_seconds=self.consult_timeout_seconds,
            )

        reply = ""
        added_rules: list[dict[str, Any]] = []
        proposal_title: str | None = None
        proposal_prompt: str | None = None
        if ok and out:
            parsed = _parse_json_dict_loose(out)
            if parsed is not None:
                reply = self._redact(str(parsed.get("reply") or "").strip())
                raw_rules = parsed.get("new_memory_rules")
                if isinstance(raw_rules, list):
                    for item in raw_rules[:5]:
                        cleaned_rule = self._redact(str(item or "").strip())
                        saved_r = self.memory_store.add_rule(cleaned_rule)
                        if saved_r is not None:
                            added_rules.append(saved_r)
                raw_prop = parsed.get("actionable_proposal")
                if isinstance(raw_prop, Mapping):
                    p_title = self._redact(str(raw_prop.get("title") or "").strip())
                    p_prompt = self._redact(str(raw_prop.get("prompt") or "").strip())
                    if p_title and p_prompt:
                        proposal_title = p_title[:160]
                        proposal_prompt = p_prompt
            if not reply:
                reply = self._redact(out[:3000])
        else:
            detail = out or "No se pudo consultar a agy."
            reply = self._redact(
                f"No pude obtener respuesta del agente en modo lectura ({detail}). "
                "Puedes revisar la configuración de agy e intentarlo nuevamente."
            )

        assistant_rec = self.memory_store.add_message(
            role="assistant",
            content=reply,
            proposal_title=proposal_title,
            proposal_prompt=proposal_prompt,
            status="proposal" if proposal_prompt else "ok",
        )
        all_rules = self.memory_store.list_rules()
        return {
            "ok": True,
            "user_message": user_rec,
            "assistant_message": assistant_rec,
            "reply": reply,
            "new_memory_rules": [r["rule_text"] for r in added_rules],
            "actionable_proposal": (
                {"title": proposal_title, "prompt": proposal_prompt}
                if proposal_title and proposal_prompt
                else None
            ),
            "memory_rules": all_rules,
            "memory_count": len(all_rules),
        }

    def _snapshot_forbidden_state(self) -> dict[str, Any]:
        """Record mtime/size and git status for forbidden paths without reading .env contents (INV-19)."""
        stats: dict[str, tuple[int, int] | None] = {}
        for rel in _FORBIDDEN_SELF_IMPROVE_PREFIXES:
            p = self.repo_root / rel
            if p.is_file():
                try:
                    st = p.stat()
                    stats[rel] = (int(st.st_mtime_ns), int(st.st_size))
                except OSError:
                    stats[rel] = None
        porcelain = self._git_porcelain_forbidden()
        return {"stats": stats, "porcelain": porcelain}

    def _git_porcelain_forbidden(self) -> str:
        clean_env = build_sanitized_cli_env()
        try:
            res = self.runner.run(
                ["git", "status", "--porcelain", "--", *_FORBIDDEN_SELF_IMPROVE_PREFIXES],
                cwd=self.repo_root,
                env=clean_env,
            )
            return res.stdout if res.exit_code == 0 else ""
        except (CommandTimeoutError, OSError, RuntimeError, ValueError):
            return ""

    def _find_forbidden_modifications(self, baseline: dict[str, Any]) -> list[str]:
        changed: list[str] = []
        base_stats = baseline.get("stats", {})
        for rel in _FORBIDDEN_SELF_IMPROVE_PREFIXES:
            p = self.repo_root / rel
            if p.is_file():
                try:
                    st = p.stat()
                    cur = (int(st.st_mtime_ns), int(st.st_size))
                except OSError:
                    cur = None
                if (rel in base_stats and cur != base_stats[rel]) or (
                    rel not in base_stats and rel == ".env"
                ):
                    changed.append(rel)
        if self._git_porcelain_forbidden() != baseline.get("porcelain", ""):
            changed.append("forbidden_git_tree")
        return changed

    def _resolve_venv_tool(self, tool_name: str) -> list[str]:
        if isinstance(self.runner, SubprocessRunner):
            ext = ".exe" if os.name == "nt" else ""
            for base_dir in (
                self.repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
                Path(sys.executable).resolve().parent,
            ):
                cand = base_dir / f"{tool_name}{ext}"
                if cand.is_file():
                    return [str(cand)]
            resolved = resolve_executable(tool_name)
            if resolved:
                return [resolved]
            return [sys.executable, "-m", tool_name]
        return [tool_name]

    def _rollback_savepoint(self, env: Mapping[str, str]) -> None:
        self.runner.run(
            ["git", "reset", "--hard", "pre-self-improve"],
            cwd=self.repo_root,
            timeout_seconds=30.0,
            env=env,
        )
        self.runner.run(
            ["git", "clean", "-fd", "src", "tests"],
            cwd=self.repo_root,
            timeout_seconds=30.0,
            env=env,
        )

    def _schedule_process_restart(self) -> None:
        def _do_restart() -> None:
            self.clock.sleep(self.restart_delay_seconds)
            if self.pre_exit_hook is not None:
                try:
                    self.pre_exit_hook()
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger(__name__).debug("pre_exit_hook failed: %s", exc)
            self.restart_fn(RESTART_EXIT_CODE)

        threading.Thread(
            target=_do_restart,
            name="liwi-self-improve-restart",
            daemon=True,
        ).start()

    def apply_proposal(
        self,
        proposal_prompt: str,
        *,
        proposal_title: str = "",
        message_id: str | None = None,
        github_token: str = "",
    ) -> dict[str, Any]:
        """Tag HEAD, run agy (600s), gate with ruff + pytest (600s), rollback on failure, commit+push+exit 42 on pass."""
        clean_prompt = str(proposal_prompt or "").strip()
        if not clean_prompt:
            raise ValueError("El campo 'proposal_prompt' es obligatorio.")
        clean_title = self._redact(str(proposal_title or "Auto-mejora Liwi").strip())[:100]

        if not self._apply_lock.acquire(blocking=False):
            raise RuntimeError("Ya hay una auto-mejora en ejecución.")

        clean_env = build_sanitized_cli_env()
        try:
            # 1. Create Git savepoint tag
            tag_res = self.runner.run(
                ["git", "tag", "-f", "pre-self-improve", "HEAD"],
                cwd=self.repo_root,
                timeout_seconds=20.0,
                env=clean_env,
            )
            if tag_res.exit_code != 0:
                detail = self._redact(tag_res.stderr or tag_res.stdout or "git tag failed")
                raise RuntimeError(f"No se pudo crear el punto de guardado Git: {detail}")

            baseline = self._snapshot_forbidden_state()

            # 2. Execute agy inside factory repo with explicit forbidden-file guardrails
            apply_prompt = (
                "Implementa la siguiente mejora en Agent Factory usando TDD.\n"
                "REGLA ESTRICTA DE SEGURIDAD: PROHIBIDO leer o modificar .env, MASTER_PROMPT.md, "
                "AGENTS.md, config/models.toml, data/ o workspaces/.\n\n"
                f"Propuesta ({clean_title}):\n{clean_prompt}"
            )
            agy_ok, agy_out = self._run_agy(
                prompt=apply_prompt,
                cwd=self.repo_root,
                allow_writes=True,
                timeout_seconds=self.apply_timeout_seconds,
                extra_add_dirs=(self.repo_root,),
            )

            forbidden_touched = self._find_forbidden_modifications(baseline)
            failure_reason = ""
            if forbidden_touched:
                failure_reason = (
                    f"Intento bloqueado: se tocaron rutas protegidas ({', '.join(forbidden_touched)})."
                )
            elif not agy_ok:
                failure_reason = f"Falló la ejecución de agy: {agy_out}"
            else:
                # 3. Quality gate: ruff check src tests + pytest -q --no-cov
                ws_env = build_workspace_pytest_env()
                ruff_cmd = [*self._resolve_venv_tool("ruff"), "check", "src", "tests"]
                ruff_res = self.runner.run(
                    ruff_cmd,
                    cwd=self.repo_root,
                    timeout_seconds=self.apply_timeout_seconds,
                    env=ws_env,
                )
                if ruff_res.exit_code != 0:
                    tail = (ruff_res.stdout or ruff_res.stderr or "ruff check failed").strip()[-1200:]
                    failure_reason = f"Falló ruff check src tests:\n{self._redact(tail)}"
                else:
                    pytest_cmd = [*self._resolve_venv_tool("pytest"), "-q", "--no-cov"]
                    pytest_res = self.runner.run(
                        pytest_cmd,
                        cwd=self.repo_root,
                        timeout_seconds=self.apply_timeout_seconds,
                        env=ws_env,
                    )
                    if pytest_res.exit_code != 0:
                        tail = (pytest_res.stdout or pytest_res.stderr or "pytest failed").strip()[-1200:]
                        failure_reason = f"Falló pytest -q --no-cov:\n{self._redact(tail)}"

            if failure_reason:
                self._rollback_savepoint(clean_env)
                if message_id:
                    self.memory_store.update_message_status(message_id, "rolled_back")
                err_msg = self.memory_store.add_message(
                    role="assistant",
                    content=self._redact(
                        f"Auto-mejora revertida automáticamente (`pre-self-improve`).\n\n{failure_reason}"
                    ),
                    status="rolled_back",
                )
                return {
                    "ok": False,
                    "applied": False,
                    "rolled_back": True,
                    "restart_scheduled": False,
                    "error": self._redact(failure_reason),
                    "assistant_message": err_msg,
                    "messages": self.memory_store.list_messages(),
                }

            # 4. Verification passed: stage src & tests, commit on main, best-effort push, schedule exit 42
            add_res = self.runner.run(
                ["git", "add", "-A", "--", "src", "tests"],
                cwd=self.repo_root,
                timeout_seconds=20.0,
                env=clean_env,
            )
            if add_res.exit_code != 0:
                self._rollback_savepoint(clean_env)
                detail = self._redact(add_res.stderr or add_res.stdout or "git add failed")
                raise RuntimeError(f"Error preparando commit de auto-mejora: {detail}")

            diff_res = self.runner.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self.repo_root,
                timeout_seconds=10.0,
                env=clean_env,
            )
            if diff_res.exit_code != 0:
                name_chk = self.runner.run(["git", "config", "--get", "user.name"], cwd=self.repo_root, env=clean_env)
                email_chk = self.runner.run(["git", "config", "--get", "user.email"], cwd=self.repo_root, env=clean_env)
                commit_prefix = (
                    []
                    if (name_chk.exit_code == 0 and name_chk.stdout.strip() and email_chk.exit_code == 0 and email_chk.stdout.strip())
                    else ["-c", "user.name=AgentFactory", "-c", "user.email=agent@factory.local"]
                )
                slug_msg = re.sub(r"[\r\n\"`]+", " ", clean_title).strip() or "apply self-improvement"
                commit_res = self.runner.run(
                    ["git", *commit_prefix, "commit", "-m", f"chore(self-improve): {slug_msg}"],
                    cwd=self.repo_root,
                    timeout_seconds=20.0,
                    env=clean_env,
                )
                if commit_res.exit_code != 0:
                    self._rollback_savepoint(clean_env)
                    detail = self._redact(commit_res.stderr or commit_res.stdout or "git commit failed")
                    raise RuntimeError(f"Error haciendo commit en main: {detail}")

            head_res = self.runner.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                timeout_seconds=10.0,
                env=clean_env,
            )
            commit_sha = head_res.stdout.strip() if head_res.exit_code == 0 else ""

            push_env, _push_sec = build_git_push_env(clean_env, github_token)
            try:
                self.runner.run(
                    ["git", "push", "origin", "main"],
                    cwd=self.repo_root,
                    timeout_seconds=20.0,
                    env=push_env,
                )
            except (CommandTimeoutError, OSError, RuntimeError, ValueError):
                pass

            if message_id:
                self.memory_store.update_message_status(message_id, "applied")
            ok_msg = self.memory_store.add_message(
                role="assistant",
                content=(
                    f"✓ Mejora aplicada y verificada (`ruff check src tests` + `pytest -q --no-cov` en verde). "
                    f"Commit `{commit_sha[:7]}` en `main`. Reiniciando Liwi..."
                ),
                status="applied",
            )
            self._schedule_process_restart()
            return {
                "ok": True,
                "applied": True,
                "rolled_back": False,
                "restart_scheduled": True,
                "exit_code": RESTART_EXIT_CODE,
                "commit_sha": commit_sha,
                "assistant_message": ok_msg,
                "messages": self.memory_store.list_messages(),
            }
        finally:
            self._apply_lock.release()
