import { describe, expect, it } from "vitest";
import { codexAccountId, mirrorAccountId, withMirroredCodexAccount } from "./argus-codex-stub.js";

const claims = {
  sub: "x",
  "https://api.openai.com/auth": {
    chatgpt_account_id: "onecli-managed",
    chatgpt_plan_type: "free",
  },
};
const jwt = `h.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.s`;
const stub = JSON.stringify({
  auth_mode: "chatgpt",
  OPENAI_API_KEY: null,
  tokens: {
    id_token: jwt,
    access_token: "onecli-managed",
    refresh_token: "onecli-managed",
    account_id: "onecli-managed",
  },
});

describe("mirrorAccountId", () => {
  it("puts the real workspace id in the stub and its id_token claim, nothing else", () => {
    const doc = JSON.parse(mirrorAccountId(stub, "ws-123"));
    expect(doc.tokens.account_id).toBe("ws-123");
    expect(doc.tokens.access_token).toBe("onecli-managed");
    expect(doc.tokens.refresh_token).toBe("onecli-managed");
    const payload = JSON.parse(
      Buffer.from(doc.tokens.id_token.split(".")[1], "base64url").toString(),
    );
    expect(payload["https://api.openai.com/auth"]).toEqual({
      chatgpt_account_id: "ws-123",
      chatgpt_plan_type: "free",
    });
    expect(payload.sub).toBe("x");
  });

  it("leaves API-key stubs and garbage alone", () => {
    const key = JSON.stringify({ auth_mode: "apikey", OPENAI_API_KEY: "onecli-managed" });
    expect(mirrorAccountId(key, "ws")).toBe(key);
    expect(mirrorAccountId("not json", "ws")).toBe("not json");
  });
});

describe("withMirroredCodexAccount", () => {
  const config = {
    env: {},
    caCertificate: "",
    caCertificateContainerPath: "/ca",
    credentialStubs: [
      { containerPath: "/home/node/.codex/auth.json", content: stub },
      { containerPath: "/home/node/.other", content: "keep" },
    ],
  } as any;

  it("patches only the codex stub", async () => {
    const out = await withMirroredCodexAccount(config, async () => "ws-9");
    expect(JSON.parse(out.credentialStubs![0]!.content).tokens.account_id).toBe("ws-9");
    expect(out.credentialStubs![1]!.content).toBe("keep");
  });

  it("is a no-op without an id, and does not look one up for agents without a codex stub", async () => {
    expect(await withMirroredCodexAccount(config, async () => null)).toBe(config);
    let calls = 0;
    const plain = { ...config, credentialStubs: [{ containerPath: "/x", content: "y" }] };
    await withMirroredCodexAccount(plain, async () => (calls++, "ws"));
    expect(calls).toBe(0);
  });
});

describe("codexAccountId", () => {
  const f = (body: unknown, ok = true) =>
    (async () => ({ ok, json: async () => body })) as unknown as typeof fetch;
  it("reads the metadata of the group's Argus secret", async () => {
    const secrets = [
      { name: "argus-g1", metadata: {} },
      { name: "argus-g1-openai", metadata: { authMode: "oauth", accountId: "ws-1" } },
    ];
    expect(await codexAccountId("http://o", undefined, "g1", f(secrets))).toBe("ws-1");
    expect(await codexAccountId("http://o", undefined, "g2", f(secrets))).toBeNull();
    expect(await codexAccountId("http://o", undefined, "g1", f(secrets, false))).toBeNull();
  });
});
