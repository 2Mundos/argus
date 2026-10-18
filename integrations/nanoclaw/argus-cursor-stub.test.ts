import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  augmentCursorContainerConfig,
  CURSOR_AUTH_CONTAINER_PATH,
  CURSOR_CLI_CONFIG_CONTAINER_PATH,
  CURSOR_CONFIG_CONTAINER_DIR,
  CURSOR_SENTINEL_ACCESS_TOKEN,
  cursorAuthStubContent,
  cursorExecutorAssigned,
  ensureHttp1CliConfig,
  fixtureOnecliFetch,
  gatewayFixturesDir,
  isCursorGenericSecret,
  withCursorDelegateRuntime,
} from "./argus-cursor-stub.js";

const baseConfig = () =>
  JSON.parse(
    readFileSync(`${gatewayFixturesDir}/container-config-before-augment.json`, "utf8"),
  ) as import("@onecli-sh/sdk").ContainerConfig;

describe("ensureHttp1CliConfig", () => {
  it("sets useHttp1ForAgent without dropping other fields", () => {
    const raw = JSON.stringify({ version: 1, editor: { vimMode: true }, network: {} });
    const doc = JSON.parse(ensureHttp1CliConfig(raw));
    expect(doc.network.useHttp1ForAgent).toBe(true);
    expect(doc.editor).toEqual({ vimMode: true });
  });
});

describe("augmentCursorContainerConfig", () => {
  it("is a no-op when Cursor is not assigned", () => {
    const config = baseConfig();
    expect(augmentCursorContainerConfig(config, false)).toBe(config);
    expect(config.env?.CURSOR_API_KEY).toBeUndefined();
  });

  it("adds auth.json and cli-config stubs without CURSOR_API_KEY", () => {
    const out = augmentCursorContainerConfig(baseConfig(), true);
    expect(out.env?.CURSOR_CONFIG_DIR).toBe(CURSOR_CONFIG_CONTAINER_DIR);
    expect(out.env?.CURSOR_API_KEY).toBeUndefined();
    const auth = out.credentialStubs!.find((s) => s.containerPath === CURSOR_AUTH_CONTAINER_PATH)!;
    const cli = out.credentialStubs!.find(
      (s) => s.containerPath === CURSOR_CLI_CONFIG_CONTAINER_PATH,
    )!;
    expect(JSON.parse(auth.content).accessToken).toBe(CURSOR_SENTINEL_ACCESS_TOKEN);
    expect(JSON.parse(cli.content).network.useHttp1ForAgent).toBe(true);
    expect(cursorAuthStubContent()).not.toContain("REALFAKE");
  });

  it("preserves unrelated existing stubs", () => {
    const config = {
      ...baseConfig(),
      credentialStubs: [{ containerPath: "/home/node/.codex/auth.json", content: "{}" }],
    };
    const out = augmentCursorContainerConfig(config, true);
    expect(out.credentialStubs!.some((s) => s.containerPath.endsWith("/.codex/auth.json"))).toBe(
      true,
    );
  });
});

describe("cursorExecutorAssigned", () => {
  const fetchImpl = fixtureOnecliFetch(gatewayFixturesDir);

  it("detects assigned Cursor generic secrets from fixture metadata only", async () => {
    expect(
      await cursorExecutorAssigned("http://onecli.test", undefined, "scratch-agent", fetchImpl),
    ).toBe(true);
    expect(
      await cursorExecutorAssigned("http://onecli.test", undefined, "default", fetchImpl),
    ).toBe(false);
  });

  it("classifies Cursor path/host metadata", () => {
    expect(
      isCursorGenericSecret({
        id: "x",
        type: "generic",
        hostPattern: "api2.cursor.sh",
        pathPattern: "/agent.v1.*",
      }),
    ).toBe(true);
    expect(
      isCursorGenericSecret({
        id: "x",
        type: "generic",
        hostPattern: "evil.cursor.sh",
        pathPattern: "/agent.v1.*",
      }),
    ).toBe(false);
  });
});

describe("withCursorDelegateRuntime", () => {
  it("leaves recorded container-config unchanged when unassigned", async () => {
    const before = baseConfig();
    const after = await withCursorDelegateRuntime(before, async () => false);
    expect(after).toBe(before);
    expect(after.credentialStubs ?? []).toHaveLength(0);
  });

  it("augments the recorded container-config when assigned", async () => {
    const before = baseConfig();
    const after = await withCursorDelegateRuntime(before, async () => true);
    expect(after.credentialStubs?.some((s) => s.containerPath === CURSOR_AUTH_CONTAINER_PATH)).toBe(
      true,
    );
  });
});
