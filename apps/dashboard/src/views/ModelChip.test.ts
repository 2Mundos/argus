import { expect, it } from "vitest";
import { shortModelName } from "./ModelChip";

it("shortens versioned model ids and leaves others alone", () => {
  expect(shortModelName("claude-haiku-5-5")).toBe("Haiku 5.5");
  expect(shortModelName("gpt-sol-6-1")).toBe("Sol 6.1");
  expect(shortModelName("gpt-6")).toBe("gpt-6");
});
