"""Shared fixtures.

The mock app runs in-process on a background thread rather than as a subprocess:
the integration tests need to reach into `MEMBERS` and `FAULTS` directly to arm a
condition mid-run, which is the only way to exercise a session drop that happens
*between* two steps.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import mockapp.app as mockapp
from cua.evidence import EvidenceRecorder
from cua.escalation.lease import InterventionQueue, SessionLease
from cua.replay.engine import ReplayEngine, new_run_id
from cua.safety.policy import Allowlist, PolicyGate
from cua.schema.artifact import CapabilityArtifact
from cua.surface.session import bootstrap_session
from cua.surface.web import WebSurfaceAdapter

REPO = Path(__file__).resolve().parent.parent
ARTIFACT = REPO / "capabilities" / "member.savings_balance.read.v1.json"
ALLOWLIST = REPO / "config" / "allowlist.yaml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def app_url() -> str:
    port = _free_port()
    threading.Thread(
        target=lambda: mockapp.app.run(host="127.0.0.1", port=port, threaded=True),
        daemon=True,
    ).start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return base
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("mock app did not start")


@pytest.fixture(autouse=True)
def clean_app_state():
    """Every test starts from the pristine synthetic data and no armed faults."""
    mockapp.reset_state()
    yield
    mockapp.reset_state()


@pytest.fixture
def artifact(app_url) -> CapabilityArtifact:
    """The seeded capability, re-pointed at this run's ephemeral port."""
    art = CapabilityArtifact.model_validate_json(ARTIFACT.read_text())
    art.target.entry_url_template = f"{app_url}/servicing/"
    art.steps[0].action.url_template = f"{app_url}/servicing/"
    return art


@pytest.fixture
def allowlist(app_url) -> Allowlist:
    allow = Allowlist.load(str(ALLOWLIST), "acme-servicing-readonly")
    allow.url_patterns = [f"{app_url}/servicing/*", f"{app_url}/servicing"]
    allow.auth_url_patterns = [f"{app_url}/login"]
    return allow


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def replay(browser, artifact, allowlist, tmp_path, monkeypatch):
    """Build a ready-to-run engine against a freshly authenticated session.

    Returns a callable so a test can supply its own adapter subclass -- which is
    how the session-drop test arms a fault partway through the flow.
    """
    monkeypatch.setenv("SVC_OPERATOR_ID", "test-operator")
    monkeypatch.setenv("SVC_PASSWORD", "test-password")

    def _run(params: dict, adapter_cls=WebSurfaceAdapter, **engine_kwargs):
        page = browser.new_context().new_page()
        gate = PolicyGate(allowlist)
        gate.guard_navigation(page)
        entry = artifact.target.entry_url_template
        bootstrap_session(page, entry, artifact.target.app_id)
        engine = ReplayEngine(
            adapter_cls(page), gate,
            EvidenceRecorder(str(tmp_path), new_run_id("test")),
            SessionLease(), InterventionQueue(str(tmp_path / "interventions")),
            reauth=lambda: bootstrap_session(page, entry, artifact.target.app_id),
            **engine_kwargs,
        )
        return engine.replay(artifact, params), gate, engine

    return _run
