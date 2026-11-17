"""Unit tests that need neither the API nor the database."""

import asyncio

import pytest

from app.errors import ApiError
from app.locks import KeyedLock
from app.validation import parse_operation, validate_idempotency_key, validate_user_id


def test_parse_operation_accepts_valid_body():
    assert parse_operation(b'{"user_id": "u-1_A", "amount": 10000, "extra": {}}') == ("u-1_A", 10000)


@pytest.mark.parametrize("amount", ["true", "false", "1.0", "1e2", '"5"', "0", "-1", "1000000001", "null"])
def test_parse_operation_rejects_bad_amounts(amount):
    with pytest.raises(ApiError) as error:
        parse_operation(b'{"user_id": "u1", "amount": ' + amount.encode() + b"}")
    assert (error.value.status, error.value.code) == (400, "invalid_request")


@pytest.mark.parametrize("user_id", ["", "a" * 65, "a b", "a\n", "é", None, 5, ["a"]])
def test_validate_user_id_rejects(user_id):
    with pytest.raises(ApiError):
        validate_user_id(user_id)


def test_validate_idempotency_key():
    assert validate_idempotency_key(None) is None
    assert validate_idempotency_key("abc-123") == "abc-123"
    for bad in ["", "has space", "x" * 256, "tab\t"]:
        with pytest.raises(ApiError):
            validate_idempotency_key(bad)


def test_keyed_lock_serialises_one_key_but_not_others():
    async def scenario():
        locks = KeyedLock()
        running = {"a": 0, "b": 0}
        most_at_once = {"a": 0, "b": 0}
        both_keys_overlapped = False

        async def work(key):
            nonlocal both_keys_overlapped
            async with locks.hold(key):
                running[key] += 1
                most_at_once[key] = max(most_at_once[key], running[key])
                if running["a"] and running["b"]:
                    both_keys_overlapped = True
                await asyncio.sleep(0.01)
                running[key] -= 1

        await asyncio.gather(*(work(key) for key in "ab" * 10))
        return locks, most_at_once, both_keys_overlapped

    locks, most_at_once, both_keys_overlapped = asyncio.run(scenario())
    assert most_at_once == {"a": 1, "b": 1}  # never two holders of the same key
    assert both_keys_overlapped  # different keys ran at the same time
    assert len(locks) == 0  # entries are removed once nobody uses them


def test_keyed_lock_is_released_when_the_holder_fails():
    async def scenario():
        locks = KeyedLock()
        with pytest.raises(RuntimeError):
            async with locks.hold("a"):
                raise RuntimeError("boom")
        async with locks.hold("a"):  # would hang here if the lock leaked
            return len(locks)

    assert asyncio.run(scenario()) == 1
