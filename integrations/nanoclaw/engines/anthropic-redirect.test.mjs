import assert from "node:assert/strict";
import { test } from "node:test";
import {
  anthropicRedirectRejection,
  cloudHarnessCredentialPresent,
  LOCAL_HARNESS_AUTH_TOKEN,
  normalizeAnthropicBaseUrl,
} from "./anthropic-redirect.mjs";

const ALLOWED = "http://host.docker.internal:8080";

test("normalizeAnthropicBaseUrl strips a trailing slash", () => {
  assert.equal(normalizeAnthropicBaseUrl(`${ALLOWED}/`), ALLOWED);
});

test("allows the exact registered local URL with only the harness placeholder", () => {
  assert.equal(
    anthropicRedirectRejection(
      {
        ANTHROPIC_BASE_URL: ALLOWED,
        ARGUS_ALLOWED_ANTHROPIC_BASE_URL: ALLOWED,
        ANTHROPIC_AUTH_TOKEN: LOCAL_HARNESS_AUTH_TOKEN,
      },
      ALLOWED,
    ),
    null,
  );
});

test("rejects another base URL", () => {
  assert.equal(
    anthropicRedirectRejection(
      { ANTHROPIC_BASE_URL: "https://evil.example", ARGUS_ALLOWED_ANTHROPIC_BASE_URL: ALLOWED },
      ALLOWED,
    ),
    "api_redirected",
  );
});

test("rejects an allowlist URL whose host is not local", () => {
  assert.equal(
    anthropicRedirectRejection(
      {
        ANTHROPIC_BASE_URL: "http://evil.example:8080",
        ARGUS_ALLOWED_ANTHROPIC_BASE_URL: "http://evil.example:8080",
        ANTHROPIC_AUTH_TOKEN: LOCAL_HARNESS_AUTH_TOKEN,
      },
      "http://evil.example:8080",
    ),
    "api_redirected",
  );
});

test("rejects allowed URL together with a cloud credential", () => {
  assert.equal(
    anthropicRedirectRejection(
      {
        ANTHROPIC_BASE_URL: ALLOWED,
        ARGUS_ALLOWED_ANTHROPIC_BASE_URL: ALLOWED,
        CLAUDE_CODE_OAUTH_TOKEN: "onecli-managed-sentinel",
      },
      ALLOWED,
    ),
    "api_redirected",
  );
  assert.equal(cloudHarnessCredentialPresent({ ANTHROPIC_API_KEY: "sk-x" }), true);
});
