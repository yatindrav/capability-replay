"""
Escalation and control transfer.

The core idea: the browser context is long-lived and is never closed, replaced,
or replicated when a human steps in. Automation simply stops issuing actions.
The human drives the *same* live session — same cookies, same session token,
same form state — so the hard problem ("how do we hand over a session?") reduces
to an easy one ("who currently holds the lease?").
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from cua.safety.policy import redact_text


class Owner(str, Enum):
    AUTOMATION = "automation"
    PENDING_HANDOFF = "pending_handoff"
    OPERATOR = "operator"


class LeaseViolation(Exception):
    """Raised when something tries to act while not holding the lease."""


class SessionLease:
    """Exactly one owner at a time.

    The executor asserts the lease before every action, so a late or duplicated
    action cannot race the human once control has been ceded.
    """

    def __init__(self) -> None:
        self._owner = Owner.AUTOMATION
        self._lock = threading.Lock()
        self._transitions: list[dict[str, Any]] = []

    @property
    def owner(self) -> Owner:
        return self._owner

    def _transition(self, to: Owner, why: str) -> None:
        with self._lock:
            self._transitions.append({
                "from": self._owner.value, "to": to.value, "why": why,
                "at": datetime.now(timezone.utc).isoformat(),
            })
            self._owner = to

    def request_handoff(self, why: str) -> None:
        if self._owner != Owner.AUTOMATION:
            raise LeaseViolation(f"cannot request handoff while owner={self._owner.value}")
        self._transition(Owner.PENDING_HANDOFF, why)

    def operator_take_control(self) -> None:
        if self._owner != Owner.PENDING_HANDOFF:
            raise LeaseViolation(f"no pending handoff (owner={self._owner.value})")
        self._transition(Owner.OPERATOR, "operator accepted")

    def operator_hand_back(self, note: str = "") -> None:
        if self._owner != Owner.OPERATOR:
            raise LeaseViolation(f"operator does not hold the lease (owner={self._owner.value})")
        self._transition(Owner.AUTOMATION, f"operator handed back: {note}")

    def reclaim(self, why: str) -> None:
        """Return the lease to automation from wherever it currently sits.

        For the case where an escalation is resolved without a person ever
        touching the browser — a policy confirmation granted out of band, or the
        recorder authorising its own verification replay. `request_handoff` was
        still the right thing to do (the run paused, the request was raised and
        logged), but no operator took control, so there is nothing to hand back.

        Deliberately not a way around `operator_hand_back`: if an operator does
        hold the lease, this records that they released it rather than pretending
        they never had it.
        """
        if self._owner is Owner.AUTOMATION:
            return
        self._transition(Owner.AUTOMATION, why)

    def assert_automation(self) -> None:
        if self._owner != Owner.AUTOMATION:
            raise LeaseViolation(
                f"automation attempted to act while owner={self._owner.value}"
            )

    def history(self) -> list[dict[str, Any]]:
        return list(self._transitions)


class StuckReason(str, Enum):
    """Every trigger is explicit. None of these are inferred after the fact."""

    CONDITION_ESCALATE = "condition_escalate"     # a handler said so
    LOCATOR_EXHAUSTED = "locator_exhausted"       # no strategy resolved
    NO_PROGRESS = "no_progress"                   # surface unchanged across N actions
    BUDGET_EXCEEDED = "budget_exceeded"           # step/time limit (discovery)
    RISK_GATE = "risk_gate"                       # policy needs confirmation
    CHECKPOINT_FAILED = "checkpoint_failed"


@dataclass
class InterventionRequest:
    """Carries enough context for an operator to act without reading code."""

    request_id: str = field(default_factory=lambda: f"iv_{uuid.uuid4().hex[:10]}")
    run_id: str = ""
    capability_id: str = ""
    goal: str = ""
    step_id: str | None = None
    step_intent: str | None = None
    reason: StuckReason = StuckReason.NO_PROGRESS
    detail: str = ""
    observed_url: str = ""
    observed_tree: str = ""
    params_redacted: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    operator_note: str = ""
    human_action_summary: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "capability_id": self.capability_id,
            "goal": self.goal,
            "step_id": self.step_id,
            "step_intent": self.step_intent,
            "reason": self.reason.value,
            "detail": self.detail,
            "observed_url": self.observed_url,
            "observed_tree": redact_text(self.observed_tree),
            "params_redacted": self.params_redacted,
            "screenshot_path": self.screenshot_path,
            "created_at": self.created_at.isoformat(),
            "resolved": self.resolved,
            "operator_note": self.operator_note,
            "human_action_summary": self.human_action_summary,
        }


class InterventionQueue:
    """File-backed so the mock operator console can be a separate process.

    A real deployment would put this behind the same service that owns the
    browser pool; the file is the seam, not the design.
    """

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, request_id: str) -> Path:
        return self.dir / f"{request_id}.json"

    def raise_request(self, req: InterventionRequest) -> Path:
        p = self.path_for(req.request_id)
        p.write_text(json.dumps(req.to_dict(), indent=2), encoding="utf-8")
        return p

    def load(self, request_id: str) -> dict[str, Any]:
        return json.loads(self.path_for(request_id).read_text(encoding="utf-8"))

    def update(self, request_id: str, **changes) -> None:
        doc = self.load(request_id)
        doc.update(changes)
        self.path_for(request_id).write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def pending(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("iv_*.json")):
            doc = json.loads(p.read_text(encoding="utf-8"))
            if not doc.get("resolved"):
                out.append(doc)
        return out


def summarize_human_actions(before_tree: str, after_tree: str) -> list[str]:
    """Diff two a11y snapshots into a human-readable summary.

    We record *what changed on the surface*, not keystrokes. That captures what
    the operator accomplished for the audit trail while keeping the typed
    content — which in production is member PII — out of the log entirely.
    """
    before = set(before_tree.splitlines())
    after = set(after_tree.splitlines())
    added = [l.strip() for l in after - before if l.strip()]
    removed = [l.strip() for l in before - after if l.strip()]

    summary = []
    for line in removed[:12]:
        summary.append(f"- gone: {redact_text(line)}")
    for line in added[:12]:
        summary.append(f"+ new:  {redact_text(line)}")
    if not summary:
        summary.append("(no observable change to the surface)")
    return summary
