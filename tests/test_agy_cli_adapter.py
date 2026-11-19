"""Tests for AgyCliAdapter and end-to-end offline integration with TemplateScaffold and BuildWorker (A2b-3, A2b-4, A2b-5, A2b-6, INV-06, INV-09, INV-13, INV-18, INV-19, INV-23)."""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.agy_cli_adapter import AgyCliAdapter
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.model_resolver import ModelResolver
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker, JobState, LocalJobQueue
from agent_factory.domain.events import EventType
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    FileSpec,
    Plan,
)
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.code_agent import CodeAgentError, QuotaExhaustedError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError
from agent_factory.ports.vcs import CIRunResult
from agent_factory.scaffold import DEFAULT_TEMPLATES_ROOT, TemplateScaffold
from agent_factory.workspace import SandboxViolationError, WorkspaceManager


class _FakeClock:
    """Deterministic clock recording sleep calls for quota backoff verification."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 11, 16, 18, 0, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _sample_static_web_plan() -> Plan:
    return Plan(
        summary="Interactive counter widget for static web template",
        rationale="Pure ES module in src/app.js with zero external dependencies",
        template_id="static-web",
        files=[
            FileSpec(
                path="src/app.js",
                purpose="Counter state and DOM update logic",
                contract="export function increment(count) { return count + 1; }",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT-01",
                description="increment(0) returns 1 and increment(4) returns 5",
                type=AcceptanceTestType.UNIT,
            ),
            AcceptanceTest(
                id="AT-02",
                description="Renders index.html without console.error or pageerror",
                type=AcceptanceTestType.E2E,
            ),
        ],
        risks=["None"],
        deployment_target="github-pages",
    )


def _approved_static_web_orchestrator(
    project_id: str,
    clock: _FakeClock | None = None,
) -> tuple[Orchestrator, MemoryEventStore]:
    store = MemoryEventStore()
    plan = _sample_static_web_plan()
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Interactive counter widget",
                    "users": ["Gabriel"],
                    "features": ["Increment counter"],
                    "business_rules": ["Step is +1"],
                    "edge_cases": ["Zero start"],
                    "constraints": ["100% static"],
                    "success_criteria": ["Zero console errors"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {
                "approach": "Single ES module",
                "components": ["src/app.js"],
                "tradeoffs": ["In-memory state"],
            },
            {
                "critique": ["Ensure strict integer addition"],
                "robust_proposal": "Numeric coercion guard",
                "architectural_safeguards": ["Unit + e2e checks"],
            },
            plan.model_dump(mode="json"),
        ]
    )
    orchestrator = Orchestrator(
        store=store,
        llm=fake_llm,
        clock=clock or _FakeClock(),
    )
    orchestrator.create_project(
        project_id=project_id,
        name="Counter App",
        objective="Interactive counter widget",
    )
    orchestrator.handle_interview_message(project_id, "msg-01", "Complete counter spec")
    waiting = orchestrator.run_debate(project_id)
    assert waiting.plan_hash is not None
    orchestrator.submit_approval(
        project_id,
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 18, 5, tzinfo=UTC),
            decision="approved",
        ),
    )
    return orchestrator, store


def test_agy_cli_adapter_executes_in_workspace_cwd_and_strips_host_secrets_from_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2b-3 positive (INV-18, INV-19, INV-23): AgyCliAdapter runs CLI with cwd inside workspaces/<project_id>/ and strips sensitive host environment variables."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-cli-env")

    # Local CLI script that writes its actual cwd and received environment to tests/cli_probe.json
    cli_script = tmp_path / "probe_agy_cli.py"
    cli_script.write_text(
        "import json, os, pathlib, sys\n"
        "out_dir = pathlib.Path.cwd() / 'tests'\n"
        "out_dir.mkdir(parents=True, exist_ok=True)\n"
        "(out_dir / 'test_generated.py').write_text('def test_ok(): assert 1 == 1\\n', encoding='utf-8')\n"
        "probe = {'cwd': str(pathlib.Path.cwd().resolve()), 'env': dict(os.environ), 'argv': sys.argv[1:]}\n"
        "(out_dir / 'cli_probe.json').write_text(json.dumps(probe), encoding='utf-8')\n"
        "print(json.dumps({'files_written': ['tests/test_generated.py'], 'summary': 'Generated tests'}))\n",
        encoding="utf-8",
    )

    # Inject sensitive secrets into host environment
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret_host_token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-openai-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-key-value")
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-secret-token")
    monkeypatch.setenv("AGY_API_SECRET", "leaked-secret")
    monkeypatch.setenv("UNLISTED_RANDOM_HOST_VAR", "should-not-leak")
    monkeypatch.setenv("AGY_HOME", str(tmp_path / "agy_home"))

    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(cli_script)],
    )

    clean_env = adapter.build_clean_env()
    assert "GITHUB_TOKEN" not in clean_env
    assert "OPENAI_API_KEY" not in clean_env
    assert "AWS_SECRET_ACCESS_KEY" not in clean_env
    assert "VERCEL_TOKEN" not in clean_env
    assert "AGY_API_SECRET" not in clean_env
    assert "UNLISTED_RANDOM_HOST_VAR" not in clean_env
    assert clean_env.get("AGY_HOME") == str(tmp_path / "agy_home")
    assert "PATH" in clean_env

    result = adapter.generate_tests(
        ws_dir,
        [
            AcceptanceTest(
                id="AT-01",
                description="increment(0) returns 1",
                type=AcceptanceTestType.UNIT,
            )
        ],
    )

    assert "tests/test_generated.py" in result.files_written
    probe_data = json.loads((ws_dir / "tests" / "cli_probe.json").read_text(encoding="utf-8"))
    assert Path(probe_data["cwd"]).resolve() == ws_dir.resolve()
    child_env: dict[str, str] = probe_data["env"]
    assert "GITHUB_TOKEN" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "AWS_SECRET_ACCESS_KEY" not in child_env
    assert "VERCEL_TOKEN" not in child_env
    assert "UNLISTED_RANDOM_HOST_VAR" not in child_env
    assert child_env.get("AGY_HOME") == str(tmp_path / "agy_home")


def test_agy_cli_adapter_rejects_cwd_outside_workspaces_sandbox(tmp_path: Path) -> None:
    """A2b-3 negative (INV-18, INV-22, INV-23): AgyCliAdapter rejects workspace_dir outside workspaces/<project_id>/ before invoking Runner."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    fake_runner = FakeRunner()
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=fake_runner,
        cli_command=["agy"],
    )
    plan = _sample_static_web_plan()

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    invalid_id_dir = tmp_path / "workspaces" / "INVALID_UPPERCASE_ID"
    invalid_id_dir.mkdir(parents=True, exist_ok=True)

    for bad_dir in (outside_dir, ws_mgr.base_dir, invalid_id_dir):
        with pytest.raises(SandboxViolationError):
            adapter.validate_workspace_cwd(bad_dir)

        with pytest.raises(SandboxViolationError):
            adapter.generate_tests(bad_dir, plan.acceptance_tests)

        with pytest.raises(SandboxViolationError):
            adapter.implement_or_fix(bad_dir, plan, failure_output="fail", attempt=1)

    assert len(fake_runner.calls) == 0


def test_agy_cli_adapter_isolates_test_generation_prompt_from_plan_and_passes_plan_only_in_implement(
    tmp_path: Path,
) -> None:
    """A2b-4 positive (A2a-5, INV-13, INV-23): generate_tests sends ONLY acceptance_tests (never Plan or src/), while implement_or_fix sends Plan and failure_output."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-prompt-iso")
    plan = _sample_static_web_plan()

    cli_script = tmp_path / "record_payload_cli.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "raw_in = sys.stdin.read()\n"
        "if raw_in.strip():\n"
        "    payload = json.loads(raw_in)\n"
        "else:\n"
        "    payload = json.loads(args[args.index('--payload') + 1])\n"
        "mode = args[args.index('--mode') + 1] if '--mode' in args else payload.get('mode', '')\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode in ('generate-tests', 'generate_tests'):\n"
        "    (ws / 'tests').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'tests' / 'test_at.py').write_text('def test_at(): pass\\n', encoding='utf-8')\n"
        "    (ws / 'gen_payload.json').write_text(json.dumps(payload), encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_at.py'], 'summary': 'ok'}))\n"
        "else:\n"
        "    (ws / 'src').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'src' / 'app.js').write_text('export const ok = 1;\\n', encoding='utf-8')\n"
        "    (ws / 'impl_payload.json').write_text(json.dumps(payload), encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'implemented'}))\n",
        encoding="utf-8",
    )

    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(cli_script)],
    )

    # 1. Step A: generate_tests payload must NOT contain Plan, rationale, summary, or files
    adapter.generate_tests(ws_dir, plan.acceptance_tests)
    gen_payload: dict[str, Any] = json.loads(
        (ws_dir / "gen_payload.json").read_text(encoding="utf-8")
    )
    assert gen_payload["mode"] == "generate_tests"
    assert "plan" not in gen_payload
    assert "rationale" not in gen_payload
    assert "summary" not in gen_payload
    assert "files" not in gen_payload
    assert "Pure ES module in src/app.js" not in json.dumps(gen_payload)
    assert gen_payload["acceptance_tests"] == [
        {
            "id": "AT-01",
            "description": "increment(0) returns 1 and increment(4) returns 5",
            "type": "unit",
        },
        {
            "id": "AT-02",
            "description": "Renders index.html without console.error or pageerror",
            "type": "e2e",
        },
    ]

    # 2. Step B: implement_or_fix payload MUST include Plan, failure_output, and attempt
    adapter.implement_or_fix(
        ws_dir,
        plan,
        failure_output="AssertionError: expected 1 but got 0 in AT-01",
        attempt=2,
    )
    impl_payload: dict[str, Any] = json.loads(
        (ws_dir / "impl_payload.json").read_text(encoding="utf-8")
    )
    assert impl_payload["mode"] == "implement_or_fix"
    assert impl_payload["attempt"] == 2
    assert impl_payload["failure_output"] == "AssertionError: expected 1 but got 0 in AT-01"
    assert impl_payload["plan"]["summary"] == "Interactive counter widget for static web template"
    assert impl_payload["plan"]["files"][0]["path"] == "src/app.js"


def test_agy_cli_adapter_generate_tests_rejects_workspace_with_existing_src_files(
    tmp_path: Path,
) -> None:
    """A2b-4 negative (A2a-5, INV-13, INV-23): AgyCliAdapter.generate_tests raises SandboxViolationError if src/ already contains files."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-dirty-testgen")
    (ws_dir / "src").mkdir(parents=True, exist_ok=True)
    (ws_dir / "src" / "app.js").write_text("export const leaked = true;\n", encoding="utf-8")

    fake_runner = FakeRunner()
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=fake_runner,
        cli_command=["agy"],
    )
    plan = _sample_static_web_plan()

    with pytest.raises(SandboxViolationError):
        adapter.build_test_generation_payload(ws_dir, plan.acceptance_tests)

    with pytest.raises(SandboxViolationError):
        adapter.generate_tests(ws_dir, plan.acceptance_tests)

    assert len(fake_runner.calls) == 0


def test_agy_cli_adapter_translates_quota_errors_execution_failures_and_runner_timeouts(
    tmp_path: Path,
) -> None:
    """A2b-5 positive & negative (INV-06, INV-09, INV-23): AgyCliAdapter maps quota exhaustion to QuotaExhaustedError, non-zero exit to CodeAgentError, and respects SubprocessRunner timeouts."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-cli-errors")
    plan = _sample_static_web_plan()

    cli_script = tmp_path / "error_modes_cli.py"
    cli_script.write_text(
        "import json, sys, time\n"
        "args = sys.argv[1:]\n"
        "raw_in = sys.stdin.read()\n"
        "if raw_in.strip():\n"
        "    payload = json.loads(raw_in)\n"
        "else:\n"
        "    payload = json.loads(args[args.index('--payload') + 1])\n"
        "attempt = payload.get('attempt', 1)\n"
        "if attempt == 1:\n"
        "    sys.stderr.write('Error 429: Plan quota exhausted. retry_after=3.5\\n')\n"
        "    sys.exit(42)\n"
        "elif attempt == 2:\n"
        "    sys.stderr.write('Fatal CLI internal crash: unexpected token\\n')\n"
        "    sys.exit(2)\n"
        "else:\n"
        "    time.sleep(5.0)\n",
        encoding="utf-8",
    )

    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=5.0),
        cli_command=[sys.executable, str(cli_script)],
        timeout_seconds=5.0,
        default_retry_after_seconds=3.5,
    )

    # 1. Quota exhausted -> QuotaExhaustedError
    with pytest.raises(QuotaExhaustedError) as quota_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=1)
    assert quota_exc.value.retry_after_seconds == 3.5

    # 2. Fatal CLI exit -> CodeAgentError (and NOT QuotaExhaustedError)
    with pytest.raises(CodeAgentError) as fatal_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=2)
    assert not isinstance(fatal_exc.value, QuotaExhaustedError)
    assert "Fatal CLI internal crash" in str(fatal_exc.value)

    # 3. Subprocess hang -> timeout enforced by SubprocessRunner -> CommandTimeoutError or CodeAgentError
    timeout_adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=0.3),
        cli_command=[sys.executable, str(cli_script)],
        timeout_seconds=0.3,
        default_retry_after_seconds=3.5,
    )
    with pytest.raises((CommandTimeoutError, CodeAgentError)) as timeout_exc:
        timeout_adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=3)
    assert "timed out" in str(timeout_exc.value).lower()


def test_end_to_end_offline_scaffold_agy_cli_adapter_and_build_worker_converge(
    tmp_path: Path,
) -> None:
    """A2b-6 positive (AC-2.1, AC-2.2, INV-13, INV-18, INV-20, INV-23): Full offline integration of TemplateScaffold + WorkspaceManager + AgyCliAdapter + BuildWorker converges to DELIVERED."""
    project_id = "proj-e2e-2b"
    orchestrator, _ = _approved_static_web_orchestrator(project_id)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    # 1. Materialize static-web template into isolated workspace
    ws_dir = scaffold.scaffold(project_id=project_id, template_id="static-web")
    assert (ws_dir / "package.json").is_file()
    assert [p for p in (ws_dir / "src").rglob("*") if p.is_file()] == []

    # 2. Local simulated agy CLI executable script in tmp_path
    cli_script = tmp_path / "sim_agy_e2e.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "raw_in = sys.stdin.read()\n"
        "if raw_in.strip():\n"
        "    payload = json.loads(raw_in)\n"
        "else:\n"
        "    payload = json.loads(args[args.index('--payload') + 1])\n"
        "mode = args[args.index('--mode') + 1] if '--mode' in args else payload.get('mode', '')\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode in ('generate-tests', 'generate_tests'):\n"
        "    tdir = ws / 'tests'\n"
        "    tdir.mkdir(parents=True, exist_ok=True)\n"
        "    (tdir / 'test_counter.py').write_text(\n"
        "        'def test_at_01(): assert True\\ndef test_at_02(): assert True\\n',\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "    print(json.dumps({'files_written': ['tests/test_counter.py'], 'summary': 'Tests ready'}))\n"
        "else:\n"
        "    sdir = ws / 'src'\n"
        "    sdir.mkdir(parents=True, exist_ok=True)\n"
        "    attempt = payload.get('attempt', 1)\n"
        "    if attempt <= 2:\n"
        "        (sdir / 'app.js').write_text('export function increment(c) { return c; }\\n', encoding='utf-8')\n"
        "    else:\n"
        "        (sdir / 'app.js').write_text('export function increment(c) { return c + 1; }\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': f'Attempt {attempt}'}))\n",
        encoding="utf-8",
    )

    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(cli_script)],
    )
    test_runner = FakeRunner(
        results=[
            # Attempt 1 (invocation 2): AT-01 fails locally
            CommandResult(
                exit_code=1,
                stdout="1 passed, 1 failed",
                stderr="AssertionError in AT-01",
                test_results={"AT-01": False, "AT-02": True},
            ),
            # Attempt 2 (invocation 3): both AT-01 and AT-02 pass locally
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                stderr="",
                test_results={"AT-01": True, "AT-02": True},
            ),
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")])

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=adapter,
        runner=test_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(project_id=project_id)

    status = orchestrator.get_status(project_id)
    assert status.state == ProjectState.DELIVERED
    assert status.progress_pct == 100
    assert report.invocations == 3
    assert report.ci_rounds == 1
    assert report.failure_cause is None
    assert report.test_results == {"AT-01": True, "AT-02": True}
    assert (ws_dir / "PROJECT_PROMPT.md").is_file()
    assert "return c + 1;" in (ws_dir / "src" / "app.js").read_text(encoding="utf-8")

    events_e2e = orchestrator.store.get_events(project_id)
    for ev_type in (
        EventType.AGENT_INVOKED,
        EventType.TESTS_UPDATED,
        EventType.CI_ROUND_COMPLETED,
        EventType.PROJECT_DELIVERED,
    ):
        matching = [e for e in events_e2e if e.type == ev_type and "duration_seconds" in (e.payload or {})]
        assert matching, f"Expected duration_seconds on {ev_type}"
    assert adapter.model_resolver.get_active_activity()["activity_summary"] == "Guardando tu proyecto terminado en GitHub..."


def test_end_to_end_offline_build_worker_pauses_on_agy_cli_quota_and_fails_on_cli_crash(
    tmp_path: Path,
) -> None:
    """A2b-6 negative (AC-2.4, INV-09, INV-23): BuildWorker with AgyCliAdapter pauses on CLI quota error without failing and transitions to FAILED on fatal CLI error."""
    # 1. Quota pause and resume via AgyCliAdapter
    project_quota = "proj-e2e-cli-quota"
    clock = _FakeClock()
    orchestrator_1, _ = _approved_static_web_orchestrator(project_quota, clock=clock)
    ws_mgr_1 = WorkspaceManager(base_dir=tmp_path / "ws_quota")
    scaffold_1 = TemplateScaffold(workspace_manager=ws_mgr_1, templates_root=DEFAULT_TEMPLATES_ROOT)
    scaffold_1.scaffold(project_id=project_quota, template_id="static-web")

    cli_quota_script = tmp_path / "sim_agy_quota.py"
    cli_quota_script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "mode = args[args.index('--mode') + 1]\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
        "    (ws / 'tests').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'tests' / 'test_c.py').write_text('def test_1(): pass\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_c.py'], 'summary': 'ok'}))\n"
        "else:\n"
        "    marker = ws / '.quota_once'\n"
        "    if not marker.exists():\n"
        "        marker.write_text('1', encoding='utf-8')\n"
        "        sys.stderr.write('429 Quota exhausted\\n')\n"
        "        sys.exit(42)\n"
        "    (ws / 'src').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'src' / 'app.js').write_text('export const ok = 1;\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'done'}))\n",
        encoding="utf-8",
    )

    queue = LocalJobQueue()
    adapter_1 = AgyCliAdapter(
        workspace_manager=ws_mgr_1,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(cli_quota_script)],
        default_retry_after_seconds=2.0,
    )
    worker_1 = BuildWorker(
        orchestrator=orchestrator_1,
        workspace_manager=ws_mgr_1,
        code_agent=adapter_1,
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=0,
                    stdout="2 passed",
                    test_results={"AT-01": True, "AT-02": True},
                )
            ]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")]),
        queue=queue,
        clock=clock,
    )
    worker_1.enqueue_approved_project(project_id=project_quota)

    paused_report = worker_1.run_next()
    assert paused_report is not None
    assert paused_report.invocations == 1
    assert paused_report.failure_cause is None
    job_1 = queue.get_job(project_quota)
    assert job_1 is not None
    assert job_1.state == JobState.PAUSED_QUOTA
    assert clock.sleeps == [2.0]

    resumed_report = worker_1.run_next()
    assert resumed_report is not None
    assert resumed_report.invocations == 2
    assert orchestrator_1.get_status(project_quota).state == ProjectState.DELIVERED

    # 2. Fatal CLI crash transitions project to FAILED
    project_crash = "proj-e2e-cli-crash"
    orchestrator_2, _ = _approved_static_web_orchestrator(project_crash)
    ws_mgr_2 = WorkspaceManager(base_dir=tmp_path / "ws_crash")
    scaffold_2 = TemplateScaffold(workspace_manager=ws_mgr_2, templates_root=DEFAULT_TEMPLATES_ROOT)
    scaffold_2.scaffold(project_id=project_crash, template_id="static-web")

    cli_crash_script = tmp_path / "sim_agy_crash.py"
    cli_crash_script.write_text(
        "import sys\n"
        "sys.stderr.write('Unrecoverable CLI binary failure\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    adapter_2 = AgyCliAdapter(
        workspace_manager=ws_mgr_2,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(cli_crash_script)],
    )
    worker_2 = BuildWorker(
        orchestrator=orchestrator_2,
        workspace_manager=ws_mgr_2,
        code_agent=adapter_2,
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    crash_report = worker_2.run_job(project_id=project_crash)
    status_2 = orchestrator_2.get_status(project_crash)
    assert status_2.state == ProjectState.FAILED
    assert crash_report.failure_cause is not None
    assert "Unrecoverable CLI binary failure" in crash_report.failure_cause


def test_build_worker_captures_agy_cli_adapter_timeouts_in_test_gen_and_impl_without_getting_stuck(
    tmp_path: Path,
) -> None:
    """F2B-01 (INV-06, INV-09, A2b-5, A2b-6): Timeouts in AgyCliAdapter during generate_tests or implement_or_fix record the call, emit PROJECT_FAILED, and transition to FAILED with BuildReport."""
    # 1. Timeout during Step A (generate_tests)
    project_step_a = "proj-cli-timeout-step-a"
    orch_a, store_a = _approved_static_web_orchestrator(project_step_a)
    ws_mgr_a = WorkspaceManager(base_dir=tmp_path / "ws_to_a")
    TemplateScaffold(workspace_manager=ws_mgr_a, templates_root=DEFAULT_TEMPLATES_ROOT).scaffold(
        project_id=project_step_a, template_id="static-web"
    )

    hang_step_a_script = tmp_path / "hang_step_a.py"
    hang_step_a_script.write_text("import time\ntime.sleep(5.0)\n", encoding="utf-8")

    queue_a = LocalJobQueue()
    adapter_a = AgyCliAdapter(
        workspace_manager=ws_mgr_a,
        runner=SubprocessRunner(default_timeout_seconds=0.4),
        cli_command=[sys.executable, str(hang_step_a_script)],
        timeout_seconds=0.4,
    )
    worker_a = BuildWorker(
        orchestrator=orch_a,
        workspace_manager=ws_mgr_a,
        code_agent=adapter_a,
        runner=FakeRunner(),
        vcs=FakeVCS(),
        queue=queue_a,
    )
    worker_a.enqueue_approved_project(project_id=project_step_a)

    report_a = worker_a.run_job(project_id=project_step_a)
    status_a = orch_a.get_status(project_step_a)
    job_a = queue_a.get_job(project_step_a)
    assert status_a.state == ProjectState.FAILED
    assert job_a is not None
    assert job_a.state == JobState.FAILED
    assert report_a.invocations == 1
    assert report_a.failure_cause is not None
    assert "timed out" in report_a.failure_cause.lower()
    assert orch_a.circuit_breaker.get_calls(project_step_a, ProjectState.BUILDING) == 1
    assert status_a.agent_invocations == 2
    assert any(e.type == EventType.PROJECT_FAILED for e in store_a.get_events(project_step_a))

    # 2. Timeout during Step B (implement_or_fix) after generate_tests succeeds
    project_step_b = "proj-cli-timeout-step-b"
    orch_b, store_b = _approved_static_web_orchestrator(project_step_b)
    ws_mgr_b = WorkspaceManager(base_dir=tmp_path / "ws_to_b")
    TemplateScaffold(workspace_manager=ws_mgr_b, templates_root=DEFAULT_TEMPLATES_ROOT).scaffold(
        project_id=project_step_b, template_id="static-web"
    )

    hang_step_b_script = tmp_path / "hang_step_b.py"
    hang_step_b_script.write_text(
        "import json, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "mode = args[args.index('--mode') + 1]\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
        "    (ws / 'tests').mkdir(parents=True, exist_ok=True)\n"
        "    (ws / 'tests' / 'test_c.py').write_text('def test_1(): pass\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_c.py'], 'summary': 'ok'}))\n"
        "else:\n"
        "    time.sleep(5.0)\n",
        encoding="utf-8",
    )

    queue_b = LocalJobQueue()
    adapter_b = AgyCliAdapter(
        workspace_manager=ws_mgr_b,
        runner=SubprocessRunner(default_timeout_seconds=1.2),
        cli_command=[sys.executable, str(hang_step_b_script)],
        timeout_seconds=1.2,
    )
    worker_b = BuildWorker(
        orchestrator=orch_b,
        workspace_manager=ws_mgr_b,
        code_agent=adapter_b,
        runner=FakeRunner(),
        vcs=FakeVCS(),
        queue=queue_b,
    )
    worker_b.enqueue_approved_project(project_id=project_step_b)

    report_b = worker_b.run_job(project_id=project_step_b)
    status_b = orch_b.get_status(project_step_b)
    job_b = queue_b.get_job(project_step_b)
    assert status_b.state == ProjectState.FAILED
    assert job_b is not None
    assert job_b.state == JobState.FAILED
    assert report_b.invocations == 2
    assert report_b.failure_cause is not None
    assert "timed out" in report_b.failure_cause.lower()
    assert orch_b.circuit_breaker.get_calls(project_step_b, ProjectState.BUILDING) == 2
    assert status_b.agent_invocations == 3
    assert any(e.type == EventType.PROJECT_FAILED for e in store_b.get_events(project_step_b))


def test_agy_cli_adapter_does_not_misclassify_429_substring_on_success_or_non_quota_failure(
    tmp_path: Path,
) -> None:
    """F2B-02 (INV-09, A2b-5, INV-23): Success output containing '429'/'rate limit' returns CodeAgentResult, while non-quota errors containing '429' raise non-quota CodeAgentError."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-429-precision")
    plan = _sample_static_web_plan()

    cli_script = tmp_path / "line_429_cli.py"
    cli_script.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "raw_in = sys.stdin.read()\n"
        "if raw_in.strip():\n"
        "    payload = json.loads(raw_in)\n"
        "else:\n"
        "    payload = json.loads(args[args.index('--payload') + 1])\n"
        "attempt = payload.get('attempt', 1)\n"
        "if attempt == 1:\n"
        "    print(json.dumps({'files_written': ['src/app.js'], 'summary': 'Wrote 429 bytes near line 429 rate limit check'}))\n"
        "    sys.exit(0)\n"
        "elif attempt == 2:\n"
        "    sys.stderr.write('SyntaxError at src/app.js line 429: unexpected token\\n')\n"
        "    sys.exit(1)\n"
        "elif attempt == 3:\n"
        "    sys.stderr.write('HTTP 429 Too Many Requests: slow down\\n')\n"
        "    sys.exit(1)\n"
        "elif attempt == 4:\n"
        "    sys.stderr.write('TypeError at src/app.js:429:12 - undefined is not a function\\n')\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    sys.stderr.write('AssertionError: expected 429 to equal 200\\n')\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )

    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=SubprocessRunner(default_timeout_seconds=5.0),
        cli_command=[sys.executable, str(cli_script)],
        timeout_seconds=5.0,
        default_retry_after_seconds=4.0,
    )

    # 1. Exit code 0 with '429' and 'rate limit' in summary succeeds without QuotaExhaustedError
    ok_res = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=1)
    assert ok_res.files_written == ["src/app.js"]
    assert "429 bytes" in ok_res.summary

    # 2. Exit code 1 with 'line 429' in SyntaxError raises non-quota CodeAgentError
    with pytest.raises(CodeAgentError) as syntax_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=2)
    assert not isinstance(syntax_exc.value, QuotaExhaustedError)
    assert "line 429" in str(syntax_exc.value)

    # 3. Real HTTP 429 status on exit code 1 still triggers QuotaExhaustedError
    with pytest.raises(QuotaExhaustedError) as quota_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=3)
    assert quota_exc.value.retry_after_seconds == 4.0

    # 4. Exit code 1 with ':429:12' stacktrace location raises non-quota CodeAgentError
    with pytest.raises(CodeAgentError) as type_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=4)
    assert not isinstance(type_exc.value, QuotaExhaustedError)
    assert "TypeError at src/app.js:429:12" in str(type_exc.value)

    # 5. Exit code 1 with 'expected 429' assertion error raises non-quota CodeAgentError
    with pytest.raises(CodeAgentError) as assert_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=5)
    assert not isinstance(assert_exc.value, QuotaExhaustedError)
    assert "AssertionError: expected 429 to equal 200" in str(assert_exc.value)


def test_agy_cli_adapter_rejects_nested_workspace_subdir_and_handles_multiline_or_plain_stdout(
    tmp_path: Path,
) -> None:
    """F2B-07 (A2b-3, INV-18, INV-22): Rejects nested subdir workspaces/<id>/sub and handles multiline JSON, trailing-line JSON, and plain-text stdout on exit 0 and exit 1."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-sub-and-stdout")
    nested_dir = ws_dir / "subdir"
    nested_dir.mkdir(parents=True, exist_ok=True)
    plan = _sample_static_web_plan()

    fake_runner = FakeRunner(
        results=[
            # 1. Multiline pretty-printed JSON stdout
            CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {"files_written": ["src/app.js"], "summary": "Pretty JSON output"},
                    indent=2,
                ),
            ),
            # 2. Plain text stdout (non-JSON fallback, exit_code == 0)
            CommandResult(
                exit_code=0,
                stdout="Plain text completion message\nLine 2",
            ),
            # 3. Log preamble followed by trailing JSON line (covers agy_cli_adapter.py:242-246)
            CommandResult(
                exit_code=0,
                stdout=(
                    "Analyzing workspace...\n"
                    '{"files_written": ["src/tail.js"], "summary": "Tail JSON ok"}'
                ),
            ),
            # 4. Malformed brace line inside plain text stdout (covers agy_cli_adapter.py:249-250)
            CommandResult(
                exit_code=0,
                stdout="{not valid json}\nPlain text fallback",
            ),
            # 5. Empty stdout (exit_code == 0)
            CommandResult(
                exit_code=0,
                stdout="",
            ),
            # 6. Non-dict valid JSON stdout (exit_code == 0)
            CommandResult(
                exit_code=0,
                stdout='["not-a-dict"]',
            ),
            # 7. Plain text non-JSON failure (exit_code == 1)
            CommandResult(
                exit_code=1,
                stdout="Plain build step output",
                stderr="Plain compiler failure without json",
            ),
        ]
    )
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=fake_runner,
        cli_command=["agy"],
    )

    with pytest.raises(SandboxViolationError):
        adapter.validate_workspace_cwd(nested_dir)

    res_multiline = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=1)
    assert res_multiline.files_written == ["src/app.js"]
    assert res_multiline.summary == "Pretty JSON output"

    res_plain = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=2)
    assert res_plain.files_written == []
    assert "Plain text completion message" in res_plain.summary

    res_tail = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=3)
    assert res_tail.files_written == ["src/tail.js"]
    assert res_tail.summary == "Tail JSON ok"

    res_bad_brace = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=4)
    assert res_bad_brace.files_written == []
    assert res_bad_brace.summary == "{not valid json}\nPlain text fallback"

    res_empty = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=5)
    assert res_empty.files_written == []
    assert res_empty.summary == ""

    res_non_dict = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=6)
    assert res_non_dict.files_written == []
    assert res_non_dict.summary == '["not-a-dict"]'

    with pytest.raises(CodeAgentError) as plain_fail_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=7)
    assert not isinstance(plain_fail_exc.value, QuotaExhaustedError)
    assert "Plain compiler failure without json" in str(plain_fail_exc.value)


def test_agy_cli_adapter_command_has_no_payload_and_uses_stdin(tmp_path: Path) -> None:
    """Verify agy command has no -payload/--payload, contains -p and --add-dir, and delivers payload via stdin."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-stdin-cli")
    plan = _sample_static_web_plan()

    runner = FakeRunner()
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=runner,
        cli_command=("agy",),
        model="custom-builder-model",
    )

    # 1. Test generate_tests
    adapter.generate_tests(ws_dir, plan.acceptance_tests)
    assert len(runner.calls) == 1
    call_gen = runner.calls[0]
    cmd_gen = call_gen["command"]
    assert "-payload" not in cmd_gen
    assert "--payload" not in cmd_gen
    assert "-p" in cmd_gen
    assert "--add-dir" in cmd_gen
    assert str(ws_dir.resolve()) in cmd_gen
    assert "--dangerously-skip-permissions" in cmd_gen
    assert "--model" in cmd_gen
    assert "custom-builder-model" in cmd_gen
    assert call_gen["input"] is not None
    payload_gen = json.loads(call_gen["input"])
    assert payload_gen["mode"] == "generate_tests"
    assert "acceptance_tests" in payload_gen

    # 2. Test implement_or_fix
    adapter.implement_or_fix(ws_dir, plan, failure_output="AssertionError: 1 != 0", attempt=1)
    assert len(runner.calls) == 2
    call_impl = runner.calls[1]
    cmd_impl = call_impl["command"]
    assert "-payload" not in cmd_impl
    assert "--payload" not in cmd_impl
    assert "-p" in cmd_impl
    assert "--add-dir" in cmd_impl
    assert str(ws_dir.resolve()) in cmd_impl
    assert "--dangerously-skip-permissions" in cmd_impl
    assert "--model" in cmd_impl
    assert "custom-builder-model" in cmd_impl
    assert call_impl["input"] is not None
    payload_impl = json.loads(call_impl["input"])
    assert payload_impl["mode"] == "implement_or_fix"
    assert "plan" in payload_impl
    assert payload_impl["failure_output"] == "AssertionError: 1 != 0"


def test_agy_cli_adapter_resolves_and_retries_fallback_models_on_invalid_selection_and_quota(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolve and retry fallback models on invalid selection and quota errors up to 3 attempts."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-model-fallback")
    plan = _sample_static_web_plan()
    dummy_secret = "DUMMY_TEST_SECRET_VALUE_999"

    available_output = (
        'error: invalid model selection (--model "gemini-2.5-pro" --effort ""): '
        "model gemini-2.5-pro is not recognized as a known model or custom model in settings\n\n"
        "Available models:\n"
        "  - Gemini 3.5 Pro\n"
        "  - Gemini 4 Neon (High)\n"
        "  - Gemini 4 Argon (High)\n"
        "  - Claude Sonnet 4.5\n"
        "  - Gemini 4 Hydrogen (Medium)\n"
        "  - Gemini 4 Hydrogen (High)\n"
        "  - Gemini 3.8 Flash (High)\n"
        f"leaked_debug={dummy_secret}\n"
    )

    # Phase 1: implement_or_fix fails on configured "gemini-2.5-pro", parses Available models:,
    # retries on credit-efficient heavy preference "Gemini 4 Hydrogen (Medium)" and succeeds.
    runner = FakeRunner(
        results=[
            CommandResult(exit_code=1, stdout="", stderr=available_output),
            CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {"files_written": ["index.html"], "summary": "Implemented with fallback"}
                ),
            ),
            # Phase 2: generate_tests starts proactively on cached "Gemini 4 Hydrogen (Medium)",
            # hits quota (exit_code=1 / 429), retries on "Gemini 4 Hydrogen (High)" and succeeds.
            CommandResult(
                exit_code=1,
                stdout="",
                stderr=f"429 rate limit exceeded quota exhausted token={dummy_secret}",
            ),
            CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {"files_written": ["tests/test_app.py"], "summary": "Generated tests"}
                ),
            ),
            # Phase 3: exhausting 3 distinct models stops at attempt 3 and raises.
            CommandResult(exit_code=1, stdout="", stderr="429 quota exceeded on model 1"),
            CommandResult(exit_code=1, stdout="", stderr="429 quota exceeded on model 2"),
            CommandResult(exit_code=1, stdout="", stderr="429 quota exceeded on model 3"),
        ]
    )

    resolver = ModelResolver(
        runner=runner,
        cli_command=("agy",),
        auto_probe=False,
        secrets=(dummy_secret,),
    )
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=runner,
        cli_command=("agy",),
        model="gemini-2.5-pro",
        model_resolver=resolver,
        secrets=(dummy_secret,),
    )

    with caplog.at_level(logging.INFO):
        res_impl = adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=1)
        assert res_impl.files_written == ["index.html"]
        assert res_impl.summary == "Implemented with fallback"
        assert res_impl.model == "Gemini 4 Hydrogen (Medium)"
        assert resolver.get_active_usage()["model"] == "Gemini 4 Hydrogen (Medium)"
        assert resolver.get_active_usage()["stage"] == "BUILDING"

        assert len(runner.calls) == 2
        assert "gemini-2.5-pro" in runner.calls[0]["command"]
        assert "Gemini 4 Hydrogen (Medium)" in runner.calls[1]["command"]

        # Resolver now has cached models; generate_tests uses "Gemini 4 Hydrogen (Medium)" proactively
        # then falls back to "Gemini 4 Hydrogen (High)" on quota error.
        res_tests = adapter.generate_tests(ws_dir, plan.acceptance_tests)
        assert res_tests.files_written == ["tests/test_app.py"]
        assert res_tests.model == "Gemini 4 Hydrogen (High)"
        assert resolver.get_active_usage()["model"] == "Gemini 4 Hydrogen (High)"
        assert len(runner.calls) == 4
        assert "Gemini 4 Hydrogen (Medium)" in runner.calls[2]["command"]
        assert "Gemini 4 Hydrogen (High)" in runner.calls[3]["command"]

        # Exhausting 3 distinct models caps retries at 3 calls and raises QuotaExhaustedError / CodeAgentError
        calls_before_exhaust = len(runner.calls)
        with pytest.raises((QuotaExhaustedError, CodeAgentError)):
            adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=2)
        assert len(runner.calls) - calls_before_exhaust == 3

    # Verify secrets were never written into logs
    assert dummy_secret not in caplog.text
    assert "Gemini 4 Hydrogen (Medium)" in caplog.text
    assert "Gemini 4 Hydrogen (High)" in caplog.text


def test_agy_cli_adapter_passes_auto_mode_and_explicit_workspace_file_prompts(
    tmp_path: Path,
) -> None:
    """Real `agy` command includes `--mode auto`, `--dangerously-skip-permissions`, workspace paths, and explicit file instructions in `-p`."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-auto-mode")
    plan = _sample_static_web_plan()

    runner = FakeRunner()
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=runner,
        cli_command=("agy",),
        model="Gemini 4 Hydrogen (Medium)",
    )

    adapter.generate_tests(ws_dir, plan.acceptance_tests)
    cmd_gen = runner.calls[0]["command"]
    assert "--mode" in cmd_gen
    assert cmd_gen[cmd_gen.index("--mode") + 1] == "auto"
    assert "--dangerously-skip-permissions" in cmd_gen
    prompt_gen = cmd_gen[cmd_gen.index("-p") + 1]
    assert str(ws_dir.resolve()) in prompt_gen
    assert "tests/test_app.py" in prompt_gen
    assert "AT-01" in prompt_gen
    assert "Pure ES module in src/app.js" not in prompt_gen

    adapter.implement_or_fix(ws_dir, plan, failure_output="AT-01 failed", attempt=1)
    cmd_impl = runner.calls[1]["command"]
    assert "--mode" in cmd_impl
    assert cmd_impl[cmd_impl.index("--mode") + 1] == "auto"
    prompt_impl = cmd_impl[cmd_impl.index("-p") + 1]
    assert str(ws_dir.resolve()) in prompt_impl
    assert "src/app.js" in prompt_impl


def test_agy_cli_adapter_extracts_test_blocks_retries_zero_tests_and_enforces_inv13_inv22(
    tmp_path: Path,
) -> None:
    """Verify fallback extraction from stdout blocks into workspace/tests, explicit retry on empty test output, strict INV-13 preservation, and INV-22 traversal rejection."""
    # A. Fallback extraction from fenced code blocks in CLI stdout (single call, no retry needed)
    ws_mgr_a = WorkspaceManager(base_dir=tmp_path / "ws_extract")
    ws_dir_a = ws_mgr_a.create_workspace("proj-extract-blocks")
    plan = _sample_static_web_plan()

    markdown_stdout = (
        "Here are the acceptance tests:\n\n"
        "```python tests/test_pomodoro.py\n"
        "def test_at_01_pomodoro():\n"
        "    assert 25 * 60 == 1500\n"
        "```\n\n"
        "```python:tests/sub/test_extra.py\n"
        "def test_at_02_extra():\n"
        "    assert True\n"
        "```\n"
    )
    runner_extract = FakeRunner(results=[CommandResult(exit_code=0, stdout=markdown_stdout)])
    adapter_extract = AgyCliAdapter(
        workspace_manager=ws_mgr_a,
        runner=runner_extract,
        cli_command=("agy",),
        retry_if_no_tests_written=True,
    )
    res_extract = adapter_extract.generate_tests(ws_dir_a, plan.acceptance_tests)
    assert len(runner_extract.calls) == 1
    pomodoro_file = ws_dir_a / "tests" / "test_pomodoro.py"
    extra_file = ws_dir_a / "tests" / "sub" / "test_extra.py"
    assert pomodoro_file.is_file()
    assert "def test_at_01_pomodoro():" in pomodoro_file.read_text(encoding="utf-8")
    assert extra_file.is_file()
    assert "def test_at_02_extra():" in extra_file.read_text(encoding="utf-8")
    assert "tests/test_pomodoro.py" in res_extract.files_written
    assert "tests/sub/test_extra.py" in res_extract.files_written

    # B. Explicit retry succeeds when CLI writes nothing on attempt 1 and creates tests/ on attempt 2
    ws_mgr_b = WorkspaceManager(base_dir=tmp_path / "ws_retry")
    ws_dir_b = ws_mgr_b.create_workspace("proj-retry-tests")
    retry_script = tmp_path / "retry_tests_cli.py"
    retry_script.write_text(
        "import json, pathlib, sys\n"
        "ws = pathlib.Path.cwd()\n"
        "marker = ws / '.first_run_seen'\n"
        "if not marker.exists():\n"
        "    marker.write_text('1', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': [], 'summary': 'forgot to write'}))\n"
        "else:\n"
        "    tdir = ws / 'tests'\n"
        "    tdir.mkdir(parents=True, exist_ok=True)\n"
        "    (tdir / 'test_app.py').write_text('def test_retry_ok(): assert True\\n', encoding='utf-8')\n"
        "    print(json.dumps({'files_written': ['tests/test_app.py'], 'summary': 'written on retry'}))\n",
        encoding="utf-8",
    )
    adapter_retry = AgyCliAdapter(
        workspace_manager=ws_mgr_b,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(retry_script)],
    )
    res_retry = adapter_retry.generate_tests(ws_dir_b, plan.acceptance_tests)
    assert (ws_dir_b / "tests" / "test_app.py").is_file()
    assert "tests/test_app.py" in res_retry.files_written

    # C. Preserves INV-13 failure in BuildWorker if both initial attempt and retry write zero tests
    project_empty = "proj-inv13-empty"
    orch_c, _ = _approved_static_web_orchestrator(project_empty)
    ws_mgr_c = WorkspaceManager(base_dir=tmp_path / "ws_inv13")
    TemplateScaffold(workspace_manager=ws_mgr_c, templates_root=DEFAULT_TEMPLATES_ROOT).scaffold(
        project_id=project_empty,
        template_id="static-web",
    )
    empty_script = tmp_path / "empty_tests_cli.py"
    empty_script.write_text(
        "import json\n"
        "print(json.dumps({'files_written': [], 'summary': 'no tests written'}))\n",
        encoding="utf-8",
    )
    adapter_empty = AgyCliAdapter(
        workspace_manager=ws_mgr_c,
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(empty_script)],
    )
    worker_c = BuildWorker(
        orchestrator=orch_c,
        workspace_manager=ws_mgr_c,
        code_agent=adapter_empty,
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )
    report_c = worker_c.run_job(project_id=project_empty)
    assert orch_c.get_status(project_empty).state == ProjectState.FAILED
    assert report_c.failure_cause is not None
    assert "INV-13" in report_c.failure_cause
    assert report_c.invocations == 1

    # D. Rejects path traversal outside workspace/tests (INV-22)
    ws_mgr_d = WorkspaceManager(base_dir=tmp_path / "ws_traversal")
    ws_dir_d = ws_mgr_d.create_workspace("proj-traversal-guard")
    outside_target = tmp_path / "escaped_test.py"
    for bad_label in (
        "../escaped_test.py",
        "tests/../../escaped_test.py",
        str(outside_target.resolve()),
    ):
        mal_stdout = f"```python {bad_label}\ndef test_evil(): pass\n```\n"
        mal_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout=mal_stdout)])
        mal_adapter = AgyCliAdapter(
            workspace_manager=ws_mgr_d,
            runner=mal_runner,
            cli_command=("agy",),
            retry_if_no_tests_written=False,
        )
        with pytest.raises(SandboxViolationError):
            mal_adapter.generate_tests(ws_dir_d, plan.acceptance_tests)
        assert not outside_target.exists()


def test_agy_cli_adapter_defaults_to_300s_timeout_closes_stdin_and_redacts_timeout_errors(
    tmp_path: Path,
) -> None:
    """Default timeout is 300.0s, stdin closes cleanly on EOF without hanging, and timeout errors redact secrets."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-timeout-300")
    plan = _sample_static_web_plan()
    secret_tok = "DUMMY_CLI_TIMEOUT_SECRET_888"

    default_adapter = AgyCliAdapter(workspace_manager=ws_mgr)
    assert default_adapter.timeout_seconds == 300.0
    assert default_adapter.runner.default_timeout_seconds == 300.0

    explicit_adapter = AgyCliAdapter(workspace_manager=ws_mgr, timeout_seconds=45.0)
    assert explicit_adapter.timeout_seconds == 45.0

    # Real subprocess reading stdin must receive EOF cleanly and not block
    eof_script = tmp_path / "read_stdin_eof.py"
    eof_script.write_text(
        "import json, sys\n"
        "data = sys.stdin.read()\n"
        "print(json.dumps({'files_written': ['src/app.js'], 'summary': f'read {len(data)} bytes'}))\n",
        encoding="utf-8",
    )
    real_runner = SubprocessRunner(default_timeout_seconds=10.0)
    res_no_stdin = real_runner.run(
        command=[sys.executable, str(eof_script)],
        cwd=ws_dir,
    )
    assert res_no_stdin.exit_code == 0
    assert "read 0 bytes" in res_no_stdin.stdout

    eof_adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=real_runner,
        cli_command=[sys.executable, str(eof_script)],
    )
    res_with_stdin = eof_adapter.implement_or_fix(ws_dir, plan, attempt=1)
    assert res_with_stdin.files_written == ["src/app.js"]

    # Timeout errors from Runner must redact secret tokens in CommandTimeoutError
    timeout_runner = FakeRunner(
        results=[
            CommandTimeoutError(
                f"Command timed out after 300.0s with token={secret_tok}"
            )
        ]
    )
    timeout_adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=timeout_runner,
        cli_command=("agy",),
        secrets=(secret_tok,),
    )
    with pytest.raises(CommandTimeoutError) as exc_info:
        timeout_adapter.implement_or_fix(ws_dir, plan, attempt=1)
    assert secret_tok not in str(exc_info.value)
    assert "timed out" in str(exc_info.value).lower()


def test_agy_cli_adapter_cleans_fenced_json_summary_and_enforces_stdlib_test_prompt(
    tmp_path: Path,
) -> None:
    """Fenced JSON stdout blocks extract inner summary without leaking backticks/braces, and generate-tests forbids uninstalled browser packages."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-clean-summary")
    plan = _sample_static_web_plan()

    fenced_stdout = (
        "Trabajo completado.\n\n"
        "```json\n"
        "{\n"
        '  "files_written": ["index.html", "src/app.js"],\n'
        '  "summary": "Construida la interfaz y lógica principal."\n'
        "}\n"
        "```\n"
    )
    nested_fence_summary = json.dumps(
        {
            "files_written": ["src/app.js"],
            "summary": 'Actualizado módulo.\n```json\n{"files_written": ["src/app.js"], "debug": 1}\n```',
        }
    )
    truncated_fenced_stdout = (
        "```json\n"
        '{\n  "files_written": ["src/app.js"],\n  "summary": "Parcial...'
    )
    plain_with_leaked_json = (
        'Hecho. {"files_written": ["src/app.js"], "summary": "Se corrigió el contador."}'
    )

    runner = FakeRunner(
        results=[
            # 1. Fenced JSON block (multiline)
            CommandResult(exit_code=0, stdout=fenced_stdout),
            # 2. Inner summary containing a nested ```json``` block
            CommandResult(exit_code=0, stdout=nested_fence_summary),
            # 3. Unclosed/truncated ```json``` fence in plain text fallback
            CommandResult(exit_code=0, stdout=truncated_fenced_stdout),
            # 4. Inline single-line JSON embedded in prose
            CommandResult(exit_code=0, stdout=plain_with_leaked_json),
            # 5. generate_tests response
            CommandResult(
                exit_code=0,
                stdout=(
                    "```python tests/test_selfcontained.py\n"
                    "def test_ok(): assert True\n"
                    "```\n"
                    "```json\n"
                    '{"files_written": ["tests/test_selfcontained.py"], "summary": "Pruebas listas"}\n'
                    "```"
                ),
            ),
        ]
    )
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=runner,
        cli_command=("agy",),
        retry_if_no_tests_written=False,
    )

    r1 = adapter.implement_or_fix(ws_dir, plan, attempt=1)
    assert r1.files_written == ["index.html", "src/app.js"]
    assert r1.summary == "Construida la interfaz y lógica principal."
    assert "```" not in r1.summary
    assert "files_written" not in r1.summary

    r2 = adapter.implement_or_fix(ws_dir, plan, attempt=2)
    assert r2.files_written == ["src/app.js"]
    assert r2.summary == "Actualizado módulo."
    assert "```" not in r2.summary
    assert "files_written" not in r2.summary

    r3 = adapter.implement_or_fix(ws_dir, plan, attempt=3)
    assert "```" not in r3.summary
    assert "files_written" not in r3.summary

    r4 = adapter.implement_or_fix(ws_dir, plan, attempt=4)
    assert r4.files_written == ["src/app.js"]
    assert r4.summary == "Se corrigió el contador."
    assert "files_written" not in r4.summary

    r_gen = adapter.generate_tests(ws_dir, plan.acceptance_tests)
    assert r_gen.files_written == ["tests/test_selfcontained.py"]
    assert r_gen.summary == "Pruebas listas"
    gen_cmd = runner.calls[-1]["command"]
    gen_prompt = gen_cmd[gen_cmd.index("-p") + 1]
    assert "pathlib" in gen_prompt
    assert "html.parser" in gen_prompt
    assert "playwright" in gen_prompt.lower()
    assert "selenium" in gen_prompt.lower()
    assert "standard library" in gen_prompt.lower()


def test_agy_cli_adapter_excludes_pycache_and_bytecode_files_and_handles_binary_stdout(
    tmp_path: Path,
) -> None:
    """generate_tests filters __pycache__/*.pyc/*.pyo from files_written, and SubprocessRunner handles invalid UTF-8 bytes safely."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-pyc-filter")
    plan = _sample_static_web_plan()

    cli_script = tmp_path / "emit_pyc_and_tests.py"
    cli_script.write_text(
        "import json, pathlib, sys\n"
        "ws = pathlib.Path.cwd()\n"
        "tdir = ws / 'tests'\n"
        "cache = tdir / '__pycache__'\n"
        "cache.mkdir(parents=True, exist_ok=True)\n"
        "(tdir / 'test_app.py').write_text('def test_at_01(): assert True\\n', encoding='utf-8')\n"
        "(cache / 'test_app.cpython-312-pytest-9.2.0.pyc').write_bytes(b'\\xcb\\x0d\\x0d\\x0a\\x00\\x00\\xff')\n"
        "(tdir / 'stale.pyo').write_bytes(b'\\xcb\\x0d\\x0d\\x0a')\n"
        "sys.stdout.buffer.write(json.dumps({\n"
        "    'files_written': [\n"
        "        'tests/test_app.py',\n"
        "        'tests/__pycache__/test_app.cpython-312-pytest-9.2.0.pyc',\n"
        "        'tests/stale.pyo',\n"
        "    ],\n"
        "    'summary': 'Tests and cache created',\n"
        "}).encode('utf-8'))\n",
        encoding="utf-8",
    )

    real_runner = SubprocessRunner(default_timeout_seconds=10.0)
    adapter = AgyCliAdapter(
        workspace_manager=ws_mgr,
        runner=real_runner,
        cli_command=[sys.executable, str(cli_script)],
    )

    res = adapter.generate_tests(ws_dir, plan.acceptance_tests)
    assert res.files_written == ["tests/test_app.py"]
    assert res.summary == "Tests and cache created"

    # Also verify SubprocessRunner decodes non-UTF8 bytes with replacement without UnicodeDecodeError
    raw_bytes_script = tmp_path / "raw_bytes_out.py"
    raw_bytes_script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'line ok \\xcb\\x0d\\xff end\\n')\n"
        "sys.stderr.buffer.write(b'err \\xfe\\xff detail\\n')\n",
        encoding="utf-8",
    )
    bin_out = real_runner.run(command=[sys.executable, str(raw_bytes_script)], cwd=ws_dir)
    assert bin_out.exit_code == 0
    assert "line ok" in bin_out.stdout
    assert "\ufffd" in bin_out.stdout
    assert "err" in bin_out.stderr


def test_agy_cli_adapter_implement_prompt_requires_reading_tests_without_modifying_them(
    tmp_path: Path,
) -> None:
    """implement_or_fix prompt instructs the agent to read tests/ alongside pytest output before returning empty files_written, while strictly forbidding modifications to tests/ (INV-13)."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    ws_dir = ws_mgr.create_workspace("proj-read-tests-prompt")
    plan = _sample_static_web_plan()
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout=json.dumps({"files_written": ["src/app.js"], "summary": "Aligned DOM"}),
            )
        ]
    )
    adapter = AgyCliAdapter(workspace_manager=ws_mgr, runner=runner, cli_command=("agy",))

    res = adapter.implement_or_fix(
        ws_dir,
        plan,
        failure_output="FAILED tests/test_app.py::test_start - AssertionError: missing #start-btn",
        attempt=2,
    )
    assert res.files_written == ["src/app.js"]

    cmd = runner.calls[0]["command"]
    prompt_impl = cmd[cmd.index("-p") + 1]
    lower_prompt = prompt_impl.lower()
    assert "read" in lower_prompt and "tests/" in lower_prompt
    assert "read-only" in lower_prompt
    assert "assertions" in lower_prompt
    assert "never" in lower_prompt and "modify" in lower_prompt and "tests/" in lower_prompt
    assert "inv-13" in lower_prompt








