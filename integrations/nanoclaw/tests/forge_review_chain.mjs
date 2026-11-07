// Driver for test_forge_review_chain.py: one task claim (JSON from Argus) goes through the
// channel's real policy code to an environment, then through the real `argus-task pr-ready`
// against a fake forge. Reads {claim, argv, routes, head_sequence?} on stdin; prints
// {env, lines, code, out}. Node strips the TypeScript types of argus-forge-review.ts.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { forgeReviewLines, scopeForgeReview } from "../argus-forge-review.ts";
import { main } from "../argus-task.mjs";

const REPO = "https://git.example.com/team/project";
const API = "/api/v1/repos/team/project";
const { claim, argv, routes, head_sequence: heads } = JSON.parse(readFileSync(0, "utf8"));

const env = {
  ARGUS_REPOSITORY_URL: REPO,
  ARGUS_REPOSITORIES_JSON: JSON.stringify({ p: REPO }),
};
scopeForgeReview(env, claim);

const cwd = mkdtempSync(join(tmpdir(), "forge-review-chain-"));
execFileSync("git", ["init", "-q", "-b", "main"], { cwd });

let pullReads = 0;
const fetch = async (target, init = {}) => {
  const url = new URL(target);
  const key = `${init.method ?? "GET"} ${url.pathname.slice(API.length) || "/"}`;
  let value = routes[key];
  if (value === undefined) return new Response("{}", { status: 404 });
  if (key === "GET /pulls/7" && heads) {
    value = { ...value, head: { sha: heads[Math.min(pullReads++, heads.length - 1)] } };
  }
  return Response.json(value);
};

const out = [];
const code = await main(argv, env, {
  fetch,
  cwd,
  retrySleep: async () => {},
  stdout: { write: (s) => out.push(s) },
  stderr: { write: () => {} },
});
console.log(
  JSON.stringify({
    env: env.ARGUS_FORGE_REVIEW ?? null,
    lines: forgeReviewLines(claim),
    code,
    out: out.length ? JSON.parse(out.join("")) : null,
  }),
);
