"""Disjoint-file contract planner and bounded parallel worker executor for .liwi-staging."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.config import build_workspace_pytest_env
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

MAX_PARALLEL_WORKERS: int = 3
DEFAULT_WORKER_TIMEOUT_SECONDS: float = 300.0
DEFAULT_VERIFY_TIMEOUT_SECONDS: float = 300.0
_MAX_INLINE_PROMPT_CHARS: int = 6000

_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    ".env",
    "MASTER_PROMPT.md",
    "AGENTS.md",
    "config/models.toml",
    "data",
    "workspaces",
)
_FORBIDDEN_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
    }
)
_SECRET_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
    }
)
_SECRET_EXTENSIONS: frozenset[str] = frozenset({".pem", ".key", ".pfx", ".p12", ".jks"})
_UI_EXTENSIONS: frozenset[str] = frozenset({".html", ".css", ".js", ".svg"})
_UI_STEM_TOKENS: tuple[str, ...] = ("dashboard", "template", "static")


@dataclass(frozen=True)
class ModuleContract:
    """Typed contract shared across disjoint parallel workers."""

    module: str
    symbols: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        clean_mod = str(self.module or "").strip()
        if not clean_mod:
            raise ValueError("ModuleContract.module must be a non-empty string")
        clean_symbols = tuple(str(s).strip() for s in self.symbols if str(s).strip())
        object.__setattr__(self, "module", clean_mod)
        object.__setattr__(self, "symbols", clean_symbols)
        object.__setattr__(self, "description", str(self.description or "").strip())


@dataclass(frozen=True)
class ParallelSubtask:
    """Disjoint unit of work assigned to a single parallel staging worker."""

    subtask_id: str
    role: str
    assigned_files: tuple[str, ...]
    instructions: str = ""
    contracts: tuple[ModuleContract, ...] = ()

    def __post_init__(self) -> None:
        clean_id = str(self.subtask_id or "").strip()
        clean_role = str(self.role or "").strip()
        if not clean_id:
            raise ValueError("ParallelSubtask.subtask_id must be a non-empty string")
        if not clean_role:
            raise ValueError("ParallelSubtask.role must be a non-empty string")
        object.__setattr__(self, "subtask_id", clean_id)
        object.__setattr__(self, "role", clean_role)
        object.__setattr__(self, "assigned_files", tuple(self.assigned_files))
        object.__setattr__(self, "instructions", str(self.instructions or "").strip())
        object.__setattr__(self, "contracts", tuple(self.contracts))


@dataclass(frozen=True)
class ParallelPlan:
    """Complete disjoint-file parallel improvement plan for .liwi-staging."""

    title: str
    subtasks: tuple[ParallelSubtask, ...]
    contracts: tuple[ModuleContract, ...] = ()
    target_tests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        clean_title = str(self.title or "").strip()
        if not clean_title:
            raise ValueError("ParallelPlan.title must be a non-empty string")
        object.__setattr__(self, "title", clean_title)
        object.__setattr__(self, "subtasks", tuple(self.subtasks))
        object.__setattr__(self, "contracts", tuple(self.contracts))
        object.__setattr__(self, "target_tests", tuple(self.target_tests))


@dataclass(frozen=True)
class WorkerResult:
    """Execution and per-worker ruff verification outcome."""

    subtask_id: str
    role: str
    assigned_files: tuple[str, ...]
    success: bool
    output: str = ""
    error: str = ""
    ruff_ok: bool = False
    ruff_output: str = ""
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "assigned_files", tuple(self.assigned_files))


@dataclass(frozen=True)
class ParallelRunResult:
    """Aggregated outcome of running a ParallelPlan inside .liwi-staging."""

    success: bool
    plan: ParallelPlan
    worker_results: tuple[WorkerResult, ...]
    pytest_ran: bool = False
    pytest_ok: bool = False
    pytest_output: str = ""
    target_tests: tuple[str, ...] = ()
    out_of_partition_files: tuple[str, ...] = ()
    error: str = ""
    commit_allowed: bool = False
    commit_sha: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_results", tuple(self.worker_results))
        object.__setattr__(self, "target_tests", tuple(self.target_tests))
        object.__setattr__(self, "out_of_partition_files", tuple(self.out_of_partition_files))


WorkerRunner = Callable[[ParallelSubtask, str, Path], tuple[bool, str]]
RuffRunner = Callable[[Sequence[str], Path], tuple[bool, str]]
PytestRunner = Callable[[Sequence[str], Path], tuple[bool, str]]
ChangedFilesInspector = Callable[[Path], Sequence[str]]


def _canonical_path_key(norm_path: str) -> str:
    """Return case-normalized collision key (lowercase on Windows, exact on POSIX)."""
    return norm_path.lower() if os.name == "nt" else norm_path


def _matches_forbidden_prefix(norm_path: str) -> str | None:
    low = norm_path.lower()
    for raw_pref in _FORBIDDEN_PREFIXES:
        pref = raw_pref.replace("\\", "/").strip("/").lower()
        if low == pref or low.startswith(pref + "/"):
            return raw_pref
    return None


def normalize_relative_path(path: str) -> str:
    """Normalize a repository-relative path to POSIX '/' form and reject unsafe/forbidden paths (INV-19, INV-22)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("La ruta de archivo debe ser una cadena no vacía.")
    raw = path.strip()
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise ValueError(f"Caracter de control inválido en ruta (INV-22): {path!r}")
    if raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError(f"Ruta absoluta o unidad rechazada (INV-22): {path!r}")

    if PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute():
        raise ValueError(f"Ruta absoluta rechazada (INV-22): {path!r}")

    posix_raw = raw.replace("\\", "/")
    cleaned_parts: list[str] = []
    for seg in posix_raw.split("/"):
        if seg in {"", "."}:
            continue
        if seg == "..":
            raise ValueError(f"Traversal de directorio ('..') rechazado (INV-22): {path!r}")
        cleaned_parts.append(seg)

    if not cleaned_parts:
        raise ValueError(f"La ruta resuelve a la raíz del repositorio: {path!r}")

    for seg in cleaned_parts:
        if seg.lower() in _FORBIDDEN_DIR_SEGMENTS:
            raise ValueError(f"Segmento de directorio prohibido '{seg}' (INV-22): {path!r}")

    filename_low = cleaned_parts[-1].lower()
    if (
        filename_low in _SECRET_FILENAMES
        or filename_low.startswith(".env.")
        or any(filename_low.endswith(ext) for ext in _SECRET_EXTENSIONS)
    ):
        raise ValueError(f"Ruta de archivo sensible o .env rechazada (INV-19, INV-22): {path!r}")

    norm = "/".join(cleaned_parts)
    forbidden = _matches_forbidden_prefix(norm)
    if forbidden is not None:
        raise ValueError(
            f"La ruta '{norm}' viola el prefijo protegido '{forbidden}' (INV-19, INV-22)"
        )
    return norm


def validate_disjoint_subtasks(
    subtasks: Sequence[ParallelSubtask],
) -> tuple[ParallelSubtask, ...]:
    """Validate that subtasks are non-empty, safe, and strictly disjoint across normalized file paths."""
    if not subtasks:
        raise ValueError("El plan paralelo requiere al menos una sub-tarea.")

    validated: list[ParallelSubtask] = []
    seen_ids: set[str] = set()
    claimed_by: dict[str, tuple[str, str]] = {}

    for subtask in subtasks:
        if subtask.subtask_id in seen_ids:
            raise ValueError(f"subtask_id duplicado: '{subtask.subtask_id}'")
        seen_ids.add(subtask.subtask_id)

        if not subtask.assigned_files:
            raise ValueError(
                f"La sub-tarea '{subtask.subtask_id}' debe declarar al menos un archivo asignado."
            )

        normalized_files: list[str] = []
        seen_in_subtask: set[str] = set()
        for raw_file in subtask.assigned_files:
            norm = normalize_relative_path(raw_file)
            key = _canonical_path_key(norm)
            if key in claimed_by:
                owner_id, owner_norm = claimed_by[key]
                raise ValueError(
                    f"Conflicto de archivo compartido '{owner_norm}' entre sub-tareas "
                    f"'{owner_id}' y '{subtask.subtask_id}'"
                )
            if key not in seen_in_subtask:
                seen_in_subtask.add(key)
                normalized_files.append(norm)

        for norm in normalized_files:
            claimed_by[_canonical_path_key(norm)] = (subtask.subtask_id, norm)

        validated.append(
            ParallelSubtask(
                subtask_id=subtask.subtask_id,
                role=subtask.role,
                assigned_files=tuple(normalized_files),
                instructions=subtask.instructions,
                contracts=subtask.contracts,
            )
        )

    return tuple(validated)


def _path_to_module_name(norm_path: str) -> str:
    stripped = norm_path.removeprefix("src/").removesuffix(".py")
    return stripped.replace("/", ".")


def generate_module_contracts(
    files: Sequence[str],
    *,
    symbols_by_module: Mapping[str, Sequence[str]] | None = None,
    descriptions: Mapping[str, str] | None = None,
) -> tuple[ModuleContract, ...]:
    """Generate baseline ModuleContract objects for non-test Python modules in `files`."""
    sym_map = symbols_by_module or {}
    desc_map = descriptions or {}
    contracts: list[ModuleContract] = []
    seen_modules: set[str] = set()

    for raw in files:
        norm = normalize_relative_path(raw)
        low = norm.lower()
        if low.startswith("tests/") or not low.endswith(".py") or Path(norm).name == "__init__.py":
            continue
        module_name = _path_to_module_name(norm)
        if module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        symbols = tuple(sym_map.get(module_name, ()))
        description = desc_map.get(module_name) or f"Contrato público del módulo {module_name} ({norm})."
        contracts.append(
            ModuleContract(
                module=module_name,
                symbols=symbols,
                description=description,
            )
        )
    return tuple(contracts)


def _classify_partition(norm_path: str) -> str:
    low = norm_path.lower()
    if low.startswith("tests/"):
        return "tests"
    path_obj = PurePosixPath(norm_path)
    stem_low = path_obj.stem.lower()
    if path_obj.suffix.lower() in _UI_EXTENSIONS or any(
        tok in stem_low for tok in _UI_STEM_TOKENS
    ):
        return "ui"
    return "logic"


def _merge_contracts(*groups: Sequence[ModuleContract]) -> tuple[ModuleContract, ...]:
    merged: list[ModuleContract] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for group in groups:
        for contract in group:
            sig = (contract.module, contract.symbols, contract.description)
            if sig not in seen:
                seen.add(sig)
                merged.append(contract)
    return tuple(merged)


def _default_role_instruction(role: str, files: Sequence[str], prompt: str) -> str:
    file_list = ", ".join(files)
    role_hints = {
        "logic": f"Implementa la lógica de negocio estrictamente en: {file_list}.",
        "ui": f"Implementa los cambios de interfaz/UI estrictamente en: {file_list}.",
        "tests": f"Implementa y actualiza pruebas TDD estrictamente en: {file_list}.",
    }
    base = role_hints.get(role, f"Implementa los cambios asignados en: {file_list}.")
    clean_prompt = str(prompt or "").strip()
    return f"{base}\nObjetivo general: {clean_prompt}" if clean_prompt else base


def _extract_test_files(paths: Sequence[str]) -> tuple[str, ...]:
    tests: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = normalize_relative_path(path)
        low = norm.lower()
        name_low = PurePosixPath(norm).name.lower()
        if low.startswith("tests/") and name_low.startswith("test_") and low.endswith(".py"):
            key = _canonical_path_key(norm)
            if key not in seen:
                seen.add(key)
                tests.append(norm)
    return tuple(tests)


def build_disjoint_plan(
    title: str,
    files: Sequence[str] = (),
    *,
    prompt: str = "",
    contracts: Sequence[ModuleContract] = (),
    subtasks: Sequence[ParallelSubtask] | None = None,
    role_instructions: Mapping[str, str] | None = None,
    target_tests: Sequence[str] = (),
) -> ParallelPlan:
    """Partition files into <=3 disjoint subtasks (logic/ui/tests) or validate explicit subtasks and propagate contracts."""
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("El título del plan paralelo es obligatorio.")

    if subtasks is not None:
        validated_subtasks = validate_disjoint_subtasks(subtasks)
        all_assigned = [f for st in validated_subtasks for f in st.assigned_files]
        subtask_contracts = [c for st in validated_subtasks for c in st.contracts]
        plan_contracts = _merge_contracts(contracts, subtask_contracts)
        if not plan_contracts:
            plan_contracts = generate_module_contracts(all_assigned)

        propagated_subtasks = tuple(
            ParallelSubtask(
                subtask_id=st.subtask_id,
                role=st.role,
                assigned_files=st.assigned_files,
                instructions=st.instructions,
                contracts=_merge_contracts(plan_contracts, st.contracts),
            )
            for st in validated_subtasks
        )
    else:
        if not files:
            raise ValueError("Se requiere al menos un archivo o sub-tareas explícitas para construir el plan.")
        normalized_files: list[str] = []
        seen_keys: set[str] = set()
        for raw_file in files:
            norm = normalize_relative_path(raw_file)
            key = _canonical_path_key(norm)
            if key not in seen_keys:
                seen_keys.add(key)
                normalized_files.append(norm)

        plan_contracts = (
            _merge_contracts(contracts)
            if contracts
            else generate_module_contracts(normalized_files)
        )

        buckets: dict[str, list[str]] = {"logic": [], "ui": [], "tests": []}
        for norm in normalized_files:
            buckets[_classify_partition(norm)].append(norm)

        role_instr_map = role_instructions or {}
        built: list[ParallelSubtask] = []
        for role in ("logic", "ui", "tests"):
            bucket_files = buckets[role]
            if not bucket_files:
                continue
            instr = str(role_instr_map.get(role) or "").strip() or _default_role_instruction(
                role, bucket_files, prompt
            )
            built.append(
                ParallelSubtask(
                    subtask_id=f"worker-{role}",
                    role=role,
                    assigned_files=tuple(bucket_files),
                    instructions=instr,
                    contracts=plan_contracts,
                )
            )
        propagated_subtasks = validate_disjoint_subtasks(built)
        all_assigned = normalized_files

    if target_tests:
        resolved_tests_list: list[str] = []
        seen_test_keys: set[str] = set()
        for raw_test in target_tests:
            norm_test = normalize_relative_path(raw_test)
            key = _canonical_path_key(norm_test)
            if key not in seen_test_keys:
                seen_test_keys.add(key)
                resolved_tests_list.append(norm_test)
        resolved_target_tests = tuple(resolved_tests_list)
    else:
        resolved_target_tests = _extract_test_files(all_assigned)

    return ParallelPlan(
        title=clean_title,
        subtasks=propagated_subtasks,
        contracts=plan_contracts,
        target_tests=resolved_target_tests,
    )


def build_worker_prompt(plan: ParallelPlan, subtask: ParallelSubtask) -> str:
    """Build strict, contract-aware worker prompt confining edits to subtask.assigned_files."""
    effective_contracts = _merge_contracts(plan.contracts, subtask.contracts)
    if effective_contracts:
        contract_lines: list[str] = []
        for c in effective_contracts:
            syms = "; ".join(c.symbols) if c.symbols else "(sin firmas explícitas)"
            desc = f" — {c.description}" if c.description else ""
            contract_lines.append(f"- {c.module}: {syms}{desc}")
        contracts_block = "\n".join(contract_lines)
    else:
        contracts_block = "- (sin contratos inter-módulo adicionales)"

    allowed_block = "\n".join(f"- {rel}" for rel in subtask.assigned_files)

    return (
        f"PLAN PARALELO EN STAGING: {plan.title}\n"
        f"WORKER: {subtask.subtask_id} (rol: {subtask.role})\n\n"
        "REGLAS ESTRICTAS DE PARTICIÓN Y SEGURIDAD:\n"
        "1. PROHIBIDO leer o modificar .env, MASTER_PROMPT.md, AGENTS.md, config/models.toml, data/ o workspaces/.\n"
        "2. PROHIBIDO crear, editar o eliminar cualquier archivo fuera de tu partición asignada.\n"
        "3. PROHIBIDO ejecutar la suite completa de pytest dentro del worker; cada worker solo pasa `ruff check` "
        "sobre sus archivos asignados y `pytest -q --no-cov` dirigido se ejecuta una sola vez al finalizar todos los workers.\n\n"
        f"ARCHIVOS PERMITIDOS (ÚNICA PARTICIÓN AUTORIZADA):\n{allowed_block}\n\n"
        f"CONTRATOS Y FIRMAS ACORDADOS ENTRE MÓDULOS:\n{contracts_block}\n\n"
        f"INSTRUCCIONES DE LA SUB-TAREA:\n{subtask.instructions or 'Implementa los cambios solicitados en tus archivos asignados.'}\n"
    )


def _parse_porcelain_paths(stdout: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        raw_entry = line[3:].strip()
        if not raw_entry:
            continue
        candidates = (
            [part.strip() for part in raw_entry.split(" -> ", 1)]
            if " -> " in raw_entry
            else [raw_entry]
        )
        for item in candidates:
            cleaned = item.strip('"').replace("\\", "/")
            while cleaned.startswith("./"):
                cleaned = cleaned[2:]
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                paths.append(cleaned)
    return paths


class ParallelImprover:
    """Executes disjoint ParallelSubtask workers concurrently inside .liwi-staging (max 3 workers)."""

    def __init__(
        self,
        worktree_path: Path | str,
        *,
        max_workers: int = MAX_PARALLEL_WORKERS,
        runner: Runner | None = None,
        agy_command: Sequence[str] = ("agy",),
        model: str | None = None,
        secrets_to_redact: Sequence[str] = (),
        worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
        verify_timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        worker_runner: WorkerRunner | None = None,
        ruff_runner: RuffRunner | None = None,
        pytest_runner: PytestRunner | None = None,
        changed_files_inspector: ChangedFilesInspector | None = None,
    ) -> None:
        self.worktree_path: Path = Path(worktree_path).resolve()
        self.max_workers: int = max(1, min(MAX_PARALLEL_WORKERS, int(max_workers)))
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=worker_timeout_seconds)
        )
        self.agy_command: tuple[str, ...] = tuple(agy_command) if agy_command else ("agy",)
        self.model: str | None = model.strip() if isinstance(model, str) and model.strip() else None
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self.worker_timeout_seconds: float = float(worker_timeout_seconds)
        self.verify_timeout_seconds: float = float(verify_timeout_seconds)

        self._worker_runner: WorkerRunner = (
            worker_runner if worker_runner is not None else self._default_worker_runner
        )
        self._ruff_runner: RuffRunner = (
            ruff_runner if ruff_runner is not None else self._default_ruff_runner
        )
        self._pytest_runner: PytestRunner = (
            pytest_runner if pytest_runner is not None else self._default_pytest_runner
        )
        self._changed_files_inspector: ChangedFilesInspector = (
            changed_files_inspector
            if changed_files_inspector is not None
            else self._default_changed_files_inspector
        )

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=self.secrets_to_redact)

    def _resolve_venv_tool(self, tool_name: str, cwd: Path) -> list[str]:
        if isinstance(self.runner, SubprocessRunner):
            ext = ".exe" if os.name == "nt" else ""
            scripts_dir = "Scripts" if os.name == "nt" else "bin"
            for base_dir in (
                cwd / ".venv" / scripts_dir,
                cwd.parent / ".venv" / scripts_dir,
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

    def _default_worker_runner(
        self,
        _subtask: ParallelSubtask,
        prompt: str,
        cwd: Path,
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
                    prefix="af_liwi_par_",
                    delete=False,
                ) as tf:
                    tf.write(prompt)
                    tmp_prompt = Path(tf.name).resolve()
                prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

            cmd = [
                *base_cmd,
                "-p",
                prompt_arg,
                "--add-dir",
                str(cwd),
                "--dangerously-skip-permissions",
            ]
            if is_real_agy:
                cmd.extend(["--mode", "auto"])
            if self.model:
                cmd.extend(["--model", self.model])

            res = self.runner.run(
                cmd,
                cwd=cwd,
                timeout_seconds=self.worker_timeout_seconds,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return False, self._redact(str(exc) or type(exc).__name__)
        finally:
            if tmp_prompt is not None:
                tmp_prompt.unlink(missing_ok=True)

        if res.exit_code != 0:
            raw = (res.stderr or res.stdout or f"exit code {res.exit_code}").strip()
            return False, self._redact(raw)
        return True, self._redact(res.stdout.strip())

    def _default_ruff_runner(self, files: Sequence[str], cwd: Path) -> tuple[bool, str]:
        py_files = [f for f in files if f.lower().endswith(".py")]
        if not py_files:
            return True, ""
        cmd = [*self._resolve_venv_tool("ruff", cwd), "check", *py_files]
        try:
            res = self.runner.run(
                cmd,
                cwd=cwd,
                timeout_seconds=self.verify_timeout_seconds,
                env=build_workspace_pytest_env(),
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return False, self._redact(str(exc) or type(exc).__name__)
        output = self._redact((res.stdout or res.stderr or "").strip())
        return res.exit_code == 0, output

    def _default_pytest_runner(self, target_tests: Sequence[str], cwd: Path) -> tuple[bool, str]:
        cmd = [*self._resolve_venv_tool("pytest", cwd), "-q", "--no-cov", *target_tests]
        try:
            res = self.runner.run(
                cmd,
                cwd=cwd,
                timeout_seconds=self.verify_timeout_seconds,
                env=build_workspace_pytest_env(),
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return False, self._redact(str(exc) or type(exc).__name__)
        output = self._redact((res.stdout or res.stderr or "").strip())
        return res.exit_code == 0, output

    def _default_changed_files_inspector(self, cwd: Path) -> Sequence[str]:
        if not (cwd / ".git").exists():
            return ()
        clean_env = build_sanitized_cli_env()
        try:
            res = self.runner.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=cwd,
                timeout_seconds=15.0,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError):
            return ()
        if res.exit_code != 0 or not res.stdout:
            return ()
        return _parse_porcelain_paths(res.stdout)

    def _infer_target_tests(
        self,
        plan: ParallelPlan,
        validated_subtasks: Sequence[ParallelSubtask],
    ) -> tuple[str, ...]:
        if plan.target_tests:
            return tuple(normalize_relative_path(t) for t in plan.target_tests)
        all_assigned = [f for st in validated_subtasks for f in st.assigned_files]
        explicit_tests = _extract_test_files(all_assigned)
        if explicit_tests:
            return explicit_tests

        tests_dir = self.worktree_path / "tests"
        inferred: list[str] = []
        seen: set[str] = set()
        if tests_dir.is_dir():
            for rel in all_assigned:
                if rel.startswith("src/") and rel.endswith(".py"):
                    stem = PurePosixPath(rel).stem
                    if not stem or stem == "__init__":
                        continue
                    for match in sorted(tests_dir.glob(f"test_*{stem}*.py")):
                        cand = f"tests/{match.name}"
                        key = _canonical_path_key(cand)
                        if key not in seen:
                            seen.add(key)
                            inferred.append(cand)
        return tuple(inferred)

    def _execute_worker(self, plan: ParallelPlan, subtask: ParallelSubtask) -> WorkerResult:
        started = time.monotonic()
        prompt = build_worker_prompt(plan, subtask)
        try:
            worker_ok, worker_out = self._worker_runner(subtask, prompt, self.worktree_path)
        except Exception as exc:  # noqa: BLE001
            err = self._redact(f"{subtask.subtask_id} error: {exc}")
            return WorkerResult(
                subtask_id=subtask.subtask_id,
                role=subtask.role,
                assigned_files=subtask.assigned_files,
                success=False,
                output="",
                error=err,
                ruff_ok=False,
                ruff_output="",
                duration_seconds=time.monotonic() - started,
            )

        clean_out = self._redact(str(worker_out or "").strip())
        if not worker_ok:
            return WorkerResult(
                subtask_id=subtask.subtask_id,
                role=subtask.role,
                assigned_files=subtask.assigned_files,
                success=False,
                output=clean_out,
                error=clean_out or f"Worker '{subtask.subtask_id}' falló.",
                ruff_ok=False,
                ruff_output="",
                duration_seconds=time.monotonic() - started,
            )

        try:
            ruff_ok, ruff_out = self._ruff_runner(list(subtask.assigned_files), self.worktree_path)
        except Exception as exc:  # noqa: BLE001
            err = self._redact(f"ruff check falló en {subtask.subtask_id}: {exc}")
            return WorkerResult(
                subtask_id=subtask.subtask_id,
                role=subtask.role,
                assigned_files=subtask.assigned_files,
                success=False,
                output=clean_out,
                error=err,
                ruff_ok=False,
                ruff_output=err,
                duration_seconds=time.monotonic() - started,
            )

        clean_ruff = self._redact(str(ruff_out or "").strip())
        if not ruff_ok:
            return WorkerResult(
                subtask_id=subtask.subtask_id,
                role=subtask.role,
                assigned_files=subtask.assigned_files,
                success=False,
                output=clean_out,
                error=f"Falló ruff check en {subtask.subtask_id}: {clean_ruff or 'lint error'}",
                ruff_ok=False,
                ruff_output=clean_ruff,
                duration_seconds=time.monotonic() - started,
            )

        return WorkerResult(
            subtask_id=subtask.subtask_id,
            role=subtask.role,
            assigned_files=subtask.assigned_files,
            success=True,
            output=clean_out,
            error="",
            ruff_ok=True,
            ruff_output=clean_ruff,
            duration_seconds=time.monotonic() - started,
        )

    def run(self, plan: ParallelPlan) -> ParallelRunResult:
        """Execute all disjoint subtasks (<=3 concurrent workers), verify partitions, and run targeted pytest once."""
        validated_subtasks = validate_disjoint_subtasks(plan.subtasks)
        effective_target_tests = self._infer_target_tests(plan, validated_subtasks)
        normalized_plan = ParallelPlan(
            title=plan.title,
            subtasks=validated_subtasks,
            contracts=plan.contracts,
            target_tests=effective_target_tests,
        )

        baseline_keys: set[str] = set()
        for raw_before in self._changed_files_inspector(self.worktree_path):
            cleaned_before = str(raw_before).strip().replace("\\", "/")
            if cleaned_before:
                baseline_keys.add(_canonical_path_key(cleaned_before))

        if len(validated_subtasks) == 1:
            worker_results = (self._execute_worker(normalized_plan, validated_subtasks[0]),)
        else:
            pool_size = max(1, min(self.max_workers, MAX_PARALLEL_WORKERS, len(validated_subtasks)))
            with ThreadPoolExecutor(max_workers=pool_size) as pool:
                futures: list[Future[WorkerResult]] = [
                    pool.submit(self._execute_worker, normalized_plan, subtask)
                    for subtask in validated_subtasks
                ]
                worker_results = tuple(fut.result() for fut in futures)

        failed_workers = [wr for wr in worker_results if not wr.success]
        if failed_workers:
            combined_err = self._redact(
                "; ".join(wr.error or f"Worker '{wr.subtask_id}' falló" for wr in failed_workers)
            )
            return ParallelRunResult(
                success=False,
                plan=normalized_plan,
                worker_results=worker_results,
                pytest_ran=False,
                pytest_ok=False,
                pytest_output="",
                target_tests=effective_target_tests,
                out_of_partition_files=(),
                error=combined_err,
                commit_allowed=False,
                commit_sha=None,
            )

        allowed_keys = {
            _canonical_path_key(rel)
            for st in validated_subtasks
            for rel in st.assigned_files
        }
        out_of_partition: list[str] = []
        seen_oop: set[str] = set()
        for raw_after in self._changed_files_inspector(self.worktree_path):
            raw_clean = str(raw_after).strip().replace("\\", "/")
            if not raw_clean:
                continue
            raw_key = _canonical_path_key(raw_clean)
            if raw_key in baseline_keys:
                continue
            try:
                norm_after = normalize_relative_path(raw_clean)
            except ValueError:
                if raw_key not in seen_oop:
                    seen_oop.add(raw_key)
                    out_of_partition.append(raw_clean)
                continue
            norm_key = _canonical_path_key(norm_after)
            if norm_key not in allowed_keys and norm_key not in seen_oop:
                seen_oop.add(norm_key)
                out_of_partition.append(norm_after)

        if out_of_partition:
            err_msg = self._redact(
                "Conflicto de partición: archivos modificados fuera de la partición asignada: "
                + ", ".join(out_of_partition)
            )
            return ParallelRunResult(
                success=False,
                plan=normalized_plan,
                worker_results=worker_results,
                pytest_ran=False,
                pytest_ok=False,
                pytest_output="",
                target_tests=effective_target_tests,
                out_of_partition_files=tuple(out_of_partition),
                error=err_msg,
                commit_allowed=False,
                commit_sha=None,
            )

        try:
            pytest_ok, pytest_out = self._pytest_runner(
                list(effective_target_tests),
                self.worktree_path,
            )
        except Exception as exc:  # noqa: BLE001
            err_msg = self._redact(f"Falló pytest dirigido: {exc}")
            return ParallelRunResult(
                success=False,
                plan=normalized_plan,
                worker_results=worker_results,
                pytest_ran=True,
                pytest_ok=False,
                pytest_output=err_msg,
                target_tests=effective_target_tests,
                out_of_partition_files=(),
                error=err_msg,
                commit_allowed=False,
                commit_sha=None,
            )

        clean_pytest_out = self._redact(str(pytest_out or "").strip())
        if not pytest_ok:
            return ParallelRunResult(
                success=False,
                plan=normalized_plan,
                worker_results=worker_results,
                pytest_ran=True,
                pytest_ok=False,
                pytest_output=clean_pytest_out,
                target_tests=effective_target_tests,
                out_of_partition_files=(),
                error=f"Falló pytest dirigido: {clean_pytest_out or 'pytest failed'}",
                commit_allowed=False,
                commit_sha=None,
            )

        return ParallelRunResult(
            success=True,
            plan=normalized_plan,
            worker_results=worker_results,
            pytest_ran=True,
            pytest_ok=True,
            pytest_output=clean_pytest_out,
            target_tests=effective_target_tests,
            out_of_partition_files=(),
            error="",
            commit_allowed=True,
            commit_sha=None,
        )
