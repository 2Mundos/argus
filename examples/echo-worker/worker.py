"""A minimal Argus worker (the `plain` runtime contract), standard library only.

It registers with its bootstrap token, heartbeats, claims tasks, "does" them by
echoing the title back, and exits cleanly when drained or sent SIGTERM. It is the
image the integration tests scale, and a reference for writing real workers.

Environment (set by Argus when it starts the container):
  ARGUS_URL, ARGUS_BOOTSTRAP_TOKENS, ARGUS_CAPABILITIES, ARGUS_INSTANCE_ID
Optional:
  ECHO_WORK_SECONDS  how long each task "takes" (default 1)
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

URL = os.environ["ARGUS_URL"].rstrip("/")
if not URL.startswith(("http://", "https://")):
    sys.exit(f"ARGUS_URL must be http(s): {URL!r}")
BOOTSTRAP = os.environ["ARGUS_BOOTSTRAP_TOKENS"].split(",")[0]
CAPABILITIES = [c for c in os.environ.get("ARGUS_CAPABILITIES", "").split(",") if c]
WORK_SECONDS = float(os.environ.get("ECHO_WORK_SECONDS", "1"))

stop = threading.Event()
draining = threading.Event()
lease: dict = {}  # the task currently held: {"task_id", "attempt"}
token = ""


def call(method: str, path: str, body: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(  # noqa: S310 - scheme checked at startup
        URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310
            raw = res.read()
            return res.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def heartbeat_loop(interval: float) -> None:
    while not stop.wait(interval):
        status, body = call(
            "POST", "/api/agents/me/heartbeat", {"leases": [lease] if lease else []}
        )
        if status == 401:
            print("token revoked; exiting", flush=True)
            stop.set()
        elif status == 200 and body["state"] == "draining":
            draining.set()


def main() -> int:
    global token
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    for attempt in range(30):  # Argus may still be coming up
        try:
            status, reg = call(
                "POST",
                "/api/agents/register",
                {"bootstrap_token": BOOTSTRAP, "capabilities": CAPABILITIES},
            )
            break
        except OSError as err:
            print(f"cannot reach {URL} ({err}); retrying", flush=True)
            time.sleep(min(2**attempt, 10))
    else:
        return 1
    if status != 201:
        print(f"registration failed: {status} {reg}", flush=True)
        return 1
    token = reg["token"]
    print(f"registered as {reg['agent']['id']}", flush=True)
    threading.Thread(
        target=heartbeat_loop, args=(reg["heartbeat_interval_s"],), daemon=True
    ).start()

    while not stop.is_set() and not draining.is_set():
        status, task = call(
            "POST",
            "/api/tasks/claim",
            {"capabilities": CAPABILITIES, "wait_s": 5},
            timeout=35,
        )
        if status == 204:
            continue
        if status != 200:
            print(f"claim failed: {status} {task}", flush=True)
            time.sleep(2)
            continue
        lease.update(task_id=task["id"], attempt=task["attempt"])
        print(f"working on {task['title']!r}", flush=True)
        time.sleep(WORK_SECONDS)
        status, _ = call(
            "POST",
            f"/api/tasks/{task['id']}/complete",
            {
                "attempt": task["attempt"],
                "result": {"summary": f"echo: {task['title']}"},
            },
        )
        print(f"completed {task['id']}: {status}", flush=True)
        lease.clear()

    call("POST", "/api/agents/me/leave")
    print("left cleanly", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
