"""deploy/argus against a fake docker and curl (docs/specs/launcher.spec.md 6).

The fakes keep their state in one JSON file: containers, volumes (with a fake
database content and backup files), pulled images, and which versions fail to
start. A version that fails exits at once, so readiness never waits.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from _pytest.outcomes import Failed

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "deploy" / "argus"
IMAGE = "ghcr.io/2mundos/argus"

FAKE_DOCKER = r'''
import json, os, sys

STATE = os.environ["FAKE_STATE"]
with open(STATE) as f:
    s = json.load(f)
argv = sys.argv[1:]
s["calls"].append(argv)

def save():
    with open(STATE, "w") as f:
        json.dump(s, f)


# Every call is recorded, including those that change no other state.
import atexit
atexit.register(save)

def out(text):
    sys.stdout.write(text)

def ref_of(img):
    return img[4:] if img.startswith("img:") else img

def norm(ref):
    """name:tag@digest is stored as name@digest, as Docker does."""
    ref = ref_of(ref)
    if "@" in ref:
        name, digest = ref.split("@", 1)
        if ":" in name.rsplit("/", 1)[-1]:
            name = name.rsplit(":", 1)[0]
        return name + "@" + digest
    return ref

def version_of(img):
    return ref_of(img).rsplit(":", 1)[1]

def vkey(v):
    return tuple(int(p) for p in v.lstrip("v").split("."))

def volume(name):
    return s["volumes"].setdefault(name, {"db": "", "files": []})

def fmt(template, c):
    if template == "{{.State.Running}}":
        return "true" if c["running"] else "false"
    if template == "{{.Image}}":
        return "img:" + c["image"]
    if template.startswith("{{index .Config.Labels"):
        return c.get("label", "")
    if template.startswith("{{range .Config.Env}}"):
        return "".join(e + "\n" for e in c["env"])
    if template.startswith("{{range .Mounts}}"):
        return "".join(m + "\n" for m in c["mounts"])
    raise SystemExit("fake docker: unknown template " + template)

def opts(args):
    """Split `run` options from image and command."""
    flags = {"-d", "--rm", "-i", "-t"}
    o, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in flags:
            o.append((args[i], None)); i += 1
        else:
            o.append((args[i], args[i + 1])); i += 2
    return o, args[i], args[i + 1:]

def run(args):
    o, img, cmd = opts(args)
    d = dict(o)
    vol = None
    for k, v in o:
        if k == "-v" and v.endswith(":/data"):
            vol = v.split(":")[0]
    ep = d.get("--entrypoint")
    if "-d" in d and d["--name"].startswith("argus-onecli"):
        # The gateway and its database: recorded, never "Argus".
        s["containers"][d["--name"]] = {
            "image": norm(img), "running": True, "env": [], "mounts": [], "args": args,
        }
        s["images"].append(norm(img))
        for k, v in o:
            if k == "-v":
                volume(v.split(":")[0])
        return save()
    if "-d" in d:
        name = d["--name"]
        ver = version_of(img)
        ok = ver not in s["broken"]
        env = [v for k, v in o if k == "-e"]
        s["containers"][name] = {
            "image": ref_of(img), "running": ok, "env": env, "label": ver,
            "mounts": ["volume:%s:/data" % vol], "args": args,
        }
        if ok:
            s["ready"] = True
        # Starting a newer version migrates the database, even when it then fails.
        v = volume(vol)
        if not v["db"] or vkey(ver) > vkey(v["db"].split("@")[1]):
            v["db"] = "schema@" + ver
        return save()
    if ep == "age-keygen":
        if version_of(img) in s["no_keygen"]:
            sys.stderr.write("exec: age-keygen: not found\n")
            sys.exit(127)
        if cmd[:1] == ["-y"]:
            return out("age1fakederived\n")
        return out("# created: now\n# public key: age1fakepublic\nAGE-SECRET-KEY-1FAKE\n")
    if ep == "stat":
        return out(s["socket_gid"] + "\n")
    if ep == "cat" and cmd == ["/app/bin/argus"]:
        with open(s["launcher_source"]) as f:
            return out(f.read() + "\n# fake launcher for " + version_of(img) + "\n")
    if ep == "sh":
        for name, body in s["notes"].items():
            out("@@@ /app/releases/" + name + "\n" + body + "\n")
        return
    if ep == "test":
        path = cmd[1].removeprefix("/data/")
        sys.exit(0 if path in volume(vol)["files"] else 1)
    if ep == "python":
        script, rest = cmd[1], cmd[2:]
        v = volume(vol)
        if "argus.db.restore" in script:
            v["db"] = v["backup_content"][rest[0]]
            return save()
        if "secrets.sops.yaml" in script:  # the uninstall summary
            out("%d\n%s\n" % (len(v["db"]), "yes" if "secrets.sops.yaml" in v["files"] else "no"))
            for f in sorted(v["files"]):
                if f.startswith("backups/"):
                    out(f.removeprefix("backups/") + "\n")
            return
        return backup(v, rest)
    raise SystemExit("fake docker: unexpected run " + repr(args))

def backup(v, rest):
    name = rest[0]
    v["files"].append("backups/" + name)
    v.setdefault("backup_content", {})[name] = v["db"]
    backups = sorted(f for f in v["files"] if f.startswith("backups/argus-"))
    protect = rest[1] if len(rest) > 1 else ""
    for f in backups[:-5]:
        if f != "backups/" + protect:
            v["files"].remove(f)
    save()

cmd, rest = argv[0], argv[1:]
if cmd == "container" and rest[0] == "inspect":
    name = rest[-1]
    c = s["containers"].get(name)
    if c is None:
        sys.exit(1)
    if rest[1] == "-f":
        out(fmt(rest[2], c))
elif cmd == "volume" and rest[0] == "inspect":
    sys.exit(0 if rest[1] in s["volumes"] else 1)
elif cmd == "image" and rest[0] == "inspect":
    if rest[1] == "-f":
        out("PATH=/usr/bin\nARGUS_VERSION=x\nARGUS_DATA_DIR=/data\n")
    else:
        sys.exit(0 if norm(rest[-1]) in s["images"] else 1)
elif cmd == "pull":
    if s["pull_fails"] or any(rest[0].startswith(m) for m in s["missing_images"]):
        sys.stderr.write("Error response from daemon: unauthorized\n")
        sys.exit(1)
    s["images"].append(norm(rest[0]))
    save()
elif cmd == "rmi":
    ref = norm(rest[-1])
    if ref not in s["images"] or any(norm(c["image"]) == ref for c in s["containers"].values()):
        sys.stderr.write("Error response from daemon: conflict\n")
        sys.exit(1)
    s["images"].remove(ref)
    save()
elif cmd == "info":
    out(s["engine"] + "\n")
elif cmd == "network":
    if rest[:2] == ["inspect", "bridge"]:
        out("172.17.0.1\n")
    elif rest[0] == "inspect":
        sys.exit(0 if rest[1] in s["networks"] else 1)
    elif rest[0] == "create":
        s["networks"].append(rest[1])
        save()
    elif rest[0] == "rm":
        s["networks"].remove(rest[1])
        save()
elif cmd == "create":
    out("cid-" + rest[0] + "\n")
elif cmd == "cp":
    # Copying /opt/nanoclaw out of a host image: a small built checkout.
    dest = rest[1]
    os.makedirs(os.path.join(dest, "dist"))
    with open(os.path.join(dest, "dist", "index.js"), "w") as f:
        f.write(rest[0])
elif cmd == "volume" and rest[0] == "rm":
    s["volumes"].pop(rest[1], None)
    save()
elif cmd == "run":
    run(rest)
elif cmd == "exec":
    name, tool = rest[0], rest[1:]
    c = s["containers"][name]
    if tool[:2] == ["cat", "/data/admin-token"]:
        if s["token"]:
            out(s["token"] + "\n")
        else:
            sys.exit(1)
    elif tool[0] == "python":
        vol = c["mounts"][0].split(":")[1]
        backup(volume(vol), tool[3:])
    else:
        out("")
elif cmd in ("stop", "rm"):
    c = s["containers"].get(rest[-1])
    if cmd == "stop" and c:
        c["running"] = False
    if cmd == "rm":
        s["containers"].pop(rest[-1], None)
    save()
elif cmd == "logs":
    out("fake log line\n")
else:
    raise SystemExit("fake docker: unexpected " + repr(argv))
'''

FAKE_CURL = r"""
import json, os, sys
with open(os.environ["FAKE_STATE"]) as f:
    s = json.load(f)
url = sys.argv[-1]
if url.endswith("/releases/latest"):
    print("HTTP/2 302\r\nlocation: https://github.com/2Mundos/argus/releases/tag/%s\r\n" % s["latest"])
    sys.exit(0)
if url.endswith("/readyz"):
    running = s["containers"].get("argus", {}).get("running", False)
    sys.exit(0 if running and s["ready"] else 22)
sys.exit(22)
"""

NOTES = {
    "2026-10-30.v0-6-0.ref.md": "# v0.6.0\n\n## Upgrading\n\nNotes for 0.6\n",
    "2026-10-31.v0-7-0.ref.md": "# v0.7.0\n\n## Upgrading\n\nMigrations 0062\n\nSpecs added or changed:\n- x\n",
    "2026-11-02.v0-8-0.ref.md": "# v0.8.0\n\n## Upgrading\n\nMigrations 0069\n\n## Features\n- y\n",
}

# deploy/argus: worker nc_root is $ARGUS_DATA_HOME/nanoclaw; must be <= 54 characters.
_NANOCLAW_UNDER_DATA_HOME = "/argus/nanoclaw"
_MAX_DATA_HOME_LEN = 54 - len(_NANOCLAW_UNDER_DATA_HOME)
_LAUNCHER_OWNED = ".launcher-test-owned"
_LT_DIR = "lt"
_DH_DIR = "dh"


@contextmanager
def _launcher_data_home_lock() -> Generator[None]:
    """Serialize claims when several workers share one max-length TMPDIR."""
    scratch = _scratch_dir()
    lock = scratch / ".launcher-data.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _scratch_dir() -> Path:
    raw = os.environ.get("TMPDIR")
    if not raw:
        pytest.fail("TMPDIR must be set when running launcher tests")
    return Path(raw).resolve()


def _pick_data_home_path(scratch: Path, tag: str) -> Path | None:
    for suffix in (f"{_DH_DIR}/{tag[:8]}", f"{_DH_DIR}/{tag[:4]}", ""):
        data_home = scratch if not suffix else scratch / suffix
        if len(str(data_home)) <= _MAX_DATA_HOME_LEN:
            return data_home
    return None


def _path_within_scratch(path: Path, scratch: Path) -> bool:
    try:
        path.resolve().relative_to(scratch.resolve())
    except ValueError:
        return False
    return True


def _occupied_argus(argus: Path) -> str | None:
    """Why `argus` cannot be claimed, or None when the directory is absent."""
    if not argus.exists():
        return None
    marker = argus / _LAUNCHER_OWNED
    if not marker.is_file():
        return f"{argus} already exists without a launcher test marker"
    return f"{argus} is already occupied"


def _resolved_data_home_for_tag(tag: str) -> Path:
    """Same path choice as `_acquire_data_home` (without creating anything)."""
    scratch = _scratch_dir()
    data_home = _pick_data_home_path(scratch, tag)
    if data_home is None:
        pytest.fail(
            f"TMPDIR {scratch} ({len(str(scratch))} chars) is too long for worker setup;"
            f" need data home length <= {_MAX_DATA_HOME_LEN}"
        )
    return data_home


def _acquire_data_home(tag: str) -> tuple[Path, Path, bool]:
    """Claim a data home under TMPDIR; return (data_home, argus_dir, owned)."""
    scratch = _scratch_dir()
    data_home = _resolved_data_home_for_tag(tag)
    if not _path_within_scratch(data_home, scratch):
        pytest.fail(f"data home escapes TMPDIR: {data_home}")
    data_home.mkdir(parents=True, exist_ok=True)
    argus = data_home / "argus"
    why = _occupied_argus(argus)
    if why:
        pytest.fail(why)
    try:
        argus.mkdir(mode=0o700)
    except FileExistsError:
        pytest.fail(_occupied_argus(argus) or f"{argus} exists")
    (argus / _LAUNCHER_OWNED).write_text(f"{tag}\n", encoding="utf-8")
    return data_home, argus, True


def _release_data_home(argus: Path, tag: str, owned: bool) -> None:
    if not owned or not argus.exists():
        return
    marker = argus / _LAUNCHER_OWNED
    if not marker.is_file():
        return
    if marker.read_text(encoding="utf-8").strip() != tag:
        return
    scratch = _scratch_dir()
    if not _path_within_scratch(argus, scratch):
        return
    shutil.rmtree(argus, ignore_errors=True)


def _allocate_tmp_sandbox(request: pytest.FixtureRequest) -> Path:
    scratch = _scratch_dir()
    base = scratch / _LT_DIR
    for attempt in range(16):
        leaf = (
            f"{os.getpid():x}{abs(hash((request.node.nodeid, attempt))) % 0xFFFF:04x}"
        )
        root = base / leaf
        if not _path_within_scratch(root, scratch):
            pytest.fail(f"tmp sandbox escapes TMPDIR: {root}")
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return root
    pytest.fail("could not allocate a unique launcher tmp sandbox under TMPDIR")


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Generator[Path]:
    """Short sandboxes under TMPDIR only; always removed in finally."""
    scratch = _scratch_dir()
    root = _allocate_tmp_sandbox(request)
    try:
        yield root
    finally:
        if _path_within_scratch(root, scratch):
            shutil.rmtree(root, ignore_errors=True)


class Env:
    def __init__(self, tmp: Path, data_home: Path, tag: str) -> None:
        self.tmp = tmp
        self.tag = tag
        self.state_path = tmp / "state.json"
        self.home = tmp / "home"
        self.data_home = data_home
        self.argus_dir = data_home / "argus"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        fakes = tmp / "fakes"
        fakes.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            (fakes / f"{name}.py").write_text(body)
            stub = self.bin / name
            stub.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{fakes / name}.py" "$@"\n'
            )
            stub.chmod(0o755)
        self.launcher = self.bin / "argus"
        shutil.copy(LAUNCHER, self.launcher)
        self.state = {
            "calls": [],
            "containers": {},
            "volumes": {},
            "images": [],
            "broken": [],
            "ready": False,
            "latest": "v0.8.0",
            "engine": "Ubuntu 24.04",
            "socket_gid": "998",
            "no_keygen": [],
            "pull_fails": False,
            "missing_images": [],
            "networks": [],
            "token": "argus_admin_generated",
            "notes": NOTES,
            "launcher_source": str(LAUNCHER),
        }
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def load(self) -> dict:
        self.state = json.loads(self.state_path.read_text())
        return self.state

    def run(
        self,
        *args: str,
        ok: bool = True,
        script: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.save()
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "ARGUS_HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data_home),
            "FAKE_STATE": str(self.state_path),
            "ARGUS_POLL_INTERVAL": "0",
            "ARGUS_READY_TIMEOUT": "0",
            "ARGUS_TTY": str(self.tmp / "no-terminal"),
            **(extra_env or {}),
        }
        r = subprocess.run(
            ["sh", str(script or self.launcher), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.load()
        if ok:
            assert r.returncode == 0, r.stdout + r.stderr
        else:
            assert r.returncode != 0, r.stdout + r.stderr
        return r

    def config(self) -> dict[str, str]:
        out = {}
        for line in (self.home / "argus.env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v.strip("'")
        return out

    def run_args(self) -> list[str]:
        return self.state["containers"]["argus"]["args"]

    def db(self) -> str:
        return self.state["volumes"]["argus-data"]["db"]

    def backups(self) -> list[str]:
        files = self.state["volumes"]["argus-data"]["files"]
        return sorted(
            f.removeprefix("backups/") for f in files if f.startswith("backups/")
        )

    def answer(self, text: str) -> dict[str, str]:
        """A terminal that answers `text` to the launcher's question."""
        tty = self.tmp / "tty"
        tty.write_text(text + "\n")
        return {"ARGUS_TTY": str(tty)}

    def install(self, version: str = "v0.7.0") -> None:
        """A launcher-managed install at `version`, running."""
        self.run("up", "--version", version)
        self.state["calls"] = []


@pytest.fixture
def env(tmp_path: Path, request: pytest.FixtureRequest) -> Generator[Env]:
    tag = f"{os.getpid():x}{abs(hash(request.node.nodeid)) % 0xFFFFFF:06x}"
    data_home: Path | None = None
    argus: Path | None = None
    owned = False
    with _launcher_data_home_lock():
        data_home, argus, owned = _acquire_data_home(tag)
        try:
            yield Env(tmp_path, data_home, tag)
        finally:
            if argus is not None:
                _release_data_home(argus, tag, owned)


def test_first_install_creates_config_key_and_prints_token(env: Env) -> None:
    r = env.run("up")
    assert env.config()["ARGUS_VERSION"] == "v0.8.0"  # newest release
    key = env.home / "age-key.txt"
    assert oct(key.stat().st_mode & 0o777) == "0o644"
    assert oct(env.home.stat().st_mode & 0o777) == "0o700"
    args = env.run_args()
    assert "SOPS_AGE_RECIPIENTS=age1fakepublic" in args
    assert "127.0.0.1:8000:8000" in args
    assert "/var/run/docker.sock:/var/run/docker.sock" not in args
    assert "argus_admin_generated" in r.stdout
    assert not (env.home / "lock").exists()


@pytest.mark.parametrize(("pin", "expected"), [(None, "v0.8.0"), ("v0.7.0", "v0.7.0")])
def test_install_sh_takes_the_newest_release_unless_pinned(
    env: Env, pin: str | None, expected: str
) -> None:
    bin_dir = env.tmp / "installed"
    extra = {"ARGUS_BIN_DIR": str(bin_dir)} | ({"ARGUS_VERSION": pin} if pin else {})
    r = env.run(script=ROOT / "deploy" / "install.sh", extra_env=extra)
    assert ["pull", f"{IMAGE}:{expected}"] in env.state["calls"]
    assert (bin_dir / "argus").read_text().endswith(f"fake launcher for {expected}\n")
    assert f"Installed argus ({expected})" in r.stdout


def test_ready_without_an_admin_token_file(env: Env) -> None:
    env.state["token"] = ""
    r = env.run("up")
    assert "existing or preset" in r.stdout


@pytest.mark.parametrize(
    ("engine", "bridge"),
    [("Ubuntu 24.04", True), ("Docker Desktop", False)],
)
def test_docker_pools_flags(env: Env, engine: str, bridge: bool) -> None:
    env.state["engine"] = engine
    env.run("up", "--docker", "--port", "8100")
    args = env.run_args()
    i = args.index("--group-add")
    assert args[i + 1] == "998"
    assert "host.docker.internal:host-gateway" in args
    assert "ARGUS_PUBLIC_URL=http://host.docker.internal:8100" in args
    assert ("172.17.0.1:8100:8000" in args) is bridge
    assert env.config()["ARGUS_DOCKER"] == "1"


def test_up_version_refused_on_existing_install(env: Env) -> None:
    env.install()
    r = env.run("up", "--version", "v0.8.0", ok=False)
    assert "argus update" in r.stderr
    assert env.config()["ARGUS_VERSION"] == "v0.7.0"


def test_adopts_a_plain_readme_container_after_a_backup(env: Env) -> None:
    env.state["volumes"]["argus-data"] = {"db": "schema@v0.7.0", "files": []}
    env.state["containers"]["argus"] = {
        "image": f"{IMAGE}:v0.7.0",
        "running": True,
        "env": ["PATH=/usr/bin", "ARGUS_VERSION=x", "ARGUS_DATA_DIR=/data"],
        "label": "v0.7.0",
        "mounts": ["volume:argus-data:/data"],
        "args": [],
    }
    env.state["images"].append(f"{IMAGE}:v0.7.0")
    env.state["no_keygen"] = ["v0.7.0"]  # images before the launcher lack age-keygen
    r = env.run("up")
    assert env.config()["ARGUS_VERSION"] == "v0.7.0"
    assert len(env.backups()) == 1
    assert env.backups()[0].endswith("-v0.7.0.db")
    assert "AGE-SECRET-KEY" in (env.home / "age-key.txt").read_text()
    assert list(env.home.glob("age-key.txt.*")) == []
    assert "http://127.0.0.1:8000" in r.stdout
    assert "v0.8.0 is available" in r.stdout


def test_token_and_status_without_an_install_say_run_up(env: Env) -> None:
    r = env.run("token", ok=False)
    assert "argus up" in r.stderr
    env.install()
    env.state["containers"]["argus"]["running"] = False
    r = env.run("token", ok=False)
    assert "not running" in r.stderr


def test_an_unexpected_failure_is_reported(env: Env) -> None:
    env.state["pull_fails"] = True
    r = env.run("up", ok=False)
    assert "unauthorized" in r.stderr
    assert "argus: " in r.stderr
    assert not (env.home / "lock").exists()


@pytest.mark.parametrize(
    ("env_vars", "mounts", "files", "message"),
    [
        (
            ["SOPS_AGE_KEY_FILE=/keys/age.txt"],
            ["bind::/keys"],
            [],
            "env:SOPS_AGE_KEY_FILE",
        ),
        (["LITESTREAM_REPLICA_URL=s3://b"], [], [], "env:LITESTREAM_REPLICA_URL"),
        ([], ["bind::/srv/extra"], [], "mount:/srv/extra"),
        ([], [], ["secrets.sops.yaml"], "--age-key"),
    ],
)
def test_adoption_refused_before_anything_is_removed(
    env: Env, env_vars: list[str], mounts: list[str], files: list[str], message: str
) -> None:
    env.state["volumes"]["argus-data"] = {"db": "schema@v0.7.0", "files": files}
    env.state["containers"]["argus"] = {
        "image": f"{IMAGE}:v0.7.0",
        "running": True,
        "env": ["PATH=/usr/bin", *env_vars],
        "label": "v0.7.0",
        "mounts": ["volume:argus-data:/data", *mounts],
        "args": [],
    }
    env.state["images"].append(f"{IMAGE}:v0.7.0")
    r = env.run("up", ok=False)
    assert message in r.stderr
    assert env.state["containers"]["argus"]["running"]
    assert not any(c[0] in ("stop", "rm") for c in env.state["calls"])
    assert not (env.home / "argus.env").exists()


def test_adoption_with_the_existing_key(env: Env) -> None:
    key = env.tmp / "old-key.txt"
    key.write_text("# public key: age1oldkey\nAGE-SECRET-KEY-1OLD\n")
    env.state["volumes"]["argus-data"] = {
        "db": "schema@v0.7.0",
        "files": ["secrets.sops.yaml"],
    }
    env.state["containers"]["argus"] = {
        "image": f"{IMAGE}:v0.7.0",
        "running": True,
        "env": [
            "PATH=/usr/bin",
            "SOPS_AGE_KEY_FILE=/keys/age.txt",
            "SOPS_AGE_RECIPIENTS=age1oldkey",
        ],
        "label": "v0.7.0",
        "mounts": ["volume:argus-data:/data", "bind::/keys/age.txt"],
        "args": [],
    }
    env.state["images"].append(f"{IMAGE}:v0.7.0")
    env.run("up", "--age-key", str(key))
    assert (env.home / "age-key.txt").read_text() == key.read_text()
    assert "SOPS_AGE_RECIPIENTS=age1oldkey" in env.run_args()


def test_update_backs_up_after_stopping_and_upgrades(env: Env) -> None:
    env.install("v0.6.0")
    r = env.run("update", "-y")
    assert env.config()["ARGUS_VERSION"] == "v0.8.0"
    assert env.state["containers"]["argus"]["image"] == f"{IMAGE}:v0.8.0"
    # Notes of every skipped release, not the current one or later sections.
    assert "Migrations 0062" in r.stdout and "Migrations 0069" in r.stdout
    assert "Notes for 0.6" not in r.stdout and "Features" not in r.stdout
    # The backup is taken offline, after the stop, from the pre-upgrade schema.
    calls = env.state["calls"]
    stop = calls.index(["stop", "argus"])
    backup_run = next(i for i, c in enumerate(calls) if c[0] == "run" and "python" in c)
    assert stop < backup_run
    (backup,) = env.backups()
    assert (
        env.state["volumes"]["argus-data"]["backup_content"][backup] == "schema@v0.6.0"
    )
    assert "# fake launcher for v0.8.0" in env.launcher.read_text()
    assert not (env.bin / "argus.prev").exists()


def test_failed_update_rolls_back_image_database_version_and_launcher(env: Env) -> None:
    env.install("v0.7.0")
    before = env.launcher.read_text()
    env.state["broken"] = ["v0.8.0"]
    r = env.run("update", "-y", ok=False)
    assert "Rolled back" in r.stderr
    assert "fake log line" in r.stderr
    assert env.config()["ARGUS_VERSION"] == "v0.7.0"
    assert env.state["containers"]["argus"]["image"] == f"{IMAGE}:v0.7.0"
    # v0.8.0 migrated the database before failing; the restore undid that.
    assert env.db() == "schema@v0.7.0"
    assert env.launcher.read_text() == before
    assert not (env.home / "lock").exists()


def test_update_refuses_a_downgrade(env: Env) -> None:
    env.install("v0.8.0")
    r = env.run("update", "v0.7.0", "-y", ok=False)
    assert "refusing to downgrade" in r.stderr
    assert not any(c[0] == "stop" for c in env.state["calls"])


def test_restore_runs_the_backup_version_and_keeps_the_current_db(env: Env) -> None:
    env.install("v0.7.0")
    env.run("update", "-y")
    (backup,) = env.backups()
    env.run("restore", backup)
    assert env.config()["ARGUS_VERSION"] == "v0.7.0"
    assert len(env.backups()) == 2  # the restore backed up the v0.8.0 database first
    assert (
        env.state["volumes"]["argus-data"]["backup_content"][env.backups()[1]]
        == "schema@v0.8.0"
    )


def test_backups_keep_the_newest_five(env: Env) -> None:
    env.install()
    vol = env.state["volumes"]["argus-data"]
    vol["files"] = [f"backups/argus-2026010{i}T000000Z-v0.7.0.db" for i in range(1, 6)]
    env.run("backup")
    assert len(env.backups()) == 5
    assert "argus-20260101T000000Z-v0.7.0.db" not in env.backups()


def test_a_held_lock_refuses_mutating_commands(env: Env) -> None:
    env.install()
    (env.home / "lock").mkdir()
    (env.home / "lock" / "pid").write_text(f"{os.getpid()}\n")
    for cmd in (
        ["up"],
        ["update", "-y"],
        ["backup"],
        ["restore", "argus-1-v0.7.0.db"],
        ["down"],
        ["worker", "setup", "-y"],
        ["worker", "remove"],
        ["uninstall", "-y"],
    ):
        r = env.run(*cmd, ok=False)
        assert "another argus command is running" in r.stderr, cmd
    assert env.state["containers"]["argus"]["running"]
    env.run("status")  # read-only commands take no lock
    env.run("worker", "status")


def test_a_stale_lock_needs_unlock(env: Env) -> None:
    env.install()
    dead = subprocess.run(
        ["sh", "-c", "echo $$"], capture_output=True, text=True, check=True
    )
    (env.home / "lock").mkdir()
    (env.home / "lock" / "pid").write_text(dead.stdout)
    r = env.run("up", ok=False)
    assert "argus unlock" in r.stderr
    env.run("unlock")
    env.run("up")


def test_launcher_is_posix_sh() -> None:
    for shell in ("sh", "dash"):
        if shutil.which(shell):
            subprocess.run([shell, "-n", str(LAUNCHER)], check=True)
    assert LAUNCHER.read_text().startswith("#!/bin/sh\n")


# -- local workers (docs/specs/local-workers.spec.md) --------------------------

HOST = "ghcr.io/2mundos/argus-nanoclaw-host"
AGENT = "ghcr.io/2mundos/argus-nanoclaw-agent"


def arg_env(args: list[str]) -> dict[str, str]:
    pairs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    return dict(p.split("=", 1) for p in pairs)


def nc_root(env: Env) -> Path:
    return env.data_home / "argus" / "nanoclaw"


@pytest.mark.parametrize(
    ("engine", "bridge"), [("Ubuntu 24.04", True), ("Docker Desktop", False)]
)
def test_worker_setup_prepares_images_checkout_gateway_and_argus(
    env: Env, engine: str, bridge: bool
) -> None:
    env.state["engine"] = engine
    env.install("v0.7.0")
    r = env.run("worker", "setup", "-y")
    assert "New worker" in r.stdout
    assert {f"{HOST}:v0.7.0", f"{AGENT}:v0.7.0"} <= set(env.state["images"])
    root = nc_root(env)
    assert (root / "v0.7.0" / "src" / "dist" / "index.js").is_file()
    assert (root / "i").is_dir()
    assert list(root.glob(".v*")) == []  # no partial copy left
    cfg = env.config()
    assert (cfg["ARGUS_WORKERS"], cfg["ARGUS_DOCKER"]) == ("1", "1")

    gw = env.state["containers"]["argus-onecli"]["args"]
    assert gw[-1].startswith("ghcr.io/onecli/onecli:1.42.0@sha256:")
    assert "127.0.0.1:10254:10254" in gw
    assert gw[gw.index("--network-alias") + 1] == "onecli"  # the name Argus validates
    assert not any(
        p.endswith(":10254:10254") and not p.startswith("127.0.0.1:") for p in gw
    )
    assert ("172.17.0.1:10255:10255" in gw) is bridge
    db = env.state["containers"]["argus-onecli-db"]["args"]
    assert db[-1].startswith("postgres:18-alpine@sha256:") and "-p" not in db
    assert env.state["networks"] == ["argus-onecli-control"]
    secrets = env.home / "onecli.env"
    assert oct(secrets.stat().st_mode & 0o777) == "0o600"
    password = next(
        line.split("=", 1)[1]
        for line in secrets.read_text().splitlines()
        if line.startswith("POSTGRES_PASSWORD=")
    )

    args = env.run_args()
    assert f"{root}:{root}" in args
    e = arg_env(args)
    assert e["ARGUS_LAUNCHER"] == "1"
    assert e["ARGUS_NANOCLAW_INSTALL_DIR"] == f"{root}/v0.7.0/src"
    assert e["ARGUS_NANOCLAW_INSTANCES_ROOT"] == f"{root}/i"
    assert e["ARGUS_NANOCLAW_HOST_IMAGE"] == f"{HOST}:v0.7.0"
    assert e["ARGUS_NANOCLAW_AGENT_IMAGE"] == f"{AGENT}:v0.7.0"
    assert e["ARGUS_NANOCLAW_ONECLI_URL"] == "http://onecli:10254"
    assert e["ARGUS_NANOCLAW_GROUP_ADD"] == "998"
    assert password not in " ".join(args)  # gateway secrets never reach Argus


def test_worker_setup_without_worker_images_changes_nothing(env: Env) -> None:
    env.install("v0.7.0")
    env.state["missing_images"] = [HOST]
    r = env.run("worker", "setup", "-y", ok=False)
    assert "argus worker remove" in r.stderr or "argus update" in r.stderr
    assert env.config()["ARGUS_WORKERS"] == "0"
    assert "argus-onecli" not in env.state["containers"]
    assert not any(c[0] == "stop" for c in env.state["calls"])


def test_update_with_workers_prepares_first_and_prunes_after(env: Env) -> None:
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    env.state["calls"] = []
    env.run("update", "-y")
    calls = env.state["calls"]
    pull = calls.index(["pull", f"{AGENT}:v0.8.0"])
    assert pull < calls.index(["stop", "argus"])
    root = nc_root(env)
    assert sorted(p.name for p in root.iterdir()) == ["i", "v0.8.0"]
    assert arg_env(env.run_args())["ARGUS_NANOCLAW_INSTALL_DIR"] == f"{root}/v0.8.0/src"


def test_failed_update_with_workers_keeps_the_old_checkout_in_use(env: Env) -> None:
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    env.state["broken"] = ["v0.8.0"]
    env.run("update", "-y", ok=False)
    root = nc_root(env)
    assert (root / "v0.7.0" / "src").is_dir()
    assert arg_env(env.run_args())["ARGUS_NANOCLAW_INSTALL_DIR"] == f"{root}/v0.7.0/src"


def test_update_to_a_release_without_worker_images_stops_before_any_change(
    env: Env,
) -> None:
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    env.state["missing_images"] = [f"{HOST}:v0.8.0"]
    env.state["calls"] = []
    env.run("update", "-y", ok=False)
    assert not any(c[0] == "stop" for c in env.state["calls"])
    assert env.config()["ARGUS_VERSION"] == "v0.7.0"


@pytest.mark.parametrize("purge", [False, True])
def test_worker_remove_undoes_setup(env: Env, purge: bool) -> None:
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    env.state["volumes"]["argus-onecli-data"] = {"db": "", "files": []}
    env.run("worker", "remove", *(["--purge"] if purge else []))
    assert not {"argus-onecli", "argus-onecli-db"} & set(env.state["containers"])
    assert env.state["networks"] == []
    assert env.config()["ARGUS_WORKERS"] == "0"
    assert not any(k.startswith("ARGUS_NANOCLAW_") for k in arg_env(env.run_args()))
    assert nc_root(env).exists() is not purge
    assert (env.home / "onecli.env").exists() is not purge
    assert ("argus-onecli-data" in env.state["volumes"]) is not purge
    assert "argus-data" in env.state["volumes"]  # Argus's own data is never touched


def test_workers_need_docker_access(env: Env) -> None:
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    r = env.run("up", "--no-docker", ok=False)
    assert "argus worker remove" in r.stderr
    assert env.config()["ARGUS_DOCKER"] == "1"


def test_a_long_data_home_is_refused(env: Env) -> None:
    env.install("v0.7.0")
    long_home = env.data_home / ("x" * 60)
    cfg = (env.home / "argus.env").read_text()
    (env.home / "argus.env").write_text(cfg + f"ARGUS_DATA_HOME='{long_home}'\n")
    r = env.run("worker", "setup", "-y", ok=False)
    assert "56 characters" in r.stderr
    assert env.state["images"].count(f"{HOST}:v0.7.0") == 0


def test_restore_with_workers_uses_that_versions_workers(env: Env) -> None:
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    env.run("update", "-y")
    (backup,) = env.backups()
    # The v0.7.0 checkout was pruned by the update; restore brings it back first.
    env.state["calls"] = []
    env.run("restore", backup)
    calls = env.state["calls"]
    assert calls.index(["create", f"{HOST}:v0.7.0"]) < calls.index(["stop", "argus"])
    root = nc_root(env)
    assert arg_env(env.run_args())["ARGUS_NANOCLAW_INSTALL_DIR"] == f"{root}/v0.7.0/src"


# -- first run offers workers (local workers 3.7) -------------------------------


@pytest.mark.parametrize(("answer", "workers"), [("", True), ("y", True), ("n", False)])
def test_the_first_run_asks_once_whether_to_set_up_workers(
    env: Env, answer: str, workers: bool
) -> None:
    r = env.run("up", extra_env=env.answer(answer))
    assert "ROOT-EQUIVALENT" in r.stdout and "Set up workers?" in r.stdout
    assert env.config()["ARGUS_WORKERS"] == ("1" if workers else "0")
    assert ("argus-onecli" in env.state["containers"]) is workers
    assert ("ARGUS_NANOCLAW_INSTALL_DIR" in arg_env(env.run_args())) is workers
    starts = [c for c in env.state["calls"] if c[:2] == ["run", "-d"] and "argus" in c]
    assert len(starts) == 1  # Argus starts once, already with the worker settings
    r = env.run("up", extra_env=env.answer(answer))
    assert "Set up workers?" not in r.stdout


@pytest.mark.parametrize(
    ("flags", "workers"), [(["--workers"], "1"), (["--no-workers"], "0"), ([], "0")]
)
def test_the_first_run_without_a_terminal_follows_the_flag_or_says_no(
    env: Env, flags: list[str], workers: str
) -> None:
    r = env.run("up", *flags)
    assert "Set up workers?" not in r.stdout
    assert env.config()["ARGUS_WORKERS"] == workers
    if workers == "0":
        assert "argus worker setup" in r.stdout
        assert "/var/run/docker.sock:/var/run/docker.sock" not in env.run_args()


@pytest.mark.parametrize(
    ("flag", "command"), [("--workers", "setup"), ("--no-workers", "remove")]
)
def test_later_runs_change_workers_only_with_the_worker_commands(
    env: Env, flag: str, command: str
) -> None:
    env.install()
    r = env.run("up", flag, ok=False)
    assert f"argus worker {command}" in r.stderr


def test_a_failed_worker_step_still_starts_argus_without_workers(env: Env) -> None:
    env.state["missing_images"] = [HOST]
    r = env.run("up", extra_env=env.answer("y"), ok=False)
    assert "argus worker setup" in r.stderr
    assert env.state["containers"]["argus"]["running"]
    assert env.config()["ARGUS_WORKERS"] == "0"
    assert "ARGUS_NANOCLAW_INSTALL_DIR" not in arg_env(env.run_args())
    assert not (env.home / "lock").exists()


def test_a_machine_that_cannot_run_workers_is_told_instead_of_asked(env: Env) -> None:
    long_home = str(env.data_home / ("x" * 60))
    r = env.run("up", extra_env={**env.answer("y"), "XDG_DATA_HOME": long_home})
    assert "56 characters" in r.stdout and "Set up workers?" not in r.stdout
    assert env.config()["ARGUS_WORKERS"] == "0"


# -- uninstall (launcher 3.9) ---------------------------------------------------


def with_workers_and_two_versions(env: Env) -> None:
    """Workers set up, then an update: images and a backup of two versions."""
    env.install("v0.7.0")
    env.run("worker", "setup", "-y")
    env.run("update", "-y")
    env.state["containers"]["unrelated"] = {
        "image": "nginx:1",
        "running": True,
        "env": [],
        "mounts": [],
        "args": [],
    }
    env.state["volumes"]["unrelated-data"] = {"db": "", "files": []}
    env.state["images"].append("nginx:1")


@pytest.mark.parametrize("keep_data", [False, True])
def test_uninstall_removes_only_what_the_launcher_named(
    env: Env, keep_data: bool
) -> None:
    with_workers_and_two_versions(env)
    r = env.run("uninstall", "-y", *(["--keep-data"] if keep_data else []))
    assert "database" in r.stdout or keep_data
    assert set(env.state["containers"]) == {"unrelated"}
    assert env.state["networks"] == []
    assert env.state["images"] == ["nginx:1"]  # both versions, workers, gateway
    assert not nc_root(env).exists()
    assert not env.launcher.exists()
    kept = {"argus-data", "argus-onecli-data", "argus-onecli-pg"} & set(
        env.state["volumes"]
    )
    assert "unrelated-data" in env.state["volumes"]
    if not keep_data:
        assert kept == set() and not env.home.exists()
        return
    assert kept == {"argus-data", "argus-onecli-data", "argus-onecli-pg"}
    assert env.home.exists()
    # Reinstalling starts the same install, workers included.
    shutil.copy(LAUNCHER, env.launcher)
    env.run("up")
    assert env.config()["ARGUS_VERSION"] == "v0.8.0"
    assert "argus-onecli" in env.state["containers"]
    assert arg_env(env.run_args())["ARGUS_NANOCLAW_INSTALL_DIR"].endswith("/v0.8.0/src")


def test_uninstall_leaves_an_image_in_use_and_can_run_again(env: Env) -> None:
    env.install("v0.7.0")
    env.state["containers"]["unrelated"] = {
        "image": f"{IMAGE}:v0.7.0",
        "running": True,
        "env": [],
        "mounts": [],
        "args": [],
    }
    r = env.run("uninstall", "-y", ok=False)
    assert f"left image {IMAGE}:v0.7.0" in r.stderr
    assert "unrelated" in env.state["containers"]
    assert env.launcher.exists() and (env.home / "argus.env").exists()
    assert "argus-data" not in env.state["volumes"]
    del env.state["containers"]["unrelated"]
    env.run("uninstall", "-y")
    assert not env.launcher.exists() and not env.home.exists()


@pytest.mark.parametrize("answer", [None, "n"])
def test_uninstall_without_a_yes_removes_nothing(env: Env, answer: str | None) -> None:
    env.install()
    r = env.run("uninstall", extra_env=env.answer(answer) if answer else None, ok=False)
    assert "rerun with -y" in r.stderr or "cancelled" in r.stderr
    assert "argus-data" in r.stdout and "the age key go with it" in r.stdout
    assert not any(
        c[0] in ("stop", "rm", "rmi", "volume") and c[1:2] != ["inspect"]
        for c in env.state["calls"]
    )
    assert env.launcher.exists() and env.state["containers"]["argus"]["running"]


# -- fixture safety (TMPDIR / argus tree) ---------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _launcher_test_tmpdir_root() -> Generator[Path]:
    """Keep all launcher test I/O under one TMPDIR (never hard-coded /tmp or $HOME)."""
    raw = os.environ.get("TMPDIR")
    created = False
    if raw:
        root = Path(raw).resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="argus-lt-"))
        os.environ["TMPDIR"] = str(root)
        created = True
    root.mkdir(parents=True, exist_ok=True)
    yield root
    if created:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def outer_tmpdir(_launcher_test_tmpdir_root: Path) -> Path:
    return _launcher_test_tmpdir_root


@pytest.fixture
def isolated_tmpdir(
    outer_tmpdir: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Path]:
    """Real scratch under outer TMPDIR; monkeypatched for the test only."""
    uid = f"{os.getpid():x}{abs(hash(request.node.nodeid)) % 0xFFFF:04x}"
    candidate = outer_tmpdir / _LT_DIR / uid
    if _pick_data_home_path(candidate, "deadbeef") is not None:
        candidate.mkdir(parents=True, exist_ok=True)
        scratch = candidate
        remove_after = True
    else:
        scratch = outer_tmpdir
        remove_after = False
    monkeypatch.setenv("TMPDIR", str(scratch))
    try:
        yield scratch
    finally:
        if remove_after and _path_within_scratch(candidate, outer_tmpdir):
            shutil.rmtree(candidate, ignore_errors=True)


def _expect_foreign_argus_path(scratch: Path, tag: str) -> Path:
    assert _scratch_dir() == scratch.resolve()
    data_home = _resolved_data_home_for_tag(tag)
    assert _path_within_scratch(data_home, scratch)
    return data_home / "argus"


@pytest.mark.parametrize(
    ("tmpdir", "tag", "expected_rel"),
    [
        ("/ab", "deadbeef", f"{_DH_DIR}/deadbeef"),
        ("/" + "a" * 29, "deadbeef", f"{_DH_DIR}/dead"),
        ("/" + "a" * 32, "deadbeef", ""),
        ("/xy", "release-check", f"{_DH_DIR}/release-"),
        ("/" + "b" * 32, "release-check", ""),
    ],
)
def test_resolved_data_home_for_hypothetical_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmpdir: str, tag: str, expected_rel: str
) -> None:
    monkeypatch.setenv("TMPDIR", tmpdir)
    scratch = Path(tmpdir)
    got = _resolved_data_home_for_tag(tag)
    if expected_rel == "":
        assert got == scratch
    else:
        assert got == scratch / expected_rel


def _remove_foreign_argus(argus: Path, keep: Path, *, owned_marker: bool) -> None:
    keep.unlink(missing_ok=True)
    if owned_marker:
        (argus / _LAUNCHER_OWNED).unlink(missing_ok=True)
    if argus.is_dir() and not any(argus.iterdir()):
        argus.rmdir()
    elif argus.is_dir():
        pytest.fail(f"foreign argus tree not clean: {list(argus.iterdir())}")


@pytest.mark.parametrize("tag", ["deadbeef"])
def test_preexisting_argus_under_tmpdir_is_not_removed(
    isolated_tmpdir: Path, tag: str
) -> None:
    with _launcher_data_home_lock():
        argus = _expect_foreign_argus_path(isolated_tmpdir, tag)
        if argus.exists():
            pytest.fail(f"unexpected pre-existing {argus}")
        argus.mkdir(parents=True, exist_ok=True)
        keep = argus / "sentinel-do-not-delete"
        keep.write_text("precious", encoding="utf-8")
        try:
            with pytest.raises(Failed, match="launcher test marker"):
                _acquire_data_home(tag)
            assert keep.read_text(encoding="utf-8") == "precious"
        finally:
            _remove_foreign_argus(argus, keep, owned_marker=False)


@pytest.mark.parametrize("tag", ["deadbeef"])
def test_release_does_not_remove_a_foreign_owned_argus_tree(
    isolated_tmpdir: Path, tag: str
) -> None:
    with _launcher_data_home_lock():
        argus = _expect_foreign_argus_path(isolated_tmpdir, tag)
        if argus.exists():
            pytest.fail(f"unexpected pre-existing {argus}")
        argus.mkdir(parents=True, exist_ok=True)
        (argus / _LAUNCHER_OWNED).write_text("other-tag\n", encoding="utf-8")
        keep = argus / "sentinel-do-not-delete"
        keep.write_text("precious", encoding="utf-8")
        try:
            _release_data_home(argus, "my-tag", owned=True)
            assert keep.read_text(encoding="utf-8") == "precious"
        finally:
            _remove_foreign_argus(argus, keep, owned_marker=True)


def test_owned_argus_tree_is_removed_on_release(isolated_tmpdir: Path) -> None:
    tag = "release-check"
    with _launcher_data_home_lock():
        _expect_foreign_argus_path(isolated_tmpdir, tag)
        _data_home, argus, owned = _acquire_data_home(tag)
        assert owned
        probe = argus / "nanoclaw" / "probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("x", encoding="utf-8")
        _release_data_home(argus, tag, owned)
        assert not argus.exists()
