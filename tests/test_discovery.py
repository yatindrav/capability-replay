"""Recording → artifact, without the model in the loop.

The discovery *loop* is not tested end to end: it is nondeterministic and costs
API calls, and the brief covers it with recorded evidence instead. What is
tested here is everything downstream of the model's choices — the distillation
that turns a list of accepted tool calls into a typed capability, and whether
the artifact it emits actually replays.

The recording below is the sub-account write flow, hand-written in exactly the
shape `DiscoveryAgent._record` produces. It exists because that flow is what
proved the old vocabulary was too small: two `<select>` controls the agent had
no tool for, and an irreversible post it would have classified by keyword.
"""

from __future__ import annotations

import pytest

import mockapp.app as mockapp
from cua.agent.discovery import (
    DiscoveryAgent,
    DiscoveryRequest,
    build_artifact,
    resolve_goal,
)
from cua.escalation.lease import InterventionQueue, SessionLease
from cua.evidence import EvidenceRecorder
from cua.replay.engine import ReplayEngine, new_run_id
from cua.safety.policy import Allowlist, PolicyGate
from cua.schema.artifact import ParamSpec, RiskClass
from cua.schema.result import ReplayStatus
from cua.surface.session import bootstrap_session
from cua.surface.web import WebSurfaceAdapter

MEMBER_SPEC = [ParamSpec(name="member_id", type="string",
                         description="Credit-union member number.",
                         pattern=r"^\d+$")]


def _inp(**kw):
    """Tool input as the model would supply it.

    No `frame`: navigating straight to the deep sub-account URL renders the form
    as the top-level document, so there is no frameset to scope into. The read
    capability covers frame-scoped addressing.
    """
    return {"robustness_note": "vendor-fixed label", **kw}


def subaccount_recording(base_url: str, member_id: str) -> dict:
    """What a successful discovery run over the write flow records."""
    return {
        "ok": True,
        "success_text": "Sub-Account Opened",
        "summary": "Opened a funded sub-account.",
        "parameters": {"member_id": member_id},
        "recorded": [
            {"tool": "navigate", "risk": "safe_reversible", "control": None,
             "input": {"url": f"{base_url}/servicing/subaccount?mid={member_id}",
                       "intent": "Open the sub-account form for the member"}},
            {"tool": "select_option", "risk": "safe_reversible",
             "control": {"role": "combobox", "near_text": "Account Type", "fallbacks": [
                             {"strategy": "text_anchor", "value": "after=Account Type",
                              "confidence": 0.6}]},
             "input": _inp(role="combobox", value="Savings",
                                    near_text="Account Type",
                                    intent="Choose the account type")},
            {"tool": "type_text", "risk": "safe_reversible",
             "control": {"role": "textbox", "near_text": "Nickname", "fallbacks": [
                             {"strategy": "text_anchor", "value": "after=Nickname",
                              "confidence": 0.6}]},
             "input": _inp(role="textbox", text="Vacation Fund",
                                    near_text="Nickname",
                                    intent="Name the new sub-account")},
            {"tool": "type_text", "risk": "safe_reversible",
             "control": {"role": "textbox", "near_text": "Opening Deposit", "fallbacks": [
                             {"strategy": "text_anchor", "value": "after=Opening Deposit",
                              "confidence": 0.6}]},
             "input": _inp(role="textbox", text="250.00",
                                    near_text="Opening Deposit",
                                    intent="Fund the opening deposit")},
            {"tool": "click", "risk": "safe_reversible",
             "control": {"role": "button", "name": "Continue"},
             "input": _inp(role="button", name="Continue",
                                    intent="Go to the review screen")},
            {"tool": "assert_state", "risk": "safe_reversible", "control": None,
             "input": {"text": "Review and Post",
                       "intent": "Prove we reached the last reversible screen"}},
            # The step the whole exercise exists for.
            {"tool": "click", "risk": "irreversible",
             "control": {"role": "button", "name": "Post"},
             "input": _inp(role="button", name="Post",
                                    intent="Post the transaction")},
        ],
    }


def build(base_url: str, member_id: str = "12345", allowlist_id="acme-servicing-write"):
    return build_artifact(
        capability_id="member.subaccount.open",
        title="Open a funded sub-account",
        description="Opens a sub-account for a member, funded from an existing account.",
        goal="open a sub-account for member {member_id}",
        entry_url=f"{base_url}/servicing/",
        app_id="acme-servicing",
        allowlist_id=allowlist_id,
        discovery=subaccount_recording(base_url, member_id),
        model="test",
        run_id="disc_test",
        param_specs=MEMBER_SPEC,
    )


class TestIntakeContract:
    def test_placeholders_are_substituted_for_the_model(self):
        req = DiscoveryRequest(goal="look up member {member_id}",
                               params={"member_id": "12345"},
                               param_specs=MEMBER_SPEC, entry_url="http://x/",
                               app_id="a", allowlist_id="b")
        req.validate_params()
        assert req.resolved_goal() == "look up member 12345"

    @pytest.mark.parametrize("params,why", [
        ({}, "missing required"),
        ({"member_id": "abc"}, "fails the declared pattern"),
        ({"member_id": "1", "branch": "x"}, "undeclared parameter"),
    ])
    def test_bad_parameters_fail_before_the_browser_opens(self, params, why):
        req = DiscoveryRequest(goal="g", params=params, param_specs=MEMBER_SPEC,
                               entry_url="http://x/", app_id="a", allowlist_id="b")
        with pytest.raises(ValueError):
            req.validate_params()


class TestTheModelSeesResolvedParameters:
    """The seam that a real discovery run found broken.

    `resolved_goal` existed and was unit-tested above, but nothing on the CLI
    path ever called it, so the model was handed the literal `{member_id}` and
    typed it into a form that validates "Member number must be numeric". The
    run escalated as STUCK, correctly and expensively. Testing the helper in
    isolation is what let the gap survive, so this asserts on the prompt the
    agent actually builds — with a stub client, since the loop itself is
    deliberately not exercised here.
    """

    class _Stop(Exception):
        """Abort the loop after the first request; we only want the prompt."""

    def test_the_prompt_carries_the_value_not_the_placeholder(self, tmp_path):
        sent: list = []
        stop = self._Stop

        class _Snapshot:
            url = "http://x/servicing/"
            tree = "textbox 'Member Number'"

        class _Adapter:
            def navigate(self, url: str) -> None: ...
            def snapshot(self): return _Snapshot()

        class _Messages:
            @staticmethod
            def create(**kwargs):
                sent.append(kwargs["messages"])
                raise stop

        class _Client:
            messages = _Messages()

        agent = DiscoveryAgent(_Adapter(), None,
                               EvidenceRecorder(tmp_path, "disc_test"),
                               SessionLease())
        agent.client = _Client()

        with pytest.raises(self._Stop):
            agent.run("look up member {member_id} and read their savings balance",
                      "http://x/servicing/", {"member_id": "12345"})

        prompt = sent[0][0]["content"]
        assert "member 12345" in prompt
        assert "{member_id}" not in prompt

    def test_substitution_is_exact_and_leaves_undeclared_placeholders_alone(self):
        assert resolve_goal("member {member_id}", {"member_id": "12345"}) == "member 12345"
        assert resolve_goal("member {member_id}", {}) == "member {member_id}"


class TestDistillation:
    def test_the_new_tools_become_actions(self, app_url):
        art = build(app_url)
        assert [s.action.kind for s in art.steps] == [
            "navigate", "select", "type", "type", "click", "assert", "click"]

    def test_declared_risk_survives_into_the_artifact(self, app_url):
        """Not re-derived at distillation: what was reasoned about is what ships."""
        art = build(app_url)
        posts = [s for s in art.steps if s.action.kind == "click"
                 and s.target and s.target.name == "Post"]
        assert [s.risk for s in posts] == [RiskClass.IRREVERSIBLE]
        assert art.steps[4].risk is RiskClass.SAFE_REVERSIBLE  # Continue

    def test_parameter_values_are_templated_not_baked_in(self, app_url):
        art = build(app_url, member_id="12345")
        assert "{member_id}" in art.steps[0].action.url_template
        assert "12345" not in art.steps[0].action.url_template

    def test_example_is_never_populated_from_live_parameters(self, app_url):
        """A discovery run's params are real member identifiers."""
        art = build(app_url)
        assert all(p.example is None for p in art.inputs)

    def test_artifact_starts_as_an_unapproved_draft(self, app_url):
        assert build(app_url).approval_state.value == "draft"


@pytest.mark.integration
class TestTheArtifactActuallyReplays:
    """Distillation that produces an unreplayable artifact is worth nothing."""

    def _engine(self, browser, app_url, tmp_path, attended: bool):
        allow = Allowlist.load("config/allowlist.yaml", "acme-servicing-write")
        allow.url_patterns = [f"{app_url}/servicing/*"]
        allow.auth_url_patterns = [f"{app_url}/login"]
        page = browser.new_context().new_page()
        gate = PolicyGate(allow, attended=attended)
        gate.guard_navigation(page)
        bootstrap_session(page, f"{app_url}/servicing/", "acme-servicing")
        return ReplayEngine(
            WebSurfaceAdapter(page), gate,
            EvidenceRecorder(str(tmp_path), new_run_id("test")),
            SessionLease(), InterventionQueue(str(tmp_path / "iv")),
        ), gate

    def test_it_stops_at_the_irreversible_step_rather_than_posting(
            self, browser, app_url, tmp_path, monkeypatch):
        monkeypatch.setenv("SVC_OPERATOR_ID", "op")
        monkeypatch.setenv("SVC_PASSWORD", "pw")
        engine, _ = self._engine(browser, app_url, tmp_path, attended=False)

        result = engine.replay(build(app_url), {"member_id": "12345"})

        assert result.status is ReplayStatus.ESCALATED
        assert result.resume_from_step == "s7"
        assert result.escalation_id
        # Everything up to the gate ran, and nothing was posted.
        assert [s.step_id for s in result.steps] == [f"s{i}" for i in range(1, 8)]
        assert len(mockapp.MEMBERS["12345"]["accounts"]) == 3
        assert mockapp.MEMBERS["12345"]["accounts"][0][2] == "1,204.18"

    def test_the_assert_step_is_a_real_checkpoint(
            self, browser, app_url, tmp_path, monkeypatch):
        """It has to fail when the screen is not the one it names."""
        monkeypatch.setenv("SVC_OPERATOR_ID", "op")
        monkeypatch.setenv("SVC_PASSWORD", "pw")
        art = build(app_url)
        art.steps[5].action.detector.value = "A Screen That Does Not Exist"
        engine, _ = self._engine(browser, app_url, tmp_path, attended=False)

        result = engine.replay(art, {"member_id": "12345"})

        assert result.status is not ReplayStatus.SUCCESS
        assert result.steps[-1].step_id == "s6"
