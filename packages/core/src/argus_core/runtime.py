"""Project runtime: what a project's tasks need in their agent image, declared
data only (docs/specs/project-runtime.spec.md).

A project declares *component ids* from the approved catalogue below. Nothing a
task or a project can write ever reaches a Dockerfile as text: `render_dockerfile`
is the only author of build instructions, from this catalogue's pinned recipes.
Pure functions, no I/O besides reading the helper asset.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bump when `render_dockerfile`, a probe or the helper changes meaning: every
# derived image is rebuilt (a recipe is data, but its renderer is code).
SCHEMA = 1
IMAGE_REPO = "argus-project-runtime"
PROBE_USER = "1000:1000"  # the NanoClaw agent image's `node` user
SERVICE_KINDS = ("postgres", "valkey")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
SERVICE_FIELDS = {
    "postgres": {"host", "port", "user", "password", "database", "url", "url_asyncpg", "url_psycopg"},
    "valkey": {"host", "port", "password", "url"},
}  # fmt: skip

Kind = Literal["provision", "base", "gap"]
ServiceKind = Literal["postgres", "valkey"]


@dataclass(frozen=True)
class Component:
    id: str
    kind: Kind  # provision: the recipe installs it; base: the agent image must have it; gap: not provisionable
    versions: tuple[str, ...] = ()  # allowed pins; the last is the default
    purpose: str = ""


CATALOG: dict[str, Component] = {
    c.id: c
    for c in (
        Component("python", "provision", ("3.12", "3.13"), "interpreter installed by uv for the node user"),
        Component("pango", "provision", (), "pango/harfbuzz/fonts that weasyprint loads at import"),
        Component("graft", "provision", ("0.21.1",), "codebase navigation CLI, npm integrity pinned"),
        Component("psql", "provision", ("16",), "PostgreSQL client from the PGDG apt repository"),
        Component("postgres-server", "provision", ("16",), "PostgreSQL server binaries, run unprivileged per task"),
        Component("valkey", "provision", ("8.0",), "valkey-server and valkey-cli built from a sha256-pinned source tarball"),
        Component("node", "base", ("22",), "Node.js already in the agent image"),
        Component("git", "base", (), "git already in the agent image"),
        Component("chromium", "base", (), "browser already in the agent image"),
        Component("pnpm", "gap", (), "not provisioned by Argus yet"),
        Component("sops-age", "gap", (), "not provisioned by Argus yet"),
        Component("minio", "gap", (), "no disposable MinIO lifecycle exists yet"),
        Component("elasticsearch", "gap", (), "no disposable Elasticsearch lifecycle exists yet"),
    )
}  # fmt: skip

# Pins an auditor can check against the vendors (verified at build time by the recipe).
VALKEY_VERSION = "8.0.11"
VALKEY_SHA256 = "e4ee4ad51c2e9cb75c5fac8865d34792482a597bf3b136b42b296c9933bd31f8"
PGDG_KEY_FPR = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"
GRAFT_INTEGRITY = "sha512-0CSgoGx+FyzgCkFDhM94pvPnYaTU9jL19BixqMmZ0eyQK618GINPHhQ5Xtto7LcHdNiBGl5RFQcwJob6Mxq4nA=="
APT_PANGO = (
    "libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fonts-dejavu-core shared-mime-info"
)


def helper_bytes() -> bytes:
    return (
        resources.files("argus_core") / "runtime_assets" / "argus-test-services"
    ).read_bytes()


class ComponentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    version: str | None = Field(default=None, max_length=20)
    # False: the project would like it but its tasks can run without; shown as a gap.
    required: bool = True


class ServiceRef(BaseModel):
    """A disposable test service a task's QA contract may start. `env` maps
    variable names to templates over the service's synthetic values."""

    model_config = ConfigDict(extra="forbid")
    kind: ServiceKind
    env: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator("env")
    @classmethod
    def _env(cls, env: dict[str, str]) -> dict[str, str]:
        for name, template in env.items():
            if not ENV_NAME.match(name) or len(template) > 200:
                raise ValueError(f"invalid variable {name!r}")
        return env


class RuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    components: list[ComponentRef] = Field(default_factory=list, max_length=24)
    services: list[ServiceRef] = Field(default_factory=list, max_length=2)
    codex_turn_timeout_s: int | None = Field(default=None, ge=600, le=14400)


class RuntimeSpecError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Resolved:
    id: str
    version: str | None
    kind: Kind
    required: bool


@dataclass(frozen=True)
class Gap:
    component: str
    reason: str


@dataclass(frozen=True)
class Recipe:
    project: str
    components: tuple[Resolved, ...]
    services: tuple[ServiceRef, ...]
    gaps: tuple[Gap, ...]
    codex_turn_timeout_s: int | None

    def has(self, component: str) -> bool:
        return any(c.id == component for c in self.components)

    def version(self, component: str) -> str | None:
        return next((c.version for c in self.components if c.id == component), None)


def resolve(project: str, spec: RuntimeSpec) -> Recipe:
    """The approved recipe for `spec`, or `RuntimeSpecError` naming every problem
    and what to do about it. Equal specs (any order) resolve equal."""
    problems: list[str] = []
    wanted: dict[str, Resolved] = {}
    gaps: list[Gap] = []
    for ref in spec.components:
        comp = CATALOG.get(ref.id)
        if comp is None:
            problems.append(
                f"unknown component {ref.id!r}; approved: {', '.join(sorted(CATALOG))}"
            )
            continue
        if ref.id in wanted:
            problems.append(f"component {ref.id!r} is declared twice")
            continue
        if comp.kind == "gap":
            if ref.required:
                problems.append(
                    f"{ref.id!r} is required but {comp.purpose}; declare it with required=false to "
                    "record the gap, or ask for the component to be added to the catalogue"
                )
            else:
                gaps.append(Gap(ref.id, comp.purpose))
            continue
        version = ref.version or (comp.versions[-1] if comp.versions else None)
        if version is not None and version not in comp.versions:
            problems.append(
                f"{ref.id!r} version {version!r} is not approved; use one of {', '.join(comp.versions) or 'none'}"
            )
            continue
        wanted[ref.id] = Resolved(ref.id, version, comp.kind, ref.required)
    for svc in spec.services:
        need = {"postgres": "postgres-server", "valkey": "valkey"}[svc.kind]
        if need not in wanted:
            problems.append(
                f"service {svc.kind!r} needs component {need!r} in the image; add it"
            )
        for name, template in svc.env.items():
            bad = set(PLACEHOLDER.findall(template)) - SERVICE_FIELDS[svc.kind]
            if bad:
                problems.append(
                    f"{name}: placeholders {sorted(bad)} do not exist for {svc.kind}"
                )
    if "postgres-server" in wanted and "psql" not in wanted:
        problems.append(
            "'postgres-server' needs 'psql' (the client the tasks and probes use); add it"
        )
    if problems:
        raise RuntimeSpecError(problems)
    components = tuple(sorted(wanted.values(), key=lambda c: c.id))
    services = tuple(sorted(spec.services, key=lambda s: s.kind))
    return Recipe(
        project,
        components,
        services,
        tuple(sorted(gaps, key=lambda g: g.component)),
        spec.codex_turn_timeout_s,
    )


def render_dockerfile(recipe: Recipe) -> str:
    """The only author of build instructions. `BASE` is an image id; the build
    context holds the helper alone; no secrets, args or env reach it."""
    out = ["ARG BASE=unset-base-ref"]
    final: list[str] = []
    if recipe.has("valkey"):
        out += [
            "FROM ${BASE} AS valkey-build",
            "USER root",
            "RUN set -eu; apt-get update; "
            "apt-get install -y --no-install-recommends build-essential curl ca-certificates; "
            f"curl -fsSL -o /tmp/valkey.tgz https://github.com/valkey-io/valkey/archive/refs/tags/{VALKEY_VERSION}.tar.gz; "
            f'echo "{VALKEY_SHA256}  /tmp/valkey.tgz" | sha256sum -c -; '
            "mkdir /tmp/src; tar -xzf /tmp/valkey.tgz -C /tmp/src --strip-components=1; "
            'make -C /tmp/src -j"$(nproc)" BUILD_TLS=no; '
            "install -D /tmp/src/src/valkey-server /out/valkey-server; "
            "install -D /tmp/src/src/valkey-cli /out/valkey-cli",
        ]
        final.append(
            "COPY --from=valkey-build /out/valkey-server /out/valkey-cli /usr/local/bin/"
        )
    if recipe.has("graft"):
        out += [
            "FROM ${BASE} AS graft-build",
            "USER root",
            "RUN set -eu; apt-get update; "
            "apt-get install -y --no-install-recommends build-essential python3 make g++; "
            f"cd /tmp; npm pack @nanonets/graft@{recipe.version('graft')}; "
            f'test "sha512-$(openssl dgst -sha512 -binary nanonets-graft-*.tgz | base64 -w0)" = "{GRAFT_INTEGRITY}"; '
            "npm install -g --prefix /out nanonets-graft-*.tgz",
        ]
        final.append(
            "COPY --from=graft-build /out/lib/node_modules/@nanonets/graft /usr/local/lib/graft"
        )
    out += ["FROM ${BASE}", "USER root"]
    apt: list[str] = []
    if recipe.has("pango"):
        apt.append(APT_PANGO)
    if recipe.has("psql") or recipe.has("postgres-server"):
        v = recipe.version("postgres-server") or recipe.version("psql")
        pkgs = [f"postgresql-client-{recipe.version('psql')}"]
        if recipe.has("postgres-server"):
            pkgs.append(f"postgresql-{v}")
        out.append(
            "RUN set -eu; apt-get update; apt-get install -y --no-install-recommends gnupg ca-certificates curl; "
            "curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /tmp/pgdg.asc; "
            f'test "$(gpg --show-keys --with-colons /tmp/pgdg.asc | awk -F: \'/^fpr:/{{print $10; exit}}\')" = "{PGDG_KEY_FPR}"; '
            "gpg --dearmor < /tmp/pgdg.asc > /usr/share/keyrings/pgdg.gpg; rm /tmp/pgdg.asc; "
            '. /etc/os-release; echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list; '
            "mkdir -p /etc/postgresql-common; echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.conf; "
            f"apt-get update; apt-get install -y --no-install-recommends {' '.join(pkgs)}; rm -rf /var/lib/apt/lists/*"
        )
    if apt:
        out.append(
            f"RUN apt-get update && apt-get install -y --no-install-recommends {' '.join(apt)} && rm -rf /var/lib/apt/lists/*"
        )
    out += final
    if recipe.has("graft"):
        out.append(
            "RUN printf '%s\\n' '#!/bin/sh' 'exec node /usr/local/lib/graft/dist/cli.js \"$@\"' > /usr/local/bin/graft "
            "&& chmod 0755 /usr/local/bin/graft && graft --version"
        )
    if recipe.services or recipe.has("postgres-server") or recipe.has("valkey"):
        out.append(
            "COPY --chmod=0755 argus-test-services /usr/local/bin/argus-test-services"
        )
    out.append("USER node")
    if recipe.codex_turn_timeout_s:
        out.append(
            f"ENV ARGUS_CODEX_TURN_TIMEOUT_MS={recipe.codex_turn_timeout_s * 1000}"
        )
    if recipe.has("python"):
        py = recipe.version("python")
        out.append(f"RUN uv python install {py} && uv python find {py}")
    out.append(
        f'LABEL dev.argus.runtime.project="{recipe.project}" dev.argus.runtime.schema="{SCHEMA}"'
    )
    return "\n".join(out) + "\n"


def recipe_digest(recipe: Recipe) -> str:
    """Identity of the recipe's output, independent of the base: the rendered
    Dockerfile (project label included) and the helper it installs. Anything that
    does not change the image (service env mapping, description) is not in it."""
    h = hashlib.sha256()
    h.update(render_dockerfile(recipe).encode())
    h.update(hashlib.sha256(helper_bytes()).digest())
    return h.hexdigest()


def build_key(recipe: Recipe, base_id: str, platform: str) -> str:
    doc = {
        "schema": SCHEMA,
        "recipe": recipe_digest(recipe),
        "base": base_id,
        "platform": platform,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def image_ref(recipe: Recipe, key: str, repo: str = IMAGE_REPO) -> str:
    return f"{repo}:{recipe.project}-{key[:12]}"


@dataclass(frozen=True)
class Probe:
    name: str
    argv: tuple[str, ...]  # run as the entrypoint with these arguments
    pattern: str  # must match the combined output


def _sh(script: str) -> tuple[str, ...]:
    return ("sh", "-c", script)


def probes(recipe: Recipe) -> list[Probe]:
    """Every check that must pass before the image is `ready`. A version string
    alone never proves a service works: services are exercised by `selftest`."""
    p = [
        Probe("git", ("git", "--version"), r"^git version"),
        Probe("ssh", ("ssh", "-V"), r"OpenSSH"),
    ]
    for c in recipe.components:
        match c.id:
            case "python":
                p.append(
                    Probe(
                        "python",
                        _sh(f"$(uv python find {c.version}) --version"),
                        rf"^Python {re.escape(c.version or '')}\.",
                    )
                )
            case "node":
                p.append(Probe("node", ("node", "--version"), rf"^v{c.version}\."))
            case "chromium":
                p.append(
                    Probe(
                        "chromium",
                        _sh(
                            "command -v chromium || command -v chromium-browser || command -v google-chrome"
                        ),
                        r"chrom",
                    )
                )
            case "pango":
                p.append(
                    Probe(
                        "pango",
                        _sh(
                            "python3 -c \"import ctypes; ctypes.CDLL('libpango-1.0.so.0'); print('pango ok')\""
                        ),
                        r"pango ok",
                    )
                )
            case "graft":
                p.append(
                    Probe(
                        "graft",
                        ("graft", "--version"),
                        rf"{re.escape(c.version or '')}",
                    )
                )
            case "psql":
                p.append(
                    Probe(
                        "psql", ("psql", "--version"), rf"\(PostgreSQL\) {c.version}\."
                    )
                )
            case "postgres-server":
                p.append(
                    Probe(
                        "postgres-server",
                        _sh(f"/usr/lib/postgresql/{c.version}/bin/postgres --version"),
                        rf"\(PostgreSQL\) {c.version}\.",
                    )
                )
            case "valkey":
                p.append(
                    Probe(
                        "valkey-server",
                        ("valkey-server", "--version"),
                        rf"Valkey server v={re.escape(VALKEY_VERSION)}",
                    )
                )
                p.append(
                    Probe(
                        "valkey-cli",
                        ("valkey-cli", "--version"),
                        rf"valkey-cli {re.escape(VALKEY_VERSION)}",
                    )
                )
    selftest = [
        k
        for k, need in (("postgres", "postgres-server"), ("valkey", "valkey"))
        if recipe.has(need)
    ]
    if selftest:
        args = " ".join(f"--service {k}" for k in selftest)
        p.append(
            Probe(
                "test-services",
                _sh(f"argus-test-services selftest {args}"),
                r'"probe": "(postgres|valkey)\.cleanup", "ok": true',
            )
        )
    return p


def required_probe_names(recipe: Recipe) -> set[str]:
    return {p.name for p in probes(recipe)}


# -- wire models: the interface between Argus, the worker and the dashboard ----------

State = Literal["ready", "preparing", "building", "verifying", "failed", "disabled"]
IMAGE_REF = r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,199}$"
IMAGE_ID = r"^sha256:[0-9a-f]{64}$"


class PrepareRequest(BaseModel):
    """What the claiming worker knows about itself; the project comes from the task."""

    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    host_id: str = Field(min_length=1, max_length=100)  # the worker's Docker daemon id
    run_as: str = Field(
        pattern=r"^[0-9]{1,9}:[0-9]{1,9}$"
    )  # uid:gid its session containers run as
    base_ref: str = Field(pattern=IMAGE_REF)  # the pool's agent image (CONTAINER_IMAGE)
    base_id: str = Field(pattern=IMAGE_ID)
    platform: str = Field(pattern=r"^[a-z0-9]+/[a-z0-9_]+$")


class ToolEvidence(BaseModel):
    name: str
    ok: bool
    output: str


class ServiceContract(BaseModel):
    kind: ServiceKind
    env: dict[str, str]  # variable -> template; the values are synthetic, per task
    command: str  # how a task uses it without knowing any URL


class GapView(BaseModel):
    component: str
    reason: str


class ComponentView(BaseModel):
    id: str
    version: str | None
    provisioned: bool  # False: the base image must already carry it (probed)
    required: bool


class SelectedImage(BaseModel):
    ref: str
    id: str
    base_ref: str
    base_id: str
    platform: str


class Selection(BaseModel):
    """The project-runtime contract a worker (and the task-continuation front) consumes:
    requirement, selected image, readiness and gaps. `state != ready` means the task
    was held (`task_held`) and nothing was started."""

    state: State
    project: str
    declared: bool
    reason: str | None = None
    build_key: str | None = None
    requirement: list[ComponentView] = Field(default_factory=list)
    image: SelectedImage | None = None
    tools: list[ToolEvidence] = Field(default_factory=list)
    services: list[ServiceContract] = Field(default_factory=list)
    gaps: list[GapView] = Field(default_factory=list)
    task_held: bool = False


class HoldRequest(BaseModel):
    """The worker could not establish the environment (its Docker host, the switch):
    hold the task durably instead of starting it or spending an attempt."""

    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    error: str = Field(min_length=1, max_length=1500)


class UsedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    container: str = Field(min_length=1, max_length=200)
    image_id: str = Field(
        pattern=IMAGE_ID
    )  # what the running session container reports


class UsedResult(BaseModel):
    verified: bool
    selected_image_id: str
    used_image_id: str


class BuildView(BaseModel):
    build_key: str
    host_id: str
    run_as: str
    state: State
    reason: str | None
    image_ref: str
    image_id: str | None
    base_ref: str
    base_id: str
    platform: str
    attempts: int
    evidence: list[ToolEvidence]
    current: bool  # matches the project's present recipe
    updated_at: str


class RuntimeView(BaseModel):
    project: str
    declared: bool
    spec: RuntimeSpec
    problems: list[str]
    requirement: list[ComponentView]
    gaps: list[GapView]
    services: list[ServiceRef]
    builds: list[BuildView]


def contract_services(recipe: Recipe) -> list[ServiceContract]:
    defaults = {
        "postgres": "ARGUS_TEST_POSTGRES_URL",
        "valkey": "ARGUS_TEST_VALKEY_URL",
    }
    return [
        ServiceContract(
            kind=s.kind,
            env=s.env or {defaults[s.kind]: "{url}"},
            command="argus-test-services run -- <command>",
        )
        for s in recipe.services
    ]


def requirement(recipe: Recipe) -> list[ComponentView]:
    return [
        ComponentView(
            id=c.id,
            version=c.version,
            provisioned=c.kind == "provision",
            required=c.required,
        )
        for c in recipe.components
    ]


def declaration(task_id: str, services: list[ServiceContract]) -> dict:
    """What the worker writes for `argus-test-services` before the task starts: the
    exact task, the services its project declared and their variable mapping."""
    return {
        "task_id": task_id,
        "services": [{"kind": s.kind, "env": s.env} for s in services],
    }
