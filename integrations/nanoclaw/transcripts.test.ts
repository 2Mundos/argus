import { describe, expect, it } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { parseUsage } from "./engines/usage.mjs";
import { DERIVE, deriveCopilot, readEvents } from "./transcripts-derive.js";

const ROOT = join(import.meta.dir, "fixtures", "transcripts");

// Copilot recordings are plain text, not events.jsonl; it has no success recording (see below).
const TEXT_ONLY = new Set(["copilot"]);

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
    const events = readEvents(join(dir, "events.jsonl"));
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
