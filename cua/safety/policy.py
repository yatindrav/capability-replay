"""
Safety: allowlist enforcement, risk gating, redaction.

Every action from both the discovery loop and the replay engine passes through
`PolicyGate.check()` before the adapter sees it. There is no second route to the
surface, so a prompt-injected model has exactly the authority a reviewed
artifact has and no more.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import yaml

from cua.schema.artifact import RiskClass, Sensitivity

RISK_ORDER = {
    RiskClass.SAFE_REVERSIBLE: 0,
    RiskClass.RISKY: 1,
    RiskClass.IRREVERSIBLE: 2,
}


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


@dataclass
class PolicyVerdict:
    decision: Decision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


@dataclass
class Allowlist:
    allowlist_id: str
    url_patterns: list[str]
    action_kinds: list[str]
    max_risk_unattended: RiskClass = RiskClass.SAFE_REVERSIBLE

    @classmethod
    def load(cls, path: str, allowlist_id: str) -> "Allowlist":
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        entry = doc["allowlists"][allowlist_id]
        return cls(
            allowlist_id=allowlist_id,
            url_patterns=entry["url_patterns"],
            action_kinds=entry["action_kinds"],
            max_risk_unattended=RiskClass(
                entry.get("max_risk_unattended", "safe_reversible")
            ),
        )

    def url_permitted(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        # Match against scheme://host/path so a pattern can pin the route, not
        # just the host — "any page on this domain" is rarely what we mean.
        canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return any(fnmatch.fnmatch(canonical, pat) for pat in self.url_patterns)


class PolicyViolation(Exception):
    pass


class PolicyGate:
    def __init__(self, allowlist: Allowlist, attended: bool = False):
        self.allowlist = allowlist
        # `attended` means a human is present to confirm. Unattended production
        # replay is the default and the stricter posture.
        self.attended = attended
        self.violations: list[str] = []

    def check(self, action_kind: str, risk: RiskClass,
              url: str | None = None) -> PolicyVerdict:
        if action_kind not in self.allowlist.action_kinds:
            return self._deny(f"action '{action_kind}' not in allowlist "
                              f"'{self.allowlist.allowlist_id}'")

        if url is not None and not self.allowlist.url_permitted(url):
            return self._deny(f"url '{url}' outside allowlist")

        if RISK_ORDER[risk] > RISK_ORDER[self.allowlist.max_risk_unattended]:
            if self.attended:
                return PolicyVerdict(Decision.REQUIRE_CONFIRMATION,
                                     f"risk '{risk.value}' needs human confirmation")
            # Escalate rather than hard-block: blocking outright would make the
            # system useless for the write flows that are the business value.
            return PolicyVerdict(Decision.REQUIRE_CONFIRMATION,
                                 f"risk '{risk.value}' exceeds unattended ceiling "
                                 f"'{self.allowlist.max_risk_unattended.value}'")

        return PolicyVerdict(Decision.ALLOW, "ok")

    def _deny(self, reason: str) -> PolicyVerdict:
        self.violations.append(reason)
        return PolicyVerdict(Decision.DENY, reason)

    def guard_navigation(self, page) -> None:
        """Block browser-initiated navigation off the allowlist.

        Checking only our own navigate actions is not enough: a redirect, a
        meta-refresh, or an injected link can walk the session somewhere it
        should not be without us ever issuing an action.
        """

        def handler(route, req):
            if req.resource_type == "document" and not self.allowlist.url_permitted(req.url):
                self.violations.append(f"blocked navigation to '{req.url}'")
                route.abort()
            else:
                route.continue_()

        page.route("**/*", handler)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Pattern-based catch-net. Deliberately conservative — it will over-redact.
PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED:CARD]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[REDACTED:EMAIL]"),
]

SECRET_REF = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")


def resolve_secrets(template: str) -> str:
    """Resolve {{secret:NAME}} from the environment at use time.

    Secrets are never stored in an artifact, a log, or an evidence file — only
    the reference is. This is the only place a real value materialises.
    """
    def sub(m):
        val = os.environ.get(m.group(1))
        if val is None:
            raise PolicyViolation(f"secret '{m.group(1)}' not set in environment")
        return val
    return SECRET_REF.sub(sub, template)


def redact_text(text: str) -> str:
    if SECRET_REF.search(text):
        text = SECRET_REF.sub("[REDACTED:SECRET]", text)
    for pat, repl in PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_value(value: Any, sensitivity: Sensitivity) -> Any:
    """Label-driven redaction. Stronger than the pattern net, where we know."""
    if sensitivity == Sensitivity.SECRET:
        return "[REDACTED:SECRET]"
    if sensitivity == Sensitivity.PII:
        s = str(value)
        if len(s) <= 4:
            return "[REDACTED:PII]"
        return f"[REDACTED:PII:...{s[-2:]}]"
    return value


def redact_params(params: dict[str, Any], specs) -> dict[str, Any]:
    by_name = {p.name: p.sensitivity for p in specs}
    return {
        k: redact_value(v, by_name.get(k, Sensitivity.PUBLIC))
        for k, v in params.items()
    }
