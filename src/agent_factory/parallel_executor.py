"""Concurrent multi-agent git worktree executor with disjoint file ownership (INV-06, INV-19, INV-22)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import FileSpec
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

MAX_PARALLEL_AGENTS: int = 3

ProgressStatus = Literal["pending", "running", "done", "failed"]
_VALID_PROGRESS_STATUSES: frozenset[str] = frozenset({"pending", "running", "done", "failed"})

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
_CONFLICT_MARKER_RE: re.Pattern[str] = re.compile(
    r"^(?:<<<<<<<|=======|>>>>>>>)(?:\s|$)",
    re.MULTILINE,
)

_GIT_AUTHOR_ARGS: tuple[str, ...] = (
    "-c",
    "user.name=AgentFactory",
    "-c",
    "user.email=agent@factory.local",
)


@dataclass(frozen=True)
class ParallelSubtask:
    """Disjoint unit of work assigned to a parallel sub-agent."""

    task_id: str
    label: str
    owned_files: tuple[str, ...]
    instructions: str = ""
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("ParallelSubtask.task_id must be a non-empty string")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("ParallelSubtask.label must be a non-empty string")
        object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(self, "owned_files", tuple(self.owned_files))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True)
class SubtaskProgress:
    """Immutable progress snapshot for a single parallel sub-agent."""

    agent_index: int
    total_agents: int
    task_id: str
    label: str
    owned_files: tuple[str, ...]
    status: ProgressStatus
    detail: str = ""

    def __post_init__(self) -> None:
        if self.agent_index < 1 or self.total_agents < 1 or self.agent_index > self.total_agents:
            raise ValueError(
                f"Invalid agent index {self.agent_index}/{self.total_agents}"
            )
        if self.status not in _VALID_PROGRESS_STATUSES:
            raise ValueError(f"Invalid progress status '{self.status}'")
        object.__setattr__(self, "owned_files", tuple(self.owned_files))

    @property
    def agent_tag(self) -> str:
        """Return compact human-readable agent identifier (e.g. 'Agente 1/3')."""
        return f"Agente {self.agent_index}/{self.total_agents}"

    def format_line(self) -> str:
        """Format progress line as 'Agente 1/3: <label> (<status>)' with optional detail."""
        base = f"{self.agent_tag}: {self.label} ({self.status})"
        clean_detail = self.detail.strip()
        return f"{base} - {clean_detail}" if clean_detail else base

    @property
    def formatted(self) -> str:
        """Alias for format_line()."""
        return self.format_line()

    def __str__(self) -> str:
        return self.format_line()


@dataclass(frozen=True)
class SubtaskExecutionResult:
    """Outcome of running a single subtask in its isolated git worktree."""

    task_id: str
    label: str
    branch_name: str
    owned_files: tuple[str, ...]
    modified_files: list[str]
    succeeded: bool
    error: str = ""
    summary: str = ""
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "owned_files", tuple(self.owned_files))
        object.__setattr__(self, "modified_files", list(self.modified_files))


@dataclass(frozen=True)
class ParallelExecutionReport:
    """Aggregate report of a parallel worktree execution and merge cycle."""

    succeeded: bool
    subtasks: list[SubtaskExecutionResult]
    merged_branches: list[str]
    conflict_files: list[str]
    integration_attempted: bool
    integration_succeeded: bool
    error: str = ""


def format_progress_summary(progress_items: Sequence[SubtaskProgress]) -> str:
    """Format a multi-line human-readable progress summary across all active sub-agents."""
    return "\n".join(item.format_line() for item in progress_items)


def _normalize_forbidden_prefix(prefix: str) -> str:
    """Normalize a forbidden path prefix into clean lowercase POSIX form."""
    raw = str(prefix or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.strip("/")
    return raw.lower()


def _matches_forbidden_prefix(normalized_path: str, forbidden_prefixes: Sequence[str]) -> str | None:
    """Return the first matching forbidden prefix if normalized_path falls under it, else None."""
    norm_low = normalized_path.lower()
    for raw_pref in forbidden_prefixes:
        pref = _normalize_forbidden_prefix(raw_pref)
        if not pref:
            continue
        if norm_low == pref or norm_low.startswith(pref + "/"):
            return raw_pref
    return None


def normalize_owned_path(path: str) -> str:
    """Validate and normalize a repository-relative POSIX path (INV-19, INV-22).

    Rejects empty paths, null/newline chars, backslashes, absolute paths, drive letters,
    parent traversal ('..'), forbidden directories (.git/, .venv/, venv/, node_modules/),
    and .env or secret credential files.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Owned file path must be a non-empty string")
    raw = path.strip()
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise ValueError(f"Invalid control character in owned path: {path!r}")
    if "\\" in raw:
        raise ValueError(f"Backslashes are forbidden in owned path (INV-22): {path!r}")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError(f"Absolute path or drive letter rejected (INV-22): {path!r}")

    win_check = PureWindowsPath(raw)
    posix_check = PurePosixPath(raw)
    if win_check.is_absolute() or posix_check.is_absolute():
        raise ValueError(f"Absolute path rejected (INV-22): {path!r}")

    raw_segments = raw.split("/")
    cleaned_parts: list[str] = []
    for seg in raw_segments:
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            raise ValueError(f"Path traversal ('..') rejected (INV-22): {path!r}")
        cleaned_parts.append(seg)

    if not cleaned_parts:
        raise ValueError(f"Owned file path resolves to repository root: {path!r}")

    for seg in cleaned_parts:
        if seg.lower() in _FORBIDDEN_DIR_SEGMENTS:
            raise ValueError(f"Forbidden directory segment '{seg}' in path (INV-22): {path!r}")

    filename_low = cleaned_parts[-1].lower()
    if (
        filename_low in _SECRET_FILENAMES
        or filename_low.startswith(".env.")
        or any(filename_low.endswith(ext) for ext in _SECRET_EXTENSIONS)
    ):
        raise ValueError(f"Secret or .env file path rejected (INV-19, INV-22): {path!r}")

    return "/".join(cleaned_parts)


def validate_disjoint_subtasks(
    subtasks: Sequence[ParallelSubtask],
    forbidden_prefixes: Sequence[str] = (),
) -> list[ParallelSubtask]:
    """Validate that subtasks have non-empty, safe, non-overlapping owned_files."""
    validated: list[ParallelSubtask] = []
    seen_task_ids: set[str] = set()
    claimed_files: dict[str, str] = {}

    for subtask in subtasks:
        if subtask.task_id in seen_task_ids:
            raise ValueError(f"Duplicate subtask task_id '{subtask.task_id}'")
        seen_task_ids.add(subtask.task_id)

        if not subtask.owned_files:
            raise ValueError(f"Subtask '{subtask.task_id}' must declare at least one owned file")

        normalized_owned: list[str] = []
        seen_in_subtask: set[str] = set()
        for raw_file in subtask.owned_files:
            norm = normalize_owned_path(raw_file)
            matched_pref = _matches_forbidden_prefix(norm, forbidden_prefixes)
            if matched_pref is not None:
                raise ValueError(
                    f"Path '{norm}' in subtask '{subtask.task_id}' violates forbidden prefix "
                    f"'{matched_pref}' (INV-19, INV-22)"
                )
            norm_key = norm.lower()
            if norm_key in claimed_files:
                owner = claimed_files[norm_key]
                raise ValueError(
                    f"Overlapping owned file '{norm}' between subtasks "
                    f"'{owner}' and '{subtask.task_id}'"
                )
            if norm_key not in seen_in_subtask:
                seen_in_subtask.add(norm_key)
                normalized_owned.append(norm)

        for norm in normalized_owned:
            claimed_files[norm.lower()] = subtask.task_id

        validated.append(
            ParallelSubtask(
                task_id=subtask.task_id,
                label=subtask.label,
                owned_files=tuple(normalized_owned),
                instructions=subtask.instructions,
                depends_on=subtask.depends_on,
            )
        )

    return validated


def _try_normalize_dependency_path(raw_dep: str) -> str | None:
    """Attempt to normalize a dependency string as a relative path; return None if not a file path."""
    cleaned = str(raw_dep or "").strip().replace("\\", "/")
    if not cleaned:
        return None
    try:
        return normalize_owned_path(cleaned)
    except ValueError:
        return None


def _build_subtask_label(group_index: int, specs: Sequence[FileSpec]) -> str:
    """Generate a concise human-readable label for a partitioned group of FileSpecs."""
    if not specs:
        return f"Grupo {group_index}"
    if len(specs) == 1:
        return specs[0].path
    first_path = specs[0].path
    return f"{first_path} (+{len(specs) - 1})"


def _build_subtask_instructions(specs: Sequence[FileSpec]) -> str:
    """Generate concise deterministic file-contract instructions for a subtask."""
    lines: list[str] = []
    for spec in specs:
        deps = f" | depends_on: {', '.join(spec.depends_on)}" if spec.depends_on else ""
        lines.append(f"- {spec.path}: {spec.purpose} | contract: {spec.contract}{deps}")
    return "\n".join(lines)


def partition_plan_files(
    files: Sequence[FileSpec],
    *,
    max_workers: int = MAX_PARALLEL_AGENTS,
) -> list[ParallelSubtask]:
    """Group FileSpecs by dependency connected components into <= MAX_PARALLEL_AGENTS disjoint subtasks.

    Files connected directly or transitively via `FileSpec.depends_on` are placed into the same
    connected component to prevent merge conflicts. Independent components are load-balanced across
    at most `min(max_workers, MAX_PARALLEL_AGENTS)` strictly disjoint `ParallelSubtask` instances.
    Returns 0 or 1 subtask when parallelization is unnecessary, allowing clean sequential fallback.
    """
    if not files:
        return []

    worker_cap = max(1, min(MAX_PARALLEL_AGENTS, int(max_workers)))

    # Normalize and deduplicate FileSpecs deterministically by normalized path
    ordered_specs: list[FileSpec] = []
    path_to_idx: dict[str, int] = {}
    for spec in files:
        norm_path = normalize_owned_path(spec.path)
        key = norm_path.lower()
        if key in path_to_idx:
            continue
        path_to_idx[key] = len(ordered_specs)
        ordered_specs.append(
            FileSpec(
                path=norm_path,
                purpose=spec.purpose,
                contract=spec.contract,
                depends_on=list(spec.depends_on),
            )
        )

    n = len(ordered_specs)
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for idx, spec in enumerate(ordered_specs):
        for dep in spec.depends_on:
            norm_dep = _try_normalize_dependency_path(dep)
            if norm_dep is None:
                continue
            dep_idx = path_to_idx.get(norm_dep.lower())
            if dep_idx is not None:
                _union(idx, dep_idx)

    components_by_root: dict[int, list[int]] = {}
    for idx in range(n):
        root = _find(idx)
        components_by_root.setdefault(root, []).append(idx)

    components: list[list[int]] = [
        components_by_root[root] for root in sorted(components_by_root)
    ]

    if len(components) <= 1 or worker_cap == 1:
        all_specs = ordered_specs
        all_owned = tuple(s.path for s in all_specs)
        owned_lower = {p.lower() for p in all_owned}
        ext_deps: list[str] = []
        for spec in all_specs:
            for dep in spec.depends_on:
                norm_dep = _try_normalize_dependency_path(dep)
                key = norm_dep.lower() if norm_dep else dep.strip().lower()
                if dep.strip() and key not in owned_lower and dep.strip() not in ext_deps:
                    ext_deps.append(dep.strip())
        return [
            ParallelSubtask(
                task_id="subtask-1",
                label=_build_subtask_label(1, all_specs),
                owned_files=all_owned,
                instructions=_build_subtask_instructions(all_specs),
                depends_on=tuple(ext_deps),
            )
        ]

    num_buckets = min(worker_cap, len(components))
    buckets: list[list[int]] = [[] for _ in range(num_buckets)]

    # Greedy LPT load balancing: assign largest connected components first
    for comp in sorted(components, key=lambda c: (-len(c), c[0])):
        target_bucket = min(range(num_buckets), key=lambda b: (len(buckets[b]), b))
        buckets[target_bucket].extend(comp)

    # Sort file indices within each bucket and order buckets deterministically by earliest file
    non_empty_buckets = [sorted(b) for b in buckets if b]
    non_empty_buckets.sort(key=lambda b: b[0])

    subtasks: list[ParallelSubtask] = []
    for bucket_idx, file_indices in enumerate(non_empty_buckets, start=1):
        bucket_specs = [ordered_specs[i] for i in file_indices]
        owned = tuple(s.path for s in bucket_specs)
        owned_lower = {p.lower() for p in owned}
        ext_deps = []
        for spec in bucket_specs:
            for dep in spec.depends_on:
                norm_dep = _try_normalize_dependency_path(dep)
                key = norm_dep.lower() if norm_dep else dep.strip().lower()
                if dep.strip() and key not in owned_lower and dep.strip() not in ext_deps:
                    ext_deps.append(dep.strip())
        subtasks.append(
            ParallelSubtask(
                task_id=f"subtask-{bucket_idx}",
                label=_build_subtask_label(bucket_idx, bucket_specs),
                owned_files=owned,
                instructions=_build_subtask_instructions(bucket_specs),
                depends_on=tuple(ext_deps),
            )
        )

    return validate_disjoint_subtasks(subtasks)


def _safe_rmtree(path: Path) -> None:
    """Remove a directory tree cleanly on POSIX and Windows (handling read-only git objects)."""
    if not path.exists():
        return

    def _handle_readonly(func: object, target: str, _exc: object) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        if callable(func):
            try:
                func(target)
            except OSError:
                pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_handle_readonly)
        else:
            shutil.rmtree(path, onerror=_handle_readonly)
    except OSError:
        pass


def _parse_porcelain_paths(stdout: str) -> list[str]:
    """Extract all touched repository-relative POSIX paths from `git status --porcelain -uall`."""
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


def _has_conflict_markers(repo_root: Path, rel_paths: Sequence[str]) -> list[str]:
    """Return subset of rel_paths that still contain unresolved git conflict markers on disk."""
    still_marked: list[str] = []
    for rel in rel_paths:
        target = repo_root / rel
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _CONFLICT_MARKER_RE.search(content):
            still_marked.append(rel)
    return still_marked


class ParallelWorktreeExecutor:
    """Executes disjoint subtasks concurrently in isolated git worktrees and merges results cleanly."""

    def __init__(
        self,
        repo_root: Path,
        runner: Runner | None = None,
        *,
        max_workers: int = MAX_PARALLEL_AGENTS,
        forbidden_prefixes: Sequence[str] = (),
        secrets_to_redact: Sequence[str] = (),
    ) -> None:
        self.repo_root: Path = Path(repo_root).resolve()
        self.runner: Runner = (
            runner if runner is not None else SubprocessRunner(default_timeout_seconds=120.0)
        )
        self.max_workers: int = max(1, min(MAX_PARALLEL_AGENTS, int(max_workers)))
        self.forbidden_prefixes: tuple[str, ...] = tuple(
            str(p).strip() for p in forbidden_prefixes if str(p).strip()
        )
        self.secrets_to_redact: tuple[str, ...] = tuple(
            str(s) for s in secrets_to_redact if str(s).strip()
        )

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=self.secrets_to_redact)

    def _git_env(self) -> dict[str, str]:
        env = build_sanitized_cli_env()
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float = 60.0,
    ) -> CommandResult:
        try:
            return self.runner.run(
                ["git", *args],
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return CommandResult(exit_code=1, stdout="", stderr=self._redact(str(exc)))

    def _get_unmerged_files(self, env: dict[str, str]) -> list[str]:
        res = self._run_git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=self.repo_root,
            env=env,
        )
        if res.exit_code != 0 or not res.stdout:
            return []
        files: list[str] = []
        for line in res.stdout.splitlines():
            clean = line.strip().strip('"').replace("\\", "/")
            if clean and clean not in files:
                files.append(clean)
        return files

    def _is_merge_in_progress(self, env: dict[str, str]) -> bool:
        res = self._run_git(
            ["rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=self.repo_root,
            env=env,
        )
        return res.exit_code == 0

    def _abort_merge_best_effort(self, env: dict[str, str]) -> None:
        if self._is_merge_in_progress(env):
            self._run_git(["merge", "--abort"], cwd=self.repo_root, env=env)

    def _cleanup_worktrees_and_branches(
        self,
        worktrees: Sequence[Path],
        branches: Sequence[str],
        wt_base: Path | None,
        env: dict[str, str],
    ) -> None:
        for wt_path in worktrees:
            self._run_git(
                ["worktree", "remove", "--force", str(wt_path)],
                cwd=self.repo_root,
                env=env,
            )
            _safe_rmtree(wt_path)

        if wt_base is not None:
            _safe_rmtree(wt_base)

        self._run_git(["worktree", "prune"], cwd=self.repo_root, env=env)

        for branch_name in branches:
            self._run_git(
                ["branch", "-D", branch_name],
                cwd=self.repo_root,
                env=env,
            )

    def execute(
        self,
        subtasks: Sequence[ParallelSubtask],
        worker_fn: Callable[[ParallelSubtask, Path, str], tuple[bool, str]],
        *,
        conflict_resolver_fn: Callable[[Path, Sequence[str], str], tuple[bool, str]] | None = None,
        on_progress: Callable[[list[SubtaskProgress], str], None] | None = None,
    ) -> ParallelExecutionReport:
        """Execute subtasks concurrently in git worktrees, enforce ownership, and merge cleanly."""
        validated = validate_disjoint_subtasks(
            subtasks,
            forbidden_prefixes=self.forbidden_prefixes,
        )
        if not validated:
            return ParallelExecutionReport(
                succeeded=True,
                subtasks=[],
                merged_branches=[],
                conflict_files=[],
                integration_attempted=False,
                integration_succeeded=False,
                error="",
            )

        git_env = self._git_env()
        run_id = uuid.uuid4().hex[:8]
        total = len(validated)
        wt_base: Path | None = None
        created_worktrees: list[Path] = []
        created_branches: list[str] = []

        progress_lock = threading.Lock()
        callback_lock = threading.Lock()
        progress_state: list[SubtaskProgress] = [
            SubtaskProgress(
                agent_index=idx,
                total_agents=total,
                task_id=st.task_id,
                label=st.label,
                owned_files=st.owned_files,
                status="pending",
            )
            for idx, st in enumerate(validated, start=1)
        ]

        def _emit_progress(idx_zero: int, status: ProgressStatus, detail: str = "") -> None:
            safe_detail = self._redact(detail)
            with progress_lock:
                cur = progress_state[idx_zero]
                progress_state[idx_zero] = SubtaskProgress(
                    agent_index=cur.agent_index,
                    total_agents=cur.total_agents,
                    task_id=cur.task_id,
                    label=cur.label,
                    owned_files=cur.owned_files,
                    status=status,
                    detail=safe_detail,
                )
                snapshot = list(progress_state)
                summary = format_progress_summary(snapshot)
            if on_progress is not None:
                with callback_lock:
                    on_progress(snapshot, summary)

        try:
            wt_base = Path(tempfile.mkdtemp(prefix="af_wt_")).resolve()

            # 1. Create isolated worktree + temporary branch for each subtask
            worktree_specs: list[tuple[int, ParallelSubtask, Path, str]] = []
            for idx_zero, subtask in enumerate(validated):
                idx_one = idx_zero + 1
                branch_name = f"af-par-{run_id}-{idx_one}"
                wt_path = wt_base / f"agent-{idx_one}"
                add_res = self._run_git(
                    ["worktree", "add", "-b", branch_name, str(wt_path), "HEAD"],
                    cwd=self.repo_root,
                    env=git_env,
                )
                if add_res.exit_code != 0:
                    err_msg = self._redact(
                        f"Failed to create git worktree for {subtask.task_id}: "
                        f"{(add_res.stderr or add_res.stdout).strip()}"
                    )
                    return ParallelExecutionReport(
                        succeeded=False,
                        subtasks=[],
                        merged_branches=[],
                        conflict_files=[],
                        integration_attempted=False,
                        integration_succeeded=False,
                        error=err_msg,
                    )
                created_worktrees.append(wt_path)
                created_branches.append(branch_name)
                worktree_specs.append((idx_zero, subtask, wt_path, branch_name))

            # 2. Run all subtasks concurrently in their isolated worktrees
            def _run_subtask(
                idx_zero: int,
                subtask: ParallelSubtask,
                wt_path: Path,
                branch_name: str,
            ) -> SubtaskExecutionResult:
                agent_tag = f"Agente {idx_zero + 1}/{total}"
                started = time.monotonic()
                _emit_progress(idx_zero, "running", "En ejecución")

                try:
                    worker_ok, worker_msg = worker_fn(subtask, wt_path, agent_tag)
                except Exception as exc:  # noqa: BLE001
                    err_msg = self._redact(f"{agent_tag} ({subtask.task_id}) error: {exc}")
                    _emit_progress(idx_zero, "failed", err_msg[:160])
                    return SubtaskExecutionResult(
                        task_id=subtask.task_id,
                        label=subtask.label,
                        branch_name=branch_name,
                        owned_files=subtask.owned_files,
                        modified_files=[],
                        succeeded=False,
                        error=err_msg,
                        summary="",
                        duration_seconds=time.monotonic() - started,
                    )

                safe_summary = self._redact(str(worker_msg or "").strip())

                # Inspect modified/untracked files inside this worktree
                status_res = self._run_git(
                    ["status", "--porcelain", "-uall"],
                    cwd=wt_path,
                    env=git_env,
                )
                if status_res.exit_code != 0:
                    err_msg = self._redact(
                        f"{agent_tag} ({subtask.task_id}) git status failed: "
                        f"{(status_res.stderr or status_res.stdout).strip()}"
                    )
                    _emit_progress(idx_zero, "failed", err_msg[:160])
                    return SubtaskExecutionResult(
                        task_id=subtask.task_id,
                        label=subtask.label,
                        branch_name=branch_name,
                        owned_files=subtask.owned_files,
                        modified_files=[],
                        succeeded=False,
                        error=err_msg,
                        summary=safe_summary,
                        duration_seconds=time.monotonic() - started,
                    )

                modified_files = _parse_porcelain_paths(status_res.stdout)

                # Enforce strict file ownership and forbidden prefixes
                allowed_lower = {p.lower() for p in subtask.owned_files}
                violations: list[str] = []
                for touched in modified_files:
                    try:
                        norm_touched = normalize_owned_path(touched)
                    except ValueError:
                        violations.append(f"{touched} (unsafe/protected path)")
                        continue
                    matched_pref = _matches_forbidden_prefix(norm_touched, self.forbidden_prefixes)
                    if matched_pref is not None:
                        violations.append(f"{norm_touched} (forbidden prefix '{matched_pref}')")
                        continue
                    if norm_touched.lower() not in allowed_lower:
                        violations.append(f"{norm_touched} (outside owned_files)")

                if violations:
                    err_msg = self._redact(
                        f"{agent_tag} ({subtask.task_id}) ownership violation: "
                        + ", ".join(violations)
                    )
                    _emit_progress(idx_zero, "failed", err_msg[:160])
                    return SubtaskExecutionResult(
                        task_id=subtask.task_id,
                        label=subtask.label,
                        branch_name=branch_name,
                        owned_files=subtask.owned_files,
                        modified_files=modified_files,
                        succeeded=False,
                        error=err_msg,
                        summary=safe_summary,
                        duration_seconds=time.monotonic() - started,
                    )

                if not worker_ok:
                    err_msg = self._redact(
                        safe_summary or f"{agent_tag} ({subtask.task_id}) reported failure"
                    )
                    _emit_progress(idx_zero, "failed", err_msg[:160])
                    return SubtaskExecutionResult(
                        task_id=subtask.task_id,
                        label=subtask.label,
                        branch_name=branch_name,
                        owned_files=subtask.owned_files,
                        modified_files=modified_files,
                        succeeded=False,
                        error=err_msg,
                        summary=safe_summary,
                        duration_seconds=time.monotonic() - started,
                    )

                if modified_files:
                    add_res = self._run_git(["add", "-A"], cwd=wt_path, env=git_env)
                    if add_res.exit_code != 0:
                        err_msg = self._redact(
                            f"{agent_tag} ({subtask.task_id}) git add failed: "
                            f"{(add_res.stderr or add_res.stdout).strip()}"
                        )
                        _emit_progress(idx_zero, "failed", err_msg[:160])
                        return SubtaskExecutionResult(
                            task_id=subtask.task_id,
                            label=subtask.label,
                            branch_name=branch_name,
                            owned_files=subtask.owned_files,
                            modified_files=modified_files,
                            succeeded=False,
                            error=err_msg,
                            summary=safe_summary,
                            duration_seconds=time.monotonic() - started,
                        )

                    commit_res = self._run_git(
                        [
                            *_GIT_AUTHOR_ARGS,
                            "commit",
                            "--no-verify",
                            "-m",
                            f"chore(parallel): {subtask.task_id}",
                        ],
                        cwd=wt_path,
                        env=git_env,
                    )
                    if commit_res.exit_code != 0:
                        err_msg = self._redact(
                            f"{agent_tag} ({subtask.task_id}) git commit failed: "
                            f"{(commit_res.stderr or commit_res.stdout).strip()}"
                        )
                        _emit_progress(idx_zero, "failed", err_msg[:160])
                        return SubtaskExecutionResult(
                            task_id=subtask.task_id,
                            label=subtask.label,
                            branch_name=branch_name,
                            owned_files=subtask.owned_files,
                            modified_files=modified_files,
                            succeeded=False,
                            error=err_msg,
                            summary=safe_summary,
                            duration_seconds=time.monotonic() - started,
                        )

                _emit_progress(idx_zero, "done", safe_summary[:160])
                return SubtaskExecutionResult(
                    task_id=subtask.task_id,
                    label=subtask.label,
                    branch_name=branch_name,
                    owned_files=subtask.owned_files,
                    modified_files=modified_files,
                    succeeded=True,
                    error="",
                    summary=safe_summary,
                    duration_seconds=time.monotonic() - started,
                )

            pool_size = min(self.max_workers, total)
            futures: list[Future[SubtaskExecutionResult]] = []
            with ThreadPoolExecutor(max_workers=pool_size) as pool:
                for idx_zero, subtask, wt_path, branch_name in worktree_specs:
                    futures.append(
                        pool.submit(_run_subtask, idx_zero, subtask, wt_path, branch_name)
                    )
                results = [fut.result() for fut in futures]

            # 3. Abort before merging if any subtask failed
            failed_results = [r for r in results if not r.succeeded]
            if failed_results:
                combined_err = self._redact(
                    "; ".join(r.error or f"{r.task_id} failed" for r in failed_results)
                )
                return ParallelExecutionReport(
                    succeeded=False,
                    subtasks=results,
                    merged_branches=[],
                    conflict_files=[],
                    integration_attempted=False,
                    integration_succeeded=False,
                    error=combined_err,
                )

            # 4. Merge each branch with changes in deterministic order
            merged_branches: list[str] = []
            integration_attempted = False
            integration_succeeded = False

            for res in results:
                if not res.modified_files:
                    continue

                merge_res = self._run_git(
                    [*_GIT_AUTHOR_ARGS, "merge", "--no-ff", "--no-edit", res.branch_name],
                    cwd=self.repo_root,
                    env=git_env,
                )
                if merge_res.exit_code == 0:
                    merged_branches.append(res.branch_name)
                    continue

                conflict_files = self._get_unmerged_files(git_env)
                merge_err = self._redact(
                    (merge_res.stderr or merge_res.stdout or "git merge failed").strip()
                )

                if conflict_files and conflict_resolver_fn is not None and not integration_attempted:
                    integration_attempted = True
                    try:
                        resolver_ok, resolver_msg = conflict_resolver_fn(
                            self.repo_root,
                            list(conflict_files),
                            merge_err,
                        )
                    except Exception as exc:  # noqa: BLE001
                        resolver_ok = False
                        resolver_msg = f"Conflict resolver raised an exception: {exc}"

                    safe_resolver_msg = self._redact(str(resolver_msg or "").strip())

                    if resolver_ok:
                        remaining_markers = _has_conflict_markers(self.repo_root, conflict_files)
                        if remaining_markers:
                            self._abort_merge_best_effort(git_env)
                            err_msg = self._redact(
                                "Conflict markers still present after integration round: "
                                + ", ".join(remaining_markers)
                            )
                            return ParallelExecutionReport(
                                succeeded=False,
                                subtasks=results,
                                merged_branches=merged_branches,
                                conflict_files=remaining_markers,
                                integration_attempted=True,
                                integration_succeeded=False,
                                error=err_msg,
                            )

                        # Check that resolver did not touch forbidden paths
                        post_status = self._run_git(
                            ["status", "--porcelain", "-uall"],
                            cwd=self.repo_root,
                            env=git_env,
                        )
                        if post_status.exit_code == 0:
                            touched_after = _parse_porcelain_paths(post_status.stdout)
                            forbidden_hits = [
                                p
                                for p in touched_after
                                if _matches_forbidden_prefix(p, self.forbidden_prefixes) is not None
                            ]
                            if forbidden_hits:
                                self._abort_merge_best_effort(git_env)
                                err_msg = self._redact(
                                    "Conflict resolver modified forbidden paths: "
                                    + ", ".join(forbidden_hits)
                                )
                                return ParallelExecutionReport(
                                    succeeded=False,
                                    subtasks=results,
                                    merged_branches=merged_branches,
                                    conflict_files=conflict_files,
                                    integration_attempted=True,
                                    integration_succeeded=False,
                                    error=err_msg,
                                )

                        if self._is_merge_in_progress(git_env):
                            stage_res = self._run_git(["add", "-A"], cwd=self.repo_root, env=git_env)
                            still_unmerged = self._get_unmerged_files(git_env)
                            if stage_res.exit_code != 0 or still_unmerged:
                                self._abort_merge_best_effort(git_env)
                                err_msg = self._redact(
                                    "Unresolved conflicts remain after integration round: "
                                    + ", ".join(still_unmerged or conflict_files)
                                )
                                return ParallelExecutionReport(
                                    succeeded=False,
                                    subtasks=results,
                                    merged_branches=merged_branches,
                                    conflict_files=still_unmerged or conflict_files,
                                    integration_attempted=True,
                                    integration_succeeded=False,
                                    error=err_msg,
                                )

                            finish_res = self._run_git(
                                [*_GIT_AUTHOR_ARGS, "commit", "--no-verify", "--no-edit"],
                                cwd=self.repo_root,
                                env=git_env,
                            )
                            if finish_res.exit_code != 0:
                                self._abort_merge_best_effort(git_env)
                                err_msg = self._redact(
                                    f"Failed to finalize merge commit after conflict resolution: "
                                    f"{(finish_res.stderr or finish_res.stdout).strip()}"
                                )
                                return ParallelExecutionReport(
                                    succeeded=False,
                                    subtasks=results,
                                    merged_branches=merged_branches,
                                    conflict_files=conflict_files,
                                    integration_attempted=True,
                                    integration_succeeded=False,
                                    error=err_msg,
                                )

                        integration_succeeded = True
                        merged_branches.append(res.branch_name)
                        continue

                    self._abort_merge_best_effort(git_env)
                    err_msg = self._redact(
                        safe_resolver_msg or f"Conflict resolution failed for {res.branch_name}"
                    )
                    return ParallelExecutionReport(
                        succeeded=False,
                        subtasks=results,
                        merged_branches=merged_branches,
                        conflict_files=conflict_files,
                        integration_attempted=True,
                        integration_succeeded=False,
                        error=err_msg,
                    )

                self._abort_merge_best_effort(git_env)
                return ParallelExecutionReport(
                    succeeded=False,
                    subtasks=results,
                    merged_branches=merged_branches,
                    conflict_files=conflict_files,
                    integration_attempted=integration_attempted,
                    integration_succeeded=integration_succeeded,
                    error=merge_err,
                )

            return ParallelExecutionReport(
                succeeded=True,
                subtasks=results,
                merged_branches=merged_branches,
                conflict_files=[],
                integration_attempted=integration_attempted,
                integration_succeeded=integration_succeeded,
                error="",
            )
        finally:
            self._cleanup_worktrees_and_branches(
                created_worktrees,
                created_branches,
                wt_base,
                git_env,
            )
