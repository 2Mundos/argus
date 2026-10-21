#!/usr/bin/env python3
"""Put the Argus specialist CLIs block into a NanoClaw agent Dockerfile (called by
install-engines.sh).

usage: patch-engines-dockerfile.py /path/to/nanoclaw/container/Dockerfile

A pristine Dockerfile gets the whole block before NanoClaw's ncl wrapper. A
Dockerfile an earlier install already patched is upgraded in place, whatever the
layout it was left in (the COPY line with or without model-policy, steps added one
release at a time): every module argus-delegate needs gets its install step, and the
COPY line then lists every file the RUN step reads from /tmp/argus-engines/, so an
install step can never outrun the COPY (the build fails with "cannot stat"
otherwise). Idempotent: a second run changes nothing. A shape it cannot anchor on
(no COPY line, two of them, no cleanup step) stops the install.
"""

import re
import sys
from pathlib import Path

STAGE = "/tmp/argus-engines/"  # noqa: S108 (a path inside the image build, not on this host)
# What install-engines.sh copies next to the Dockerfile: the only files a COPY may name.
SHIPPED = (
    "install-cursor-agent.sh",
    "argus-delegate.mjs",
    "account-settings.mjs",
    "anthropic-redirect.mjs",
    "model-policy.mjs",
    "model-policy.defaults.json",
    "milestones.mjs",
    "livelog.mjs",
    "usage.mjs",
    "gitea-api.mjs",
)
MARKER = "# ---- ncl CLI wrapper"
BLOCK = f"""# ---- Argus specialist CLIs ---------------------------------------------------
# claude, codex and opencode come from cli-tools.json above (pinned). The Cursor
# agent is a checksummed tarball; argus-delegate is the non-interactive entry the
# worker uses to hand work to any of the four. The smoke fails the build if a CLI
# does not run, so "in the image" is proven, not assumed. Authentication is NOT
# part of the image: each worker logs in with the vendor client.
COPY {" ".join(SHIPPED)} {STAGE}
RUN sh /tmp/argus-engines/install-cursor-agent.sh \\
 && install -m 0644 /tmp/argus-engines/anthropic-redirect.mjs /usr/local/bin/anthropic-redirect.mjs \\
 && install -m 0755 /tmp/argus-engines/argus-delegate.mjs /usr/local/bin/argus-delegate \\
 && install -m 0644 /tmp/argus-engines/account-settings.mjs /tmp/argus-engines/model-policy.mjs /tmp/argus-engines/model-policy.defaults.json /tmp/argus-engines/milestones.mjs /tmp/argus-engines/livelog.mjs /tmp/argus-engines/usage.mjs /usr/local/bin/ \\
 && install -m 0644 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/gitea-api.mjs \\
 && npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\
 && install -m 0755 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/argus-gitea-api \\
 && rm -rf /tmp/argus-engines \\
 && claude --version && codex --version && opencode --version && agent --version

"""
MODEL_POLICY = "COPY model-policy.mjs model-policy.defaults.json /usr/local/bin/"
COPY_RE = re.compile(
    r"^COPY install-cursor-agent\.sh argus-delegate\.mjs(?P<rest>(?: \S+)*?) "
    + re.escape(STAGE)
    + r"$",
    re.M,
)
CLEANUP = " && rm -rf /tmp/argus-engines \\\n"
# (what the RUN step must contain, the step that provides it), for modules that
# releases after the first block added. Keyed on the install step, never on the name
# alone: a name on the COPY line says nothing about whether it is installed.
STEPS = (
    (
        f"{STAGE}milestones.mjs",
        " && install -m 0644 /tmp/argus-engines/milestones.mjs /tmp/argus-engines/gitea-api.mjs /usr/local/bin/ \\\n",
    ),
    (
        f"{STAGE}livelog.mjs",
        " && install -m 0644 /tmp/argus-engines/livelog.mjs /usr/local/bin/ \\\n",
    ),
    (
        f"{STAGE}usage.mjs",
        " && install -m 0644 /tmp/argus-engines/usage.mjs /usr/local/bin/ \\\n",
    ),
    (
        f"{STAGE}anthropic-redirect.mjs",
        " && install -m 0644 /tmp/argus-engines/anthropic-redirect.mjs /usr/local/bin/anthropic-redirect.mjs \\\n",
    ),
    (
        f"{STAGE}account-settings.mjs",
        " && install -m 0644 /tmp/argus-engines/account-settings.mjs /usr/local/bin/ \\\n",
    ),
    (
        "/usr/local/bin/argus-gitea-api",
        " && npm install --prefix /usr/local/lib/argus-gitea --omit=dev --no-audit --no-fund undici@8.12.2 \\\n"
        " && install -m 0755 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/argus-gitea-api \\\n",
    ),
)


def fail(p: Path, why: str) -> None:
    raise SystemExit(f"{p}: {why}: Dockerfile shape changed")


def patch(p: Path, s: str) -> str:
    if "Argus specialist CLIs" not in s:
        if s.count(MARKER) != 1:
            fail(p, "no single ncl wrapper marker")
        s = s.replace(MARKER, BLOCK + MARKER, 1)
    if not re.search("^" + re.escape(MODEL_POLICY) + "$", s, re.M):
        s = s.rstrip("\n") + f"\n\n{MODEL_POLICY}\n"
    copies = list(COPY_RE.finditer(s))
    if len(copies) != 1:
        fail(p, f"{len(copies)} Argus COPY lines, expected 1")
    copy = copies[0]
    end = s.find(CLEANUP, copy.end())
    if end < 0:
        fail(p, "no cleanup step after the Argus COPY line")
    run = s[copy.end() : end]
    for needed, step in STEPS:
        if needed not in run:
            run += step
    files = ["install-cursor-agent.sh", "argus-delegate.mjs", *copy["rest"].split()]
    for name in re.findall(re.escape(STAGE) + r"([\w.-]+)", run):
        if name not in files:
            files.append(name)
    unknown = [f for f in files if f not in SHIPPED]
    if unknown:
        fail(p, f"COPY names files install-engines.sh does not ship: {unknown}")
    line = f"COPY {' '.join(dict.fromkeys(files))} {STAGE}"
    return s[: copy.start()] + line + run + s[end:]


def main() -> None:
    p = Path(sys.argv[1])
    s = p.read_text()
    new = patch(p, s)
    if new != s:
        p.write_text(new)
        print(f"patched {p}")


if __name__ == "__main__":
    main()
