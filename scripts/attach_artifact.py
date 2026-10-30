#!/usr/bin/env python3
"""Attach a local file to an Argus task, byte-exact (docs/specs/task-inputs.spec.md 3.1).

    ARGUS_TOKEN=... python3 scripts/attach_artifact.py --url http://host:8000 \\
        --task ID --file PATH [--name N] [--role R] [--media-type T] \\
        [--share-with-workers]

Streams the file with its Content-Length and the expected size and SHA-256, then
asks the API to re-verify the stored bytes. Safe to retry: an attachment with the
same name, size and hash is reported as done; the same name with other bytes (or
another sharing) fails. Prints one JSON line of metadata, never content or the
token. Exit codes: 0 attached or already present, 1 failed, 2 usage, 3 conflict.
Standard library only.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CHUNK = 1 << 16
TIMEOUT_S = 300


class Failure(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def digest(path: Path) -> tuple[int, str]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


class Api:
    def __init__(self, url: str, token: str) -> None:
        base = url.rstrip("/")
        self.base = base if base.endswith("/api") else base + "/api"
        self.token = token

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
                return res.status, json.loads(res.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"null")
            except ValueError:
                detail = None
            return e.code, detail
        except (urllib.error.URLError, OSError) as e:
            raise Failure(f"cannot reach {self.base}: {e.__class__.__name__}") from None


def error(status: int, detail: Any) -> str:
    d = detail.get("detail") if isinstance(detail, dict) else None
    if isinstance(d, dict):
        return f"{status} {d.get('code', 'error')}: {d.get('message', '')}".rstrip(": ")
    return f"HTTP {status}"


def existing(api: Api, task: str, name: str) -> dict | None:
    status, body = api.call("GET", f"/tasks/{urllib.parse.quote(task)}/artifacts")
    if status != 200:
        raise Failure(f"cannot list task artifacts: {error(status, body)}")
    for art in body:
        if art.get("source") == "attachment" and art.get("name") == name:
            return art
    return None


def same(art: dict, size: int, sha256: str, shared: bool) -> bool:
    """True when `art` is this file; raises a conflict when the name is taken."""
    if (art["size"], art["sha256"]) != (size, sha256):
        raise Failure(
            f"attachment {art['name']!r} already exists on this task with other bytes"
            f" ({art['size']} bytes, sha256 {art['sha256']}); use another --name",
            exit_code=3,
        )
    if bool(art.get("worker_readable")) != shared:
        raise Failure(
            f"attachment {art['name']!r} already exists with worker_readable="
            f"{str(bool(art.get('worker_readable'))).lower()}; use another --name",
            exit_code=3,
        )
    return True


def upload(api: Api, args: argparse.Namespace, size: int, sha256: str) -> dict:
    params = {
        "name": args.name,
        "expected_size": str(size),
        "expected_sha256": sha256,
    }
    if args.role:
        params["role"] = args.role
    if args.share_with_workers:
        params["worker_readable"] = "true"
    with args.file.open("rb") as f:
        status, body = api.call(
            "POST",
            f"/tasks/{urllib.parse.quote(args.task)}/attachments",
            params=params,
            body=f,
            headers={"Content-Type": args.media_type, "Content-Length": str(size)},
        )
    if status == 201:
        return body
    if status == 409 and error(status, body).startswith("409 artifact_exists"):
        art = existing(api, args.task, args.name)  # a concurrent or earlier upload
        if art is not None and same(art, size, sha256, args.share_with_workers):
            return art
    raise Failure(f"upload failed: {error(status, body)}")


def verify(api: Api, art: dict, size: int, sha256: str) -> None:
    status, body = api.call("POST", f"/artifacts/{art['id']}/verify")
    if status != 200:
        raise Failure(f"verify failed: {error(status, body)}")
    if not body.get("ok") or (body.get("size"), body.get("sha256")) != (size, sha256):
        raise Failure(
            f"stored bytes differ: {body.get('size')} bytes, sha256"
            f" {body.get('sha256')}; local file is {size} bytes, sha256 {sha256}"
        )


def run(args: argparse.Namespace, token: str) -> dict:
    if not args.file.is_file():
        raise Failure(f"{args.file} is not a readable file", exit_code=2)
    args.name = args.name or args.file.name
    args.media_type = args.media_type or (
        mimetypes.guess_type(args.file.name)[0] or "application/octet-stream"
    )
    size, sha256 = digest(args.file)
    api = Api(args.url, token)
    art = existing(api, args.task, args.name)
    status = "exists"
    if art is None:
        art, status = upload(api, args, size, sha256), "attached"
    else:
        same(art, size, sha256, args.share_with_workers)
    verify(api, art, size, sha256)
    if digest(args.file) != (size, sha256):
        raise Failure("the local file changed during the upload; run again")
    return {
        "status": status,
        "id": art["id"],
        "task_id": args.task,
        "name": art["name"],
        "media_type": art["media_type"],
        "size": size,
        "sha256": sha256,
        "worker_readable": bool(art.get("worker_readable")),
        "ref": f"attachment:{art['id']}:{sha256}",
    }


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True, help="Argus API base URL")
    p.add_argument("--task", required=True, help="task id")
    p.add_argument("--file", required=True, type=Path, help="local file to attach")
    p.add_argument("--name", help="attachment name (default: the file name)")
    p.add_argument("--role", help="artifact role")
    p.add_argument("--media-type", help="Content-Type (default: guessed)")
    p.add_argument(
        "--share-with-workers",
        action="store_true",
        help="make it an input every worker leasing the task can read",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    token = os.environ.get("ARGUS_TOKEN", "")
    if not token:
        print("ARGUS_TOKEN is not set", file=sys.stderr)
        return 2
    try:
        out = run(args, token)
    except Failure as e:
        print(str(e).replace(token, "***"), file=sys.stderr)
        return e.exit_code
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
