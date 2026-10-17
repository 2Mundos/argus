"""Preferred models: read and edit the model policy (docs/specs/model-selection.spec.md).

Validation and the revision mirror integrations/nanoclaw/engines/model-policy.mjs;
integrations/nanoclaw/engines/model-policy.fixture.json pins both to the same revisions.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from argus_api import events
from argus_api.deps import AdminDep, CallerDep, DbDep, SettingsDep
from argus_api.errors import ValidationFailedError

router = APIRouter(tags=["models"])

ENGINES = frozenset({"codex", "claude", "opencode", "agent"})
SCOPES = ("harness", "implementation")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
DEFAULTS_PATH = (
    Path(__file__).resolve().parents[4]
    / "integrations/nanoclaw/engines/model-policy.defaults.json"
)

Policy = dict[str, dict[str, list[str]]]


def validate(raw: Any) -> Policy:
    if not isinstance(raw, dict) or sorted(raw) != list(SCOPES):
        raise ValidationFailedError(
            "model policy requires only harness and implementation maps"
        )
    policy: Policy = {}
    for scope in SCOPES:
        if not isinstance(raw[scope], dict):
            raise ValidationFailedError(f"invalid {scope} map")
        policy[scope] = {}
        for name in sorted(raw[scope]):
            chain = raw[scope][name]
            if (
                name not in ENGINES
                or not isinstance(chain, list)
                or not 1 <= len(chain) <= 10
                or not all(
                    isinstance(m, str) and _MODEL.fullmatch(m) is not None
                    for m in chain
                )
                or len(set(chain)) != len(chain)
            ):
                raise ValidationFailedError(f"invalid model chain: {scope}.{name}")
            policy[scope][name] = list(chain)
    return policy


def canonical(policy: Policy) -> str:
    """Sorted engines, scopes in fixed order, no whitespace. Model IDs are ASCII, so
    this equals JS `JSON.stringify` byte for byte."""
    return json.dumps(validate(policy), separators=(",", ":"), ensure_ascii=True)


def revision(policy: Policy) -> str:
    """SHA-256 of the canonical JSON."""
    return hashlib.sha256(canonical(policy).encode()).hexdigest()


class ModelPolicy(BaseModel):
    harness: dict[str, list[str]]
    implementation: dict[str, list[str]]
    revision: str
    source: Literal["env", "file", "defaults"]
    path: str
    # The pool's ARGUS_MODEL_POLICY, when set, wins over the file.
    env_overrides: bool
    # What the file says; null when there is none.
    file_revision: str | None = None
    notice: str | None = None


class ModelPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Shape is checked by `validate`, so a bad chain is named, not a pydantic path.
    harness: Any
    implementation: Any


def _parse(text: str, what: str) -> Policy:
    try:
        return validate(json.loads(text))
    except ValueError as e:
        raise ValidationFailedError(f"{what} is not valid JSON: {e}") from e


def _read(path: Path) -> ModelPolicy:
    env = os.environ.get("ARGUS_MODEL_POLICY")
    file = _parse(path.read_text(), path.name) if path.exists() else None
    if env:
        policy, source = _parse(env, "ARGUS_MODEL_POLICY"), "env"
    elif file is not None:
        policy, source = file, "file"
    else:
        defaults = _parse(DEFAULTS_PATH.read_text(), DEFAULTS_PATH.name)
        policy, source = defaults, "defaults"
    return ModelPolicy(
        **policy,
        revision=revision(policy),
        source=source,  # type: ignore[arg-type]
        path=str(path),
        env_overrides=bool(env),
        file_revision=revision(file) if file is not None else None,
        notice=(
            "ARGUS_MODEL_POLICY is set and wins over the file; edits are saved but "
            "have no effect until it is removed."
            if env
            else None
        ),
    )


def effective_env(path: Path) -> tuple[str, str]:
    """(canonical JSON, revision) of the policy `GET /api/models` reports, for the
    ARGUS_MODEL_POLICY of a new instance. Raises ValidationFailedError if it is invalid."""
    policy = _read(path)
    plain: Policy = {"harness": policy.harness, "implementation": policy.implementation}
    return canonical(plain), policy.revision


def _write(path: Path, policy: Policy) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(policy, indent=2) + "\n")
    tmp.replace(path)


async def _audit(db: DbDep, actor: str, action: str, result: ModelPolicy) -> None:
    async with db.write() as tx:
        await events.record(
            tx,
            actor,
            "models",
            "policy",
            f"models.policy_{action}",
            {"revision": result.revision, "source": result.source},
        )


@router.get("/models")
async def get_models(settings: SettingsDep, _: CallerDep) -> ModelPolicy:
    """The effective policy, its origin and revision. Availability of a model at
    the provider is not checked."""
    return _read(settings.model_policy_path)


@router.put("/models")
async def put_models(
    body: ModelPolicyBody, settings: SettingsDep, db: DbDep, caller: AdminDep
) -> ModelPolicy:
    """Validate and save the file policy, all or nothing."""
    _write(settings.model_policy_path, validate(body.model_dump()))
    result = _read(settings.model_policy_path)
    await _audit(db, caller.actor, "saved", result)
    return result


@router.delete("/models")
async def restore_models(
    settings: SettingsDep, db: DbDep, caller: AdminDep
) -> ModelPolicy:
    """Remove the file so the repository defaults apply again."""
    settings.model_policy_path.unlink(missing_ok=True)
    result = _read(settings.model_policy_path)
    await _audit(db, caller.actor, "restored", result)
    return result
