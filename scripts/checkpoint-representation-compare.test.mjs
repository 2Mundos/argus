import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import {
  git,
  loadArgusModules,
  makeFixture,
  NODE_A,
  NODE_B,
  REPO_URL,
  restoreHiddenRef,
  runComparison,
  runWorkspaceArchiveAndRestore,
  saveHiddenRef,
  sha256,
  tryGit,
  unsafeRevert,
} from "./checkpoint-representation-compare.mjs";

let mods;
let root;

before(async () => {
  mods = await loadArgusModules();
  root = fs.mkdtempSync(path.join(os.tmpdir(), "argus-ckpt-cmp-test-"));
});

after(() => {
  if (root) fs.rmSync(root, { recursive: true, force: true });
});

const saveCur = (f, extra = {}) =>
  mods.checkpoint.checkpointTaskWorkspace({
    groupDir: f.groupA,
    storeRoot: f.store,
    taskId: f.taskId,
    attempt: 1,
    repositoryUrl: REPO_URL,
    nodeId: NODE_A,
    ...extra,
  });

const restoreCur = (f, extra = {}) =>
  mods.checkpoint.restoreTaskWorkspace({
    groupDir: f.groupB,
    storeRoot: f.store,
    taskId: f.taskId,
    attempt: 2,
    repositoryUrl: REPO_URL,
    nodeId: NODE_A,
    ...extra,
  });

describe("checkpoint representation comparison", () => {
  it("committed and uncommitted changes (staged, unstaged, binary)", async () => {
    const f = await makeFixture(root, mods, { withBaseBin: true });
    fs.writeFileSync(path.join(f.wsA, "local.txt"), "unpublished commit\n");
    git(f.wsA, ["add", "local.txt"]);
    git(f.wsA, ["commit", "-q", "-m", "local commit"]);
    const localHead = git(f.wsA, ["rev-parse", "HEAD"]);

    const bStaged = Buffer.from([0x00, 0x01, 0x02, 0xff, 0xfe, 0x00, 0x89, 0x50]);
    const bUnstaged = Buffer.from([0x00, 0x10, 0x20, 0xff, 0xde, 0xad, 0xbe, 0xef]);
    const bUntracked = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x00, 0x00, 0x01]);
    fs.writeFileSync(path.join(f.wsA, "base.txt"), "base\nstaged edit\n");
    fs.writeFileSync(path.join(f.wsA, "staged-new.txt"), "staged new\n");
    fs.writeFileSync(path.join(f.wsA, "staged.bin"), bStaged);
    git(f.wsA, ["add", "base.txt", "staged-new.txt", "staged.bin"]);
    fs.writeFileSync(path.join(f.wsA, "tracked.txt"), "one\nunstaged edit\n");
    fs.writeFileSync(path.join(f.wsA, "tracked.bin"), bUnstaged);
    fs.writeFileSync(path.join(f.wsA, "untracked.bin"), bUntracked);
    const stagedDiffA = git(f.wsA, ["diff", "--cached", "--binary"]);
    const unstagedDiffA = git(f.wsA, ["diff", "--binary"]);

    // Current checkpoint preserves commit, staged/unstaged separation, and binary hashes
    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    const r = await restoreCur(f);
    assert.equal(r.ok && r.restored, true);
    assert.equal(git(f.wsB, ["rev-parse", "HEAD"]), localHead);
    assert.equal(git(f.wsB, ["diff", "--cached", "--binary"]), stagedDiffA);
    assert.equal(git(f.wsB, ["diff", "--binary"]), unstagedDiffA);
    assert.equal(sha256(fs.readFileSync(path.join(f.wsB, "staged.bin"))), sha256(bStaged));
    assert.equal(sha256(fs.readFileSync(path.join(f.wsB, "tracked.bin"))), sha256(bUnstaged));
    assert.equal(sha256(fs.readFileSync(path.join(f.wsB, "untracked.bin"))), sha256(bUntracked));

    // Hidden refs preserve commit and file/binary bytes, but flatten staged vs unstaged index
    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);
    git(f.wsA, ["reset", "-q", "--hard", f.baseHead]);
    git(f.wsA, ["clean", "-fdq"]);
    restoreHiddenRef(f.wsA, hr.ref);
    assert.equal(git(f.wsA, ["rev-parse", "HEAD"]), localHead);
    assert.equal(fs.readFileSync(path.join(f.wsA, "base.txt"), "utf8"), "base\nstaged edit\n");
    assert.equal(fs.readFileSync(path.join(f.wsA, "staged-new.txt"), "utf8"), "staged new\n");
    assert.equal(fs.readFileSync(path.join(f.wsA, "tracked.txt"), "utf8"), "one\nunstaged edit\n");
    assert.equal(sha256(fs.readFileSync(path.join(f.wsA, "staged.bin"))), sha256(bStaged));
    assert.equal(sha256(fs.readFileSync(path.join(f.wsA, "tracked.bin"))), sha256(bUnstaged));
    assert.equal(sha256(fs.readFileSync(path.join(f.wsA, "untracked.bin"))), sha256(bUntracked));
    assert.equal(git(f.wsA, ["diff", "--cached"]), "");
  });

  it("untracked files are preserved and ignored files are omitted in both", async () => {
    const f = await makeFixture(root, mods);
    fs.mkdirSync(path.join(f.wsA, "src", "deep"), { recursive: true });
    fs.writeFileSync(path.join(f.wsA, "src", "deep", "new.txt"), "hello\n");
    fs.writeFileSync(path.join(f.wsA, "run.sh"), "#!/bin/sh\n", { mode: 0o755 });
    fs.chmodSync(path.join(f.wsA, "run.sh"), 0o755);
    fs.symlinkSync("src/deep/new.txt", path.join(f.wsA, "link.txt"));
    fs.writeFileSync(path.join(f.wsA, "ignored.log"), "noise\n");

    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    const r = await restoreCur(f);
    assert.equal(r.ok && r.restored, true);
    assert.equal(fs.readFileSync(path.join(f.wsB, "src", "deep", "new.txt"), "utf8"), "hello\n");
    assert.notEqual(fs.lstatSync(path.join(f.wsB, "run.sh")).mode & 0o111, 0);
    assert.equal(fs.readlinkSync(path.join(f.wsB, "link.txt")), "src/deep/new.txt");
    assert.equal(fs.existsSync(path.join(f.wsB, "ignored.log")), false);

    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);
    git(f.wsA, ["reset", "-q", "--hard", f.baseHead]);
    git(f.wsA, ["clean", "-fdxq"]);
    restoreHiddenRef(f.wsA, hr.ref);
    assert.equal(fs.readFileSync(path.join(f.wsA, "src", "deep", "new.txt"), "utf8"), "hello\n");
    assert.notEqual(fs.lstatSync(path.join(f.wsA, "run.sh")).mode & 0o111, 0);
    assert.equal(fs.readlinkSync(path.join(f.wsA, "link.txt")), "src/deep/new.txt");
    assert.equal(fs.existsSync(path.join(f.wsA, "ignored.log")), false);
  });

  it("non-Git state cannot be represented by either format", async () => {
    const taskId = randomUUID();
    const base = fs.mkdtempSync(path.join(root, "nongit-"));
    const groupDir = path.join(base, "group");
    const storeRoot = path.join(base, "store");
    const ws = mods.workspace.taskWorkspacePath(groupDir, taskId);
    fs.mkdirSync(ws, { recursive: true });
    fs.writeFileSync(path.join(ws, "plain.txt"), "no git directory here\n");

    const c = await mods.checkpoint.checkpointTaskWorkspace({
      groupDir,
      storeRoot,
      taskId,
      attempt: 1,
      repositoryUrl: REPO_URL,
      nodeId: NODE_A,
    });
    assert.equal(c.ok, false);
    assert.equal(c.reason, "env_checkpoint_invalid");

    const hr = saveHiddenRef(ws, taskId, 1);
    assert.equal(hr.ok, false);
    assert.match(hr.error, /not a git repository/i);
  });

  it("secrets by name (.env, *.pem) and committed secret-named files", async () => {
    const f = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f.wsA, "committed.pem"), "fixture-committed-pem-placeholder\n");
    git(f.wsA, ["add", "committed.pem"]);
    git(f.wsA, ["commit", "-q", "-m", "commit fixture pem"]);
    fs.writeFileSync(path.join(f.wsA, ".env"), "APP_DUMMY_VAR=fixture-placeholder-not-secret\n");
    fs.writeFileSync(path.join(f.wsA, "cert.pem"), "fixture-pem-placeholder-not-a-real-key\n");

    assert.equal(mods.checkpoint.isSensitive(".env"), true);
    assert.equal(mods.checkpoint.isSensitive("cert.pem"), true);
    assert.equal(mods.checkpoint.isSensitive("base.txt"), false);

    // Current excludes untracked/dirty sensitive names and audits committed ones
    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    const manifest = JSON.parse(fs.readFileSync(path.join(f.store, f.taskId, c.id, "manifest.json"), "utf8"));
    assert.deepEqual(manifest.skipped_sensitive, [".env", "cert.pem"]);
    assert.deepEqual(manifest.sensitive_in_commits, ["committed.pem"]);
    const r = await restoreCur(f);
    assert.equal(r.ok && r.restored, true);
    assert.equal(fs.existsSync(path.join(f.wsB, ".env")), false);
    assert.equal(fs.existsSync(path.join(f.wsB, "cert.pem")), false);
    assert.equal(fs.existsSync(path.join(f.wsB, "committed.pem")), true);

    // Hidden refs capture .env and cert.pem into git objects without filtering or audit
    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);
    git(f.wsA, ["reset", "-q", "--hard", f.baseHead]);
    git(f.wsA, ["clean", "-fdq"]);
    restoreHiddenRef(f.wsA, hr.ref);
    assert.equal(fs.existsSync(path.join(f.wsA, ".env")), true);
    assert.equal(fs.existsSync(path.join(f.wsA, "cert.pem")), true);
    assert.equal(fs.existsSync(path.join(f.wsA, "committed.pem")), true);
  });

  it("disk limits: current enforces ARGUS_CHECKPOINT_MAX_BYTES while hidden refs ignore it", async () => {
    const f = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f.wsA, "large.bin"), Buffer.alloc(4096, 0x41));

    const prev = process.env.ARGUS_CHECKPOINT_MAX_BYTES;
    process.env.ARGUS_CHECKPOINT_MAX_BYTES = "1024";
    let c;
    try {
      c = await saveCur(f);
    } finally {
      if (prev === undefined) delete process.env.ARGUS_CHECKPOINT_MAX_BYTES;
      else process.env.ARGUS_CHECKPOINT_MAX_BYTES = prev;
    }
    assert.equal(c.ok, false);
    assert.equal(c.reason, "env_checkpoint_limit");
    const entries = fs.existsSync(path.join(f.store, f.taskId)) ? fs.readdirSync(path.join(f.store, f.taskId)) : [];
    assert.deepEqual(entries, []);

    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);
    assert.equal(git(f.wsA, ["rev-parse", hr.ref]), hr.oid);
  });

  it("integrity verification: current manifest/file sha256 vs git object hash only", async () => {
    const f = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f.wsA, "tracked.txt"), "one\nedited\n");

    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    const parsed = mods.checkpoint.parseCheckpointRef(c.ref);
    assert.deepEqual(parsed, { id: c.id, sha256: c.manifestSha256 });
    assert.equal(mods.checkpoint.checkpointRef(c.id, c.manifestSha256), c.ref);

    // Wrong expected manifest digest is rejected
    const badDigest = await restoreCur(f, { expected: { id: c.id, sha256: "0".repeat(64) } });
    assert.equal(badDigest.ok, false);
    assert.equal(badDigest.reason, "env_checkpoint_invalid");

    // Same-size 1-bit flip in unstaged.patch triggers SHA-256 verification failure
    const patchFile = path.join(f.store, f.taskId, c.id, "unstaged.patch");
    fs.chmodSync(patchFile, 0o600);
    const flipped = Buffer.from(fs.readFileSync(patchFile));
    flipped[flipped.length - 2] ^= 0x01;
    fs.writeFileSync(patchFile, flipped);
    const tampered = await restoreCur(f, { expected: parsed });
    assert.equal(tampered.ok, false);
    assert.equal(tampered.reason, "env_checkpoint_invalid");
    assert.match(tampered.error, /fails its hash/);

    // Hidden refs: git catches corrupt loose object, but has no manifest digest or identity check
    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    const blobOid = git(f.wsA, ["rev-parse", `${hr.ref}:tracked.txt`]);
    const objPath = path.join(f.wsA, ".git", "objects", blobOid.slice(0, 2), blobOid.slice(2));
    fs.chmodSync(objPath, 0o600);
    const orig = fs.readFileSync(objPath);
    const corrupt = Buffer.from(orig);
    corrupt[corrupt.length - 1] ^= 0xff;
    fs.writeFileSync(objPath, corrupt);
    assert.equal(tryGit(f.wsA, ["cat-file", "-p", blobOid]).ok, false);
    fs.writeFileSync(objPath, orig);
    // Repointing ref to an unrelated commit succeeds silently
    git(f.wsA, ["update-ref", hr.ref, f.baseHead]);
    assert.equal(git(f.wsA, ["rev-parse", hr.ref]), f.baseHead);
  });

  it("cleanup and restore after worker or node loss", async () => {
    const f = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f.wsA, "wip.txt"), "valuable-work\n");
    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);

    // Copy .git separately to test the narrow case where only the object store survives
    const survivingGit = path.join(f.base, "surviving.git");
    fs.cpSync(path.join(f.wsA, ".git"), survivingGit, { recursive: true });

    // Remove the entire worker instance A directory (worker loss / workspace cleanup)
    fs.rmSync(path.join(f.base, "inst-a"), { recursive: true, force: true });
    assert.equal(fs.existsSync(f.wsA), false);

    // Current checkpoint survives worker loss on the same node
    const rSameNode = await restoreCur(f);
    assert.equal(rSameNode.ok && rSameNode.restored, true);
    assert.equal(fs.readFileSync(path.join(f.wsB, "wip.txt"), "utf8"), "valuable-work\n");

    // Node loss / different nodeId fails closed with env_checkpoint_unavailable
    const groupC = path.join(f.base, "inst-c", "groups", "argus");
    fs.mkdirSync(groupC, { recursive: true });
    const rDiffNode = await restoreCur(f, { groupDir: groupC, attempt: 3, nodeId: NODE_B });
    assert.equal(rDiffNode.ok, false);
    assert.equal(rDiffNode.reason, "env_checkpoint_unavailable");

    // Hidden refs are gone once wsA/.git is deleted; recoverable only from the surviving .git copy
    const recovered = path.join(f.base, "recovered-from-git");
    fs.mkdirSync(recovered, { recursive: true });
    git(recovered, ["init", "-q"]);
    git(recovered, ["fetch", "-q", survivingGit, "refs/argus/*:refs/argus/*"]);
    restoreHiddenRef(recovered, hr.ref);
    assert.equal(fs.readFileSync(path.join(recovered, "wip.txt"), "utf8"), "valuable-work\n");
  });

  it("survival into a later attempt: checkpoint restore vs surviving .git vs git bundle --all", async () => {
    const f = await makeFixture(root, mods);
    // Set up a bare remote standing in for origin
    const remoteBare = path.join(f.base, "origin.git");
    git(f.base, ["init", "--bare", "-q", remoteBare]);
    git(f.wsA, ["push", "-q", remoteBare, "HEAD:refs/heads/main"]);
    fs.writeFileSync(path.join(f.wsA, "attempt1.txt"), "attempt-1-wip\n");

    // Current: restoreTaskWorkspace into fresh groupB
    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    const r = await restoreCur(f, { expected: mods.checkpoint.parseCheckpointRef(c.ref) });
    assert.equal(r.ok && r.restored, true);
    assert.equal(fs.readFileSync(path.join(f.wsB, "attempt1.txt"), "utf8"), "attempt-1-wip\n");

    // Hidden refs
    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);

    // (0) Fresh attempt cloning origin bare remote does not have hidden refs
    const freshOriginClone = path.join(f.base, "fresh-origin-clone");
    git(f.base, ["clone", "-q", remoteBare, freshOriginClone]);
    assert.equal(git(freshOriginClone, ["for-each-ref", "refs/argus/"]), "");

    // (i) When repo's .git survives, explicit fetch recovers the hidden ref
    const freshFromGit = path.join(f.base, "fresh-from-git");
    fs.mkdirSync(freshFromGit, { recursive: true });
    git(freshFromGit, ["init", "-q"]);
    git(freshFromGit, ["fetch", "-q", f.wsA, "refs/argus/*:refs/argus/*"]);
    restoreHiddenRef(freshFromGit, hr.ref);
    assert.equal(fs.readFileSync(path.join(freshFromGit, "attempt1.txt"), "utf8"), "attempt-1-wip\n");

    // (ii) git bundle create --all includes refs/argus/*; plain clone omits them; explicit fetch recovers them
    const bundlePath = path.join(f.base, "work.bundle");
    git(f.wsA, ["bundle", "create", bundlePath, "--all"]);
    assert.equal(git(f.wsA, ["bundle", "list-heads", bundlePath]).includes(hr.ref), true);

    const plainClone = path.join(f.base, "plain-bundle-clone");
    git(f.base, ["clone", "-q", bundlePath, plainClone]);
    assert.equal(git(plainClone, ["for-each-ref", "refs/argus/"]).includes(hr.ref), false);

    const explicitFetch = path.join(f.base, "explicit-bundle-fetch");
    fs.mkdirSync(explicitFetch, { recursive: true });
    git(explicitFetch, ["init", "-q"]);
    git(explicitFetch, ["fetch", "-q", bundlePath, "refs/argus/*:refs/argus/*"]);
    restoreHiddenRef(explicitFetch, hr.ref);
    assert.equal(fs.readFileSync(path.join(explicitFetch, "attempt1.txt"), "utf8"), "attempt-1-wip\n");
  });

  it("survival through workspace cleanup archive/restore: current checkpoint path", async () => {
    const f = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f.wsA, "wip-cur.txt"), "archived-cur-wip\n");
    const c = await saveCur(f);
    assert.equal(c.ok && !c.empty, true);
    // Also place a copy of the checkpoint store inside inst-a to test whether find_repos archives it
    fs.cpSync(f.store, path.join(f.groupA, "embedded-store"), { recursive: true });

    const archRoot = path.join(f.base, "arch-cur");
    const dest = path.join(f.base, "dest-cur");
    fs.mkdirSync(archRoot, { recursive: true });
    const info = runWorkspaceArchiveAndRestore(path.join(f.base, "inst-a"), archRoot, dest);

    assert.equal(info.repos.length, 1);
    assert.equal(info.repos[0].has_wip, true);
    const restoredWs = path.join(dest, "groups", "argus", "task-workspaces", f.taskId);
    assert.equal(fs.readFileSync(path.join(restoredWs, "wip-cur.txt"), "utf8"), "archived-cur-wip\n");
    // Non-git checkpoint store directories are not archived by workspace_archive
    assert.equal(fs.existsSync(path.join(dest, "groups", "argus", "embedded-store")), false);
    // External node-local checkpoint store survives instance deletion independently
    const r = await restoreCur(f);
    assert.equal(r.ok && r.restored, true);
  });

  it("survival through workspace cleanup archive/restore: hidden refs path", async () => {
    const f = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f.wsA, "wip-ref.txt"), "archived-ref-wip\n");
    const hr = saveHiddenRef(f.wsA, f.taskId, 1);
    assert.equal(hr.ok, true);
    // Remove working-tree copy so the hidden ref is the ONLY record of wip-ref.txt
    fs.rmSync(path.join(f.wsA, "wip-ref.txt"));

    const archRoot = path.join(f.base, "arch-ref");
    const dest = path.join(f.base, "dest-ref");
    fs.mkdirSync(archRoot, { recursive: true });
    const info = runWorkspaceArchiveAndRestore(path.join(f.base, "inst-a"), archRoot, dest);

    assert.equal(info.repos.length, 1);
    assert.equal(info.repos[0].local_commits, 1);
    assert.equal(info.repos[0].has_wip, true);
    const restoredWs = path.join(dest, "groups", "argus", "task-workspaces", f.taskId);
    assert.equal(git(restoredWs, ["for-each-ref", "refs/argus/"]).includes(hr.ref), true);
    restoreHiddenRef(restoredWs, hr.ref);
    assert.equal(fs.readFileSync(path.join(restoredWs, "wip-ref.txt"), "utf8"), "archived-ref-wip\n");
  });

  it("unsafeRevert detects reverting below conversation state and allows safe reverts for both representations", async () => {
    // Current checkpoint representation with two real sequential checkpoints
    const f1 = await makeFixture(root, mods);
    let clock = Date.parse("2026-11-21T12:00:00Z");
    fs.writeFileSync(path.join(f1.wsA, "step.txt"), "v1\n");
    const ckpt1 = await saveCur(f1, { attempt: 1, now: () => (clock += 2000) });
    fs.writeFileSync(path.join(f1.wsA, "step.txt"), "v2\n");
    const ckpt2 = await saveCur(f1, { attempt: 2, now: () => (clock += 2000) });
    assert.equal(ckpt1.ok && !ckpt1.empty && ckpt2.ok && !ckpt2.empty, true);
    const lineageCur = [ckpt1.id, ckpt2.id];

    // Negative test: conversation saw ckpt2, revert target is older ckpt1 -> rejected
    const reasonCur = unsafeRevert({
      representation: "current",
      conversation: { sawChangesAt: ckpt2.id },
      revertTarget: ckpt1.id,
      lineage: lineageCur,
    });
    assert.equal(typeof reasonCur, "string");
    assert.match(reasonCur, /older than changes seen/);

    // Positive controls: revert target matches or supersedes conversation state -> null
    assert.equal(
      unsafeRevert({
        representation: "current",
        conversation: { sawChangesAt: ckpt2.id },
        revertTarget: ckpt2.id,
        lineage: lineageCur,
      }),
      null,
    );
    assert.equal(
      unsafeRevert({
        representation: "current",
        conversation: { sawChangesAt: ckpt1.id },
        revertTarget: ckpt2.id,
        lineage: lineageCur,
      }),
      null,
    );

    // Hidden refs representation with real git commit ancestry
    const f2 = await makeFixture(root, mods);
    fs.writeFileSync(path.join(f2.wsA, "step.txt"), "v1\n");
    git(f2.wsA, ["add", "step.txt"]);
    git(f2.wsA, ["commit", "-q", "-m", "v1"]);
    const oid1 = git(f2.wsA, ["rev-parse", "HEAD"]);
    fs.writeFileSync(path.join(f2.wsA, "step.txt"), "v2\n");
    const ref2 = saveHiddenRef(f2.wsA, f2.taskId, 2);
    assert.equal(ref2.ok, true);
    const ancestorsOf1 = new Set(git(f2.wsA, ["rev-list", oid1]).split("\n").filter(Boolean));
    const ancestorsOf2 = new Set(git(f2.wsA, ["rev-list", ref2.oid]).split("\n").filter(Boolean));

    // Negative test: conversation saw ref2.oid, revert target is older oid1 -> rejected
    const reasonRef = unsafeRevert({
      representation: "hidden_refs",
      conversation: { sawChangesAt: ref2.oid },
      revertTarget: oid1,
      targetContains: ancestorsOf1,
    });
    assert.equal(typeof reasonRef, "string");
    assert.match(reasonRef, /not contained in revert target/);

    // Positive controls: ref2.oid contains oid1 (and itself) -> null
    assert.equal(
      unsafeRevert({
        representation: "hidden_refs",
        conversation: { sawChangesAt: oid1 },
        revertTarget: ref2.oid,
        targetContains: ancestorsOf2,
      }),
      null,
    );
    assert.equal(
      unsafeRevert({
        representation: "hidden_refs",
        conversation: { sawChangesAt: ref2.oid },
        revertTarget: ref2.oid,
        targetContains: ancestorsOf2,
      }),
      null,
    );

    // Sibling hidden refs created from the same HEAD lack git ancestry -> fail closed
    const sib1 = saveHiddenRef(f2.wsA, f2.taskId, 3);
    fs.writeFileSync(path.join(f2.wsA, "step.txt"), "v3\n");
    const sib2 = saveHiddenRef(f2.wsA, f2.taskId, 4);
    const ancestorsOfSib2 = new Set(git(f2.wsA, ["rev-list", sib2.oid]).split("\n").filter(Boolean));
    assert.equal(ancestorsOfSib2.has(sib1.oid), false);
    const siblingReason = unsafeRevert({
      representation: "hidden_refs",
      conversation: { sawChangesAt: sib1.oid },
      revertTarget: sib2.oid,
      targetContains: ancestorsOfSib2,
    });
    assert.equal(typeof siblingReason, "string");
  });

  it("runComparison emits valid JSONL schema for all dimensions and passes positive controls", async () => {
    const rows = await runComparison();
    assert.equal(rows.length, 28);
    const allowedOutcomes = new Set(["preserved", "lost", "cannot_represent"]);
    const allowedReps = new Set(["current", "hidden_refs"]);
    for (const row of rows) {
      assert.equal(typeof row.dimension, "string");
      assert.equal(allowedReps.has(row.representation), true);
      assert.equal(allowedOutcomes.has(row.outcome), true);
      assert.equal(typeof row.evidence, "string");
      assert.ok(row.evidence.length > 0);
    }
  });
});
