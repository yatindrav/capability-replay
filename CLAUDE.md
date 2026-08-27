# CLAUDE.md

Project context for Claude Code. Read this before touching anything.

## What this is

A take-home for interface.ai: a record-once / replay-many computer-use system
for driving legacy bank back-office UIs. An LLM discovers how to accomplish a
goal against a live surface; the successful run becomes a typed, versioned
**capability artifact**; production invocations replay that artifact with no
model in the decision loop.

The through-line, which every decision is checked against:

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the AI agent invokes it in production.

**The test:** does this keep the model out of the production execution path? If a
mechanism is convenient during discovery but would need a model call at replay
time, it does not go in the artifact.

## Read these first, in order

| File | What it is |
|---|---|
| `DESIGN.md` | Build spec. Six-stage pipeline, component interfaces, §10 end-to-end thread. **Source of truth for _how_.** |
| `REQUIREMENTS.md` | Traceability matrix — every requirement bullet → component, with status. Update it when you build something. |
| `REPORT.md` | The submitted write-up (their seven mandated headings). Source of truth for _why_. |

## Current state — READ THIS

A parallel session built most of milestones 2–8 (`surface/`, `safety/`,
`escalation/`, `agent/discovery.py`, `replay/engine.py`, `__main__.py`, a seeded
artifact, one replay evidence run). **That code predates three design decisions**
(the "joins" in `DESIGN.md` §10), which are still open below.

The read capability now replays green end to end and the write flow exists. What
follows separates what has been *run* from what has only been *written* — assume
nothing in the second list works until you have seen it work.

### Closed, with evidence

**Gap 0 — the seeded artifact now replays green.** It never had, and the cause
was not the artifact. Four defects, each found by running it:

- The navigation guard aborted the platform's *own* sign-in POST, because
  `/login` was not on the allowlist. The recorded `frame 'detailFrame' not
  found` was a symptom. Fixed by `auth_url_patterns`, honoured by
  `guard_navigation` and deliberately **not** by `PolicyGate.check()` — the
  platform may drive the browser to the auth route, a recorded `navigate` may
  not. Do not merge those two lists.
- `_text_anchor` ignored `ref.role`, so "the textbox after Member Number" also
  matched the Search button. Role now filters position.
- `contains_text` asked a frameset document for a `<body>` it cannot have,
  spending the full locator timeout once per global condition per step:
  52,434ms → 269ms.
- **A `reauthenticate` recovery retried only the failed step.** Re-auth resets
  the app to its entry state, so the search box came back empty and the run
  returned a confident `MEMBER_NOT_FOUND` for a member that exists. A false
  business outcome is the worst thing this system can emit. Reauth now restarts
  the flow, and *refuses* to when a non-reversible step has already run —
  escalating instead, because nothing outside the app can tell whether that
  write landed.

**Gap 1 — the write flow exists.** `mockapp/app.py` now has
`Open Sub-Account → validation → Review and Post → post`, funded from an
existing account. Posting debits the source and credits a new account, and a
one-time token makes a second post a no-op. Two business outcomes
(`DEPOSIT_BELOW_MINIMUM`, `INSUFFICIENT_FUNDS`) and two validation errors.
`POST /_reset` restores balances — the write flow mutates state, so evidence
runs are not reproducible without it. Three new faults: `native_confirm`,
`compliance_modal`, `post_error`.

### Open gaps, in build order

1. **Join 2 — escalation must be a pause, not a terminus.** `replay/engine.py`
   returns `ReplayStatus.ESCALATED` terminally at four sites. Correct semantics:
   a *resolved* escalation returns `SUCCESS`/`BUSINESS_OUTCOME` with an
   `EscalationRecord` appended to `ReplayResult.escalations`. `ESCALATED` is
   returned **only** when the request times out or is abandoned. See `DESIGN.md`
   §10. The schema side (`EscalationRecord`) is already in `schema/result.py`.

2. **Join 1 — verification replay after recording.** The recorder must replay a
   freshly distilled artifact once (no model, same params) before writing it to
   `capabilities/`. An artifact that fails its own verification goes to
   `evidence/` with the failure detail attached, never to `capabilities/`. This
   is what catches volatile checkpoints (a synthesized checkpoint containing a
   timestamp fails on the very next run).

3. **Join 3 — audit chain.** `discovery.py` writes `provenance.discovery_run_id`,
   but `engine.py` never copies it onto `ReplayResult.discovery_run_id`. One
   line, but it's what makes evidence walkable from a production result back to
   the discovery that produced the capability.

4. **Detectors `dialog_present` and `load_failed` are declared but unimplemented.**
   In the schema, absent from `engine.py` and `surface/web.py`. Also needed: the
   adapter must register a Playwright dialog handler at session open — but **not**
   for the reason previously written here. Measured against the mock app's
   `native_confirm` fault: with no handler Playwright silently auto-*dismisses*
   the dialog, so `onclick="return confirm(...)"` returns false, the form never
   submits, and `click()` returns in 0.0s reporting success. It does not hang; it
   posts nothing and says it worked. Registering the handler is what makes the
   dialog observable at all, and is the only way `dialog_present` can ever fire.

   Related design rule: an **unmodeled** blocking dialog (role `dialog`/
   `alertdialog` not present in the recorded observation) must **escalate**, not
   fail. A novel modal in a bank app is exactly where a human should look.

## Non-negotiables

- **The discovery run must be real.** At least one genuine LLM-driven run against
  the live mock app, evidence in `/evidence/`. Cannot be stubbed or simulated.
- **The demo replay must use a different parameter value than discovery did.**
  Replaying with the same member is indistinguishable from a hardcoded flow and
  demonstrates nothing about parameterization. Demo = discovery on member A,
  replay on member B, third run on a nonexistent member for `BUSINESS_OUTCOME`.
- **No secrets in the repo.** `ANTHROPIC_API_KEY` from env only.
- **Licensing: MIT / BSD / Apache-2.0 / PSF only.** No GPL, LGPL, MPL, EPL,
  CDDL, AGPL — including transitively. The current tree was audited clean. Check
  before adding any dependency.
- **`ParamSpec.example` must never be auto-populated from a discovery run's
  parameter values.** Those are live member identifiers. Author-supplied only.

## Design decisions not to relitigate

- **Perception is the accessibility tree, not the DOM.** `role` + accessible name
  is the only addressing scheme shared by browsers, Windows UIA, macOS AX and
  AT-SPI, so it is the only format that ports to desktop. CSS/XPath exist only as
  demoted, adapter-private fallback hints.
- **Discovery and replay share one executor, one action vocabulary, one policy
  gate.** The LLM gets no richer powers than replay has. This makes recording
  faithful by construction and gives safety a single chokepoint.
- **`ConditionHandler` is declarative, not code.** Replay never improvises. A
  reviewer can audit a capability's entire failure surface by reading the artifact.
- **Recording is incremental (`StepDraft`), not transcript parsing.** The
  transcript is evidence; the drafts are the record.
- **Parameters are declared at intake, not inferred afterward.** Goals carry
  `{placeholders}`; templating is exact substitution, not inference.
- **We deliberately reject "assisted LLM recovery on replay failure"** (one of
  their stretch goals). It reintroduces nondeterminism exactly where a bank needs
  it absent. Argued in `REPORT.md` §7.
- **No queues, no DB, no services.** Single process, JSON on disk. The brief
  explicitly penalizes building scaling infrastructure.

## Conventions

- Python 3.11, Pydantic v2, Playwright, `argparse` for CLI. Types on public
  functions.
- Comments explain *why*, not *what*. The reviewer is grading judgment.
- Artifacts are **immutable**. Re-recording writes `v<N+1>`; nothing mutates in
  place.
- Setup (Python 3.12 is what the venv uses; 3.10 on PATH is too old):
  `py -3.12 -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"`
  then `.venv/Scripts/python.exe -m playwright install chromium`.
- Run the mock app: `python mockapp/app.py` → http://localhost:8099/servicing/
- Replay: `SVC_OPERATOR_ID=demo SVC_PASSWORD=demo python -m cua replay
  --capability capabilities/<file>.json --param member_id=12345 --allow-draft`.
  Credentials come from the environment only, and the mock app accepts any pair.
  `--allow-draft` is needed while artifacts are `draft`.
- Arm a fault: `curl -X POST localhost:8099/_fault -d name=session_timeout`
- Reset written state: `curl -X POST localhost:8099/_reset` — do this before any
  evidence run that exercises the write flow, or balances carry over.

## Still unresolved (decide with evidence, not on paper)

- No-progress threshold (3 unchanged snapshot hashes) may fight the checkpoint
  timeout on a slow legacy page. Tune against the real mock app.
- Checkpoint synthesis may produce volatile checkpoints (timestamps, session
  ids). Needs a volatility filter — gap 3 above makes this self-detecting.
- `OutputSpec` can declare a repeated record, but `ReadAction` extracts a single
  value. A `read_table` action is needed to actually produce one. Deferred until
  we see what a legacy table's a11y tree really looks like.
