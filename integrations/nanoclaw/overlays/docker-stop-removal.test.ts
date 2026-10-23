import { describe, expect, it, vi } from "vitest";
import { normalizeDockerError } from "./docker-driver.js";
import {
  DEFAULT_REMOVAL_DEADLINE_MS,
  DEFAULT_REMOVAL_POLL_MS,
  dockerPsListsName,
  dockerPsNameFilterArgs,
  waitForContainerRemoval,
} from "./docker-stop-removal.js";
import { FakeCli } from "./fake-cli.js";

describe("dockerPsListsName", () => {
  it("treats an empty ps table as removed", () => {
    expect(dockerPsListsName("", "ncl-spike-s1")).toBe(false);
    expect(dockerPsListsName("\n  \n", "ncl-spike-s1")).toBe(false);
  });

  it("matches bare and slash-prefixed docker names", () => {
    expect(dockerPsListsName("ncl-spike-s1\n", "ncl-spike-s1")).toBe(true);
    expect(dockerPsListsName("/ncl-spike-s1\n", "ncl-spike-s1")).toBe(true);
    expect(dockerPsListsName("other\n", "ncl-spike-s1")).toBe(false);
  });
});

describe("waitForContainerRemoval", () => {
  it("uses the anchored name filter Argus operators rely on", () => {
    expect(dockerPsNameFilterArgs("ncl-spike-s1")).toEqual([
      "ps",
      "-a",
      "--filter",
      "name=^/ncl-spike-s1$",
      "--format",
      "{{.Names}}",
    ]);
  });

  it("succeeds immediately when ps is already empty after rm failed", async () => {
    const cli = new FakeCli();
    cli.responses = [{ match: /^ps /, output: "" }];
    await expect(
      waitForContainerRemoval({
        cli,
        name: "ncl-spike-s1",
        rmError: new Error("removal already in progress"),
        normalizeError: normalizeDockerError,
      }),
    ).resolves.toBeUndefined();
    expect(cli.joined().filter((c) => c.startsWith("ps "))).toHaveLength(1);
  });

  it("polls until the name disappears (stop/rm/ps race)", async () => {
    const cli = new FakeCli();
    let psCalls = 0;
    const origRun = cli.run.bind(cli);
    cli.run = (args, opts) => {
      if (args.join(" ").startsWith("ps ")) {
        psCalls += 1;
        return psCalls < 3 ? "ncl-spike-s1\n" : "";
      }
      return origRun(args, opts);
    };
    const sleeps: number[] = [];
    await waitForContainerRemoval({
      cli,
      name: "ncl-spike-s1",
      rmError: new Error("conflict"),
      normalizeError: normalizeDockerError,
      pollMs: 5,
      deadlineMs: 100,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      now: () => 0,
    });
    expect(psCalls).toBe(3);
    expect(sleeps).toEqual([5, 5]);
  });

  it("throws runtime-unavailable when the daemon is down during confirmation", async () => {
    const cli = new FakeCli();
    cli.responses = [{ match: /^ps /, throws: new Error("Cannot connect to the Docker daemon") }];
    await expect(
      waitForContainerRemoval({
        cli,
        name: "ncl-spike-s1",
        rmError: new Error("rm failed"),
        normalizeError: normalizeDockerError,
      }),
    ).rejects.toMatchObject({ kind: "runtime-unavailable" });
  });

  it("fails closed when the name is still listed at the deadline", async () => {
    const cli = new FakeCli();
    cli.responses = [{ match: /^ps /, output: "ncl-spike-s1\n" }];
    const now = vi.fn();
    now.mockReturnValueOnce(0).mockReturnValueOnce(0).mockReturnValue(DEFAULT_REMOVAL_DEADLINE_MS);
    await expect(
      waitForContainerRemoval({
        cli,
        name: "ncl-spike-s1",
        rmError: new Error("still there"),
        normalizeError: normalizeDockerError,
        pollMs: DEFAULT_REMOVAL_POLL_MS,
        deadlineMs: DEFAULT_REMOVAL_DEADLINE_MS,
        sleep: async () => {},
        now,
      }),
    ).rejects.toMatchObject({ kind: "unknown" });
  });
});
