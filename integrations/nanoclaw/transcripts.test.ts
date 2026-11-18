import { describe, expect, it } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { parseUsage } from "./engines/usage.mjs";
import {
  DERIVE,
  deriveCopilot,
  deriveStall,
  readEvents,
  type StallRow,
} from "./transcripts-derive.js";

const ROOT = join(import.meta.dir, "fixtures", "transcripts");

// Copilot recordings are plain text (stdout.txt), not events.jsonl; they are replayed below.
const TEXT_ONLY = new Set(["copilot"]);

// Copilot cases every recording set must carry, with the authRejected each derives.
const COPILOT_REQUIRED: [string, boolean][] = [
  ["success", false],
  ["auth-rejected", true],
];

// The only way a required Copilot case may be absent. Remove a case from here when it is recorded.
const COPILOT_NOT_RECORDED_REASON =
  "no Copilot executor route in this deployment (argus-delegate copilot exits 78, auth_missing; no copilot account in the registry)";
const COPILOT_NOT_RECORDED = new Set(["success", "auth-rejected"]);

const cases = readdirSync(ROOT, { withFileTypes: true })
  .filter((d) => d.isDirectory() && d.name in DERIVE && !TEXT_ONLY.has(d.name))
  .flatMap((engine) =>
    readdirSync(join(ROOT, engine.name), { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map(
        (c) => [`${engine.name}/${c.name}`, engine.name as keyof typeof DERIVE, c.name] as const,
      ),
  );

describe("engine transcripts", () => {
  it("has recordings", () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  it.each(cases)("%s derives what expected.json says", (_label, engine, name) => {
    const dir = join(ROOT, engine, name);
    // A case with no engine output (the engine was killed before it wrote an event) has
    // stderr.txt instead of events.jsonl and derives from zero events.
    const eventsFile = join(dir, "events.jsonl");
    const events =
      existsSync(eventsFile) || !existsSync(join(dir, "stderr.txt")) ? readEvents(eventsFile) : [];
    const expected = JSON.parse(readFileSync(join(dir, "expected.json"), "utf8"));
    const derived = DERIVE[engine](events as never) as Record<string, unknown>;
    for (const key of Object.keys(expected)) expect(derived[key]).toEqual(expected[key]);
    expect(Object.keys(derived).sort()).toEqual(Object.keys(expected).sort());
  });

  it.each(cases)("%s keeps stderr.txt bounded and redacted", (_label, engine, name) => {
    const file = join(ROOT, engine, name, "stderr.txt");
    if (!existsSync(file)) return;
    const text = readFileSync(file, "utf8");
    expect(Buffer.byteLength(text)).toBeLessThanOrEqual(16384);
    expect(text).not.toMatch(/\/(?:home|Users)\/(?!user(?:\/|\s|$))/);
    expect(text).not.toMatch(/@[\w-]+(?:\.[\w-]+)+/);
  });
});

describe("copilot transcripts", () => {
  for (const [name, authRejected] of COPILOT_REQUIRED) {
    const dir = join(ROOT, "copilot", name);
    const listed = COPILOT_NOT_RECORDED.has(name);
    const present = existsSync(dir);

    if (listed) {
      it(`copilot/${name} is not recorded: ${COPILOT_NOT_RECORDED_REASON}`, () => {
        if (present)
          throw new Error(`copilot/${name} exists: remove it from the not-recorded list`);
        expect(present).toBe(false);
      });
    } else {
      it(`copilot/${name} derives what expected.json says`, () => {
        if (!present) throw new Error(`copilot/${name} is missing: the fixture was removed`);
        const stderrFile = join(dir, "stderr.txt");
        const stdout = readFileSync(join(dir, "stdout.txt"), "utf8");
        const stderr = existsSync(stderrFile) ? readFileSync(stderrFile, "utf8") : "";
        const expected = JSON.parse(readFileSync(join(dir, "expected.json"), "utf8"));
        const derived = deriveCopilot({ stdout, stderr }) as Record<string, unknown>;
        expect(derived.authRejected).toBe(authRejected);
        for (const key of Object.keys(expected)) expect(derived[key]).toEqual(expected[key]);
        expect(Object.keys(derived).sort()).toEqual(Object.keys(expected).sort());
      });
    }
  }
});

const probe = JSON.parse(
  readFileSync(join(import.meta.dir, "fixtures", "copilot-cli", "probe-1.0.105.json"), "utf8"),
);

describe("copilot 1.0.105 failure signals", () => {
  const rejected: [string, string][] = [
    ...(probe.stableFailureSignals.authRejected as string[]).map((l): [string, string] => [l, l]),
    ["classicToken", probe.stableFailureSignals.classicToken],
  ];

  it.each(rejected)("%s is an auth rejection", (_label, line) => {
    expect(deriveCopilot(line).authRejected).toBe(true);
    expect(deriveCopilot({ stdout: "", stderr: `${line}\n` }).authRejected).toBe(true);
  });

  it("treats ordinary answer text as no rejection and no usage", () => {
    const derived = deriveCopilot("OK");
    expect(derived.authRejected).toBe(false);
    expect(derived.usage).toEqual(parseUsage("copilot", "OK"));
    expect(derived.usage).toMatchObject({ reason: "no_usage_output" });
  });
});

// The production stall classifier is Python (argus_api.tasks.expiry), fed by what the worker
// host observes for a lease, not by engine output. So a recording cannot say *why* a claim went
// quiet; expected.json deliberately has no stall field, and nothing below edits a recording.
const NOW = "2026-10-20T22:06:00+00:00";
const CLAIMED_AT = "2026-10-20T20:00:00+00:00";
const FRESH = "2026-10-20T22:05:30+00:00"; // 30 s before NOW: inside the 90 s window
const STALE = "2026-10-20T22:00:00+00:00"; // 6 min before NOW: older than three heartbeats
const obs = (workspace: string, session: string, delegate: string) =>
  JSON.stringify({ workspace, session, delegate });
const quietClaim = (observation: string | null, observation_at: string | null): StallRow => ({
  state: "claimed",
  quiet_at: "2026-10-20T21:40:00+00:00",
  claimed_at: CLAIMED_AT,
  observation,
  observation_at,
});

describe("stall classifier on authentic recordings", () => {
  // A recording holds engine output only, never a lease observation, so the contract-correct
  // input for a quiet claim built from it is "no observation", which the classifier calls
  // `unknown` (stall-visibility.spec.md 3.7). That is the whole claim; no cause is inferred.
  it.each(cases)("%s: no recorded observation is an unknown cause", (_label, engine, name) => {
    expect(existsSync(join(ROOT, engine, name, "expected.json"))).toBe(true);
    expect(deriveStall(quietClaim(null, null), NOW)).toMatchObject({
      quiet: true,
      stall_cause: "unknown",
    });
  });
});

describe("stall classifier on SYNTHETIC inputs (not recordings)", () => {
  // Hand-built rows: nothing here was recorded from a real host or engine.
  const synthetic: [string, StallRow, string | null][] = [
    [
      "no stall: claim with recent signals is not quiet",
      { ...quietClaim(null, null), quiet_at: null },
      null,
    ],
    [
      "no stall: a queued task reports no cause",
      { ...quietClaim(obs("missing", "gone", "silent"), FRESH), state: "queued" },
      null,
    ],
    [
      "workspace missing wins over everything",
      quietClaim(obs("missing", "gone", "active"), FRESH),
      "workspace_missing",
    ],
    ["session gone", quietClaim(obs("ok", "gone", "active"), FRESH), "session_gone"],
    [
      "delegate active but silent in the log",
      quietClaim(obs("ok", "running", "active"), FRESH),
      "silent_delegate_active",
    ],
    [
      "healthy session, no progress",
      quietClaim(obs("ok", "running", "silent"), FRESH),
      "no_progress",
    ],
    ["partly unobserved", quietClaim(obs("ok", "unknown", "silent"), FRESH), "unknown"],
    ["stale observation", quietClaim(obs("missing", "gone", "silent"), STALE), "unknown"],
    [
      "observation from before the claim",
      quietClaim(obs("missing", "gone", "silent"), "2026-10-20T19:59:00+00:00"),
      "unknown",
    ],
    [
      "disk guard pause beats the observation",
      {
        ...quietClaim(obs("ok", "running", "silent"), FRESH),
        quiet_at: null,
        paused_disk_at: FRESH,
      },
      "paused_disk",
    ],
  ];

  it.each(synthetic)("%s", (_label, row, cause) => {
    const facts = deriveStall(row, NOW);
    expect(facts.stall_cause).toBe(cause);
    expect(facts.quiet).toBe(row.state === "claimed" && row.quiet_at !== null);
  });
});
