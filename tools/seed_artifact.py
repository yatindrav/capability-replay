"""
Seed capability artifacts without calling the model.

This exists so replay, error handling and escalation can be exercised and tested
without an API key. It feeds recorded-shape transcripts through the *same*
`build_artifact()` the discovery agent uses — it does not hand-write the
artifacts. Running `python -m cua discover` produces the real thing and
supersedes these.

Two capabilities, because they exercise different halves of the system:

- `member.savings_balance.read` — a read. Returns a typed output, and is the
  one whose replay is expected to run green end to end.
- `member.subaccount.open` — a write whose last step is `IRREVERSIBLE`. Its
  replay is *expected to escalate*: the risk gate stops it, which is the whole
  point. Nothing is posted unless a human authorises it.

Provenance says `(seeded)` on both. They were not discovered by a model and the
artifact should not claim otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.agent.discovery import build_artifact
from cua.agent.recorder import capability_path
from cua.schema.artifact import ParamSpec, Sensitivity

ENTRY = "http://127.0.0.1:8099/servicing/"

# Shape of what DiscoveryAgent.recorded contains after a successful run.
TRANSCRIPT = {
    "ok": True,
    "parameters": {"member_id": "12345"},
    "success_text": "Account Summary",
    "summary": ("Look up a credit-union member by member number and return their "
                "current savings balance from the account summary screen."),
    "recorded": [
        {
            "tool": "navigate",
            "input": {"url": ENTRY, "intent": "Open the servicing frameset"},
            "control": None,
        },
        {
            "tool": "type_text",
            "input": {
                "role": "textbox", "frame": "detailFrame", "text": "12345",
                "near_text": "Member Number",
                "intent": "Enter the member number into the search form",
                "robustness_note": (
                    "The input has no accessible name in this table-based layout, "
                    "but it is the only textbox in the search frame and sits "
                    "immediately after the 'Member Number' label, which is a "
                    "vendor string rather than tenant branding."),
            },
            "control": {
                "role": "textbox", "name": None, "name_match": "exact",
                "frame": {"path": ["detailFrame"]}, "nth": None,
                "near_text": "Member Number", "within_section": None,
                "fallbacks": [{
                    "strategy": "text_anchor", "value": "after=Member Number",
                    "confidence": 0.6,
                    "note": "Positional anchor relative to stable label text.",
                }],
                "robustness_note": "Only textbox in the search frame; anchored to a vendor label.",
            },
        },
        {
            "tool": "click",
            "input": {
                "role": "button", "name": "Search", "frame": "detailFrame",
                "intent": "Submit the member search",
                "robustness_note": (
                    "Submit control carries the accessible name 'Search' from its "
                    "value attribute. Tenants re-brand colours and logos, not the "
                    "vendor's control labels."),
            },
            "control": {
                "role": "button", "name": "Search", "name_match": "exact",
                "frame": {"path": ["detailFrame"]}, "nth": None,
                "near_text": None, "within_section": None, "fallbacks": [],
                "robustness_note": "Vendor-fixed control label.",
            },
        },
        {
            "tool": "read_value",
            "input": {
                "output_name": "savings_balance", "role": "cell",
                "frame": "detailFrame", "row_label": "Savings",
                "col_label": "Current Balance", "section": "Account Summary",
                "intent": "Read the savings balance from the account summary table",
                "robustness_note": (
                    "Addressed by row and column *labels* rather than cell position. "
                    "Account ordering and markup differ between tenant installs; the "
                    "'Savings' row label and 'Current Balance' column header are "
                    "vendor-fixed."),
            },
            "control": {
                "role": "cell", "name": None, "name_match": "exact",
                "frame": {"path": ["detailFrame"]}, "nth": None,
                "near_text": None, "within_section": "Account Summary",
                "fallbacks": [{
                    "strategy": "table_cell",
                    "value": "row=Savings;col=Current Balance", "confidence": 0.85,
                    "note": "Row/column labels are vendor-fixed strings.",
                }],
                "robustness_note": "Row/column label addressing survives re-branding.",
            },
        },
    ],
}


SUBACCOUNT = {
    "ok": True,
    # Two parameters, because the amount is as much a per-invocation input as
    # the member is. Baking "250.00" into the artifact would make the capability
    # "open a $250 sub-account", which is not a capability anyone would call
    # twice -- and it would hide a whole class of business outcome, since the
    # deposit is what triggers INSUFFICIENT_FUNDS and DEPOSIT_BELOW_MINIMUM.
    # The nickname is a per-invocation input for exactly the reason the deposit
    # is: baked in, this becomes "open a sub-account called Vacation Fund", and
    # every account it ever opens carries one caller's chosen name.
    "parameters": {"member_id": "12345", "opening_deposit": "250.00",
                   "nickname": "Vacation Fund"},
    "success_text": "Sub-Account Opened",
    "summary": ("Open a new sub-account for a credit-union member, funded by a "
                "transfer from one of their existing accounts, and post it."),
    "recorded": [
        {"tool": "navigate", "risk": "safe_reversible", "control": None,
         "input": {"url": ENTRY.rstrip("/") + "/subaccount?mid=12345",
                   "intent": "Open the sub-account form for this member"}},
        {"tool": "select_option", "risk": "safe_reversible",
         "control": {"role": "combobox", "near_text": "Account Type",
                     "robustness_note": "Anchored to the vendor's field label.",
                     "fallbacks": [{"strategy": "text_anchor",
                                    "value": "after=Account Type",
                                    "confidence": 0.6,
                                    "note": "Nearest combobox after the label."}]},
         "input": {"role": "combobox", "value": "Savings",
                   "near_text": "Account Type",
                   "robustness_note": "Anchored to the vendor's field label.",
                   "intent": "Choose the type of sub-account to open"}},
        {"tool": "type_text", "risk": "safe_reversible",
         "control": {"role": "textbox", "near_text": "Nickname",
                     "robustness_note": "Anchored to the vendor's field label.",
                     "fallbacks": [{"strategy": "text_anchor",
                                    "value": "after=Nickname", "confidence": 0.6,
                                    "note": "Nearest textbox after the label."}]},
         "input": {"role": "textbox", "text": "Vacation Fund",
                   "near_text": "Nickname",
                   "robustness_note": "Anchored to the vendor's field label.",
                   "intent": "Name the new sub-account"}},
        {"tool": "type_text", "risk": "safe_reversible",
         "control": {"role": "textbox", "near_text": "Opening Deposit",
                     "robustness_note": "Anchored to the vendor's field label.",
                     "fallbacks": [{"strategy": "text_anchor",
                                    "value": "after=Opening Deposit",
                                    "confidence": 0.6,
                                    "note": "Nearest textbox after the label."}]},
         "input": {"role": "textbox", "text": "250.00",
                   "near_text": "Opening Deposit",
                   "robustness_note": "Anchored to the vendor's field label.",
                   "intent": "Set the opening deposit"}},
        {"tool": "click", "risk": "safe_reversible",
         "control": {"role": "button", "name": "Continue",
                     "robustness_note": "Vendor-fixed control label."},
         "input": {"role": "button", "name": "Continue",
                   "robustness_note": "Vendor-fixed control label.",
                   "intent": "Submit the form for server-side validation"}},
        {"tool": "assert_state", "risk": "safe_reversible", "control": None,
         "input": {"text": "Review and Post",
                   "intent": "Prove we reached the last reversible screen "
                             "before committing anything"}},
        {"tool": "click", "risk": "irreversible",
         "control": {"role": "button", "name": "Post",
                     "robustness_note": "Vendor-fixed control label."},
         "input": {"role": "button", "name": "Post",
                   "robustness_note": "Vendor-fixed control label.",
                   "intent": "Post the transaction: debits the source account "
                             "and opens the new sub-account"}},
    ],
}


def _write(art) -> None:
    out = capability_path("capabilities", art)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(art.model_dump_json(indent=2), encoding="utf-8")
    print(f"wrote {out}")


def main() -> None:
    art = build_artifact(
        capability_id="member.savings_balance.read",
        title="Read member savings balance",
        description=TRANSCRIPT["summary"],
        goal="look up member 12345 and read their current savings balance",
        entry_url=ENTRY,
        app_id="acme-servicing",
        allowlist_id="acme-servicing-readonly",
        discovery=TRANSCRIPT,
        model="(seeded — replaced by a real discovery run)",
        run_id="disc_seed",
        param_specs=[ParamSpec(
            name="member_id", type="string", required=True,
            description="Credit-union member number.",
            sensitivity=Sensitivity.PII, pattern=r"^\d+$", example="12345",
        )],
        output_specs_hint={
            "savings_balance": "Current savings account balance in USD.",
        },
    )
    _write(art)

    write_cap = build_artifact(
        capability_id="member.subaccount.open",
        title="Open a funded sub-account",
        description=SUBACCOUNT["summary"],
        goal="open a new sub-account for member 12345 and reach the confirmation screen",
        entry_url=ENTRY,
        app_id="acme-servicing",
        # The write allowlist permits risky steps unattended; the irreversible
        # post still exceeds its ceiling and stops for a human.
        allowlist_id="acme-servicing-write",
        discovery=SUBACCOUNT,
        model="(seeded — replaced by a real discovery run)",
        run_id="disc_seed_write",
        param_specs=[
            ParamSpec(
                name="member_id", type="string", required=True,
                description="Credit-union member number.",
                sensitivity=Sensitivity.PII, pattern=r"^\d+$", example="12345",
            ),
            ParamSpec(
                name="opening_deposit", type="string", required=True,
                description=("Opening deposit in dollars, debited from the "
                             "member's first listed account. Minimum $25.00."),
                pattern=r"^\d{1,3}(,\d{3})*(\.\d{2})?$|^\d+(\.\d{2})?$",
                example="100.00",
            ),
            ParamSpec(
                name="nickname", type="string", required=True,
                description=("Display name for the new sub-account, as the "
                             "member asked for it."),
                # Author-supplied, and deliberately not the value the recording
                # used: `example` must never be filled from a run.
                example="Holiday Savings",
            ),
        ],
    )
    _write(write_cap)

    print()
    print(json.dumps(art.tool_schema(), indent=2))


if __name__ == "__main__":
    main()
