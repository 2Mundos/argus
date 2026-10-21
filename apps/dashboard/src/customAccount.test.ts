import { describe, expect, it } from "vitest";
import { buildConfig, checkBaseUrl, emptyForm } from "./customAccount";

describe("buildConfig", () => {
  it("drops empty fields and splits comma lists", () => {
    const f = emptyForm();
    f.executor = {
      model: " deepseek/deepseek-chat ",
      order: "deepinfra, ,together",
      only: "",
      quantizations: "fp8",
      fallbacks: "deny",
    };
    expect(buildConfig(f)).toEqual({
      base_url: "https://openrouter.ai/api/v1",
      label: "openrouter",
      engine: {},
      executor: {
        model: "deepseek/deepseek-chat",
        routing_options: {
          order: ["deepinfra", "together"],
          quantizations: ["fp8"],
          allow_fallbacks: false,
        },
      },
    });
  });

  it("sends no routing options for another label", () => {
    const f = emptyForm();
    f.label = "vllm";
    f.engine = { ...f.engine, model: "m", order: "a" };
    expect(buildConfig(f).engine).toEqual({ model: "m" });
  });
});

describe("checkBaseUrl", () => {
  it.each(["ftp://x/v1", "nope", "https://u:p@x/v1", "https://x/v1?k=1"])("rejects %s", (url) => {
    expect(checkBaseUrl(url)).not.toBeNull();
  });
  it("accepts a plain https URL", () => {
    expect(checkBaseUrl("https://openrouter.ai/api/v1")).toBeNull();
  });
});
