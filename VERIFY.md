# VERIFY — check the claims yourself

This project claims that an AI figures out how to use a banking screen once, and
that afterwards the same job runs **without any AI involved**. Those are strong
claims. This page lets you check them yourself.

**You do not need to understand the code.** Every step below is copy-and-paste,
and each one says what you should see and what it proves.

**Time:** about 15 minutes. **Cost:** nothing, except the optional Step 8.

---

## What you need

- A computer with **Python 3.11 or newer** (`python3 --version` to check)
- An internet connection for the first install
- A terminal (Terminal on macOS/Linux, **Git Bash** on Windows — not PowerShell)

You do **not** need an Anthropic API key for Steps 1–7. Only Step 8 uses one.

---

## Step 1 — Get the code and install it

```bash
git clone https://github.com/yatindrav/capability-replay.git
cd capability-replay
```

**macOS / Linux:**
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
```

**Windows (Git Bash):**
```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m playwright install chromium
```

> From here on, `PY` means your Python. Set it once so the rest of this page
> works on either platform:
>
> ```bash
> export PY=.venv/bin/python              # macOS / Linux
> export PY=.venv/Scripts/python.exe      # Windows
> ```

**You should see:** installs finish without an error. The last one downloads a
browser (~180 MB) and prints where it saved it.

---

## Step 2 — Start the pretend bank, and look at it

This project drives a deliberately old-fashioned banking screen. It is a real
web app, not a pretend one inside the code — you can open it and click it
yourself.

In **terminal 1**:

```bash
$PY mockapp/app.py
```

**You should see:** `Running on http://127.0.0.1:8099`. Leave this running.

Now open **http://127.0.0.1:8099/servicing/** in your browser. Sign in with any
username and password (it accepts anything). Search for member **12345**.

**What this proves:** the automation later in this page is driving this same
screen. Nothing is faked or stubbed out.

---

## Step 3 — Run the automated tests

Open **terminal 2** (leave terminal 1 running) and set `PY` again there:

```bash
$PY -m pytest
```

**You should see:** `98 passed` at the end. It takes about two minutes because
some tests drive a real browser.

**What this proves:** the individual pieces do what they say. Note this is the
weaker check — the interesting one is next.

---

## Step 4 — Run the whole thing end to end

Still in terminal 2:

```bash
export SVC_OPERATOR_ID=demo SVC_PASSWORD=demo
bash tools/demo.sh
```

This runs five scenarios. Here is what each should print and what it means.

### 4a. It looks up a **different** customer than it was taught on

```
STATUS            SUCCESS
outputs           {"savings_balance": 231.09}
```

The recording was made on member **12345**. This run asked for member **23456**
and got that member's balance.

**Why it matters:** if the system had simply memorised a sequence of clicks, it
could only ever return 12345's balance. Getting a *different* member's number
back is the difference between a reusable capability and a hardcoded macro.

### 4b. "No such customer" is an **answer**, not a crash

```
STATUS            BUSINESS_OUTCOME
outcome_code      MEMBER_NOT_FOUND
```

**Why it matters:** the caller asked a question and got a real answer. A system
that reported this as an error would make its users unable to tell "this
customer does not exist" from "the automation broke."

### 4c. It refuses to move money unsupervised

```
STATUS            ESCALATED
message           risk 'irreversible' exceeds unattended ceiling 'risky'
```

**Why it matters:** this scenario opens an account and moves money. Running
with nobody watching, it stopped itself and asked for a human. It did **not**
quietly do it anyway, and it did **not** simply give up either — it raised a
request.

### 4d. A human approves, and the same run finishes

```
STATUS            SUCCESS
steps executed    7
```

**Why it matters:** the human took over the *same* browser session, approved the
step, and handed control back — and the run continued to completion rather than
starting over. Pausing for a person is not the same as failing.

---

## Step 5 — Check that nothing is hardcoded

This is the single most important check on this page.

```bash
grep -n "value_template" capabilities/acme-servicing/member.savings_balance.read/v*.json
```

**You should see:**

```
"value_template": "{member_id}",
```

**What this proves:** the saved recording does not contain a customer number. It
contains a **blank to be filled in** — `{member_id}`. That is why Step 4a could
run it for a different member.

> **One thing that may confuse you:** searching the same file for `12345` finds
> exactly one match, on a line labelled `"example"`. That is documentation for
> whoever calls this capability — a hand-written illustration of what a member
> number looks like — not data left over from the recording. The project has an
> explicit rule that this field is never auto-filled from a real run, because
> real runs contain real customer identifiers.

---

## Step 6 — Check that no AI runs during the replay

The central claim is that the AI is used *once*, to learn the job, and is then
completely absent when the job is actually run.

```bash
grep -rnE "^\s*(import|from)\s+\S*(anthropic|openai)" --include=*.py cua/replay/
```

**You should see:** nothing at all. No output means no match.

**What this proves:** the code that performs the work in production does not
even load an AI library, so it cannot call one. This is not a promise in a
document — it is checkable in ten seconds.

---

## Step 7 — Follow the paper trail

Every run leaves a record. You can walk from a result back to the AI session
that originally learned the job.

```bash
cat evidence/rep_read_member_b/result.json | grep -E "capability_id|capability_version|discovery_run_id"
```

**You should see** a capability name, a version number, and a `discovery_run_id`
such as `disc_7034557bab`. Then look at that original session:

```bash
ls evidence/disc_*/
```

**What this proves:** an auditor holding only a production result can find which
saved recording produced it, and which AI session created that recording. This
is the chain a bank would be asked for.

---

## Step 8 — *(Optional, costs a little money)* Watch the AI learn it live

Steps 1–7 use a recording that already exists. This step makes a fresh one.

You need an Anthropic API key with credit on it. If your key is tied to a
specific workspace, you also need that workspace's ID.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_WORKSPACE_ID=wrkspc_...   # only if your key requires it
WITH_DISCOVERY=1 bash tools/demo.sh
```

**You should see** a new first section where the AI drives the screen itself,
then:

```
verifying: replaying the fresh artifact once, no model...
state:    draft_verified
```

then the same five scenarios from Step 4, now running against the recording the
AI just made.

**What this proves:** the recording is genuinely produced by an AI working
against a live screen. And note what happens between the two: the system
**replays its own fresh recording once, with the AI switched off**, before it
will save it. A recording that cannot reproduce itself is never stored.

Cost is a few cents. It usually takes under a minute.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| `py: command not found` | You are on macOS/Linux — use `python3`, not `py`. |
| `.venv/Scripts/python.exe: No such file` | You are on macOS/Linux — `export PY=.venv/bin/python`. |
| `Address already in use` | An old copy of the app is still running. Close terminal 1 and reopen it. |
| `the mock app is not running` | Terminal 1 stopped. Start it again (Step 2). |
| Browser fails to launch on Linux | Missing system libraries. Run `.venv/bin/python -m playwright install --with-deps chromium`, or ask an administrator to install Chromium's dependencies. |
| `credit balance is too low` | Step 8 only. Add credit at console.anthropic.com → Plans & Billing. |

### Putting things back

The demo rewrites the example records that ship with the project. To restore
them:

```bash
git checkout -- evidence/ capabilities/
```

Nothing outside this folder is touched, and no real customer data exists
anywhere in it — every member, balance and account number is invented.

---

## What you have and have not checked

**Checked:** that the job runs for an input it was never taught, that a "not
found" answer is not a crash, that money movement stops for a human, that a
paused run resumes, that no AI library is loaded during replay, that the
recording holds a placeholder rather than a customer number, and — in Step 8 —
that an AI really does produce the recording and that the recording must prove
itself before being saved.

**Not checked:** that this behaves the same against a real bank system. The
screen here is a stand-in built to be awkward in the ways old banking software
is awkward, but it is a stand-in.
