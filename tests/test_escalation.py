"""Escalation is a pause, not a terminus (DESIGN §10, Join 2).

The distinction the brief cares about: after a handback the run must "resume or
complete". So a *resolved* escalation returns SUCCESS or BUSINESS_OUTCOME like
any other run, carrying an EscalationRecord that says how the answer was really
obtained. ESCALATED means only one thing — nobody resolved it, and a person
still has to deal with this run.

`ok_for_caller` is the load-bearing consequence: a caller that paused for a
human and got its answer has succeeded, and must not be told otherwise.
"""

from __future__ import annotations

import pytest

import mockapp.app as mockapp
from cua.escalation.lease import InterventionQueue, SessionLease
from cua.evidence import EvidenceRecorder
from cua.replay.engine import ReplayEngine, new_run_id
from cua.safety.policy import Allowlist, PolicyGate
from cua.schema.result import ReplayStatus
from cua.surface.session import bootstrap_session
from cua.surface.web import WebSurfaceAdapter
from tests.test_discovery import build as build_write_capability

pytestmark = pytest.mark.integration


@pytest.fixture
def write_engine(browser, app_url, tmp_path, monkeypatch):
    """Engine bound to the sub-account capability, whose last step is a post."""
    monkeypatch.setenv("SVC_OPERATOR_ID", "op")
    monkeypatch.setenv("SVC_PASSWORD", "pw")

    def _make(on_escalation=None):
        allow = Allowlist.load("config/allowlist.yaml", "acme-servicing-write")
        allow.url_patterns = [f"{app_url}/servicing/*"]
        allow.auth_url_patterns = [f"{app_url}/login"]
        page = browser.new_context().new_page()
        gate = PolicyGate(allow)
        gate.guard_navigation(page)
        bootstrap_session(page, f"{app_url}/servicing/", "acme-servicing")
        lease = SessionLease()
        engine = ReplayEngine(
            WebSurfaceAdapter(page), gate,
            EvidenceRecorder(str(tmp_path), new_run_id("test")),
            lease, InterventionQueue(str(tmp_path / "iv")),
            on_escalation=on_escalation,
        )
        return engine, lease, build_write_capability(app_url)

    return _make


def _operator_authorises(lease):
    """Stands in for a human at the console taking and returning the lease."""
    def handler(req):
        lease.operator_take_control()
        lease.operator_hand_back("reviewed the transaction and authorised it")
        return True
    return handler


class TestUnresolved:
    def test_nobody_answering_is_the_only_escalated_case(self, write_engine):
        engine, _, art = write_engine(on_escalation=None)
        result = engine.replay(art, {"member_id": "12345"})

        assert result.status is ReplayStatus.ESCALATED
        assert result.ok_for_caller is False
        assert len(result.escalations) == 1
        assert result.escalations[0].resolved is False
        assert result.escalations[0].trigger == "risk_gate"
        # Nothing was posted.
        assert len(mockapp.MEMBERS["12345"]["accounts"]) == 3


class TestResolved:
    def test_the_run_completes_and_says_how(self, write_engine):
        engine, lease, art = write_engine()
        engine._on_escalation = _operator_authorises(lease)

        result = engine.replay(art, {"member_id": "12345"})

        assert result.status is ReplayStatus.SUCCESS
        assert result.ok_for_caller is True, (
            "a run that paused for a human and finished got its answer")
        assert result.status is not ReplayStatus.ESCALATED

        # The result still tells the truth about how it was obtained.
        assert len(result.escalations) == 1
        record = result.escalations[0]
        assert record.resolved is True
        assert record.trigger == "risk_gate"
        assert record.step_id == "s7"
        assert record.escalation_id

    def test_the_authorised_action_actually_ran(self, write_engine):
        """Authorising must mean the post happened, not that it was skipped."""
        engine, lease, art = write_engine()
        engine._on_escalation = _operator_authorises(lease)

        engine.replay(art, {"member_id": "12345"})

        accounts = mockapp.MEMBERS["12345"]["accounts"]
        assert len(accounts) == 4
        assert accounts[-1] == ("Savings", "S0004", "250.00")
        assert accounts[0][2] == "954.18"  # 1,204.18 debited by 250.00

    def test_what_the_human_did_is_recorded(self, write_engine):
        engine, lease, art = write_engine()
        engine._on_escalation = _operator_authorises(lease)

        result = engine.replay(art, {"member_id": "12345"})

        # Diffed from the surface, not replayed keystrokes -- keeps PII out.
        assert result.escalations[0].human_action_summary is not None
        assert result.escalations[0].held_lease_ms >= 0

    def test_authorisation_does_not_widen_the_allowlist(self, write_engine):
        """Scoped to one step of one run; the gate itself is untouched."""
        engine, lease, art = write_engine()
        engine._on_escalation = _operator_authorises(lease)
        engine.replay(art, {"member_id": "12345"})

        assert engine._authorised == {"s7"}
        assert engine.gate.allowlist.max_risk_unattended.value == "risky"


class TestAuditChain:
    def test_the_result_names_the_discovery_that_produced_the_capability(
            self, write_engine):
        """Join 3: walkable from a production result back to its origin."""
        engine, _, art = write_engine(on_escalation=None)
        result = engine.replay(art, {"member_id": "12345"})

        assert art.provenance.discovery_run_id
        assert result.discovery_run_id == art.provenance.discovery_run_id

    def test_the_chain_survives_every_terminal_status(self, write_engine, app_url):
        engine, lease, art = write_engine()
        engine._on_escalation = _operator_authorises(lease)
        success = engine.replay(art, {"member_id": "12345"})
        assert success.status is ReplayStatus.SUCCESS
        assert success.discovery_run_id == art.provenance.discovery_run_id

        engine2, _, art2 = write_engine(on_escalation=None)
        failed = engine2.replay(art2, {"member_id": "abc"})
        assert failed.status is ReplayStatus.FAILED
        assert failed.discovery_run_id == art2.provenance.discovery_run_id
