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
artifact, one replay evidence run). **That code predates three design decisions.**
It all imports and validates, but the following are NOT implemented:

### Open gaps, in build order

1. **Mock app has no write flow.** `mockapp/app.py` is read-only (search →
   detail → balance). There is no irreversible action, so `RiskClass.IRREVERSIBLE`,
   the policy gate's `CONFIRM` path, and the strongest escalation story have
   nothing to fire against. Requirement 3.4 is untestable until this exists.
   Build: sub-account creation → validation → confirmation screen.

2. **Join 2 — escalation must be a pause, not a terminus.** `replay/engine.py`
   returns `ReplayStatus.ESCALATED` terminally at four sites. Correct semantics:
   a *resolved* escalation returns `SUCCESS`/`BUSINESS_OUTCOME` with an
   `EscalationRecord` appended to `ReplayResult.escalations`. `ESCALATED` is
   returned **only** when the request times out or is abandoned. See `DESIGN.md`
   §10. The schema side (`EscalationRecord`) is already in `schema/result.py`.

3. **Join 1 — verification replay after recording.** The recorder must replay a
   freshly distilled artifact once (no model, same params) before writing it to
   `capabilities/`. An artifact that fails its own verification goes to
   `evidence/` with the failure detail attached, never to `capabilities/`. This
   is what catches volatile checkpoints (a synthesized checkpoint containing a
   timestamp fails on the very next run).

4. **Join 3 — audit chain.** `discovery.py` writes `provenance.discovery_run_id`,
   but `engine.py` never copies it onto `ReplayResult.discovery_run_id`. One
   line, but it's what makes evidence walkable from a production result back to
   the discovery that produced the capability.

5. **Detectors `dialog_present` and `load_failed` are declared but unimplemented.**
   In the schema, absent from `engine.py` and `surface/web.py`. Also needed: the
   adapter must register a Playwright dialog handler at session open, or a native
   `confirm()` hangs the run outright.

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
- Run the mock app: `python mockapp/app.py` → http://localhost:8099/servicing/
- Arm a fault: `curl -X POST localhost:8099/_fault -d name=session_timeout`

## Still unresolved (decide with evidence, not on paper)

- No-progress threshold (3 unchanged snapshot hashes) may fight the checkpoint
  timeout on a slow legacy page. Tune against the real mock app.
- Checkpoint synthesis may produce volatile checkpoints (timestamps, session
  ids). Needs a volatility filter — gap 3 above makes this self-detecting.
- `OutputSpec` can declare a repeated record, but `ReadAction` extracts a single
  value. A `read_table` action is needed to actually produce one. Deferred until
  we see what a legacy table's a11y tree really looks like.
