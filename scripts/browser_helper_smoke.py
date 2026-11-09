#!/usr/bin/env python3
"""Opt-in validation of a dedicated, sandbox-ON Chromium browser-helper container.

Spec: docs/specs/browser-helper-validation.spec.md. Standard library only.

Commands:
  check                      verify the shipped seccomp assets against deploy/browser-helper/provenance.json
  derive OUT                 write the candidate profile derived from the pinned baseline (OUT must not exist)
  run --image IMG --out DIR  run deploy/browser-helper/smoke.mjs in a hardened disposable container and
                             verify its artefacts on the host; DIR must be a fresh directory outside the repo

This validates assets only. Nothing here wires a browser into workers, and the candidate profile is not
security-approved: it permits unprivileged user namespaces for the container that uses it.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "deploy" / "browser-helper"
BASELINE = HELPER / "seccomp" / "moby-default.json"
CANDIDATE = HELPER / "seccomp" / "chromium-userns.json"
SMOKE = HELPER / "smoke.mjs"
PROVENANCE = HELPER / "provenance.json"

# The one rule appended to the baseline. `setns` is deliberately absent.
ADDED_NAMES = ["clone", "unshare", "chroot"]
ADDED_COMMENT = (
    "argus: Chromium namespace sandbox; unshare/setns create/enter its OWN userns, "
    "chroot runs with namespace-local CAP_SYS_CHROOT"
)
FORBIDDEN_ADDED = {"setns"}

# Digest-pinned images only: an image ID or name@sha256:digest. Tags move.
IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[A-Za-z0-9][A-Za-z0-9_./:-]*@sha256:[0-9a-f]{64})$"
)
NAME_RE = re.compile(r"^argus-browser-smoke-[0-9a-f]{16}$")

# Assertions smoke.mjs must report as ok; a missing one is a failure.
REQUIRED_ASSERTIONS = (
    "dom_js",
    "screenshot_png",
    "download_hash",
    "chrome_sandbox_adequate",
    "have_browser_renderer_zygote",
    "every_proc_has_argv",
    "all_live_procs_inspected",
    "launch_argv_no_bypass_flag",
    "no_bypass_flag_in_any_argv",
    "all_uid_gid_1000",
    "all_apparmor_enforce",
    "all_nonewprivs_seccomp2",
    "container_userns_procs_zero_caps",
    "nested_userns_exist",
    "renderers_nested_zero_caps",
    "nested_caps_only_sys_chroot_bit21",
    "nested_ns_maps_single_uid_1000_to_inner",
    "outer_userns_map_recorded",
    "no_sandbox_stderr_errors",
)
PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
IN_CONTAINER_TIMEOUT = 90
DEFAULT_TIMEOUT = 150
MAX_TIMEOUT = 600
SERVED_DOWNLOAD = b"ARGUS_DOWNLOAD_SMOKE\n" * 50  # the bytes smoke.mjs serves
SERVED_SHA256 = hashlib.sha256(SERVED_DOWNLOAD).hexdigest()
RECEIPT_TAIL = 65536

# Options the container must have, and values it must never have.
REQUIRED_ARGV = (
    "--user=1000:1000",
    "--cap-drop=ALL",
    "--network=none",
    "--read-only",
    "--ipc=private",
    "--pull=never",
    "--security-opt=no-new-privileges",
    "--security-opt=apparmor=docker-default",
    "--memory=2g",
    "--memory-swap=2g",
    "--pids-limit=512",
    "--cpus=2",
    "--ulimit=core=0",
    "--shm-size=1024m",
    "--tmpfs=/tmp:rw,exec,nosuid,size=512m",
)
FORBIDDEN_ARGV = (
    "--privileged",
    "--cap-add",
    "--device",
    "--pid=host",
    "--ipc=host",
    "--network=host",
    "--userns=host",
    "seccomp=unconfined",
    "apparmor=unconfined",
    "systempaths=unconfined",
    "label=disable",
    "SYS_ADMIN",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "/var/run/docker.sock",
)


class HelperError(Exception):
    """An asset, argument or artefact failed validation."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- seccomp assets


def derive(baseline: dict, names: list[str] | None = None) -> dict:
    """The baseline plus exactly one appended unconditional allow rule."""
    names = list(ADDED_NAMES if names is None else names)
    if FORBIDDEN_ADDED & set(names) or not names:
        raise HelperError(
            f"refusing to add {sorted(FORBIDDEN_ADDED & set(names)) or 'no syscalls'}"
        )
    if set(names) - set(ADDED_NAMES):
        raise HelperError(f"only {ADDED_NAMES} may be added, got {names}")
    out = copy.deepcopy(baseline)
    out["syscalls"].append(
        {"names": names, "action": "SCMP_ACT_ALLOW", "comment": ADDED_COMMENT}
    )
    return out


def dump(profile: dict) -> bytes:
    """The serialisation the shipped candidate uses (tabs, no trailing newline)."""
    return json.dumps(profile, indent="\t").encode()


def check_candidate(baseline_raw: bytes, candidate_raw: bytes) -> None:
    """The candidate is the baseline plus the one expected rule, nothing else."""
    base, cand = json.loads(baseline_raw), json.loads(candidate_raw)
    if cand["syscalls"][:-1] != base["syscalls"] or {
        k: v for k, v in cand.items() if k != "syscalls"
    } != {k: v for k, v in base.items() if k != "syscalls"}:
        raise HelperError(
            "candidate differs from the baseline by more than one appended rule"
        )
    rule = cand["syscalls"][-1]
    if (
        rule.get("names") != ADDED_NAMES
        or rule.get("action") != "SCMP_ACT_ALLOW"
        or set(rule) - {"names", "action", "comment"}
    ):
        raise HelperError(
            f"appended rule is not an unconditional allow of {ADDED_NAMES}: {rule}"
        )
    if candidate_raw != dump(derive(base)):
        raise HelperError("candidate is not byte-identical to the derivation")


def check_assets(helper: Path = HELPER) -> dict[str, str]:
    """Hashes of the shipped assets against provenance.json, then the one-rule check."""
    prov = json.loads((helper / "provenance.json").read_text())
    hashes = {}
    for key in ("baseline", "candidate"):
        raw = (helper / prov[key]["file"]).read_bytes()
        hashes[key] = sha256_bytes(raw)
        if hashes[key] != prov[key]["sha256"]:
            raise HelperError(
                f"{prov[key]['file']} sha256 {hashes[key]} != pinned {prov[key]['sha256']}"
            )
    check_candidate(
        (helper / prov["baseline"]["file"]).read_bytes(),
        (helper / prov["candidate"]["file"]).read_bytes(),
    )
    return hashes


# ------------------------------------------------------------- inputs and argv


def check_image(image: str) -> str:
    if not IMAGE_RE.fullmatch(image):
        raise HelperError(
            f"image must be an image ID (sha256:…) or name@sha256:digest, got {image!r}"
        )
    return image


def require_host_uid_1000() -> None:
    """The container writes artefacts as uid 1000, so the host user must be uid 1000."""
    if os.getuid() != 1000:
        raise HelperError(
            f"run as host uid 1000 (the container user); current uid is {os.getuid()}"
        )


def check_timeout(timeout: float) -> float:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise HelperError(f"timeout must be a finite number, got {timeout!r}")
    if not 0 < timeout <= MAX_TIMEOUT:
        raise HelperError(
            f"timeout must be in (0, {MAX_TIMEOUT}] seconds, got {timeout}"
        )
    return float(timeout)


def prepare_out_dir(path: str, repo_root: Path = ROOT) -> Path:
    """A fresh absolute directory outside the repo, owned by the caller, mode 0700."""
    raw = Path(path)
    if not raw.is_absolute():
        raise HelperError("output directory must be an absolute path")
    if set(path) & set(":,\n\0"):
        raise HelperError(
            "output directory path must not contain ':' ',' or control characters"
        )
    if raw.is_symlink():
        raise HelperError("output directory must not be a symlink")
    resolved = raw.resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents or resolved in root.parents:
        raise HelperError("output directory must be outside the repository")
    if not resolved.parent.is_dir():
        raise HelperError("parent of the output directory must exist")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise HelperError("output directory must be new or empty")
        if resolved.stat().st_uid != os.getuid():
            raise HelperError(
                "an existing output directory must be owned by the current user"
            )
        resolved.chmod(0o700)
    else:
        resolved.mkdir(mode=0o700)
    return resolved


def docker_argv(
    image: str, out_dir: Path, name: str, seccomp: Path = CANDIDATE
) -> list[str]:
    """The `docker run` command. Pure, so tests can inspect every flag."""
    check_image(image)
    if not NAME_RE.fullmatch(name):
        raise HelperError(f"bad container name {name!r}")
    mounts = {"smoke": SMOKE.resolve(), "seccomp": seccomp.resolve(), "out": out_dir}
    if any(set(str(p)) & set(":,\n\0") for p in mounts.values()):
        raise HelperError(
            "paths used in mounts must not contain ':' ',' or control characters"
        )
    inner = (
        "mkdir -p /tmp/home /tmp/config /out/downloads; "
        f"exec timeout -k 5 {IN_CONTAINER_TIMEOUT} node /probe/smoke.mjs /out"
    )
    return [
        "docker", "run", "--rm", f"--name={name}", "--label=argus.browser-helper-smoke=1",
        "--pull=never",
        "--user=1000:1000", "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={mounts['seccomp']}",
        "--security-opt=apparmor=docker-default",
        "--network=none", "--read-only", "--ipc=private",
        "--tmpfs=/tmp:rw,exec,nosuid,size=512m", "--shm-size=1024m",
        "--memory=2g", "--memory-swap=2g", "--pids-limit=512", "--cpus=2", "--ulimit=core=0",
        "-e", "HOME=/tmp/home", "-e", "XDG_CONFIG_HOME=/tmp/config",
        "-v", f"{mounts['smoke']}:/probe/smoke.mjs:ro",
        "-v", f"{out_dir}:/out:rw",
        "--entrypoint=/bin/sh", image, "-c", inner,
    ]  # fmt: skip


def assert_hardened(argv: list[str]) -> None:
    """Refuse an argv missing a required protection or carrying a forbidden one."""
    missing = [a for a in REQUIRED_ARGV if a not in argv]
    if missing:
        raise HelperError(f"hardening missing: {missing}")
    bad = [f for f in FORBIDDEN_ARGV if any(f in a for a in argv)]
    if bad:
        raise HelperError(f"forbidden option present: {bad}")
    if not any(
        a.startswith("--security-opt=seccomp=") and a.endswith(".json") for a in argv
    ):
        raise HelperError("a seccomp profile file is required")


# ---------------------------------------------------------------- output checks


def verify_output(out_dir: Path) -> dict:
    """Independent host-side checks of the artefacts; raises on anything short of a full pass."""
    for p in out_dir.rglob("*"):
        if p.is_symlink():
            raise HelperError(f"symlink in output: {p.name}")
    try:
        result = json.loads((out_dir / "result.json").read_text())
    except (OSError, ValueError) as e:
        raise HelperError(f"result.json missing or invalid: {e}") from e
    if result.get("error"):
        raise HelperError(f"smoke reported an error: {str(result['error'])[:200]}")
    by_name = {a.get("name"): a for a in result.get("assertions", [])}
    names = [a.get("name") for a in result.get("assertions", [])]
    if len(names) != len(set(names)):
        raise HelperError("duplicate assertion names")
    absent = [n for n in REQUIRED_ASSERTIONS if n not in by_name]
    if absent:
        raise HelperError(f"assertions missing: {absent}")
    failed = [n for n, a in by_name.items() if a.get("ok") is not True]
    if failed or result.get("all_pass") is not True:
        raise HelperError(f"assertions failed: {failed or ['all_pass is not true']}")
    shot = (
        (out_dir / "screenshot.png").read_bytes()
        if (out_dir / "screenshot.png").is_file()
        else b""
    )
    if (
        shot[:8] != PNG_MAGIC
        or sha256_bytes(shot) != result["steps"]["screenshot"]["sha256"]
    ):
        raise HelperError("screenshot.png is not the PNG the result describes")
    dl = result["steps"]["download"]
    downloads = out_dir / "downloads"
    hashes = (
        [sha256_bytes(f.read_bytes()) for f in downloads.iterdir()]
        if downloads.is_dir()
        else []
    )
    if dl.get("expected_sha256") != SERVED_SHA256 or SERVED_SHA256 not in hashes:
        raise HelperError("no downloaded file matches the known served bytes")
    return result


# --------------------------------------------------------------------- running


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return (text or "")[-RECEIPT_TAIL:]


def run_smoke(image: str, out: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run the smoke in a disposable container; the container is always removed afterwards.

    `receipt.json` in the output directory records argv, resolved image ID, return code, bounded
    stdout/stderr and the timeout outcome, whether the run passes or fails.
    """
    timeout = check_timeout(timeout)
    require_host_uid_1000()
    check_assets()
    check_image(image)
    inspect = _docker("image", "inspect", "--format", "{{.Id}}", image)
    if inspect.returncode != 0:
        raise HelperError(f"image not available locally (nothing is pulled): {image}")
    resolved_id = inspect.stdout.strip()
    if image.startswith("sha256:") and resolved_id != image:
        raise HelperError(
            f"image ID mismatch: asked {image}, docker resolved {resolved_id}"
        )
    out_dir = prepare_out_dir(out)
    name = f"argus-browser-smoke-{secrets.token_hex(8)}"
    argv = docker_argv(image, out_dir, name)
    assert_hardened(argv)
    receipt: dict = {
        "argv": argv,
        "image": image,
        "image_resolved_id": resolved_id,
        "timeout_s": timeout,
        "timed_out": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "status": "fail",
        "error": None,
    }
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            receipt["timed_out"] = True
            raise HelperError(
                f"smoke exceeded {timeout}s; container {name} removed"
            ) from None
        receipt.update(
            returncode=proc.returncode, stdout=_tail(stdout), stderr=_tail(stderr)
        )
        if proc.returncode != 0:
            raise HelperError(f"docker run exited {proc.returncode}: {stderr[-500:]}")
        result = verify_output(out_dir)
        receipt["status"] = "pass"
        return result
    except BaseException as e:
        receipt["error"] = f"{type(e).__name__}: {e}"[:1000]
        raise
    finally:
        # Only the uniquely named container this call started.
        if proc.poll() is None:
            _docker("kill", name)
            proc.kill()
        try:
            stdout, stderr = proc.communicate()
        except ValueError:
            stdout = stderr = ""
        if receipt["returncode"] is None:
            receipt.update(
                returncode=proc.returncode, stdout=_tail(stdout), stderr=_tail(stderr)
            )
        _docker("rm", "-f", name)
        (out_dir / "receipt.json").write_text(json.dumps(receipt, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("derive")
    d.add_argument("out")
    r = sub.add_parser("run")
    r.add_argument(
        "--image", required=True, help="image ID or name@sha256:digest (no default)"
    )
    r.add_argument(
        "--out", required=True, help="fresh absolute directory outside the repo"
    )
    r.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            print(json.dumps(check_assets(), indent=2))
        elif args.cmd == "derive":
            target = Path(args.out)
            data = dump(derive(json.loads(BASELINE.read_bytes())))
            with target.open("xb") as f:  # never overwrites
                f.write(data)
            print(f"wrote {target} sha256={sha256_bytes(data)}")
        else:
            result = run_smoke(args.image, args.out, args.timeout)
            print(
                f"PASS {len(result['assertions'])} assertions; artefacts in {args.out}"
            )
    except (HelperError, OSError, subprocess.SubprocessError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
