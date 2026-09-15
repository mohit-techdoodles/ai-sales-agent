"""
policy.py
V3 — Autonomy controls (the "Policy Engine" from the blueprint's V3
architecture: Safe -> execute, Risky -> human approval).

Safe default: every action type requires human approval unless staff
explicitly configures otherwise — matches the blueprint's own guardrail
("require human approval for ... high-value opportunities" etc.) by
defaulting to the MORE conservative behavior, not less.

Rules are keyed by action type (matching draft.py's modes) and can
optionally cap auto-approval to deals below a certain value — a $50k
opportunity should probably always get human eyes, even if low-value
nudges are fine to automate.
"""

import json

from settings import get_setting, set_setting
from opportunities import get_opportunity_for_lead

ACTION_TYPES = ["initial", "followup", "nudge", "meeting_confirmation"]

POLICY_SETTING_KEY = "autonomy_policy"


def _default_policy() -> dict:
    return {action_type: {"auto_approve": False, "max_value": None} for action_type in ACTION_TYPES}


def get_policy() -> dict:
    """Loads the current policy, filling in safe defaults for any action type not yet configured."""
    raw = get_setting(POLICY_SETTING_KEY, "")
    policy = _default_policy()
    if raw:
        try:
            saved = json.loads(raw)
            policy.update(saved)
        except json.JSONDecodeError:
            pass  # corrupted setting — fall back to safe defaults rather than crash
    return policy


def set_policy(policy: dict) -> None:
    set_setting(POLICY_SETTING_KEY, json.dumps(policy))


def should_auto_approve(lead_id: int, action_type: str) -> bool:
    """
    True only if: this action type is explicitly enabled for auto-approval,
    AND (no value cap is set, OR the lead's opportunity value is under the cap).
    A lead with no tracked opportunity is treated as $0 (i.e. passes any cap),
    since we have no evidence it's high-value.
    """
    policy = get_policy()
    rule = policy.get(action_type, {"auto_approve": False, "max_value": None})

    if not rule.get("auto_approve"):
        return False

    max_value = rule.get("max_value")
    if max_value is not None:
        opportunity = get_opportunity_for_lead(lead_id)
        deal_value = (opportunity.get("value") if opportunity else 0) or 0
        if deal_value > max_value:
            return False

    return True