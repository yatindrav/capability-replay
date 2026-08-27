"""
Mock legacy credit-union servicing app.

Deliberately hostile surface, mirroring the brief's description of the real
environment: framesets, table-based layout, non-semantic markup, no test IDs,
server-rendered, inline handlers. All data is synthetic.

Two flows: a read (search -> member detail -> balance) and a write (open a
funded sub-account -> validation -> review -> post). The write flow exists so
that `RiskClass.IRREVERSIBLE`, the policy gate's confirmation path, and the
escalation story have something real to fire against: posting moves money
between accounts and a one-time token makes it non-idempotent.

Fault injection (POST /_fault) lets replay evidence include real runtime
conditions on demand rather than waiting for luck:
    not_found | validation | session_timeout | interstitial | slow | app_error
    native_confirm | compliance_modal | post_error

POST /_reset restores the synthetic balances, since the write flow mutates them.
"""

from __future__ import annotations

import copy
import re
import time
from datetime import datetime

from flask import Flask, request, redirect, make_response

app = Flask(__name__)

# --- synthetic data -------------------------------------------------------

PRISTINE = {
    "12345": {"name": "Jane Q. Sample", "branch": "Downtown",
              "accounts": [("Checking", "S0001", "1,204.18"),
                           ("Savings", "S0002", "4,812.55"),
                           ("Money Market", "S0003", "15,000.00")]},
    "23456": {"name": "Robert T. Example", "branch": "Northgate",
              "accounts": [("Checking", "S0101", "88.42"),
                           ("Savings", "S0102", "231.09")]},
}

# The sub-account flow mutates this. `PRISTINE` is the reset point.
MEMBERS = copy.deepcopy(PRISTINE)

# Posted-but-unconsumed confirmation tokens, keyed by token. A legacy app guards
# double submission this way, and it is what makes the post step genuinely
# non-idempotent rather than merely labelled irreversible.
PENDING: dict[str, dict] = {}

FAULTS: dict[str, bool] = {}
SEQ = {"token": 0, "confirmation": 8830}


def fault(name: str) -> bool:
    return FAULTS.get(name, False)


def _cents(display: str) -> int:
    """'1,204.18' -> 120418. Integer cents; a bank app that rounds is a bug."""
    cleaned = display.replace(",", "").replace("$", "").strip()
    whole, _, frac = cleaned.partition(".")
    return int(whole or 0) * 100 + int((frac + "00")[:2])


def _display(cents: int) -> str:
    """120418 -> '1,204.18'. Inverse of `_cents`, same format as the seed data."""
    return f"{cents // 100:,}.{cents % 100:02d}"


def _next_account_number(accounts: list[tuple[str, str, str]]) -> str:
    """Next suffix in the member's own numbering, preserving width.

    S0001..S0003 -> S0004;  S0101..S0102 -> S0103. Vendor apps number within
    the member, not globally, and the width is part of the format.
    """
    best, width, prefix = 0, 4, "S"
    for _, number, _ in accounts:
        m = re.fullmatch(r"([A-Za-z]*)(\d+)", number)
        if m:
            prefix, digits = m.group(1), m.group(2)
            width = len(digits)
            best = max(best, int(digits))
    return f"{prefix}{best + 1:0{width}d}"


@app.post("/_fault")
def set_fault():
    """Test hook. Not part of the automated surface; not on the allowlist."""
    FAULTS.clear()
    name = request.form.get("name") or (request.json or {}).get("name")
    if name and name != "none":
        FAULTS[name] = True
    return {"faults": list(FAULTS)}


@app.post("/_reset")
def reset():
    """Test hook, like /_fault and equally off the allowlist.

    The write flow moves money, so without a reset point the second evidence run
    starts from the first run's balances and nothing is reproducible.
    """
    MEMBERS.clear()
    MEMBERS.update(copy.deepcopy(PRISTINE))
    PENDING.clear()
    FAULTS.clear()
    return {"reset": sorted(MEMBERS)}


# --- chrome ---------------------------------------------------------------

STYLE = """<style>
body{font-family:Verdana,Arial;font-size:11px;background:#dfe3e8;margin:0}
table{border-collapse:collapse}
.hdr{background:#1b4a7a;color:#fff;padding:6px;font-weight:bold}
.box{border:1px solid #8a99aa;background:#fff;margin:8px;padding:0}
.lbl{background:#eceff3;padding:4px 8px;border-bottom:1px solid #d5dae0}
.val{padding:4px 8px;border-bottom:1px solid #d5dae0}
.err{color:#a00;font-weight:bold;padding:6px}
input,select{font-family:Verdana;font-size:11px}
</style>"""


def page(body: str) -> str:
    return f"<html><head>{STYLE}</head><body>{body}</body></html>"


# --- session --------------------------------------------------------------


def logged_in() -> bool:
    return request.cookies.get("svc_session") == "ok" and not fault("session_timeout")


def timeout_page() -> str:
    return page(
        '<div class="box"><div class="hdr">Session</div>'
        '<div class="err">Your session has expired. Please sign in again.</div>'
        '<form method="post" action="/login">'
        '<input type="submit" value="Sign In"></form></div>'
    )


@app.get("/")
def root():
    return redirect("/servicing/")


@app.get("/servicing/")
def frameset():
    """Classic frameset. The only reliable addressing here is role+name."""
    if not logged_in():
        return page(
            '<div class="box"><div class="hdr">ACME Credit Union &mdash; Servicing</div>'
            '<form method="post" action="/login"><table>'
            '<tr><td class="lbl">Operator ID</td><td class="val">'
            '<input type="text" name="op"></td></tr>'
            '<tr><td class="lbl">Password</td><td class="val">'
            '<input type="password" name="pw"></td></tr>'
            '<tr><td colspan="2" class="val">'
            '<input type="submit" value="Sign In"></td></tr>'
            "</table></form></div>"
        )
    return (
        '<html><head><title>Servicing</title></head>'
        '<frameset cols="180,*" border="1">'
        '<frame name="navFrame" src="/servicing/nav">'
        '<frame name="detailFrame" src="/servicing/search">'
        "</frameset></html>"
    )


@app.post("/login")
def login():
    FAULTS.pop("session_timeout", None)
    r = make_response(redirect("/servicing/"))
    r.set_cookie("svc_session", "ok")
    return r


@app.get("/servicing/nav")
def nav():
    return page(
        '<div class="box"><div class="hdr">Menu</div>'
        '<table><tr><td class="val">'
        '<a href="/servicing/search" target="detailFrame">Member Search</a></td></tr>'
        '<tr><td class="val">'
        '<a href="/servicing/reports" target="detailFrame">Reports</a></td></tr>'
        "</table></div>"
    )


# --- member search --------------------------------------------------------


@app.get("/servicing/search")
def search():
    if not logged_in():
        return timeout_page()
    if fault("interstitial"):
        return page(
            '<div class="box"><div class="hdr">Notice</div>'
            '<div class="val">Scheduled maintenance this Sunday 02:00-04:00.</div>'
            '<div class="val"><form method="get" action="/servicing/search">'
            '<input type="submit" value="Acknowledge"></form></div></div>'
        )
    return page(
        '<div class="box"><div class="hdr">Member Search</div>'
        '<form method="get" action="/servicing/member"><table>'
        '<tr><td class="lbl">Member Number</td><td class="val">'
        '<input type="text" name="mid" size="14"></td></tr>'
        '<tr><td colspan="2" class="val">'
        '<input type="submit" value="Search"></td></tr>'
        "</table></form></div>"
    )


@app.get("/servicing/member")
def member():
    if not logged_in():
        return timeout_page()

    mid = (request.args.get("mid") or "").strip()

    if fault("slow"):
        time.sleep(6)
    if fault("app_error"):
        return page(
            '<div class="box"><div class="hdr">Error</div>'
            '<div class="err">SVC-500: An unexpected application error occurred. '
            "Reference 8831-A.</div></div>"
        ), 500

    # Validation: the vendor app rejects non-numeric input before lookup.
    if fault("validation") or (mid and not mid.isdigit()):
        return page(
            '<div class="box"><div class="hdr">Member Search</div>'
            '<div class="err">Member number must be numeric.</div>'
            '<form method="get" action="/servicing/member"><table>'
            '<tr><td class="lbl">Member Number</td><td class="val">'
            f'<input type="text" name="mid" size="14" value="{mid}"></td></tr>'
            '<tr><td colspan="2" class="val">'
            '<input type="submit" value="Search"></td></tr>'
            "</table></form></div>"
        )

    rec = None if fault("not_found") else MEMBERS.get(mid)
    if rec is None:
        return page(
            '<div class="box"><div class="hdr">Member Search</div>'
            '<div class="err">No member found for that number.</div>'
            '<form method="get" action="/servicing/member"><table>'
            '<tr><td class="lbl">Member Number</td><td class="val">'
            '<input type="text" name="mid" size="14"></td></tr>'
            '<tr><td colspan="2" class="val">'
            '<input type="submit" value="Search"></td></tr>'
            "</table></form></div>"
        )

    rows = "".join(
        f'<tr><td class="val">{t}</td><td class="val">{n}</td>'
        f'<td class="val" align="right">${b}</td></tr>'
        for t, n, b in rec["accounts"]
    )
    return page(
        f'<div class="box"><div class="hdr">Member Detail</div>'
        f'<table><tr><td class="lbl">Name</td><td class="val">{rec["name"]}</td></tr>'
        f'<tr><td class="lbl">Member Number</td><td class="val">{mid}</td></tr>'
        f'<tr><td class="lbl">Branch</td><td class="val">{rec["branch"]}</td></tr>'
        "</table></div>"
        '<div class="box"><div class="hdr">Account Summary</div>'
        '<table><tr><td class="lbl">Type</td><td class="lbl">Account</td>'
        '<td class="lbl">Current Balance</td></tr>'
        f"{rows}</table></div>"
        '<div class="box"><div class="hdr">Servicing Actions</div>'
        f'<table><tr><td class="val">'
        f'<a href="/servicing/subaccount?mid={mid}">Open Sub-Account</a>'
        "</td></tr></table></div>"
    )


# --- open sub-account: the write flow -------------------------------------
#
# Three screens on purpose. The entry form is where parameters land, the review
# screen is the last reversible point, and the post is the irreversible one.
# Collapsing review into the form would remove the only place a human -- or a
# policy gate -- can still say no.

MIN_DEPOSIT_CENTS = 2500
ACCOUNT_TYPES = ["Savings", "Money Market", "Certificate"]


def _not_found_page() -> str:
    return page(
        '<div class="box"><div class="hdr">Member Search</div>'
        '<div class="err">No member found for that number.</div>'
        '<form method="get" action="/servicing/member"><table>'
        '<tr><td class="lbl">Member Number</td><td class="val">'
        '<input type="text" name="mid" size="14"></td></tr>'
        '<tr><td colspan="2" class="val">'
        '<input type="submit" value="Search"></td></tr>'
        "</table></form></div>"
    )


def _subaccount_form(mid: str, rec: dict, error: str = "",
                     atype: str = "", nick: str = "", amt: str = "",
                     src: str = "") -> str:
    """Entry form.

    Labels are bare table cells with no `for` attribute, like the rest of this
    app -- so none of these controls has an accessible name, and role alone is
    ambiguous between the two textboxes and the two selects. Resolving them is
    what the `near_text` / `text_anchor` strategies are for.
    """
    types = "".join(
        f'<option value="{t}"{" selected" if t == atype else ""}>{t}</option>'
        for t in ACCOUNT_TYPES
    )
    sources = "".join(
        f'<option value="{n}"{" selected" if n == src else ""}>{t} {n}</option>'
        for t, n, _ in rec["accounts"]
    )
    err = f'<div class="err">{error}</div>' if error else ""
    return page(
        '<div class="box"><div class="hdr">Open Sub-Account</div>'
        f"{err}"
        '<form method="post" action="/servicing/subaccount/review"><table>'
        f'<tr><td class="lbl">Member Number</td><td class="val">{mid}</td></tr>'
        f'<tr><td class="lbl">Member Name</td><td class="val">{rec["name"]}</td></tr>'
        '<tr><td class="lbl">Account Type</td><td class="val">'
        f'<select name="atype">{types}</select></td></tr>'
        '<tr><td class="lbl">Nickname</td><td class="val">'
        f'<input type="text" name="nick" size="24" value="{nick}"></td></tr>'
        '<tr><td class="lbl">Opening Deposit</td><td class="val">'
        f'<input type="text" name="amt" size="12" value="{amt}"></td></tr>'
        '<tr><td class="lbl">Fund From</td><td class="val">'
        f'<select name="src">{sources}</select></td></tr>'
        '<tr><td colspan="2" class="val">'
        f'<input type="hidden" name="mid" value="{mid}">'
        '<input type="submit" value="Continue"></td></tr>'
        "</table></form></div>"
    )


@app.get("/servicing/subaccount")
def subaccount_form():
    if not logged_in():
        return timeout_page()
    mid = (request.args.get("mid") or "").strip()
    rec = MEMBERS.get(mid)
    if rec is None:
        return _not_found_page()
    return _subaccount_form(mid, rec, src=rec["accounts"][0][1])


@app.post("/servicing/subaccount/review")
def subaccount_review():
    """Server-side validation, then the last reversible screen.

    Two of these are validation errors and two are business outcomes, and the
    difference is not cosmetic: a malformed amount means the caller sent
    something wrong, while "below the minimum opening deposit" is the app
    correctly answering a well-formed request. A replay engine that conflates
    them reports a crash where the caller needed an answer.
    """
    if not logged_in():
        return timeout_page()

    mid = (request.form.get("mid") or "").strip()
    rec = MEMBERS.get(mid)
    if rec is None:
        return _not_found_page()

    atype = request.form.get("atype") or ""
    nick = (request.form.get("nick") or "").strip()
    amt = (request.form.get("amt") or "").strip()
    src = request.form.get("src") or ""
    form = lambda err: _subaccount_form(mid, rec, err, atype, nick, amt, src)

    if fault("validation") or not re.fullmatch(r"\$?[\d,]+(\.\d{1,2})?", amt or ""):
        return form("Opening deposit must be a dollar amount.")
    if not re.fullmatch(r"[A-Za-z0-9 ]{1,20}", nick):
        return form("Nickname must be 1-20 letters, digits or spaces.")

    source = next((a for a in rec["accounts"] if a[1] == src), None)
    if source is None:
        return form("Select an account to fund from.")

    cents = _cents(amt)
    if cents < MIN_DEPOSIT_CENTS:
        return form(f"Opening deposit must be at least "
                    f"${_display(MIN_DEPOSIT_CENTS)}.")
    if cents > _cents(source[2]):
        return form("Insufficient funds in the source account.")

    SEQ["token"] += 1
    token = f"tk{SEQ['token']:06d}"
    PENDING[token] = {"mid": mid, "atype": atype, "nick": nick,
                      "cents": cents, "src": src}

    after = _display(_cents(source[2]) - cents)
    # An unmodeled modal: nothing in a recorded observation of this screen
    # accounts for it, which is the case that must escalate rather than fail.
    modal = (
        '<div role="dialog" aria-label="Regulation CC Disclosure" '
        'style="border:2px solid #1b4a7a;background:#fff;margin:8px;padding:8px">'
        '<div class="hdr">Regulation CC Disclosure</div>'
        '<div class="val">Funds availability disclosure must be acknowledged '
        'before this account can be opened.</div>'
        '<div class="val"><input type="submit" value="Acknowledge"></div></div>'
    ) if fault("compliance_modal") else ""
    # A native confirm() blocks the browser outright until something answers it.
    confirm = (' onclick="return confirm(\'Post this transaction?\')"'
               if fault("native_confirm") else "")

    return page(
        f"{modal}"
        '<div class="box"><div class="hdr">Review and Post</div>'
        '<table>'
        f'<tr><td class="lbl">Member Number</td><td class="val">{mid}</td></tr>'
        f'<tr><td class="lbl">Member Name</td><td class="val">{rec["name"]}</td></tr>'
        f'<tr><td class="lbl">Account Type</td><td class="val">{atype}</td></tr>'
        f'<tr><td class="lbl">Nickname</td><td class="val">{nick}</td></tr>'
        f'<tr><td class="lbl">Opening Deposit</td>'
        f'<td class="val">${_display(cents)}</td></tr>'
        f'<tr><td class="lbl">Fund From</td>'
        f'<td class="val">{source[0]} {source[1]}</td></tr>'
        f'<tr><td class="lbl">Source Balance After</td>'
        f'<td class="val">${after}</td></tr>'
        "</table>"
        '<form method="post" action="/servicing/subaccount/post">'
        f'<input type="hidden" name="token" value="{token}">'
        f'<div class="val"><input type="submit" value="Post"{confirm}>'
        f'&nbsp;<a href="/servicing/member?mid={mid}">Cancel</a></div>'
        "</form></div>"
    )


@app.post("/servicing/subaccount/post")
def subaccount_post():
    """The irreversible step: debits the source and opens the new account."""
    if not logged_in():
        return timeout_page()

    token = request.form.get("token") or ""
    pend = PENDING.pop(token, None)
    if pend is None:
        # The token is spent. Re-posting must not open a second account, and
        # saying so plainly is what lets a caller tell "already done" from
        # "failed" -- the distinction automation cannot make on its own.
        return page(
            '<div class="box"><div class="hdr">Open Sub-Account</div>'
            '<div class="err">This request has already been processed. '
            "No further action was taken.</div></div>"
        )

    rec = MEMBERS[pend["mid"]]
    number = _next_account_number(rec["accounts"])
    rec["accounts"] = [
        (t, n, _display(_cents(b) - pend["cents"]) if n == pend["src"] else b)
        for t, n, b in rec["accounts"]
    ]
    rec["accounts"].append((pend["atype"], number, _display(pend["cents"])))

    if fault("post_error"):
        # The worst case in the taxonomy, and the reason the escalation path
        # exists: the transaction HAS posted, but the operator sees only an
        # error. Retrying would be wrong and giving up would be wrong; from
        # outside the app nothing can tell which happened.
        return page(
            '<div class="box"><div class="hdr">Error</div>'
            '<div class="err">SVC-500: An unexpected application error occurred. '
            "Reference 8831-B. The status of this transaction is unknown.</div>"
            "</div>"
        ), 500

    SEQ["confirmation"] += 1
    return page(
        '<div class="box"><div class="hdr">Sub-Account Opened</div>'
        '<table>'
        f'<tr><td class="lbl">New Account Number</td>'
        f'<td class="val">{number}</td></tr>'
        f'<tr><td class="lbl">Nickname</td><td class="val">{pend["nick"]}</td></tr>'
        f'<tr><td class="lbl">Opening Deposit</td>'
        f'<td class="val">${_display(pend["cents"])}</td></tr>'
        f'<tr><td class="lbl">Confirmation Number</td>'
        f'<td class="val">{SEQ["confirmation"]}-C</td></tr>'
        f'<tr><td class="lbl">Posted</td><td class="val">'
        f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</td></tr>'
        "</table></div>"
        f'<div class="box"><div class="val">'
        f'<a href="/servicing/member?mid={pend["mid"]}">Return to Member Detail</a>'
        "</div></div>"
    )


@app.get("/servicing/reports")
def reports():
    if not logged_in():
        return timeout_page()
    return page('<div class="box"><div class="hdr">Reports</div>'
                '<div class="val">No reports available.</div></div>')


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8099, threaded=True)
