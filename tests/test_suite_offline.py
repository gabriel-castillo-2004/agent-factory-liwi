"""Tests for offline suite execution and FakeLLM contract (AC-1.11, INV-02, INV-03)."""

import socket

import pytest
import pytest_socket

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.agents.pragmatist import PragmatistProposal


def test_fake_llm_and_domain_pipeline_run_completely_offline() -> None:
    """AC-1.11: Suite executes with network blocked and FakeLLM completes offline."""
    with pytest.raises(pytest_socket.SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    fake_llm = FakeLLM(
        responses=[
            {
                "approach": "Offline deterministic execution",
                "components": ["domain", "agents"],
                "tradeoffs": ["Scripted responses only"],
            }
        ]
    )
    response = fake_llm.complete(
        messages=[{"role": "user", "content": "Propose offline architecture"}],
        schema=PragmatistProposal,
    )
    assert response["approach"] == "Offline deterministic execution"
    assert len(fake_llm.calls) == 1
