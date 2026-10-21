import time

import pytest
from argus_core.redact import REDACTED, redact

SECRETS = {
    "authorization header": "Authorization: Bearer abcdef0123456789",
    "authorization token scheme": "authorization: token ghp_abcdefghijklmnopqrstuv",
    "bearer": "curl -H 'x: Bearer abcdef0123456789.xyz'",
    "openai key": "key=sk-proj-abcdefghijklmnopqrstuvwx",
    "anthropic key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "github ghp": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github gho": "gho_abcdefghijklmnopqrstuvwxyz0123456789",
    "github pat": "github_pat_11ABCDEFG0123456789_abcdefghij",
    "gitlab": "glpat-abcdefghijklmnopqrstu",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "token env": "GITEA_TOKEN=s3cr3tvalue",
    "password colon": "password: hunter2",
    "password json": '{"db_password": "hunter2 with space"}',
    "api key": "API_KEY = abc123",
    "flag": "tool --token abc123def",
    "url password": "git clone https://bot:hunter2@gitea.local/x.git",
}


@pytest.mark.parametrize("shape", SECRETS)
def test_secret_shapes_are_masked(shape: str) -> None:
    text = SECRETS[shape]
    out = redact(f"before {text} after")
    assert REDACTED in out
    for word in ("hunter2", "abc123", "s3cr3t", "abcdefghijklmnopqrstuv", "EXAMPLE"):
        assert word not in out
    assert out.startswith("before ")


def test_private_key_block_is_masked_across_lines() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    assert redact(f"a\n{pem}\nb") == "a\n[redacted private key]\nb"
    assert redact("-----BEGIN PRIVATE KEY-----\nMIIE\nrest") == "[redacted private key]"


def test_exact_secrets_are_masked_but_short_ones_are_not() -> None:
    assert redact("my tok-9f8e7d6c5b ok", ("tok-9f8e7d6c5b",)) == "my [redacted] ok"
    assert redact("a cat", ("cat",)) == "a cat"


def test_ordinary_text_and_counters_survive() -> None:
    line = "ran 12 tests; max_tokens=2000 input_tokens: 5 key points listed"
    assert redact(line) == line


def test_redaction_is_idempotent() -> None:
    once = redact("API_TOKEN=abc Authorization: Bearer abcdefgh12345")
    assert redact(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "a." * 32_000,
        "a-" * 32_000,
        "--" + "a-" * 32_000,
        "a" * 64_000 + "://",
        "x://a:" * 10_000,
        "x://a:" + "b" * 64_000,
        "-----BEGIN " + "A " * 32_000,
        "-----BEGIN PRIVATE KEY-----" * 2_000,
        "token=" * 10_000,
        "bearer " * 9_000,
        "eyJ" + "a" * 64_000,
        "authorization:" + " " * 64_000,
    ],
)
def test_worst_case_input_stays_linear(text: str) -> None:
    started = time.perf_counter()
    redact(text, ("exact-secret-value",))
    assert time.perf_counter() - started < 1.0


def test_bounded_names_still_mask_ordinary_long_names() -> None:
    name = "MY_" + "X" * 50 + "_TOKEN"
    assert redact(f"{name}=s3cr3tvalue") == f"{name}=[redacted]"
    assert redact("tool --gitea-access-token s3cr3tvalue") == (
        "tool --gitea-access-token [redacted]"
    )
