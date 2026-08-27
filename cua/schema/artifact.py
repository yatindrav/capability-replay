"""
Capability artifact schema.

Design thesis: the artifact is a *capability contract*, not a macro recording.
A calling agent must be able to read it and know what it needs and what it
returns; a human reviewer must be able to audit it without the model transcript.

The single most important design constraint here: nothing in this schema may be
web-specific at the level that matters. Controls are described semantically
(role + accessible name + anchor), because role/name exists in browser a11y
trees, Windows UIA, macOS AX and AT-SPI alike. CSS/XPath appear only as
demoted, adapter-private fallback hints. That is the seam that lets the same
artifact target a modern web app, a frameset-era legacy app, or a desktop app.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


class SurfaceKind(str, Enum):
    """Which adapter family can execute this artifact.

    The artifact body is written to be surface-agnostic; this field tells the
    executor which adapter to load, and lets us refuse to replay a web-recorded
    artifact against a desktop surface without an explicit port.
    """

    WEB = "web"
    LEGACY_WEB = "legacy_web"  # framesets, table layout, no test ids
    DESKTOP = "desktop"


# ---------------------------------------------------------------------------
# Control targeting
# ---------------------------------------------------------------------------


class LocatorStrategy(str, Enum):
    ROLE_NAME = "role_name"  # a11y role + accessible name  (portable)
    LABEL_PROXIMITY = "label_proximity"  # nearest label text  (portable)
    TABLE_CELL = "table_cell"  # row header + column header  (portable)
    TEXT_ANCHOR = "text_anchor"  # n-th control after some text  (portable)
    CSS = "css"  # web adapter only
    XPATH = "xpath"  # web adapter only
    RELATIVE_COORDS = "relative_coords"  # last resort; fraction of viewport


class LocatorHint(BaseModel):
    """One ranked way to find a control.

    Replay walks hints in order and takes the first that resolves to exactly
    one control. Recording confidence is captured so drift analysis can tell
    "we fell back to hint 3" from "hint 1 still works".
    """

    strategy: LocatorStrategy
    value: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    note: str | None = Field(
        default=None, description="Why the recorder believed this is robust."
    )


class FrameRef(BaseModel):
    """Frame/window scoping.

    Legacy apps live in framesets; desktop apps live in window hierarchies.
    Same idea, so it is modelled once. Named path preferred over index, since
    frame ordering is the first thing that changes between tenant builds.
    """

    path: list[str] = Field(
        default_factory=list,
        description="Ordered frame/window names from root, e.g. ['navFrame','detail'].",
    )


class ControlRef(BaseModel):
    """How a step identifies the control it acts on.

    `role` + `name` is the primary key and is deliberately the only *required*
    part: it is the one addressing scheme that survives a port to UIA/AX/AT-SPI.
    Everything else narrows or rescues it.
    """

    role: str = Field(description="Accessibility role: button, textbox, link, cell, ...")
    name: str | None = Field(default=None, description="Accessible name.")
    name_match: Literal["exact", "contains", "regex"] = "exact"

    frame: FrameRef | None = None
    nth: int | None = Field(
        default=None, description="Disambiguator when role+name is genuinely ambiguous."
    )

    near_text: str | None = Field(
        default=None,
        description="Semantic anchor for non-semantic markup — the control sits near this text.",
    )
    within_section: str | None = Field(
        default=None, description="Enclosing section/legend/table caption, if any."
    )

    fallbacks: list[LocatorHint] = Field(
        default_factory=list,
        description="Ranked rescue hints, tried in order after role+name fails.",
    )

    robustness_note: str | None = Field(
        default=None,
        description="Recorder's reasoning about why this targeting should hold up. Reviewed by humans.",
    )


# ---------------------------------------------------------------------------
# Parameters and outputs (the agent-facing contract)
# ---------------------------------------------------------------------------


class Sensitivity(str, Enum):
    """Drives redaction in logs, evidence and artifacts.

    SECRET never touches disk in any form. PII is masked in evidence but may be
    returned to the caller in memory, because the caller is the bank's own agent.
    """

    PUBLIC = "public"
    PII = "pii"
    SECRET = "secret"


class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean", "date"]
    required: bool = True
    description: str
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    pattern: str | None = Field(default=None, description="Validated before replay starts.")
    example: str | None = Field(
        default=None,
        description="Synthetic illustration for the calling agent. NEVER auto-populated "
        "from a discovery run's parameter values — those are live member data. "
        "Author-supplied only; the recorder must leave this None.",
    )


class FieldSpec(BaseModel):
    """One scalar field inside a structured output."""

    name: str
    type: Literal["string", "integer", "number", "boolean", "date"]
    description: str
    sensitivity: Sensitivity = Sensitivity.PUBLIC


class OutputSpec(BaseModel):
    """A declared output and its *shape*.

    3.2 asks for outputs "and their shape", which scalars alone cannot express:
    "read the savings balance" is a scalar, but "list this member's
    sub-accounts" is a repeated record, and a caller needs to know which it is
    getting before it invokes. `cardinality` + `fields` covers both without
    admitting arbitrary nesting — deliberately capped at one level, because a
    capability returning a deep object graph is a sign the flow should have been
    split into two capabilities.
    """

    name: str
    cardinality: Literal["one", "many"] = "one"
    type: Literal["string", "integer", "number", "boolean", "date", "record"] = "string"
    fields: list[FieldSpec] = Field(
        default_factory=list,
        description="Required when type == 'record'. Ignored otherwise.",
    )
    description: str
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    from_step: str = Field(description="Step id whose extraction produces this value.")


# ---------------------------------------------------------------------------
# Detection: how we recognise a state
# ---------------------------------------------------------------------------


class Detector(BaseModel):
    """A predicate over observed surface state.

    Used for three different jobs on purpose — checkpoints, condition triggers
    and success conditions are all 'is the surface in state X?', and collapsing
    them keeps the evaluator small and the schema legible.
    """

    kind: Literal[
        "text_present",
        "text_absent",
        "control_present",
        "control_absent",
        "url_matches",
        "value_equals",
        "dialog_present",  # modal/alertdialog role, or a native browser dialog
        "load_failed",  # navigation error, or an HTTP error page
    ]
    value: str | None = None
    control: ControlRef | None = None
    case_sensitive: bool = False


class Checkpoint(BaseModel):
    """An asserted post-condition. Never assume a click worked."""

    detectors: list[Detector] = Field(min_length=1)
    require: Literal["all", "any"] = "all"
    timeout_ms: int = 10_000
    description: str


# ---------------------------------------------------------------------------
# The error taxonomy — the piece the brief cares most about
# ---------------------------------------------------------------------------


class Disposition(str, Enum):
    """The three-way split the brief calls the most common design mistake.

    BUSINESS_OUTCOME is a *successful* replay with a negative answer: "no such
    member" is information the caller asked for, not a crash. It returns
    normally, with an outcome code.

    RECOVERABLE is noise the flow is expected to encounter — an interstitial to
    dismiss, a transient load to retry. Handled in-band, run continues.

    HARD_FAILURE stops the run and surfaces a debuggable error.

    ESCALATE hands the live session to a human rather than guessing.
    """

    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"
    ESCALATE = "escalate"


class RecoveryAction(BaseModel):
    kind: Literal["dismiss", "retry_step", "reload", "reauthenticate", "none"] = "none"
    dismiss_control: ControlRef | None = None
    max_attempts: int = 2
    backoff_ms: int = 1000


class ConditionHandler(BaseModel):
    """Declarative: 'if you see this, it means that, do this.'

    Declarative rather than code so replay never improvises, so a reviewer can
    audit the whole failure surface of a capability by reading the artifact, and
    so handlers can be inherited from a shared app profile across tenants.
    """

    condition_id: str
    detect: Detector
    disposition: Disposition
    outcome_code: str | None = Field(
        default=None,
        description="Stable code returned to the caller, e.g. MEMBER_NOT_FOUND.",
    )
    message: str
    recovery: RecoveryAction = Field(default_factory=RecoveryAction)
    scope: Literal["step", "global"] = "step"


# ---------------------------------------------------------------------------
# Actions and steps
# ---------------------------------------------------------------------------


class RiskClass(str, Enum):
    """Governs what unattended replay is allowed to do.

    Recorded per step rather than inferred at replay time, so the risk posture
    of a capability is reviewable before it is ever approved.
    """

    SAFE_REVERSIBLE = "safe_reversible"  # read, navigate, search
    RISKY = "risky"  # writes that can be undone
    IRREVERSIBLE = "irreversible"  # posts a transaction, sends money


class NavigateAction(BaseModel):
    kind: Literal["navigate"] = "navigate"
    url_template: str = Field(description="May reference {param} placeholders.")


class ClickAction(BaseModel):
    kind: Literal["click"] = "click"


class TypeAction(BaseModel):
    kind: Literal["type"] = "type"
    value_template: str = Field(description="e.g. '{member_id}'. Never a literal secret.")
    clear_first: bool = True


class SelectAction(BaseModel):
    kind: Literal["select"] = "select"
    value_template: str


class ReadAction(BaseModel):
    kind: Literal["read"] = "read"
    output_name: str
    transform: Literal["raw", "strip_currency", "digits_only", "trim"] = "trim"


class WaitAction(BaseModel):
    kind: Literal["wait"] = "wait"
    until: Detector


class AssertAction(BaseModel):
    kind: Literal["assert"] = "assert"
    detector: Detector


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        TypeAction,
        SelectAction,
        ReadAction,
        WaitAction,
        AssertAction,
    ],
    Field(discriminator="kind"),
]


class Step(BaseModel):
    id: str
    intent: str = Field(
        description="Why this step exists, in plain language. Survives from discovery "
        "so a human reviewer can follow the flow without the transcript."
    )
    action: Action
    target: ControlRef | None = Field(
        default=None, description="Required for click/type/select/read."
    )
    risk: RiskClass = RiskClass.SAFE_REVERSIBLE
    checkpoint: Checkpoint | None = Field(
        default=None, description="Post-condition asserted before moving on."
    )
    conditions: list[ConditionHandler] = Field(
        default_factory=list, description="Step-scoped handlers, checked before checkpoint."
    )
    timeout_ms: int = 15_000
    optional: bool = Field(
        default=False, description="Skip silently if the target never appears."
    )


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TargetBinding(BaseModel):
    """Separates 'which vendor product' from 'which tenant's instance of it'.

    This is what makes cross-tenant reuse possible: the artifact is recorded
    against app_id at some base variant, and a tenant that runs the same product
    supplies a thin overlay rather than a re-recording.
    """

    app_id: str = Field(description="Vendor product identity, e.g. 'symitar-episys'.")
    app_version: str | None = None
    variant: str = Field(default="base", description="'base', or a tenant/variant id.")
    surface: SurfaceKind = SurfaceKind.WEB
    entry_url_template: str | None = None


class Provenance(BaseModel):
    """Audit trail without the transcript.

    We deliberately store a *reference* to the discovery run rather than the
    model transcript itself: transcripts contain observed screen content, which
    in production means member PII.
    """

    discovered_by: str = Field(description="Model id that produced the recording.")
    discovery_run_id: str
    recorded_at: datetime
    recorded_by: str | None = None
    transcript_ref: str | None = Field(
        default=None, description="Pointer to redacted evidence, not inline content."
    )


class ApprovalState(str, Enum):
    """Three gates, not two.

    `DRAFT_VERIFIED` is the one that carries information a human cannot supply:
    the recorder replayed this artifact once, with no model and the same
    parameters, and it worked. That is a machine-checkable claim, and it sits
    below `APPROVED`, which remains a human judgement about whether the
    capability *should* exist. An artifact that never reaches DRAFT_VERIFIED is
    not written to `capabilities/` at all.
    """

    DRAFT = "draft"
    DRAFT_VERIFIED = "draft_verified"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class Stability(BaseModel):
    replay_count: int = 0
    success_count: int = 0
    last_verified_at: datetime | None = None

    @property
    def score(self) -> float | None:
        return self.success_count / self.replay_count if self.replay_count else None


class PolicyBinding(BaseModel):
    allowlist_id: str = Field(description="Named allowlist this capability runs under.")
    max_risk_unattended: RiskClass = Field(
        default=RiskClass.SAFE_REVERSIBLE,
        description="Steps above this class require human confirmation, even on an approved artifact.",
    )


class CapabilityArtifact(BaseModel):
    """A versioned, reviewable, agent-invocable capability."""

    schema_version: str = SCHEMA_VERSION
    capability_id: str = Field(description="Stable name, e.g. 'member.savings_balance.read'.")
    version: int = Field(default=1, description="Bumped on re-record; artifacts are immutable.")
    title: str
    description: str = Field(description="What a calling agent should understand this does.")

    target: TargetBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)

    steps: list[Step] = Field(min_length=1)
    success: Checkpoint = Field(description="Overall success condition for the capability.")
    global_conditions: list[ConditionHandler] = Field(
        default_factory=list,
        description="Checked after every step — session timeout, permission denied, app error.",
    )

    policy: PolicyBinding
    approval_state: ApprovalState = ApprovalState.DRAFT
    stability: Stability = Field(default_factory=Stability)
    provenance: Provenance

    def tool_schema(self) -> dict:
        """Render as a function-calling tool definition for a calling agent."""
        type_map = {
            "string": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "date": "string",
        }
        return {
            "name": self.capability_id.replace(".", "_"),
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": {
                    p.name: {"type": type_map[p.type], "description": p.description}
                    for p in self.inputs
                },
                "required": [p.name for p in self.inputs if p.required],
            },
        }
