"""Tests for AgyCliAdapter and end-to-end offline integration with TemplateScaffold and BuildWorker (A2b-3, A2b-4, A2b-5, A2b-6, INV-06, INV-09, INV-13, INV-18, INV-19, INV-23)."""

import json
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
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker, JobState, LocalJobQueue
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
        "mode = args[args.index('--mode') + 1]\n"
        "payload = json.loads(args[args.index('--payload') + 1])\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
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
        "payload = json.loads(args[args.index('--payload') + 1])\n"
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
        runner=SubprocessRunner(default_timeout_seconds=0.4),
        cli_command=[sys.executable, str(cli_script)],
        timeout_seconds=0.4,
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
    with pytest.raises((CommandTimeoutError, CodeAgentError)) as timeout_exc:
        adapter.implement_or_fix(ws_dir, plan, failure_output="", attempt=3)
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
        "mode = args[args.index('--mode') + 1]\n"
        "payload = json.loads(args[args.index('--payload') + 1])\n"
        "ws = pathlib.Path.cwd()\n"
        "if mode == 'generate-tests':\n"
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
