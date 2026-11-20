"""Isolated workspace sandbox and deterministic test-lock hash verification (INV-13, INV-18, INV-22)."""

import base64
import hashlib
import html
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, Field

from agent_factory.domain.models import validate_project_id
from agent_factory.secrets import redact_secrets

_CACHE_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", ".pytest_cache"})
_BYTECODE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})


def is_python_cache_path(path: str | Path) -> bool:
    """Return True if path refers to Python cache directories or bytecode (*.pyc/*.pyo)."""
    norm = str(path).strip().replace("\\", "/")
    if not norm:
        return False
    parts = [part for part in PurePosixPath(norm).parts if part and part != "."]
    if any(part in _CACHE_DIR_NAMES for part in parts):
        return True
    lower = norm.lower()
    return any(lower.endswith(ext) for ext in _BYTECODE_SUFFIXES)


def workspace_nonempty_test_files(workspace_dir: Path) -> list[str]:
    """Return sorted workspace-relative paths of non-empty test files excluding bytecode/caches."""
    ws = Path(workspace_dir).resolve()
    tests_dir = ws / "tests"
    if not tests_dir.is_dir():
        return []
    found: list[str] = []
    for file_path in tests_dir.rglob("*"):
        if not file_path.is_file():
            continue
        rel_posix = file_path.relative_to(ws).as_posix()
        if is_python_cache_path(rel_posix):
            continue
        if file_path.stat().st_size > 0:
            found.append(rel_posix)
    found.sort()
    return found

RUNNER_CONFIG_FILENAMES: tuple[str, ...] = (
    "pyproject.toml",
    "pytest.ini",
    "package.json",
    "vitest.config.ts",
    "vitest.config.js",
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "conftest.py",
    "setup.cfg",
)

DEFAULT_WORKSPACE_PYTEST_INI: str = "[pytest]\ntestpaths = tests\naddopts = -ra --tb=short\n"

WORKSPACE_GIT_EXCLUDES: tuple[str, ...] = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".venv/",
    "venv/",
    ".agent-factory/",
)


def _merge_ignore_patterns(ignore_file: Path, ws_resolved: Path, patterns: Sequence[str]) -> None:
    """Safely ensure ignore_file inside ws_resolved contains each pattern without following symlinks (INV-22)."""
    if ignore_file.is_symlink():
        return
    try:
        resolved = ignore_file.resolve()
    except OSError:
        return
    if resolved == ws_resolved or not resolved.is_relative_to(ws_resolved):
        return
    if resolved.exists() and not resolved.is_file():
        return
    try:
        existing = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
    except OSError:
        return
    existing_lines = {ln.strip() for ln in existing.splitlines() if ln.strip()}
    missing = [pat for pat in patterns if pat not in existing_lines]
    if not missing:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    updated = existing + prefix + "\n".join(missing) + "\n"
    try:
        resolved.write_text(updated, encoding="utf-8")
    except OSError:
        pass


def ensure_workspace_git_excludes(
    workspace_dir: Path,
    *,
    write_gitignore: bool = True,
) -> None:
    """Ensure .venv/, .agent-factory/, and Python caches are excluded via .git/info/exclude and .gitignore."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return
    if write_gitignore:
        _merge_ignore_patterns(ws / ".gitignore", ws, WORKSPACE_GIT_EXCLUDES)

    git_dir = ws / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return
    if not (git_dir / "HEAD").is_file():
        return
    info_dir = git_dir / "info"
    if info_dir.is_symlink():
        return
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _merge_ignore_patterns(info_dir / "exclude", ws, WORKSPACE_GIT_EXCLUDES)


class SandboxViolationError(ValueError):
    """Raised when a path or project_id attempts to escape workspaces/<project_id>/ (INV-18, INV-22)."""


class TestLockSnapshot(BaseModel):
    """Cryptographic SHA-256 lock of tests/ and runner configuration files (INV-13, AC-2.2)."""

    tests_hash: str
    runner_config_hash: str
    combined_hash: str
    saved_files: dict[str, str] = Field(default_factory=dict)
    saved_binary_files: dict[str, str] = Field(default_factory=dict)


class WorkspaceManager:
    """Manages isolated workspaces/<project_id>/ or authorized external project dirs with containment."""

    def __init__(self, base_dir: Path = Path("workspaces")) -> None:
        self.base_dir = Path(base_dir).resolve()
        self._external_dirs: dict[str, Path] = {}

    def is_external_workspace(self, project_id: str) -> bool:
        """Return True if project_id is bound to an imported external directory."""
        validate_project_id(project_id)
        return project_id in self._external_dirs

    def register_external_workspace(
        self,
        project_id: str,
        external_path: str | Path,
        *,
        factory_root: Path | None = None,
    ) -> Path:
        """Validate and bind an external project directory for project_id (INV-22)."""
        validate_project_id(project_id)
        validated = validate_external_project_path(external_path, factory_root=factory_root)
        self._external_dirs[project_id] = validated
        return validated

    def unregister_external_workspace(self, project_id: str) -> None:
        """Remove external directory binding for project_id without touching disk files."""
        self._external_dirs.pop(project_id, None)

    def get_workspace_dir(self, project_id: str) -> Path:
        """Validate project_id and return the resolved internal or authorized external workspace directory."""
        try:
            validate_project_id(project_id)
        except ValueError as err:
            raise SandboxViolationError(str(err)) from err

        if project_id in self._external_dirs:
            return self._external_dirs[project_id]

        target = (self.base_dir / project_id).resolve()
        if not target.is_relative_to(self.base_dir) or target == self.base_dir:
            raise SandboxViolationError(f"Escape attempt via project_id: {project_id}")
        return target

    def create_workspace(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> Path:
        """Create an isolated workspace for project_id and populate optional initial files."""
        ws_dir = self.get_workspace_dir(project_id)
        ws_dir.mkdir(parents=True, exist_ok=True)
        if template_files:
            for rel_path, content in template_files.items():
                self.write_file(project_id, rel_path, content)
        return ws_dir

    def remove_workspace(self, project_id: str) -> bool:
        """Remove internal workspaces/<project_id>/; unregister external imported folders without deleting files (INV-22)."""
        ws_dir = self.get_workspace_dir(project_id)
        if project_id in self._external_dirs:
            self._external_dirs.pop(project_id, None)
            return False

        raw_path = self.base_dir / project_id
        if raw_path.is_symlink():
            raise SandboxViolationError(
                f"Refusing to remove symlinked workspace '{raw_path}' (INV-22)"
            )
        if not ws_dir.exists():
            return False

        def _handle_readonly(func: object, path: str, _exc: object) -> None:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            if callable(func):
                func(path)

        if sys.version_info >= (3, 12):
            shutil.rmtree(ws_dir, onexc=_handle_readonly)
        else:
            shutil.rmtree(ws_dir, onerror=_handle_readonly)
        return True

    def resolve_safe_path(self, project_id: str, relative_path: str | Path) -> Path:
        """Resolve relative_path inside workspaces/<project_id>/ and reject any traversal or symlink escape."""
        ws_dir = self.get_workspace_dir(project_id)
        path_str = str(relative_path).strip()
        if not path_str:
            raise SandboxViolationError("Empty relative path is invalid")

        if "\\" in str(relative_path):
            raise SandboxViolationError(
                f"Backslash in path rejected: {relative_path}"
            )

        win_path = PureWindowsPath(path_str)
        if (
            any(part == ".." for part in win_path.parts)
            or win_path.is_absolute()
            or path_str.startswith(("/", "\\"))
            or Path(relative_path).is_absolute()
            or (len(path_str) > 1 and path_str[1] == ":")
        ):
            raise SandboxViolationError(
                f"Absolute path or drive letter rejected: {relative_path}"
            )

        candidate = (ws_dir / relative_path).resolve()
        if not candidate.is_relative_to(ws_dir) or candidate == ws_dir:
            raise SandboxViolationError(
                f"Path '{relative_path}' resolves outside workspace '{ws_dir}': {candidate}"
            )
        return candidate

    resolve_path = resolve_safe_path

    def write_file(self, project_id: str, relative_path: str | Path, content: str) -> Path:
        """Safely write content to relative_path inside workspaces/<project_id>/."""
        target_path = self.resolve_safe_path(project_id, relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        return target_path

    def read_file(self, project_id: str, relative_path: str | Path) -> str:
        """Safely read content from relative_path inside workspaces/<project_id>/."""
        target_path = self.resolve_safe_path(project_id, relative_path)
        if not target_path.is_file():
            raise FileNotFoundError(f"File not found: {target_path}")
        return target_path.read_text(encoding="utf-8")


def ensure_workspace_pytest_ini(
    workspace_manager: WorkspaceManager,
    project_id: str,
) -> Path:
    """Ensure workspaces/<project_id>/pytest.ini exists so pytest never climbs to parent configs (INV-13, INV-22)."""
    ws_dir = workspace_manager.get_workspace_dir(project_id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    raw_ini = ws_dir / "pytest.ini"
    if raw_ini.is_symlink():
        raise SandboxViolationError(
            f"Workspace pytest.ini must not be a symlink: {raw_ini}"
        )
    ini_path = workspace_manager.resolve_safe_path(project_id, "pytest.ini")
    if not ini_path.exists():
        workspace_manager.write_file(project_id, "pytest.ini", DEFAULT_WORKSPACE_PYTEST_INI)
    return ini_path


def _read_lock_entry(file_path: Path) -> tuple[bytes, str | None]:
    """Read file as raw bytes; normalize LF for UTF-8 text files and preserve raw bytes for binary."""
    raw_bytes = file_path.read_bytes()
    if b"\x00" not in raw_bytes:
        try:
            text = raw_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            return text.encode("utf-8"), text
        except UnicodeDecodeError:
            pass
    return raw_bytes, None


def _hash_lock_entries(entries: Sequence[tuple[str, bytes]]) -> str:
    """Compute deterministic SHA-256 digest over ordered (rel_posix, raw_bytes) pairs."""
    if not entries:
        return hashlib.sha256(b"").hexdigest()
    hasher = hashlib.sha256()
    for rel_posix, data in entries:
        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")
    return hasher.hexdigest()


def compute_test_lock(
    workspace_dir: Path,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> TestLockSnapshot:
    """Compute deterministic SHA-256 hashes and byte-safe snapshots for tests/ and runner configs (INV-13)."""
    ws = Path(workspace_dir).resolve()
    tests_dir = ws / "tests"

    test_entries: list[tuple[str, bytes, str | None]] = []
    if tests_dir.is_dir():
        for file_path in tests_dir.rglob("*"):
            if file_path.is_file():
                rel_posix = file_path.relative_to(ws).as_posix()
                if is_python_cache_path(rel_posix):
                    continue
                hash_bytes, text_val = _read_lock_entry(file_path)
                test_entries.append((rel_posix, hash_bytes, text_val))
    test_entries.sort(key=lambda x: x[0])

    cfg_entries: list[tuple[str, bytes, str | None]] = []
    for name in sorted(runner_config_names):
        cfg_file = ws / name
        if cfg_file.is_file():
            rel_posix = cfg_file.relative_to(ws).as_posix()
            hash_bytes, text_val = _read_lock_entry(cfg_file)
            cfg_entries.append((rel_posix, hash_bytes, text_val))

    tests_hash = _hash_lock_entries([(rel, data) for rel, data, _ in test_entries])
    runner_config_hash = _hash_lock_entries([(rel, data) for rel, data, _ in cfg_entries])
    combined_hash = hashlib.sha256(f"{tests_hash}:{runner_config_hash}".encode()).hexdigest()

    saved_files: dict[str, str] = {}
    saved_binary_files: dict[str, str] = {}
    for rel, data, text_val in test_entries + cfg_entries:
        if text_val is not None:
            saved_files[rel] = text_val
        else:
            saved_binary_files[rel] = base64.b64encode(data).decode("ascii")

    return TestLockSnapshot(
        tests_hash=tests_hash,
        runner_config_hash=runner_config_hash,
        combined_hash=combined_hash,
        saved_files=saved_files,
        saved_binary_files=saved_binary_files,
    )


def verify_and_restore_test_lock(
    workspace_dir: Path,
    snapshot: TestLockSnapshot,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> bool:
    """Verify tests/ and runner config hashes against snapshot; if altered, revert files via bytes and return False."""
    ws = Path(workspace_dir).resolve()
    current = compute_test_lock(ws, runner_config_names=runner_config_names)
    if current.combined_hash == snapshot.combined_hash:
        return True

    known_paths = set(snapshot.saved_files) | set(snapshot.saved_binary_files)

    # Tampering detected: revert workspace to snapshot
    tests_dir = ws / "tests"
    # 1. Delete any rogue files inside tests/ that are not in snapshot (preserve cache artifacts)
    if tests_dir.is_dir():
        for file_path in list(tests_dir.rglob("*")):
            if file_path.is_file():
                rel_posix = file_path.relative_to(ws).as_posix()
                if is_python_cache_path(rel_posix):
                    continue
                if rel_posix not in known_paths:
                    file_path.unlink()

    # 2. Delete any rogue runner config files that are not in snapshot
    for name in runner_config_names:
        cfg_file = ws / name
        if cfg_file.is_file():
            rel_posix = cfg_file.relative_to(ws).as_posix()
            if rel_posix not in known_paths:
                cfg_file.unlink()

    # 3. Restore all saved text and binary files from snapshot using raw bytes
    for rel_posix, content in snapshot.saved_files.items():
        file_path = ws / rel_posix
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content.encode("utf-8"))

    for rel_posix, b64_data in snapshot.saved_binary_files.items():
        file_path = ws / rel_posix
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(base64.b64decode(b64_data))

    return False


MAX_CUSTOM_LOGO_BYTES: int = 512 * 1024
LOGO_MIME_BY_EXT: dict[str, str] = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}
_SVG_DANGEROUS_TAG_RE = re.compile(
    r"<\s*(?:script|foreignobject|iframe|object|embed)\b",
    re.IGNORECASE,
)
_SVG_EVENT_HANDLER_RE = re.compile(
    r"""(?:\s|/)on[a-z]+\s*=""",
    re.IGNORECASE,
)
_SVG_DANGEROUS_URI_RE = re.compile(
    r"""(?:javascript|vbscript)\s*:|data\s*:\s*text/html""",
    re.IGNORECASE,
)


def _normalize_keywords(text: str) -> str:
    norm = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return norm.lower()


def generate_project_logo_svg(title: str, objective: str = "") -> str:
    """Generate a minimalist 64x64 high-contrast SVG logo tailored to project domain."""
    combined = _normalize_keywords(f"{title} {objective}")
    label = html.escape((title or "Proyecto")[:40].strip() or "Proyecto", quote=True)

    if any(
        kw in combined
        for kw in (
            "music",
            "musica",
            "cancion",
            "song",
            "audio",
            "mp3",
            "sound",
            "playlist",
            "radio",
            "yt-dlp",
            "reproductor",
        )
    ):
        grad_a, grad_b = "#4f46e5", "#7c3aed"
        glyph = (
            '<path d="M27 42V22l17-4v18" fill="none" stroke="#ffffff" stroke-width="3.2" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="23" cy="43" r="4.2" fill="#ffffff"/>'
            '<circle cx="40" cy="39" r="4.2" fill="#ffffff"/>'
            '<path d="M47 22c2.8 3.2 2.8 8.8 0 12" fill="none" stroke="#e0e7ff" '
            'stroke-width="2.6" stroke-linecap="round"/>'
        )
    elif any(
        kw in combined
        for kw in (
            "pomodoro",
            "timer",
            "temporizador",
            "reloj",
            "clock",
            "cronometro",
            "foco",
            "focus",
            "alarma",
            "countdown",
        )
    ):
        grad_a, grad_b = "#ea580c", "#f59e0b"
        glyph = (
            '<circle cx="32" cy="34" r="15" fill="none" stroke="#ffffff" stroke-width="3.2"/>'
            '<path d="M27 14h10M32 34v-8M32 34l5.5 3.5" fill="none" stroke="#ffffff" '
            'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    elif any(
        kw in combined
        for kw in (
            "nota",
            "diario",
            "journal",
            "libro",
            "book",
            "blog",
            "tarea",
            "todo",
            "checklist",
            "presupuesto",
            "gasto",
            "markdown",
        )
    ):
        grad_a, grad_b = "#059669", "#10b981"
        glyph = (
            '<rect x="19" y="16" width="26" height="32" rx="4" fill="none" stroke="#ffffff" stroke-width="3"/>'
            '<path d="M25 25h14M25 32h14M25 39h9" fill="none" stroke="#ecfdf5" '
            'stroke-width="2.8" stroke-linecap="round"/>'
        )
    else:
        grad_a, grad_b = "#0284c7", "#0ea5e9"
        glyph = (
            '<rect x="16" y="17" width="32" height="30" rx="5" fill="none" stroke="#ffffff" stroke-width="3"/>'
            '<path d="M23 28l5 4-5 4M32 36h8" fill="none" stroke="#f0f9ff" '
            'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" '
        f'role="img" aria-label="{label}">'
        "<defs>"
        f'<linearGradient id="afg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{grad_a}"/>'
        f'<stop offset="100%" stop-color="{grad_b}"/>'
        "</linearGradient>"
        "</defs>"
        '<rect x="2" y="2" width="60" height="60" rx="16" fill="url(#afg)"/>'
        f"{glyph}"
        "</svg>\n"
    )


def validate_and_sanitize_logo_bytes(
    data: bytes,
    filename: str = "",
) -> tuple[str, str, bytes]:
    """Validate uploaded logo size (<=512KB), magic bytes, and SVG XSS safety."""
    if not data:
        raise ValueError("El archivo de logo está vacío.")
    if len(data) > MAX_CUSTOM_LOGO_BYTES:
        raise ValueError("El logo supera el límite máximo permitido de 512 KB.")

    detected_ext: str | None = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_ext = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected_ext = "jpg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        detected_ext = "webp"
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "Formato de imagen no soportado; usa PNG, JPG, WebP o SVG."
            ) from exc
        stripped = text.lstrip()
        low = stripped.lower()
        if "<svg" in low[:2048] and ("</svg>" in low or "/>" in low):
            if (
                _SVG_DANGEROUS_TAG_RE.search(stripped)
                or _SVG_EVENT_HANDLER_RE.search(stripped)
                or _SVG_DANGEROUS_URI_RE.search(stripped)
            ):
                raise ValueError(
                    "El SVG contiene scripts, manejadores o enlaces inseguros rechazados."
                )
            detected_ext = "svg"

    if detected_ext is None:
        raise ValueError("Archivo inválido: la firma del archivo no corresponde a SVG, PNG, JPG ni WebP.")

    if filename:
        raw_ext = Path(filename).suffix.lower().lstrip(".")
        norm_ext = "jpg" if raw_ext == "jpeg" else raw_ext
        if norm_ext and norm_ext in LOGO_MIME_BY_EXT and norm_ext != detected_ext:
            raise ValueError(
                f"La extensión '.{raw_ext}' no coincide con el contenido real ({detected_ext})."
            )

    return detected_ext, LOGO_MIME_BY_EXT[detected_ext], data


def ensure_workspace_favicon(
    workspace_dir: Path,
    title: str,
    objective: str = "",
    *,
    overwrite: bool = False,
) -> Path | None:
    """Write default minimalist favicon.svg into workspace root (and built static subdir) if absent."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return None
    svg_content = generate_project_logo_svg(title, objective)
    target_dirs = [ws]
    for sub_name in ("src", "static", "public", "web"):
        sub_dir = ws / sub_name
        if sub_dir.is_dir() and (sub_dir / "index.html").is_file():
            target_dirs.append(sub_dir)

    primary_written: Path | None = None
    for idx, tdir in enumerate(target_dirs):
        fav_svg = tdir / "favicon.svg"
        fav_ico = tdir / "favicon.ico"
        if fav_svg.is_symlink() or fav_ico.is_symlink():
            continue
        if not overwrite and (fav_svg.exists() or fav_ico.exists()):
            if idx == 0:
                primary_written = fav_svg if fav_svg.exists() else fav_ico
            continue
        fav_svg.write_text(svg_content, encoding="utf-8")
        if idx == 0:
            primary_written = fav_svg
    return primary_written


_DEFAULT_FACTORY_ROOT: Path = Path(__file__).resolve().parents[2]
_CONTEXT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
_BINARY_SKIP_EXTS: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".7z",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pdf",
        ".mp3",
        ".mp4",
        ".wav",
        ".webm",
    }
)
_SECRET_FILENAME_EXTS: frozenset[str] = frozenset({".pem", ".key", ".pfx", ".p12", ".jks"})


def _is_link_or_junction(path: Path) -> bool:
    """Return True if path is a POSIX/Windows symlink or NTFS junction."""
    if path.is_symlink():
        return True
    is_junc = getattr(os.path, "isjunction", None)
    return bool(is_junc and is_junc(path))


def validate_external_project_path(
    local_path: str | Path,
    *,
    factory_root: Path | None = None,
) -> Path:
    """Validate local_path is a real existing directory outside system folders, symlinks, and factory repo (INV-22)."""
    if not isinstance(local_path, (str, Path)):
        raise SandboxViolationError("La ruta del proyecto debe ser texto válido.")
    raw_str = str(local_path).strip()
    if not raw_str or "\x00" in raw_str:
        raise SandboxViolationError("La ruta local no puede estar vacía ni contener caracteres inválidos.")
    if raw_str.startswith("\\\\") and not raw_str.startswith("\\\\?\\"):
        raise SandboxViolationError("No se permiten rutas de red UNC al importar proyectos.")

    candidate = Path(raw_str).expanduser()
    if _is_link_or_junction(candidate):
        raise SandboxViolationError(f"Se rechazó un symlink o junction externo (INV-22): {candidate}")
    for ancestor in candidate.parents:
        if ancestor == ancestor.parent:
            break
        if _is_link_or_junction(ancestor):
            raise SandboxViolationError(f"Se rechazó ruta con symlink padre (INV-22): {ancestor}")

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise SandboxViolationError(f"No se pudo resolver la ruta local '{raw_str}': {exc}") from exc

    if not resolved.exists() or not resolved.is_dir():
        raise SandboxViolationError(f"La carpeta '{raw_str}' no existe o no es un directorio válido.")
    if resolved == resolved.parent:
        raise SandboxViolationError("No se permite importar la raíz del sistema de archivos (INV-22).")

    eff_factory = (
        Path(factory_root).resolve() if factory_root is not None else _DEFAULT_FACTORY_ROOT
    )
    if (
        resolved == eff_factory
        or resolved.is_relative_to(eff_factory)
        or eff_factory.is_relative_to(resolved)
    ):
        raise SandboxViolationError(
            "No se permite importar carpetas dentro de Agent Factory ni directorios contenedores (INV-22)."
        )

    posix_low = resolved.as_posix().lower()
    win_prefixes = [
        "c:/windows",
        "c:/program files",
        "c:/program files (x86)",
        "c:/programdata",
        "c:/system volume information",
    ]
    for env_key in ("SystemRoot", "WINDIR"):
        env_val = os.environ.get(env_key, "").strip()
        if env_val:
            try:
                sys_p = Path(env_val).resolve().as_posix().lower()
                if sys_p not in win_prefixes:
                    win_prefixes.append(sys_p)
            except OSError:
                pass
    for prefix in win_prefixes:
        if posix_low == prefix or posix_low.startswith(prefix + "/"):
            raise SandboxViolationError(f"Directorio del sistema rechazado por seguridad (INV-22): {resolved}")

    posix_sys_dirs = (
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/sys",
        "/usr",
        "/system",
        "/library",
    )
    for sys_dir in posix_sys_dirs:
        if posix_low == sys_dir or posix_low.startswith(sys_dir + "/"):
            raise SandboxViolationError(f"Directorio del sistema rechazado por seguridad (INV-22): {resolved}")

    return resolved


def _is_sensitive_or_binary_file(file_path: Path) -> bool:
    name_low = file_path.name.lower()
    if name_low == ".env" or name_low.startswith(".env."):
        return True
    suf = file_path.suffix.lower()
    if suf in _BINARY_SKIP_EXTS or suf in _SECRET_FILENAME_EXTS:
        return True
    try:
        head = file_path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\x00" in head


def _context_file_sort_key(rel_posix: str) -> tuple[int, str]:
    low = rel_posix.lower()
    if low in {
        "readme.md",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        ".agent-factory/manifest.json",
    } or low.startswith("requirements"):
        return (0, rel_posix)
    if low.startswith("src/") or low in {"app.py", "main.py", "server.py", "index.html", "app.js", "styles.css"}:
        return (1, rel_posix)
    if low.startswith("tests/") or "/test_" in low:
        return (3, rel_posix)
    return (2, rel_posix)


def collect_workspace_code_context(
    workspace_dir: Path,
    max_chars: int = 36000,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Safely collect contained, redacted source context from workspace_dir up to max_chars (INV-19, INV-22)."""
    if max_chars <= 0:
        return ""
    ws = Path(workspace_dir)
    if not ws.is_dir() or _is_link_or_junction(ws):
        return ""
    ws_resolved = ws.resolve()

    collected: list[tuple[int, str, Path]] = []

    def _walk(current: Path, depth: int) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if _is_link_or_junction(entry):
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved != ws_resolved and not resolved.is_relative_to(ws_resolved):
                continue
            if resolved.is_dir():
                if resolved.name in _CONTEXT_IGNORED_DIRS:
                    continue
                if depth < 5:
                    _walk(resolved, depth + 1)
            elif resolved.is_file():
                rel_posix = resolved.relative_to(ws_resolved).as_posix()
                if is_python_cache_path(rel_posix) or _is_sensitive_or_binary_file(resolved):
                    continue
                tier, key_path = _context_file_sort_key(rel_posix)
                collected.append((tier, key_path, resolved))

    _walk(ws_resolved, 0)
    if not collected:
        return ""

    collected.sort(key=lambda item: (item[0], item[1]))
    tree_lines = ["Archivos del proyecto:"]
    for _, rel_p, _ in collected[:60]:
        tree_lines.append(f"- {rel_p}")
    header = "\n".join(tree_lines) + "\n\n"

    chunks: list[str] = [header]
    used = len(header)
    for tier, rel_p, fpath in collected:
        if used >= max_chars - 80:
            break
        per_file_cap = min(2200 if tier == 3 else 6000, max_chars - used - 60)
        if per_file_cap < 80:
            break
        try:
            raw_txt = fpath.read_bytes()[: max(per_file_cap * 2, 8192)].decode("utf-8", errors="replace")
        except OSError:
            continue
        clean_txt = redact_secrets(raw_txt, extra_secrets=extra_secrets)
        if len(clean_txt) > per_file_cap:
            clean_txt = clean_txt[: per_file_cap - 30].rstrip() + "\n... [archivo recortado]"
        block = f"=== {rel_p} ===\n{clean_txt.rstrip()}\n\n"
        if used + len(block) > max_chars:
            rem = max_chars - used
            if rem > 80:
                chunks.append(block[: rem - 22].rstrip() + "\n... [contexto recortado]\n")
                used = max_chars
            break
        chunks.append(block)
        used += len(block)

    combined = redact_secrets("".join(chunks), extra_secrets=extra_secrets)
    if len(combined) > max_chars:
        combined = combined[:max_chars]
    return combined


def _scrub_manifest_value(val: Any, extra_secrets: Sequence[str]) -> Any:
    if isinstance(val, str):
        return redact_secrets(val, extra_secrets=extra_secrets)
    if isinstance(val, Mapping):
        return {str(k): _scrub_manifest_value(v, extra_secrets) for k, v in val.items()}
    if isinstance(val, Sequence) and not isinstance(val, (bytes, bytearray)):
        return [_scrub_manifest_value(item, extra_secrets) for item in val]
    return val


def write_workspace_manifest(
    workspace_dir: Path,
    payload: Mapping[str, Any],
    *,
    extra_secrets: Sequence[str] = (),
) -> Path:
    """Safely write redacted .agent-factory/manifest.json inside workspace_dir (INV-19, INV-22)."""
    ws_resolved = Path(workspace_dir).resolve()
    if not ws_resolved.is_dir():
        raise SandboxViolationError(f"Workspace directory '{workspace_dir}' does not exist")
    agent_dir = ws_resolved / ".agent-factory"
    if _is_link_or_junction(agent_dir):
        raise SandboxViolationError(f"Symlink .agent-factory rejected in '{ws_resolved}' (INV-22)")
    agent_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = agent_dir.resolve()
    if not resolved_dir.is_relative_to(ws_resolved):
        raise SandboxViolationError(f".agent-factory escapes workspace '{ws_resolved}' (INV-22)")

    manifest_path = agent_dir / "manifest.json"
    if _is_link_or_junction(manifest_path):
        raise SandboxViolationError(f"Symlink manifest.json rejected in '{ws_resolved}' (INV-22)")
    resolved_manifest = manifest_path.resolve()
    if not resolved_manifest.is_relative_to(ws_resolved):
        raise SandboxViolationError(f"manifest.json escapes workspace '{ws_resolved}' (INV-22)")

    sanitized = _scrub_manifest_value(dict(payload), extra_secrets)
    resolved_manifest.write_text(
        json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return resolved_manifest
