"""Unit tests for per-workspace .venv helpers and LocalAppRunner isolation (INV-01, INV-22, INV-23)."""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import os
import subprocess
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_factory.runtime_runner import (
    _IMPORT_TO_PYPI_PACKAGE,
    LocalAppError,
    LocalAppRunner,
    _detect_workspace_third_party_packages,
    build_venv_env,
    ensure_workspace_venv,
    get_workspace_venv_python,
    is_python_workspace,
)
from agent_factory.workspace import SandboxViolationError, WorkspaceManager


def _create_dir_link(link_path: Path, target_dir: Path) -> None:
    """Create a directory symlink or Windows NTFS junction without requiring elevation."""
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
        )


def _make_offline_venv_runner(
    *,
    recorded_cmds: list[list[str]],
    recorded_envs: list[dict[str, str]] | None = None,
    pytest_installed_initially: bool = False,
    fail_pip_stderr: str | None = None,
) -> Any:
    """Offline command runner stub creating fast real venvs (--without-pip) and simulating pip/pytest checks."""
    real_run = subprocess.run
    state = {"pytest_installed": pytest_installed_initially}

    def _runner(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_list = [str(part) for part in cmd]
        recorded_cmds.append(cmd_list)
        if recorded_envs is not None:
            recorded_envs.append(dict(kwargs.get("env") or {}))

        if len(cmd_list) >= 4 and cmd_list[1:3] == ["-m", "venv"]:
            return real_run(
                [cmd_list[0], "-m", "venv", "--without-pip", *cmd_list[3:]],
                **kwargs,
            )

        if len(cmd_list) >= 3 and cmd_list[1:3] == ["-c", "import pytest"]:
            rc = 0 if state["pytest_installed"] else 1
            stderr = "" if rc == 0 else "ModuleNotFoundError: No module named 'pytest'\n"
            return subprocess.CompletedProcess(cmd_list, rc, stdout="", stderr=stderr)

        if "-m" in cmd_list and "pip" in cmd_list:
            if fail_pip_stderr is not None:
                return subprocess.CompletedProcess(cmd_list, 1, stdout="", stderr=fail_pip_stderr)
            if "pytest" in cmd_list:
                state["pytest_installed"] = True
            return subprocess.CompletedProcess(cmd_list, 0, stdout="ok", stderr="")

        return real_run(cmd_list, **kwargs)

    return _runner


def test_is_python_workspace_detects_manifests_py_files_and_ignores_static_or_excluded_dirs(
    tmp_path: Path,
) -> None:
    """is_python_workspace detects requirements*.txt, pyproject.toml, and .py files (including tests/), while rejecting static-only workspaces and ignored dirs."""
    # Nonexistent dir -> False
    assert is_python_workspace(tmp_path / "missing_ws") is False

    # Purely static project -> False
    ws_static = tmp_path / "static_only"
    ws_static.mkdir()
    (ws_static / "index.html").write_text("<h1>Hello</h1>\n", encoding="utf-8")
    (ws_static / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (ws_static / "app.js").write_text("console.log('hi');\n", encoding="utf-8")
    assert is_python_workspace(ws_static) is False

    # .py files ONLY inside ignored directories -> still False
    for ignored in (".git", ".venv", "venv", "node_modules", "__pycache__", ".agent-factory"):
        ign_dir = ws_static / ignored
        ign_dir.mkdir(parents=True, exist_ok=True)
        (ign_dir / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    assert is_python_workspace(ws_static) is False

    # Root requirements.txt -> True
    ws_req = tmp_path / "req_ws"
    ws_req.mkdir()
    (ws_req / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    assert is_python_workspace(ws_req) is True

    # Variant requirements-dev.txt -> True
    ws_req_dev = tmp_path / "req_dev_ws"
    ws_req_dev.mkdir()
    (ws_req_dev / "requirements-dev.txt").write_text("pytest==8.3.0\n", encoding="utf-8")
    assert is_python_workspace(ws_req_dev) is True

    # pyproject.toml -> True
    ws_pyproj = tmp_path / "pyproj_ws"
    ws_pyproj.mkdir()
    (ws_pyproj / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    assert is_python_workspace(ws_pyproj) is True

    # Root app.py -> True
    ws_root_py = tmp_path / "root_py_ws"
    ws_root_py.mkdir()
    (ws_root_py / "app.py").write_text("print('ok')\n", encoding="utf-8")
    assert is_python_workspace(ws_root_py) is True

    # Nested src/pkg/mod.py -> True
    ws_src_py = tmp_path / "src_py_ws"
    (ws_src_py / "src" / "pkg").mkdir(parents=True)
    (ws_src_py / "src" / "pkg" / "mod.py").write_text("VAL = 42\n", encoding="utf-8")
    assert is_python_workspace(ws_src_py) is True

    # tests/test_smoke.py ONLY (even alongside index.html) -> True
    ws_tests_py = tmp_path / "tests_py_ws"
    (ws_tests_py / "tests").mkdir(parents=True)
    (ws_tests_py / "index.html").write_text("<h1>Static + Pytest</h1>\n", encoding="utf-8")
    (ws_tests_py / "tests" / "test_smoke.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    assert is_python_workspace(ws_tests_py) is True


def test_get_workspace_venv_python_and_build_venv_env_windows_and_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_workspace_venv_python resolves Scripts/python.exe on Windows and bin/python on POSIX; build_venv_env sets VIRTUAL_ENV and prepends venv bin dir to PATH."""
    ws_dir = tmp_path / "ws_platform"
    ws_dir.mkdir()
    venv_dir = (ws_dir / ".venv").resolve()

    # Windows resolution
    monkeypatch.setattr(os, "name", "nt")
    win_py = get_workspace_venv_python(ws_dir)
    assert win_py == venv_dir / "Scripts" / "python.exe"

    base_env = {"PATH": f"C:\\Tools{os.pathsep}C:\\Windows\\System32", "CUSTOM": "1"}
    win_env = build_venv_env(base_env, venv_dir)
    assert base_env["PATH"] == f"C:\\Tools{os.pathsep}C:\\Windows\\System32"
    assert "VIRTUAL_ENV" not in base_env
    assert win_env["VIRTUAL_ENV"] == str(venv_dir)
    assert win_env["CUSTOM"] == "1"
    assert win_env["PATH"].split(os.pathsep)[0] == str(venv_dir / "Scripts")
    assert win_env["PATH"].endswith(f"C:\\Tools{os.pathsep}C:\\Windows\\System32")

    # Empty PATH edge case and idempotency
    win_env_empty = build_venv_env({}, venv_dir)
    assert win_env_empty["VIRTUAL_ENV"] == str(venv_dir)
    assert win_env_empty["PATH"] == str(venv_dir / "Scripts")
    win_env_twice = build_venv_env(win_env_empty, venv_dir)
    assert win_env_twice["PATH"] == str(venv_dir / "Scripts")

    # POSIX resolution
    monkeypatch.setattr(os, "name", "posix")
    posix_py = get_workspace_venv_python(ws_dir)
    assert posix_py == venv_dir / "bin" / "python"

    posix_env = build_venv_env({"PATH": "/usr/bin:/bin"}, venv_dir)
    assert posix_env["VIRTUAL_ENV"] == str(venv_dir)
    assert posix_env["PATH"].split(os.pathsep)[0] == str(venv_dir / "bin")
    assert posix_env["PATH"].endswith("/usr/bin:/bin")


def test_ensure_workspace_venv_creates_venv_updates_gitignore_and_skips_static(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_workspace_venv returns None for static workspaces; for Python workspaces it runs [sys.executable, '-m', 'venv', ...], sanitizes env, and updates .gitignore/.git/info/exclude."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST_VENV_LEAK_SECRET_777")

    # Static workspace -> None, no .venv created
    ws_static = tmp_path / "ws_static"
    ws_static.mkdir()
    (ws_static / "index.html").write_text("<h1>Static</h1>\n", encoding="utf-8")
    static_cmds: list[list[str]] = []
    assert (
        ensure_workspace_venv(
            ws_static,
            project_id="proj-static",
            dep_command_runner=_make_offline_venv_runner(recorded_cmds=static_cmds),
        )
        is None
    )
    assert static_cmds == []
    assert not (ws_static / ".venv").exists()

    # Python workspace with only app.py + git repo -> creates .venv and updates git excludes
    ws_py = tmp_path / "ws_py_only"
    ws_py.mkdir()
    (ws_py / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (ws_py / ".git").mkdir()
    (ws_py / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    cmds: list[list[str]] = []
    envs: list[dict[str, str]] = []
    result = ensure_workspace_venv(
        ws_py,
        project_id="proj-py-only",
        install_deps=True,
        ensure_pytest=False,
        dep_command_runner=_make_offline_venv_runner(recorded_cmds=cmds, recorded_envs=envs),
    )
    assert result is not None
    venv_py, venv_dir = result
    assert venv_dir == (ws_py / ".venv").resolve()
    assert venv_py == get_workspace_venv_python(ws_py)
    assert venv_py.is_file()

    assert len(cmds) == 1
    assert cmds[0][0] == sys.executable
    assert cmds[0][1:3] == ["-m", "venv"]
    assert Path(cmds[0][3]).resolve() == venv_dir

    # Subprocess env must be sanitized (no secrets leaked)
    assert "TELEGRAM_BOT_TOKEN" not in envs[0]

    # .gitignore and .git/info/exclude must contain .venv/
    gitignore_lines = (ws_py / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv/" in gitignore_lines
    exclude_lines = (ws_py / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".venv/" in exclude_lines


def test_ensure_workspace_venv_installs_manifests_ast_and_conditional_pytest_with_hash_cache(
    tmp_path: Path,
) -> None:
    """ensure_workspace_venv installs requirements.txt, pyproject.toml, AST imports, and conditional pytest, caching via .deps_hash and redacting secrets on failure."""
    # 1. requirements.txt + missing AST package + ensure_pytest=True (pytest missing -> installed -> cached)
    ws_req = tmp_path / "ws_req_pytest"
    ws_req.mkdir()
    req_text = "flask==3.0.0\n"
    (ws_req / "requirements.txt").write_text(req_text, encoding="utf-8", newline="\n")
    (ws_req / "app.py").write_text("import flask\nimport requests\n", encoding="utf-8")

    cmds: list[list[str]] = []
    runner = _make_offline_venv_runner(recorded_cmds=cmds, pytest_installed_initially=False)

    res1 = ensure_workspace_venv(
        ws_req,
        project_id="proj-req-pytest",
        install_deps=True,
        ensure_pytest=True,
        dep_command_runner=runner,
    )
    assert res1 is not None
    venv_py, venv_dir = res1

    # Expected command sequence: venv create -> pip -r requirements.txt -> pip install requests (AST) -> python -c import pytest -> pip install pytest
    assert any(c[1:3] == ["-m", "venv"] for c in cmds)
    pip_cmds = [c for c in cmds if "-m" in c and "pip" in c and "install" in c]
    assert len(pip_cmds) == 3
    assert pip_cmds[0] == [str(venv_py), "-m", "pip", "install", "-r", str((ws_req / "requirements.txt").resolve())]
    assert pip_cmds[1] == [str(venv_py), "-m", "pip", "install", "requests"]
    assert pip_cmds[2] == [str(venv_py), "-m", "pip", "install", "pytest"]
    assert any(c == [str(venv_py), "-c", "import pytest"] for c in cmds)

    # Verify .deps_hash exists and differs from ensure_pytest=False hash
    hash_file = venv_dir / ".deps_hash"
    assert hash_file.is_file()
    hash_without_pytest = hashlib.sha256(
        b"requirements.txt\0"
        + req_text.encode("utf-8")
        + b"\0__ast__\0flask\nrequests\0"
    ).hexdigest()
    assert hash_file.read_text(encoding="utf-8").strip() != hash_without_pytest

    # Second call with ensure_pytest=True hits .deps_hash cache -> ZERO additional commands
    cmd_count_after_first = len(cmds)
    res2 = ensure_workspace_venv(
        ws_req,
        project_id="proj-req-pytest",
        install_deps=True,
        ensure_pytest=True,
        dep_command_runner=runner,
    )
    assert res2 == (venv_py, venv_dir)
    assert len(cmds) == cmd_count_after_first

    # 2. pyproject.toml [project] + pytest already available -> installs -e . and skips pip install pytest
    ws_pyproj = tmp_path / "ws_pyproj_pytest"
    ws_pyproj.mkdir()
    (ws_pyproj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["httpx"]\n',
        encoding="utf-8",
    )
    (ws_pyproj / "main.py").write_text("import httpx\n", encoding="utf-8")

    cmds_pyproj: list[list[str]] = []
    runner_pyproj = _make_offline_venv_runner(
        recorded_cmds=cmds_pyproj,
        pytest_installed_initially=True,
    )
    res_p = ensure_workspace_venv(
        ws_pyproj,
        project_id="proj-pyproj",
        install_deps=True,
        ensure_pytest=True,
        dep_command_runner=runner_pyproj,
    )
    assert res_p is not None
    venv_py_p, _ = res_p
    pip_p = [c for c in cmds_pyproj if "-m" in c and "pip" in c and "install" in c]
    assert pip_p == [[str(venv_py_p), "-m", "pip", "install", "-e", "."]]
    assert [str(venv_py_p), "-c", "import pytest"] in cmds_pyproj

    # 3. Secret redaction when pip fails
    ws_fail = tmp_path / "ws_secret_fail"
    ws_fail.mkdir()
    (ws_fail / "requirements.txt").write_text("broken-pkg==0.0.1\n", encoding="utf-8")
    secret_tok = "TEST_PIP_SECRET_TOKEN_9944"
    fail_cmds: list[list[str]] = []
    fail_runner = _make_offline_venv_runner(
        recorded_cmds=fail_cmds,
        fail_pip_stderr=f"ERROR: Could not fetch https://user:{secret_tok}@private.local/simple\n",
    )
    with pytest.raises(LocalAppError) as err_info:
        ensure_workspace_venv(
            ws_fail,
            project_id="proj-secret-fail",
            install_deps=True,
            dep_command_runner=fail_runner,
            secrets_to_redact=(secret_tok,),
        )
    msg = str(err_info.value)
    assert secret_tok not in msg
    assert "[REDACTED]" in msg


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_local_app_runner_uses_workspace_venv_even_with_app_py_only(
    tmp_path: Path,
) -> None:
    """LocalAppRunner.start(..., install_deps=True) creates and runs <workspace>/.venv even for a stdlib-only app.py without requirements.txt; install_deps=False reuses existing .venv."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_runner_venv")
    pid = "proj-stdlib-only"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "app.py").write_text(
        "import http.server, json, os, sys\n"
        "host = os.environ['HOST']\n"
        "port = int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({\n"
        "            'prefix': sys.prefix,\n"
        "            'exe': sys.executable,\n"
        "            'virtual_env': os.environ.get('VIRTUAL_ENV', ''),\n"
        "            'path_head': os.environ.get('PATH', '').split(os.pathsep)[0],\n"
        "        }).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "if __name__ == '__main__':\n"
        "    http.server.ThreadingHTTPServer((host, port), H).serve_forever()\n",
        encoding="utf-8",
    )

    cmds: list[list[str]] = []
    runner = LocalAppRunner(
        workspace_manager=ws_mgr,
        dep_command_runner=_make_offline_venv_runner(recorded_cmds=cmds),
    )
    expected_venv_dir = (ws_dir / ".venv").resolve()
    expected_venv_py = get_workspace_venv_python(ws_dir)

    try:
        # 1. start(install_deps=True) MUST create <ws>/.venv and run app.py inside it (not global factory python)
        st1 = runner.start(pid, install_deps=True)
        assert st1.running is True
        assert st1.app_type == "python_local"
        assert st1.url is not None
        assert expected_venv_py.is_file()
        assert ".venv/" in (ws_dir / ".gitignore").read_text(encoding="utf-8")

        with urllib.request.urlopen(st1.url, timeout=5.0) as resp:
            info1 = json.loads(resp.read().decode("utf-8"))
        assert Path(info1["prefix"]).resolve() == expected_venv_dir
        assert Path(info1["virtual_env"]).resolve() == expected_venv_dir
        assert Path(info1["path_head"]).resolve() == expected_venv_py.parent.resolve()
        assert Path(info1["prefix"]).resolve() != Path(sys.prefix).resolve()
        runner.stop(pid)

        # 2. start(install_deps=False) when .venv already exists MUST still use the isolated .venv interpreter and env
        cmd_count_before = len(cmds)
        st2 = runner.start(pid, install_deps=False)
        assert st2.running is True
        assert st2.url is not None
        assert len(cmds) == cmd_count_before
        with urllib.request.urlopen(st2.url, timeout=5.0) as resp2:
            info2 = json.loads(resp2.read().decode("utf-8"))
        assert Path(info2["prefix"]).resolve() == expected_venv_dir
        assert Path(info2["virtual_env"]).resolve() == expected_venv_dir
        assert Path(info2["path_head"]).resolve() == expected_venv_py.parent.resolve()
    finally:
        runner.stop_all()


def test_workspace_venv_rejects_symlinks_and_junctions_inv22(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symlinks/junctions for .venv or its python executable are rejected with SandboxViolationError (INV-22)."""
    outside_venv = tmp_path / "outside_venv"
    outside_venv.mkdir()

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_symlink_guard")
    pid = "proj-venv-symlink"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "app.py").write_text("print('hi')\n", encoding="utf-8")

    # 1. Symlink/junction at <ws>/.venv pointing outside workspace
    _create_dir_link(ws_dir / ".venv", outside_venv)

    with pytest.raises(SandboxViolationError):
        get_workspace_venv_python(ws_dir)

    with pytest.raises(SandboxViolationError):
        ensure_workspace_venv(ws_dir, project_id=pid, install_deps=True)

    with pytest.raises(SandboxViolationError):
        is_python_workspace(ws_dir)

    runner = LocalAppRunner(workspace_manager=ws_mgr)
    with pytest.raises(SandboxViolationError):
        runner.start(pid, install_deps=True)
    with pytest.raises(SandboxViolationError):
        runner.start(pid, install_deps=False)

    # 2. Regular .venv directory, but symlinked python executable inside .venv
    ws_py_link = tmp_path / "ws_py_exe_link"
    ws_py_link.mkdir()
    (ws_py_link / "app.py").write_text("print('hi')\n", encoding="utf-8")
    venv_bin = ws_py_link / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / ("python.exe" if os.name == "nt" else "python")
    fake_py.write_text("", encoding="utf-8")

    real_is_symlink = Path.is_symlink
    fake_py_norm = str(fake_py.resolve()).lower()

    def _mock_is_symlink(self: Path) -> bool:
        if str(self).lower() == fake_py_norm:
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", _mock_is_symlink)

    with pytest.raises(SandboxViolationError):
        get_workspace_venv_python(ws_py_link)

    with pytest.raises(SandboxViolationError):
        ensure_workspace_venv(ws_py_link, project_id="proj-py-exe-link", install_deps=True)


def test_ast_dependency_filter_rejects_flag_underscore_stdlib_and_invalid_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AST dependency filter discards flag/underscore prefixes (pre- and post-normalization), stdlib modules, and non-PEP508 tokens while keeping valid external packages."""
    # 1. Reproduces `__main__` -> `--main--` and `_interno` -> `-interno` plus non-ASCII identifier `café`
    ws = tmp_path / "ws_ast_hardening"
    ws.mkdir()
    (ws / "app.py").write_text(
        "import __main__\n"
        "from __main__ import main_fn\n"
        "import _interno\n"
        "from _private_mod import helper\n"
        "import café\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import requests\n"
        "import fastapi\n"
        "import sklearn\n"
        "import scikit_learn\n",
        encoding="utf-8",
    )

    detected = _detect_workspace_third_party_packages(ws)
    assert detected == ["fastapi", "requests", "scikit-learn", "uvicorn"]

    # Verify `ensure_workspace_venv` never passes `--main--` or invalid tokens to `python -m pip install`
    cmds: list[list[str]] = []
    res = ensure_workspace_venv(
        ws,
        project_id="proj-ast-hardening",
        install_deps=True,
        ensure_pytest=False,
        dep_command_runner=_make_offline_venv_runner(recorded_cmds=cmds),
    )
    assert res is not None
    venv_py, _ = res
    pip_cmds = [c for c in cmds if "-m" in c and "pip" in c and "install" in c]
    assert pip_cmds == [
        [str(venv_py), "-m", "pip", "install", "fastapi", "requests", "scikit-learn", "uvicorn"]
    ]

    # 2. Synthetic AST tokens covering leading `-`/`_`, regex violations, and whitespace/newline smuggling
    ws_synth = tmp_path / "ws_ast_synthetic"
    ws_synth.mkdir()
    (ws_synth / "main.py").write_text("# placeholder\n", encoding="utf-8")

    synthetic_tree = ast.Module(
        body=[
            ast.Import(
                names=[
                    ast.alias(name="__main__"),
                    ast.alias(name="--main--"),
                    ast.alias(name="_interno"),
                    ast.alias(name="-opcion"),
                    ast.alias(name=".leadingdot"),
                    ast.alias(name="bad!pkg"),
                    ast.alias(name="bad/slash"),
                    ast.alias(name="bad;semi"),
                    ast.alias(name="bad space"),
                    ast.alias(name="requests\n--extra"),
                    ast.alias(name="requests"),
                    ast.alias(name="scikit_learn"),
                ]
            ),
            ast.ImportFrom(module="--flag-from", names=[ast.alias(name="x")], level=0),
            ast.ImportFrom(module="_under_from", names=[ast.alias(name="y")], level=0),
            ast.ImportFrom(module="fastapi", names=[ast.alias(name="FastAPI")], level=0),
        ],
        type_ignores=[],
    )
    real_ast_parse = ast.parse
    monkeypatch.setattr(ast, "parse", lambda source, filename="<unknown>": synthetic_tree)
    try:
        assert _detect_workspace_third_party_packages(ws_synth) == [
            "fastapi",
            "requests",
            "scikit-learn",
            "uvicorn",
        ]
    finally:
        monkeypatch.setattr(ast, "parse", real_ast_parse)

    # 3. Post-normalization rejection (even if a mapping yields `-`, `_`, invalid regex, or stdlib)
    ws_mapped = tmp_path / "ws_ast_post_norm"
    ws_mapped.mkdir()
    (ws_mapped / "app.py").write_text(
        "import alias_dash\n"
        "import alias_opt\n"
        "import alias_under\n"
        "import alias_bad_chars\n"
        "import alias_stdlib\n"
        "import requests\n"
        "import sklearn\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(_IMPORT_TO_PYPI_PACKAGE, "alias_dash", "--main--")
    monkeypatch.setitem(_IMPORT_TO_PYPI_PACKAGE, "alias_opt", "-opcion")
    monkeypatch.setitem(_IMPORT_TO_PYPI_PACKAGE, "alias_under", "_interno")
    monkeypatch.setitem(_IMPORT_TO_PYPI_PACKAGE, "alias_bad_chars", "pkg!invalid")
    monkeypatch.setitem(_IMPORT_TO_PYPI_PACKAGE, "alias_stdlib", "json")

    assert _detect_workspace_third_party_packages(ws_mapped) == [
        "requests",
        "scikit-learn",
    ]

    # 4. Every module present in sys.stdlib_module_names is discarded
    ws_stdlib = tmp_path / "ws_ast_stdlib_all"
    ws_stdlib.mkdir()
    stdlib_imports = [
        f"import {mod}"
        for mod in sorted(sys.stdlib_module_names)
        if mod.isidentifier() and not keyword.iskeyword(mod)
    ]
    assert len(stdlib_imports) > 50
    (ws_stdlib / "stdlib_only.py").write_text("\n".join(stdlib_imports) + "\n", encoding="utf-8")
    assert _detect_workspace_third_party_packages(ws_stdlib) == []

