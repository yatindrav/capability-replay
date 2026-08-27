"""
Replay result contract.

This is the type the calling AI agent actually consumes, so its job is to make
the business-outcome / failure distinction impossible to fumble. A caller that
only checks `status == SUCCESS` still behaves correctly, because a business
outcome is not silently dressed up as success — but it is also not an exception.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from cua.schema.artifact import Disposition


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"
    ESCALATED = "escalated"


class EscalationRecord(BaseModel):
    """An escalation that happened *during* a run.

    Escalation is a pause, not a terminus. If a human takes control and hands
    back, the run resumes and returns SUCCESS or BUSINESS_OUTCOME like any
    other — with the escalation recorded here, so the result still tells the
    truth about how it was obtained.

    `ReplayStatus.ESCALATED` is returned only when an escalation is *not*
    resolved: the request timed out or was abandoned. Treating every escalation
    as terminal would break the brief's requirement that the run "resume or
    complete" after handback.
    """

    escalation_id: str
    step_id: str | None
    trigger: str
    reason: str
    resolved: bool = False
    human_action_summary: str | None = Field(
        default=None, description="Snapshot diff of what changed while the operator held the lease."
    )
    held_lease_ms: int = 0


class StepRecord(BaseModel):
    """One executed step, for the structured log and for debugging."""

    step_id: str
    intent: str
    action_kind: str
    resolved_by: str | None = Field(
        default=None,
        description="Which locator strategy actually resolved the control. "
        "Falling back is the earliest signal of tenant/version drift.",
    )
    fallback_depth: int = Field(
        default=0, description="0 = primary role+name worked."
    )
    checkpoint_passed: bool | None = None
    conditions_fired: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    attempts: int = 1


class FailureDetail(BaseModel):
    """Enough to debug without re-running: what step, expected, observed."""

    step_id: str
    stage: str = Field(description="locate | act | checkpoint | success_condition")
    expected: str
    observed: str
    evidence_refs: list[str] = Field(
        default_factory=list, description="Paths to screenshot / a11y snapshot / trace."
    )


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    capability_version: int
    run_id: str

    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Declared outputs. Empty unless status is SUCCESS."
    )

    outcome_code: str | None = Field(
        default=None, description="Set when status is BUSINESS_OUTCOME, e.g. MEMBER_NOT_FOUND."
    )
    message: str | None = None

    failure: FailureDetail | None = None
    escalation_id: str | None = Field(
        default=None, description="Set when status is ESCALATED — the unresolved request."
    )
    escalations: list[EscalationRecord] = Field(
        default_factory=list,
        description="All escalations during this run, resolved or not. A run that paused "
        "for a human and completed still reports SUCCESS, with the pause recorded here.",
    )

    discovery_run_id: str | None = Field(
        default=None,
        description="Copied from the artifact's provenance. Closes the audit chain: a "
        "production result points at its capability version, which points at the "
        "discovery run that produced it.",
    )
    resume_from_step: str | None = Field(
        default=None,
        description="Step to re-attempt after a human hands control back. The run "
        "continues on the same live session, so this is an index, not a restart.",
    )

    steps: list[StepRecord] = Field(default_factory=list)
    started_at: datetime
    duration_ms: int = 0
    evidence_dir: str | None = None

    drift_signals: list[str] = Field(
        default_factory=list,
        description="Non-fatal observations worth reviewing — e.g. 'step_3 resolved via "
        "fallback depth 2'. Feeds the per-tenant drift report.",
    )

    @property
    def ok_for_caller(self) -> bool:
        """True when the run answered the question, negatively or otherwise."""
        return self.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME)


DISPOSITION_TO_STATUS = {
    Disposition.BUSINESS_OUTCOME: ReplayStatus.BUSINESS_OUTCOME,
    Disposition.HARD_FAILURE: ReplayStatus.FAILED,
    Disposition.ESCALATE: ReplayStatus.ESCALATED,
    # RECOVERABLE never terminates a run; it is handled in-band.
}
