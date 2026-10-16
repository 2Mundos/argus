import base64
import json

import pytest
from argus_core import codex_auth


def jwt(payload) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def doc(access: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {"id_token": "i", "access_token": access, "refresh_token": "r"},
    }


@pytest.mark.parametrize(
    "payload", [[], None, "str", 5, {"exp": "soon"}, {"exp": True}, {}]
)
def test_non_object_or_bad_exp_claims_mean_unknown_expiry(payload) -> None:
    assert codex_auth.validate(doc(jwt(payload))).access_expires_at is None


def raw_jwt(raw: bytes) -> str:
    return "e30." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + ".sig"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"exp":NaN}',
        b'{"exp":Infinity}',
        b'{"exp":1e400}',
        b'{"exp":-1e30}',
        b'{"exp":' + b"9" * 5000 + b"}",
        b"\xff\xfe",
        b"[" * 200_000 + b"]" * 200_000,
        b'{"a":' * 100_000 + b"1" + b"}" * 100_000,
    ],
)
def test_hostile_payloads_mean_unknown_expiry_without_raising(raw: bytes) -> None:
    assert codex_auth.validate(doc(raw_jwt(raw))).access_expires_at is None


@pytest.mark.parametrize("token", ["notajwt", "a.b", "a.!!!.c", ""])
def test_malformed_tokens_do_not_crash(token: str) -> None:
    if token == "":
        with pytest.raises(codex_auth.CodexAuthError):
            codex_auth.validate(doc(token))
    else:
        assert codex_auth.validate(doc(token)).access_expires_at is None


def test_valid_exp_is_read() -> None:
    s = codex_auth.validate(doc(jwt({"exp": 4102444800})))
    assert s.access_expires_at is not None and s.access_expires_at.year == 2100


def test_errors_name_fields_only() -> None:
    with pytest.raises(codex_auth.CodexAuthError) as e:
        codex_auth.validate(
            {"auth_mode": "chatgpt", "tokens": {"access_token": "LEAK"}}
        )
    assert "LEAK" not in str(e.value) and "refresh_token" in str(e.value)
