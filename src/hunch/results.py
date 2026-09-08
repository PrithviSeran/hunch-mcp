"""Additive detailed receipts; transport acceptance never implies a verified effect."""
from .errors import HunchError

FIELDS = {"value", "title", "enabled", "selected", "expanded"}


def validate_postcondition(condition):
    if condition is None:
        return
    if (not isinstance(condition, dict) or set(condition) != {"ref", "field", "equals"}
            or not isinstance(condition["ref"], str) or not condition["ref"]
            or not isinstance(condition["field"], str)
            or condition["field"] not in FIELDS
            or not isinstance(condition["equals"], (str, int, float, bool, type(None)))):
        raise HunchError("postcondition requires ref, field (value/title/enabled/selected/expanded), and scalar equals")


def action_receipt(messages, requested, mechanism, target, disturbances, condition=None, read=None):
    # Compatibility backends return text. Be conservative: only an explicit readback
    # below may produce verified, never an AX/CDP dispatch success string.
    blocked = any(any(word in message.lower() for word in (
        "refused", "skipped", "stale", "error on", "unknown action", "not selectable", "drag needs"))
                  for message in messages)
    result = {"status": "blocked" if blocked else "performed_unverified",
              "mechanism": mechanism, "target": target, "disturbances": disturbances,
              "requested_actions": requested, "attempted_actions": len(messages),
              "unattempted_actions": max(0, requested - len(messages)), "actions": messages}
    if condition is not None and not blocked:
        try:
            observed = read(condition["ref"], condition["field"])
            matched = observed == condition["equals"]
            result["postcondition"] = {"matched": matched, "ref": condition["ref"], "field": condition["field"]}
            if matched and len(messages) == requested:
                result["status"] = "verified"
                result["verification_scope"] = "requested final field value; not each intermediate action"
        except Exception as error:
            result["postcondition"] = {"matched": False, "reason": str(error)}
    return result
