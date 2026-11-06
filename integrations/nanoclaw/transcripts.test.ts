import { describe, expect, it } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { DERIVE, readEvents } from "./transcripts-derive.js";

const ROOT = join(import.meta.dir, "fixtures", "transcripts");

const cases = readdirSync(ROOT, { withFileTypes: true })
  .filter((d) => d.isDirectory() && d.name in DERIVE)
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
