"""`dialog_present`, `load_failed`, and the unmodeled-blocker rule.

Both detector kinds were declared in the schema and implemented nowhere. The
gap they leave is not symmetrical with the others: a missing detector does not
make a run fail loudly, it makes the engine *unable to notice* a whole class of
runtime condition, which is how the native-confirm no-op stayed invisible.
"""

from __future__ import annotations

import pytest

import mockapp.app as mockapp
from cua.schema.artifact import (
    Checkpoint,
    ConditionHandler,
    Detector,
    Disposition,
    RecoveryAction,
)
from cua.schema.result import ReplayStatus
from cua.surface.web import WebSurfaceAdapter

pytestmark = pytest.mark.integration


class TestNativeDialogs:
    def test_a_native_confirm_is_recorded_rather_than_silently_dismissed(
            self, browser, app_url):
        """The measurement behind the design note: it does not hang, it lies."""
        page = browser.new_context().new_page()
        adapter = WebSurfaceAdapter(page)
        page.goto(f"{app_url}/servicing/")
        page.locator("input[name='op']").fill("op")
        page.locator("input[name='pw']").fill("pw")
        page.get_by_role("button", name="Sign In").click()

        mockapp.FAULTS["native_confirm"] = True
        page.goto(f"{app_url}/servicing/subaccount?mid=12345")
        page.locator("input[name='nick']").fill("Vacation Fund")
        page.locator("input[name='amt']").fill("250.00")
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Post").click()

        assert adapter.dialogs == [
            {"type": "confirm", "message": "Post this transaction?"}]
        # Dismissed, so the post did not happen -- and now we can prove it.
        assert len(mockapp.MEMBERS["12345"]["accounts"]) == 3

    def test_dialog_present_sees_it(self, browser, app_url):
        page = browser.new_context().new_page()
        adapter = WebSurfaceAdapter(page)
        page.goto(f"{app_url}/servicing/")
        page.evaluate("setTimeout(() => confirm('Post this transaction?'), 0)")
        page.wait_for_timeout(200)

        assert adapter.pending_dialogs()
        assert adapter.pending_dialogs()[0]["message"] == "Post this transaction?"


class TestLoadFailed:
    def test_an_http_error_page_is_a_failed_load(self, browser, app_url):
        """A served 500 is well-formed HTML; only the status says it broke."""
        page = browser.new_context().new_page()
        adapter = WebSurfaceAdapter(page)
        page.goto(f"{app_url}/servicing/")
        page.locator("input[name='op']").fill("op")
        page.locator("input[name='pw']").fill("pw")
        page.get_by_role("button", name="Sign In").click()

        mockapp.FAULTS["app_error"] = True
        adapter.navigate(f"{app_url}/servicing/member?mid=12345")
        assert adapter.last_status == 500

    def test_a_healthy_page_is_not(self, browser, app_url):
        page = browser.new_context().new_page()
        adapter = WebSurfaceAdapter(page)
        adapter.navigate(f"{app_url}/servicing/")
        assert adapter.last_status == 200
        assert adapter.last_nav_error is None


class TestUnmodeledBlocker:
    """An unexpected modal has no handler by construction — so it escalates."""

    def test_an_undeclared_modal_escalates_rather_than_failing(self, replay):
        mockapp.FAULTS["compliance_modal"] = True
        # The read capability knows nothing about a Regulation CC disclosure.
        result, _, _ = replay({"member_id": "12345"},
                              adapter_cls=_ModalOnMemberDetail)

        assert result.status is ReplayStatus.ESCALATED
        assert "unmodeled" in (result.message or "")
        assert result.escalations and not result.escalations[0].resolved

    def test_a_declared_dialog_is_handled_not_escalated(self, replay, artifact):
        """Declaring it is what turns a blocker into a known condition."""
        artifact.global_conditions.insert(0, ConditionHandler(
            condition_id="reg_cc_disclosure",
            detect=Detector(kind="dialog_present", value="Regulation CC"),
            disposition=Disposition.HARD_FAILURE,
            message="Disclosure acknowledgment required.",
            recovery=RecoveryAction(kind="none"),
            scope="global"))
        mockapp.FAULTS["compliance_modal"] = True

        result, _, _ = replay({"member_id": "12345"},
                              adapter_cls=_ModalOnMemberDetail)

        assert result.status is ReplayStatus.FAILED
        assert "unmodeled" not in (result.message or "")


class _ModalOnMemberDetail(WebSurfaceAdapter):
    """Raises an undeclared ARIA modal once the flow reaches member detail.

    Injected at snapshot time rather than on click: a click here submits a form,
    and the resulting navigation throws away anything added to the old document.
    Snapshot runs after the surface has settled, which is also where a real
    app's own script would have raised the thing.
    """

    def snapshot(self, with_screenshot: bool = False):
        # detailFrame, not the main document -- a frameset has no body.
        frame = next((f for f in self.page.frames if f.name == "detailFrame"), None)
        if frame is not None:
            try:
                frame.evaluate("""
                    () => {
                      if (!document.body) return;
                      if (!document.body.innerText.includes('Account Summary')) return;
                      if (document.querySelector('[role=dialog]')) return;
                      const d = document.createElement('div');
                      d.setAttribute('role', 'dialog');
                      d.setAttribute('aria-label', 'Regulation CC Disclosure');
                      d.textContent = 'Regulation CC funds availability must be acknowledged.';
                      document.body.appendChild(d);
                    }
                """)
            except Exception:
                pass
        return super().snapshot(with_screenshot)
