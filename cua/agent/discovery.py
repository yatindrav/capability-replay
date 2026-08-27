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
import time
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic

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
    NavigateAction,
    OutputSpec,
    ParamSpec,
    PolicyBinding,
    Provenance,
    ReadAction,
    RecoveryAction,
    RiskClass,
    Sensitivity,
    Step,
    SurfaceKind,
    TargetBinding,
    TypeAction,
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
- Take ONE action per turn. Observe the result before deciding the next one.
- When the goal is met, call `finish` with the extracted data.
- If you cannot proceed, call `stuck` with a reason. Do not guess.

You are being recorded. Every action you take becomes a replayable step."""

TOOLS = [
    {
        "name": "navigate",
        "description": "Load a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "intent": {"type": "string"},
            },
            "required": ["url", "intent"],
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
            },
            "required": ["role", "intent", "robustness_note"],
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
            },
            "required": ["role", "text", "intent", "robustness_note"],
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
        self.client = Anthropic()
        self.recorded: list[dict[str, Any]] = []

    # ------------------------------------------------------------------

    def run(self, goal: str, entry_url: str,
            parameters: dict[str, str] | None = None) -> dict[str, Any]:
        """Returns {'ok': bool, 'recorded': [...], 'success_text': str, ...}."""
        parameters = parameters or {}
        self.ev.log("discovery_start", goal=goal, entry_url=entry_url,
                    model=self.model, parameters=list(parameters))

        self.a.navigate(entry_url)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": f"GOAL: {goal}\n\nENTRY: {entry_url}\n\n"
                       f"{self._observe_block()}",
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

            obs = self.a.snapshot()
            digests.append(obs.digest())
            if len(digests) >= 3 and len(set(digests[-3:])) == 1:
                return self._escalate(goal, "surface unchanged across 3 actions",
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

    def _execute(self, call, parameters: dict[str, str]) -> str:
        kind = {"navigate": "navigate", "click": "click",
                "type_text": "type", "read_value": "read"}[call.name]
        inp = call.input

        self.lease.assert_automation()
        verdict = self.gate.check(kind, RiskClass.SAFE_REVERSIBLE,
                                  inp.get("url") if kind == "navigate" else None)
        if verdict.decision != Decision.ALLOW:
            self.ev.log("policy_denied", tool=call.name, reason=verdict.reason)
            return f"BLOCKED BY POLICY: {verdict.reason}"

        ref = self._control_ref(inp) if kind != "navigate" else None

        if kind == "navigate":
            self.a.navigate(inp["url"])
            self._record(call.name, inp, None)
            return f"Navigated to {inp['url']}"

        res = self.a.resolve(ref)
        if not res.ok:
            return f"COULD NOT RESOLVE that control: {res.error}"

        if kind == "click":
            self.a.click(res)
            self._record(call.name, inp, ref)
            time.sleep(0.4)
            return "Clicked."

        if kind == "type":
            self.a.type_text(res, inp["text"], True)
            self._record(call.name, inp, ref)
            return f"Typed into the {inp['role']}."

        if kind == "read":
            value = self.a.read(res)
            self._record(call.name, inp, ref)
            return f"Read value: {redact_text(value)}"

        return "unknown action"

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

    def _record(self, tool: str, inp: dict, ref: ControlRef | None) -> None:
        self.recorded.append({"tool": tool, "input": inp,
                              "control": ref.model_dump() if ref else None})

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

        if tool == "navigate":
            action = NavigateAction(url_template=canonical(inp["url"]))
            risk = RiskClass.SAFE_REVERSIBLE
        elif tool == "click":
            action = ClickAction()
            # A click that submits a search is reversible; a click that commits
            # a record is not. We classify conservatively by name and let a
            # human reviewer correct it before approval.
            risk = _classify_click(inp.get("name", ""))
        elif tool == "type_text":
            action = TypeAction(value_template=canonical(inp["text"]))
            risk = RiskClass.SAFE_REVERSIBLE
        elif tool == "read_value":
            action = ReadAction(
                output_name=inp["output_name"],
                transform="strip_currency" if "balance" in inp["output_name"] else "trim",
            )
            risk = RiskClass.SAFE_REVERSIBLE
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
