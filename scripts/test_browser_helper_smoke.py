"""Browser-helper validation assets: derivation, hardening, output rejection, lifecycle."""

import hashlib
import json
import subprocess
from pathlib import Path

import browser_helper_smoke as bh
import pytest

IMAGE = "sha256:" + "a" * 64


def _baseline() -> dict:
    return json.loads(bh.BASELINE.read_bytes())


def test_shipped_assets_match_provenance_and_derivation():
    prov = json.loads(bh.PROVENANCE.read_text())
    hashes = bh.check_assets()
    assert hashes["baseline"] == prov["baseline"]["sha256"]
    assert hashes["candidate"] == prov["candidate"]["sha256"]
    # The candidate is reproduced byte for byte from the pinned baseline.
    assert bh.dump(bh.derive(_baseline())) == bh.CANDIDATE.read_bytes()


def test_candidate_adds_one_rule_without_setns():
    base, cand = _baseline(), json.loads(bh.CANDIDATE.read_bytes())
    assert cand["syscalls"][:-1] == base["syscalls"]
    rule = cand["syscalls"][-1]
    assert rule["names"] == ["clone", "unshare", "chroot"]
    assert rule["action"] == "SCMP_ACT_ALLOW"
    assert "args" not in rule
    assert "setns" not in rule["names"]
    # the baseline's own setns rule stays capability-gated, so setns is still denied to cap-drop ALL
    assert all(
        r.get("includes", {}).get("caps")
        for r in cand["syscalls"][:-1]
        if "setns" in r["names"]
    )


@pytest.mark.parametrize(
    "names", [["clone", "setns"], ["setns"], [], ["clone", "mount"], ["bpf"]]
)
def test_derive_refuses_other_syscalls(names):
    with pytest.raises(bh.HelperError):
        bh.derive(_baseline(), names)


def _tamper(mutate) -> tuple[bytes, bytes]:
    cand = json.loads(bh.CANDIDATE.read_bytes())
    mutate(cand)
    return bh.BASELINE.read_bytes(), json.dumps(cand, indent="\t").encode()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["syscalls"][-1]["names"].append("setns"),
        lambda c: c["syscalls"][-1].update(
            args=[{"index": 0, "value": 1, "op": "SCMP_CMP_EQ"}]
        ),
        lambda c: c["syscalls"].append({"names": ["bpf"], "action": "SCMP_ACT_ALLOW"}),
        lambda c: c.update(defaultAction="SCMP_ACT_ALLOW"),
        lambda c: c["syscalls"].pop(0),
    ],
    ids=[
        "setns",
        "arg-filter",
        "second-rule",
        "default-allow",
        "baseline-rule-removed",
    ],
)
def test_check_candidate_rejects_tampering(mutate):
    with pytest.raises(bh.HelperError):
        bh.check_candidate(*_tamper(mutate))


def test_check_assets_rejects_hash_drift(tmp_path):
    for p in (bh.HELPER / "seccomp").iterdir():
        (tmp_path / "seccomp").mkdir(exist_ok=True)
        (tmp_path / "seccomp" / p.name).write_bytes(p.read_bytes())
    (tmp_path / "provenance.json").write_bytes(bh.PROVENANCE.read_bytes())
    assert bh.check_assets(tmp_path)
    (tmp_path / "seccomp" / "moby-default.json").write_bytes(
        bh.BASELINE.read_bytes() + b" "
    )
    with pytest.raises(bh.HelperError, match="pinned"):
        bh.check_assets(tmp_path)


@pytest.mark.parametrize(
    "image",
    [
        "debian:latest",
        "argus-project-runtime:argus-x",
        "sha256:abc",
        "",
        "x@sha256:short",
        "--privileged",
    ],
)
def test_image_must_be_a_digest(image):
    with pytest.raises(bh.HelperError):
        bh.check_image(image)


def test_image_accepts_id_and_repo_digest():
    assert bh.check_image(IMAGE)
    assert bh.check_image("registry.example/app@sha256:" + "b" * 64)


def test_docker_argv_is_hardened(tmp_path):
    argv = bh.docker_argv(IMAGE, tmp_path, "argus-browser-smoke-" + "0" * 16)
    bh.assert_hardened(argv)
    assert argv.index(IMAGE) > argv.index("--entrypoint=/bin/sh")
    assert f"--security-opt=seccomp={bh.CANDIDATE}" in argv


@pytest.mark.parametrize(
    "extra",
    [
        "--privileged",
        "--cap-add=SYS_ADMIN",
        "--security-opt=seccomp=unconfined",
        "--security-opt=apparmor=unconfined",
        "--network=host",
        "--ipc=host",
        "--device=/dev/fuse",
        "--no-sandbox",
        "/var/run/docker.sock:/s",
    ],
)
def test_assert_hardened_rejects_forbidden(tmp_path, extra):
    argv = bh.docker_argv(IMAGE, tmp_path, "argus-browser-smoke-" + "0" * 16)
    with pytest.raises(bh.HelperError, match="forbidden"):
        bh.assert_hardened([*argv, extra])


@pytest.mark.parametrize("flag", list(bh.REQUIRED_ARGV))
def test_assert_hardened_rejects_missing_protection(tmp_path, flag):
    argv = bh.docker_argv(IMAGE, tmp_path, "argus-browser-smoke-" + "0" * 16)
    with pytest.raises(bh.HelperError, match="missing"):
        bh.assert_hardened([a for a in argv if a != flag])


def test_smoke_script_never_disables_the_sandbox():
    text = bh.SMOKE.read_text()
    launch = text[
        text.index("const args = [") : text.index("];", text.index("const args = ["))
    ]
    for flag in (
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-namespace-sandbox",
    ):
        assert flag not in launch
    assert "/home/" not in text


def test_out_dir_rules(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fresh = bh.prepare_out_dir(str(tmp_path / "out"), repo)
    assert fresh.is_dir()
    assert bh.prepare_out_dir(str(fresh), repo) == fresh  # empty is fine
    (fresh / "stale").write_text("x")
    for bad, why in [
        (str(fresh), "new or empty"),
        ("relative/out", "absolute"),
        (str(repo / "out"), "outside the repository"),
        (str(repo), "outside the repository"),
        (str(tmp_path), "outside the repository"),
        (str(tmp_path / "a:b"), "must not contain"),
        (str(tmp_path / "missing" / "out"), "parent"),
    ]:
        with pytest.raises(bh.HelperError, match=why):
            bh.prepare_out_dir(bad, repo)
    # an existing empty directory owned by someone else is refused, never chmod 0777
    other = tmp_path / "other-owner"
    other.mkdir()
    monkeypatch.setattr(bh.os, "getuid", lambda: other.stat().st_uid + 1)
    with pytest.raises(bh.HelperError, match="owned by the current user"):
        bh.prepare_out_dir(str(other), repo)
    assert (other.stat().st_mode & 0o777) != 0o777
    monkeypatch.undo()
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(bh.HelperError, match="symlink"):
        bh.prepare_out_dir(str(link), repo)


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), -1, 0, 0.0, bh.MAX_TIMEOUT + 1, 10**9, "5", None],
)
def test_timeout_must_be_finite_and_bounded(bad):
    with pytest.raises(bh.HelperError):
        bh.check_timeout(bad)


def test_timeout_accepts_the_bounds():
    assert bh.check_timeout(bh.DEFAULT_TIMEOUT) == bh.DEFAULT_TIMEOUT
    assert bh.check_timeout(bh.MAX_TIMEOUT) == bh.MAX_TIMEOUT


def test_host_uid_must_be_1000(monkeypatch):
    monkeypatch.setattr(bh.os, "getuid", lambda: 1001)
    with pytest.raises(bh.HelperError, match="uid 1000"):
        bh.require_host_uid_1000()
    monkeypatch.setattr(bh.os, "getuid", lambda: 1000)
    bh.require_host_uid_1000()


# ------------------------------------------------------------ output rejection


def _good_output(out: Path) -> dict:
    png = bh.PNG_MAGIC + b"x" * 2000
    dl = bh.SERVED_DOWNLOAD
    (out / "downloads").mkdir(parents=True)
    (out / "screenshot.png").write_bytes(png)
    (out / "downloads" / "dl.bin").write_bytes(dl)
    result = {
        "assertions": [{"name": n, "ok": True} for n in bh.REQUIRED_ASSERTIONS],
        "all_pass": True,
        "steps": {
            "screenshot": {"sha256": hashlib.sha256(png).hexdigest()},
            "download": {"expected_sha256": hashlib.sha256(dl).hexdigest()},
        },
    }
    (out / "result.json").write_text(json.dumps(result))
    return result


def _rewrite(out: Path, mutate) -> None:
    result = json.loads((out / "result.json").read_text())
    mutate(result)
    (out / "result.json").write_text(json.dumps(result))


def test_verify_output_accepts_complete_pass(tmp_path):
    _good_output(tmp_path)
    assert bh.verify_output(tmp_path)["all_pass"] is True


def _fail_one(r):
    r["assertions"][3]["ok"] = False


def _drop_one(r):
    r["assertions"].pop()


def _truthy_string(r):
    r["assertions"][0]["ok"] = "true"


def _all_pass_false(r):
    r["all_pass"] = False


def _error(r):
    r["error"] = "boom"


def _wrong_shot_hash(r):
    r["steps"]["screenshot"]["sha256"] = "0" * 64


def _wrong_dl_hash(r):
    r["steps"]["download"]["expected_sha256"] = "0" * 64


def _duplicate_name(r):
    r["assertions"].append(dict(r["assertions"][0]))


@pytest.mark.parametrize(
    "mutate",
    [
        _fail_one,
        _drop_one,
        _truthy_string,
        _all_pass_false,
        _error,
        _wrong_shot_hash,
        _wrong_dl_hash,
        _duplicate_name,
    ],
)
def test_verify_output_rejects(tmp_path, mutate):
    _good_output(tmp_path)
    _rewrite(tmp_path, mutate)
    with pytest.raises(bh.HelperError):
        bh.verify_output(tmp_path)


def test_verify_output_checks_the_served_constant_not_self_reported_hash(tmp_path):
    # a self-consistent but wrong download (e.g. an error page) must not pass
    _good_output(tmp_path)
    other = b"<html>error</html>"
    (tmp_path / "downloads" / "dl.bin").write_bytes(other)
    _rewrite(
        tmp_path,
        lambda r: r["steps"]["download"].update(
            expected_sha256=hashlib.sha256(other).hexdigest()
        ),
    )
    with pytest.raises(bh.HelperError, match="known served"):
        bh.verify_output(tmp_path)


@pytest.mark.parametrize(
    "remove", ["result.json", "screenshot.png", "downloads/dl.bin"]
)
def test_verify_output_rejects_missing_artefacts(tmp_path, remove):
    _good_output(tmp_path)
    (tmp_path / remove).unlink()
    with pytest.raises(bh.HelperError):
        bh.verify_output(tmp_path)


def test_verify_output_rejects_non_png_and_symlinks(tmp_path):
    _good_output(tmp_path)
    (tmp_path / "screenshot.png").write_bytes(b"not a png")
    with pytest.raises(bh.HelperError):
        bh.verify_output(tmp_path)
    _good_output_again = tmp_path / "other"
    _good_output_again.mkdir()
    _good_output(_good_output_again)
    (_good_output_again / "link").symlink_to("/etc/passwd")
    with pytest.raises(bh.HelperError, match="symlink"):
        bh.verify_output(_good_output_again)


# -------------------------------------------------------------------- lifecycle


class _FakeProc:
    def __init__(self, hang: bool, rc: int = 0):
        self.hang, self.returncode, self.killed = hang, rc, False

    def communicate(self, timeout=None):
        if self.hang and not self.killed and timeout is not None:
            raise subprocess.TimeoutExpired("docker", timeout)
        return "", ""

    def poll(self):
        return None if self.hang and not self.killed else self.returncode

    def kill(self):
        self.killed = True


def _patch_docker(monkeypatch, proc: _FakeProc) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args, timeout=30):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, IMAGE + "\n" if args[0] == "image" else "", ""
        )

    monkeypatch.setattr(bh, "_docker", fake_docker)
    monkeypatch.setattr(bh, "require_host_uid_1000", lambda: None)
    monkeypatch.setattr(bh.subprocess, "Popen", lambda *a, **k: proc)
    return calls


def test_timeout_kills_and_removes_only_the_named_container(tmp_path, monkeypatch):
    proc = _FakeProc(hang=True)
    calls = _patch_docker(monkeypatch, proc)
    with pytest.raises(bh.HelperError, match="exceeded"):
        bh.run_smoke(IMAGE, str(tmp_path / "out"), timeout=0.01)
    kills = [c for c in calls if c[0] == "kill"]
    removes = [c for c in calls if c[0] == "rm"]
    assert proc.killed and len(kills) == 1 and len(removes) == 1
    assert kills[0][1] == removes[0][2] and kills[0][1].startswith(
        "argus-browser-smoke-"
    )
    assert removes[0][1] == "-f"


def test_nonzero_exit_fails_and_still_removes(tmp_path, monkeypatch):
    calls = _patch_docker(monkeypatch, _FakeProc(hang=False, rc=3))
    with pytest.raises(bh.HelperError, match="exited 3"):
        bh.run_smoke(IMAGE, str(tmp_path / "out"))
    assert [c[0] for c in calls].count("rm") == 1


def test_receipt_is_written_on_failure(tmp_path, monkeypatch):
    _patch_docker(monkeypatch, _FakeProc(hang=False, rc=3))
    out = tmp_path / "out"
    with pytest.raises(bh.HelperError):
        bh.run_smoke(IMAGE, str(out))
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["status"] == "fail" and receipt["returncode"] == 3
    assert receipt["image_resolved_id"] == IMAGE and receipt["timed_out"] is False
    assert "--cap-drop=ALL" in receipt["argv"] and "exited 3" in receipt["error"]


def test_receipt_records_timeout(tmp_path, monkeypatch):
    _patch_docker(monkeypatch, _FakeProc(hang=True))
    out = tmp_path / "out"
    with pytest.raises(bh.HelperError):
        bh.run_smoke(IMAGE, str(out), timeout=0.01)
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["timed_out"] is True and receipt["timeout_s"] == 0.01
    assert receipt["status"] == "fail"


def test_receipt_records_pass(tmp_path, monkeypatch):
    _patch_docker(monkeypatch, _FakeProc(hang=False, rc=0))
    out = tmp_path / "out"
    monkeypatch.setattr(bh, "verify_output", lambda d: {"assertions": []})
    bh.run_smoke(IMAGE, str(out))
    assert json.loads((out / "receipt.json").read_text())["status"] == "pass"


def test_invalid_timeout_has_no_side_effects(tmp_path, monkeypatch):
    calls = _patch_docker(monkeypatch, _FakeProc(hang=False))
    with pytest.raises(bh.HelperError):
        bh.run_smoke(IMAGE, str(tmp_path / "out"), timeout=float("nan"))
    assert calls == [] and not (tmp_path / "out").exists()


def test_resolved_image_id_must_match_an_id_request(tmp_path, monkeypatch):
    def fake_docker(*args, timeout=30):
        return subprocess.CompletedProcess(args, 0, "sha256:" + "c" * 64 + "\n", "")

    monkeypatch.setattr(bh, "_docker", fake_docker)
    monkeypatch.setattr(bh, "require_host_uid_1000", lambda: None)
    with pytest.raises(bh.HelperError, match="mismatch"):
        bh.run_smoke(IMAGE, str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_missing_image_is_not_pulled(tmp_path, monkeypatch):
    def fake_docker(*args, timeout=30):
        return subprocess.CompletedProcess(args, 1, "", "no such image")

    monkeypatch.setattr(bh, "_docker", fake_docker)
    monkeypatch.setattr(bh, "require_host_uid_1000", lambda: None)
    with pytest.raises(bh.HelperError, match="not available locally"):
        bh.run_smoke(IMAGE, str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()
