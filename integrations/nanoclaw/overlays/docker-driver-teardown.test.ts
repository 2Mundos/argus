/**
 * Argus overlay — docker session teardown races (stop/rm/ps, concurrent stop).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DockerSessionDriver } from "./docker-driver.js";
import { FakeCli } from "./fake-cli.js";
import { FIXTURE_POLICY, fixtureSpec } from "./spec-fixture.js";

vi.mock("../log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));

vi.mock("fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("fs")>();
  return { default: { constants: actual.constants, existsSync: vi.fn(() => true) } };
});

let cli: FakeCli;
let clock: number;

function driver(): DockerSessionDriver {
  return new DockerSessionDriver({
    ...FIXTURE_POLICY,
    cli,
    removalConfirmDeadlineMs: 50,
    removalConfirmPollMs: 5,
    removalConfirmNow: () => clock,
    removalConfirmSleep: async (ms) => {
      clock += ms;
    },
  });
}

function patchPsSequence(outputs: Array<string | "daemon-down">) {
  let i = 0;
  const origRun = cli.run.bind(cli);
  cli.run = (args, opts) => {
    const joined = args.join(" ");
    if (/^ps /.test(joined)) {
      const next = outputs[i++] ?? outputs.at(-1) ?? "";
      if (next === "daemon-down") throw new Error("Cannot connect to the Docker daemon");
      return next;
    }
    return origRun(args, opts);
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  clock = 0;
  cli = new FakeCli("docker");
  cli.responses = [{ match: /^inspect /, throws: new Error("No such object") }];
});

describe("DockerHandle.stop teardown", () => {
  it("accepts teardown when rm fails but ps clears on retry (auto-removal race)", async () => {
    const handle = await driver().prepare(fixtureSpec());
    cli.responses.unshift({
      match: /^rm --force ncl-spike-s1$/,
      throws: new Error("removal in progress"),
    });
    patchPsSequence(["ncl-spike-s1\n", ""]);
    await expect(handle.stop("watchdog")).resolves.toBeUndefined();
  });

  it("shares one in-flight stop across concurrent callers", async () => {
    const handle = await driver().prepare(fixtureSpec());
    let rmCalls = 0;
    let psCalls = 0;
    const origRun = cli.run.bind(cli);
    cli.run = (args, opts) => {
      const joined = args.join(" ");
      if (joined === "rm --force ncl-spike-s1") {
        rmCalls += 1;
        throw new Error("busy");
      }
      if (/^ps /.test(joined)) {
        psCalls += 1;
        return psCalls < 3 ? "ncl-spike-s1\n" : "";
      }
      return origRun(args, opts);
    };
    await Promise.all([handle.stop("host-shutdown"), handle.stop("host-shutdown")]);
    expect(rmCalls).toBe(1);
    await handle.stop("again");
    expect(rmCalls).toBe(1);
  });

  it("does not report successful teardown when removal never confirms", async () => {
    const handle = await driver().prepare(fixtureSpec());
    cli.responses.unshift({ match: /^rm --force ncl-spike-s1$/, throws: new Error("busy") });
    patchPsSequence(["ncl-spike-s1\n"]);
    await expect(handle.stop("shutdown")).rejects.toMatchObject({ kind: "unknown" });
  });

  it("still refuses teardown when every probe hits a dead daemon", async () => {
    const handle = await driver().prepare(fixtureSpec());
    cli.responses.unshift({
      match: /^(stop|rm|ps) /,
      throws: new Error("Cannot connect to the Docker daemon"),
    });
    await expect(handle.stop("shutdown")).rejects.toMatchObject({ kind: "runtime-unavailable" });
  });
});
