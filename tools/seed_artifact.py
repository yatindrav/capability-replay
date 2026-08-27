"""
Seed a capability artifact without calling the model.

This exists so replay, error handling and escalation can be exercised and tested
without an API key. It feeds a recorded-shape transcript through the *same*
`build_artifact()` the discovery agent uses — it does not hand-write the
artifact. Running `python -m cua discover` produces the real thing and
overwrites this file.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.agent.discovery import build_artifact
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
    out = Path("capabilities") / f"{art.capability_id}.v{art.version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(art.model_dump_json(indent=2))
    print(f"wrote {out}")
    print(json.dumps(art.tool_schema(), indent=2))


if __name__ == "__main__":
    main()
