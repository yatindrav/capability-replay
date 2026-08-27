"""
Deterministic replay: the production execution path.

No LLM is imported in this module, by design. The artifact fully determines the
next action; there is no model fallback in the failure path (see REPORT §7 for
why we argue against one).
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from cua.evidence import EvidenceRecorder
from cua.escalation.lease import (
    InterventionQueue,
    InterventionRequest,
    SessionLease,
    StuckReason,
    summarize_human_actions,
)
from cua.safety.policy import (
    Decision,
    PolicyGate,
    redact_params,
    resolve_secrets,
)
from cua.schema.artifact import (
    CapabilityArtifact,
    Checkpoint,
    ConditionHandler,
    ControlRef,
    Detector,
    Disposition,
    Step,
)
from cua.schema.result import (
    DISPOSITION_TO_STATUS,
    FailureDetail,
    ReplayResult,
    ReplayStatus,
    StepRecord,
)
from cua.surface.web import Observation, SurfaceAdapter

CURRENCY = re.compile(r"[^\d.\-]")


class ReplayEngine:
    def __init__(
        self,
        adapter: SurfaceAdapter,
        gate: PolicyGate,
        evidence: EvidenceRecorder,
        lease: SessionLease | None = None,
        interventions: InterventionQueue | None = None,
        no_progress_limit: int = 3,
        reauth=None,
    ):
        self.a = adapter
        self.gate = gate
        self.ev = evidence
        self.lease = lease or SessionLease()
        self.iq = interventions
        self.no_progress_limit = no_progress_limit
        self._reauth = reauth
        self._recent_digests: list[str] = []

    # --- detectors --------------------------------------------------------

    def _evaluate(self, d: Detector) -> bool:
        if d.kind == "text_present":
            return self.a.contains_text(d.value or "", d.case_sensitive)
        if d.kind == "text_absent":
            return not self.a.contains_text(d.value or "", d.case_sensitive)
        if d.kind == "url_matches":
            return re.search(d.value or "", self.a.current_url()) is not None
        if d.kind == "control_present":
            return bool(d.control) and self.a.resolve(d.control).ok
        if d.kind == "control_absent":
            return not (bool(d.control) and self.a.resolve(d.control).ok)
        if d.kind == "value_equals":
            if not d.control:
                return False
            res = self.a.resolve(d.control)
            return res.ok and self.a.read(res) == (d.value or "")
        return False

    def _check(self, cp: Checkpoint) -> bool:
        """Poll until satisfied or timeout. Never a fixed sleep."""
        deadline = time.time() + cp.timeout_ms / 1000
        while True:
            results = [self._evaluate(d) for d in cp.detectors]
            ok = all(results) if cp.require == "all" else any(results)
            if ok or time.time() >= deadline:
                return ok
            time.sleep(0.25)

    def _first_firing(self, handlers: list[ConditionHandler]) -> ConditionHandler | None:
        for h in handlers:
            if self._evaluate(h.detect):
                return h
        return None

    # --- escalation -------------------------------------------------------

    def escalate(
        self,
        reason: StuckReason,
        detail: str,
        *,
        run_id: str,
        capability_id: str,
        goal: str = "",
        step: Step | None = None,
        params: dict[str, Any] | None = None,
    ) -> InterventionRequest:
        obs = self.a.snapshot(with_screenshot=True)
        shot = self.ev.screenshot(f"escalation_{reason.value}", obs.screenshot_png)

        req = InterventionRequest(
            run_id=run_id,
            capability_id=capability_id,
            goal=goal,
            step_id=step.id if step else None,
            step_intent=step.intent if step else None,
            reason=reason,
            detail=detail,
            observed_url=obs.url,
            observed_tree=obs.tree,
            params_redacted=params or {},
            screenshot_path=shot,
        )

        self.lease.request_handoff(f"{reason.value}: {detail}")
        if self.iq:
            self.iq.raise_request(req)
        self.ev.log("escalation_raised", request_id=req.request_id,
                    reason=reason.value, detail=detail, step_id=req.step_id)
        return req

    def resume_after_handoff(self, req: InterventionRequest,
                             before_tree: str) -> list[str]:
        """Called once the operator hands the lease back.

        We diff the surface rather than replaying keystrokes, then let the
        caller re-evaluate the current step's checkpoint.
        """
        after = self.a.snapshot()
        summary = summarize_human_actions(before_tree, after.tree)
        if self.iq:
            self.iq.update(req.request_id, resolved=True,
                           human_action_summary=summary)
        self.ev.log("handoff_complete", request_id=req.request_id,
                    human_action_summary=summary)
        return summary

    # --- the main path ----------------------------------------------------

    def replay(self, art: CapabilityArtifact, params: dict[str, Any],
               start_at: str | None = None) -> ReplayResult:
        run_id = self.ev.run_id
        started = datetime.now(timezone.utc)
        t0 = time.time()
        records: list[StepRecord] = []
        outputs: dict[str, Any] = {}
        drift: list[str] = []

        safe_params = redact_params(params, art.inputs)
        self.ev.log("replay_start", capability_id=art.capability_id,
                    version=art.version, params=safe_params)

        # Validate inputs before touching the surface: a bad parameter should
        # fail before we are half-way through a form.
        try:
            self._validate(art, params)
        except ValueError as exc:
            return self._fail(art, run_id, started, t0, records, drift,
                              FailureDetail(step_id="(inputs)", stage="validation",
                                            expected="parameters matching declared specs",
                                            observed=str(exc)))

        started_yet = start_at is None
        for step in art.steps:
            if not started_yet:
                if step.id != start_at:
                    continue
                started_yet = True
                self.ev.log("resuming", step_id=step.id)

            rec = StepRecord(step_id=step.id, intent=step.intent,
                             action_kind=step.action.kind)
            s0 = time.time()

            terminal = self._run_step(art, step, params, rec, outputs, drift, run_id)
            rec.duration_ms = int((time.time() - s0) * 1000)
            records.append(rec)

            if terminal is not None:
                terminal.steps = records
                terminal.drift_signals = drift
                terminal.duration_ms = int((time.time() - t0) * 1000)
                terminal.evidence_dir = str(self.ev.dir)
                self.ev.log("replay_end", status=terminal.status.value,
                            outcome_code=terminal.outcome_code)
                return terminal

        # Overall success condition
        if not self._check(art.success):
            return self._fail(art, run_id, started, t0, records, drift,
                              FailureDetail(step_id="(success)", stage="success_condition",
                                            expected=art.success.description,
                                            observed=self._observe_brief()))

        result = ReplayResult(
            status=ReplayStatus.SUCCESS, capability_id=art.capability_id,
            capability_version=art.version, run_id=run_id, outputs=outputs,
            steps=records, started_at=started, drift_signals=drift,
            duration_ms=int((time.time() - t0) * 1000), evidence_dir=str(self.ev.dir),
        )
        self.ev.log("replay_end", status="success",
                    outputs=redact_params(outputs, art.outputs))
        return result

    def _run_step(self, art, step: Step, params, rec: StepRecord,
                  outputs: dict, drift: list, run_id: str) -> ReplayResult | None:
        """Execute one step. Returns a terminal ReplayResult, or None to continue."""
        attempt = 0
        while True:
            attempt += 1
            rec.attempts = attempt

            self.lease.assert_automation()

            # --- policy gate: every action, no exceptions ---
            url = None
            if step.action.kind == "navigate":
                url = _template(step.action.url_template, params)
            verdict = self.gate.check(step.action.kind, step.risk, url)

            if verdict.decision == Decision.DENY:
                self.ev.log("policy_denied", step_id=step.id, reason=verdict.reason)
                return self._terminal_fail(art, run_id, step,
                                           "policy", "action permitted by allowlist",
                                           verdict.reason)

            if verdict.decision == Decision.REQUIRE_CONFIRMATION:
                req = self.escalate(StuckReason.RISK_GATE, verdict.reason,
                                    run_id=run_id, capability_id=art.capability_id,
                                    step=step, params=redact_params(params, art.inputs))
                return ReplayResult(
                    status=ReplayStatus.ESCALATED, capability_id=art.capability_id,
                    capability_version=art.version, run_id=run_id,
                    escalation_id=req.request_id, message=verdict.reason,
                    resume_from_step=step.id,
                    started_at=datetime.now(timezone.utc),
                )

            # --- act ---
            try:
                self._act(step, params, rec, outputs)
            except _StepLocateError as exc:
                if step.optional:
                    self.ev.log("step_skipped", step_id=step.id, why=str(exc))
                    return None
                # Before calling it a hard failure, check whether a known
                # condition explains it — a validation error or a not-found
                # screen legitimately removes the control we were looking for.
                handled = self._handle_conditions(art, step, rec, run_id, params)
                if handled is not None:
                    return handled if isinstance(handled, ReplayResult) else None
                req = self.escalate(StuckReason.LOCATOR_EXHAUSTED, str(exc),
                                    run_id=run_id, capability_id=art.capability_id,
                                    step=step, params=redact_params(params, art.inputs))
                return ReplayResult(
                    status=ReplayStatus.ESCALATED, capability_id=art.capability_id,
                    capability_version=art.version, run_id=run_id,
                    escalation_id=req.request_id, message=str(exc),
                    failure=FailureDetail(step_id=step.id, stage="locate",
                                          expected=str(step.target),
                                          observed=str(exc)),
                    resume_from_step=step.id,
                    started_at=datetime.now(timezone.utc),
                )

            if rec.fallback_depth > 0:
                drift.append(f"{step.id}: resolved via fallback depth {rec.fallback_depth} "
                             f"({rec.resolved_by})")

            # --- no-progress detection ---
            obs = self.a.snapshot()
            self._recent_digests.append(obs.digest())
            if len(self._recent_digests) > self.no_progress_limit:
                self._recent_digests.pop(0)
            if (len(self._recent_digests) == self.no_progress_limit
                    and len(set(self._recent_digests)) == 1
                    and step.action.kind in ("click", "navigate")):
                req = self.escalate(
                    StuckReason.NO_PROGRESS,
                    f"surface unchanged across {self.no_progress_limit} actions",
                    run_id=run_id, capability_id=art.capability_id, step=step,
                    params=redact_params(params, art.inputs))
                return ReplayResult(
                    status=ReplayStatus.ESCALATED, capability_id=art.capability_id,
                    capability_version=art.version, run_id=run_id,
                    escalation_id=req.request_id, message="no progress",
                    resume_from_step=step.id,
                    started_at=datetime.now(timezone.utc))

            # --- conditions, then checkpoint ---
            outcome = self._handle_conditions(art, step, rec, run_id, params)
            if isinstance(outcome, ReplayResult):
                return outcome
            if outcome == "retry":
                if attempt <= 3:
                    continue
                return self._terminal_fail(art, run_id, step, "recovery",
                                           "recoverable condition to clear",
                                           "still firing after 3 attempts")

            if step.checkpoint:
                passed = self._check(step.checkpoint)
                rec.checkpoint_passed = passed
                if not passed:
                    # A checkpoint miss may itself be an expected outcome.
                    late = self._handle_conditions(art, step, rec, run_id, params)
                    if isinstance(late, ReplayResult):
                        return late
                    return self._terminal_fail(art, run_id, step, "checkpoint",
                                               step.checkpoint.description,
                                               self._observe_brief())
            return None

    def _handle_conditions(self, art, step, rec, run_id, params):
        """Global handlers first: a session timeout invalidates any step-level
        conclusion you would otherwise draw."""
        for scope, handlers in (("global", art.global_conditions),
                                ("step", step.conditions)):
            h = self._first_firing(handlers)
            if h is None:
                continue
            rec.conditions_fired.append(h.condition_id)
            self.ev.log("condition_fired", step_id=step.id, condition=h.condition_id,
                        disposition=h.disposition.value, scope=scope, message=h.message)

            if h.disposition == Disposition.RECOVERABLE:
                self._recover(h)
                return "retry"

            if h.disposition == Disposition.ESCALATE:
                req = self.escalate(StuckReason.CONDITION_ESCALATE, h.message,
                                    run_id=run_id, capability_id=art.capability_id,
                                    step=step, params=redact_params(params, art.inputs))
                return ReplayResult(
                    status=ReplayStatus.ESCALATED, capability_id=art.capability_id,
                    capability_version=art.version, run_id=run_id,
                    escalation_id=req.request_id, message=h.message,
                    resume_from_step=step.id,
                    started_at=datetime.now(timezone.utc))

            status = DISPOSITION_TO_STATUS[h.disposition]
            return ReplayResult(
                status=status, capability_id=art.capability_id,
                capability_version=art.version, run_id=run_id,
                outcome_code=h.outcome_code, message=h.message,
                failure=(FailureDetail(step_id=step.id, stage="condition",
                                       expected="no fault condition",
                                       observed=h.message)
                         if status == ReplayStatus.FAILED else None),
                started_at=datetime.now(timezone.utc))
        return None

    def _recover(self, h: ConditionHandler) -> None:
        r = h.recovery
        self.ev.log("recovery_attempt", condition=h.condition_id, kind=r.kind)
        if r.kind == "dismiss" and r.dismiss_control:
            res = self.a.resolve(r.dismiss_control)
            if res.ok:
                self.a.click(res)
        elif r.kind == "reload":
            self.a.navigate(self.a.current_url())
        elif r.kind == "reauthenticate":
            # Re-enter the session bootstrap. This is the loop closing: auth is
            # not in the artifact, so recovery delegates back to the platform.
            if self._reauth is not None:
                self._reauth()
            else:
                self.a.navigate(self.a.current_url())
        time.sleep(r.backoff_ms / 1000)

    # --- actions ----------------------------------------------------------

    def _act(self, step: Step, params, rec: StepRecord, outputs: dict) -> None:
        act = step.action

        if act.kind == "navigate":
            self.a.navigate(_template(act.url_template, params))
            rec.resolved_by, rec.fallback_depth = "url", 0
            return

        if act.kind == "wait":
            self._check(Checkpoint(detectors=[act.until], description="wait",
                                   timeout_ms=step.timeout_ms))
            return

        if act.kind == "assert":
            if not self._evaluate(act.detector):
                raise _StepLocateError(f"assertion failed: {act.detector.kind}")
            return

        if step.target is None:
            raise _StepLocateError(f"step {step.id} requires a target")

        res = self.a.resolve(step.target)
        if not res.ok:
            raise _StepLocateError(res.error or "control not found")
        rec.resolved_by, rec.fallback_depth = res.strategy, res.depth

        if act.kind == "click":
            self.a.click(res)
        elif act.kind == "type":
            value = resolve_secrets(_template(act.value_template, params))
            self.a.type_text(res, value, act.clear_first)
        elif act.kind == "select":
            self.a.select(res, _template(act.value_template, params))
        elif act.kind == "read":
            raw = self.a.read(res)
            outputs[act.output_name] = _transform(raw, act.transform)

    # --- helpers ----------------------------------------------------------

    def _validate(self, art: CapabilityArtifact, params: dict) -> None:
        for spec in art.inputs:
            if spec.name not in params:
                if spec.required:
                    raise ValueError(f"missing required parameter '{spec.name}'")
                continue
            if spec.pattern and not re.match(spec.pattern, str(params[spec.name])):
                raise ValueError(f"parameter '{spec.name}' does not match {spec.pattern}")

    def _observe_brief(self) -> str:
        obs = self.a.snapshot()
        return f"url={obs.url}; tree_digest={obs.digest()}"

    def _terminal_fail(self, art, run_id, step, stage, expected, observed):
        obs = self.a.snapshot(with_screenshot=True)
        shot = self.ev.screenshot(f"failure_{step.id}", obs.screenshot_png)
        snap = self.ev.snapshot(f"failure_{step.id}", obs.tree)
        return ReplayResult(
            status=ReplayStatus.FAILED, capability_id=art.capability_id,
            capability_version=art.version, run_id=run_id,
            failure=FailureDetail(step_id=step.id, stage=stage, expected=expected,
                                  observed=observed,
                                  evidence_refs=[p for p in (shot, snap) if p]),
            started_at=datetime.now(timezone.utc))

    def _fail(self, art, run_id, started, t0, records, drift, detail):
        self.ev.log("replay_end", status="failed", stage=detail.stage,
                    observed=detail.observed)
        return ReplayResult(
            status=ReplayStatus.FAILED, capability_id=art.capability_id,
            capability_version=art.version, run_id=run_id, failure=detail,
            steps=records, started_at=started, drift_signals=drift,
            duration_ms=int((time.time() - t0) * 1000), evidence_dir=str(self.ev.dir))


class _StepLocateError(Exception):
    pass


def _template(tpl: str, params: dict) -> str:
    out = tpl
    for k, v in params.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _transform(raw: str, mode: str) -> Any:
    if mode == "strip_currency":
        cleaned = CURRENCY.sub("", raw)
        return float(cleaned) if cleaned else None
    if mode == "digits_only":
        return re.sub(r"\D", "", raw)
    if mode == "trim":
        return raw.strip()
    return raw


def new_run_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"
