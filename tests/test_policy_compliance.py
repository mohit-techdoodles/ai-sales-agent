"""
tests/test_policy_compliance.py

Regression tests for policy.py — the V3 "Policy Engine" (Safe -> execute,
Risky -> human approval). The blueprint's own guardrail is conservative by
default: "Require human approval for discounts, pricing exceptions,
contractual commitments, sensitive claims and high-value opportunities."

These tests lock in that the SAFE DEFAULT (nothing configured) never
auto-approves anything, and that a configured value cap is actually
enforced against real opportunity data rather than trusted blindly.
"""
import policy
from leads import receive_lead
from opportunities import create_opportunity


def _lead():
    import uuid
    return receive_lead(
        name="Policy Test", email=f"policy-{uuid.uuid4().hex[:8]}@example.com",
        company="PolicyCo", source="test",
    )


def test_default_policy_requires_approval_for_every_action_type():
    """Nothing should be pre-configured to auto-send out of the box."""
    default = policy.get_policy()
    for action_type in policy.ACTION_TYPES:
        assert default[action_type]["auto_approve"] is False
        assert default[action_type]["max_value"] is None


def test_should_auto_approve_is_false_with_no_policy_configured():
    lead = _lead()
    for action_type in policy.ACTION_TYPES:
        assert policy.should_auto_approve(lead["id"], action_type) is False


def test_should_auto_approve_true_when_explicitly_enabled_with_no_cap():
    lead = _lead()
    pol = policy.get_policy()
    pol["nudge"] = {"auto_approve": True, "max_value": None}
    policy.set_policy(pol)

    assert policy.should_auto_approve(lead["id"], "nudge") is True
    assert policy.should_auto_approve(lead["id"], "initial") is False  # untouched, still safe


def test_should_auto_approve_respects_value_cap_under_limit():
    lead = _lead()
    create_opportunity(lead["id"], value=500)
    pol = policy.get_policy()
    pol["followup"] = {"auto_approve": True, "max_value": 1000}
    policy.set_policy(pol)

    assert policy.should_auto_approve(lead["id"], "followup") is True


def test_should_auto_approve_blocks_over_value_cap():
    lead = _lead()
    create_opportunity(lead["id"], value=50000)
    pol = policy.get_policy()
    pol["followup"] = {"auto_approve": True, "max_value": 1000}
    policy.set_policy(pol)

    assert policy.should_auto_approve(lead["id"], "followup") is False


def test_should_auto_approve_treats_missing_opportunity_as_zero_value():
    """A lead with no tracked opportunity shouldn't be blocked by a value
    cap just because we have no evidence it's high-value."""
    lead = _lead()  # no opportunity created
    pol = policy.get_policy()
    pol["meeting_confirmation"] = {"auto_approve": True, "max_value": 100}
    policy.set_policy(pol)

    assert policy.should_auto_approve(lead["id"], "meeting_confirmation") is True


def test_policy_persists_and_leaves_other_action_types_at_safe_defaults():
    pol = policy.get_policy()
    pol["initial"] = {"auto_approve": True, "max_value": 250}
    policy.set_policy(pol)

    reloaded = policy.get_policy()
    assert reloaded["initial"] == {"auto_approve": True, "max_value": 250}
    assert reloaded["nudge"] == {"auto_approve": False, "max_value": None}