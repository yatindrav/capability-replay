# PLAN — closing the gap between DESIGN.md and the tree

**Written:** 2026-08-27. **Basis:** the §-by-§ audit in this file's companion
sections, run against the actual code, not against `HANDOFF.md`.

`DESIGN.md` is roughly half implemented. §4 (replay) and most of §6 (safety) are
built and verified; §5 (escalation) is built and has never been executed; §1–§3
(intake, discovery, recording) are partial with real deviations; all three §10
joins are open. This is the order to close them in, and why that order.

## The ordering constraint

Two things drive it.

**The real discovery run is non-negotiable and it is downstream of almost
everything.** The brief will not accept a described run, so `evidence/disc_*`
has to exist. But a discovery run today would produce a bad artifact: the agent
has no `select` tool (so it cannot drive the sub-account form), it infers risk
from button labels, and nothing verifies the artifact it emits. Every phase
before Phase 5 exists to make that one run worth its API call.

**Verification replay must precede the discovery run, not follow it.** Join 1 is
what catches a checkpoint synthesized around a volatile value. The mock app's
receipt now carries a posting timestamp precisely so this is testable — build the
detector before you generate the thing it detects, or you will ship a capability
that fails on its second invocation and find out in the demo.

```
Phase 0  tests            ─┐
Phase 1  discovery tools   ├─► Phase 5  real discovery runs ──► Phase 6  escalation demo ──► Phase 7  deliverables
Phase 3  verification     ─┘                                          ▲
Phase 2  joins 2 + 3 ─────────────────────────────────────────────────┘
Phase 4  dialogs ─────────────────────────────────────────────────────┘
```

Phases 0–4 are independent of each other and can be done in any order. 5, 6, 7
are strictly sequential and strictly last.

---

## Phase 0 — Lock in what is currently green (1–2h)

`tests/` does not exist, though `pyproject.toml` already points `testpaths` at
it. Four subtle fixes landed this session and every later phase edits the same
engine; without a harness their regressions are silent.

- Unit: allowlist asymmetry (`url_permitted` vs `auth_permitted`), redaction,
  param templating, `_evaluate` per detector kind, lease transitions.
- Integration against the live mock app: replay green on `12345`; business
  outcome on `99999`; mid-run session drop → restart → correct balance;
  double-post refused with account count unchanged.
- The restart test is the important one. It encodes the rule that a session drop
  must not be answerable with a business outcome, which is the failure mode most
  likely to be reintroduced by someone tidying `_handle_conditions`.

**Done when** `pytest` is green and the restart case fails if you revert
`_handle_conditions` to `return "retry"`.

## Phase 1 — Give discovery the vocabulary and honesty it needs (3–4h)

Blocks Phase 5. Two independent defects, both in [discovery.py](cua/agent/discovery.py).

**Tool vocabulary.** `TOOLS` has `navigate`, `click`, `type_text`, `read_value`
— no `select`, `wait`, or `assert`. The sub-account form has two `<select>`
controls, so discovery cannot complete the write goal at all. Add all three,
wire them through `_execute`, and map them in `build_artifact` to
`SelectAction` / `WaitAction` / `AssertAction` (all three already exist in the
schema and the replay engine already executes them).

**Risk must be declared, not guessed.** §2 argues that requiring `risk` at
decision time is deliberate — it makes the model reason about danger while it
chooses, and puts a reviewable justification in the artifact. The code does the
opposite: `_classify_click()` keyword-matches the button label afterward.
`"Post"` classifies correctly today by luck of vocabulary. Add `risk` as a
required field on every action tool; keep `_classify_click` only as a
cross-check that *logs* disagreement between the model's declaration and the
heuristic, which is a useful drift signal and costs nothing.

Also here, both small: a `DiscoveryRequest` model so params are validated at
intake per §1 (replay validates, discovery does not), and the §2 two-strikes
rule — gate denials are logged at `discovery.py:271` but never counted, so a
model repeatedly reaching for a forbidden action loops instead of escalating.

**Done when** an artifact built from a recording containing `select` and
`assert` replays green, and a run whose model declares `risk: irreversible`
carries that into the artifact.

## Phase 2 — Make the thread walkable (3–4h)

**Join 3 first — it is one line.** `provenance.discovery_run_id` is written by
discovery and `ReplayResult.discovery_run_id` exists in the schema, but
`engine.py` never copies one to the other. Copy it at artifact load. This is
what lets an auditor walk from a production result back to the discovery that
produced the capability, and it is the cheapest requirement in the entire brief.

**Join 2 — escalation is a pause, not a terminus.** `ESCALATED` is returned
terminally at five sites and `ReplayResult.escalations` is never populated.
Correct semantics per §10: a *resolved* escalation returns `SUCCESS` or
`BUSINESS_OUTCOME` with an `EscalationRecord` appended; `ESCALATED` is returned
only when the request times out or is abandoned. `EscalationRecord` already
exists in `schema/result.py`. Touches the five return sites plus the handback
loop in `__main__.py`.

Do these before Phase 6, since the escalation demo is what proves them.

**Done when** a run that pauses for a human and finishes reports `SUCCESS` with
a populated `escalations` list, and `ok_for_caller` is `True`.

## Phase 3 — Verification replay, Join 1 (2–3h)

Blocks Phase 5. The recorder currently finishes at "artifact written." It must
replay the fresh artifact once — same app, same params, no model — before the
file reaches `capabilities/`. An artifact that fails its own verification goes
to `evidence/` with the failure attached and never to `capabilities/`.

- Add `draft_verified` to `ApprovalState`. Artifacts stay immutable; this is a
  value the recorder writes once, not a mutation of a stored file.
- `Stability` gets its first data point for free.
- Move storage to `capabilities/<app_id>/<capability_id>/v<N>.json` per §3. The
  current flat `member.savings_balance.read.v1.json` has no room for two apps or
  two versions. Keep the loader able to find the existing file, or migrate it.
- **The irreversible problem.** Verifying a write capability re-posts the
  transaction. Decision: the recorder's verification path calls `POST /_reset`
  first and runs the gate `attended=True` with a recorder-only auto-confirm.
  Write this down in `REPORT.md` §6 — it is a real hole in an otherwise clean
  story, and a reviewer will find it faster than they will find the mitigation.

**Done when** an artifact whose checkpoint contains the receipt's posting
timestamp is refused by its own verification replay and lands in `evidence/`.

## Phase 4 — Unmodeled blockers (2–3h)

`dialog_present` and `load_failed` are declared in the schema and absent from
both `engine.py`'s `_evaluate` and `surface/web.py`.

- Register a Playwright dialog handler at session open and record what it saw.
  **Not because dialogs hang the run** — measured, they do not. With no handler
  Playwright silently auto-dismisses, so `onclick="return confirm(...)"` returns
  false, the form never submits, and `click()` reports success in 0.0s. The run
  believes it posted a transaction that never happened. The handler is what makes
  the dialog observable at all.
- Implement both detector kinds.
- Add the unmodeled-blocker check before checkpoint evaluation: a `dialog` or
  `alertdialog` role not present in the recorded observation **escalates**. A
  novel modal in a bank app is where a human should look.

**Done when** the `compliance_modal` fault escalates rather than fails, and
`native_confirm` no longer produces a silent no-op.

## Phase 5 — The real discovery runs (2–3h) — non-negotiable

Needs `ANTHROPIC_API_KEY`. Two goals, because one of each kind is the point:

1. **Read** — "look up member `{member_id}` and read their savings balance."
2. **Write** — "open a new sub-account for member `{member_id}` and reach the
   confirmation screen." This one exercises `IRREVERSIBLE` → gate `CONFIRM` →
   escalation, which is the strongest thing this system does.

Hold the demonstration constraint from §10: discovery on member A, replay on
member B, third replay on a nonexistent member for `BUSINESS_OUTCOME`. Replaying
the member the model happened to look up demonstrates nothing about
parameterization.

`POST /_reset` before each write run or balances carry over between them.

**Done when** `evidence/disc_*` exists for both, and each emitted artifact
replays green on a different parameter than it was recorded with.

## Phase 6 — Run the escalation cycle end to end (1–2h)

The lease machine, intervention queue, and operator console all exist and have
never been executed. Run the full cycle against the write capability's gate
escalation: pause → intervention raised with context → operator takes control of
the *same* browser context → acts → hands back → run resumes and completes.

With Phase 2 in place it should return `SUCCESS` carrying an `EscalationRecord`.
Capture the evidence; this is a heavily weighted criterion and currently has
nothing behind it.

## Phase 7 — Deliverables (2–3h)

- **`README.md` does not exist** and is a required deliverable: setup, keys,
  running without live services, and the exact demo commands.
- **Evidence tidy.** `evidence/` holds 14 replay directories from this session's
  debugging. Curate to a discovery run, a green replay, a business-outcome
  replay, an error/escalation replay. Keep `replay_f64a251e93` — the original
  failure is a legitimate error-state exhibit.
- **`DESIGN.md` §7 names twelve files that do not exist.** Most of the
  difference is sensible consolidation (`resolver`+`detectors` → `engine`+`web`,
  `gate`+`allowlist`+`redact` → `policy.py`, `cli.py` → `__main__.py`). Fix the
  document, not the code — but fix it, because a design doc that misdescribes
  its own tree is a communication problem and communication is graded.
- Reconcile §2's `Observation`/`ObservedControl` and §3's `StepDraft` with
  whatever is true after Phase 1, or move them to "cut" honestly.
- Refresh `REQUIREMENTS.md`; complete `REPORT.md` §7 Cuts.

---

## Deliberately not on the critical path

Real value, none of it required for a complete vertical slice. Take these only
if Phases 0–7 are done.

| Item | Why it is worth something | Why it can wait |
|---|---|---|
| `ObservedControl` + `Observation` reshape (§2) | The model would pick from an enumerated control list instead of parsing an aria text blob — better discovery, and it makes §2's "no translation layer" claim true | Invasive; the text blob demonstrably works |
| `StepDraft` + backtrack pruning (§3) | Artifacts free of the model's dead ends | Recorded runs are short enough that dead ends are rare |
| Per-step checkpoint synthesis from deltas (§3) | "Never assume a click worked" currently holds only for the final step | Partially compensated by condition handlers firing per step |
| `field_rules` in the allowlist (§6) | Sensitivity would be inherited rather than hand-authored per artifact | Hand-authored labels are correct today, just not automatic |
| Screenshot blur (§6) | Completes the redaction story for the richest evidence signal | Text redaction already covers logs and snapshots |
| `read_table` action (§9) | `OutputSpec` can declare a repeated record that no action can produce | No capability needs one yet |

## Estimate

Phases 0–7 total roughly 16–22 hours. If time forces a cut, cut *depth inside*
Phase 0 and Phase 4 — they are the two whose absence a reviewer would read as a
gap rather than as a missing feature. Do not cut Phase 6: §3.6 is explicitly
graded as "a real mechanism, not just a TODO," and it is the requirement with
the least evidence behind it right now.
