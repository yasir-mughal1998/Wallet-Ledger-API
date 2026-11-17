"""Input rules from the brief, checked before any database access."""

import json
import re

from app.errors import invalid_request

MAX_AMOUNT = 1_000_000_000

_USER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Printable ASCII without spaces, 1 to 255 characters.
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")


def validate_user_id(value: object) -> str:
    # fullmatch, not match with "$": "$" would also accept a trailing newline.
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise invalid_request("user_id must be 1 to 64 characters of A-Z, a-z, 0-9, _ or -")
    return value


def validate_amount(value: object) -> int:
    # type(...) is int, not isinstance: in Python True/False are ints too, and
    # the JSON parser turns 10.0 and 1e3 into float, so those are rejected here.
    if type(value) is not int or not 1 <= value <= MAX_AMOUNT:
        raise invalid_request(f"amount must be a JSON integer between 1 and {MAX_AMOUNT}")
    return value


def parse_operation(body: bytes) -> tuple[str, int]:
    """Parse and validate the body of POST /credit and POST /debit."""
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON, invalid UTF-8 and absurdly long
        # integer literals; RecursionError covers pathologically deep nesting.
        raise invalid_request("request body must be valid JSON") from None

    if not isinstance(payload, dict):
        raise invalid_request("request body must be a JSON object")

    # Unknown fields are ignored; a missing field reads as None and fails below.
    user_id = validate_user_id(payload.get("user_id"))
    amount = validate_amount(payload.get("amount"))
    return user_id, amount


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise invalid_request("Idempotency-Key must be 1 to 255 printable ASCII characters")
    return value
