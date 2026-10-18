import { describe, expect, it } from "vitest";

import { applyGitSslCaInfo } from "./argus-onecli-git-ca.js";

describe("applyGitSslCaInfo", () => {
  it("mirrors SSL_CERT_FILE when the combined bundle is mounted", () => {
    const env: Record<string, string> = { SSL_CERT_FILE: "/tmp/onecli-combined-ca.pem" };
    applyGitSslCaInfo(env, "/tmp/onecli-gateway-ca.pem");
    expect(env.GIT_SSL_CAINFO).toBe("/tmp/onecli-combined-ca.pem");
  });

  it("falls back to the gateway CA path when no combined bundle env is set", () => {
    const env: Record<string, string> = {};
    applyGitSslCaInfo(env, "/tmp/onecli-gateway-ca.pem");
    expect(env.GIT_SSL_CAINFO).toBe("/tmp/onecli-gateway-ca.pem");
  });
});
