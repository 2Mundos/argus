import { describe, expect, it } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { parseUsage } from "./engines/usage.mjs";
import { DERIVE, deriveCopilot, readEvents } from "./transcripts-derive.js";

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
