# DESIGN — Computer-Use Capability System

**Status:** implemented. Replay, safety, escalation and recording run against
the mock app and are covered by tests; the sections below note where the build
diverged from the plan and why. `PLAN.md` tracks what is deliberately not built.
**Relationship to `REPORT.md`:** `REPORT.md` is the submitted write-up, organized
around the evaluation headings. This document is the build specification —
organized around the six capabilities the system must have, with the component
boundaries and interfaces we implement against. Where they overlap, this document
is the source of truth for *how*, `REPORT.md` for *why*.

---

## 0. The through-line

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the AI agent invokes it in production.

Every design decision below is checked against one question: **does this keep the
model out of the production execution path?** If a mechanism is convenient during
discovery but would require a model call at replay time, it does not go in the
artifact.

Two paths through one machine:

```
DISCOVERY (once, expensive, supervised)
  goal ──> LLM loop ──> live surface ──> success ──> artifact

REPLAY (many, cheap, unattended)
  capability_id + params ──> artifact ──> live surface ──> typed result
```

They share the executor, the action vocabulary, the observation format, the
policy gate, and the evidence writer. **Only the decider differs** — an LLM in
one case, a list of steps in the other. That symmetry is the core architectural
bet: it makes recording faithful by construction, because discovery cannot take
an action that replay is unable to express.

---

## 1. Stage 1 — Goal intake

### Input contract

```python
class DiscoveryRequest(BaseModel):
    goal: str                    # "look up member {member_id} and read their savings balance"
    params: dict[str, str]       # {"member_id": "12345"}  — concrete values for this run
    param_specs: list[ParamSpec] # typed declaration of what those params are
    target: TargetBinding        # app_id, variant, surface kind, entry URL
    allowlist_id: str
    max_steps: int = 25
    timeout_s: int = 300
```

### Decision: parameterize at intake, not by post-hoc inference

The obvious approach is to accept `"look up member 12345"` and, after the run,
ask a model which literals were parameters. We reject that. Inference over a
transcript is guessy — `12345` could be a member ID, a branch code, or an account
suffix, and getting it wrong silently produces a capability that hardcodes one
member's data. Instead the caller declares parameters up front, and the goal
carries `{placeholders}`. Discovery substitutes them to get a runnable goal
string; recording knows exactly which typed values flowed into which steps,
because it watched them go in.

Cost: slightly more ceremony to launch a discovery run. Benefit: the input half
of the capability contract is correct by construction rather than by inference.

### Bootstrap

1. Validate `params` against `param_specs` (`pattern`, `required`, type).
2. Load and validate the allowlist; refuse to start if `target.entry_url_template`
   is not on it.
3. Open a `Session` (long-lived, non-headless browser context) and register the
   navigation interceptor.
4. Create `evidence/<run_id>/` and start the structured log.

---

## 2. Stage 2 — Discovery: the LLM loop

### The loop

```
  ┌─────────────────────────────────────────────┐
  │  observe:  adapter.snapshot() -> Observation │
  │  decide:   LLM(goal, observation, history)   │
  │            -> Action + intent + control ref  │
  │  gate:     PolicyGate.check(action)          │
  │  act:      executor.execute(action)          │
  │  record:   append StepDraft                  │
  └────────────────┬────────────────────────────┘
                   │ repeat until
                   ▼
     goal_reached | max_steps | timeout | dead_end | escalation
```

### Observation format

The model sees what replay will see — nothing richer.

```python
class Observation(BaseModel):
    url: str
    title: str
    frames: list[FrameRef]
    controls: list[ObservedControl]   # role, name, value, frame_path, enabled, bbox
    text_content: str                 # visible text, truncated & redacted
    screenshot_ref: str | None        # path; passed to the model for spatial cases
    snapshot_hash: str                # drives the no-progress detector
```

`ObservedControl` is deliberately the same shape `ControlRef` targets. When the
model says "click the control with role=button, name='Search'", that maps 1:1
into the artifact. No translation layer, no lossy re-derivation.

**As built, this is not what shipped.** `Observation` carries the flattened
accessibility tree as text (`tree`, `url`, `frames`, `digest()`), and the model
reads controls out of it rather than being handed an enumerated
`list[ObservedControl]`. The 1:1 claim above is therefore weaker in practice
than on paper: the model names a role and an accessible name in its tool call,
and `_control_ref()` builds the `ControlRef` from those arguments. Since the
tool schema asks for exactly the fields `ControlRef` holds, nothing is inferred
— but the model is parsing a text blob to fill them, and a mis-parse is possible
where an enumerated list would have made it impossible. Building `ObservedControl`
properly is the single highest-value item left; it is listed in `PLAN.md` under
work deliberately off the critical path.

**Redaction happens before the model sees it.** `_observe_block()` runs
`redact_text()` over the accessibility tree on the way to the model, and over any
value a `read` returns. The model does not need a member's SSN to decide where to
click.

**As built:** redaction is pattern-based (SSN, card, email shapes) plus
`Sensitivity` labels on declared params and outputs. The `field_rules` block
sketched in §6 — matching controls by name to assign sensitivity — was not built,
so sensitivity on an artifact's inputs and outputs is author-supplied rather than
inherited from the allowlist. The consequence is narrow but real: a PII field
this system has never been told about, and whose value matches no pattern, is
redacted in neither the log nor the model's view.

### Decision: constrained tool-calling, not free-form output

The model is given exactly the action vocabulary from the artifact schema as
function-calling tools — in the code: `navigate`, `click`, `type_text`,
`select_option`, `read_value`, `wait_for`, `assert_state`, plus two control
tools, `finish(success_text, summary)` and `stuck(reason)`. It cannot emit
anything else, and each maps to exactly one `Action` the replay engine already
executes. That is the check that keeps the two paths symmetric: a tool the model
can call but replay cannot express would break recording by construction.

Each action tool requires the model to supply, alongside the action:
- `intent` — why, in plain language (carried into the artifact for reviewers)
- `target` — role/name/frame of the control
- `robustness_note` — why this targeting should survive re-runs
- `risk` — its classification of the action

Requiring `robustness_note` and `risk` at decision time, rather than deriving
them afterward, is deliberate: it makes the model reason about durability and
danger *while* it chooses, and it puts a reviewable justification in the artifact
for every step.

`risk` is required on every state-changing tool and is passed straight to
`PolicyGate.check()`, so the gate constrains discovery exactly as it constrains
replay. The label-matching heuristic that predated this (`_classify_click`)
survives only as a cross-check: when the model's declaration and the heuristic
disagree, the run logs `risk_disagreement`, which is either a mis-declaration or
a control whose label lies. Both are worth a reviewer's attention.

### Stopping conditions

| Condition | Outcome |
|---|---|
| `goal_reached` called and final checkpoint verifies | Proceed to recording |
| `goal_reached` called but checkpoint fails | Continue loop; the model is told it was wrong |
| `stuck` called | Escalate (Stage 5) |
| `max_steps` / `timeout_s` exceeded | Escalate |
| No-progress: `snapshot_hash` unchanged across 3 actions | Escalate |
| Policy gate denial | Return denial to the model as an observation; 2 strikes then escalate |

A gate denial is fed back rather than fatal — the model may have reached for a
disallowed action and can pick a permitted one. Repeated denials mean it is
trying to do something the capability is not permitted to do, which is an
escalation, not a retry.

---

## 3. Stage 3 — Recording: run → artifact

### Decision: record incrementally, distill at the end

We do **not** parse the model transcript to build the artifact. We emit a
`StepDraft` at the moment each action is accepted and executed, capturing what
*actually happened* rather than what the model said it would do — including the
locator strategy that genuinely resolved the control.

```python
class StepDraft(BaseModel):
    seq: int
    action: Action
    intent: str
    control_ref: ControlRef | None      # as resolved, incl. working strategy
    resolved_by: LocatorStrategy | None
    risk: RiskClass
    observation_before: str             # snapshot hash
    observation_after: str
    succeeded: bool
    superseded: bool = False            # set during distillation
```

This is what "decoupled from the raw model transcript" means concretely: the
transcript is evidence, the drafts are the record.

**As built:** the decoupling is real — `DiscoveryAgent._record()` appends at the
moment each action is *accepted and executed*, and `build_artifact()` reads only
those records, never the message history. But the record is a plain dict of
`{tool, input, control, risk}`, not the typed `StepDraft` above, and it omits
`succeeded`, `observation_before/after` and `superseded`. Two consequences,
both honest to state: an action that failed is not recorded at all rather than
recorded-and-pruned, and backtrack pruning (distillation step 1) does not
happen, because nothing carries the before/after hashes it would need. On flows
this short the model rarely backtracks, so the artifacts are clean in practice
— but that is luck holding, not a mechanism.

### Distillation pass (deterministic, no model call)

1. **Prune backtracks.** Any draft whose effect was undone by a later draft, or
   that failed, is marked `superseded` and dropped from the artifact. Kept in
   evidence.
2. **Synthesize checkpoints.** For each surviving step, derive a `Checkpoint`
   from the observation delta — a control or text present after that was absent
   before. This is where "assert the click worked" comes from, mechanically.
3. **Promote extractions to outputs.** Each `ReadAction` becomes an `OutputSpec`,
   typed from the extracted value's shape, `sensitivity` inherited from the
   allowlist's field rules.
4. **Template the values.** Any typed or navigated literal matching a supplied
   param value is replaced with `{param_name}`. Because params were declared at
   intake (§1), this is exact string substitution, not inference.
5. **Attach global conditions** from the app profile for `target.app_id`
   (session timeout, permission denied, app error) — shared across capabilities
   for the same product rather than rediscovered per recording.
6. **Emit** `CapabilityArtifact` at `version=1`, `approval_state=draft`.

### Storage

`capabilities/<app_id>/<capability_id>/v<N>.json`. Immutable. Re-recording writes
`v<N+1>`; nothing ever mutates in place, so an audit can reconstruct exactly which
capability version produced a given production result.

---

## 4. Stage 4 — Deterministic replay

### Entry point

```python
def replay(capability_id: str, version: int | None, params: dict) -> ReplayResult
```

### Sequence

```
load artifact ──> validate params vs inputs ──> check approval_state
       │
       ▼
  open session, install nav interceptor
       │
       ▼
  for each step:
       resolve(ControlRef) ──> gate.check(action) ──> execute
              │
              ▼
       evaluate global_conditions        ← session timeout, permission denied
              │
              ▼
       evaluate step conditions          ← "no such member", validation error
              │
              ▼
       assert checkpoint
       │
       ▼
  assert success checkpoint ──> collect outputs ──> ReplayResult
```

Condition order is load-bearing: **global before step before checkpoint.** A
session timeout invalidates any conclusion you would draw from a step-level
check, so it must be tested first.

### Resolution

Try `role+name`, then each `fallback` in rank order. A strategy that resolves to
**more than one** control is a failure, not a first-match. Record `resolved_by`
and `fallback_depth` on the `StepRecord`; any depth > 0 emits a `drift_signal`.

### Waiting

There are no sleeps. Every wait is "poll until this `Detector` is true, or
`timeout_ms` elapses." Poll interval 250ms.

### Result

`ReplayResult` per `cua/schema/result.py` — one of `success` (with outputs),
`business_outcome` (with `outcome_code`), `failed` (with `FailureDetail`:
step, stage, expected, observed, evidence refs), or `escalated`.

---

## 5. Stage 5 — Escalation & handoff

### Lease state machine

```
                 pause()                 accept()
  AUTOMATION ──────────────> PENDING ──────────────> OPERATOR
      ▲                         │                        │
      │                         │ timeout                │ handback()
      │                         ▼                        │
      └──────── resume() ─── ABANDONED <─────────────────┘
```

`Session.lease.owner` is checked by the executor before **every** action. If the
owner is not `AUTOMATION`, the executor refuses — so a late or duplicated action
cannot race the human.

### The critical property

The browser context is never closed, never recreated, never replicated. Pausing
means the executor stops issuing actions into a context that stays exactly as it
was. The human drives the same session, same cookies, same auth token, same form
state, same scroll position. "Transfer the live session" reduces to "change who
holds the lease."

### Intervention request

```python
class InterventionRequest(BaseModel):
    escalation_id: str
    run_id: str
    capability_id: str | None
    goal: str
    current_step_id: str | None
    current_step_intent: str | None
    trigger: str              # which of the five detectors fired
    reason: str
    screenshot_ref: str
    snapshot_ref: str
    params_redacted: dict
    created_at: datetime
```

### Hand-back

1. Snapshot the surface fresh.
2. Diff against the pre-handoff snapshot; write the delta as
   `human_action_summary`. **We record what changed, not keystrokes** — that
   keeps PII out of the evidence log while still answering "what did the operator
   do?"
3. Re-evaluate the current step's checkpoint.
   - passes → resume at the next step
   - fails → re-run the current step
4. Return the lease to `AUTOMATION`.

### Operator surface (mocked, deliberately)

A bare local HTTP page: pending requests, context, screenshot, Take Control /
Hand Back. Real co-browsing — input relay, viewport sync — is out of scope. The
lease machine, the executor's refusal to act, and the diff capture are real.

---

## 6. Stage 6 — Safety guardrails

### Single chokepoint

```python
class PolicyGate:
    def check(self, action: Action, ctx: GateContext) -> GateDecision  # ALLOW | DENY | CONFIRM
```

Every action from **both** paths passes through it before reaching the adapter.
There is no second route to the surface. Discovery and replay are governed
identically, so a prompt-injected model has exactly the authority a reviewed
artifact has — none extra.

### Allowlist

```yaml
id: acme-servicing-readonly
url_patterns:
  - "http://localhost:8099/servicing/**"
allowed_actions: [navigate, click, type, select, read, wait, assert]
max_risk_unattended: safe_reversible
field_rules:
  - match: {name_contains: "SSN"}    ; sensitivity: secret
  - match: {name_contains: "Balance"} ; sensitivity: pii
```

Enforced on **browser-initiated** navigation too, via a route interceptor — a
redirect or an injected link cannot walk the session off-allowlist.

### Risk handling

`RiskClass` is recorded per step at discovery, so posture is reviewable before
approval rather than computed mid-run. Unattended replay is capped at
`SAFE_REVERSIBLE`; anything above returns `CONFIRM`, which escalates for human
decision rather than blocking outright. Blocking outright would make the system
useless for exactly the write flows that carry the business value.

### Data handling

- Secrets are `{{secret:name}}` refs resolved from env at replay time. Never
  written to artifact, log, or evidence.
- `PII` fields are masked in logs and evidence; bounding boxes blurred in
  screenshots.
- Model transcripts are never persisted — only `provenance.transcript_ref`
  pointing at redacted evidence.

---

## 7. Module layout

This is the tree as built. It is flatter than the layout this section originally
sketched, and deliberately so: several of the modules planned here turned out to
be one function each, and splitting a file per noun makes a reviewer open six
files to follow one decision.

```
cua/
  schema/
    artifact.py     CapabilityArtifact, Step, ControlRef, ConditionHandler
    result.py       ReplayResult, StepRecord, FailureDetail, EscalationRecord
  surface/
    web.py          Playwright adapter — a11y snapshot, resolution, actions,
                    dialog capture. SurfaceAdapter Protocol + Observation live
                    here too; a desktop adapter is a sibling file, not a
                    schema change
    session.py      session bootstrap. Auth is NOT part of an artifact [§1]
  agent/
    discovery.py    the LLM loop, its tool vocabulary, and the distillation
                    pass that turns accepted tool calls into a capability
    recorder.py     verification replay + storage [§10 Join 1]
  replay/
    engine.py       step execution, detector evaluation, condition handling,
                    escalation, result assembly
  safety/
    policy.py       allowlist, PolicyGate, redaction
  escalation/
    lease.py        lease state machine, InterventionRequest, queue
  evidence.py       per-run structured log, screenshots, snapshots
  __main__.py       discover | replay | catalog | operator
mockapp/app.py      legacy-style Flask target with injectable faults
tools/              seed_artifact.py, demo.sh
tests/              units + browser-driven integration
capabilities/<app_id>/<capability_id>/v<N>.json
evidence/<run_id>/
```

Four consolidations are worth naming, since each was a plan that did not survive
contact:

- **`resolver.py` + `detectors.py` → `engine.py` and `web.py`.** Detector
  evaluation is a dozen lines over the adapter's interface, and ranked
  resolution is meaningless away from the adapter that executes it. Separating
  them would have created two files whose only content was a call.
- **`gate.py` + `allowlist.py` + `redact.py` → `policy.py`.** These are one
  policy, and the whole argument of §6 is that there is exactly one chokepoint.
  Three files would have implied three.
- **`loop.py` + `tools.py` → `discovery.py`.** The tool schemas *are* the
  action vocabulary; keeping them beside the loop that dispatches them is what
  makes it checkable that the model gets no richer powers than replay has.
- **`cli.py` → `__main__.py`.** So `python -m cua` works without an install
  step. (`pyproject.toml`'s console script pointed at `cua.cli:main`, which
  never existed — fixed.)

`schema/session.py` and `surface/base.py` were never needed: `Session` collapsed
into the lease plus the Playwright context, and the `SurfaceAdapter` Protocol is
eight lines that belong next to its only implementation until there is a second.

---

## 8. Build order

Replay before discovery, deliberately: it forces the artifact to be genuinely
executable, so the discovery agent has a real target to emit rather than
something plausible-looking.

| # | Milestone | Done when |
|---|---|---|
| 1 | Mock legacy app | Framesets, table layout, no test IDs; faults injectable by query param (`?fault=timeout`) |
| 2 | Surface adapter | `snapshot()` returns a usable `Observation` from the frameset app |
| 3 | Safety gate | Allowlist blocks off-domain nav, including browser-initiated |
| 4 | Replay engine | A hand-written artifact replays green against the mock app |
| 5 | Error paths | Same artifact returns `business_outcome` on bad member, `failed` on injected fault |
| 6 | Discovery loop | Real LLM run completes the goal, evidence in `/evidence/` |
| 7 | Recorder | Discovery emits an artifact that milestone 4's engine replays |
| 8 | Escalation | Lease machine + mock console; a run pauses, a human acts, it resumes |
| 9 | Evidence + README | Discovery log, replay log, failing replay log, artifact |

Milestone 6 is the only one that cannot be stubbed — the brief requires a genuine
LLM-driven run against a live surface.

### Test plan

- Unit: detector evaluation, resolver ranking/ambiguity, param templating,
  redaction, lease transitions.
- Integration: milestones 4, 5, 8 against the mock app — these are the ones worth
  real tests, since they encode the three-way error taxonomy and the control
  transfer.
- Not tested: the discovery loop end-to-end. Nondeterministic and costs API
  calls; covered by recorded evidence instead.

---

## 9. Open questions to settle during build

1. **No-progress threshold.** 3 unchanged snapshots is a guess. It must not fight
   the checkpoint timeout on a slow legacy page — tune once the mock app exists.
   **Not yet tuned against a slow page.** It has not misfired, but the mock app
   answers in milliseconds, so that is not evidence. The `slow` fault (a 6s
   sleep) is the thing to test it against.
2. **Checkpoint synthesis quality.** ~~Deferred until we see real deltas.~~
   **Settled by §10 Join 1.** The verification replay makes this self-detecting:
   an artifact whose checkpoint captured the sub-account receipt's posting
   timestamp fails its own verification and never reaches `capabilities/`. A
   volatility filter exists in `agent/recorder.py`, but only as a *reporting*
   aid that names the offending string — the replay is the verdict. Filtering on
   suspicion alone would silently drop legitimate checkpoints that happen to
   contain the word "reference".
3. **Extraction typing.** Inferring `OutputSpec.type` from a value's shape is
   fragile for currency and dates. May need the model to declare the type in the
   `read` tool call instead.

---

## 10. The end-to-end thread

The brief's acceptance criterion is one continuous thread:

> a goal → an LLM-driven run that completes it → a saved capability artifact → a
> deterministic replay with input params, outputs, and error/outcome handling →
> a human-escalation path that can take over the live session → evidence for
> both runs.

Tracing it link by link found three places where we had both components and not
the join between them. All three are closed below.

### The thread, with its joins named

```
  goal + params                                          [§1]
       │
       ▼
  LLM discovery run ─────────────► evidence/disc_<id>/   [§2]
       │                             log.jsonl, snapshots,
       │                             screenshots, transcript(redacted)
       ▼
  StepDrafts ──► distillation ──► CapabilityArtifact v1  [§3]
       │
       ├──────────── JOIN 1: verification replay ────────┐
       │                                                 │
       ▼                                                 │
  capabilities/<app>/<cap>/v1.json  (draft_verified) ◄───┘
       │
       ▼
  replay(capability_id, params')  ────► evidence/rep_<id>/  [§4]
       │                                  log.jsonl, snapshots
       │
       ├── success ──► outputs
       ├── business_outcome ──► outcome_code
       ├── failed ──► FailureDetail
       └── escalate ──► JOIN 2 ──► lease to OPERATOR ──► handback  [§5]
                                        │
                                        └──► run resumes, returns
                                             SUCCESS with an
                                             EscalationRecord

  JOIN 3: audit chain
  ReplayResult.run_id → capability_id/version → provenance.discovery_run_id
                     → evidence for both runs, from either end
```

### Join 1 — verification replay (was missing)

Recording emitted an artifact and the design assumed it was replayable. It might
not be: checkpoint synthesis derives assertions from observation deltas, and a
delta containing a timestamp or session id produces a checkpoint that fails on
the very next run. Discovering that in production is unacceptable, and
discovering it manually is exactly the kind of thing that gets skipped.

**So the recorder does not finish at "artifact written."** Immediately after
distillation, it replays the fresh artifact once — same app, same params, same
executor, no model — and only then marks it `draft_verified`. An artifact that
fails its own verification replay is written to `evidence/` with the failure
detail attached, never to `capabilities/`.

Three things fall out of this for free: the volatile-checkpoint problem (§9.2)
becomes self-detecting rather than theoretical; `stability` gets its first data
point; and the demo's second evidence run exists without extra work.

### Join 2 — escalation is a pause, not a terminus (was wrong)

`ReplayStatus.ESCALATED` was terminal, which breaks the thread: the brief
requires that after handback the run "resume or complete." Corrected semantics:

- An escalation that a human **resolves** does not change the run's status. The
  run resumes and returns `SUCCESS` or `BUSINESS_OUTCOME` like any other, with an
  `EscalationRecord` in `ReplayResult.escalations` so the result still tells the
  truth about how the answer was obtained.
- `ESCALATED` is returned **only** when an escalation goes unresolved — the
  request timed out or was abandoned.

A caller checking `ok_for_caller` therefore gets `True` for a run that paused for
a human and finished, which is correct: it got its answer.

### Join 3 — the audit chain (was implicit)

"Evidence for both runs" is not two unrelated directories. An auditor asks: *this
production result — where did the capability that produced it come from?*

`ReplayResult.discovery_run_id` is copied from the artifact's provenance at load
time, so the chain is walkable from either end:

```
evidence/rep_004/  ──► ReplayResult.capability_id + version
                  ──► capabilities/acme-servicing/member.savings_balance.read/v1.json
                  ──► provenance.discovery_run_id = "disc_001"
                  ──► evidence/disc_001/
```

### Demonstration requirement this implies

The replay in `/evidence/` must use a **different parameter value** than the
discovery run did. Replaying with the same member the model happened to look up
demonstrates nothing about parameterization — it is indistinguishable from a
hardcoded flow. The demo path replays with a second member, plus a third run
with a nonexistent member to exercise `BUSINESS_OUTCOME`.
