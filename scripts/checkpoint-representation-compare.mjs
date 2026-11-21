#!/usr/bin/env node
/**
 * Reproducible comparison of two task-workspace checkpoint representations:
 *   (A) current: node-local hash-verified checkpoints (integrations/nanoclaw/argus-checkpoint.ts)
 *   (B) hidden_refs: hidden Git refs (refs/argus/checkpoints/<task>/<n>) via temp-index commit-tree
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import { registerHooks, stripTypeScriptTypes } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const REPO_URL = "https://git.example.test/org/repo.git";
export const NODE_A = "node-a";
export const NODE_B = "node-b";

let modulesPromise = null;

/** Load real argus-checkpoint.ts and argus-workspace.ts via Node 22 built-in TS transform. */
export async function loadArgusModules() {
  if (!modulesPromise) {
    registerHooks({
      resolve(specifier, ctx, next) {
        if (
          ctx.parentURL?.endsWith(".ts") &&
          specifier.startsWith(".") &&
          specifier.endsWith(".js")
        ) {
          const tsPath = path.resolve(
            path.dirname(fileURLToPath(ctx.parentURL)),
            `${specifier.slice(0, -3)}.ts`,
          );
          if (fs.existsSync(tsPath)) return { url: pathToFileURL(tsPath).href, shortCircuit: true };
        }
        return next(specifier, ctx);
      },
      load(url, ctx, next) {
        if (url.startsWith("file://") && url.endsWith(".ts")) {
          const source = stripTypeScriptTypes(fs.readFileSync(fileURLToPath(url), "utf8"), {
            mode: "transform",
          });
          return { format: "module", source, shortCircuit: true };
        }
        return next(url, ctx);
      },
    });
    const base = path.join(REPO_ROOT, "integrations", "nanoclaw");
    modulesPromise = Promise.all([
      import(pathToFileURL(path.join(base, "argus-checkpoint.ts")).href),
      import(pathToFileURL(path.join(base, "argus-workspace.ts")).href),
    ]).then(([checkpoint, workspace]) => ({ checkpoint, workspace }));
  }
  return modulesPromise;
}

let needsFallbackIdent = null;
function gitEnv(extra = {}) {
  const env = Object.fromEntries(
    Object.entries(process.env).filter(([k]) => !k.startsWith("GIT_")),
  );
  Object.assign(env, { GIT_TERMINAL_PROMPT: "0", LC_ALL: "C" });
  if (needsFallbackIdent === null) {
    try {
      execFileSync("git", ["var", "GIT_COMMITTER_IDENT"], { env, stdio: "pipe" });
      needsFallbackIdent = false;
    } catch {
      needsFallbackIdent = true;
    }
  }
  if (needsFallbackIdent) {
    Object.assign(env, {
      GIT_AUTHOR_NAME: "Argus Compare",
      GIT_AUTHOR_EMAIL: "compare@example.invalid",
      GIT_COMMITTER_NAME: "Argus Compare",
      GIT_COMMITTER_EMAIL: "compare@example.invalid",
    });
  }
  return { ...env, ...extra };
}

export const git = (cwd, args, extraEnv = {}) =>
  execFileSync("git", args, {
    cwd,
    env: gitEnv(extraEnv),
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
  }).trim();

export const tryGit = (cwd, args, extraEnv = {}) => {
  try {
    return { ok: true, out: git(cwd, args, extraEnv) };
  } catch (err) {
    return {
      ok: false,
      error: (err.stderr?.toString() || err.message || String(err)).trim().slice(0, 200),
    };
  }
};

export const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

/** Representation (B): save workspace state into refs/argus/checkpoints/<taskId>/<seq> via temp index. */
export function saveHiddenRef(ws, taskId, seq = 1) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "argus-ref-idx-"));
  const idx = path.join(tmpDir, "index");
  try {
    git(ws, ["add", "-A"], { GIT_INDEX_FILE: idx });
    const tree = git(ws, ["write-tree"], { GIT_INDEX_FILE: idx });
    const head = tryGit(ws, ["rev-parse", "--verify", "-q", "HEAD"]).out || null;
    const oid = git(ws, [
      "commit-tree",
      tree,
      ...(head ? ["-p", head] : []),
      "-m",
      `argus hidden checkpoint ${seq}`,
    ]);
    const ref = `refs/argus/checkpoints/${taskId}/${seq}`;
    git(ws, ["update-ref", ref, oid]);
    return { ok: true, ref, oid, head, tree };
  } catch (err) {
    return {
      ok: false,
      error: (err.stderr?.toString() || err.message || String(err)).trim().slice(0, 200),
    };
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

/** Representation (B): restore branch HEAD (parent) and working tree from a hidden ref commit. */
export function restoreHiddenRef(ws, ref) {
  const parent = tryGit(ws, ["rev-parse", "--verify", "-q", `${ref}^1`]).out || null;
  if (parent) git(ws, ["reset", "-q", "--hard", parent]);
  git(ws, ["read-tree", "--reset", "-u", ref]);
  if (parent) git(ws, ["reset", "-q", "--mixed", parent]);
  else git(ws, ["rm", "--cached", "-r", "-q", "."]);
  return { parent, head: tryGit(ws, ["rev-parse", "--verify", "-q", "HEAD"]).out || null };
}

/** Pure detector: returns a reason string when reverting to `revertTarget` discards `conversation.sawChangesAt`, else null. */
export function unsafeRevert(state) {
  const rep = state?.representation ?? "unknown";
  const seen = state?.conversation?.sawChangesAt ?? null;
  if (!seen) return null;
  const target = state?.revertTarget ?? null;
  if (!target)
    return `unsafe_revert (${rep}): missing revert target while conversation saw changes at ${seen}`;
  if (target === seen) return null;
  if (state?.targetContains != null) {
    const contains =
      state.targetContains instanceof Set ? state.targetContains : new Set(state.targetContains);
    return contains.has(seen)
      ? null
      : `unsafe_revert (${rep}): conversation saw changes at ${seen}, not contained in revert target ${target}`;
  }
  if (Array.isArray(state?.lineage)) {
    const [seenIdx, targetIdx] = [state.lineage.indexOf(seen), state.lineage.indexOf(target)];
    if (seenIdx === -1 || targetIdx === -1)
      return `unsafe_revert (${rep}): cannot verify containment of ${seen} in ${target} (absent from lineage)`;
    return targetIdx < seenIdx
      ? `unsafe_revert (${rep}): revert target ${target} is older than changes seen at ${seen}`
      : null;
  }
  return `unsafe_revert (${rep}): unverified whether revert target ${target} contains ${seen}`;
}

export async function makeFixture(root, mods, opts = {}) {
  const taskId = randomUUID();
  const base = fs.mkdtempSync(path.join(root, "case-"));
  const [store, groupA, groupB] = [
    path.join(base, "store"),
    path.join(base, "inst-a", "groups", "argus"),
    path.join(base, "inst-b", "groups", "argus"),
  ];
  fs.mkdirSync(groupA, { recursive: true });
  fs.mkdirSync(groupB, { recursive: true });
  const prep = await mods.workspace.prepareTaskWorkspace({
    groupDir: groupA,
    taskId,
    repositoryUrl: REPO_URL,
  });
  assert.equal(prep.ok, true, `prepareTaskWorkspace failed: ${prep.error}`);
  const wsA = prep.path;
  fs.writeFileSync(path.join(wsA, "base.txt"), "base\n");
  fs.writeFileSync(path.join(wsA, "tracked.txt"), "one\n");
  fs.writeFileSync(path.join(wsA, ".gitignore"), "ignored.log\n");
  if (opts.withBaseBin)
    fs.writeFileSync(path.join(wsA, "tracked.bin"), Buffer.from([0x00, 0x10, 0x20, 0xff]));
  git(wsA, ["add", "."]);
  git(wsA, ["commit", "-q", "-m", "base"]);
  git(wsA, ["update-ref", "refs/remotes/origin/main", "HEAD"]);
  return {
    base,
    taskId,
    store,
    groupA,
    groupB,
    wsA,
    baseHead: git(wsA, ["rev-parse", "HEAD"]),
    wsB: mods.workspace.taskWorkspacePath(groupB, taskId),
  };
}

export function runWorkspaceArchiveAndRestore(instDir, archiveRoot, destDir) {
  const pyEnv = { ...process.env, PYTHONPATH: path.join(REPO_ROOT, "apps", "api", "src") };
  const pyCode = [
    "import json, sys; from pathlib import Path; from argus_api.fleet.workspace_archive import Git, scan, archive",
    "base, out = Path(sys.argv[1]), Path(sys.argv[2]); g = Git(); s = scan(base, g)",
    "d = archive(base, s, out, 'inst-1', 'run-1', '2026-11-21T00:00:00Z', g, max_bytes=50*1024*1024)",
    "print(json.dumps({'archiveDir': str(d), 'repos': [{'rel': r.rel, 'local_commits': r.local_commits, 'has_wip': r.has_wip} for r in s.repos]}))",
  ].join("\n");
  const info = JSON.parse(
    execFileSync("python3", ["-c", pyCode, instDir, archiveRoot], {
      env: pyEnv,
      encoding: "utf8",
    }).trim(),
  );
  fs.rmSync(instDir, { recursive: true, force: true });
  execFileSync("python3", ["-m", "argus_api.fleet.workspace_restore", info.archiveDir, destDir], {
    env: pyEnv,
    stdio: "pipe",
  });
  return info;
}

export async function runComparison() {
  const mods = await loadArgusModules();
  const {
    checkpointTaskWorkspace,
    restoreTaskWorkspace,
    supersedeCheckpoints,
    checkpointRef,
    parseCheckpointRef,
    isSensitive,
  } = mods.checkpoint;
  const saveCur = (f, extra = {}) =>
    checkpointTaskWorkspace({
      groupDir: f.groupA,
      storeRoot: f.store,
      taskId: f.taskId,
      attempt: 1,
      repositoryUrl: REPO_URL,
      nodeId: NODE_A,
      ...extra,
    });
  const restoreCur = (f, extra = {}) =>
    restoreTaskWorkspace({
      groupDir: f.groupB,
      storeRoot: f.store,
      taskId: f.taskId,
      attempt: 2,
      repositoryUrl: REPO_URL,
      nodeId: NODE_A,
      ...extra,
    });
  const wf = (d, p, v) => fs.writeFileSync(path.join(d, p), v);
  const rf = (d, p) => fs.readFileSync(path.join(d, p), "utf8");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "argus-ckpt-cmp-"));
  const results = [];
  const add = (dimension, representation, outcome, evidence) =>
    results.push({ dimension, representation, outcome, evidence });

  try {
    // 1-5. Committed, uncommitted (staged+unstaged), binary, untracked, and ignored files
    {
      const f = await makeFixture(root, mods, { withBaseBin: true });
      wf(f.wsA, "local.txt", "unpublished commit\n");
      git(f.wsA, ["add", "local.txt"]);
      git(f.wsA, ["commit", "-q", "-m", "local commit"]);
      const localHead = git(f.wsA, ["rev-parse", "HEAD"]);
      const [bStaged, bUnstaged, bUntracked] = [
        Buffer.from([0, 1, 2, 255, 254, 0, 137, 80]),
        Buffer.from([0, 16, 32, 255, 222, 173, 190, 239]),
        Buffer.from([127, 69, 76, 70, 0, 0, 1]),
      ];
      wf(f.wsA, "base.txt", "base\nstaged edit\n");
      wf(f.wsA, "staged-new.txt", "staged new\n");
      wf(f.wsA, "staged.bin", bStaged);
      git(f.wsA, ["add", "base.txt", "staged-new.txt", "staged.bin"]);
      wf(f.wsA, "tracked.txt", "one\nunstaged edit\n");
      wf(f.wsA, "tracked.bin", bUnstaged);
      wf(f.wsA, "untracked.bin", bUntracked);
      fs.mkdirSync(path.join(f.wsA, "src"), { recursive: true });
      wf(f.wsA, "src/new.txt", "untracked\n");
      wf(f.wsA, "run.sh", "#!/bin/sh\n");
      fs.chmodSync(path.join(f.wsA, "run.sh"), 0o755);
      fs.symlinkSync("src/new.txt", path.join(f.wsA, "link.txt"));
      wf(f.wsA, "ignored.log", "ignored-data\n");
      const [stagedDiffA, unstagedDiffA] = [
        git(f.wsA, ["diff", "--cached", "--binary"]),
        git(f.wsA, ["diff", "--binary"]),
      ];

      const checkCommon = (d) => ({
        bins: [
          ["staged.bin", bStaged],
          ["tracked.bin", bUnstaged],
          ["untracked.bin", bUntracked],
        ].every(([p, b]) => sha256(fs.readFileSync(path.join(d, p))) === sha256(b)),
        untracked:
          rf(d, "src/new.txt") === "untracked\n" &&
          (fs.lstatSync(path.join(d, "run.sh")).mode & 0o111) !== 0 &&
          fs.readlinkSync(path.join(d, "link.txt")) === "src/new.txt",
        ignored: fs.existsSync(path.join(d, "ignored.log")),
      });

      const [c, r] = [await saveCur(f), await restoreCur(f)];
      assert.equal(
        c.ok && !c.empty && r.ok && r.restored && git(f.wsB, ["rev-parse", "HEAD"]) === localHead,
        true,
      );
      assert.equal(
        git(f.wsB, ["diff", "--cached", "--binary"]) === stagedDiffA &&
          git(f.wsB, ["diff", "--binary"]) === unstagedDiffA,
        true,
      );
      const curChk = checkCommon(f.wsB);
      assert.equal(curChk.bins && curChk.untracked, true);
      add(
        "committed_changes",
        "current",
        "preserved",
        `HEAD=${localHead.slice(0, 12)} restored identically via work.bundle`,
      );
      add(
        "uncommitted_changes",
        "current",
        "preserved",
        "staged.patch and unstaged.patch restored byte-identically with index boundary intact",
      );
      add(
        "binary_files",
        "current",
        "preserved",
        "staged, unstaged, and untracked binary payloads restored with identical sha256",
      );
      add(
        "untracked_files",
        "current",
        "preserved",
        "nested file, 0755 mode, and relative symlink restored byte-identically",
      );
      add(
        "ignored_files",
        "current",
        curChk.ignored ? "preserved" : "lost",
        `ignored.log present after restore=${curChk.ignored} (--exclude-standard omits ignored files)`,
      );

      const hr = saveHiddenRef(f.wsA, f.taskId, 1);
      assert.equal(hr.ok, true);
      git(f.wsA, ["reset", "-q", "--hard", f.baseHead]);
      git(f.wsA, ["clean", "-fdxq"]);
      restoreHiddenRef(f.wsA, hr.ref);
      const refChk = checkCommon(f.wsA);
      const stagedAfter = git(f.wsA, ["diff", "--cached"]);
      assert.equal(
        git(f.wsA, ["rev-parse", "HEAD"]) === localHead && refChk.bins && refChk.untracked,
        true,
      );
      assert.equal(
        rf(f.wsA, "base.txt") === "base\nstaged edit\n" &&
          rf(f.wsA, "tracked.txt") === "one\nunstaged edit\n",
        true,
      );
      add(
        "committed_changes",
        "hidden_refs",
        "preserved",
        `HEAD=${localHead.slice(0, 12)} restored via parent of ${hr.ref}`,
      );
      add(
        "uncommitted_changes",
        "hidden_refs",
        "preserved",
        `staged+unstaged file bytes preserved on disk, but staged/unstaged index separation is flattened (diff --cached empty=${stagedAfter === ""})`,
      );
      add(
        "binary_files",
        "hidden_refs",
        "preserved",
        "staged, unstaged, and untracked binary payloads stored as git blobs with identical sha256",
      );
      add(
        "untracked_files",
        "hidden_refs",
        "preserved",
        "nested file, 0755 mode, and relative symlink restored via git tree",
      );
      add(
        "ignored_files",
        "hidden_refs",
        refChk.ignored ? "preserved" : "lost",
        `ignored.log present after restore=${refChk.ignored} (git add -A respects .gitignore)`,
      );
    }

    // 6. Non-Git state
    {
      const [taskId, base] = [randomUUID(), fs.mkdtempSync(path.join(root, "nongit-"))];
      const ws = mods.workspace.taskWorkspacePath(path.join(base, "group"), taskId);
      fs.mkdirSync(ws, { recursive: true });
      wf(ws, "plain.txt", "no git dir\n");
      const c = await checkpointTaskWorkspace({
        groupDir: path.join(base, "group"),
        storeRoot: path.join(base, "store"),
        taskId,
        attempt: 1,
        repositoryUrl: REPO_URL,
        nodeId: NODE_A,
      });
      const hr = saveHiddenRef(ws, taskId, 1);
      assert.equal(c.ok === false && hr.ok === false, true);
      add(
        "non_git_state",
        "current",
        "cannot_represent",
        `refused with typed reason=${c.reason}: ${c.error}`,
      );
      add(
        "non_git_state",
        "hidden_refs",
        "cannot_represent",
        `git failed outside repo: ${hr.error.split("\n")[0]}`,
      );
    }

    // 7-8. Secrets by name & committed secret-named file
    {
      const f = await makeFixture(root, mods);
      wf(f.wsA, "committed.pem", "fixture-committed-pem-placeholder\n");
      git(f.wsA, ["add", "committed.pem"]);
      git(f.wsA, ["commit", "-q", "-m", "add fixture pem"]);
      wf(f.wsA, ".env", "APP_DUMMY_VAR=fixture-placeholder-not-secret\n");
      wf(f.wsA, "cert.pem", "fixture-pem-placeholder-not-a-real-key\n");
      assert.equal(isSensitive(".env") && isSensitive("cert.pem"), true);
      const [c, r] = [await saveCur(f), await restoreCur(f)];
      assert.equal(c.ok && !c.empty && r.ok && r.restored, true);
      const m = JSON.parse(rf(path.join(f.store, f.taskId, c.id), "manifest.json"));
      assert.equal(
        !fs.existsSync(path.join(f.wsB, ".env")) &&
          !fs.existsSync(path.join(f.wsB, "cert.pem")) &&
          fs.existsSync(path.join(f.wsB, "committed.pem")),
        true,
      );
      add(
        "secrets_by_name",
        "current",
        "lost",
        `intentionally excluded via isSensitive; manifest.skipped_sensitive=${JSON.stringify(m.skipped_sensitive)}`,
      );
      add(
        "secrets_committed",
        "current",
        "preserved",
        `committed file preserved in work.bundle and flagged in manifest.sensitive_in_commits=${JSON.stringify(m.sensitive_in_commits)}`,
      );

      const hr = saveHiddenRef(f.wsA, f.taskId, 1);
      git(f.wsA, ["reset", "-q", "--hard", f.baseHead]);
      git(f.wsA, ["clean", "-fdq"]);
      restoreHiddenRef(f.wsA, hr.ref);
      assert.equal(
        fs.existsSync(path.join(f.wsA, ".env")) &&
          fs.existsSync(path.join(f.wsA, "cert.pem")) &&
          fs.existsSync(path.join(f.wsA, "committed.pem")),
        true,
      );
      add(
        "secrets_by_name",
        "hidden_refs",
        "preserved",
        "no sensitive-name filter: .env and cert.pem are captured into git objects and restored (confidentiality risk)",
      );
      add(
        "secrets_committed",
        "hidden_refs",
        "preserved",
        "committed file preserved in parent commit history with no sensitive_in_commits audit record",
      );
    }

    // 9. Disk limits (1 KiB via ARGUS_CHECKPOINT_MAX_BYTES)
    {
      const f = await makeFixture(root, mods);
      wf(f.wsA, "payload.bin", Buffer.alloc(4096, 0x5a));
      const prevMax = process.env.ARGUS_CHECKPOINT_MAX_BYTES;
      process.env.ARGUS_CHECKPOINT_MAX_BYTES = "1024";
      let c;
      try {
        c = await saveCur(f);
      } finally {
        if (prevMax === undefined) delete process.env.ARGUS_CHECKPOINT_MAX_BYTES;
        else process.env.ARGUS_CHECKPOINT_MAX_BYTES = prevMax;
      }
      assert.equal(c.ok === false && c.reason === "env_checkpoint_limit", true);
      assert.deepEqual(
        fs.existsSync(path.join(f.store, f.taskId))
          ? fs.readdirSync(path.join(f.store, f.taskId))
          : [],
        [],
      );
      add(
        "disk_limits",
        "current",
        "cannot_represent",
        `ARGUS_CHECKPOINT_MAX_BYTES=1024 enforced: returned ${c.reason} on 4096-byte payload with 0 partial files`,
      );
      const hr = saveHiddenRef(f.wsA, f.taskId, 1);
      assert.equal(hr.ok, true);
      add(
        "disk_limits",
        "hidden_refs",
        "preserved",
        `ignores ARGUS_CHECKPOINT_MAX_BYTES=1024; wrote ${hr.oid.slice(0, 12)} with 4096-byte payload unconditionally (no cap or retention)`,
      );
    }

    // 10. Integrity verification
    {
      const f = await makeFixture(root, mods);
      wf(f.wsA, "tracked.txt", "one\nchanged\n");
      const c = await saveCur(f);
      assert.equal(c.ok && !c.empty, true);
      const parsed = parseCheckpointRef(c.ref);
      assert.deepEqual(parsed, { id: c.id, sha256: c.manifestSha256 });
      assert.equal(checkpointRef(c.id, c.manifestSha256), c.ref);
      const patchFile = path.join(f.store, f.taskId, c.id, "unstaged.patch");
      fs.chmodSync(patchFile, 0o600);
      const flipped = Buffer.from(fs.readFileSync(patchFile));
      flipped[flipped.length - 2] ^= 0x01; // same byte size -> triggers SHA-256 hash failure
      fs.writeFileSync(patchFile, flipped);
      const rTamper = await restoreCur(f, { expected: parsed });
      assert.equal(rTamper.ok === false && rTamper.reason === "env_checkpoint_invalid", true);
      add(
        "integrity_verification",
        "current",
        "preserved",
        `checkpointRef verified; same-size 1-bit tamper in unstaged.patch rejected with ${rTamper.reason} (${rTamper.error})`,
      );

      const hr = saveHiddenRef(f.wsA, f.taskId, 1);
      const blobOid = git(f.wsA, ["rev-parse", `${hr.ref}:tracked.txt`]);
      const objPath = path.join(f.wsA, ".git", "objects", blobOid.slice(0, 2), blobOid.slice(2));
      fs.chmodSync(objPath, 0o600);
      const orig = fs.readFileSync(objPath);
      const corrupt = Buffer.from(orig);
      corrupt[corrupt.length - 1] ^= 0xff;
      fs.writeFileSync(objPath, corrupt);
      const catRes = tryGit(f.wsA, ["cat-file", "-p", blobOid]);
      assert.equal(catRes.ok, false);
      fs.writeFileSync(objPath, orig);
      git(f.wsA, ["update-ref", hr.ref, f.baseHead]);
      add(
        "integrity_verification",
        "hidden_refs",
        "cannot_represent",
        `no manifest-level digest or task/repo/node identity binding (repointing ref to ${f.baseHead.slice(0, 12)} undetected); loose-object bit-flip caught by git (${catRes.error.split("\n")[0]})`,
      );
    }

    // 11-12. Cleanup & Restore after worker or node loss
    {
      const f = await makeFixture(root, mods);
      wf(f.wsA, "wip.txt", "keep-me\n");
      const c = await saveCur(f);
      assert.equal(c.ok && !c.empty && saveHiddenRef(f.wsA, f.taskId, 1).ok, true);
      const noProof = await supersedeCheckpoints({
        groupDir: f.groupA,
        storeRoot: f.store,
        taskId: f.taskId,
        attempt: 1,
        repositoryUrl: REPO_URL,
        published: null,
        nodeId: NODE_A,
      });
      assert.equal(noProof.ok, false);
      fs.rmSync(f.wsA, { recursive: true, force: true });
      const rAfterRm = await restoreCur(f);
      assert.equal(rAfterRm.ok && rAfterRm.restored && rf(f.wsB, "wip.txt") === "keep-me\n", true);
      const groupC = path.join(f.base, "inst-c", "groups", "argus");
      fs.mkdirSync(groupC, { recursive: true });
      const rOtherNode = await restoreCur(f, { groupDir: groupC, attempt: 3, nodeId: NODE_B });
      assert.equal(
        rOtherNode.ok === false && rOtherNode.reason === "env_checkpoint_unavailable",
        true,
      );
      add(
        "cleanup",
        "current",
        "preserved",
        `store outside workspace survives rmSync(wsA) and restores; supersede without proof refused (${noProof.reason})`,
      );
      add(
        "cleanup",
        "hidden_refs",
        "lost",
        "removing workspace deletes .git/refs/argus/* and .git/objects; refs are lost with the workspace",
      );
      add(
        "restore_after_loss",
        "current",
        "preserved",
        `survives worker loss on same node via store; node loss (nodeId=${NODE_B}) fails closed (${rOtherNode.reason})`,
      );
      add(
        "restore_after_loss",
        "hidden_refs",
        "lost",
        "lost when worker .git is destroyed; recoverable only if repo .git object store survives on disk, with no node identity check",
      );
    }

    // 13. Survival into a later attempt (checkpoint restore vs surviving .git vs git bundle --all)
    {
      const f = await makeFixture(root, mods);
      wf(f.wsA, "attempt1.txt", "from-attempt-1\n");
      const c = await saveCur(f);
      const r = await restoreCur(f, { expected: parseCheckpointRef(c.ref) });
      assert.equal(c.ok && !c.empty && r.ok && r.restored, true);
      add(
        "survival_later_attempt",
        "current",
        "preserved",
        `restoreTaskWorkspace rebuilt fresh groupB matching expected ref ${c.id}`,
      );

      const hr = saveHiddenRef(f.wsA, f.taskId, 1);
      const fetchInto = (dir, src) => {
        fs.mkdirSync(dir, { recursive: true });
        git(dir, ["init", "-q"]);
        git(dir, ["fetch", "-q", src, "refs/argus/*:refs/argus/*"]);
        restoreHiddenRef(dir, hr.ref);
        return rf(dir, "attempt1.txt") === "from-attempt-1\n";
      };
      const survivedFromGit = fetchInto(path.join(f.base, "fresh-git"), f.wsA);
      const bundlePath = path.join(f.base, "all.bundle");
      git(f.wsA, ["bundle", "create", bundlePath, "--all"]);
      const inBundle = git(f.wsA, ["bundle", "list-heads", bundlePath]).includes(hr.ref);
      const plainClone = path.join(f.base, "plain-clone");
      git(f.base, ["clone", "-q", bundlePath, plainClone]);
      const inPlainClone = git(plainClone, ["for-each-ref", "refs/argus/"]).includes(hr.ref);
      const survivedFromBundle = fetchInto(path.join(f.base, "bundle-fetch"), bundlePath);
      assert.equal(survivedFromGit && inBundle && !inPlainClone && survivedFromBundle, true);
      add(
        "survival_later_attempt",
        "hidden_refs",
        "lost",
        `not in remote/fresh clone by default (plain clone from bundle has refs/argus=${inPlainClone}); survives only with surviving .git fetch=${survivedFromGit} or git bundle --all (listed=${inBundle}) + explicit refs/argus/* fetch=${survivedFromBundle}`,
      );
    }

    // 14. Survival through workspace cleanup archive/restore (real Python modules)
    {
      const fc = await makeFixture(root, mods);
      wf(fc.wsA, "wip-cur.txt", "cur-archive-wip\n");
      assert.equal((await saveCur(fc)).ok, true);
      fs.cpSync(fc.store, path.join(fc.groupA, "embedded-store"), { recursive: true });
      const [archRootC, destC] = [path.join(fc.base, "arch-cur"), path.join(fc.base, "dest-cur")];
      fs.mkdirSync(archRootC, { recursive: true });
      const infoC = runWorkspaceArchiveAndRestore(path.join(fc.base, "inst-a"), archRootC, destC);
      const wsRestoredC = fs.existsSync(
        path.join(destC, "groups", "argus", "task-workspaces", fc.taskId, "wip-cur.txt"),
      );
      const embeddedStoreInDest = fs.existsSync(
        path.join(destC, "groups", "argus", "embedded-store"),
      );
      const extStoreStillWorks = (await restoreCur(fc)).restored;
      assert.equal(wsRestoredC && !embeddedStoreInDest && extStoreStillWorks, true);
      add(
        "survival_workspace_archive",
        "current",
        "preserved",
        `workspace repo WIP archived/restored=${wsRestoredC} (repos=${infoC.repos.length}); external store unaffected and still restores=${extStoreStillWorks}, but checkpoint store dirs are not archived by find_repos (embeddedStoreInDest=${embeddedStoreInDest})`,
      );

      const fr = await makeFixture(root, mods);
      wf(fr.wsA, "wip-ref.txt", "ref-archive-wip\n");
      const hr = saveHiddenRef(fr.wsA, fr.taskId, 1);
      fs.rmSync(path.join(fr.wsA, "wip-ref.txt")); // WIP lives ONLY in the hidden ref
      const [archRootR, destR] = [path.join(fr.base, "arch-ref"), path.join(fr.base, "dest-ref")];
      fs.mkdirSync(archRootR, { recursive: true });
      const infoR = runWorkspaceArchiveAndRestore(path.join(fr.base, "inst-a"), archRootR, destR);
      const rws = path.join(destR, "groups", "argus", "task-workspaces", fr.taskId);
      const refSurvives = git(rws, ["for-each-ref", "refs/argus/"]).includes(hr.ref);
      restoreHiddenRef(rws, hr.ref);
      assert.equal(refSurvives && rf(rws, "wip-ref.txt") === "ref-archive-wip\n", true);
      add(
        "survival_workspace_archive",
        "hidden_refs",
        "preserved",
        `workspace_archive detected hidden ref (local_commits=${infoR.repos[0]?.local_commits}, has_wip=${infoR.repos[0]?.has_wip}); workspace_restore restored ${hr.ref} and wip-ref.txt`,
      );
    }

    // Positive controls for unsafeRevert across both representations
    assert.ok(
      unsafeRevert({
        representation: "current",
        conversation: { sawChangesAt: "ckpt-2" },
        revertTarget: "ckpt-1",
        lineage: ["ckpt-1", "ckpt-2"],
      }),
    );
    assert.equal(
      unsafeRevert({
        representation: "current",
        conversation: { sawChangesAt: "ckpt-1" },
        revertTarget: "ckpt-2",
        lineage: ["ckpt-1", "ckpt-2"],
      }),
      null,
    );
    assert.ok(
      unsafeRevert({
        representation: "hidden_refs",
        conversation: { sawChangesAt: "oid-2" },
        revertTarget: "oid-1",
        targetContains: new Set(["oid-1"]),
      }),
    );
    assert.equal(
      unsafeRevert({
        representation: "hidden_refs",
        conversation: { sawChangesAt: "oid-1" },
        revertTarget: "oid-2",
        targetContains: new Set(["oid-1", "oid-2"]),
      }),
      null,
    );
    return results;
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const rows = await runComparison();
  for (const row of rows) console.log(JSON.stringify(row));
}
