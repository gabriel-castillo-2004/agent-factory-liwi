"""Managed local app execution and HTTP smoke testing on 127.0.0.1 (INV-06, INV-19, INV-22)."""

from __future__ import annotations

import atexit
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agent_factory.domain.models import validate_project_id
from agent_factory.secrets import redact_secrets, sanitize_subprocess_env
from agent_factory.workspace import SandboxViolationError, WorkspaceManager

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT_MIN = 8081
DEFAULT_PORT_MAX = 8199

_EXTERNAL_OR_SKIP_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "//",
    "data:",
    "blob:",
    "mailto:",
    "tel:",
    "javascript:",
    "#",
)
_ROOT_PYTHON_NAMES: tuple[str, ...] = (
    "app.py",
    "main.py",
    "server.py",
    "web.py",
    "run.py",
    "cli.py",
)
_PACKAGE_PYTHON_NAMES: tuple[str, ...] = (
    "app.py",
    "main.py",
    "server.py",
    "web.py",
    "run.py",
)
_IGNORED_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "tests",
        "__pycache__",
        ".venv",
        "venv",
        ".agent-factory",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_MAIN_GUARD_RE = re.compile(r"""if\s+__name__\s*==\s*["']__main__["']\s*:""")
_WEB_SERVER_HINT_RE = re.compile(
    r"\b(?:HTTPServer|ThreadingHTTPServer|Flask|FastAPI|uvicorn|Bottle|Tornado|http\.server)\b"
)
_STATIC_SUBDIR_NAMES: tuple[str, ...] = ("src", "static", "public", "web")
_MAIN_EMPTY_RE = re.compile(r"<main[^>]*>\s*</main>", re.IGNORECASE)
_SRC_APP_REF_RE = re.compile(r"""src\s*=\s*["']\./src/app\.js["']""", re.IGNORECASE)


class LocalAppError(RuntimeError):
    """Raised when a generated project cannot be started or verified locally (INV-07)."""


class WorkspaceAppInfo(BaseModel):
    """Detected local runtime metadata for a project workspace."""

    app_type: Literal["static", "python_local", "unknown"] = "unknown"
    serve_dir: Path | None = None
    entrypoint: Path | None = None
    module_name: str | None = None
    pythonpath: Path | None = None


class LocalAppStatus(BaseModel):
    """Public status snapshot of a locally managed application process."""

    running: bool = False
    url: str | None = None
    app_type: str = "unknown"
    port: int | None = None

    def to_api_dict(self) -> dict[str, object]:
        """Serialize public API payload expected under local_app."""
        return {
            "running": self.running,
            "url": self.url,
            "app_type": self.app_type,
        }


class HttpSmokeTestResult(BaseModel):
    """Result of starting an app locally and probing GET / and relative HTML assets."""

    passed: bool
    url: str | None = None
    summary: str = ""
    failed_resources: list[str] = Field(default_factory=list)


@dataclass
class _RunningAppProcess:
    project_id: str
    process: subprocess.Popen[str]
    port: int
    url: str
    app_type: str
    serve_dir: Path
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    stderr_thread: threading.Thread | None = None


def _ensure_contained(path: Path, workspace_dir: Path) -> Path:
    """Validate path is non-symlink (if present) and resolves strictly inside workspace_dir (INV-22)."""
    ws_resolved = workspace_dir.resolve()
    if path.is_symlink():
        raise SandboxViolationError(
            f"Symlink rejected inside workspace '{ws_resolved}': {path}"
        )
    resolved = path.resolve()
    if resolved != ws_resolved and not resolved.is_relative_to(ws_resolved):
        raise SandboxViolationError(
            f"Path '{path}' escapes workspace '{ws_resolved}': {resolved}"
        )
    return resolved


def _is_template_index_html(html_path: Path, workspace_dir: Path) -> bool:
    """Return True if root index.html is still the untouched empty static-web template."""
    try:
        content = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    has_template_title = "<title>Static Web App</title>" in content
    has_empty_main = bool(_MAIN_EMPTY_RE.search(content))
    if not (has_template_title and has_empty_main):
        return False

    if _SRC_APP_REF_RE.search(content) and not (workspace_dir / "src" / "app.js").is_file():
        return True
    for rel_sub in _STATIC_SUBDIR_NAMES:
        if (workspace_dir / rel_sub / "index.html").is_file():
            return True
    return False


def _scan_workspace_files(
    ws: Path,
    *,
    max_depth: int = 3,
    suffix: str | None = None,
    filename: str | None = None,
) -> list[Path]:
    """Safely walk workspace_dir up to max_depth ignoring test/env/cache dirs (INV-22)."""
    results: list[Path] = []
    ws_resolved = ws.resolve()

    def _walk(current: Path, depth: int) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_file(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                _ensure_contained(entry, ws_resolved)
            if entry.is_dir():
                if entry.name in _IGNORED_SCAN_DIRS or entry.name.startswith("."):
                    continue
                if depth < max_depth:
                    _ensure_contained(entry, ws_resolved)
                    _walk(entry, depth + 1)
            elif entry.is_file():
                if filename is not None and entry.name != filename:
                    continue
                if suffix is not None and entry.suffix.lower() != suffix.lower():
                    continue
                results.append(_ensure_contained(entry, ws_resolved))

    _walk(ws_resolved, 0)
    return results


def resolve_static_root(workspace_dir: Path) -> Path | None:
    """Return built static directory containing index.html across root/src/static/public/web or subdirs (depth<=3)."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return None

    root_index = ws / "index.html"
    has_root_index = root_index.exists() and _ensure_contained(root_index, ws).is_file()
    root_is_empty_template = has_root_index and _is_template_index_html(root_index, ws)

    subdir_candidates: list[Path] = []
    for rel in _STATIC_SUBDIR_NAMES:
        sub = ws / rel
        sub_idx = sub / "index.html"
        if sub.exists() and _ensure_contained(sub, ws).is_dir() and sub_idx.is_file():
            _ensure_contained(sub_idx, ws)
            subdir_candidates.append(sub.resolve())

    if not subdir_candidates:
        for idx_path in _scan_workspace_files(ws, max_depth=3, filename="index.html"):
            if idx_path.parent != ws:
                subdir_candidates.append(idx_path.parent)

    if subdir_candidates and (not has_root_index or root_is_empty_template):
        return subdir_candidates[0]
    if has_root_index:
        return ws
    if subdir_candidates:
        return subdir_candidates[0]
    return None


def _is_non_test_python(py_file: Path) -> bool:
    name = py_file.name.lower()
    return not (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name in {"conftest.py", "setup.py", "__init__.py"}
    )


def _has_main_web_server(py_file: Path) -> bool:
    try:
        snippet = py_file.read_text(encoding="utf-8", errors="replace")[:16384]
    except OSError:
        return False
    return bool(_MAIN_GUARD_RE.search(snippet) and _WEB_SERVER_HINT_RE.search(snippet))


def _find_python_entrypoint(ws: Path) -> Path | None:
    """Recursively locate a contained Python server entrypoint up to depth 3 ignoring tests/caches."""
    for name in _ROOT_PYTHON_NAMES:
        cand = ws / name
        if cand.exists() and _ensure_contained(cand, ws).is_file():
            return _ensure_contained(cand, ws)

    priority_subpaths = [
        *(f"src/{name}" for name in _ROOT_PYTHON_NAMES),
        *(f"app/{name}" for name in _PACKAGE_PYTHON_NAMES),
    ]
    for rel in priority_subpaths:
        cand = ws / rel
        if cand.exists() and _ensure_contained(cand, ws).is_file():
            return _ensure_contained(cand, ws)

    scanned_py = _scan_workspace_files(ws, max_depth=3, suffix=".py")
    # 1. Explicit package/subdir server names ordered by shallowest depth
    pkg_matches = [
        p
        for p in scanned_py
        if p.name in _PACKAGE_PYTHON_NAMES and _is_non_test_python(p)
    ]
    if pkg_matches:
        pkg_matches.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
        return pkg_matches[0]

    # 2. Any module with `if __name__ == "__main__":` + known web server framework/stdlib server
    main_matches = [
        p for p in scanned_py if _is_non_test_python(p) and _has_main_web_server(p)
    ]
    if main_matches:
        main_matches.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
        return main_matches[0]

    # 3. Fallback when requirements.txt exists: first executable .py in workspace
    req_file = ws / "requirements.txt"
    if req_file.exists() and _ensure_contained(req_file, ws).is_file():
        non_test = [p for p in scanned_py if _is_non_test_python(p)]
        if non_test:
            non_test.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
            return non_test[0]
    return None


def detect_workspace_app(workspace_dir: Path) -> WorkspaceAppInfo:
    """Detect app_type ('static', 'python_local', or 'unknown'), entrypoint, and package module inside workspace_dir."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return WorkspaceAppInfo(app_type="unknown")

    py_entry = _find_python_entrypoint(ws)
    if py_entry is not None:
        module_name: str | None = None
        pythonpath: Path | None = None
        src_dir = (ws / "src").resolve()
        if src_dir.is_dir() and py_entry.is_relative_to(src_dir):
            rel_src = py_entry.relative_to(src_dir)
            if len(rel_src.parts) >= 2 and (py_entry.parent / "__init__.py").is_file():
                module_name = ".".join(rel_src.with_suffix("").parts)
                pythonpath = src_dir
        return WorkspaceAppInfo(
            app_type="python_local",
            serve_dir=ws,
            entrypoint=py_entry,
            module_name=module_name,
            pythonpath=pythonpath,
        )

    static_root = resolve_static_root(ws)
    if static_root is not None:
        return WorkspaceAppInfo(
            app_type="static",
            serve_dir=static_root,
            entrypoint=static_root / "index.html",
        )

    return WorkspaceAppInfo(app_type="unknown")


def _drain_stderr(proc: subprocess.Popen[str], lines: deque[str]) -> None:
    """Consume child stderr in background so buffers never stall."""
    pipe = proc.stderr
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            lines.append(line.rstrip("\r\n"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Forcefully terminate process tree on Windows (taskkill /T /F) or POSIX (INV-06)."""
    if proc.poll() is None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=5.0,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                proc.terminate()
            except OSError:
                pass

        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                pass

    if proc.stderr is not None:
        try:
            proc.stderr.close()
        except OSError:
            pass


def _weak_atexit_cleanup(runner_ref: weakref.ref[LocalAppRunner]) -> None:
    runner = runner_ref()
    if runner is not None:
        runner.stop_all()


class LocalAppRunner:
    """Thread-safe manager launching/stopping generated apps on 127.0.0.1 ports 8081..8199."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager | None = None,
        *,
        host: str = LOOPBACK_HOST,
        port_min: int = DEFAULT_PORT_MIN,
        port_max: int = DEFAULT_PORT_MAX,
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError(f"LocalAppRunner must bind exclusively to {LOOPBACK_HOST}, got '{host}'")
        if port_min < 1024 or port_max > 65535 or port_min > port_max:
            raise ValueError(f"Invalid port range {port_min}..{port_max}")
        self.workspace_manager = workspace_manager
        self.host = host
        self.port_min = port_min
        self.port_max = port_max
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self._lock = threading.RLock()
        self._processes: dict[str, _RunningAppProcess] = {}
        atexit.register(_weak_atexit_cleanup, weakref.ref(self))

    def _resolve_workspace_dir(
        self, project_id: str, workspace_dir: Path | None = None
    ) -> Path:
        validate_project_id(project_id)
        if self.workspace_manager is not None:
            managed = self.workspace_manager.get_workspace_dir(project_id)
            if workspace_dir is not None and Path(workspace_dir).resolve() != managed:
                raise SandboxViolationError(
                    f"Workspace '{workspace_dir}' outside managed directory '{managed}' (INV-22)"
                )
            return managed
        if workspace_dir is None:
            raise ValueError(f"workspace_dir required when workspace_manager is unset ({project_id})")
        return Path(workspace_dir).resolve()

    def _prune_dead_locked(self) -> None:
        dead = [pid for pid, entry in self._processes.items() if entry.process.poll() is not None]
        for pid in dead:
            entry = self._processes.pop(pid)
            _terminate_process_tree(entry.process)

    def _allocate_free_port_locked(self) -> int:
        used_ports = {entry.port for entry in self._processes.values()}
        for port in range(self.port_min, self.port_max + 1):
            if port in used_ports:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((self.host, port))
                    return port
                except OSError:
                    continue
        raise LocalAppError(
            f"No free loopback ports available in {self.host}:{self.port_min}-{self.port_max}"
        )

    def _build_env(
        self,
        *,
        port: int,
        app_type: str,
        pythonpath: Path | None = None,
    ) -> dict[str, str]:
        clean = sanitize_subprocess_env()
        if app_type == "python_local":
            clean["PORT"] = str(port)
            clean["HOST"] = self.host
            clean["PYTHONUNBUFFERED"] = "1"
            if pythonpath is not None:
                existing_pp = clean.get("PYTHONPATH", "").strip()
                clean["PYTHONPATH"] = (
                    f"{pythonpath}{os.pathsep}{existing_pp}" if existing_pp else str(pythonpath)
                )
        return clean

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=self.secrets_to_redact)

    def get_status(
        self,
        project_id: str,
        *,
        workspace_dir: Path | None = None,
    ) -> LocalAppStatus:
        """Return current running status, loopback URL, and detected app_type for project_id."""
        ws_dir = self._resolve_workspace_dir(project_id, workspace_dir)
        detected_type = detect_workspace_app(ws_dir).app_type if ws_dir.is_dir() else "unknown"
        with self._lock:
            self._prune_dead_locked()
            entry = self._processes.get(project_id)
            if entry is not None and entry.process.poll() is None:
                return LocalAppStatus(
                    running=True,
                    url=entry.url,
                    app_type=entry.app_type,
                    port=entry.port,
                )
        return LocalAppStatus(running=False, url=None, app_type=detected_type, port=None)

    def start(
        self,
        project_id: str,
        *,
        workspace_dir: Path | None = None,
        readiness_timeout: float = 8.0,
    ) -> LocalAppStatus:
        """Start project_id app on a free 127.0.0.1 port (8081..8199) and wait until HTTP socket is ready."""
        ws_dir = self._resolve_workspace_dir(project_id, workspace_dir)
        if not ws_dir.is_dir():
            raise LocalAppError(f"Workspace directory for '{project_id}' does not exist")

        app_info = detect_workspace_app(ws_dir)
        if app_info.app_type == "unknown" or app_info.serve_dir is None:
            raise LocalAppError(
                f"No runnable static index.html or Python entrypoint found in workspace '{project_id}'"
            )

        with self._lock:
            self._prune_dead_locked()
            existing = self._processes.get(project_id)
            if existing is not None and existing.process.poll() is None:
                return LocalAppStatus(
                    running=True,
                    url=existing.url,
                    app_type=existing.app_type,
                    port=existing.port,
                )

            port = self._allocate_free_port_locked()
            url = f"http://{self.host}:{port}"
            sub_env = self._build_env(
                port=port,
                app_type=app_info.app_type,
                pythonpath=app_info.pythonpath,
            )

            if app_info.app_type == "static":
                cmd = [sys.executable, "-m", "http.server", str(port), "--bind", self.host]
                cwd = app_info.serve_dir
            elif app_info.module_name:
                cmd = [sys.executable, "-m", app_info.module_name]
                cwd = ws_dir
            else:
                assert app_info.entrypoint is not None
                rel_entry = app_info.entrypoint.resolve().relative_to(ws_dir)
                cmd = [sys.executable, str(rel_entry)]
                cwd = ws_dir

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    env=sub_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except (OSError, ValueError) as exc:
                err_msg = self._redact(str(exc) or type(exc).__name__)
                raise LocalAppError(f"Failed to start local app '{project_id}': {err_msg}") from None

            stderr_lines: deque[str] = deque(maxlen=50)
            reader = threading.Thread(
                target=_drain_stderr,
                args=(proc, stderr_lines),
                name=f"local-app-stderr-{project_id}",
                daemon=True,
            )
            reader.start()
            entry = _RunningAppProcess(
                project_id=project_id,
                process=proc,
                port=port,
                url=url,
                app_type=app_info.app_type,
                serve_dir=cwd,
                stderr_lines=stderr_lines,
                stderr_thread=reader,
            )
            self._processes[project_id] = entry

        deadline = time.monotonic() + max(0.5, readiness_timeout)
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                if entry.stderr_thread is not None:
                    entry.stderr_thread.join(timeout=1.0)
                recent = list(entry.stderr_lines)[-8:]
                tail = " | ".join(recent)
                self.stop(project_id)
                detail = self._redact(tail) if tail else f"exit code {exit_code}"
                raise LocalAppError(
                    f"La app local '{project_id}' se detuvo al iniciar (código {exit_code}): {detail}"
                )
            try:
                with socket.create_connection((self.host, port), timeout=0.2):
                    return LocalAppStatus(
                        running=True,
                        url=url,
                        app_type=app_info.app_type,
                        port=port,
                    )
            except OSError:
                time.sleep(0.05)

        if entry.stderr_thread is not None:
            entry.stderr_thread.join(timeout=0.5)
        tail = " | ".join(entry.stderr_lines)
        self.stop(project_id)
        detail = f": {self._redact(tail)}" if tail else ""
        raise LocalAppError(
            f"Local app '{project_id}' failed to listen on {url} within {readiness_timeout:.1f}s{detail}"
        )

    def stop(self, project_id: str, *, workspace_dir: Path | None = None) -> LocalAppStatus:
        """Stop running local app process tree for project_id cleanly and release port."""
        validate_project_id(project_id)
        with self._lock:
            entry = self._processes.pop(project_id, None)
        if entry is not None:
            _terminate_process_tree(entry.process)
            if entry.stderr_thread is not None and entry.stderr_thread.is_alive():
                entry.stderr_thread.join(timeout=1.0)

        detected_type = "unknown"
        if self.workspace_manager is not None:
            ws_dir = self.workspace_manager.get_workspace_dir(project_id)
            if ws_dir.is_dir():
                detected_type = detect_workspace_app(ws_dir).app_type
        elif workspace_dir is not None and Path(workspace_dir).is_dir():
            detected_type = detect_workspace_app(Path(workspace_dir)).app_type
        return LocalAppStatus(running=False, url=None, app_type=detected_type, port=None)

    def stop_all(self) -> None:
        """Terminate all running managed local app processes without raising."""
        with self._lock:
            entries = list(self._processes.values())
            self._processes.clear()
        for entry in entries:
            try:
                _terminate_process_tree(entry.process)
                if entry.stderr_thread is not None and entry.stderr_thread.is_alive():
                    entry.stderr_thread.join(timeout=1.0)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass


class _RelativeAssetHTMLParser(HTMLParser):
    """Collect relative local script[src] and link[href] URLs from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.raw_refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_low = tag.lower()
        attr_map = {k.lower(): v for k, v in attrs if v is not None}
        candidate: str | None = None
        if tag_low == "script":
            candidate = attr_map.get("src")
        elif tag_low == "link":
            candidate = attr_map.get("href")
        if candidate is None:
            return
        cleaned = candidate.strip()
        if not cleaned:
            return
        low = cleaned.lower()
        if low.startswith(_EXTERNAL_OR_SKIP_PREFIXES):
            return
        self.raw_refs.append(cleaned)


def _fetch_local_http(url: str, timeout_seconds: float = 3.0) -> tuple[int, str, bytes]:
    """Perform direct loopback HTTP GET bypassing host proxy settings."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method="GET")
    with opener.open(req, timeout=timeout_seconds) as resp:
        status_code = int(resp.status)
        content_type = str(resp.headers.get("Content-Type", "")).lower()
        body = resp.read(1024 * 1024)
        return status_code, content_type, body


def run_http_smoke_test(
    app_runner: LocalAppRunner,
    project_id: str,
    workspace_dir: Path,
    *,
    timeout_seconds: float = 8.0,
) -> HttpSmokeTestResult:
    """Start project_id on 127.0.0.1, probe GET / and relative script/link resources, and stop cleanly."""
    ws_dir = Path(workspace_dir).resolve()
    app_info = detect_workspace_app(ws_dir)
    if app_info.app_type == "unknown":
        return HttpSmokeTestResult(
            passed=True,
            summary="No runnable web/server entrypoint detected; smoke test skipped.",
        )

    started_url: str | None = None
    try:
        try:
            started = app_runner.start(
                project_id,
                workspace_dir=ws_dir,
                readiness_timeout=timeout_seconds,
            )
            started_url = started.url
        except (LocalAppError, SandboxViolationError, ValueError, OSError) as exc:
            clean_err = app_runner._redact(str(exc))
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed to start app: {clean_err}",
            )

        assert started_url is not None
        root_url = f"{started_url}/"
        try:
            root_code, content_type, root_bytes = _fetch_local_http(root_url)
        except urllib.error.HTTPError as http_err:
            http_err.close()
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed: GET / returned HTTP {http_err.code}",
                failed_resources=[f"/ (HTTP {http_err.code})"],
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed requesting GET /: {app_runner._redact(str(exc))}",
            )

        if root_code != 200:
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed: GET / returned HTTP {root_code}",
                failed_resources=[f"/ (HTTP {root_code})"],
            )

        root_text = root_bytes.decode("utf-8", errors="replace")
        is_html = "text/html" in content_type or root_text.lstrip().startswith("<")
        if not is_html:
            return HttpSmokeTestResult(
                passed=True,
                url=started_url,
                summary=f"HTTP Smoke Test passed: GET {root_url} returned HTTP 200.",
            )

        parser = _RelativeAssetHTMLParser()
        parser.feed(root_text)
        parser.close()

        failed_resources: list[str] = []
        seen_urls: set[str] = set()
        expected_netloc = urllib.parse.urlsplit(root_url).netloc

        for ref in parser.raw_refs:
            resolved = urllib.parse.urljoin(root_url, ref)
            parsed_resolved = urllib.parse.urlsplit(resolved)
            if parsed_resolved.netloc != expected_netloc:
                continue
            if resolved in seen_urls:
                continue
            seen_urls.add(resolved)

            try:
                asset_code, _, _ = _fetch_local_http(resolved)
                if asset_code != 200:
                    failed_resources.append(f"{ref} (HTTP {asset_code})")
            except urllib.error.HTTPError as http_err:
                failed_resources.append(f"{ref} (HTTP {http_err.code})")
                http_err.close()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                failed_resources.append(f"{ref} ({app_runner._redact(str(exc))})")

        if failed_resources:
            joined = ", ".join(failed_resources)
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed on {root_url}: missing or broken assets: {joined}",
                failed_resources=failed_resources,
            )

        return HttpSmokeTestResult(
            passed=True,
            url=started_url,
            summary=f"HTTP Smoke Test passed: GET / and {len(seen_urls)} local asset(s) returned HTTP 200.",
        )
    finally:
        app_runner.stop(project_id, workspace_dir=ws_dir)
