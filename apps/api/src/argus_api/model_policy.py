"""Preferred models: read and edit the model policy (docs/specs/model-selection.spec.md).

Validation and the revision mirror integrations/nanoclaw/engines/model-policy.mjs;
integrations/nanoclaw/engines/model-policy.fixture.json pins both to the same revisions.
"""

import hashlib
import json
import os
import re
import tempfile
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
# Optional (task complexity spec): `trivial` replaces `implementation` for trivial
# tasks and local leases; `hard_harness` / `hard_implementation` replace `harness` /
# `implementation` for hard tasks, per engine. Absent from a policy, the revision is
# what it was without them. Order is canonical (mirrors model-policy.mjs).
OPTIONAL_SCOPES = ("trivial", "hard_harness", "hard_implementation")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
DEFAULTS_REL = "integrations/nanoclaw/engines/model-policy.defaults.json"

Policy = dict[str, dict[str, list[str]]]


def validate(raw: Any) -> Policy:
    if (
        not isinstance(raw, dict)
        or not set(SCOPES) <= raw.keys()
        or not raw.keys() <= {*SCOPES, *OPTIONAL_SCOPES}
    ):
        raise ValidationFailedError(
            "model policy requires harness and implementation maps"
            " and allows only optional trivial, hard_harness and hard_implementation ones"
        )
    policy: Policy = {}
    for scope in (*SCOPES, *(s for s in OPTIONAL_SCOPES if s in raw)):
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
    trivial: dict[str, list[str]] | None = None
    hard_harness: dict[str, list[str]] | None = None
    hard_implementation: dict[str, list[str]] | None = None
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
    trivial: Any = None
    hard_harness: Any = None
    hard_implementation: Any = None


def _parse(text: str, what: str) -> Policy:
    try:
        return validate(json.loads(text))
    except ValueError as e:
        raise ValidationFailedError(f"{what} is not valid JSON: {e}") from e


def _parse_file(path: Path, what: str) -> Policy:
    """Parse a policy file; an unreadable one is the same named error as a corrupt one."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise ValidationFailedError(f"{what} is not readable: {e}") from e
    return _parse(text, what)


def _defaults_path() -> Path:
    """ARGUS_MODEL_DEFAULTS_FILE, else the repository copy (also in the image)."""
    if override := os.environ.get("ARGUS_MODEL_DEFAULTS_FILE"):
        return Path(override)
    parents = Path(__file__).resolve().parents
    return (parents[4] if len(parents) > 4 else Path()) / DEFAULTS_REL


def _env() -> Policy | None:
    env = os.environ.get("ARGUS_MODEL_POLICY")
    return _parse(env, "ARGUS_MODEL_POLICY") if env else None


def _read(path: Path) -> ModelPolicy:
    env = _env()
    file: Policy | None = None
    if env is not None:
        # The file is informational here: a bad one must not break the effective read.
        try:
            file = _parse_file(path, path.name) if path.exists() else None
        except ValidationFailedError:
            file = None
        policy, source = env, "env"
    else:
        file = _parse_file(path, path.name) if path.exists() else None
        if file is not None:
            policy, source = file, "file"
        else:
            defaults = _defaults_path()
            try:
                policy = _parse_file(defaults, defaults.name)
            except ValidationFailedError as e:
                raise ValidationFailedError(
                    f"model policy defaults unavailable ({e}); "
                    "set ARGUS_MODEL_DEFAULTS_FILE"
                ) from e
            source = "defaults"
    return ModelPolicy(
        **policy,
        revision=revision(policy),
        source=source,  # type: ignore[arg-type]
        path=str(path),
        env_overrides=env is not None,
        file_revision=revision(file) if file is not None else None,
        notice=(
            "ARGUS_MODEL_POLICY is set and wins over the file; edits are saved but "
            "have no effect until it is removed."
            if env is not None
            else None
        ),
    )


def effective_env(path: Path) -> tuple[str, str]:
    """(canonical JSON, revision) of the policy `GET /api/models` reports, for the
    ARGUS_MODEL_POLICY of a new instance. Raises ValidationFailedError if it is invalid."""
    policy = _read(path)
    plain: Policy = {"harness": policy.harness, "implementation": policy.implementation}
    for scope in OPTIONAL_SCOPES:
        if (chains := getattr(policy, scope)) is not None:
            plain[scope] = chains
    return canonical(plain), policy.revision


def _write(path: Path, policy: Policy) -> None:
    """Atomic: unique temp file in the same directory, fsync, then rename over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(policy, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
    given = body.model_dump()
    for scope in OPTIONAL_SCOPES:
        if given[scope] is None:
            del given[scope]  # null and absent both mean no such map
    policy = validate(given)
    _env()  # a malformed ARGUS_MODEL_POLICY fails before the file changes
    _write(settings.model_policy_path, policy)
    result = _read(settings.model_policy_path)
    await _audit(db, caller.actor, "saved", result)
    return result


@router.delete("/models")
async def restore_models(
    settings: SettingsDep, db: DbDep, caller: AdminDep
) -> ModelPolicy:
    """Remove the file so the repository defaults apply again."""
    _env()  # fail before the file is removed
    settings.model_policy_path.unlink(missing_ok=True)
    result = _read(settings.model_policy_path)
    await _audit(db, caller.actor, "restored", result)
    return result
