"""
Session bootstrap.

Authentication is deliberately **not** part of a capability artifact.

Two reasons. First, every capability recorded against an app would otherwise
carry a duplicate copy of the same login steps, and re-recording after a login
page change would mean re-recording every capability. Second, credentials are
tenant-runtime configuration, not discovered behaviour — putting them anywhere
near a recorded flow invites them into the artifact.

So the seam is: the platform establishes an authenticated session, then hands a
ready session to discovery or replay. The artifact starts from "signed in".

The session-expiry ConditionHandler in the artifact closes the loop: if the
session drops mid-replay, the recoverable handler re-enters this bootstrap.
"""

from __future__ import annotations

import os

from playwright.sync_api import Page

from cua.safety.policy import PolicyViolation


def bootstrap_session(page: Page, entry_url: str, app_id: str) -> bool:
    """Establish an authenticated session on the live page.

    Returns True if a login was performed, False if already authenticated.
    Per-app logic; in a real deployment this is a small per-vendor plugin
    alongside the surface adapter.
    """
    if app_id != "acme-servicing":
        raise NotImplementedError(f"no bootstrap registered for app '{app_id}'")

    page.goto(entry_url, wait_until="load")

    # Already signed in: the frameset is present.
    if page.locator("frameset").count() > 0:
        return False

    operator = os.environ.get("SVC_OPERATOR_ID")
    password = os.environ.get("SVC_PASSWORD")
    if not operator or not password:
        raise PolicyViolation(
            "SVC_OPERATOR_ID and SVC_PASSWORD must be set in the environment. "
            "Credentials are never stored in artifacts, config, or the repo."
        )

    page.locator("input[name='op']").fill(operator)
    page.locator("input[name='pw']").fill(password)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_load_state("load")

    if page.locator("frameset").count() == 0:
        raise PolicyViolation("session bootstrap failed: sign-in did not complete")
    return True
