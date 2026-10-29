// Task changes for a delegated run (docs/specs/task-changes.spec.md).
//
// A diff snapshot of the task workspace against its merge base, computed here in the
// session container: git reads a `.git` the worker controls, so it must never run on
// the host. The snapshot is written whole to a file in the agent group's directory; the
// NanoClaw host (argus.ts) reads it and puts it to Argus with the agent token. This
// module has no token, no network, and never throws into the delegate run.

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { redactText } from "./livelog.mjs";

export const CHANGES_FILE = "argus-task-changes.json";
export const MAX_FILES = 500;
export const MAX_UNTRACKED = 200;
export const MAX_COMMITS = 50;
export const MAX_PATCH_BYTES = 256 * 1024;
export const MAX_FILE_PATCH_BYTES = 64 * 1024;
export const CUT_MARK = "… (cut)\n";
const GIT_TIMEOUT_MS = 10_000;
const GIT_MAX_BUFFER = 8 * 1024 * 1024; // a larger diff output is not read at all
const SHA = /^[0-9a-f]{40}([0-9a-f]{24})?$/;
const STATUS = new Set(["A", "M", "D", "R", "C", "T", "U", "X"]);
/** The delegate's own output directory (`--out` default) is not the task's work. */
export const IGNORED = [".argus-delegate/"];

/** A git runner for `cwd`: stdout on success, else null. Read-only and lock-free. */
export function gitRunner(cwd, run = spawnSync) {
  return (...args) => {
    const r = run("git", ["-C", cwd, "-c", "core.fsmonitor=false", ...args], {
      encoding: "utf8",
      timeout: GIT_TIMEOUT_MS,
      maxBuffer: GIT_MAX_BUFFER,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0" },
    });
    return r.status === 0 && typeof r.stdout === "string" ? r.stdout : null;
  };
}

const trim = (s) => (s == null ? null : s.trim() || null);

/** The remote's default branch as `origin/<name>`, when it has been fetched. */
function defaultRef(git) {
  const sym = trim(git("symbolic-ref", "-q", "refs/remotes/origin/HEAD"));
  if (sym?.startsWith("refs/remotes/")) return sym.slice("refs/remotes/".length);
  for (const ref of ["origin/main", "origin/master"])
    if (trim(git("rev-parse", "--verify", "-q", `${ref}^{commit}`))) return ref;
  return null;
}

/** `-z` name-status: `M\0path\0`, `R100\0old\0new\0`. Keyed by the (new) path. */
export function parseNameStatus(out) {
  const parts = out.split("\0");
  const map = new Map();
  for (let i = 0; i < parts.length - 1; ) {
    const letter = parts[i][0];
    const two = letter === "R" || letter === "C";
    const p = parts[i + (two ? 2 : 1)];
    if (p !== undefined && STATUS.has(letter)) map.set(p, letter);
    i += two ? 3 : 2;
  }
  return map;
}

/** `-z` numstat: `a\td\tpath\0`, or `a\td\t\0old\0new\0` for a rename. Binary is `-\t-`. */
export function parseNumstat(out) {
  const parts = out.split("\0");
  const map = new Map();
  for (let i = 0; i < parts.length - 1; ) {
    const [a, d, p] = parts[i].split("\t");
    const renamed = p === "";
    const file = renamed ? parts[i + 2] : p;
    i += renamed ? 3 : 1;
    if (file === undefined) continue;
    const binary = a === "-" && d === "-";
    map.set(file, {
      added: binary ? null : Number(a) || 0,
      deleted: binary ? null : Number(d) || 0,
      binary,
    });
  }
  return map;
}

/** Cut `text` to `max` UTF-8 bytes after a whole line, with the cut mark; and whether it cut. */
export function cutAtLine(text, max) {
  if (Buffer.byteLength(text) <= max) return [text, false];
  const room = max - Buffer.byteLength(CUT_MARK);
  const head = Buffer.from(text).subarray(0, room).toString("utf8").replace(/�$/, "");
  const end = head.lastIndexOf("\n");
  return [(end >= 0 ? head.slice(0, end + 1) : "") + CUT_MARK, true];
}

/** Each file's hunk within MAX_FILE_PATCH_BYTES, the whole within MAX_PATCH_BYTES. */
export function boundPatch(patch) {
  let cut = false;
  const pieces = patch.split(/(?=^diff --git )/m).map((piece) => {
    const [p, c] = cutAtLine(piece, MAX_FILE_PATCH_BYTES);
    cut ||= c;
    return p;
  });
  const [whole, c] = cutAtLine(pieces.join(""), MAX_PATCH_BYTES);
  return [whole, cut || c];
}

/**
 * The snapshot of the workspace at `cwd` (spec 3.1), or null when `cwd` is not a git
 * worktree or git fails. Strings are redacted best effort; the server redacts again.
 */
export function snapshotChanges(
  cwd,
  { run = spawnSync, secrets = [], now = () => new Date(), ignored = IGNORED } = {},
) {
  try {
    const git = gitRunner(cwd, run);
    if (trim(git("rev-parse", "--is-inside-work-tree")) !== "true") return null;
    const red = (s) => redactText(s, secrets);
    const kept = (p) => !ignored.some((prefix) => p.startsWith(prefix));
    const head = trim(git("rev-parse", "--verify", "-q", "HEAD^{commit}"));
    const branch = trim(git("symbolic-ref", "--short", "-q", "HEAD")) ?? "HEAD";
    const ref = head ? defaultRef(git) : null;
    const baseSha = ref ? trim(git("merge-base", "HEAD", ref)) : null;
    const base = ref && baseSha && SHA.test(baseSha) ? { ref, sha: baseSha } : null;
    const against = base?.sha ?? trim(git("hash-object", "-t", "tree", "/dev/null"));
    if (!against) return null;
    const diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M"];
    const names = git(...diff, "--name-status", "-z", against);
    const nums = git(...diff, "--numstat", "-z", against);
    const patchOut = git(...diff, against);
    if (names == null || nums == null || patchOut == null) return null;
    const statuses = parseNameStatus(names);
    const counts = parseNumstat(nums);
    const all = [...statuses]
      .filter(([p]) => kept(p))
      .map(([p, status]) => ({
        path: red(p),
        status,
        ...(counts.get(p) ?? { added: null, deleted: null, binary: false }),
      }));
    const range = base ? [`${base.sha}..HEAD`] : head ? ["HEAD"] : null;
    const log = range
      ? (git("log", "--format=%H%x00%s", "-n", String(MAX_COMMITS), ...range) ?? "")
      : "";
    const commits = log
      .split("\n")
      .filter(Boolean)
      .map((row) => row.split("\0"))
      .filter(([sha]) => SHA.test(sha))
      .map(([sha, subject = ""]) => ({ sha, subject: red(subject).slice(0, 1000) }));
    const others = (git("ls-files", "--others", "--exclude-standard", "-z") ?? "")
      .split("\0")
      .filter((p) => p && kept(p));
    const untracked = others.slice(0, MAX_UNTRACKED).map((p) => {
      let bytes = 0;
      try {
        bytes = fs.lstatSync(path.join(cwd, p)).size; // never follows a link
      } catch {
        /* gone since */
      }
      return { path: red(p), bytes };
    });
    const [patch, patchCut] = boundPatch(red(patchOut));
    return {
      at: now().toISOString(),
      branch: red(branch).slice(0, 255),
      base,
      head: head && SHA.test(head) ? head : null,
      commits,
      files: all.slice(0, MAX_FILES),
      untracked,
      patch,
      truncated: {
        files: all.length > MAX_FILES || others.length > MAX_UNTRACKED,
        patch: patchCut,
      },
    };
  } catch {
    return null;
  }
}

/** What changed since the last snapshot: HEAD plus the porcelain status, hashed. */
export function worktreeKey(cwd, run = spawnSync) {
  const git = gitRunner(cwd, run);
  const status = git("status", "--porcelain=v1", "-z", "--untracked-files=normal");
  if (status == null) return null;
  const head = git("rev-parse", "-q", "--verify", "HEAD") ?? "";
  return createHash("sha256").update(head).update("\0").update(status).digest("hex");
}

/** Write `record` to `file` whole: a temp file in the same directory, then rename. */
function writeWhole(file, record) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(record)}\n`, { mode: 0o666 });
  fs.renameSync(tmp, file);
}

/**
 * Snapshots of `cwd` for one task attempt, every `intervalMs` while the executor runs
 * (only when the worktree changed) and once at `stop()`. Never throws.
 */
export function startChanges({
  cwd,
  file,
  taskId,
  attempt,
  secrets = [],
  intervalMs = 30_000,
  run = spawnSync,
  now,
}) {
  let lastKey = null;
  const take = (force) => {
    try {
      const key = worktreeKey(cwd, run);
      if (key == null || (!force && key === lastKey)) return;
      const snap = snapshotChanges(cwd, { run, secrets, now });
      if (!snap) return;
      writeWhole(file, { v: 1, task_id: taskId, attempt, ...snap });
      lastKey = key;
    } catch {
      /* a snapshot is never worth failing the run */
    }
  };
  const timer = setInterval(() => take(false), intervalMs);
  timer.unref?.();
  return {
    tick: () => take(false),
    stop() {
      clearInterval(timer);
      take(true);
    },
  };
}
