import { mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeSelection } from "./argus.js";
import {
  discoverSessionContainersForGroup,
  runtimeFromEnv,
  runtimeTaskLines,
  sessionMountProvesGroup,
  TEST_SERVICES_DECLARATION,
} from "./argus.js";

const { groupDir, group, updateScalars, execMock } = vi.hoisted(() => {
  const groupDir =
    "/home/pedromoraes/.hermes/cache/scratch/argus-project-runtime-images-nc-tests/runtime-group-fixed";
  const updateScalars = vi.fn(
    async (_groupId: string, _scalars: { image_tag?: string }) => undefined,
  );
  const execMock = vi.fn(
    (
      _file: string,
      args: string[],
      _opts: unknown,
      cb: (err: Error | null, stdout: string, stderr: string) => void,
    ) => {
      if (args[0] === "info") return cb(null, "daemon-1|linux/amd64", "");
      if (args[0] === "image" && args[1] === "inspect")
        return cb(null, "sha256:" + "a".repeat(64), "");
      if (args[0] === "ps") return cb(null, "", "");
      if (args[0] === "inspect") return cb(null, "[]", "");
      return cb(new Error(`unexpected docker args: ${args.join(" ")}`), "", "");
    },
  );
  return {
    groupDir,
    group: { id: "g-1", folder: "worker", dir: groupDir },
    updateScalars,
    execMock,
  };
});

vi.mock("../db/messaging-groups.js", () => ({
  getMessagingGroupByPlatform: async () => ({ id: "mg-1" }),
  getMessagingGroupAgents: async () => [{ agent_group_id: group.id }],
}));
vi.mock("../db/agent-groups.js", () => ({
  getAgentGroup: async () => group,
}));
vi.mock("../db/sessions.js", () => ({
  getSessionsByAgentGroup: async () => [],
}));
vi.mock("../db/container-configs.js", () => ({
  getContainerConfig: async () => ({ image_tag: "argus-project-runtime:radar-abc" }),
  updateContainerConfigScalars: (groupId: string, scalars: { image_tag?: string }) =>
    updateScalars(groupId, scalars),
}));
vi.mock("node:child_process", async (importOriginal) => {
  const mod = await importOriginal<typeof import("node:child_process")>();
  return { ...mod, execFile: execMock };
});

const readySelection = (ref: string): RuntimeSelection => ({
  state: "ready",
  project: "radar",
  declared: true,
  requirement: [],
  image: {
    ref,
    id: "sha256:" + "b".repeat(64),
    base_ref: "argus-nanoclaw-agent:base",
    base_id: "sha256:" + "a".repeat(64),
    platform: "linux/amd64",
  },
  tools: [{ name: "psql", ok: true, output: "(PostgreSQL) 16.1" }],
  services: [
    {
      kind: "postgres",
      env: { RADAR_TEST_DATABASE_URL: "{url_asyncpg}" },
      command: "argus-test-services run -- <command>",
    },
  ],
  gaps: [{ component: "pnpm", reason: "not provisioned" }],
  task_held: false,
});

describe("runtimeTaskLines", () => {
  it("names the selected image and verified tools; gaps stay explicit", () => {
    const lines = runtimeTaskLines(readySelection("argus-project-runtime:radar-abc"));
    expect(lines.some((l) => l.includes("argus-project-runtime:radar-abc"))).toBe(true);
    expect(lines.some((l) => l.includes("psql"))).toBe(true);
    expect(lines.some((l) => l.includes("apt"))).toBe(true);
  });
});

describe("runtimeFromEnv", () => {
  const env = { ...process.env };

  beforeEach(() => {
    mkdirSync(path.resolve("groups", group.folder), { recursive: true });
    updateScalars.mockClear();
    execMock.mockClear();
    process.env.CONTAINER_IMAGE = "argus-nanoclaw-agent:base";
    delete process.env.ARGUS_PROJECT_RUNTIME;
    process.env.CONTAINER_RUNTIME_BIN = "docker";
  });

  afterEach(() => {
    process.env = { ...env };
    rmSync(path.resolve("groups", group.folder, TEST_SERVICES_DECLARATION), { force: true });
  });

  it("is disabled without CONTAINER_IMAGE or when ARGUS_PROJECT_RUNTIME=off", () => {
    delete process.env.CONTAINER_IMAGE;
    expect(runtimeFromEnv()).toBeUndefined();
    process.env.CONTAINER_IMAGE = "argus-nanoclaw-agent:base";
    process.env.ARGUS_PROJECT_RUNTIME = "off";
    expect(runtimeFromEnv()).toBeUndefined();
  });

  it("apply points the agent group at the selected image before the session starts", async () => {
    const rt = runtimeFromEnv();
    expect(rt).toBeTruthy();
    const ref = "argus-project-runtime:radar-new";
    await rt!.apply("task-1", readySelection(ref));
    expect(updateScalars).toHaveBeenCalledWith(group.id, { image_tag: ref });
    const decl = JSON.parse(
      readFileSync(path.resolve("groups", group.folder, TEST_SERVICES_DECLARATION), "utf8"),
    );
    expect(decl.task_id).toBe("task-1");
    expect(decl.services[0].kind).toBe("postgres");
  });

  it("does not rewrite image_tag when the group already runs the selected ref", async () => {
    const rt = runtimeFromEnv();
    const ref = "argus-project-runtime:radar-abc";
    await rt!.apply("task-2", readySelection(ref));
    expect(updateScalars).not.toHaveBeenCalled();
  });

  it("discovers current ncl-*-sess-* names for usedImage (inspect image id separately)", async () => {
    const nclName = "ncl-cc76dcb0-sess-1792668511005-tmegdx";
    const legacyName = `nanoclaw-v2-${group.folder}-1700000000000`;
    const otherName = "ncl-deadbeef-sess-999-other";
    const imageId = "sha256:" + "b".repeat(64);
    const docker = vi.fn(async (args: string[]) => {
      const joined = args.join(" ");
      if (args[0] === "ps") {
        if (joined.includes("label=nanoclaw-group=")) return `${nclName}\n`;
        if (joined.includes(`name=^nanoclaw-v2-${group.folder}-`)) return `${legacyName}\n`;
        return "";
      }
      const target = args[args.length - 1]!;
      if (args[2] === "{{json .Mounts}}") {
        if (target === otherName)
          return JSON.stringify([{ Source: "/tmp/evil", Destination: "/workspace" }]);
        return JSON.stringify([{ Source: groupDir, Destination: "/workspace" }]);
      }
      if (args[2] === "{{json .Config.Labels}}") {
        if (target === nclName)
          return JSON.stringify({
            "nanoclaw-group": group.id,
            "nanoclaw-group-folder": group.folder,
            "nanoclaw-role": "agent",
          });
        if (target === legacyName) return "{}";
        return JSON.stringify({ "nanoclaw-group": "ag-other", "nanoclaw-role": "agent" });
      }
      if (args[2] === "{{.Image}}") return imageId;
      throw new Error(`unexpected: ${joined}`);
    });
    const found = await discoverSessionContainersForGroup(docker, {
      id: group.id,
      folder: group.folder,
      dir: groupDir,
    });
    expect(found).toEqual([nclName, legacyName]);
    expect(await docker(["inspect", "--format", "{{.Image}}", found[0]!])).toBe(imageId);
    expect(docker).not.toHaveBeenCalledWith(
      expect.arrayContaining(["inspect", "--format", "{{.Image}}", otherName]),
    );
  });
});

describe("discoverSessionContainersForGroup", () => {
  const groupRef = { id: "ag-1", folder: "worker", dir: groupDir };

  it("accepts workspace mounts for current ncl names and legacy nanoclaw-v2 names", () => {
    const mounts = [{ Source: groupDir, Destination: "/workspace" }];
    expect(sessionMountProvesGroup(mounts, groupRef, { "nanoclaw-group": "ag-1" })).toBe(true);
    expect(sessionMountProvesGroup(mounts, groupRef, undefined)).toBe(true);
  });

  it("rejects another group's labels even when the name looks similar", () => {
    const mounts = [{ Source: groupDir, Destination: "/workspace" }];
    expect(
      sessionMountProvesGroup(mounts, groupRef, {
        "nanoclaw-group": "ag-other",
        "nanoclaw-role": "agent",
      }),
    ).toBe(false);
  });

  it("rejects containers without the group workspace mount", () => {
    expect(
      sessionMountProvesGroup(
        [{ Source: "/tmp/not-the-group", Destination: "/workspace" }],
        groupRef,
        { "nanoclaw-group": "ag-1", "nanoclaw-role": "agent" },
      ),
    ).toBe(false);
  });

  it("returns empty when docker ps fails", async () => {
    const docker = vi.fn(async () => {
      throw new Error("daemon down");
    });
    await expect(discoverSessionContainersForGroup(docker, groupRef)).rejects.toThrow(
      "daemon down",
    );
  });

  it("lists legacy and ncl names but cleanup targets only verified containers", async () => {
    const nclName = "ncl-cc76dcb0-sess-1";
    const execCalls: string[][] = [];
    const docker = vi.fn(async (args: string[]) => {
      execCalls.push(args);
      if (args[0] === "ps" && args.join(" ").includes("label=nanoclaw-group="))
        return `${nclName}\n`;
      if (args[2] === "{{json .Mounts}}")
        return JSON.stringify([{ Source: groupDir, Destination: "/workspace" }]);
      if (args[2] === "{{json .Config.Labels}}")
        return JSON.stringify({
          "nanoclaw-group": groupRef.id,
          "nanoclaw-group-folder": groupRef.folder,
          "nanoclaw-role": "agent",
        });
      return "";
    });
    const found = await discoverSessionContainersForGroup(docker, groupRef);
    expect(found).toEqual([nclName]);
    expect(execCalls.some((a) => a[0] === "ps" && a.join(" ").includes("nanoclaw-group="))).toBe(
      true,
    );
  });

  it("skips uninspectable names instead of trusting ps output", async () => {
    const docker = vi.fn(async (args: string[]) => {
      if (args[0] === "ps") return "ncl-cc76dcb0-sess-x\n";
      if (args[2] === "{{json .Mounts}}") throw new Error("no such object");
      return "{}";
    });
    await expect(discoverSessionContainersForGroup(docker, groupRef)).resolves.toEqual([]);
  });
});
