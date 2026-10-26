"""WIP preservation for workspace cleanup (workspace-cleanup.spec.md 3.5).

Finds git repositories under an instance directory without following symlinks,
classifies what is unpublished, and writes a verified archive (bundle, binary
patches, untracked tar, manifest) outside the instances root. Synchronous: the
caller runs it in a worker thread. Errors carry a reason code, never file
contents or git output.
"""

import errno
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict

# Directory names that are rebuilt from sources (exact names, nothing else).
REGENERABLE = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".turbo",
        "target",
        "dist",
        "build",
    }
)
SENSITIVE_DIRS = (".ssh", ".aws", ".gnupg", ".config/gh", ".docker", "secrets")
SENSITIVE_FILES = (
    ".env*",
    "*.pem",
    "*.key",
    "id_*",
    ".netrc",
    ".git-credentials",
    "auth.json",
    "credentials*",
    ".credentials.json",
)
MAX_SCAN_DEPTH = 64
MAX_REPOS = 50
MAX_SCAN_ENTRIES = 1_000_000
MAX_UNTRACKED_FILES = 100_000
MAX_GITFILE_BYTES = 4096
# Where NanoClaw mounts an instance directory inside its container. Gitfiles
# written in the container name this prefix; it maps to the scanned directory.
CONTAINER_MOUNT = "/workspace/agent"
CHUNK = 1024 * 1024
RepoKind = Literal["worktree", "bare"]


class ArchiveError(Exception):
    """A preservation step failed; `code` is the audit reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def is_sensitive(rel: str) -> bool:
    parts = rel.strip("/").split("/")
    joined = "/".join(parts)
    for d in SENSITIVE_DIRS:
        if joined == d or joined.startswith(d + "/") or f"/{d}/" in f"/{joined}/":
            return True
    return any(fnmatch.fnmatchcase(parts[-1], p) for p in SENSITIVE_FILES)


def _pathspec_excludes() -> list[str]:
    specs = [f":(exclude,glob)**/{p}" for p in SENSITIVE_FILES]
    specs += [f":(exclude,glob)**/{d}/**" for d in SENSITIVE_DIRS]
    return specs


def sanitize(exc: BaseException) -> str:
    """Exception class and errno name only: never a message that may carry data."""
    name = type(exc).__name__
    code = getattr(exc, "errno", None)
    if isinstance(code, int) and code in errno.errorcode:
        return f"{name}: {errno.errorcode[code]}"
    return name


def classify_os_error(exc: OSError) -> str:
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return "archive_no_space"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "archive_permission"
    return "archive_error"


class Git:
    """Runs git against untrusted repositories with hooks, fsmonitor, external
    diff and textconv disabled; output is only parsed, never logged."""

    def __init__(self, binary: str = "git", timeout_s: float = 300.0) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        self.deadline: float | None = None  # monotonic; bounds every call

    def _timeout(self) -> float:
        if self.deadline is None:
            return self.timeout_s
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ArchiveError("archive_timeout")
        return min(self.timeout_s, left)

    def _argv(
        self,
        args: list[str],
        git_dir: Path | None,
        cwd: Path | None,
        work_tree: Path | None,
    ) -> list[str]:
        argv = [
            self.binary,
            "--no-optional-locks",
            "-c",
            "safe.directory=*",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.quotePath=false",
        ]
        if git_dir is not None:
            argv += ["--git-dir", str(git_dir)]
        if work_tree is not None:
            argv += ["--work-tree", str(work_tree)]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        return argv + args

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
        return env

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        ok: tuple[int, ...] = (0,),
    ) -> tuple[int, bytes]:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, no shell
                self._argv(args, git_dir, cwd, work_tree),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout(),
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ArchiveError("git_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArchiveError("archive_timeout") from exc
        except OSError as exc:
            raise ArchiveError("git_missing", sanitize(exc)) from exc
        if proc.returncode not in ok:
            raise ArchiveError("git_error", f"exit {proc.returncode}")
        return proc.returncode, proc.stdout

    def to_file(
        self,
        args: list[str],
        out: BinaryIO,
        *,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> None:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, no shell
                self._argv(args, git_dir, cwd, work_tree),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout(),
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ArchiveError("git_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArchiveError("archive_timeout") from exc
        if proc.returncode != 0:
            raise ArchiveError("git_error", f"exit {proc.returncode}")


@dataclass
class Artifact:
    name: str
    bytes: int
    sha256: str


@dataclass
class RepoScan:
    rel: str  # relative to the instance directory; "." for the top
    kind: RepoKind
    # A worktree's git and common directories, relative to the instance
    # directory (a gitfile's target is resolved and remapped by find_repos).
    git_dir: str | None = None
    common_dir: str | None = None
    head: str | None = None
    branch: str | None = None
    remote_refs: dict[str, str] = field(default_factory=dict)
    local_commits: int = 0
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    untracked_bytes: int = 0
    skipped_sensitive: list[str] = field(default_factory=list)
    sensitive_in_commits: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)

    @property
    def has_wip(self) -> bool:
        # Sensitive-only changes still get a manifest that lists what was skipped.
        return bool(
            self.local_commits
            or self.staged
            or self.unstaged
            or self.untracked
            or self.skipped_sensitive
        )


@dataclass
class Scan:
    repos: list[RepoScan]
    cache_dirs: list[str]  # relative paths of allowlisted regenerable directories

    @property
    def has_wip(self) -> bool:
        return any(r.has_wip for r in self.repos)


def _join(rel: str, name: str) -> str:
    return name if rel == "." else f"{rel}/{name}"


def find_repos(
    base: Path, mount: str | None = None
) -> tuple[list[RepoScan], list[str]]:
    """Every repository under `base` (location fields only) and the allowlisted
    cache directories. Each directory is opened with O_NOFOLLOW and must be the
    inode its parent listed, on `base`'s device. A regenerable name counts as a
    cache only inside a repository's working tree; elsewhere it is walked like
    any directory, so a repository under `build/` is still found. A `.git` file
    is resolved inside `base`, `mount` (default CONTAINER_MOUNT) mapping to it."""
    mount = CONTAINER_MOUNT if mount is None else mount
    try:
        top = os.lstat(base)
    except OSError as exc:
        raise ArchiveError("unreadable", sanitize(exc)) from exc
    repos: list[RepoScan] = []
    caches: list[str] = []
    seen = 0
    stack = [(str(base), ".", 0, False, (top.st_dev, top.st_ino))]
    while stack:
        path, rel, depth, in_repo, ident = stack.pop()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ArchiveError("unreadable", sanitize(exc)) from exc
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != ident:
                raise ArchiveError("path_changed")
            with os.scandir(fd) as it:
                entries = list(it)
            seen += len(entries)
            if seen > MAX_SCAN_ENTRIES:
                raise ArchiveError("scan_limit", "entry_count")
            names = {e.name: e for e in entries}
            git = names.get(".git")
            bare = git is None and (
                "HEAD" in names
                and names.get("objects") is not None
                and names["objects"].is_dir(follow_symlinks=False)
                and names.get("refs") is not None
                and names["refs"].is_dir(follow_symlinks=False)
            )
            if git is not None:
                if git.is_dir(follow_symlinks=False):
                    repos.append(RepoScan(rel, "worktree", _join(rel, ".git")))
                elif git.is_file(follow_symlinks=False):
                    repos.append(_gitfile_repo(base, rel, fd, top.st_dev, mount))
                else:
                    raise ArchiveError("unsupported_repo", "gitfile_type")
                in_repo = True
            elif bare:
                repos.append(RepoScan(rel, "bare"))
            if len(repos) > MAX_REPOS:
                raise ArchiveError("scan_limit", "repo_count")
            if bare:
                continue  # a bare repository holds no working tree to descend into
            for e in entries:
                if e.name == ".git" or not e.is_dir(follow_symlinks=False):
                    continue
                child = _join(rel, e.name)
                if in_repo and e.name in REGENERABLE:
                    caches.append(child)
                    continue
                if depth + 1 > MAX_SCAN_DEPTH:
                    # A deeper repository would be missed.
                    raise ArchiveError("scan_limit", "depth")
                cst = e.stat(follow_symlinks=False)
                if cst.st_dev != top.st_dev:
                    raise ArchiveError("mount_present")
                ident = (cst.st_dev, cst.st_ino)
                stack.append(
                    (os.path.join(path, e.name), child, depth + 1, in_repo, ident)
                )
        except OSError as exc:
            raise ArchiveError("unreadable", sanitize(exc)) from exc
        finally:
            os.close(fd)
    repos.sort(key=lambda r: r.rel)
    caches.sort()
    return repos, caches


def _read_pointer(name: str, dir_fd: int, prefix: str, what: str) -> str | None:
    """The one-line path in a gitfile or `commondir` file, or None when it does
    not exist. Size-capped, never through a symlink, never a FIFO."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArchiveError("unsupported_repo", f"{what}_unreadable") from exc
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ArchiveError("unsupported_repo", f"{what}_type")
        data = f.read(MAX_GITFILE_BYTES + 1)
    if len(data) > MAX_GITFILE_BYTES:
        raise ArchiveError("unsupported_repo", f"{what}_too_large")
    try:
        text = data.decode().rstrip()
    except UnicodeDecodeError as exc:
        raise ArchiveError("unsupported_repo", f"{what}_invalid") from exc
    value = text.removeprefix(prefix)
    if not text.startswith(prefix) or not value or "\n" in value or "\0" in value:
        raise ArchiveError("unsupported_repo", f"{what}_invalid")
    return value


def _resolve(
    base: Path, start: str, target: str, mount: str, dev: int, what: str
) -> str:
    """`target` as a directory relative to `base`. A relative target resolves
    from `start`; an absolute one must lie under `mount` (remapped to `base`)
    or under `base`. Every component must be a real directory on `base`'s
    device, never a symlink, and `..` may not leave `base`."""
    b = str(base)
    if os.path.isabs(target):
        for prefix in (mount, b):
            if target == prefix or target.startswith(prefix.rstrip("/") + "/"):
                parts = target[len(prefix) :].split("/")
                break
        else:
            raise ArchiveError("unsupported_repo", f"{what}_outside")
    else:
        parts = [*([] if start == "." else start.split("/")), *target.split("/")]
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise ArchiveError("unsupported_repo", f"{what}_outside")
            out.pop()
            continue
        out.append(part)
        try:
            st = os.lstat(os.path.join(b, *out))
        except FileNotFoundError as exc:
            raise ArchiveError("unsupported_repo", f"{what}_missing") from exc
        except OSError as exc:
            raise ArchiveError("unreadable", sanitize(exc)) from exc
        if stat.S_ISLNK(st.st_mode):
            raise ArchiveError("unsupported_repo", f"{what}_symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise ArchiveError("unsupported_repo", f"{what}_missing")
        if st.st_dev != dev:
            raise ArchiveError("mount_present")
    if not out:
        raise ArchiveError("unsupported_repo", f"{what}_outside")
    return "/".join(out)


def _gitfile_repo(base: Path, rel: str, dir_fd: int, dev: int, mount: str) -> RepoScan:
    """A worktree whose `.git` is a file: a linked worktree (`gitdir:` names
    `<main>/.git/worktrees/<id>`, whose `commondir` names the main git
    directory) or a submodule (`gitdir:` names `.git/modules/<name>`)."""
    target = _read_pointer(".git", dir_fd, "gitdir: ", "gitfile")
    if target is None:
        raise ArchiveError("path_changed")
    git_dir = _resolve(base, rel, target, mount, dev, "gitdir")
    try:
        gfd = os.open(
            os.path.join(base, git_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise ArchiveError("unsupported_repo", "gitdir_unreadable") from exc
    try:
        common = _read_pointer("commondir", gfd, "", "commondir")
    finally:
        os.close(gfd)
    common_dir = None
    if common is not None:
        common_dir = _resolve(base, git_dir, common, mount, dev, "commondir")
        if os.path.isabs(common) and not common.startswith(str(base) + "/"):
            # Git rereads `commondir` itself (GIT_COMMON_DIR does not stop it), so
            # a remapped absolute one cannot work. `git worktree` writes `../..`.
            raise ArchiveError("unsupported_repo", "commondir_absolute")
    return RepoScan(rel, "worktree", git_dir, common_dir)


def _z(out: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def _safe_rel(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/")


def scan(base: Path, git: Git) -> Scan:
    base = Path(os.path.abspath(base))
    found, caches = find_repos(base)
    return Scan([_scan_repo(base, r, git) for r in found], caches)


def _scan_repo(base: Path, r: RepoScan, git: Git) -> RepoScan:
    path = base / r.rel
    where = _where(base, r)
    rc, out = git.run(["rev-parse", "--verify", "-q", "HEAD"], **where, ok=(0, 1))
    r.head = out.decode().strip() if rc == 0 else None
    rc, out = git.run(["symbolic-ref", "-q", "--short", "HEAD"], **where, ok=(0, 1))
    r.branch = out.decode().strip() if rc == 0 else None
    if r.head is None and r.branch is None:
        raise ArchiveError("git_error", "head_invalid")  # neither a commit nor a branch
    _, out = git.run(["rev-parse", "--is-shallow-repository"], **where)
    shallow = out.decode().strip() == "true"
    _, out = git.run(
        ["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], **where
    )
    for line in out.decode().splitlines():
        name, _, sha = line.partition(" ")
        r.remote_refs[name] = sha
    _, out = git.run(["rev-list", "--count", "--all", "--not", "--remotes"], **where)
    r.local_commits = int(out.decode().strip() or 0)
    if r.local_commits:
        _, out = git.run(
            ["log", "--all", "--not", "--remotes", "--name-only", "-z", "--format="],
            **where,
        )
        r.sensitive_in_commits = sorted({p for p in _z(out) if is_sensitive(p)})
    if r.kind == "bare":
        if shallow and r.has_wip:
            raise ArchiveError("unsupported_repo", "shallow")
        return r
    diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only"]
    _, out = git.run([*diff, "--cached", "-z"], **where)
    staged = _z(out)
    _, out = git.run([*diff, "-z"], **where)
    unstaged = _z(out)
    excludes = [f"--exclude={name}/" for name in sorted(REGENERABLE)]
    _, out = git.run(["ls-files", "--others", "-z", *excludes], **where)
    # Entries ending in "/" are nested repositories: archived on their own.
    untracked = [p for p in _z(out) if not p.endswith("/")]
    if len(untracked) > MAX_UNTRACKED_FILES:
        raise ArchiveError("archive_too_large")
    sensitive: set[str] = set()
    for names, target in ((staged, r.staged), (unstaged, r.unstaged)):
        for p in names:
            (sensitive.add(p) if is_sensitive(p) else target.append(p))
    for p in untracked:
        if not _safe_rel(p):
            raise ArchiveError("unsafe_path")
        if is_sensitive(p):
            sensitive.add(p)
            continue
        try:
            st = os.lstat(path / p)
        except OSError as exc:
            raise ArchiveError("unreadable", sanitize(exc)) from exc
        if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            r.untracked.append(p)
            r.untracked_bytes += st.st_size
    r.skipped_sensitive = sorted(sensitive)
    if shallow and r.has_wip:
        # A bundle of a shallow history needs objects the archive lacks.
        raise ArchiveError("unsupported_repo", "shallow")
    return r


class ArchiveWriter:
    """Filesystem side of an archive; tests subclass it to inject failures."""

    def open(self, path: Path) -> BinaryIO:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "wb")

    def hash_file(self, path: Path) -> str:
        h = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
        return h.hexdigest()

    def mkdir(self, path: Path) -> None:
        path.mkdir(mode=0o700)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_beneath(base: Path, rel: str) -> int:
    """Open `base/rel` for reading, refusing a symlink at any component."""
    parts = rel.split("/")
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)


def _write_untracked_tar(base: Path, repo: RepoScan, out: BinaryIO) -> None:
    root = base / repo.rel
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in repo.untracked:
            st = os.lstat(root / rel)
            info = tarfile.TarInfo(rel)
            info.mode = stat.S_IMODE(st.st_mode)
            info.mtime = int(st.st_mtime)
            if stat.S_ISLNK(st.st_mode):
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(root / rel)
                tar.addfile(info)
                continue
            fd = _open_beneath(root, rel)
            with os.fdopen(fd, "rb") as f:
                fst = os.fstat(f.fileno())
                if not stat.S_ISREG(fst.st_mode):
                    raise ArchiveError("path_changed")
                info.size = fst.st_size
                tar.addfile(info, f)


def _repo_has_commits(repo: RepoScan) -> bool:
    return repo.head is not None or repo.local_commits > 0 or bool(repo.remote_refs)


def archive(
    base: Path,
    result: Scan,
    archive_root: Path,
    instance_id: str,
    run_id: str,
    created_at: str,
    git: Git,
    *,
    max_bytes: int,
    min_free: int = 0,
    writer: ArchiveWriter | None = None,
) -> Path:
    """Write and verify the archive of every repository with WIP; return the
    final directory. On any failure nothing is left behind and ArchiveError says
    why. The source is never modified."""
    writer = writer or ArchiveWriter()
    wip = [r for r in result.repos if r.has_wip]
    untracked = sum(r.untracked_bytes for r in wip)
    if untracked > max_bytes:
        raise ArchiveError("archive_too_large")
    try:
        st = os.statvfs(archive_root)
    except OSError as exc:
        raise ArchiveError(classify_os_error(exc), sanitize(exc)) from exc
    if st.f_bavail * st.f_frsize < min_free + untracked:
        raise ArchiveError("archive_no_space")
    tmp = archive_root / f".tmp-{instance_id}-{run_id}"
    final_parent = archive_root / instance_id
    final = final_parent / run_id
    try:
        writer.mkdir(tmp)
        total = 0
        for n, repo in enumerate(wip):
            prefix = f"repo-{n}"
            files: list[str] = []
            where = _where(base, repo)
            if _repo_has_commits(repo):
                name = f"{prefix}.bundle"
                with writer.open(tmp / name) as f:
                    git.to_file(["bundle", "create", "-", "--all"], f, **where)
                    _fsync(f)
                git.run(["bundle", "verify", "-q", str(tmp / name)], **where)
                if _bundle_has_prerequisites(tmp / name):
                    # Verified against the source, yet not restorable without it.
                    raise ArchiveError("unsupported_repo", "bundle_not_self_contained")
                files.append(name)
            if repo.kind == "worktree":
                diff = [
                    "diff",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--no-relative",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                ]
                for label, extra in (("staged", ["--cached"]), ("unstaged", [])):
                    if not getattr(repo, label):
                        continue
                    name = f"{prefix}.{label}.patch"
                    with writer.open(tmp / name) as f:
                        git.to_file(
                            [*diff, *extra, "--", ".", *_pathspec_excludes()],
                            f,
                            **where,
                        )
                        _fsync(f)
                    files.append(name)
                if repo.untracked:
                    name = f"{prefix}.untracked.tar"
                    with writer.open(tmp / name) as f:
                        _write_untracked_tar(base, repo, f)
                        _fsync(f)
                    files.append(name)
            for name in files:
                size = os.lstat(tmp / name).st_size
                total += size
                if total > max_bytes:
                    raise ArchiveError("archive_too_large")
                repo.artifacts.append(
                    Artifact(name, size, writer.hash_file(tmp / name))
                )
        manifest = {
            "format": 1,
            "instance_id": instance_id,
            "run_id": run_id,
            "created_at": created_at,
            "repos": [_manifest_repo(r) for r in result.repos],
        }
        with writer.open(tmp / "manifest.json") as f:
            f.write(json.dumps(manifest, indent=2, sort_keys=True).encode())
            _fsync(f)
        _fsync_dir(tmp)
        verify(tmp, writer)
        final_parent.mkdir(mode=0o700, exist_ok=True)
        os.rename(tmp, final)
        _fsync_dir(final_parent)
        _fsync_dir(archive_root)
        verify(final, writer)
    except ArchiveError:
        _discard(tmp)
        raise
    except OSError as exc:
        _discard(tmp)
        raise ArchiveError(classify_os_error(exc), sanitize(exc)) from exc
    except Exception as exc:
        _discard(tmp)
        raise ArchiveError("archive_error", sanitize(exc)) from exc
    return final


class Where(TypedDict, total=False):
    cwd: Path
    work_tree: Path
    git_dir: Path


def _where(base: Path, repo: RepoScan) -> Where:
    """Explicit git locations, so git never discovers a repository by itself
    (a broken `.git` must not fall through to an enclosing one)."""
    path = base / repo.rel
    if repo.kind == "bare":
        return {"git_dir": path}
    if repo.git_dir is None:
        raise ArchiveError("unsupported_repo")
    return {"cwd": path, "work_tree": path, "git_dir": base / repo.git_dir}


def _bundle_has_prerequisites(path: Path) -> bool:
    """True when the bundle header lists a prerequisite (`-<oid>`) line."""
    with path.open("rb") as f:
        for line in f:
            if line == b"\n":
                return False
            if line.startswith(b"-"):
                return True
    raise ArchiveError("archive_error", "bundle_header")


def _fsync(f: BinaryIO) -> None:
    f.flush()
    os.fsync(f.fileno())


def _manifest_repo(r: RepoScan) -> dict:
    data = asdict(r)
    for key in ("staged", "unstaged", "untracked"):
        data[f"{key}_count"] = len(data.pop(key))
    data["has_wip"] = r.has_wip
    return data


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def verify(directory: Path, writer: ArchiveWriter | None = None) -> dict:
    """Re-read every artifact and compare it with the manifest; return it."""
    writer = writer or ArchiveWriter()
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise ArchiveError("archive_error", sanitize(exc)) from exc
    try:
        for repo in manifest["repos"]:
            for a in repo["artifacts"]:
                name = a["name"]
                if not isinstance(name, str) or "/" in name or name.startswith("."):
                    raise ArchiveError("unsafe_path")
                path = directory / name
                if os.lstat(path).st_size != a["bytes"]:
                    raise ArchiveError("archive_hash_mismatch")
                if writer.hash_file(path) != a["sha256"]:
                    raise ArchiveError("archive_hash_mismatch")
    except OSError as exc:
        raise ArchiveError("archive_hash_mismatch", sanitize(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise ArchiveError("archive_error", sanitize(exc)) from exc
    return manifest


def iter_artifacts(manifest: dict) -> Iterator[tuple[dict, dict]]:
    for repo in manifest["repos"]:
        for a in repo["artifacts"]:
            yield repo, a
