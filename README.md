# Computer-use capability system

Record-once / replay-many automation for legacy bank back-office UIs.

An LLM discovers how to accomplish a goal against a live surface. The successful
run becomes a typed, versioned **capability artifact**. Production invocations
replay that artifact with no model in the decision loop.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the AI agent invokes it in production.

Every design decision is checked against one question: **does this keep the model
out of the production execution path?** `REPORT.md` is the write-up; `DESIGN.md`
is the build spec.

---

## Setup

Python 3.11+ (3.12 used here), and a Chromium that Playwright manages.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
# python3 -m venv .venv && source .venv/bin/activate  # macOS / Linux

pip install -e ".[dev]"
playwright install chromium
```

### Configuration

| Variable | Needed for | Notes |
|---|---|---|
| `SVC_OPERATOR_ID`, `SVC_PASSWORD` | every run | Credentials for the mock app, which accepts any pair. **Never** stored in an artifact, a log, or this repo — session bootstrap reads them from the environment at use time. |
| `ANTHROPIC_API_KEY` | discovery only | Replay, escalation and the tests need no key. |
| `CUA_MODEL` | optional | Defaults to `claude-sonnet-5`. |

### Run the target app

```bash
python mockapp/app.py     # http://localhost:8099/servicing/
```

A deliberately hostile stand-in for a legacy servicing system: framesets,
table-based layout, non-semantic markup, no test IDs, inline handlers. All data
is synthetic. Two flows — a read (search → member detail → balance) and a write
(open a funded sub-account → validation → review → post).

Runtime conditions are injectable rather than waited for:

```bash
curl -X POST localhost:8099/_fault -d name=session_timeout
#   not_found | validation | session_timeout | interstitial | slow | app_error
#   native_confirm | compliance_modal | post_error
curl -X POST localhost:8099/_fault -d name=none     # disarm
curl -X POST localhost:8099/_reset                  # restore balances
```

`/_reset` matters because the write flow moves money: without it a second run
starts from the first run's balances. Both hooks are deliberately absent from the
allowlist, so the automation cannot reach its own test controls.

---

## Demo path

The whole thread, end to end:

```bash
export SVC_OPERATOR_ID=demo SVC_PASSWORD=demo
python mockapp/app.py &          # terminal 1
bash tools/demo.sh               # terminal 2
```

Add a real discovery run (needs `ANTHROPIC_API_KEY`):

```bash
WITH_DISCOVERY=1 bash tools/demo.sh
```

### Or step by step

**1. Discovery** — an LLM drives the live app until the goal is met, and the run
becomes an artifact. The artifact is replayed once before it is stored; one that
fails its own verification goes to `evidence/` and never to `capabilities/`.

```bash
python -m cua discover \
  --goal "look up member {member_id} and read their current savings balance" \
  --param member_id=12345 \
  --entry http://127.0.0.1:8099/servicing/ \
  --capability-id member.savings_balance.read \
  --allowlist acme-servicing-readonly
```

Without a key, `python tools/seed_artifact.py` produces the same two artifacts by
feeding recorded-shape transcripts through the same `build_artifact()` the agent
uses. Their provenance says `(seeded)`; they are not passed off as discovered.

**2. Replay** — the production path. No LLM is imported by `cua/replay/`.

```bash
python -m cua replay \
  --capability capabilities/acme-servicing/member.savings_balance.read/v1.json \
  --param member_id=23456 --allow-draft
```

Note the **different member** from the one discovery recorded. Replaying the
member the model happened to look up demonstrates nothing about parameterisation
— it is indistinguishable from a hardcoded flow.

**3. A capability that stops for a human** — the sub-account write ends in an
irreversible post, which exceeds the unattended risk ceiling:

```bash
# unattended: the gate stops it, nothing is posted
python -m cua replay \
  --capability capabilities/acme-servicing/member.subaccount.open/v1.json \
  --param member_id=23456 --param opening_deposit=50.00 --allow-draft

# attended: a human authorises it in the same live session, the run completes
python -m cua replay \
  --capability capabilities/acme-servicing/member.subaccount.open/v1.json \
  --param member_id=23456 --param opening_deposit=50.00 \
  --allow-draft --attended --auto-handback 1
```

**4. The capability catalog** — artifacts rendered as function-calling tool
definitions, which is how a calling agent would discover them:

```bash
python -m cua catalog
python -m cua operator      # bare operator console, http://127.0.0.1:8100
```

---

## What the demo produces

| Evidence | Status | What it shows |
|---|---|---|
| `evidence/rep_read_member_b/` | `SUCCESS` | Recorded on member 12345, replayed on 23456 — the artifact is parameterised, not hardcoded |
| `evidence/rep_read_not_found/` | `BUSINESS_OUTCOME` | `MEMBER_NOT_FOUND`. A legitimate answer, not a crash |
| `evidence/rep_write_escalated/` | `ESCALATED` | An irreversible step with nobody to answer. All seven steps ran; nothing was posted |
| `evidence/rep_write_resolved/` | `SUCCESS` | The same step, authorised by a human in the same live session. Carries an `EscalationRecord` |
| `evidence/_archive/rep_locator_failure_original/` | `ESCALATED` | The first real failure this system hit: the locator was exhausted, and it refused to guess |

Each directory holds `run.jsonl` (a structured log of what happened and *why* —
`intent` rides along on every step), `result.json`, and, on failure, a screenshot
and an accessibility snapshot.

The audit chain is walkable from either end: `result.json` names its
`capability_id` and `discovery_run_id`, so a production result leads back to the
discovery run that produced the capability.

---

## Tests

```bash
pytest                       # 81 tests
pytest -m "not integration"  # units only, no browser
```

Integration tests drive a real Chromium against the mock app, running in-process
so a test can arm a fault *between* two steps — the only way to reproduce a
session that dies mid-flow.

---

## Layout

```
cua/
  schema/artifact.py    CapabilityArtifact, Step, ControlRef, ConditionHandler
  schema/result.py      ReplayResult, StepRecord, FailureDetail, EscalationRecord
  surface/web.py        Playwright adapter — a11y snapshot, resolution, dialogs
  surface/session.py    session bootstrap; auth is NOT in the artifact
  agent/discovery.py    the LLM loop, its tool vocabulary, distillation
  agent/recorder.py     verification replay — an artifact must replay to be stored
  replay/engine.py      step execution, detectors, conditions, escalation
  safety/policy.py      allowlist, risk gate, redaction
  escalation/lease.py   who holds the live session
  evidence.py           per-run structured log, screenshots, snapshots
  __main__.py           discover | replay | catalog | operator
mockapp/                the legacy-style target, with injectable faults
capabilities/<app>/<capability>/v<N>.json
evidence/<run_id>/
```

## Safety

- **One chokepoint.** Every action from both discovery and replay passes
  `PolicyGate.check()` before an adapter sees it. There is no second route to the
  surface, so a prompt-injected model has exactly the authority a reviewed
  artifact has and no more.
- **The allowlist is route-level, and asymmetric.** `url_patterns` is what the
  agent may target; `auth_url_patterns` is what the *platform* may drive the
  browser to during session bootstrap. The sign-in route is on the second list
  and not the first, so login works and no recorded `navigate` can aim at the
  credential form.
- **Risk is declared at discovery, not inferred later.** The model classifies
  every state-changing action as it chooses it. Anything above the allowlist's
  unattended ceiling escalates for a human rather than being blocked outright —
  blocking would make the system useless for exactly the write flows that carry
  the business value.
- **Secrets are references.** `{{secret:NAME}}` resolves from the environment at
  use time and never reaches an artifact, a log, or an evidence file.
- **PII is masked by label, not just by pattern.** A member number matches no
  regex; `Sensitivity.PII` on its `ParamSpec` is what protects it.
  `ParamSpec.example` is author-supplied only — populating it from a discovery
  run would write live member identifiers into a shared artifact.

Limits are in `REPORT.md` §6.
