import { describe, expect, it } from "bun:test";

import {
  CODEX_TURN_TIMEOUT_DEFAULT_MS,
  CODEX_TURN_TIMEOUT_MAX_MS,
  CodexTurnTimeoutConfigError,
  resolveCodexTurnTimeoutMs,
} from "./codex-turn-timeout.js";

describe("resolveCodexTurnTimeoutMs", () => {
  it("defaults to ten minutes when unset", () => {
    expect(resolveCodexTurnTimeoutMs({})).toBe(CODEX_TURN_TIMEOUT_DEFAULT_MS);
  });

  it("accepts the Argus two-hour delivery ceiling", () => {
    expect(
      resolveCodexTurnTimeoutMs({ ARGUS_CODEX_TURN_TIMEOUT_MS: String(CODEX_TURN_TIMEOUT_MAX_MS) }),
    ).toBe(CODEX_TURN_TIMEOUT_MAX_MS);
  });

  it("rejects non-numeric and out-of-range values", () => {
    for (const ARGUS_CODEX_TURN_TIMEOUT_MS of ["10m", "0", "59999", "9999999999999"]) {
      expect(() => resolveCodexTurnTimeoutMs({ ARGUS_CODEX_TURN_TIMEOUT_MS })).toThrow(
        CodexTurnTimeoutConfigError,
      );
    }
  });
});
