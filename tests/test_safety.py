"""Allowlist, risk gating and redaction.

The asymmetry between `url_patterns` and `auth_url_patterns` is the load-bearing
case here: the platform drives the browser to the sign-in route during session
bootstrap, so the navigation guard must permit it, while no recorded `navigate`
action may target the credential form. Merging the two lists would pass every
other test in this file and quietly hand the agent a route to the login page.
"""

from __future__ import annotations

import pytest

from cua.safety.policy import (
    Allowlist,
    Decision,
    PolicyGate,
    PolicyViolation,
    redact_params,
    redact_text,
    redact_value,
    resolve_secrets,
)
from cua.schema.artifact import ParamSpec, RiskClass, Sensitivity

BASE = "http://127.0.0.1:8099"


@pytest.fixture
def allow() -> Allowlist:
    return Allowlist(
        allowlist_id="test",
        url_patterns=[f"{BASE}/servicing/*", f"{BASE}/servicing"],
        action_kinds=["navigate", "click", "type", "read"],
        max_risk_unattended=RiskClass.SAFE_REVERSIBLE,
        auth_url_patterns=[f"{BASE}/login"],
    )


class TestAllowlist:
    @pytest.mark.parametrize("url,permitted", [
        (f"{BASE}/servicing/member", True),
        (f"{BASE}/servicing", True),
        (f"{BASE}/login", False),          # platform-only
        (f"{BASE}/_fault", False),         # test hook, never automatable
        (f"{BASE}/_reset", False),
        ("http://evil.example.com/x", False),
        ("file:///etc/passwd", False),     # scheme is checked, not just host
        ("javascript:alert(1)", False),
    ])
    def test_agent_routes(self, allow, url, permitted):
        assert allow.url_permitted(url) is permitted

    def test_auth_route_is_reachable_by_the_platform_only(self, allow):
        assert allow.auth_permitted(f"{BASE}/login") is True
        assert allow.url_permitted(f"{BASE}/login") is False

    def test_auth_carve_out_is_one_route_not_a_wildcard(self, allow):
        for url in (f"{BASE}/_fault", f"{BASE}/servicing/member", f"{BASE}/login/x"):
            assert allow.auth_permitted(url) is False

    def test_gate_never_consults_the_auth_list(self, allow):
        """The regression that matters: `check()` must not soften for auth routes."""
        gate = PolicyGate(allow)
        verdict = gate.check("navigate", RiskClass.SAFE_REVERSIBLE, f"{BASE}/login")
        assert verdict.decision is Decision.DENY


class TestRiskGate:
    def test_action_outside_the_vocabulary_is_denied(self, allow):
        assert PolicyGate(allow).check("select", RiskClass.SAFE_REVERSIBLE).decision is Decision.DENY

    def test_safe_action_allowed(self, allow):
        assert PolicyGate(allow).check("click", RiskClass.SAFE_REVERSIBLE).allowed

    @pytest.mark.parametrize("risk", [RiskClass.RISKY, RiskClass.IRREVERSIBLE])
    def test_above_ceiling_requires_confirmation_rather_than_denial(self, allow, risk):
        """Escalate, don't block: blocking outright makes write flows unusable."""
        assert PolicyGate(allow).check("click", risk).decision is Decision.REQUIRE_CONFIRMATION

    def test_ceiling_is_per_allowlist(self, allow):
        allow.max_risk_unattended = RiskClass.RISKY
        gate = PolicyGate(allow)
        assert gate.check("click", RiskClass.RISKY).allowed
        assert gate.check("click", RiskClass.IRREVERSIBLE).decision is Decision.REQUIRE_CONFIRMATION


class TestRedaction:
    @pytest.mark.parametrize("raw,gone", [
        ("SSN 123-45-6789 on file", "123-45-6789"),
        ("card 4111 1111 1111 1111", "4111 1111 1111 1111"),
        ("mail jane@example.com", "jane@example.com"),
    ])
    def test_patterns_are_stripped(self, raw, gone):
        assert gone not in redact_text(raw)

    def test_secret_refs_never_survive(self):
        assert "hunter2" not in redact_text("pw={{secret:OP_PASSWORD}}")
        assert "OP_PASSWORD" not in redact_text("pw={{secret:OP_PASSWORD}}")

    def test_label_beats_pattern(self):
        """A member number matches no PII regex; the label is what protects it."""
        assert redact_value("12345", Sensitivity.PII) == "[REDACTED:PII:...45]"
        assert redact_value("ab", Sensitivity.PII) == "[REDACTED:PII]"
        assert redact_value("x", Sensitivity.SECRET) == "[REDACTED:SECRET]"
        assert redact_value("Downtown", Sensitivity.PUBLIC) == "Downtown"

    def test_params_redacted_by_declared_sensitivity(self):
        specs = [ParamSpec(name="member_id", type="string", description="",
                           sensitivity=Sensitivity.PII),
                 ParamSpec(name="branch", type="string", description="")]
        out = redact_params({"member_id": "12345", "branch": "Downtown"}, specs)
        assert out == {"member_id": "[REDACTED:PII:...45]", "branch": "Downtown"}


class TestSecrets:
    def test_resolved_from_environment_at_use_time(self, monkeypatch):
        monkeypatch.setenv("OP_PASSWORD", "hunter2")
        assert resolve_secrets("pw={{secret:OP_PASSWORD}}") == "pw=hunter2"

    def test_missing_secret_is_a_violation_not_an_empty_string(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        with pytest.raises(PolicyViolation):
            resolve_secrets("{{secret:NOPE}}")
