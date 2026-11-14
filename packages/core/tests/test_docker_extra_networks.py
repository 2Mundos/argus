"""DockerOptions.extra_networks validation (worker-launch spec)."""

import pytest
from argus_core.fleet import DockerOptions, PoolUpsert
from pydantic import ValidationError


def test_extra_networks_default_empty_and_are_stripped() -> None:
    assert DockerOptions().extra_networks == []
    assert DockerOptions(extra_networks=[" bridge "]).extra_networks == ["bridge"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"extra_networks": ["bridge", "  "]}, "must not be empty"),
        ({"extra_networks": ["bridge", "bridge"]}, "must not repeat a network"),
        ({"network": "onecli", "extra_networks": ["onecli"]}, "repeat docker.network"),
    ],
)
def test_invalid_extra_networks_are_rejected(options: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DockerOptions(**options)


def test_extra_networks_round_trip_through_the_pool_upsert() -> None:
    upsert = PoolUpsert(
        image="img:1",
        max_replicas=1,
        docker=DockerOptions(network="onecli", extra_networks=["bridge"]),
    )
    again = PoolUpsert.model_validate_json(upsert.model_dump_json())
    assert again.docker.extra_networks == ["bridge"]
