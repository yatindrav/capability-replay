# REQUIREMENTS — traceability

Every bullet in Section 3 mapped to the component that satisfies it. Maintained
during the build so no requirement quietly falls off. `DESIGN.md` §-numbers in
brackets.

Status key: **✔ verified by execution** · **✅ designed** · **🔨 to build** · **🧪 mocked at a seam**

`✅` means the design covers it and the code is present; `✔` means it was actually
run and observed. Everything below `3.3` that moved to `✔` was verified against the
live mock app, and the evidence directory is named in the row where there is one.

---

## 3.1 Goal-driven agent loop

| Requirement | Where | Status |
|---|---|---|
| Accept a goal + target (app/URL/entry) | `DiscoveryRequest` [§1] | ✅ |
| Observe → decide → act loop against a live surface | `agent/loop.py` [§2] | 🔨 |
| Stop on: goal met | `goal_reached` tool + final checkpoint verify | 🔨 |
| Stop on: max steps | `max_steps=25` | 🔨 |
| Stop on: timeout | `timeout_s=300` | 🔨 |
| Stop on: dead-end | no-progress detector (snapshot hash unchanged ×3) + `stuck` tool | 🔨 |
| Actually interact with a real UI | Playwright adapter, non-headless | 🔨 |
| Bias toward no-clean-DOM approaches | a11y tree primary, screenshot secondary, CSS demoted to fallback | ✅ |

## 3.2 Structured artifact

| Requirement | Where | Status |
|---|---|---|
| Ordered steps / actions | `CapabilityArtifact.steps: list[Step]` | ✅ |
| How each control is identified | `ControlRef` — role+name primary, ranked `fallbacks` | ✅ |
| **With reasoning about robustness** | `ControlRef.robustness_note`, required of the model at decision time | ✅ |
| Typed input parameters | `ParamSpec` — type, required, pattern, sensitivity | ✅ |
| Typed outputs **and their shape** | `OutputSpec` — `cardinality` + `type` + `fields` — **gap 1, closed** | ✅ |
| Checkpoint / success condition | `Step.checkpoint`, `CapabilityArtifact.success` | ✅ |
| Versioned | `version: int`, immutable, `v<N>.json` on disk | ✅ |
| Reviewable by a human | `intent` per step, `robustness_note`, `description`, declarative conditions | ✅ |
| Reviewable by a calling agent | `tool_schema()` → function-calling definition | ✅ |
| Decoupled from raw model transcript | `StepDraft` capture, not transcript parsing [§3] | ✅ |

## 3.3 Deterministic replay

| Requirement | Where | Status |
|---|---|---|
| Replay from artifact + params, no LLM in decision loop | `replay/engine.py` [§4] | ✔ |
| Stable control targeting | `surface/web.py` `resolve()`, ranked, ambiguity = failure | ✔ |
| Verify checkpoint / success condition | global → step → checkpoint order | ✔ |
| Return declared outputs | `ReplayResult.outputs` | ✔ |
| **Runtime condition: validation error** | `ConditionHandler` → `BUSINESS_OUTCOME`; four in the sub-account flow | ✅ |
| **Runtime condition: record not found** | `ConditionHandler` → `BUSINESS_OUTCOME` | ✔ |
| **Runtime condition: permission denial** | global `ConditionHandler` → `HARD_FAILURE` | ✅ |
| **Runtime condition: unexpected dialog** | `dialog_present` detector + unmodeled-blocker check — **gap 2, closed** | ✅ |
| **Runtime condition: session timeout** | global `ConditionHandler` → `RECOVERABLE`, `reauthenticate` → **flow restart** | ✔ |
| **Runtime condition: transient slowness** | detector polling to `timeout_ms`, `retry_step` recovery | ✅ |
| **Runtime condition: slow/failed load** | `load_failed` detector — **gap 3, closed** | ✅ |
| Separate expected business outcomes | `Disposition.BUSINESS_OUTCOME` → `ReplayStatus.BUSINESS_OUTCOME` | ✅ |
| Separate recoverable conditions | `Disposition.RECOVERABLE` → in-band `RecoveryAction` | ✅ |
| Separate hard failures | `Disposition.HARD_FAILURE` → `FailureDetail` | ✅ |
| Result: what step, expected, observed | `FailureDetail.step_id / stage / expected / observed` | ✅ |

## 3.4 Safety & policy guardrails

| Requirement | Where | Status |
|---|---|---|
| Configurable allowlist — permitted domains/routes | `allowlist.url_patterns`; `auth_url_patterns` for platform-only routes [§6] | ✔ |
| Configurable allowlist — permitted action types | `allowlist.action_kinds` | ✔ |
| Agent must not act outside it | `PolicyGate.check()`, single chokepoint, both paths | ✔ |
| Enforced on browser-initiated navigation | Playwright route interceptor | ✔ |
| Distinguish safe/reversible from risky/irreversible | `RiskClass` per step; the sub-account post is the `IRREVERSIBLE` case | ✅ |
| Handle risky class conservatively | `max_risk_unattended` → `CONFIRM` → escalate (justified in `REPORT.md` §6) | ✅ |
| Never persist secrets | `{{secret:name}}` refs resolved from env at replay | ✅ |
| Never persist raw PII in artifacts | `Sensitivity` labels; `example` never auto-populated — **gap 4, closed** | ✅ |
| Never persist raw PII in logs | `safety/redact.py`, masking + screenshot blur | 🔨 |

## 3.5 Evidence / observability

| Requirement | Where | Status |
|---|---|---|
| Structured log of what the agent did | `evidence.py`, per-run JSONL, `step_start`/`step_end` streamed | ✔ |
| **And why** | `intent` captured per action, carried into the log line | ✔ |
| At least one richer signal on failure | screenshot + a11y snapshot, refs in `FailureDetail.evidence_refs` | 🔨 |

## 3.6 Human-in-the-loop escalation & handoff

| Requirement | Where | Status |
|---|---|---|
| Detect stuck during discovery | 5 triggers [§5] incl. no-progress detector | 🔨 |
| Detect unrecoverable condition during replay | `Disposition.ESCALATE` + unmodeled-blocker check | 🔨 |
| Risky/irreversible step needs a person | `PolicyGate` → `CONFIRM` → escalation | 🔨 |
| Intervention request carries capability/goal | `InterventionRequest.capability_id`, `.goal` | ✅ |
| …the current step | `.current_step_id`, `.current_step_intent` | ✅ |
| …the current state or screenshot | `.screenshot_ref`, `.snapshot_ref` | ✅ |
| …and why it stopped | `.trigger`, `.reason` | ✅ |
| Human operates the **same live session** | lease transfer; context never closed or recreated | 🔨 |
| Hand control back, run resumes | `handback()` → re-evaluate checkpoint → resume or re-run step | 🔨 |
| Preserve context and evidence across handoff | evidence dir persists; lease events logged | 🔨 |
| **Record what the human did** | pre/post a11y snapshot diff → `human_action_summary` | 🔨 |
| Know who is (or should be) in control | `Session.lease.owner`, executor refuses to act unless `AUTOMATION` | 🔨 |
| Operator UI | bare local HTTP page | 🧪 |

## 3.7 Heterogeneity & scale (design only)

| Requirement | Where | Status |
|---|---|---|
| Surface abstraction seam | `SurfaceAdapter` — artifact says *what control*, adapter says *how to touch it* | ✅ |
| Extend to legacy web | `FrameRef.path`, `TABLE_CELL` / `TEXT_ANCHOR` strategies — this is our build target | 🔨 |
| Extend to desktop | role/name → UIA `ControlType`/`Name`; no schema change | ✅ design only |
| Reuse across tenants on the same vendor product | `app_id` vs `variant`; three-layer overlay resolution | ✅ design only |
| Safe specialization / override | overlay may override `ControlRef`/conditions/URLs — **never** step order, inputs, outputs | ✅ |
| Detect per-tenant/version drift | `fallback_depth` telemetry bucketed per variant | ✅ |
| Manage drift | rising depth queues a re-record for that variant only | ✅ |

---

## Gaps this audit found

**Gap 1 — outputs had no shape.** `OutputSpec` was scalar-only. "Read the
savings balance" fits; "list the member's sub-accounts" does not, and a calling
agent needs to know which it is getting *before* it invokes. Added `cardinality`
+ `type: record` + `fields`, capped at one level of nesting — a capability
returning a deep object graph is a sign the flow should have been split in two.

**Gap 2 — unexpected dialogs are unmodellable by definition.** The declarative
`ConditionHandler` model covers *known* conditions. An unexpected dialog has, by
construction, no handler. Two fixes:

- `dialog_present` detector kind, so *known* interstitials can be declared and
  dismissed as `RECOVERABLE`.
- An **unmodeled-blocker check** in the replay engine, running before checkpoint
  evaluation: if a `dialog`/`alertdialog` role is present that was not in the
  recorded observation, the run **escalates** rather than failing. This is the
  right disposition — a novel modal in a bank app is exactly the case where a
  human should look, not where automation should retry or give up.
- Native browser dialogs (`alert`/`confirm`/`beforeunload`) need a handler
  registered at session open. The original justification here — that they block
  Playwright entirely — is **wrong, and the truth is worse.** Measured against
  the mock app's `native_confirm` fault: with no handler, Playwright silently
  auto-*dismisses* the dialog. `onclick="return confirm(...)"` therefore returns
  false, the form never submits, and `click()` returns in 0.0s reporting
  success. The run believes it posted a transaction that never happened, and no
  checkpoint on the *click* can catch it. Registering the handler is what makes
  the dialog observable at all, and it is the only reason `dialog_present` can
  ever fire.

**Gap 3 — no load-failure detector.** 3.3 names "slow/failed load" as one
condition, but slow and failed want opposite dispositions: slow is
`RECOVERABLE` (poll, retry), failed is `HARD_FAILURE`. Detector polling already
covers slow; added `load_failed` for navigation errors and HTTP error pages.

**Gap 4 — `ParamSpec.example` was a PII leak.** The natural implementation
populates it from the discovery run's parameter values — which are live member
identifiers, written into an artifact that gets committed and shared across
tenants. Now explicitly author-supplied only; the recorder must leave it `None`.

## Still open

- **Structured extraction has no action yet.** `OutputSpec` can now *declare* a
  repeated record, but `ReadAction` extracts a single value. A `read_table`
  action (row scope + column refs) is needed to actually produce one. Deferred
  until the mock app exists — the shape of the fix depends on what a legacy
  table's a11y tree really looks like.
- The three items in `DESIGN.md` §9 (no-progress threshold, checkpoint
  synthesis volatility, extraction typing).
