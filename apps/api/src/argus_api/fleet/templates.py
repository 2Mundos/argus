"""Worker templates: the supported, vetted ways to get a worker (worker launch 3).

A template is a small explicit definition of a supported integration. What it
needs comes from operator configuration of this process (`Settings`) and from
read-only facts about the node; a template is `ready` on a node only when every
prerequisite is verified true. The client never supplies image, runtime, or
NanoClaw options: `build_pool` rebuilds them from the template.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from argus_core.fleet import (
    TEMPLATE_DEFAULT_MAX_REPLICAS,
    Check,
    CreateWorkerRequest,
    DockerOptions,
    ImageChoice,
    NanoclawOptions,
    NodeOption,
    Pool,
    PoolUpsert,
    Readiness,
    RuntimeKind,
    TargetKind,
    TemplateCatalog,
    TemplateDefaults,
    WorkerTemplate,
)
from pydantic import ValidationError

from argus_api.config import Settings
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.fleet import capabilities
from argus_api.fleet.targets import TargetError, TargetInfo, TargetRegistry

LABEL = "argus.worker"  # value "plain": the image speaks the Argus API
CAPABILITIES_LABEL = "argus.worker.capabilities"
EXTRA_HOSTS = ["host.docker.internal:host-gateway"]
LOCAL = "local"
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class TemplateDef:
    id: str
    name: str
    description: str
    runtime: RuntimeKind
    base_name: str
    notes: tuple[str, ...]
    warning: str | None
    setup: tuple[str, ...]


_NC_COMMON_SETUP = (
    "Run integrations/nanoclaw/provision.sh (pinned NanoClaw build, Argus channel,"
    " agent image, host image) and set the ARGUS_NANOCLAW_* variables it prints on"
    " the Argus process.",
)
NANOCLAW_SANDBOX = TemplateDef(
    id="nanoclaw-sandbox",
    name="NanoClaw, sandboxed",
    description="One NanoClaw agent in its own sandbox runtime with a private Docker"
    " daemon. The Docker socket is not shared.",
    runtime=RuntimeKind.NANOCLAW,
    base_name="nanoclaw-sandbox",
    warning=None,
    notes=(
        "Isolated: the host runs under the sandbox OCI runtime (e.g. sysbox-runc)"
        " and cannot reach the Docker host or its sibling instances.",
        "One agent per instance, by design.",
        "Ready means the host can start and register with Argus. Task execution"
        " also requires a running OneCLI gateway and configured model credentials;"
        " these are not verified by this catalog.",
    ),
    setup=(
        *_NC_COMMON_SETUP,
        "Install the sandbox runtime on the Docker host (docs/runbooks/"
        "2026-10-16.nanoclaw-worker-setup.ref.md) and set ARGUS_NANOCLAW_DOCKER_RUNTIME"
        " and ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE.",
    ),
)
NANOCLAW_SOCKET = TemplateDef(
    id="nanoclaw-socket",
    name="NanoClaw, host Docker socket",
    description="One NanoClaw agent whose host process uses this node's Docker"
    " daemon directly, through the Docker socket.",
    runtime=RuntimeKind.NANOCLAW,
    base_name="nanoclaw-socket",
    warning="Not sandboxed: this worker's host gets the Docker socket, which is"
    " root-equivalent on this Docker host. Anything that controls the agent can"
    " control the host and every container on it.",
    notes=(
        "One agent per instance, by design.",
        "Ready means the host can start and register with Argus. Task execution"
        " also requires a running OneCLI gateway and configured model credentials;"
        " these are not verified by this catalog.",
    ),
    setup=(
        *_NC_COMMON_SETUP,
        "The operator must opt in with ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true and set"
        " ARGUS_NANOCLAW_USER (uid:gid) and ARGUS_NANOCLAW_GROUP_ADD (the socket's"
        " group id); the agent image must be present on the node.",
    ),
)
ARGUS_WORKER = TemplateDef(
    id="argus-worker",
    name="Argus API worker",
    description="A container image that speaks the Argus worker API itself.",
    runtime=RuntimeKind.PLAIN,
    base_name="worker",
    warning=None,
    notes=("Only images marked as Argus workers are offered, not every image.",),
    setup=(
        "Label an installed image with argus.worker=plain (optionally"
        " argus.worker.capabilities=a,b), e.g. examples/echo-worker, or list trusted"
        " images in ARGUS_WORKER_IMAGES. The image must already be on the node.",
    ),
)
TEMPLATES = (NANOCLAW_SOCKET, NANOCLAW_SANDBOX, ARGUS_WORKER)
NANOCLAW_IDS = (NANOCLAW_SANDBOX.id, NANOCLAW_SOCKET.id)


class _Facts:
    """One node's option, probe result, and readiness for each template."""

    def __init__(self, option: NodeOption, host: str | None, info: TargetInfo | None):
        self.option, self.host, self.info = option, host, info


def _node_id(host: str | None) -> str:
    return LOCAL if host is None else host


async def _nodes(
    settings: Settings,
    pools: list[Pool],
    targets: TargetRegistry,
) -> list[_Facts]:
    hosts: list[str | None] = [None]
    for h in (
        *settings.docker_hosts,
        *(p.docker.host for p in pools if p.target == "docker"),
    ):
        if h and h not in hosts:
            hosts.append(h)
    facts = []
    for host in hosts:
        cap, info = await capabilities._probe(targets, ("docker", host, None))
        facts.append(
            _Facts(
                NodeOption(
                    id=_node_id(host),
                    label="This machine (local Docker)" if host is None else host,
                    default=host is None,
                    available=cap.available,
                    error=cap.error,
                    version=cap.version,
                    runtimes=cap.runtimes,
                    cpus=cap.cpus,
                    memory_mb=cap.memory_mb,
                    shares_filesystem=bool(info and info.shares_filesystem),
                ),
                host,
                info,
            )
        )
    return facts


class _Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.missing: list[str] = []

    def add(self, name: str, ok: bool | None, detail: str, fix: str | None = None):
        self.checks.append(Check(name=name, ok=ok, detail=detail))
        if ok is not True:
            # An unverifiable prerequisite is reported as such, not as a fix.
            self.missing.append(
                f"{detail}; {fix}" if ok is None and fix else (fix or detail)
            )

    def merge(self, check: Check, fix: str | None = None) -> None:
        self.add(check.name, check.ok, check.detail, fix)


async def _image(
    targets: TargetRegistry, host: str | None, image: str, name: str, fix: str
) -> Check:
    check = await capabilities._image_check(
        targets, ("docker", host, None), image, name
    )
    if check.ok is None and "pulled on start" in check.detail:
        # Whether it can be pulled is not probed: absent means not ready.
        return Check(name=name, ok=False, detail=f"{image} is not on the node")
    return check


def _nanoclaw_options(settings: Settings, sandboxed: bool) -> NanoclawOptions:
    """The mode is the template's, never inferred from settings: the sandbox
    template never gets the socket options, nor the socket template an archive."""
    s = settings
    return NanoclawOptions(
        install_dir=s.nanoclaw_install_dir or "",
        instances_root=s.nanoclaw_instances_root or "",
        agent_image=s.nanoclaw_agent_image or "",
        docker_socket=s.nanoclaw_docker_socket,
        onecli_url=s.nanoclaw_onecli_url or "http://host.docker.internal:10254",
        account_providers=list(s.nanoclaw_account_providers),
        agent_image_archive=s.nanoclaw_agent_image_archive if sandboxed else None,
        user=None if sandboxed else s.nanoclaw_user,
        group_add=[] if sandboxed else list(s.nanoclaw_group_add),
    )


def _ids(user: str | None, group_add: tuple[str, ...]):
    """(uid, gid, extra gids), each None when not a plain number."""
    uid = gid = None
    if user and ":" in user:
        u, g = user.split(":", 1)
        uid, gid = (int(u) if u.isdigit() else None), (int(g) if g.isdigit() else None)
    extra = [int(x) for x in group_add if x.isdigit()]
    return uid, gid, extra, len(extra) == len(group_add)


def socket_access_check(path: str, s: Settings, shares_filesystem: bool) -> Check:
    """Can the configured user open the Docker socket? Measured from its mode and
    owner on a daemon that shares this filesystem; otherwise unverifiable."""
    base = capabilities.path_check(
        "docker_socket", path, shares_filesystem, is_dir=False
    )
    if base.ok is not True:
        return base
    uid, gid, extra, numeric = _ids(s.nanoclaw_user, s.nanoclaw_group_add)
    if uid is None:
        return Check(
            name="docker_socket_access",
            ok=False,
            detail="ARGUS_NANOCLAW_USER must be uid:gid (numbers)",
        )
    if not numeric:
        return Check(
            name="docker_socket_access",
            ok=None,
            detail="ARGUS_NANOCLAW_GROUP_ADD has non-numeric groups: not verifiable",
        )
    st = os.stat(path)
    mode = st.st_mode
    reachable = (
        uid == 0
        or mode & 0o006 == 0o006
        or (st.st_uid == uid and mode & 0o600 == 0o600)
        or ((st.st_gid in extra or st.st_gid == gid) and mode & 0o060 == 0o060)
    )
    return Check(
        name="docker_socket_access",
        ok=reachable,
        detail=(
            f"user {s.nanoclaw_user} can open the socket"
            if reachable
            else f"user {s.nanoclaw_user} cannot open {path} (mode {mode & 0o777:o},"
            f" group {st.st_gid}): add the group to ARGUS_NANOCLAW_GROUP_ADD"
        ),
    )


def readable_by_check(
    name: str, path: str, s: Settings, shares_filesystem: bool
) -> Check:
    """Is the directory enterable by the host's user (top level only)?"""
    if not shares_filesystem:
        return Check(
            name=name, ok=None, detail=f"{path}: not verifiable on this daemon"
        )
    uid, gid, extra, _ = _ids(s.nanoclaw_user, s.nanoclaw_group_add)
    try:
        st = os.stat(path)
    except OSError as exc:
        return Check(name=name, ok=False, detail=f"{path}: {exc.strerror}")
    mode = st.st_mode
    ok = (
        uid == 0
        or mode & 0o005 == 0o005
        or (st.st_uid == uid and mode & 0o500 == 0o500)
        or (st.st_gid in {gid, *extra} and mode & 0o050 == 0o050)
    )
    return Check(
        name=name,
        ok=ok,
        detail=f"{path} is {'readable' if ok else 'not readable'} by user {s.nanoclaw_user}",
    )


def gateway_registration_check(install_dir: str, shares_filesystem: bool) -> Check:
    """Is a gateway provider actually registered in the built checkout? The host
    exits at startup otherwise. A provider file nobody imports does not count: the
    barrel `dist/gateway-providers/installed.js` must import it."""
    name = "nanoclaw_gateway_provider"
    root = install_dir.rstrip("/") + "/dist/gateway-providers"
    if not shares_filesystem:
        return Check(
            name=name, ok=None, detail=f"{root}: not verifiable on this daemon"
        )
    try:
        barrel = Path(root, "installed.js").read_text()
    except OSError:
        return Check(name=name, ok=False, detail=f"{root}/installed.js is missing")
    registered = (
        re.search(r"""^\s*import\s+['"]\./onecli\.js['"]""", barrel, re.M) is not None
    )
    present = Path(root, "onecli.js").is_file()
    ok = registered and present
    return Check(
        name=name,
        ok=ok,
        detail="the OneCLI provider is built and registered"
        if ok
        else "no OneCLI provider is registered in the built checkout",
    )


async def _judge_nanoclaw(
    settings: Settings,
    f: _Facts,
    targets: TargetRegistry,
    r: _Report,
    sandboxed: bool,
) -> None:
    s, info = settings, f.info
    assert info is not None
    required = {
        "ARGUS_NANOCLAW_INSTALL_DIR": s.nanoclaw_install_dir,
        "ARGUS_NANOCLAW_INSTANCES_ROOT": s.nanoclaw_instances_root,
        "ARGUS_NANOCLAW_AGENT_IMAGE": s.nanoclaw_agent_image,
    }
    for env, value in required.items():
        r.add(
            env.lower(),
            bool(value),
            f"{env} is {'set' if value else 'not set'}",
            f"Set {env} on the Argus process",
        )
    fs = info.shares_filesystem
    if sandboxed:
        runtime = s.nanoclaw_docker_runtime
        r.add(
            "oci_runtime",
            bool(runtime) and runtime in info.runtimes,
            f"sandbox runtime {runtime} is available"
            if runtime and runtime in info.runtimes
            else f"sandbox runtime {runtime or '(not configured)'} not offered"
            f" (the daemon has {info.runtimes})",
            f"Install the {runtime or 'sandbox'} runtime on this Docker host and set"
            " ARGUS_NANOCLAW_DOCKER_RUNTIME",
        )
        archive = s.nanoclaw_agent_image_archive
        if archive:
            r.merge(
                capabilities.path_check(
                    "agent_image_archive", archive, fs, is_dir=False
                ),
                "Provide the agent image archive (docker save) at that path",
            )
        else:
            r.add(
                "agent_image_archive",
                False,
                "no agent image archive configured",
                "Set ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE (the sandbox daemon starts"
                " without images; pulling from a registry is not verified)",
            )
    else:
        r.add(
            "operator_opt_in",
            s.nanoclaw_allow_docker_socket,
            "operator allows the root-equivalent Docker socket"
            if s.nanoclaw_allow_docker_socket
            else "the operator has not allowed the Docker socket",
            "Set ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true on the Argus process to accept"
            " that the host gets root-equivalent access to this Docker host",
        )
        r.add(
            "nanoclaw_user",
            bool(s.nanoclaw_user),
            "host runs as an unprivileged user"
            if s.nanoclaw_user
            else "ARGUS_NANOCLAW_USER is not set",
            "Set ARGUS_NANOCLAW_USER=uid:gid (integrations/nanoclaw/provision.sh env"
            " prints it)",
        )
        r.merge(
            socket_access_check(s.nanoclaw_docker_socket, s, fs),
            "Set ARGUS_NANOCLAW_GROUP_ADD to the Docker socket's group id",
        )
        if s.nanoclaw_agent_image:
            r.merge(
                await _image(
                    targets, f.host, s.nanoclaw_agent_image, "nanoclaw_agent_image", ""
                ),
                f"Load the NanoClaw agent image {s.nanoclaw_agent_image} on the node"
                " (provision.sh does this)",
            )
        if s.nanoclaw_install_dir and s.nanoclaw_user:
            r.merge(
                readable_by_check(
                    "nanoclaw_install_readable", s.nanoclaw_install_dir, s, fs
                ),
                "Make the NanoClaw checkout readable by the host's user",
            )
    if all(required.values()):
        try:
            _nanoclaw_options(s, sandboxed)
        except ValidationError as exc:
            r.add(
                "config",
                False,
                "; ".join(e["msg"] for e in exc.errors()),
                "Fix the NanoClaw settings: "
                + "; ".join(e["msg"] for e in exc.errors()),
            )
        r.merge(
            await _image(
                targets, f.host, s.nanoclaw_host_image, "nanoclaw_host_image", ""
            ),
            f"Build the host image: docker build -t {s.nanoclaw_host_image} deploy/nanoclaw-host",
        )
        assert s.nanoclaw_install_dir and s.nanoclaw_instances_root
        root = s.nanoclaw_install_dir.rstrip("/")
        for name, path, is_dir, fix in (
            (
                "nanoclaw_install_dir",
                s.nanoclaw_install_dir,
                True,
                "Create the NanoClaw checkout",
            ),
            (
                "nanoclaw_build",
                f"{root}/dist/index.js",
                False,
                "Build NanoClaw (pnpm install && pnpm run build)",
            ),
            (
                "nanoclaw_argus_channel",
                f"{root}/dist/channels/argus.js",
                False,
                "Install and build the Argus channel (integrations/nanoclaw/install.sh)",
            ),
            (
                "nanoclaw_instances_root",
                s.nanoclaw_instances_root,
                True,
                "Create the instances root directory",
            ),
        ):
            r.merge(capabilities.path_check(name, path, fs, is_dir=is_dir), fix)
        r.merge(
            gateway_registration_check(s.nanoclaw_install_dir, fs),
            "Install the OneCLI gateway adapter into the checkout"
            " (integrations/nanoclaw/provision.sh); the host exits at startup without"
            " a registered gateway provider",
        )


async def _judge_worker(
    settings: Settings, f: _Facts, targets: TargetRegistry, r: _Report
) -> list[ImageChoice]:
    choices: dict[str, ImageChoice] = {}
    try:
        labeled = await targets.get("docker", f.host).labeled_images(LABEL)
    except TargetError as exc:
        labeled = []
        r.add("images", False, f"could not list images: {exc}")
    for img in labeled:
        if img.labels.get(LABEL) == "plain":
            caps = [
                c.strip()
                for c in img.labels.get(CAPABILITIES_LABEL, "").split(",")
                if c.strip()
            ]
            choices.setdefault(
                img.tag, ImageChoice(image=img.tag, capabilities=caps, source="label")
            )
    for tag in settings.worker_images:
        check = await _image(targets, f.host, tag, "image", "")
        if check.ok:
            choices.setdefault(
                tag, ImageChoice(image=tag, capabilities=[], source="config")
            )
    r.add(
        "worker_image",
        bool(choices),
        f"{len(choices)} Argus worker image(s) on this node"
        if choices
        else "no Argus worker image on this node",
        "Label an installed image argus.worker=plain (e.g. build examples/echo-worker"
        " with --label argus.worker=plain) or list a present image in ARGUS_WORKER_IMAGES",
    )
    return list(choices.values())


async def _judge(
    settings: Settings, tpl: TemplateDef, f: _Facts, targets: TargetRegistry
) -> Readiness:
    r = _Report()
    r.add(
        "public_url",
        settings.public_url is not None,
        "ARGUS_PUBLIC_URL is set"
        if settings.public_url
        else "ARGUS_PUBLIC_URL is not set",
        "Set ARGUS_PUBLIC_URL to the address containers use to reach Argus",
    )
    r.add(
        "node",
        f.option.available,
        "Docker reachable"
        if f.option.available
        else f"Docker unreachable: {f.option.error}",
        f"Start or fix the Docker daemon: {f.option.error}",
    )
    images: list[ImageChoice] = []
    if f.info is not None:
        if tpl.id in NANOCLAW_IDS:
            await _judge_nanoclaw(
                settings, f, targets, r, sandboxed=tpl.id == NANOCLAW_SANDBOX.id
            )
        else:
            images = await _judge_worker(settings, f, targets, r)
    return Readiness(
        node=f.option.id,
        ready=not r.missing,
        checks=r.checks,
        missing=r.missing,
        images=images,
    )


def _suggest(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


async def catalog(
    settings: Settings,
    pools: list[Pool],
    targets: TargetRegistry,
) -> TemplateCatalog:
    facts = await _nodes(settings, pools, targets)
    taken = {p.name for p in pools}
    templates = []
    for tpl in TEMPLATES:
        readiness = [await _judge(settings, tpl, f, targets) for f in facts]
        templates.append(
            WorkerTemplate(
                id=tpl.id,
                name=tpl.name,
                description=tpl.description,
                runtime=tpl.runtime,
                notes=list(tpl.notes),
                warning=tpl.warning,
                setup=list(tpl.setup),
                defaults=TemplateDefaults(
                    name=_suggest(tpl.base_name, taken),
                    quantity=1,
                    max_replicas=TEMPLATE_DEFAULT_MAX_REPLICAS,
                    capabilities=[],
                    extra_hosts=list(EXTRA_HOSTS),
                ),
                readiness=readiness,
                ready=any(x.ready for x in readiness),
            )
        )
    return TemplateCatalog(
        public_url_configured=settings.public_url is not None,
        default_node=LOCAL,
        nodes=[f.option for f in facts],
        templates=templates,
        pools=len(pools),
    )


def validate_advanced(req: CreateWorkerRequest) -> None:
    adv = req.advanced
    if adv is None:
        return
    errors = [
        f"env {k!r} is not a valid variable name"
        for k in (adv.env or {})
        if not _ENV_KEY.match(k)
    ]
    errors += [
        f"extra host {h!r} must be name:address"
        for h in (adv.extra_hosts or [])
        if ":" not in h
    ]
    if errors:
        raise ValidationFailedError("invalid advanced options", details=errors)
    if adv.max_replicas is not None and adv.max_replicas < req.quantity:
        raise ValidationFailedError(
            f"max_replicas {adv.max_replicas} is below the quantity {req.quantity}",
            code="invalid_cap",
        )


def build_pool(
    settings: Settings,
    cat: TemplateCatalog,
    req: CreateWorkerRequest,
    pools: list[Pool],
) -> tuple[str, PoolUpsert]:
    """The pool a request stands for, rebuilt from the template alone; raises the
    precise refusal. Shared by REST and MCP, so both validate identically."""
    tpl_def = next((t for t in TEMPLATES if t.id == req.template), None)
    if tpl_def is None:
        raise ValidationFailedError(
            f"unknown template {req.template!r}; known: {[t.id for t in TEMPLATES]}",
            code="unknown_template",
        )
    node = next((n for n in cat.nodes if n.id == req.node), None)
    if node is None:
        raise ValidationFailedError(
            f"unknown node {req.node!r}; configured: {[n.id for n in cat.nodes]}",
            code="unknown_node",
        )
    validate_advanced(req)
    tpl = next(t for t in cat.templates if t.id == tpl_def.id)
    readiness = next(x for x in tpl.readiness if x.node == node.id)
    if not readiness.ready:
        raise ConflictError(
            f"template {tpl.id!r} is not ready on node {node.id!r}",
            code="template_not_ready",
            details=readiness.missing,
        )
    name = req.name
    if any(p.name == name for p in pools):
        raise ConflictError(
            f"pool {name!r} already exists; launch a worker in it instead",
            code="pool_exists",
        )
    adv = req.advanced
    desired = req.quantity
    cap = (
        adv.max_replicas
        if adv and adv.max_replicas is not None
        else max(req.quantity, tpl.defaults.max_replicas)
    )
    env = dict(adv.env) if adv and adv.env is not None else {}
    extra_hosts = (
        adv.extra_hosts
        if adv and adv.extra_hosts is not None
        else tpl.defaults.extra_hosts
    )
    host = None if node.id == LOCAL else node.id
    if tpl.id in NANOCLAW_IDS:
        if req.image is not None:
            raise ValidationFailedError(
                "this template's image is fixed by the server", code="unknown_image"
            )
        # Isolation is the template's own: never read from the request, never
        # substituted when the other mode is the one that is configured.
        sandboxed = tpl.id == NANOCLAW_SANDBOX.id
        return name, PoolUpsert(
            runtime=RuntimeKind.NANOCLAW,
            image=settings.nanoclaw_host_image,
            capabilities=adv.capabilities
            if adv and adv.capabilities is not None
            else [],
            agents_per_instance=1,
            docker=DockerOptions(
                host=host,
                network=settings.nanoclaw_network,
                extra_hosts=extra_hosts,
                runtime=settings.nanoclaw_docker_runtime if sandboxed else None,
            ),
            nanoclaw=_nanoclaw_options(settings, sandboxed),
            target=TargetKind.DOCKER,
            desired_replicas=desired,
            max_replicas=cap,
            env=env,
            template=tpl.id,
        )
    offered = {c.image: c for c in readiness.images}
    image = req.image
    if image is None and len(offered) == 1:
        image = next(iter(offered))
    if image is None:
        raise ValidationFailedError(
            f"choose an image: {sorted(offered)}", code="image_required"
        )
    if image not in offered:
        raise ValidationFailedError(
            f"image {image!r} is not an Argus worker image on node {node.id!r};"
            f" offered: {sorted(offered)}",
            code="unknown_image",
        )
    return name, PoolUpsert(
        runtime=RuntimeKind.PLAIN,
        image=image,
        capabilities=adv.capabilities
        if adv and adv.capabilities is not None
        else offered[image].capabilities,
        docker=DockerOptions(host=host, extra_hosts=extra_hosts),
        target=TargetKind.DOCKER,
        desired_replicas=desired,
        max_replicas=cap,
        env=env,
        template=tpl.id,
    )
