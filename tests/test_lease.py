"""Lease transitions.

The point of the lease is not bookkeeping, it is that exactly one party can act
on the live session at a time. The tests that matter are the refusals: a late or
duplicated automation action must not race a human who has taken control.
"""

from __future__ import annotations

import pytest

from cua.escalation.lease import LeaseViolation, Owner, SessionLease


@pytest.fixture
def lease() -> SessionLease:
    return SessionLease()


def test_starts_owned_by_automation(lease):
    assert lease.owner is Owner.AUTOMATION
    lease.assert_automation()


def test_full_cycle_returns_control(lease):
    lease.request_handoff("risk gate")
    assert lease.owner is Owner.PENDING_HANDOFF
    lease.operator_take_control()
    assert lease.owner is Owner.OPERATOR
    lease.operator_hand_back("opened the account by hand")
    assert lease.owner is Owner.AUTOMATION
    lease.assert_automation()


def test_automation_refuses_to_act_while_a_human_holds_the_lease(lease):
    lease.request_handoff("risk gate")
    lease.operator_take_control()
    with pytest.raises(LeaseViolation):
        lease.assert_automation()


def test_automation_refuses_to_act_while_a_handoff_is_pending(lease):
    """The window between raising a request and a human accepting it."""
    lease.request_handoff("risk gate")
    with pytest.raises(LeaseViolation):
        lease.assert_automation()


def test_operator_cannot_take_control_unprompted(lease):
    with pytest.raises(LeaseViolation):
        lease.operator_take_control()


def test_handback_requires_holding_the_lease(lease):
    with pytest.raises(LeaseViolation):
        lease.operator_hand_back()


def test_second_handoff_request_is_refused(lease):
    lease.request_handoff("first")
    with pytest.raises(LeaseViolation):
        lease.request_handoff("second")


def test_history_records_every_transition_with_a_reason(lease):
    lease.request_handoff("risk gate")
    lease.operator_take_control()
    lease.operator_hand_back("done")
    hist = lease.history()
    assert [h["to"] for h in hist] == ["pending_handoff", "operator", "automation"]
    assert all(h["why"] and h["at"] for h in hist)


def test_reclaim_returns_control_when_nobody_took_it(lease):
    """An escalation resolved out of band: raised, answered, never held."""
    lease.request_handoff("risk gate")
    lease.reclaim("authorised without an operator")
    assert lease.owner is Owner.AUTOMATION
    lease.assert_automation()
    assert lease.history()[-1]["from"] == "pending_handoff"


def test_reclaim_records_a_release_rather_than_erasing_the_handoff(lease):
    lease.request_handoff("risk gate")
    lease.operator_take_control()
    lease.reclaim("operator session ended")
    assert lease.owner is Owner.AUTOMATION
    assert [h["to"] for h in lease.history()] == [
        "pending_handoff", "operator", "automation"]


def test_reclaim_is_a_no_op_when_automation_already_holds_it(lease):
    lease.reclaim("nothing to do")
    assert lease.owner is Owner.AUTOMATION
    assert lease.history() == []
