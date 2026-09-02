"""Verification replay (DESIGN §10, Join 1).

The case this exists for: the sub-account receipt shows a posting timestamp, so
a checkpoint synthesized from that screen passes on the run that created it and
fails on every run afterwards. Without verification the capability reaches
`capabilities/`, gets invoked in production, and fails the first time a caller
uses it. With it, the artifact never leaves `evidence/` and the report names the
offending string.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import mockapp.app as mockapp
from cua.agent.recorder import (
    capability_path,
    requires_sandbox,
    verify_and_store,
)
from cua.escalation.lease import InterventionQueue, SessionLease
from cua.evidence import EvidenceRecorder
from cua.replay.engine import ReplayEngine, new_run_id
from cua.safety.policy import Allowlist, PolicyGate
from cua.schema.artifact import ApprovalState, Checkpoint, Detector, RiskClass
from cua.schema.result import ReplayResult, ReplayStatus
from cua.surface.session import bootstrap_session
from cua.surface.web import WebSurfaceAdapter
from tests.test_discovery import build as build_write_capability

pytestmark = pytest.mark.integration


@pytest.fixture
def recorder(browser, app_url, tmp_path, monkeypatch):
    """A verification harness over the same live session, as the CLI builds it."""
    monkeypatch.setenv("SVC_OPERATOR_ID", "op")
    monkeypatch.setenv("SVC_PASSWORD", "pw")

    def _make():
        allow = Allowlist.load("config/allowlist.yaml", "acme-servicing-write")
        allow.url_patterns = [f"{app_url}/servicing/*"]
        allow.auth_url_patterns = [f"{app_url}/login"]
        page = browser.new_context().new_page()
        gate = PolicyGate(allow, attended=True)
        gate.guard_navigation(page)
        bootstrap_session(page, f"{app_url}/servicing/", "acme-servicing")
        engine = ReplayEngine(
            WebSurfaceAdapter(page), gate,
            EvidenceRecorder(str(tmp_path), new_run_id("verify")),
            SessionLease(), InterventionQueue(str(tmp_path / "iv")),
            on_escalation=lambda req: True,
        )
        return engine, build_write_capability(app_url)

    return _make


class TestStoragePath:
    def test_nested_by_app_and_capability_and_version(self, app_url):
        art = build_write_capability(app_url)
        path = capability_path("capabilities", art)
        assert path.as_posix().endswith(
            "capabilities/acme-servicing/member.subaccount.open/v1.json")

    def test_versions_sit_beside_each_other(self, app_url):
        """Artifacts are immutable; re-recording writes v2 next to v1."""
        art = build_write_capability(app_url)
        v1 = capability_path("capabilities", art)
        art.version = 2
        v2 = capability_path("capabilities", art)
        assert v1.parent == v2.parent and v1 != v2


class TestVerificationPasses:
    def test_a_replayable_artifact_is_stored_and_marked_verified(
            self, recorder, tmp_path):
        engine, art = recorder()
        assert art.approval_state is ApprovalState.DRAFT

        outcome = verify_and_store(
            art, {"member_id": "12345"}, replay=engine.replay,
            capabilities_root=tmp_path / "caps",
            reset_app=mockapp.reset_state)

        assert outcome.ok
        assert art.approval_state is ApprovalState.DRAFT_VERIFIED
        assert outcome.artifact_path.exists()
        stored = json.loads(outcome.artifact_path.read_text(encoding="utf-8"))
        assert stored["approval_state"] == "draft_verified"

    def test_stability_gets_its_first_data_point(self, recorder, tmp_path):
        engine, art = recorder()
        verify_and_store(art, {"member_id": "12345"}, replay=engine.replay,
                         capabilities_root=tmp_path / "caps",
                         reset_app=mockapp.reset_state)
        assert art.stability.replay_count == 1
        assert art.stability.success_count == 1
        assert isinstance(art.stability.last_verified_at, datetime)


class TestVolatileCheckpointIsCaught:
    """The whole reason Join 1 exists."""

    def test_a_timestamp_checkpoint_never_reaches_capabilities(
            self, recorder, tmp_path):
        engine, art = recorder()
        # Exactly what checkpoint synthesis would derive from the receipt: the
        # screen genuinely did change to show this, and it will never say it
        # again.
        art.success = Checkpoint(
            detectors=[Detector(kind="text_present",
                                value="Posted 2026-08-27 04:11:52")],
            description="Goal state reached: the receipt is visible")

        outcome = verify_and_store(
            art, {"member_id": "12345"}, replay=engine.replay,
            capabilities_root=tmp_path / "caps",
            reset_app=mockapp.reset_state)

        assert not outcome.ok
        assert outcome.artifact_path is None
        assert not (tmp_path / "caps").exists(), (
            "an unverified artifact must not be written to capabilities/")
        assert art.approval_state is ApprovalState.DRAFT

    def test_the_failure_names_the_offending_string(self, recorder, tmp_path):
        engine, art = recorder()
        art.success = Checkpoint(
            detectors=[Detector(kind="text_present",
                                value="Posted 2026-08-27 04:11:52")],
            description="receipt visible")

        outcome = verify_and_store(
            art, {"member_id": "12345"}, replay=engine.replay,
            capabilities_root=tmp_path / "caps",
            reset_app=mockapp.reset_state)

        assert outcome.volatile_suspects
        assert any("Posted" in s for s in outcome.volatile_suspects)
        assert "success_condition" in outcome.reason

    def test_stability_records_the_failure_too(self, recorder, tmp_path):
        engine, art = recorder()
        art.success = Checkpoint(
            detectors=[Detector(kind="text_present", value="Confirmation 9999-C")],
            description="receipt visible")
        verify_and_store(art, {"member_id": "12345"}, replay=engine.replay,
                         capabilities_root=tmp_path / "caps",
                         reset_app=mockapp.reset_state)
        assert art.stability.replay_count == 1
        assert art.stability.success_count == 0


class TestIrreversibleVerification:
    def test_a_write_capability_is_flagged_as_needing_a_reset(self, app_url):
        assert requires_sandbox(build_write_capability(app_url)) is True

    def test_a_read_capability_is_not(self, artifact):
        assert all(s.risk is RiskClass.SAFE_REVERSIBLE for s in artifact.steps)
        assert requires_sandbox(artifact) is False

    def test_reset_runs_before_the_verification_replay(self, recorder, tmp_path):
        """Otherwise verifying a write capability posts a second transaction."""
        engine, art = recorder()
        # Dirty the app the way a discovery run just did.
        mockapp.MEMBERS["12345"]["accounts"].append(("Savings", "S0004", "250.00"))

        outcome = verify_and_store(
            art, {"member_id": "12345"}, replay=engine.replay,
            capabilities_root=tmp_path / "caps",
            reset_app=mockapp.reset_state)

        assert outcome.ok
        # Reset to 3, then the verification replay opened exactly one.
        assert len(mockapp.MEMBERS["12345"]["accounts"]) == 4


class TestCheckpointMustNotAssertItsOwnAnswer:
    """Verification replays with the *same* parameters the recording used, so a
    green result says nothing about whether the capability generalises.

    Run disc_473312af7b verified clean and was stored, then failed on the very
    next step of the demo: its success checkpoint was the literal text
    "Savings S0002 $4,812.55" — member 12345's balance. Replayed for member
    23456 the assertion could not hold. A checkpoint asserting the answer is a
    recording of one member wearing a capability's clothes, and the one moment
    the values are known is straight after the verification replay.
    """

    @staticmethod
    def _replay_returning(outputs):
        def fake(art, params):
            return ReplayResult(
                status=ReplayStatus.SUCCESS,
                capability_id=art.capability_id,
                capability_version=art.version,
                run_id="verify_stub",
                outputs=outputs,
                started_at=datetime.now(timezone.utc),
            )
        return fake

    def test_asserting_a_returned_value_is_refused(self, artifact, tmp_path):
        artifact.success = Checkpoint(
            detectors=[Detector(kind="text_present",
                                value="Savings S0002 $4,812.55")],
            description="Goal state reached",
        )
        caps = tmp_path / "caps"

        outcome = verify_and_store(
            artifact, {"member_id": "12345"},
            replay=self._replay_returning({"savings_balance": "$4,812.55"}),
            capabilities_root=caps)

        assert not outcome.ok
        assert "replay only for the recorded input" in outcome.reason
        assert not caps.exists(), "a hardcoded flow reached capabilities/"
        assert artifact.approval_state is not ApprovalState.DRAFT_VERIFIED

    def test_a_structural_checkpoint_is_accepted(self, artifact, tmp_path):
        artifact.success = Checkpoint(
            detectors=[Detector(kind="text_present", value="Account Summary")],
            description="Goal state reached",
        )
        caps = tmp_path / "caps"

        outcome = verify_and_store(
            artifact, {"member_id": "12345"},
            replay=self._replay_returning({"savings_balance": "$4,812.55"}),
            capabilities_root=caps)

        assert outcome.ok
        assert outcome.artifact_path.exists()
