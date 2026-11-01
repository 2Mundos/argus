// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Login } from "./Login";

vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: vi.fn(),
  setToken: vi.fn(),
}));

afterEach(cleanup);

describe("Login", () => {
  it("enables sign in when the DOM value changes without a change event", () => {
    render(<Login onLogin={() => {}} />);
    const input = screen.getByLabelText("Token") as HTMLInputElement;
    const button = screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement;

    expect(button.disabled).toBe(true);
    input.value = "argus_admin_secret";
    fireEvent.input(input);
    expect(button.disabled).toBe(false);
  });
});
