"""Rebuild repositories from a workspace cleanup archive (workspace-cleanup.spec.md 3.5).

Usage: python -m argus_api.fleet.workspace_restore <archive dir> <dest dir>
"""

import os
import stat
import sys
import tarfile
from pathlib import Path

from argus_api.fleet.workspace_archive import ArchiveError, Git, verify


def restore(archive_dir: Path, dest: Path, git: Git | None = None) -> list[Path]:
    """Restore every repository of the archive under `dest` (which must not
    exist yet or be empty); return the restored repository paths."""
    git = git or Git()
    manifest = verify(archive_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ArchiveError("destination_not_empty")
    restored = []
    for repo in manifest["repos"]:
        artifacts = {a["name"].split(".", 1)[1]: a["name"] for a in repo["artifacts"]}
        if not artifacts:
            continue  # nothing unpublished was archived for this repository
        rel = repo["rel"]
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ArchiveError("unsafe_path")
        path = dest / rel
        path.mkdir(parents=True, exist_ok=True)
        bare = repo["kind"] == "bare"
        git.run(["init", "-q", *(["--bare"] if bare else []), str(path)])
        gd = path if bare else None
        cwd = None if bare else path
        if "bundle" in artifacts:
            bundle = str(archive_dir / artifacts["bundle"])
            git.run(
                ["fetch", "-q", "--update-head-ok", bundle, "refs/*:refs/*"],
                cwd=cwd,
                git_dir=gd,
            )
        if repo["branch"]:
            git.run(
                ["symbolic-ref", "HEAD", f"refs/heads/{repo['branch']}"],
                cwd=cwd,
                git_dir=gd,
            )
        elif repo["head"]:
            git.run(
                ["update-ref", "--no-deref", "HEAD", repo["head"]], cwd=cwd, git_dir=gd
            )
        if bare:
            restored.append(path)
            continue
        if repo["head"]:
            git.run(["reset", "-q", "--hard"], cwd=path)
        if "staged.patch" in artifacts:
            patch = str(archive_dir / artifacts["staged.patch"])
            git.run(["apply", "--index", "--binary", patch], cwd=path)
        if "unstaged.patch" in artifacts:
            patch = str(archive_dir / artifacts["unstaged.patch"])
            git.run(["apply", "--binary", patch], cwd=path)
        if "untracked.tar" in artifacts:
            _extract(archive_dir / artifacts["untracked.tar"], path)
        restored.append(path)
    return restored


def _extract(tar_path: Path, repo: Path) -> None:
    """Extract regular files and symlinks, refusing anything that could land
    outside `repo` (absolute names, `..`, a symlinked parent)."""
    with tarfile.open(tar_path, mode="r:") as tar:
        for member in tar:
            parts = member.name.split("/")
            if member.name.startswith("/") or ".." in parts or "" in parts:
                raise ArchiveError("unsafe_path")
            if not (member.isfile() or member.issym()):
                raise ArchiveError("unsafe_path")
            parent = repo
            for part in parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    raise ArchiveError("unsafe_path")
                parent.mkdir(exist_ok=True)
            target = parent / parts[-1]
            if member.issym():
                os.symlink(member.linkname, target)
                continue
            src = tar.extractfile(member)
            if src is None:
                raise ArchiveError("archive_error")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(target, flags, stat.S_IMODE(member.mode) | 0o600)
            with os.fdopen(fd, "wb") as out, src:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        paths = restore(Path(args[0]), Path(args[1]))
    except ArchiveError as exc:
        print(f"restore failed: {exc.code}", file=sys.stderr)
        return 1
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
