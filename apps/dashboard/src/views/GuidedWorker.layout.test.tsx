// @vitest-environment jsdom
// DOM structure and behaviour only. Real layout (widths, gaps, overflow) is CSS and
// is checked in a browser, see the layout report's fixture and screenshots.
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { catalog, GATEWAY_NOTE } from "../catalogFixture";
import { initialState } from "../guided";
import { GuidedWorkerForm } from "./GuidedWorker";

afterEach(cleanup);

const form = (over: Parameters<typeof catalog>[0] = {}) =>
  render(<GuidedWorkerForm catalog={catalog(over)} onCreate={vi.fn(async () => {})} />);
const checked = () => screen.getAllByRole("radio").filter((r) => (r as HTMLInputElement).checked);
const create = () => screen.getByRole("button", { name: /Create/ }) as HTMLButtonElement;

describe("initial choice policy", () => {
  it("chooses nothing when no safe option is ready, and Create is disabled", () => {
    form({ worker: false, nanoclaw: false, socket: false });
    expect(checked()).toHaveLength(0);
    expect(screen.getByText("Choose a worker type to continue.")).toBeTruthy();
    expect(create().disabled).toBe(true);
    expect(screen.queryByText(/Setup details/)).toBeNull(); // nothing chosen, nothing to explain
  });

  it("does not default to a not-ready template (missing sandbox runtime)", () => {
    form({ worker: false, nanoclaw: false, socket: true });
    expect(checked()).toHaveLength(0);
  });

  it("never preselects the socket template, even when it is the only ready one", () => {
    form({ worker: false, nanoclaw: false, socket: true });
    expect(
      (screen.getByLabelText(/NanoClaw, host Docker socket/) as HTMLInputElement).checked,
    ).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(create().disabled).toBe(true);
  });

  it("preselects the first safe ready template", () => {
    form({ worker: true, nanoclaw: false, socket: true });
    expect(checked().map((r) => r.getAttribute("aria-labelledby"))).toHaveLength(1);
    expect((screen.getByLabelText(/Argus API worker/) as HTMLInputElement).checked).toBe(true);
    form({ worker: false, nanoclaw: true, socket: true });
    const sandbox = screen.getAllByLabelText(/NanoClaw, sandboxed/).at(-1) as HTMLInputElement;
    expect(sandbox.checked).toBe(true);
  });

  it("the policy is a pure function of the catalog", () => {
    expect(initialState(catalog({ worker: false, socket: true })).template).toBe("");
    expect(initialState(catalog({ worker: true, socket: true })).template).toBe("argus-worker");
  });
});

describe("radio semantics, labels and keyboard", () => {
  it("is one named radio group inside a labelled fieldset, one radio per template", () => {
    form({ worker: true, socket: true, nanoclaw: true });
    const group = screen.getByRole("group", { name: /What kind of worker/ });
    const radios = within(group).getAllByRole("radio") as HTMLInputElement[];
    expect(radios).toHaveLength(3);
    expect(new Set(radios.map((r) => r.name))).toEqual(new Set(["template"]));
  });

  it("each radio is named by the template name and described by its description", () => {
    form({ worker: true, socket: true, nanoclaw: true });
    const socket = screen.getByRole("radio", { name: "NanoClaw, host Docker socket" });
    const described = socket.getAttribute("aria-describedby") as string;
    expect(document.getElementById(described)?.textContent).toMatch(/Docker socket/);
    // The status badge is next to the name, inside the same row, not detached.
    const row = socket.closest("label") as HTMLElement;
    expect(within(row).getByText("ready")).toBeTruthy();
    expect(within(row).getByText("not sandboxed")).toBeTruthy();
  });

  it("clicking the name or description selects the choice", async () => {
    form({ worker: false, nanoclaw: false, socket: false });
    await userEvent.click(screen.getByText("Sandboxed NanoClaw host"));
    expect((screen.getByLabelText(/NanoClaw, sandboxed/) as HTMLInputElement).checked).toBe(true);
  });

  it("arrow keys move through the group and select (native radio behaviour)", async () => {
    form({ worker: true, socket: true, nanoclaw: true });
    const [first, second] = screen.getAllByRole("radio") as HTMLInputElement[];
    first?.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(second?.checked).toBe(true);
    expect(document.activeElement).toBe(second);
  });

  it("is operable by Tab to the group, Space to choose, then Create is still guarded", async () => {
    form({ worker: false, nanoclaw: false, socket: true });
    await userEvent.tab(); // into the radio group (nothing checked: the first radio)
    await userEvent.keyboard(" ");
    expect(checked()).toHaveLength(1);
  });
});

describe("disclosure and risk text", () => {
  it("keeps Advanced and Setup details collapsed initially", () => {
    form();
    const closed = Array.from(document.querySelectorAll("details")).map((d) => d.open);
    expect(closed.length).toBeGreaterThanOrEqual(2);
    expect(closed.every((open) => !open)).toBe(true);
  });

  it("puts the raw ARGUS_ names and the missing list only inside Setup details", async () => {
    form({ worker: false, nanoclaw: false, socket: false });
    await userEvent.click(screen.getByLabelText(/NanoClaw, sandboxed/));
    const setup = screen.getByText("Setup details").closest("details") as HTMLElement;
    expect(within(setup).getByText(/Install the sysbox-runc runtime/)).toBeTruthy();
    const reason = screen.getByRole("status");
    expect(reason.textContent).toMatch(/Not ready: .*sysbox-runc/);
    expect(reason.textContent).not.toMatch(/ARGUS_/); // concise, no env names
    expect(setup.contains(reason)).toBe(false);
  });

  it("shows the warning and acceptance before Create, outside any disclosure", async () => {
    form({ worker: false, nanoclaw: false, socket: true });
    await userEvent.click(screen.getByLabelText(/NanoClaw, host Docker socket/));
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/root-equivalent/);
    expect(alert.closest("details")).toBeNull();
    const box = within(alert).getByLabelText(/I understand and accept/) as HTMLInputElement;
    expect(box.checked).toBe(false);
    // The warning precedes the Create button in document order.
    expect(alert.compareDocumentPosition(create()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(create().disabled).toBe(true);
    await userEvent.click(box);
    expect(create().disabled).toBe(false);
  });

  it("keeps the task-execution limitation visible, not folded away", async () => {
    form({ worker: false, nanoclaw: false, socket: true });
    await userEvent.click(screen.getByLabelText(/NanoClaw, host Docker socket/));
    const note = screen.getByText(GATEWAY_NOTE);
    expect(note.closest("details")).toBeNull();
    expect(note.textContent).toMatch(/gateway/);
  });

  it("keeps independent statuses: socket ready, sandbox not ready", () => {
    form({ worker: false, nanoclaw: false, socket: true });
    const socket = screen.getByRole("radio", { name: "NanoClaw, host Docker socket" });
    const sandbox = screen.getByRole("radio", { name: "NanoClaw, sandboxed" });
    expect(within(socket.closest("label") as HTMLElement).getByText("ready")).toBeTruthy();
    expect(within(sandbox.closest("label") as HTMLElement).getByText("not ready")).toBeTruthy();
  });

  it("keeps a name the user typed when switching between choices", async () => {
    form({ worker: true, nanoclaw: true, socket: true });
    const name = screen.getByLabelText("Name") as HTMLInputElement;
    await userEvent.clear(name);
    await userEvent.type(name, "mine");
    await userEvent.click(screen.getByLabelText(/NanoClaw, sandboxed/));
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("mine");
  });
});
