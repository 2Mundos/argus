"""NanoclawOptions.onecli_url allowlist (security-hardening spec)."""

import pytest
from argus_core.fleet import NanoclawOptions, onecli_url_ok
from pydantic import ValidationError

_BASE = {
    "install_dir": "/opt/nc",
    "instances_root": "/srv/i",
    "agent_image": "agent:1",
}


@pytest.mark.parametrize(
    "url",
    [
        "http://host.docker.internal:10254",
        "http://127.0.0.1:10254",
        "http://localhost:10254",
        "https://onecli.example.com",
        "https://onecli.example.com:8443/path",
    ],
)
def test_onecli_url_accepted(url: str) -> None:
    assert onecli_url_ok(url)
    assert NanoclawOptions(**_BASE, onecli_url=url).onecli_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://onecli:10254",
        "http://evil.example/x",
        "https://user:pass@onecli.example.com",
        "http://user@127.0.0.1:10254",
        "ftp://host.docker.internal:10254",
    ],
)
def test_onecli_url_rejected(url: str) -> None:
    assert not onecli_url_ok(url)
    with pytest.raises(ValidationError, match=r"onecli_url"):
        NanoclawOptions(**_BASE, onecli_url=url)
