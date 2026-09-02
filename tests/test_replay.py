"""Deterministic replay against the live mock app.

These encode the three-way error taxonomy and the four defects fixed in the
`gap 0` commit. The session-drop test is the one to protect hardest: before the
fix, a mid-run re-authentication left the search box empty, the search returned
"no member found", and the run reported a confident MEMBER_NOT_FOUND for a
member that exists. A false business outcome is the worst thing this system can
emit -- it is indistinguishable, to the caller, from a real answer.
"""

from __future__ import annotations

import pytest

import mockapp.app as mockapp
from cua.schema.result import ReplayStatus
from cua.surface.web import WebSurfaceAdapter

pytestmark = pytest.mark.integration


class TestHappyPath:
    def test_returns_the_declared_output(self, replay):
        result, _, _ = replay({"member_id": "12345"})
        assert result.status is ReplayStatus.SUCCESS
        assert result.outputs == {"savings_balance": 4812.55}

    def test_same_artifact_different_parameter(self, replay):
        """Parameterisation, not a hardcoded flow: the whole premise of replay."""
        result, _, _ = replay({"member_id": "23456"})
        assert result.status is ReplayStatus.SUCCESS
        assert result.outputs == {"savings_balance": 231.09}

    def test_every_step_records_how_it_resolved(self, replay):
        result, _, _ = replay({"member_id": "12345"})
        assert [s.resolved_by for s in result.steps] == [
            "url", "text_anchor", "role_name", "table_cell"]
        assert all(s.fallback_depth == 0 for s in result.steps)
        assert not result.drift_signals

    def test_no_policy_violations_on_a_clean_run(self, replay):
        _, gate, _ = replay({"member_id": "12345"})
        assert gate.violations == []


class TestBusinessOutcome:
    def test_unknown_member_is_an_answer_not_a_crash(self, replay):
        result, _, _ = replay({"member_id": "99999"})
        assert result.status is ReplayStatus.BUSINESS_OUTCOME
        assert result.outcome_code == "MEMBER_NOT_FOUND"
        assert result.failure is None

    def test_app_side_validation_error_is_an_outcome(self, replay):
        """The *app* rejecting a well-formed input, not the caller sending junk.

        A member number that fails the declared `pattern` never reaches the
        surface (see TestInputValidation); this covers the other case, where the
        vendor app applies a rule the artifact does not model.
        """
        mockapp.FAULTS["validation"] = True
        result, _, _ = replay({"member_id": "12345"})
        assert result.status is ReplayStatus.BUSINESS_OUTCOME
        assert result.outcome_code == "INVALID_MEMBER_NUMBER"


class TestSessionDrop:
    """A session that dies mid-flow must never produce a business outcome."""

    def test_reauth_restarts_the_flow_and_returns_the_right_answer(self, replay):
        class DropsOnFirstClick(WebSurfaceAdapter):
            armed = False

            def click(self, res):
                if not DropsOnFirstClick.armed:
                    DropsOnFirstClick.armed = True
                    mockapp.FAULTS["session_timeout"] = True
                return super().click(res)

        result, gate, _ = replay({"member_id": "12345"},
                                 adapter_cls=DropsOnFirstClick)

        assert result.status is ReplayStatus.SUCCESS
        assert result.outputs == {"savings_balance": 4812.55}, (
            "a dropped session must not be answered with a business outcome")
        # The record tells the truth about how the answer was obtained.
        assert [s.step_id for s in result.steps] == ["s1", "s2", "s3",
                                                     "s1", "s2", "s3", "s4"]
        assert "session_expired" in result.steps[2].conditions_fired
        # Re-authentication goes through auth_url_patterns, not around the guard.
        assert gate.violations == []

    def test_a_session_that_keeps_dropping_fails_rather_than_looping(self, replay):
        class AlwaysDrops(WebSurfaceAdapter):
            def click(self, res):
                mockapp.FAULTS["session_timeout"] = True
                return super().click(res)

        result, _, _ = replay({"member_id": "12345"}, adapter_cls=AlwaysDrops)
        assert result.status is ReplayStatus.FAILED
        assert result.failure is not None
        assert result.failure.stage == "recovery"


class TestKnownConditions:
    def test_maintenance_interstitial_is_dismissed_in_band(self, replay):
        mockapp.FAULTS["interstitial"] = True
        result, _, _ = replay({"member_id": "12345"})
        assert result.status is ReplayStatus.SUCCESS
        assert result.outputs == {"savings_balance": 4812.55}

    def test_app_error_is_a_hard_failure_with_a_debuggable_detail(self, replay):
        mockapp.FAULTS["app_error"] = True
        result, _, _ = replay({"member_id": "12345"})
        assert result.status is ReplayStatus.FAILED
        assert result.failure is not None
        assert result.failure.step_id and result.failure.observed


class TestInputValidation:
    @pytest.mark.parametrize("params", [{}, {"member_id": "abc"}])
    def test_parameters_are_checked_before_the_surface_is_touched(self, replay, params):
        """Missing and malformed both fail at intake, not half-way through a form."""
        result, _, _ = replay(params)
        assert result.status is ReplayStatus.FAILED
        assert result.failure.stage == "validation"
        assert result.steps == []


class TestEvidenceDescribesExactlyOneRun:
    """`run.jsonl` was append-mode and the directory was never cleared.

    Re-running a fixed run-id — which tools/demo.sh does on purpose — left the
    log holding every attempt while result.json held only the last, so the two
    disagreed about what happened. Screenshots from failed attempts sat beside
    successful results, and `_seq` restarted at zero so the attempts could not
    be separated. Six demo runs produced one directory claiming to be one run.
    """

    def test_a_reused_run_id_does_not_accumulate(self, tmp_path):
        from cua.evidence import EvidenceRecorder

        first = EvidenceRecorder(tmp_path, "rep_fixed_id")
        first.log("started", attempt="one")
        first.snapshot("failure_s1", "tree from the failed attempt")
        assert (first.dir / "failure_s1.a11y.txt").exists()

        second = EvidenceRecorder(tmp_path, "rep_fixed_id")
        second.log("started", attempt="two")

        lines = (second.dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, "the previous attempt's log survived"
        assert "two" in lines[0] and "one" not in lines[0]
        assert not (second.dir / "failure_s1.a11y.txt").exists(), \
            "a failed attempt's snapshot outlived the run it belonged to"
