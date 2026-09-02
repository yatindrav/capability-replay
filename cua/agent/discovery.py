"""
Discovery: the LLM-driven observe -> decide -> act loop.

The model is given exactly the action vocabulary an artifact can express, and
exactly the observation format replay will see (the flattened a11y tree). It
cannot reach the surface except through the same PolicyGate replay uses. That
symmetry is what makes a successful run recordable by construction rather than
by hopeful translation afterwards.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel, Field

from cua.evidence import EvidenceRecorder
from cua.escalation.lease import InterventionQueue, SessionLease, StuckReason
from cua.safety.policy import Decision, PolicyGate, redact_text
from cua.schema.artifact import (
    SCHEMA_VERSION,
    ApprovalState,
    CapabilityArtifact,
    Checkpoint,
    ClickAction,
    ConditionHandler,
    ControlRef,
    Detector,
    Disposition,
    FrameRef,
    LocatorHint,
    LocatorStrategy,
    AssertAction,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    PolicyBinding,
    Provenance,
    ReadAction,
    RecoveryAction,
    RiskClass,
    SelectAction,
    Sensitivity,
    Step,
    SurfaceKind,
    TargetBinding,
    TypeAction,
    WaitAction,
)
from cua.surface.web import WebSurfaceAdapter

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-5")

SYSTEM = """You operate a legacy back-office banking application through its \
accessibility tree. You are DISCOVERING how to accomplish a goal so the flow can \
be recorded and replayed later without you.

You see a flattened accessibility snapshot, one block per frame. Identify controls \
by their ROLE and ACCESSIBLE NAME, and say which frame they are in. This app is a \
frameset: most content is in 'detailFrame'.

Rules that matter:
- Prefer role+name targeting. If a control has no accessible name (common here \
  in table-based layouts), say so and describe an anchor instead: nearby text, or \
  a table row/column label.
- For every action, give a short `intent` (why this step exists) and a \
  `robustness_note` (why this targeting will still work next month for a \
  differently-branded install of the same vendor product).
- Classify every state-changing action with `risk`. This is not paperwork: \
  anything above safe_reversible stops for a human before it runs unattended, \
  so under-classifying a step that moves money removes the only check on it. \
  Judge the consequence of the action, not the wording of the button.
- Use `assert_state` after any action whose success must be proven before you \
  continue -- it becomes a checkpoint in the recording, which is what stops a \
  replay from acting on a screen it never actually reached. Use `wait_for` \
  rather than assuming a slow page has settled.
- Take ONE action per turn. Observe the result before deciding the next one.
- When the goal is met, call `finish` with the extracted data.
- If you cannot proceed, call `stuck` with a reason. Do not guess.

You are being recorded. Every action you take becomes a replayable step."""

# Asked of every tool that can change application state. Declared by the model at
# decision time rather than inferred from the artifact afterward, which is the
# point: it makes the model reason about consequence *while* it chooses, and it
# puts a reviewable justification in the artifact for every step. Inferring risk
# after the fact -- from a button's label, say -- gets "Post" right and "Continue"
# wrong, and offers a reviewer nothing to disagree with.
RISK_FIELD = {
    "type": "string",
    "enum": ["safe_reversible", "risky", "irreversible"],
    "description": (
        "Consequence of this action. safe_reversible: reading, searching, "
        "navigating. risky: writes that could be undone. irreversible: commits a "
        "transaction, moves money, sends or deletes something. If in doubt, "
        "choose the more severe class -- an over-classified step asks a human, an "
        "under-classified one does not."
    ),
}

TOOLS = [
    {
        "name": "navigate",
        "description": "Load a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "intent": {"type": "string"},
                # Legacy apps commit state on GET more often than they should.
                "risk": RISK_FIELD,
            },
            "required": ["url", "intent", "risk"],
        },
    },
    {
        "name": "click",
        "description": "Click a control identified by role and accessible name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "button, link, cell, ..."},
                "name": {"type": "string", "description": "Accessible name. Omit if none."},
                "frame": {"type": "string", "description": "Frame name, e.g. detailFrame."},
                "near_text": {"type": "string", "description": "Anchor text if name is absent."},
                "intent": {"type": "string"},
                "robustness_note": {"type": "string"},
                "risk": RISK_FIELD,
            },
            "required": ["role", "intent", "robustness_note", "risk"],
        },
    },
    {
        "name": "type_text",
        "description": "Type into a text control.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "frame": {"type": "string"},
                "near_text": {"type": "string"},
                "text": {"type": "string"},
                "intent": {"type": "string"},
                "robustness_note": {"type": "string"},
                "risk": RISK_FIELD,
            },
            "required": ["role", "text", "intent", "robustness_note", "risk"],
        },
    },
    {
        "name": "read_value",
        "description": "Read a value to return to the caller. For table data, give row_label and col_label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_name": {"type": "string"},
                "role": {"type": "string"},
                "name": {"type": "string"},
                "frame": {"type": "string"},
                "row_label": {"type": "string"},
                "col_label": {"type": "string"},
                "section": {"type": "string", "description": "Enclosing table caption/heading."},
                "intent": {"type": "string"},
                "robustness_note": {"type": "string"},
            },
            "required": ["output_name", "role", "intent", "robustness_note"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a dropdown (a combobox / select control).",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Usually 'combobox'."},
                "name": {"type": "string"},
                "frame": {"type": "string"},
                "near_text": {"type": "string", "description": "Anchor text if name is absent."},
                "value": {"type": "string", "description": "The option value to select."},
                "intent": {"type": "string"},
                "robustness_note": {"type": "string"},
                "risk": RISK_FIELD,
            },
            "required": ["role", "value", "intent", "robustness_note", "risk"],
        },
    },
    {
        "name": "wait_for",
        "description": (
            "Wait until some text appears on screen. Use instead of guessing that a "
            "slow page has finished; never assume an action landed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "intent": {"type": "string"},
            },
            "required": ["text", "intent"],
        },
    },
    {
        "name": "assert_state",
        "description": (
            "Assert that some text is on screen right now, recording it as a "
            "checkpoint in the capability. Use after an action whose success must "
            "be proven before continuing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "intent": {"type": "string"},
            },
            "required": ["text", "intent"],
        },
    },
    {
        "name": "finish",
        "description": "The goal is accomplished.",
        "input_schema": {
            "type": "object",
            "properties": {
                "success_text": {
                    "type": "string",
                    "description": "Text visible on screen that proves the goal state was reached.",
                },
                "summary": {"type": "string"},
            },
            "required": ["success_text", "summary"],
        },
    },
    {
        "name": "stuck",
        "description": "Cannot safely proceed. Escalate to a human.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


# Only these are *supposed* to change the surface. `read_value`, `assert_state`
# and `wait_for` leaving the tree byte-identical is them succeeding, not the run
# stalling — counting them killed a discovery run that had already found the
# balance and was verifying it before finishing.
MUTATING_TOOLS = frozenset({"navigate", "click", "type_text", "select_option"})


def stalled(digests: list[str]) -> bool:
    """Three consecutive state-changing actions that changed nothing."""
    return len(digests) >= 3 and len(set(digests[-3:])) == 1


def resolve_goal(goal: str, params: dict[str, str]) -> str:
    """Substitute `{placeholders}` with the values this run will actually drive.

    The goal is templated because that is what the *artifact* takes, but the
    model is driving a live form and needs the concrete value — a search box
    validating "must be numeric" rejects the literal `{member_id}`. Handing over
    the real value does not leak it into the capability: `build_artifact`
    templates matching values back out again on the way to the artifact.
    """
    for name, value in params.items():
        goal = goal.replace("{" + name + "}", value)
    return goal


class DiscoveryRequest(BaseModel):
    """What a discovery run is asked to do (DESIGN §1).

    Parameters are declared here rather than inferred from the transcript
    afterwards. Inference is guessy — `12345` could be a member number, a branch
    code or an account suffix, and guessing wrong silently produces a capability
    that hardcodes one member's data. Declaring up front means templating is
    exact substitution, and the input half of the capability contract is correct
    by construction.
    """

    goal: str = Field(description="May carry {placeholders} naming the params.")
    params: dict[str, str] = Field(default_factory=dict)
    param_specs: list[ParamSpec] = Field(default_factory=list)
    entry_url: str
    app_id: str
    allowlist_id: str
    max_steps: int = 20
    model: str = DEFAULT_MODEL

    def validate_params(self) -> None:
        """Fail before the browser opens, not half-way through a form."""
        for spec in self.param_specs:
            value = self.params.get(spec.name)
            if value is None:
                if spec.required:
                    raise ValueError(f"missing required parameter '{spec.name}'")
                continue
            if spec.pattern and not re.fullmatch(spec.pattern, value):
                raise ValueError(
                    f"parameter '{spec.name}' does not match {spec.pattern!r}")
        declared = {s.name for s in self.param_specs}
        for name in self.params:
            if name not in declared:
                raise ValueError(f"parameter '{name}' has no declared ParamSpec")

    def resolved_goal(self) -> str:
        """The goal with placeholders substituted, which is what the model sees."""
        return resolve_goal(self.goal, self.params)


class DiscoveryAgent:
    def __init__(self, adapter: WebSurfaceAdapter, gate: PolicyGate,
                 evidence: EvidenceRecorder, lease: SessionLease,
                 interventions: InterventionQueue | None = None,
                 model: str = DEFAULT_MODEL, max_steps: int = 20):
        self.a = adapter
        self.gate = gate
        self.ev = evidence
        self.lease = lease
        self.iq = interventions
        self.model = model
        self.max_steps = max_steps
        # An identity-linked API key must name the workspace it acts in, or the
        # first request 400s. The SDK has no parameter for it, so it goes on as
        # a default header. Optional: a plain workspace key needs no such thing,
        # and the env stays the only place credentials or account ids come from.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        self.client = Anthropic(
            default_headers={"anthropic-workspace-id": workspace} if workspace else {}
        )
        self.recorded: list[dict[str, Any]] = []
        # Two strikes. One denial may be the model reaching for a plausible
        # action it is simply not permitted to take, and telling it so is enough.
        # Repeated denials mean it is trying to do something this capability is
        # not allowed to do at all, which is an escalation, not a retry.
        self.denials = 0
        self.max_denials = 2

    # ------------------------------------------------------------------

    def run(self, goal: str, entry_url: str,
            parameters: dict[str, str] | None = None) -> dict[str, Any]:
        """Returns {'ok': bool, 'recorded': [...], 'success_text': str, ...}."""
        parameters = parameters or {}
        # Log the template, never the resolved goal: the resolved form embeds
        # live parameter values, and this log records parameter *names* only.
        self.ev.log("discovery_start", goal=goal, entry_url=entry_url,
                    model=self.model, parameters=list(parameters))

        self.a.navigate(entry_url)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": f"GOAL: {resolve_goal(goal, parameters)}\n\n"
                       f"ENTRY: {entry_url}\n\n{self._observe_block()}",
        }]

        digests: list[str] = []

        for step_no in range(1, self.max_steps + 1):
            resp = self.client.messages.create(
                model=self.model, max_tokens=2000, system=SYSTEM,
                tools=TOOLS, messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                messages.append({
                    "role": "user",
                    "content": "Take an action using one of the tools.",
                })
                continue

            call = calls[0]
            self.ev.log("model_action", step=step_no, tool=call.name,
                        input=json.dumps(call.input)[:800])

            if call.name == "finish":
                self.ev.log("discovery_success", summary=call.input.get("summary", ""))
                return {"ok": True, "recorded": self.recorded,
                        "success_text": call.input["success_text"],
                        "summary": call.input.get("summary", ""),
                        "parameters": parameters}

            if call.name == "stuck":
                return self._escalate(goal, call.input.get("reason", ""),
                                      StuckReason.CONDITION_ESCALATE)

            try:
                result_text = self._execute(call, parameters)
            except Exception as exc:
                result_text = f"ERROR: {type(exc).__name__}: {exc}"
                self.ev.log("action_error", step=step_no, error=str(exc))

            if self.denials > self.max_denials:
                return self._escalate(
                    goal,
                    f"{self.denials} policy denials; the model is reaching for "
                    f"authority this capability does not have",
                    StuckReason.RISK_GATE)

            obs = self.a.snapshot()
            if call.name in MUTATING_TOOLS:
                digests.append(obs.digest())
                if stalled(digests):
                    return self._escalate(
                        goal, "surface unchanged across 3 state-changing actions",
                        StuckReason.NO_PROGRESS)

            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": f"{result_text}\n\n{self._observe_block()}",
                }],
            })

        return self._escalate(goal, f"step budget of {self.max_steps} exhausted",
                              StuckReason.BUDGET_EXCEEDED)

    # ------------------------------------------------------------------

    def _observe_block(self) -> str:
        obs = self.a.snapshot()
        # Redacted on the way to the model as well as on the way to disk: the
        # model has no need for raw PII to decide where to click.
        return f"CURRENT URL: {obs.url}\n\nACCESSIBILITY SNAPSHOT:\n{redact_text(obs.tree)}"

    TOOL_TO_ACTION_KIND = {
        "navigate": "navigate", "click": "click", "type_text": "type",
        "read_value": "read", "select_option": "select",
        "wait_for": "wait", "assert_state": "assert",
    }

    def _declared_risk(self, call) -> RiskClass:
        """The model's own classification, defaulting to the safe end.

        `read_value`, `wait_for` and `assert_state` do not carry a risk field
        because they cannot change state — they only observe. Everything that
        can must declare, and the schema makes it required.
        """
        raw = call.input.get("risk")
        if raw is None:
            return RiskClass.SAFE_REVERSIBLE
        try:
            return RiskClass(raw)
        except ValueError:
            self.ev.log("risk_unparsed", tool=call.name, declared=raw)
            return RiskClass.IRREVERSIBLE  # unparseable means ask a human

    def _execute(self, call, parameters: dict[str, str]) -> str:
        kind = self.TOOL_TO_ACTION_KIND[call.name]
        inp = call.input
        risk = self._declared_risk(call)

        # Cross-check the declaration against the old label heuristic. We trust
        # the model's answer -- it is the one that lands in the artifact and gets
        # reviewed -- but a disagreement is worth surfacing, because it is either
        # a mis-declaration or a control whose label is misleading.
        if call.name == "click":
            guessed = _classify_click(inp.get("name", ""))
            if guessed != risk:
                self.ev.log("risk_disagreement", tool=call.name,
                            control=inp.get("name"), declared=risk.value,
                            heuristic=guessed.value)

        self.lease.assert_automation()
        verdict = self.gate.check(kind, risk,
                                  inp.get("url") if kind == "navigate" else None)
        if verdict.decision != Decision.ALLOW:
            self.ev.log("policy_denied", tool=call.name, reason=verdict.reason,
                        decision=verdict.decision.value, risk=risk.value)
            self.denials += 1
            return f"BLOCKED BY POLICY: {verdict.reason}"

        needs_control = kind in ("click", "type", "read", "select")
        ref = self._control_ref(inp) if needs_control else None

        if kind == "navigate":
            self.a.navigate(inp["url"])
            self._record(call.name, inp, None, risk)
            return f"Navigated to {inp['url']}"

        if kind == "wait":
            ok = self._await_text(inp["text"])
            self._record(call.name, inp, None, risk)
            return (f"'{inp['text']}' is present." if ok
                    else f"TIMED OUT waiting for '{inp['text']}'.")

        if kind == "assert":
            if not self.a.contains_text(inp["text"]):
                # Not recorded: an assertion that does not hold is not a
                # checkpoint, it is the model being wrong about where it is.
                return (f"ASSERTION FAILED: '{inp['text']}' is not on screen. "
                        f"You are not where you think you are.")
            self._record(call.name, inp, None, risk)
            return f"Confirmed: '{inp['text']}' is on screen."

        res = self.a.resolve(ref)
        if not res.ok:
            return f"COULD NOT RESOLVE that control: {res.error}"

        if kind == "click":
            self.a.click(res)
            self._record(call.name, inp, ref, risk)
            time.sleep(0.4)
            return "Clicked."

        if kind == "type":
            self.a.type_text(res, inp["text"], True)
            self._record(call.name, inp, ref, risk)
            return f"Typed into the {inp['role']}."

        if kind == "select":
            self.a.select(res, inp["value"])
            self._record(call.name, inp, ref, risk)
            return f"Selected '{inp['value']}'."

        if kind == "read":
            value = self.a.read(res)
            self._record(call.name, inp, ref, risk)
            return f"Read value: {redact_text(value)}"

        return "unknown action"

    def _await_text(self, text: str, timeout_s: float = 10.0) -> bool:
        """Poll, never sleep — the same discipline the replay engine uses."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.a.contains_text(text):
                return True
            time.sleep(0.25)
        return self.a.contains_text(text)

    def _control_ref(self, inp: dict) -> ControlRef:
        fallbacks = []
        if inp.get("row_label") and inp.get("col_label"):
            fallbacks.append(LocatorHint(
                strategy=LocatorStrategy.TABLE_CELL,
                value=f"row={inp['row_label']};col={inp['col_label']}",
                confidence=0.85,
                note="Row/column labels are vendor-fixed strings; branding changes markup, not labels.",
            ))
        if inp.get("near_text"):
            fallbacks.append(LocatorHint(
                strategy=LocatorStrategy.TEXT_ANCHOR,
                value=f"after={inp['near_text']}", confidence=0.6,
                note="Positional anchor relative to stable label text.",
            ))
        return ControlRef(
            role=inp["role"],
            name=inp.get("name") or None,
            frame=FrameRef(path=[inp["frame"]]) if inp.get("frame") else None,
            near_text=inp.get("near_text"),
            within_section=inp.get("section"),
            fallbacks=fallbacks,
            robustness_note=inp.get("robustness_note"),
        )

    def _record(self, tool: str, inp: dict, ref: ControlRef | None,
                risk: RiskClass = RiskClass.SAFE_REVERSIBLE) -> None:
        self.recorded.append({"tool": tool, "input": inp,
                              "control": ref.model_dump() if ref else None,
                              "risk": risk.value})

    def _escalate(self, goal: str, reason: str, kind: StuckReason) -> dict[str, Any]:
        from cua.escalation.lease import InterventionRequest
        obs = self.a.snapshot(with_screenshot=True)
        shot = self.ev.screenshot(f"discovery_stuck", obs.screenshot_png)
        req = InterventionRequest(
            run_id=self.ev.run_id, capability_id="(discovery)", goal=goal,
            reason=kind, detail=reason, observed_url=obs.url,
            observed_tree=obs.tree, screenshot_path=shot,
        )
        self.lease.request_handoff(f"{kind.value}: {reason}")
        if self.iq:
            self.iq.raise_request(req)
        self.ev.log("discovery_stuck", reason=reason, request_id=req.request_id)
        return {"ok": False, "recorded": self.recorded, "reason": reason,
                "escalation_id": req.request_id}


# ---------------------------------------------------------------------------
# Recording: transcript -> artifact
# ---------------------------------------------------------------------------


def build_artifact(
    *,
    capability_id: str,
    title: str,
    description: str,
    goal: str,
    entry_url: str,
    app_id: str,
    allowlist_id: str,
    discovery: dict[str, Any],
    model: str,
    run_id: str,
    param_specs: list[ParamSpec],
    output_specs_hint: dict[str, str] | None = None,
) -> CapabilityArtifact:
    """Distil a successful discovery run into a typed capability.

    Canonicalisation happens here: concrete values the caller declared as
    parameters are replaced with `{param}` templates wherever they appear in a
    typed value or a URL. That is what turns 'a recording of member 12345' into
    'a capability that takes a member_id'.
    """
    params = discovery.get("parameters", {})
    outputs_hint = output_specs_hint or {}

    def canonical(text: str) -> str:
        for name, value in params.items():
            if value and value in text:
                text = text.replace(value, "{" + name + "}")
        return text

    steps: list[Step] = []
    outputs: list[OutputSpec] = []

    for i, rec in enumerate(discovery["recorded"], start=1):
        sid = f"s{i}"
        inp, tool = rec["input"], rec["tool"]
        ctrl = ControlRef(**rec["control"]) if rec["control"] else None
        intent = inp.get("intent", "")

        # The model declared this at decision time; we carry it through rather
        # than re-deriving it, so what a reviewer approves is what the model
        # actually reasoned about. Older recordings without the field fall back
        # to the safe end.
        risk = RiskClass(rec.get("risk", RiskClass.SAFE_REVERSIBLE.value))

        if tool == "navigate":
            action = NavigateAction(url_template=canonical(inp["url"]))
        elif tool == "click":
            action = ClickAction()
        elif tool == "type_text":
            action = TypeAction(value_template=canonical(inp["text"]))
        elif tool == "select_option":
            action = SelectAction(value_template=canonical(inp["value"]))
        elif tool == "wait_for":
            action = WaitAction(until=Detector(kind="text_present",
                                               value=canonical(inp["text"])))
        elif tool == "assert_state":
            action = AssertAction(detector=Detector(kind="text_present",
                                                    value=canonical(inp["text"])))
        elif tool == "read_value":
            action = ReadAction(
                output_name=inp["output_name"],
                transform="strip_currency" if "balance" in inp["output_name"] else "trim",
            )
            outputs.append(OutputSpec(
                name=inp["output_name"],
                type="number" if "balance" in inp["output_name"] else "string",
                description=outputs_hint.get(inp["output_name"], intent),
                sensitivity=Sensitivity.PII,
                from_step=sid,
            ))
        else:
            continue

        steps.append(Step(id=sid, intent=intent, action=action, target=ctrl, risk=risk))

    # The final step carries the checkpoint proving we reached the goal state.
    success = Checkpoint(
        detectors=[Detector(kind="text_present", value=discovery["success_text"])],
        description=f"Goal state reached: '{discovery['success_text']}' is visible",
    )
    if steps:
        steps[-1].checkpoint = success

    return CapabilityArtifact(
        schema_version=SCHEMA_VERSION,
        capability_id=capability_id,
        version=1,
        title=title,
        description=description,
        target=TargetBinding(app_id=app_id, variant="base",
                             surface=SurfaceKind.LEGACY_WEB,
                             entry_url_template=entry_url),
        inputs=param_specs,
        outputs=outputs,
        steps=steps,
        success=success,
        global_conditions=default_conditions(),
        policy=PolicyBinding(allowlist_id=allowlist_id),
        approval_state=ApprovalState.DRAFT,
        provenance=Provenance(
            discovered_by=model, discovery_run_id=run_id,
            recorded_at=datetime.now(timezone.utc),
            transcript_ref=f"evidence/{run_id}/run.jsonl",
        ),
    )


def _classify_click(name: str) -> RiskClass:
    lowered = (name or "").lower()
    if any(w in lowered for w in ("post", "transfer", "submit payment", "delete", "close account")):
        return RiskClass.IRREVERSIBLE
    if any(w in lowered for w in ("save", "update", "create", "open account")):
        return RiskClass.RISKY
    return RiskClass.SAFE_REVERSIBLE


def default_conditions() -> list[ConditionHandler]:
    """Conditions inherited from the app profile rather than rediscovered.

    In the multi-tenant design these live on the vendor-product profile: every
    capability recorded against this app inherits the same session-timeout and
    app-error handling, and a tenant overlay can add to them.
    """
    return [
        ConditionHandler(
            condition_id="session_expired", scope="global",
            detect=Detector(kind="text_present", value="Your session has expired"),
            disposition=Disposition.RECOVERABLE,
            message="Session expired; re-establishing.",
            recovery=RecoveryAction(kind="reauthenticate", max_attempts=1, backoff_ms=500),
        ),
        ConditionHandler(
            condition_id="maintenance_interstitial", scope="global",
            detect=Detector(kind="text_present", value="Scheduled maintenance"),
            disposition=Disposition.RECOVERABLE,
            message="Dismissing known maintenance interstitial.",
            recovery=RecoveryAction(
                kind="dismiss",
                dismiss_control=ControlRef(
                    role="button", name="Acknowledge",
                    frame=FrameRef(path=["detailFrame"]),
                ),
                backoff_ms=300,
            ),
        ),
        ConditionHandler(
            condition_id="member_not_found", scope="global",
            detect=Detector(kind="text_present", value="No member found"),
            disposition=Disposition.BUSINESS_OUTCOME,
            outcome_code="MEMBER_NOT_FOUND",
            message="No member exists with the supplied member number.",
        ),
        ConditionHandler(
            condition_id="invalid_member_number", scope="global",
            detect=Detector(kind="text_present", value="must be numeric"),
            disposition=Disposition.BUSINESS_OUTCOME,
            outcome_code="INVALID_MEMBER_NUMBER",
            message="The application rejected the member number as non-numeric.",
        ),
        ConditionHandler(
            condition_id="permission_denied", scope="global",
            detect=Detector(kind="text_present", value="not authorized"),
            disposition=Disposition.ESCALATE,
            message="Operator credentials lack permission for this screen.",
        ),
        ConditionHandler(
            condition_id="app_error", scope="global",
            detect=Detector(kind="text_present", value="SVC-500"),
            disposition=Disposition.HARD_FAILURE,
            outcome_code="APP_ERROR",
            message="The application returned an internal error (SVC-500).",
        ),
    ]
