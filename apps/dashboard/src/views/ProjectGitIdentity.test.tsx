// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Projects } from "./Projects";

const m = vi.hoisted(() => ({ fetchSettings: vi.fn() }));
vi.mock("../settings", async (orig) => ({ ...(await orig<object>()), ...m }));
vi.mock("./GitIdentityEditor", async () => {
  const { originOf } = await import("../gitIdentity");
  return {
    GitIdentityEditor: (p: { project: string; setting?: Parameters<typeof originOf>[1] }) => (
      <span>{originOf(p.project, p.setting)}</span>
    ),
  };
});
vi.mock("../api", async (orig) => {
  const project = (slug: string) => ({
    slug,
    name: `${slug} name`,
    description: "",
    repository_url: null,
  });
  return {
    ...(await orig<object>()),
    api: { GET: (path: string) => ({ path }) },
    call: async (r: { path: string }) =>
      r.path === "/api/projects" ? ["inherit", "over", "none", "global"].map(project) : [],
  };
});
vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: (s: AbortSignal) => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load(new AbortController().signal).then(setData);
      }, deps);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Projects git author", () => {
  it("shows inherit, override and unset per project, and nothing for global", async () => {
    const id = { name: "Ada", email: "ada@example.com" };
    m.fetchSettings.mockImplementation(async (slug: string) => ({
      scope: slug,
      settings: [
        slug === "over"
          ? { key: "git.identity", source: "project", value: id }
          : slug === "inherit"
            ? { key: "git.identity", source: "global", value: id }
            : { key: "git.identity", source: "default", value: {} },
      ],
    }));
    render(<Projects />);
    const card = async (name: string) =>
      (await screen.findByText(name)).closest("article") as HTMLElement;
    expect(await within(await card("inherit name")).findByText("inherited")).toBeTruthy();
    expect(await within(await card("over name")).findByText("override")).toBeTruthy();
    expect(await within(await card("none name")).findByText("unset")).toBeTruthy();
    expect(within(await card("global name")).queryByText("Git author")).toBeNull();
    expect(m.fetchSettings).not.toHaveBeenCalledWith("global", expect.anything());
  });
});
