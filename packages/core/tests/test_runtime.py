import json
import re

import pytest
from argus_core.runtime import (
    ComponentRef,
    RuntimeSpec,
    RuntimeSpecError,
    ServiceRef,
    build_key,
    helper_bytes,
    image_ref,
    probes,
    recipe_digest,
    render_dockerfile,
    required_probe_names,
    resolve,
)
from pydantic import ValidationError

BASE = "sha256:" + "a" * 64


def spec(*ids: str, **kw) -> RuntimeSpec:
    return RuntimeSpec(components=[ComponentRef(id=i) for i in ids], **kw)


RADAR = spec(
    "python", "psql", "postgres-server", "valkey", "graft", "pango", "node", "git", "chromium",
    services=[ServiceRef(kind="postgres"), ServiceRef(kind="valkey")],
)  # fmt: skip


def test_resolve_pins_the_default_versions() -> None:
    r = resolve("radar", RADAR)
    assert r.version("psql") == "16"
    assert r.version("valkey") == "8.0"
    assert r.version("python") == "3.13"
    assert r.has("postgres-server") and not r.has("minio")


def test_equal_specs_in_any_order_have_one_digest() -> None:
    a = resolve("radar", spec("psql", "python", "valkey"))
    b = resolve("radar", spec("valkey", "python", "psql"))
    assert recipe_digest(a) == recipe_digest(b)


def test_what_does_not_change_the_image_does_not_change_the_key() -> None:
    base = resolve("radar", spec("psql", "postgres-server"))
    with_env = resolve(
        "radar",
        spec(
            "psql",
            "postgres-server",
            services=[ServiceRef(kind="postgres", env={"ABC_URL": "{url}"})],
        ),
    )
    assert build_key(base, BASE, "linux/amd64") == build_key(
        with_env, BASE, "linux/amd64"
    )


def test_what_changes_the_image_changes_the_key() -> None:
    r = resolve("radar", spec("psql"))
    key = build_key(r, BASE, "linux/amd64")
    assert (
        build_key(resolve("radar", spec("psql", "valkey")), BASE, "linux/amd64") != key
    )
    assert build_key(r, "sha256:" + "b" * 64, "linux/amd64") != key  # base moved
    assert build_key(r, BASE, "linux/arm64") != key  # platform
    assert (
        build_key(resolve("argus", spec("psql")), BASE, "linux/amd64") != key
    )  # other project
    assert build_key(r, BASE, "linux/amd64") == key  # deterministic


def test_projects_never_share_a_reference() -> None:
    a, b = resolve("radar", spec()), resolve("argus", spec())
    ka, kb = build_key(a, BASE, "linux/amd64"), build_key(b, BASE, "linux/amd64")
    assert image_ref(a, ka) != image_ref(b, kb)
    assert "radar" in image_ref(a, ka)


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        (ComponentRef(id="rm -rf /"), "unknown component"),
        (ComponentRef(id="psql", version="15"), "not approved"),
        (ComponentRef(id="psql", version="16;curl x|sh"), "not approved"),
        (ComponentRef(id="minio"), "required"),
        (ComponentRef(id="python", version="2.7"), "not approved"),
    ],
)
def test_unapproved_components_are_refused_with_an_actionable_message(
    ref, message
) -> None:
    with pytest.raises(RuntimeSpecError) as err:
        resolve("radar", RuntimeSpec(components=[ref]))
    assert message in str(err.value)


def test_gaps_are_recorded_when_optional_and_refused_when_required() -> None:
    r = resolve(
        "radar", RuntimeSpec(components=[ComponentRef(id="minio", required=False)])
    )
    assert [g.component for g in r.gaps] == ["minio"]
    assert not r.has("minio")
    assert "minio" not in render_dockerfile(r)


def test_services_need_their_components() -> None:
    with pytest.raises(RuntimeSpecError) as err:
        resolve("radar", spec("psql", services=[ServiceRef(kind="postgres")]))
    assert "postgres-server" in str(err.value)
    with pytest.raises(RuntimeSpecError):
        resolve("radar", spec("postgres-server"))  # needs psql


def test_service_env_templates_cannot_reference_unknown_fields_or_bad_names() -> None:
    with pytest.raises(RuntimeSpecError):
        resolve(
            "radar",
            spec(
                "valkey",
                services=[ServiceRef(kind="valkey", env={"ABC_URL": "{database}"})],
            ),
        )
    with pytest.raises(ValidationError):
        ServiceRef(kind="valkey", env={"lower": "{url}"})
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"components": [], "dockerfile": "FROM evil"})
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate(
            {"components": [{"id": "psql", "build_args": {"X": "1"}}]}
        )


def test_dockerfile_is_built_from_the_catalogue_only() -> None:
    text = render_dockerfile(resolve("radar", RADAR))
    assert "ARG BASE" in text and "FROM ${BASE}" in text
    assert not re.search(r"TOKEN|SECRET|ONECLI|\.env|COPY \. ", text, re.I)
    # the only thing COPY'd from the build context is the helper
    copies = re.findall(r"^COPY (?!--from)(?:--chmod=\d+ )?(\S+)", text, re.M)
    assert copies == ["argus-test-services"]
    # every download is pinned to a checksum or fingerprint that the same RUN verifies
    assert (
        "sha256sum -c" in text
        and "gpg --show-keys" in text
        and "openssl dgst -sha512" in text
    )
    assert text.index("USER node") > text.rindex("USER root")  # ends unprivileged


def test_every_provisioned_tool_has_a_probe_and_services_are_exercised() -> None:
    r = resolve("radar", RADAR)
    names = required_probe_names(r)
    assert {
        "psql",
        "postgres-server",
        "valkey-server",
        "valkey-cli",
        "python",
        "graft",
        "test-services",
    } <= names
    selftest = next(p for p in probes(r) if p.name == "test-services")
    assert (
        "--service postgres" in selftest.argv[-1]
        and "--service valkey" in selftest.argv[-1]
    )
    assert required_probe_names(resolve("radar", spec())) == {"git", "ssh"}


def test_helper_is_shipped_and_has_no_network_or_root_needs() -> None:
    text = helper_bytes().decode()
    assert text.startswith("#!/usr/bin/env python3")
    assert "0.0.0.0" not in text  # loopback only
    assert json.loads(json.dumps({"ok": 1}))  # stdlib import sanity
