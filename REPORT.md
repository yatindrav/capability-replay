# REPORT

A record-once / replay-many system for driving legacy back-office UIs. An LLM
discovers how to accomplish a goal against a live surface; the successful run is
distilled into a typed, versioned **capability artifact**; production invocations
replay that artifact with no model in the decision loop.

---

## 1. Architecture

Four components in a single process, communicating through plain function calls
and JSON on disk. No queues, no services, no database.

```
   goal + target                      capability_id + params
         |                                      |
         v                                      v
  +--------------+                      +--------------+
  |  Discovery   |                      |    Replay    |
  |  (LLM loop)  |                      | (no LLM)     |
  +------+-------+                      +------+-------+
         |          both emit/consume          |
         |            the same Action          |
         +----------------+---------------------+
                          v
                  +---------------+
                  |  Policy Gate  |   <- every action, both paths
                  +-------+-------+
                          v
                  +---------------+
                  | Surface       |   a11y snapshot in, actions out
                  | Adapter       |
                  +-------+-------+
                          v
                   live application
```

**The decision that shapes everything: discovery and replay share one executor
and one action vocabulary.** The LLM does not get a richer set of powers than
replay has. It chooses from exactly the actions an artifact can express, against
exactly the observation format replay will see, through exactly the same policy
gate. If the model can do it, the artifact can encode it — so a successful
discovery run is recordable by construction rather than by hopeful translation.
This is also the security property: there is no path to the surface that bypasses
the gate, so "the model went rogue" and "the artifact was tampered with" collapse
into the same enforcement point.

**Perception is the accessibility tree, not the DOM.** The adapter hands the
model a flattened a11y snapshot — role, accessible name, value, frame path — plus
a screenshot for spatial disambiguation. Two reasons. First, the brief says the
common case has no clean DOM; an a11y tree exists even for a frameset-era app
rendered from tables, because the browser synthesises one. Second, role+name is
the one addressing scheme shared by browsers, Windows UIA, macOS AX and AT-SPI,
so it is the only perception format that ports to desktop without redesign.

**Trade-off accepted:** a11y trees are lossier than the DOM. Badly built legacy
apps produce generic roles and empty names, and we will sometimes fall back to
text-anchored or table-cell targeting. We took that cost deliberately — a
DOM-selector design would score better against a modern web app and would have to
be thrown away for the surfaces that actually matter here.

**Trade-off accepted:** single process means no horizontal scale and no crash
isolation between runs. The brief explicitly penalises building that
infrastructure; the seam that would allow it (Executor is stateless given a
Session) is present, unbuilt.

---

## 2. Artifact schema

Full definition in `cua/schema/artifact.py`. The artifact is a **capability
contract**, not a macro recording — a calling agent must be able to read it and
know what it needs and what it returns, and `CapabilityArtifact.tool_schema()`
renders it directly as a function-calling definition.

| Field | Purpose |
|---|---|
| `capability_id` + `version` | Stable name, immutable versions. Re-recording bumps, never mutates. |
| `target` | `app_id` (vendor product) separated from `variant` (tenant instance) — see §4. |
| `inputs` / `outputs` | Typed, described, with a `sensitivity` label driving redaction. |
| `steps` | Ordered actions, each with `intent`, `ControlRef`, `risk`, `checkpoint`. |
| `global_conditions` | Handlers checked after every step: session timeout, permission denial. |
| `policy` | Allowlist binding + max risk class permitted unattended. |
| `provenance` | Model, run id, timestamp — and a *reference* to redacted evidence, never an inline transcript. |
| `approval_state` / `stability` | draft → approved gate, plus observed replay reliability. |

**`ControlRef` is the load-bearing type.** Only `role` is required; `name` is the
primary discriminator. Everything else narrows (`frame`, `nth`, `near_text`,
`within_section`) or rescues (`fallbacks`, a ranked list of `LocatorHint`). CSS
and XPath exist in that fallback list but are marked adapter-private — the
desktop adapter simply ignores strategies it cannot honour, and degrades to the
portable ones. `robustness_note` carries the recorder's reasoning in prose,
because a human reviewing a capability before approval needs to know *why* the
recorder thought this targeting would hold.

**`ConditionHandler` is declarative rather than code.** "If you see this text, it
means `MEMBER_NOT_FOUND`, and that is a business outcome — return it." Three
consequences: replay never improvises, a reviewer can audit a capability's entire
failure surface by reading the artifact, and handlers can be inherited from a
shared app profile across tenants instead of re-derived per recording.

**What is deliberately *not* in the schema:** the model transcript, any observed
screen content, any credential, and any concrete selector as a primary. The first
three are PII and regulatory exposure. The fourth would silently make the schema
web-only.

---

## 3. Determinism & error handling

### Determinism

Replay is deterministic in the sense that matters: same artifact + same inputs →
same sequence of resolved controls and asserted checkpoints.

- **No model in the decision loop.** The artifact fully determines the next
  action. There is no fallback to the LLM (see §7 for why we cut assisted
  recovery).
- **Ranked locator resolution.** Try `role+name`, then each fallback in order.
  Resolution must yield *exactly one* control; ambiguity is a failure, not a
  coin-flip on the first match. The strategy that succeeded is recorded as
  `resolved_by` / `fallback_depth`.
- **Checkpoints instead of sleeps.** Every step asserts a post-condition before
  the next begins. Waiting is "wait until this detector is true, up to
  `timeout_ms`" — never a fixed delay.
- **Templated values.** Parameters are substituted into `value_template` /
  `url_template` at replay time and validated against `pattern` before the run
  starts, so a malformed input fails fast rather than half-way through a form.

### Error handling — the three-way split

The brief names conflating business outcomes with failures as the most common
design mistake, so the taxonomy is enforced by types (`Disposition`,
`ReplayStatus`) rather than by convention.

| Disposition | Meaning | Result |
|---|---|---|
| `BUSINESS_OUTCOME` | A legitimate negative answer the caller asked for. "No such member." | `status=business_outcome`, `outcome_code` set. **Not an error.** |
| `RECOVERABLE` | Expected noise. Interstitial to dismiss, transient load to retry, session to re-establish. | Handled in-band via `RecoveryAction`, run continues, logged. |
| `HARD_FAILURE` | Unrecognised state, exhausted locators, failed checkpoint. | `status=failed` with `FailureDetail`: step, stage, expected, observed, evidence refs. |
| `ESCALATE` | Cannot safely proceed, but a human could. | `status=escalated` + intervention request (§5). |

`ReplayResult.ok_for_caller` returns true for the first two. A caller that only
checks `status == SUCCESS` still behaves correctly, because a business outcome is
never dressed up as success — but it is also never raised as an exception.

Handler evaluation order after each step: **global conditions → step conditions →
checkpoint**. Global first, because a session timeout invalidates any conclusion
you would draw from a step-level check.

### UI drift (secondary)

Drift shows up as fallback usage before it shows up as failure. Every
`ReplayResult` carries `drift_signals` when a step resolved below the primary
strategy. Aggregated per `app_id`/`variant`, a rising fallback depth is the
early-warning signal that a tenant's app was upgraded — actioned by re-recording,
not by making replay smarter.

---

## 4. Heterogeneity & multi-tenant

### Surface abstraction

The seam is: **the artifact describes *what control, semantically*; the adapter
knows *how to find and touch it on this surface*.**

```
SurfaceAdapter (interface)
  snapshot()        -> Observation   (roles, names, values, frame paths, screenshot)
  resolve(ControlRef) -> Handle | Ambiguous | NotFound
  act(Handle, Action) -> None
```

- **Web** (implemented): Playwright, a11y snapshot, `get_by_role` resolution,
  CSS/XPath fallbacks available.
- **Legacy web** (implemented as our target): same adapter, `FrameRef.path`
  carries frameset navigation, `TABLE_CELL` and `TEXT_ANCHOR` strategies carry
  the weight because names are frequently absent.
- **Desktop** (designed, not built): pywinauto/UIA or AT-SPI. `role` and `name`
  map almost directly onto UIA `ControlType` and `Name`. `FrameRef.path` becomes
  the window/pane hierarchy. `NavigateAction` becomes launch-or-focus.
  CSS/XPath hints are ignored. **No schema change required** — which is the test
  the abstraction had to pass.

### Multi-tenant reuse

Three-layer resolution at load time:

```
base artifact (app_id, variant="base")
   + variant overlay (tenant or version specific)
   = effective artifact
```

An overlay may override only: `entry_url_template`, individual `ControlRef`s by
step id, `ConditionHandler`s, and timeouts. It **may not** change step order,
inputs, or outputs. That restriction is the point — it guarantees the capability
contract is identical across every tenant running the vendor product, so the
calling agent's understanding of what it invokes never varies by institution.

A tenant whose flow genuinely differs in *steps* is not an overlay. That is a
fork: a new capability version recorded against that variant, deliberately
visible in review rather than hidden in a config file.

Drift detection is the `fallback_depth` telemetry above, bucketed per variant. A
tenant whose fallback depth rises while its peers stay flat has been upgraded or
re-branded; that queues a re-record for that variant only.

**Not built:** the overlay resolver and any tenant registry. The schema fields
(`app_id`, `variant`) exist and the executor reads the effective artifact, so
the seam is real, but there is one variant in this repo.

---

## 5. Escalation & handoff

### Detecting "stuck"

Five triggers, all explicit rather than heuristic:

1. A `ConditionHandler` with `disposition=ESCALATE` fires.
2. Locator resolution exhausts every fallback for a required step.
3. **No-progress detector** — the a11y snapshot hash is unchanged across N
   consecutive actions. This is the one that catches genuinely novel states.
4. Step budget or wall-clock timeout exceeded (discovery only).
5. **Risk gate** — a step classed `IRREVERSIBLE` under a policy whose
   `max_risk_unattended` is lower. Not a malfunction; a designed stop.

### The control-transfer model

Session ownership is an explicit lease with exactly one owner at a time:

```
AUTOMATION --pause--> PENDING_HANDOFF --accept--> OPERATOR
     ^                                                |
     +---------------- resume --------------------- --+
```

The browser is a long-lived non-headless context. Automation does not close it,
kill it, or open a second one — it **stops issuing actions**. The human operates
the exact same live session, with the exact same cookies, session token, form
state and scroll position. That is the whole trick, and it is why the model is
"who holds the lease" rather than "how do we replicate state into a new session."

The executor refuses to act while the lease is not `AUTOMATION`, so a late or
duplicated action cannot race the human.

### The intervention request

Raised with enough context to act on without reading the code: capability id and
goal, current step id and its `intent`, why it stopped (trigger + detector that
fired), a screenshot, the a11y snapshot, and the parameters in redacted form.

### Hand-back

On resume the executor takes a fresh a11y snapshot, diffs it against the
pre-handoff snapshot, and records the delta as a `human_action_summary` in
evidence — we capture *what changed*, not keystrokes, which keeps PII out of the
log. It then re-evaluates the current step's checkpoint: if it now passes, the
run continues from the next step; if not, it re-runs the current step.

**Mocked deliberately:** the operator console is a bare local HTTP page — pending
requests, context, screenshot, Take Control / Hand Back buttons. Real co-browsing
(WebRTC, input relay, viewport sync) is out of scope per the brief. The lease
state machine, the executor's refusal to act, and the snapshot-diff capture are
all real.

---

## 6. Safety

**Single chokepoint.** Every action from both paths passes `PolicyGate.check()`
before reaching the adapter. There is no second route to the surface. Discovery
and replay are governed identically, so a prompt-injected model has exactly the
authority a reviewed artifact has — none extra.

**Allowlist** (`allowlist_id` on the artifact, config on disk):
- permitted URL patterns — enforced on *browser-initiated* navigation too, via a
  route interceptor, so a redirect or injected link cannot walk us off-allowlist
- permitted action kinds
- maximum risk class permitted unattended

**Risky and irreversible actions.** Every step carries a recorded `RiskClass`, so
posture is reviewable *before* approval rather than computed mid-run. Unattended
replay is capped at `SAFE_REVERSIBLE` by default; anything above escalates for
human confirmation rather than being blocked outright, because blocking outright
would make the system useless for the write flows that are the actual business
value.

**Data handling.**
- Secrets are `{{secret:name}}` references resolved from the environment at
  replay time. They are never written into an artifact, log, or evidence file.
- Fields marked `PII` are masked in logs and evidence; their bounding boxes are
  blurred in screenshots.
- The model transcript is never persisted — only `provenance.transcript_ref`
  pointing at redacted evidence.

**Limits, stated plainly.** The allowlist constrains *where* and *what kind*, not
*semantics*: it cannot tell a $10 transfer from a $10,000 one. Redaction is
pattern- and label-driven, so novel PII formats will leak into evidence until a
rule is added. And screenshots are the weakest link — a masked field is only
masked if we knew to mask it. A production deployment would need semantic
value-level policy on top of this, which we did not build.

---

## 7. Cuts

**Cut, with the seam left clean:**

- **Multi-tenant overlay resolver.** Schema fields exist; one variant in repo.
- **Desktop adapter.** Interface defined, `ControlRef` proven portable, no
  implementation.
- **Real operator console.** Bare HTTP page; lease machine and hand-back are real.
- **Assisted LLM recovery on replay failure.** Listed as a stretch goal, and we
  argue against it: the value of this system is that production execution is
  model-free and auditable. A bounded model call in the failure path
  reintroduces nondeterminism exactly where a bank most needs it absent. The
  right response to a replay failure is escalate-then-re-record.
- **Queues, workers, persistence.** Explicitly penalised by the brief.

**Known weakness we would fix first:** `Detector` does triple duty for
checkpoints, condition triggers and the success condition. It keeps the evaluator
small, but it cannot express "balance is numeric and greater than zero." A
richer predicate type is the first schema change we would make.

**What we would build next, in order:** (1) the overlay resolver plus a second
app variant, to prove cross-tenant reuse rather than assert it; (2) multi-run
stability scoring to make `approval_state` earned rather than declared; (3) the
desktop adapter, because it is the real test of whether the abstraction holds.
