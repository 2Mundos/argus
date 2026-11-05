// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { originOf, validateIdentity } from "../gitIdentity";
import type { SettingValue } from "../settings";
import { GitIdentityEditor } from "./GitIdentityEditor";

const m = vi.hoisted(() => ({ putSetting: vi.fn(), resetSetting: vi.fn() }));
vi.mock("../settings", async (orig) => ({ ...(await orig<object>()), ...m }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ID = { name: "Ada", email: "ada@example.com" };
const setting = (source: SettingValue["source"], value: unknown = ID): SettingValue => ({
  key: "git.identity",
  source,
  value,
});

describe("validateIdentity", () => {
  it.each([
    ["", "a@b", /Name is required/],
    ["  ", "a@b", /Name is required/],
    ["Ada", "", /Email is required/],
    ["Ada", "no-at-sign", /Email must look like/],
    ["Ada", "a b@c", /Email must look like/],
    ["Ada", "a@", /Email must look like/],
  ])("rejects %j / %j", (name, email, error) => {
    const r = validateIdentity(name, email);
    expect(r.ok).toBe(false);
    expect(r.ok ? "" : r.error).toMatch(error);
  });

  it("trims what it accepts", () => {
    expect(validateIdentity(" Ada ", " ada@example.com ")).toEqual({ ok: true, identity: ID });
  });
});

describe("originOf", () => {
  it("tells override, inherited and unset apart", () => {
    expect(originOf("p", setting("project"))).toBe("override");
    expect(originOf("p", setting("global"))).toBe("inherited");
    expect(originOf("p", setting("default", {}))).toBe("unset");
    expect(originOf(null, setting("global"))).toBe("inherited");
  });
});

describe("GitIdentityEditor (global)", () => {
  it("shows the configured value and saves a trimmed edit", async () => {
    m.putSetting.mockResolvedValue(setting("global"));
    const onChanged = vi.fn();
    render(<GitIdentityEditor project={null} setting={setting("global")} onChanged={onChanged} />);
    const name = screen.getByLabelText("git author name (global)") as HTMLInputElement;
    expect(name.value).toBe("Ada");
    expect(screen.queryByRole("button", { name: /Reset/ })).toBeNull();
    fireEvent.change(name, { target: { value: " Grace " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(m.putSetting).toHaveBeenCalledWith(
      "git.identity",
      { name: "Grace", email: "ada@example.com" },
      null,
    );
  });

  it("does not call the API when the form is invalid", async () => {
    render(
      <GitIdentityEditor project={null} setting={setting("default", {})} onChanged={vi.fn()} />,
    );
    expect(screen.getByText(/cannot commit/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("git author name (global)"), {
      target: { value: "Ada" },
    });
    fireEvent.change(screen.getByLabelText("git author email (global)"), {
      target: { value: "nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Email must look like/);
    expect(m.putSetting).not.toHaveBeenCalled();
  });

  it("explains a permission refusal and surfaces other save errors", async () => {
    m.putSetting.mockRejectedValueOnce(new ApiError(403, "forbidden", "forbidden"));
    render(<GitIdentityEditor project={null} setting={setting("global")} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Only an admin/);

    m.putSetting.mockRejectedValueOnce(
      new ApiError(422, "validation_failed", "invalid value", ["email: bad"]),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("invalid value: email: bad"),
    );
  });
});

describe("GitIdentityEditor (project)", () => {
  it("marks an inherited identity and disables reset", () => {
    render(<GitIdentityEditor project="p" setting={setting("global")} onChanged={vi.fn()} />);
    expect(screen.getByText("inherits global")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Reset to inherited" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("saves an override for the project and resets it without touching global", async () => {
    m.putSetting.mockResolvedValue(setting("project"));
    m.resetSetting.mockResolvedValue(setting("global"));
    const onChanged = vi.fn();
    render(<GitIdentityEditor project="p" setting={setting("project")} onChanged={onChanged} />);
    expect(screen.getByText("override")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save override" }));
    await waitFor(() => expect(m.putSetting).toHaveBeenCalledWith("git.identity", ID, "p"));
    fireEvent.click(screen.getByRole("button", { name: "Reset to inherited" }));
    await waitFor(() => expect(m.resetSetting).toHaveBeenCalledWith("git.identity", "p"));
    expect(m.putSetting).toHaveBeenCalledTimes(1);
  });
});
