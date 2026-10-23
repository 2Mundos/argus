#!/usr/bin/env python3
"""Bounded docker rm/ps removal confirmation and idempotent session stop (install.sh).

usage: patch-docker-teardown.py /path/to/nanoclaw

Idempotent: every hook is "ensure this exact text". Runs after patch-codex-sandbox.py.
"""

import sys
from pathlib import Path

root = Path(sys.argv[1])


def ensure(text, what, new, *, anchor=None, replace=(), insert=None):
    if new in text:
        return text
    if what == "import removal helper" and "from './docker-stop-removal.js'" in text:
        return text
    if (
        what == "adopt() handle wiring"
        and "deadlineMs: this.opts.removalConfirmDeadlineMs" in text
    ):
        return text
    for older in replace:
        if older in text:
            return text.replace(older, new, 1)
    old, full = insert
    assert text.count(old) == 1, f"{what}: upstream shape changed ({old[:60]!r}…)"
    return text.replace(old, full, 1)


def edit(path, steps):
    p = root / path
    if not p.is_file():
        return
    s = p.read_text()
    original = s
    for step in steps:
        s = ensure(s, *step[:2], **step[2])
    if s != original:
        p.write_text(s)
        print(f"patched {p}")


IMPORT_OLD = """import { CodexSandboxGuard, type SeccompSource } from './argus-codex-sandbox.js';
import { JsonDocumentStream } from './json-stream.js';"""
IMPORT_NEW = """import { CodexSandboxGuard, type SeccompSource } from './argus-codex-sandbox.js';
import { waitForContainerRemoval } from './docker-stop-removal.js';
import { JsonDocumentStream } from './json-stream.js';"""

HANDLE_FIELDS = """  /** Log hygiene only — events are never intent-filtered here (that is the hub's job). */
  #stopping = false;"""
HANDLE_FIELDS_NEW = """  /** Log hygiene only — events are never intent-filtered here (that is the hub's job). */
  #stopping = false;
  #teardownDone = false;
  #stopInFlight?: Promise<void>;"""

CTOR_OLD = """    private readonly emit: (event: SessionEvent) => void,
  ) {}"""
CTOR_NEW = """    private readonly emit: (event: SessionEvent) => void,
    private readonly removalConfirm: {
      deadlineMs?: number;
      pollMs?: number;
      now?: () => number;
      sleep?: (ms: number) => Promise<void>;
    },
  ) {}"""

ADOPT_HANDLE_OLD = "      return new DockerHandle(spec.key, name, this.#cli, null, auxiliaryNames, privateNetwork, this.#emit);"
ADOPT_HANDLE_NEW = (
    "      return new DockerHandle(\n"
    "        spec.key,\n"
    "        name,\n"
    "        this.#cli,\n"
    "        null,\n"
    "        auxiliaryNames,\n"
    "        privateNetwork,\n"
    "        this.#emit,\n"
    "        {\n"
    "          deadlineMs: this.opts.removalConfirmDeadlineMs,\n"
    "          pollMs: this.opts.removalConfirmPollMs,\n"
    "          now: this.opts.removalConfirmNow,\n"
    "          sleep: this.opts.removalConfirmSleep,\n"
    "        },\n"
    "      );"
)

PREPARE_HANDLE_OLD = "return new DockerHandle(spec.key, name, this.#cli, spec, auxiliaryNames, privateNetwork, this.#emit);"
PREPARE_HANDLE_NEW = (
    "return new DockerHandle(\n"
    "      spec.key,\n"
    "      name,\n"
    "      this.#cli,\n"
    "      spec,\n"
    "      auxiliaryNames,\n"
    "      privateNetwork,\n"
    "      this.#emit,\n"
    "      {\n"
    "        deadlineMs: this.opts.removalConfirmDeadlineMs,\n"
    "        pollMs: this.opts.removalConfirmPollMs,\n"
    "        now: this.opts.removalConfirmNow,\n"
    "        sleep: this.opts.removalConfirmSleep,\n"
    "      },\n"
    "    );"
)

LIST_HANDLE_OLD = """          handle: new DockerHandle(
            key,
            name,
            this.#cli,
            null,
            auxiliaries,
            auxiliaries.length > 0 ? sessionNetworkName({ key }) : undefined,
            this.#emit,
          ),"""
LIST_HANDLE_NEW = """          handle: new DockerHandle(
            key,
            name,
            this.#cli,
            null,
            auxiliaries,
            auxiliaries.length > 0 ? sessionNetworkName({ key }) : undefined,
            this.#emit,
            {
              deadlineMs: this.opts.removalConfirmDeadlineMs,
              pollMs: this.opts.removalConfirmPollMs,
              now: this.opts.removalConfirmNow,
              sleep: this.opts.removalConfirmSleep,
            },
          ),"""

STOP_BLOCK_OLD = """  async stop(reason: string): Promise<void> {
    this.#stopping = true;
    log.info('Stopping session container', { containerName: this.name, reason });
    const grace = String(this.pendingSpec?.stopGraceSeconds ?? 1);
    try {
      this.cli.run(['stop', '-t', grace, this.name]);
    } catch {
      // Already gone, or the daemon refused: fall back to killing the attach
      // process so supervision never hangs waiting for an exit that cannot come.
      this.#proc?.kill();
    }
    try {
      this.cli.run(['rm', '--force', this.name]);
    } catch (error) {
      this.#confirmRemoved(this.name, error);
    }
    for (const auxiliary of [...this.auxiliaryNames].reverse()) {
      try {
        this.cli.run(['stop', '-t', '1', auxiliary]);
      } catch {
        /* already gone */
      }
      try {
        this.cli.run(['rm', '--force', auxiliary]);
      } catch (error) {
        this.#confirmRemoved(auxiliary, error);
      }
    }
    if (this.privateNetwork) {
      try {
        this.cli.run(['network', 'rm', this.privateNetwork]);
      } catch {
        /* already gone or still in use */
      }
    }
  }

  /** A successful daemon query distinguishes auto-removal from a daemon outage. */
  #confirmRemoved(name: string, error: unknown): void {
    try {
      const remaining = this.cli.run(['ps', '-a', '--filter', `name=^/${name}$`, '--format', '{{.Names}}']);
      if (!remaining.trim()) return;
    } catch (probeError) {
      throw normalizeDockerError(probeError);
    }
    throw normalizeDockerError(error);
  }"""

STOP_BLOCK_NEW = """  async stop(reason: string): Promise<void> {
    if (this.#teardownDone) return;
    if (!this.#stopInFlight) {
      this.#stopInFlight = this.#stopOnce(reason).finally(() => {
        this.#stopInFlight = undefined;
      });
    }
    try {
      await this.#stopInFlight;
      this.#teardownDone = true;
    } catch (error) {
      this.#teardownDone = false;
      throw error;
    }
  }

  async #stopOnce(reason: string): Promise<void> {
    this.#stopping = true;
    log.info('Stopping session container', { containerName: this.name, reason });
    const grace = String(this.pendingSpec?.stopGraceSeconds ?? 1);
    try {
      this.cli.run(['stop', '-t', grace, this.name]);
    } catch {
      // Already gone, or the daemon refused: fall back to killing the attach
      // process so supervision never hangs waiting for an exit that cannot come.
      this.#proc?.kill();
    }
    try {
      this.cli.run(['rm', '--force', this.name]);
    } catch (error) {
      await this.#confirmRemoved(this.name, error);
    }
    for (const auxiliary of [...this.auxiliaryNames].reverse()) {
      try {
        this.cli.run(['stop', '-t', '1', auxiliary]);
      } catch {
        /* already gone */
      }
      try {
        this.cli.run(['rm', '--force', auxiliary]);
      } catch (error) {
        await this.#confirmRemoved(auxiliary, error);
      }
    }
    if (this.privateNetwork) {
      try {
        this.cli.run(['network', 'rm', this.privateNetwork]);
      } catch {
        /* already gone or still in use */
      }
    }
  }

  /** Poll until the name is gone; a single `ps` is not enough after a failed `rm`. */
  async #confirmRemoved(name: string, error: unknown): Promise<void> {
    await waitForContainerRemoval({
      cli: this.cli,
      name,
      rmError: error,
      normalizeError: normalizeDockerError,
      deadlineMs: this.removalConfirm.deadlineMs,
      pollMs: this.removalConfirm.pollMs,
      now: this.removalConfirm.now,
      sleep: this.removalConfirm.sleep,
    });
  }"""

edit(
    "src/drivers/docker-driver.ts",
    [
        (
            "import removal helper",
            IMPORT_NEW,
            {"replace": (IMPORT_OLD,), "insert": (IMPORT_OLD, IMPORT_NEW)},
        ),
        (
            "DockerHandle teardown fields",
            HANDLE_FIELDS_NEW,
            {"insert": (HANDLE_FIELDS, HANDLE_FIELDS_NEW)},
        ),
        ("DockerHandle constructor", CTOR_NEW, {"insert": (CTOR_OLD, CTOR_NEW)}),
        ("adopt() handle wiring", ADOPT_HANDLE_NEW, {"replace": (ADOPT_HANDLE_OLD,)}),
        (
            "prepare() handle wiring",
            PREPARE_HANDLE_NEW,
            {"insert": (PREPARE_HANDLE_OLD, PREPARE_HANDLE_NEW)},
        ),
        (
            "listSessions() handle wiring",
            LIST_HANDLE_NEW,
            {"insert": (LIST_HANDLE_OLD, LIST_HANDLE_NEW)},
        ),
        (
            "stop + confirmRemoved",
            STOP_BLOCK_NEW,
            {"replace": (STOP_BLOCK_OLD,), "insert": (STOP_BLOCK_OLD, STOP_BLOCK_NEW)},
        ),
    ],
)
