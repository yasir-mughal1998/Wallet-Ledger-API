"""API contract: happy paths, validation, status codes and authentication."""

import pytest

from tests.conftest import ledger_balance, ledger_rows


def test_credit_debit_balance_flow(client, user_id):
    r = client.post("/credit", json={"user_id": user_id, "amount": 10000})
    assert (r.status_code, r.json()) == (200, {"user_id": user_id, "balance": 10000})

    r = client.post("/credit", json={"user_id": user_id, "amount": 15000})
    assert (r.status_code, r.json()) == (200, {"user_id": user_id, "balance": 25000})

    r = client.post("/debit", json={"user_id": user_id, "amount": 4000})
    assert (r.status_code, r.json()) == (200, {"user_id": user_id, "balance": 21000})

    r = client.get(f"/balance/{user_id}")
    assert (r.status_code, r.json()) == (200, {"user_id": user_id, "balance": 21000})
    assert r.headers["content-type"] == "application/json"

    assert ledger_rows(user_id) == 3
    assert ledger_balance(user_id) == 21000


def test_debit_whole_balance_reaches_zero(client, user_id):
    client.post("/credit", json={"user_id": user_id, "amount": 500})
    r = client.post("/debit", json={"user_id": user_id, "amount": 500})
    assert (r.status_code, r.json()["balance"]) == (200, 0)
    assert client.get(f"/balance/{user_id}").json()["balance"] == 0


def test_insufficient_funds_writes_nothing(client, user_id):
    client.post("/credit", json={"user_id": user_id, "amount": 100})
    r = client.post("/debit", json={"user_id": user_id, "amount": 101})
    assert r.status_code == 409
    assert r.json()["error"] == "insufficient_funds"
    assert isinstance(r.json()["message"], str)
    assert client.get(f"/balance/{user_id}").json()["balance"] == 100
    assert ledger_rows(user_id) == 1


def test_unknown_user_is_404(client, user_id):
    r = client.get(f"/balance/{user_id}")
    assert (r.status_code, r.json()["error"]) == (404, "user_not_found")

    r = client.post("/debit", json={"user_id": user_id, "amount": 1})
    assert (r.status_code, r.json()["error"]) == (404, "user_not_found")
    assert ledger_rows(user_id) == 0
    # A rejected debit must not create the user.
    assert client.get(f"/balance/{user_id}").status_code == 404


def test_unknown_fields_are_ignored(client, user_id):
    r = client.post("/credit", json={"user_id": user_id, "amount": 5, "note": "hi", "balance": 9})
    assert (r.status_code, r.json()["balance"]) == (200, 5)


def test_amount_bounds(client, user_id):
    assert client.post("/credit", json={"user_id": user_id, "amount": 1}).status_code == 200
    r = client.post("/credit", json={"user_id": user_id, "amount": 1_000_000_000})
    assert (r.status_code, r.json()["balance"]) == (200, 1_000_000_001)
    assert client.post("/credit", json={"user_id": user_id, "amount": 1_000_000_001}).status_code == 400


def test_user_id_bounds(client):
    import uuid

    longest = ("L-_" + uuid.uuid4().hex * 2)[:64]
    assert len(longest) == 64
    assert client.post("/credit", json={"user_id": longest, "amount": 1}).status_code == 200
    assert client.get(f"/balance/{longest}").status_code == 200


BAD_BODIES = [
    pytest.param(b"", id="empty body"),
    pytest.param(b"{not json", id="malformed json"),
    pytest.param(b"\xff\xfe\x00", id="invalid utf-8"),
    pytest.param(b"[]", id="array instead of object"),
    pytest.param(b'"text"', id="string instead of object"),
    pytest.param(b"null", id="null"),
    pytest.param(b'{"amount": 100}', id="missing user_id"),
    pytest.param(b'{"user_id": "u1"}', id="missing amount"),
    pytest.param(b'{"user_id": null, "amount": 100}', id="null user_id"),
    pytest.param(b'{"user_id": 123, "amount": 100}', id="numeric user_id"),
    pytest.param(b'{"user_id": "", "amount": 100}', id="empty user_id"),
    pytest.param(b'{"user_id": "' + b"a" * 65 + b'", "amount": 100}', id="65-char user_id"),
    pytest.param(b'{"user_id": "has space", "amount": 100}', id="space in user_id"),
    pytest.param(b'{"user_id": "a.b", "amount": 100}', id="dot in user_id"),
    pytest.param(b'{"user_id": "u1\\n", "amount": 100}', id="trailing newline in user_id"),
    pytest.param('{"user_id": "üser", "amount": 100}'.encode(), id="non-ascii user_id"),
    pytest.param(b'{"user_id": "u1", "amount": null}', id="null amount"),
    pytest.param(b'{"user_id": "u1", "amount": 0}', id="zero"),
    pytest.param(b'{"user_id": "u1", "amount": -5}', id="negative"),
    pytest.param(b'{"user_id": "u1", "amount": 10.5}', id="decimal"),
    pytest.param(b'{"user_id": "u1", "amount": 10.0}', id="float with zero fraction"),
    pytest.param(b'{"user_id": "u1", "amount": 1e3}', id="exponent notation"),
    pytest.param(b'{"user_id": "u1", "amount": "100"}', id="string amount"),
    pytest.param(b'{"user_id": "u1", "amount": true}', id="boolean amount"),
    pytest.param(b'{"user_id": "u1", "amount": [100]}', id="array amount"),
    pytest.param(b'{"user_id": "u1", "amount": 1000000001}', id="above maximum"),
    pytest.param(b'{"user_id": "u1", "amount": ' + b"9" * 5000 + b"}", id="huge integer"),
]


@pytest.mark.parametrize("path", ["/credit", "/debit"])
@pytest.mark.parametrize("body", BAD_BODIES)
def test_invalid_request_body(client, path, body):
    r = client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
    assert isinstance(r.json()["message"], str)


@pytest.mark.parametrize("bad_id", ["a" * 65, "a.b", "a%20b", "a@b"])
def test_balance_with_invalid_user_id(client, bad_id):
    r = client.get(f"/balance/{bad_id}")
    assert (r.status_code, r.json()["error"]) == (400, "invalid_request")


ENDPOINTS = [
    ("POST", "/credit"),
    ("POST", "/debit"),
    ("GET", "/balance/u1"),
    ("GET", "/health"),
    ("GET", "/no-such-path"),
]
BAD_AUTH = [
    pytest.param(None, id="missing header"),
    pytest.param("", id="empty header"),
    pytest.param("Bearer", id="no token"),
    pytest.param("Bearer wrong-token", id="wrong token"),
    pytest.param("Bearer recruitment-test-toke", id="token prefix"),
    pytest.param("Basic {token}", id="wrong scheme"),
    pytest.param("Token {token}", id="other scheme"),
    pytest.param("{token}", id="token without scheme"),
    pytest.param("Bearer  {token}", id="two spaces"),
    pytest.param("Bearer {token} extra", id="trailing text"),
]


@pytest.mark.parametrize("method,path", ENDPOINTS)
@pytest.mark.parametrize("header", BAD_AUTH)
def test_unauthorized(anonymous, method, path, header):
    from tests.conftest import API_TOKEN

    headers = {} if header is None else {"Authorization": header.format(token=API_TOKEN)}
    # A syntactically invalid body: auth must be rejected before validation.
    r = anonymous.request(method, path, headers=headers, content=b"{not json")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert API_TOKEN not in r.text


def test_unauthorized_request_does_not_write(anonymous, client, user_id):
    r = anonymous.post("/credit", json={"user_id": user_id, "amount": 100})
    assert r.status_code == 401
    assert client.get(f"/balance/{user_id}").status_code == 404


def test_health(client):
    r = client.get("/health")
    assert (r.status_code, r.json()) == (200, {"status": "ok"})


def test_unknown_path_and_method(client):
    r = client.get("/no-such-path")
    assert (r.status_code, r.json()["error"]) == (404, "not_found")
    r = client.get("/credit")
    assert (r.status_code, r.json()["error"]) == (405, "method_not_allowed")
