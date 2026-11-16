"""Tests for test-lock SHA-256 verification and isolated test generation (AC-2.2, A2a-5, INV-13, INV-23)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.builder import BuildWorker
from agent_factory.domain.models import AcceptanceTest, AcceptanceTestType, ApprovalEvent
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.workspace import (
    SandboxViolationError,
    WorkspaceManager,
    compute_test_lock,
    verify_and_restore_test_lock,
)

# Hand-written SHA-256 digests for INV-23 independent oracle verification
EXPECTED_TESTS_SHA256 = "4d6234384596d3ed34f51acffcee52f1a915baa86aa4bab44da9a3cc20fc1a27"
EXPECTED_RUNNER_CONFIG_SHA256 = "9326c489d349f53d53f5ba02de2beb0f215c738443f75a6a178ec36bfd152617"
EXPECTED_COMBINED_SHA256 = "428fcd60237bf433328c3f44d987500073d19ecc5a3e68f77fcdc0854e53a19e"


def _approved_project_orchestrator(project_id: str = "proj-lock-01") -> Orchestrator:
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Static unit converter",
                    "users": ["Gabriel"],
                    "features": ["Convert meters to km"],
                    "business_rules": ["Round to 4 decimals"],
                    "edge_cases": ["Zero"],
                    "constraints": ["Static web"],
                    "success_criteria": ["Instant"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {
                "approach": "Vanilla TS",
                "components": ["converter.ts"],
                "tradeoffs": ["Static"],
            },
            {
                "critique": ["Precision"],
                "robust_proposal": "Strict number check",
                "architectural_safeguards": ["Input guard"],
            },
            {
                "summary": "Unit converter",
                "rationale": "Simple and safe",
                "template_id": "static-app",
                "files": [
                    {
                        "path": "src/converter.ts",
                        "purpose": "Conversion",
                        "contract": "export function toKm(m: number): number",
                        "depends_on": [],
                    }
                ],
                "acceptance_tests": [
                    {
                        "id": "AT-01",
                        "description": "1000m converts to 1km",
                        "type": "unit",
                    },
                    {
                        "id": "AT-02",
                        "description": "Renders without console.error",
                        "type": "e2e",
                    },
                ],
                "risks": ["None"],
                "deployment_target": "github-pages",
            },
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    orchestrator.create_project(project_id=project_id, name="Converter", objective="Static unit converter")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete converter")
    waiting = orchestrator.run_debate(project_id)
    assert waiting.plan_hash is not None
    orchestrator.submit_approval(
        project_id,
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 15, 0, tzinfo=UTC),
            decision="approved",
        ),
    )
    return orchestrator


def test_compute_and_verify_test_lock_matches_handwritten_sha256(tmp_path: Path) -> None:
    """AC-2.2 (INV-13, INV-23): compute_test_lock produces exact deterministic SHA-256 hashes matching hand-written oracle."""
    ws_dir = tmp_path / "ws-oracle"
    (ws_dir / "tests").mkdir(parents=True, exist_ok=True)
    (ws_dir / "tests" / "test_at_01.py").write_text(
        "def test_at_01():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    (ws_dir / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )

    snapshot = compute_test_lock(ws_dir)

    assert snapshot.tests_hash == EXPECTED_TESTS_SHA256
    assert snapshot.runner_config_hash == EXPECTED_RUNNER_CONFIG_SHA256
    assert snapshot.combined_hash == EXPECTED_COMBINED_SHA256
    assert verify_and_restore_test_lock(ws_dir, snapshot) is True


def test_verify_and_restore_test_lock_detects_and_reverts_test_and_config_modifications(
    tmp_path: Path,
) -> None:
    """AC-2.2 (INV-13): Tampering with test files, runner config, deleting tests, or adding rogue tests is detected and reverted."""
    ws_dir = tmp_path / "ws-tamper"
    (ws_dir / "tests").mkdir(parents=True, exist_ok=True)
    original_test = "def test_at_01():\n    assert 1 + 1 == 2\n"
    original_cfg = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'

    test_file = ws_dir / "tests" / "test_at_01.py"
    cfg_file = ws_dir / "pyproject.toml"
    test_file.write_text(original_test, encoding="utf-8")
    cfg_file.write_text(original_cfg, encoding="utf-8")

    snapshot = compute_test_lock(ws_dir)

    # 1. Modify existing test file
    test_file.write_text("def test_at_01():\n    assert 1 == 1\n", encoding="utf-8")
    assert verify_and_restore_test_lock(ws_dir, snapshot) is False
    assert test_file.read_text(encoding="utf-8") == original_test
    assert verify_and_restore_test_lock(ws_dir, snapshot) is True

    # 2. Modify runner config file
    cfg_file.write_text('[tool.pytest.ini_options]\naddopts = "-k nothing"\n', encoding="utf-8")
    assert verify_and_restore_test_lock(ws_dir, snapshot) is False
    assert cfg_file.read_text(encoding="utf-8") == original_cfg
    assert verify_and_restore_test_lock(ws_dir, snapshot) is True

    # 3. Delete test file
    test_file.unlink()
    assert verify_and_restore_test_lock(ws_dir, snapshot) is False
    assert test_file.is_file()
    assert test_file.read_text(encoding="utf-8") == original_test

    # 4. Add rogue file inside tests/
    rogue_file = ws_dir / "tests" / "conftest.py"
    rogue_file.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    assert verify_and_restore_test_lock(ws_dir, snapshot) is False
    assert not rogue_file.exists()


def test_build_worker_detects_test_or_config_tampering_reverts_and_counts_attempt_as_failed(
    tmp_path: Path,
) -> None:
    """AC-2.2 (INV-13): When FakeCodeAgent modifies a test or runner config, BuildWorker reverts it, skips runner/push, and counts the attempt as failed."""
    project_id = "proj-tamper-loop"
    orchestrator = _approved_project_orchestrator(project_id)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    original_test_code = "def test_at_01(): assert convert(1000) == 1\ndef test_at_02(): assert True\n"
    original_runner_cfg = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_acceptance.py": original_test_code},
        ],
        impl_steps=[
            # Attempt 1: tries to weaken the test file
            {
                "src/converter.ts": "export const bad = 1;",
                "tests/test_acceptance.py": "def test_at_01(): pass\n",
            },
            # Attempt 2: tries to alter runner config pyproject.toml
            {
                "src/converter.ts": "export const bad = 2;",
                "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["empty"]\n',
            },
            # Attempt 3: legitimate implementation without touching tests or runner config
            {
                "src/converter.ts": "export function toKm(m: number): number { return m / 1000; }\n",
            },
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            )
        ]
    )
    fake_vcs = FakeVCS(
        ci_results=[
            CIRunResult(passed=True, summary="CI passed"),
        ]
    )

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": original_runner_cfg},
    )

    ws_dir = ws_mgr.get_workspace_dir(project_id)
    # 1 test generation + 2 tampered attempts + 1 clean attempt = 4 invocations
    assert report.invocations == 4
    assert report.ci_rounds == 1
    assert report.failure_cause is None
    assert report.test_results == {"AT-01": True, "AT-02": True}
    # Runner was only invoked once (for the 3rd clean attempt, never on the 2 tampered attempts)
    assert len(fake_runner.calls) == 1
    assert len(fake_vcs.pushes) == 1
    assert (ws_dir / "tests" / "test_acceptance.py").read_text(encoding="utf-8") == original_test_code
    assert (ws_dir / "pyproject.toml").read_text(encoding="utf-8") == original_runner_cfg
    assert orchestrator.get_status(project_id).state == ProjectState.DELIVERED


def test_test_generation_step_receives_only_acceptance_tests_without_production_code(
    tmp_path: Path,
) -> None:
    """A2a-5 (INV-13): FakeCodeAgent.generate_tests receives ONLY acceptance_tests and sees zero production files."""
    project_id = "proj-blind-testgen"
    orchestrator = _approved_project_orchestrator(project_id)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_acceptance.py": "def test_all(): pass\n"},
        ],
        impl_steps=[
            {"src/converter.ts": "export function toKm(m: number): number { return m / 1000; }\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            )
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="All green")])

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    assert len(fake_agent.test_gen_calls) == 1
    call_record: dict[str, Any] = fake_agent.test_gen_calls[0]
    assert "plan" not in call_record
    assert "files" not in call_record
    assert call_record["production_files_seen"] == []
    assert call_record["acceptance_tests"] == [
        AcceptanceTest(id="AT-01", description="1000m converts to 1km", type=AcceptanceTestType.UNIT),
        AcceptanceTest(id="AT-02", description="Renders without console.error", type=AcceptanceTestType.E2E),
    ]


def test_test_generation_rejects_preexisting_production_code_or_missing_tests(
    tmp_path: Path,
) -> None:
    """A2a-5 negative (INV-13): Test generation fails if production code in src/ already exists or if no tests are generated."""
    # Case 1: Pre-existing production file in src/ contaminates test generation
    project_contaminated = "proj-contaminated-src"
    orchestrator_1 = _approved_project_orchestrator(project_contaminated)
    ws_mgr_1 = WorkspaceManager(base_dir=tmp_path / "ws1")
    fake_agent_1 = FakeCodeAgent(
        test_gen_steps=[{"tests/test_acceptance.py": "def test_1(): pass\n"}],
        impl_steps=[{"src/converter.ts": "export const x = 1;\n"}],
    )
    worker_1 = BuildWorker(
        orchestrator=orchestrator_1,
        workspace_manager=ws_mgr_1,
        code_agent=fake_agent_1,
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    with pytest.raises((SandboxViolationError, ValueError)):
        worker_1.run_job(
            project_id=project_contaminated,
            template_files={"src/converter.ts": "export const leaked = true;\n"},
        )
    assert len(fake_agent_1.test_gen_calls) == 0

    # Case 2: Test generator produces zero test files in tests/
    project_no_tests = "proj-empty-testgen"
    orchestrator_2 = _approved_project_orchestrator(project_no_tests)
    ws_mgr_2 = WorkspaceManager(base_dir=tmp_path / "ws2")
    fake_agent_2 = FakeCodeAgent(
        test_gen_steps=[{}],
        impl_steps=[{"src/converter.ts": "export const x = 1;\n"}],
    )
    worker_2 = BuildWorker(
        orchestrator=orchestrator_2,
        workspace_manager=ws_mgr_2,
        code_agent=fake_agent_2,
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    report_2 = worker_2.run_job(project_id=project_no_tests)
    assert orchestrator_2.get_status(project_no_tests).state == ProjectState.FAILED
    assert report_2.failure_cause is not None
    assert len(fake_agent_2.impl_calls) == 0
