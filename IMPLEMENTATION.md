# IMPLEMENTATION — what was built, and what running it found

A computer-use system for legacy bank back-office UIs. An LLM discovers how to
reach a goal against a live surface; the successful run becomes a typed,
versioned capability; production invocations replay it with no model in the
decision loop.

| | |
|---|---|
| Python | 4,395 lines across `cua/`, `mockapp/`, `tools/` |
| Tests | 81 passing (units + browser-driven integration) |
| Defects found by execution | 7 |
| Evidence runs | 5 |

**Companion documents.** `README.md` — setup and the demo path. `REPORT.md` —
the seven headings the brief mandates. `DESIGN.md` — the build specification,
annotated where the build diverged from it. `REQUIREMENTS.md` — the traceability
matrix. `PLAN.md` — what remains and in what order. All member data is synthetic.

---

## 1. Architecture — two paths through one machine

Discovery and replay share the executor, the action vocabulary, the observation
format, the policy gate and the evidence writer. **Only the decider differs.**

```
  DISCOVERY · once, supervised          REPLAY · many, unattended
  goal + declared params                capability_id + params
            │                                     │
            ▼                                     ▼
  LLM observe → decide → act            recorded step list
            │                                     │
            └──────────────┬──────────────────────┘
                           ▼
                    ┌─────────────┐   ← no model below this line
                    │ policy gate │
                    └─────────────┘
                           ▼
                ┌───────────────────────┐
                │ executor + adapter    │
                └───────────────────────┘
                           ▼
                   live legacy surface
```

That symmetry is the central bet, and it pays twice. Recording is faithful by
construction, because discovery cannot take an action replay is unable to
express — every tool the model can call maps to exactly one `Action` the engine
already executes. And safety has a single chokepoint, so a prompt-injected model
has exactly the authority a reviewed artifact has and no more.

`cua/replay/` imports no LLM, by design.

### Module layout

```
cua/
  schema/artifact.py    CapabilityArtifact, Step, ControlRef, ConditionHandler
  schema/result.py      ReplayResult, StepRecord, FailureDetail, EscalationRecord
  surface/web.py        Playwright adapter — a11y snapshot, resolution, dialogs
  surface/session.py    session bootstrap; auth is NOT part of an artifact
  agent/discovery.py    the LLM loop, its tool vocabulary, distillation
  agent/recorder.py     verification replay — an artifact must replay to be stored
  replay/engine.py      step execution, detectors, conditions, escalation
  safety/policy.py      allowlist, risk gate, redaction
  escalation/lease.py   who holds the live session
  evidence.py           per-run structured log, screenshots, snapshots
  __main__.py           discover | replay | catalog | operator
mockapp/app.py          the legacy-style target, with injectable faults
capabilities/<app_id>/<capability_id>/v<N>.json
evidence/<run_id>/
```

This is flatter than `DESIGN.md` §7 originally sketched. Four consolidations are
argued there; the short version is that several planned modules turned out to be
one function each, and splitting a file per noun makes a reviewer open six files
to follow one decision.

---

## 2. The artifact is a capability, not a macro

A calling agent must know what a capability needs and returns before it invokes;
a human reviewer must be able to audit it without the model transcript. Both read
the same file.

The binding constraint: **nothing may be web-specific at the level that matters.**
Controls are described semantically — `role` + accessible name + anchor — because
that addressing scheme exists in browser accessibility trees, Windows UIA, macOS
AX and AT-SPI alike. CSS and XPath appear only as demoted, adapter-private
fallback hints. That is the seam that lets one schema target a modern web app, a
frameset-era legacy app, or a desktop app.

| Field | Carries | Why it is in the artifact |
|---|---|---|
| `ControlRef` | role, name, frame path, near-text anchor, ranked fallbacks | Role + name is the only *required* part — the one scheme that survives a port to another OS accessibility API |
| `robustness_note` | why this targeting should still work next month | Demanded of the model at decision time, not derived afterward. A reviewer needs something to disagree with |
| `RiskClass` | `safe_reversible` · `risky` · `irreversible` | Recorded per step so the risk posture is reviewable *before* approval, rather than computed mid-run |
| `ConditionHandler` | detector → disposition → outcome code → recovery | Declarative, not code. Replay never improvises, and a reviewer can audit the whole failure surface by reading the file |
| `OutputSpec` | name, cardinality, type, fields | "Their shape", not just their names — a caller needs to know whether it gets a scalar or a repeated record before invoking |
| `Checkpoint` | detectors + require + timeout | Never assume a click worked |

**One rule the schema enforces against itself:** `ParamSpec.example` is
author-supplied only. The natural implementation populates it from the discovery
run's parameter values — which are live member identifiers, written into an
artifact that gets committed and shared across tenants.

Artifacts are immutable. Re-recording writes `v<N+1>` beside its predecessor;
nothing mutates in place, so an audit can reconstruct exactly which version
produced a given production result.

---

## 3. Determinism and the error taxonomy

Because these UIs are stable, the interesting failures are not layout drift —
they are runtime conditions: a validation error, a record not found, a permission
denial, an unexpected dialog, a session timeout, a slow or failed load. The
result contract has to separate an answer the caller asked for from a crash.

| Status | Meaning |
|---|---|
| `SUCCESS` | The goal state verified and the declared outputs are returned |
| `BUSINESS_OUTCOME` | "No such member" is information the caller asked for. Returns normally with an outcome code — **not** a failure, and conflating the two is the mistake the brief names |
| `ESCALATED` | A human was needed and nobody answered. Returned *only* when an escalation goes unresolved |
| `FAILED` | Stop and surface a debuggable error: which step, what was expected, what was observed, plus a screenshot and snapshot |

### How determinism is actually achieved

- **Resolution is ranked, and ambiguity is failure.** Role + name first, then
  each declared fallback in order. A strategy matching more than one control
  fails rather than taking the first — two controls answering to the same
  identity means we cannot tell which was recorded.
- **There are no sleeps.** Every wait polls a detector to a deadline at 250 ms.
  The one place that did a single-shot lookup was a bug, and it is fixed.
- **Condition order is load-bearing.** Global handlers run before step handlers,
  which run before the checkpoint. A session timeout invalidates any conclusion
  you would otherwise draw from a step-level check, so it has to be tested first.
- **Drift is measured, not guessed.** Every step records `resolved_by` and
  `fallback_depth`; any depth above zero emits a drift signal, which is the
  per-tenant re-record trigger.

---

## 4. Safety — one chokepoint, and an asymmetric allowlist

Every action from both paths passes `PolicyGate.check()` before an adapter sees
it. The allowlist is route-level rather than host-level, because "any page on
this host" is almost never the intent — and the fault-injection hooks are
deliberately absent from it, so the automation cannot reach its own test
controls.

The allowlist carries two separate route lists, and the split is the point:

```yaml
# config/allowlist.yaml
url_patterns:                        # what the AGENT may target
  - "http://127.0.0.1:8099/servicing/*"
auth_url_patterns:                   # what the PLATFORM may drive the browser to
  - "http://127.0.0.1:8099/login"
```

`guard_navigation` permits either list; `check()` consults only the first. So
session bootstrap can sign in, and no recorded `navigate` action can ever aim the
agent at the credential form. Merging the two lists would pass every other test
and quietly hand the agent a route to the login page.

### Risky actions escalate rather than block

Anything above the allowlist's unattended ceiling returns `REQUIRE_CONFIRMATION`,
which raises an intervention. Blocking outright would make the system useless for
exactly the write flows that carry the business value — and a capability nobody
can invoke is not safer, it is just unused.

### Data handling

- Secrets are `{{secret:NAME}}` references resolved from the environment at use
  time. They never reach an artifact, a log, or an evidence file.
- PII is masked by **label**, not only by pattern. A member number matches no
  regex; the `Sensitivity.PII` declaration on its `ParamSpec` is what protects it.
- Redaction runs on the way to the model as well as on the way to disk. The model
  does not need a member's SSN to decide where to click.

---

## 5. Escalation is a pause, not a terminus

The browser context is never closed, recreated, or replicated. Pausing means the
executor stops issuing actions into a context that stays exactly as it was — same
cookies, same auth token, same form state, same scroll position. "Transfer the
live session" reduces to "change who holds the lease."

`SessionLease.assert_automation()` runs before every action, so a late or
duplicated action cannot race a human who has taken control.

A *resolved* escalation does not change the run's status. The run resumes and
returns `SUCCESS` or `BUSINESS_OUTCOME` like any other, carrying an
`EscalationRecord` so the result still tells the truth about how the answer was
obtained. A caller that paused for a human and finished has succeeded — it got
its answer, and `ok_for_caller` is `True`.

What the human did is captured as a **snapshot diff**, not as keystrokes. That
answers "what changed?" while keeping PII out of the evidence log.

**One escalation stays terminal on purpose.** If a session drops *after* a
non-reversible step, resuming means replaying a flow that already committed a
write. Nothing outside the app can tell whether that write landed, and no amount
of looking at the screen makes it safe. The operator's job there is to reconcile
the account, and this run's honest answer is `ESCALATED`.

---

## 6. What running it found

Seven defects surfaced by execution rather than by reading. They fall into three
families, and the largest is the one a bank should fear most.

### Family 1 — the run reports success and nothing happened

**Re-authentication retried the step, not the flow.** A session drop mid-run
fired the `session_expired` handler, which re-authenticated and retried the
failed step. But re-auth resets the app to its entry state, so the search box
came back empty, the search submitted blank, and the run returned a confident
`MEMBER_NOT_FOUND` **for a member that exists**. A false business outcome is the
worst thing this system can emit — it is indistinguishable, to the caller, from a
real answer.
*Fix:* re-auth restarts the flow, and refuses to when a non-reversible step has
already run, escalating instead.

**A recovered condition skipped the step it recovered.** On a locate failure the
engine asked whether a known condition explained it. If one did and was recovered
in-band, it returned `None` — which the main loop reads as "step complete, move
on." A dismissed interstitial therefore caused the step to be skipped rather than
retried, and the flow clicked Search on a form it never filled. Same shape, same
consequence.
*Fix:* retry the step, bounded at three attempts.

**A native `confirm()` was silently auto-dismissed.** The design assumed native
dialogs would *hang* the run. Measured, they do not — which is worse. With no
handler registered, Playwright auto-dismisses, so `onclick="return confirm(...)"`
returns false, the form never submits, and `click()` reports success in 0.0
seconds. The run believes it posted a transaction that never happened, and no
checkpoint on the click can catch it.
*Fix:* register a handler at session open so the dialog is recorded and
observable. Dismissal stays the default — accepting an unmodeled dialog answers
"yes" to a question nobody read — but a silent no-op becomes a visible one.

### Family 2 — the guardrail did not guard what it claimed

**The navigation guard aborted the platform's own sign-in.** `/login` was not on
the allowlist, so the route interceptor aborted the bootstrap POST. The frameset
never rendered, and the recorded failure — `frame 'detailFrame' not found` — was
a symptom three layers downstream of the cause. The artifact looked broken; the
guard was eating its own session.
*Fix:* `auth_url_patterns`, honoured by the navigation guard and deliberately not
by the gate.

**The single chokepoint was not gating the LLM.** Discovery passed a hardcoded
`SAFE_REVERSIBLE` to `gate.check()` on every action. The architecture's central
safety claim — one gate, both paths, no richer powers for the model — was **false
in the discovery path**: an irreversible click sailed through unattended. Risk
was also being inferred after the fact by keyword-matching the button's label;
"Post" classified correctly by luck of vocabulary, "Continue" would not have.
*Fix:* risk is a required field on every state-changing tool, declared by the
model as it chooses and passed straight to the gate. The old heuristic survives
only to log disagreement, which is a genuine drift signal.

### Family 3 — correct, but not as written

**`_text_anchor` ignored the role it documented.** "The textbox after *Member
Number*" also matched the Search button, because the implementation filtered on
position and never on role. Later, on the sub-account form, the same strategy
matched both comboboxes — the "Fund From" select follows the "Account Type" label
as surely as its own does.
*Fix:* role filters position, and a positional strategy resolves to the
*nearest* match. Ambiguity-is-failure is right for identity strategies, where two
matches mean we cannot tell which was recorded, but every later control on a form
follows a given label.

**A frameset was asked for a `<body>` it cannot have.** `contains_text` ran once
per global condition per step, and each call spent the full locator timeout
waiting on an element the document type forbids.
*Fix:* skip frameset documents — their text lives in the child frames the same
loop already visits. **52,434 ms → 269 ms** on a four-step replay.

---

## 7. An artifact is finished when it replays, not when it is written

Checkpoint synthesis derives assertions from what changed on screen. A screen
that changed by showing a posting timestamp yields a checkpoint that passes once
and fails forever after — discovered in production, by a caller.

So the recorder replays the fresh artifact once — same app, same parameters, no
model — before it reaches `capabilities/`. One that fails goes to `evidence/`
with the failure attached, and the report names the offending string.
`ApprovalState` gains `draft_verified`, which sits below `approved` deliberately:
it is a machine-checkable claim that this thing runs, where approval remains a
human judgement about whether it should exist.

The mock app's receipt carries a posting timestamp specifically so this is
testable. It is.

**A hole worth naming.** Verifying a capability whose last step is irreversible
posts a second transaction. The recorder takes a reset hook and the CLI wires
`--reset-url` to it, which works against a mock. Against a real core banking
system, verification of an irreversible capability needs a sandbox tenant — a
deployment decision the recorder cannot paper over, so it says so instead.

---

## 8. Evidence

Each run writes `run.jsonl` — a structured log where `intent` rides along on
every step, so it answers "why", not only "what" — plus `result.json`, and on
failure a screenshot and accessibility snapshot.

| Run | Status | What it demonstrates |
|---|---|---|
| `rep_read_member_b` | `SUCCESS` | Recorded on member 12345, replayed on 23456 → `231.09`. The artifact is parameterised, not a hardcoded flow |
| `rep_read_not_found` | `BUSINESS_OUTCOME` | `MEMBER_NOT_FOUND` with no `FailureDetail`. A legitimate answer, returned normally |
| `rep_write_escalated` | `ESCALATED` | An irreversible post with nobody to answer. All seven steps ran; balances untouched |
| `rep_write_resolved` | `SUCCESS` | The same step authorised by a human in the same live session. Checking 88.42 → 38.42, S0103 opens at 50.00, and the result carries the `EscalationRecord` |
| `rep_locator_failure` | `ESCALATED` | The first real failure this system hit, kept as an exhibit: the locator was exhausted and it refused to guess |

### The audit chain

"Evidence for both runs" is not two unrelated directories. An auditor asks: *this
production result — where did the capability that produced it come from?*

```
evidence/rep_write_resolved/result.json
  ├─ capability_id      member.subaccount.open  v1
  ├─ discovery_run_id   disc_seed_write   ─────────┐
  └─ escalations[0]     resolved, risk_gate, s7    │
                                                   ▼
capabilities/acme-servicing/member.subaccount.open/v1.json
  └─ provenance.discovery_run_id ──▶ evidence/disc_seed_write/
```

---

## 9. What is deliberately not built

A thin-but-real version of every requirement beats a polished subset, so the cuts
are depth, not capabilities. Each is documented where a reviewer will look for it.

| Cut | Reasoning |
|---|---|
| **Assisted LLM recovery on replay failure** (an explicit stretch goal, declined) | It reintroduces nondeterminism exactly where a bank needs it absent. A capability that sometimes improvises is one nobody can review, and the failure it papers over is the signal that the artifact needs re-recording |
| **Enumerated `ObservedControl` list** | The model reads a flattened accessibility tree as text and names role plus accessible name in its tool call. Nothing is inferred, but a mis-parse is possible where an enumerated list would make it impossible. The highest-value item remaining |
| **Backtrack pruning at distillation** | Recording is genuinely decoupled from the transcript — capture happens at execution time — but the record omits before/after hashes, so dead ends cannot be pruned. Flows this short rarely backtrack; that is luck holding, not a mechanism |
| **Allowlist `field_rules`** | Sensitivity is author-supplied per artifact rather than inherited from the allowlist. Consequence is narrow but real: a PII field nobody declared, whose value matches no pattern, is redacted nowhere |
| **Screenshot blur, `read_table`, multi-tenant overlays** | Text redaction already covers logs and snapshots; no capability yet needs a repeated record; the overlay resolution is designed and argued but not built, per the brief's instruction not to build scaling infrastructure |

**Still open:** the genuine LLM discovery run, which the brief marks
non-negotiable. Everything it depends on is built — the agent has the full action
vocabulary, declares risk at decision time, and the recorder verifies before
storing. It needs an API key and one command.

---

## 10. Reproducing the thread

```bash
pip install -e ".[dev]" && playwright install chromium
export SVC_OPERATOR_ID=demo SVC_PASSWORD=demo

python mockapp/app.py          # terminal 1 — the legacy stand-in
bash tools/demo.sh             # terminal 2 — replay, outcome, escalation, handback

WITH_DISCOVERY=1 bash tools/demo.sh    # adds the real LLM run
pytest                                 # 81 tests
pytest -m "not integration"            # units only, no browser
```

The mock app is a deliberately hostile stand-in: framesets, table-based layout,
non-semantic markup, no test IDs, inline handlers. Runtime conditions are
injectable rather than waited for — `session_timeout`, `interstitial`,
`app_error`, `native_confirm`, `compliance_modal`, `post_error` — and both test
hooks are absent from the allowlist, so the automation cannot reach its own
controls.

Integration tests run the app in-process on a thread, which is what lets a test
arm a fault *between* two steps. That is the only way to reproduce a session that
dies mid-flow, and it is the test worth protecting: revert the restart semantics
and it fails with `BUSINESS_OUTCOME`.
