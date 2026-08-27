"""
Mock legacy credit-union servicing app.

Deliberately hostile surface, mirroring the brief's description of the real
environment: framesets, table-based layout, non-semantic markup, no test IDs,
server-rendered, inline handlers. All data is synthetic.

Fault injection (POST /_fault) lets replay evidence include real runtime
conditions on demand rather than waiting for luck:
    not_found | validation | session_timeout | interstitial | slow | app_error
"""

from __future__ import annotations

import time

from flask import Flask, request, redirect, make_response

app = Flask(__name__)

# --- synthetic data -------------------------------------------------------

MEMBERS = {
    "12345": {"name": "Jane Q. Sample", "branch": "Downtown",
              "accounts": [("Checking", "S0001", "1,204.18"),
                           ("Savings", "S0002", "4,812.55"),
                           ("Money Market", "S0003", "15,000.00")]},
    "23456": {"name": "Robert T. Example", "branch": "Northgate",
              "accounts": [("Checking", "S0101", "88.42"),
                           ("Savings", "S0102", "231.09")]},
}

FAULTS: dict[str, bool] = {}


def fault(name: str) -> bool:
    return FAULTS.get(name, False)


@app.post("/_fault")
def set_fault():
    """Test hook. Not part of the automated surface; not on the allowlist."""
    FAULTS.clear()
    name = request.form.get("name") or (request.json or {}).get("name")
    if name and name != "none":
        FAULTS[name] = True
    return {"faults": list(FAULTS)}


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
    )


@app.get("/servicing/reports")
def reports():
    if not logged_in():
        return timeout_page()
    return page('<div class="box"><div class="hdr">Reports</div>'
                '<div class="val">No reports available.</div></div>')


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8099, threaded=True)
