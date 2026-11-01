// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionButton, useAction } from "./ActionButton";

afterEach(cleanup);

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("ActionButton", () => {
  it("disables and sets aria-busy while the action runs", async () => {
    const gate = deferred();
    const action = vi.fn(() => gate.promise);
    render(<ActionButton action={action}>Save</ActionButton>);
    const btn = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    fireEvent.click(btn);
    expect(action).toHaveBeenCalledOnce();
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("aria-busy")).toBe("true");
    expect(btn.className).toContain("is-pending");
    expect(btn.querySelector(".spinner")).not.toBeNull();
    gate.resolve();
    await waitFor(() => expect(btn.disabled).toBe(false));
    expect(btn.getAttribute("aria-busy")).toBeNull();
  });

  it("ignores a second click while pending", async () => {
    const gate = deferred();
    const action = vi.fn(() => gate.promise);
    render(<ActionButton action={action}>Go</ActionButton>);
    const btn = screen.getByRole("button", { name: "Go" }) as HTMLButtonElement;
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(action).toHaveBeenCalledOnce();
    gate.resolve();
    await waitFor(() => expect(btn.disabled).toBe(false));
  });

  it("shows pendingLabel while pending", async () => {
    const gate = deferred();
    render(
      <ActionButton action={() => gate.promise} pendingLabel="Checking…">
        Sign in
      </ActionButton>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((screen.getByRole("button", { name: "Checking…" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    gate.resolve();
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
  });

  it("clears pending after unmount mid-flight without throwing", async () => {
    const gate = deferred();
    const { unmount } = render(<ActionButton action={() => gate.promise}>Run</ActionButton>);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    unmount();
    gate.resolve();
    await expect(gate.promise).resolves.toBeUndefined();
  });
});

describe("useAction", () => {
  function Toolbar({
    onCancel,
    onArchive,
  }: {
    onCancel: () => Promise<void>;
    onArchive: () => Promise<void>;
  }) {
    const action = useAction();
    return (
      <div>
        <ActionButton
          pending={action.is("cancel")}
          disabled={action.busy}
          action={() => action.run(onCancel, "cancel")}
        >
          Cancel
        </ActionButton>
        <ActionButton
          pending={action.is("archive")}
          disabled={action.busy}
          action={() => action.run(onArchive, "archive")}
        >
          Archive
        </ActionButton>
      </div>
    );
  }

  it("spins only the keyed button and disables siblings", async () => {
    const cancelGate = deferred();
    const onCancel = vi.fn(() => cancelGate.promise);
    const onArchive = vi.fn(() => Promise.resolve());
    render(<Toolbar onCancel={onCancel} onArchive={onArchive} />);
    const cancel = screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
    const archive = screen.getByRole("button", { name: "Archive" }) as HTMLButtonElement;
    fireEvent.click(cancel);
    expect(onCancel).toHaveBeenCalledOnce();
    expect(cancel.getAttribute("aria-busy")).toBe("true");
    expect(cancel.querySelector(".spinner")).not.toBeNull();
    expect(archive.disabled).toBe(true);
    expect(archive.getAttribute("aria-busy")).toBeNull();
    fireEvent.click(archive);
    expect(onArchive).not.toHaveBeenCalled();
    cancelGate.resolve();
    await waitFor(() => expect(cancel.disabled).toBe(false));
    expect(archive.disabled).toBe(false);
  });

  it("is single-flight across keys", async () => {
    const gate = deferred();
    const onCancel = vi.fn(() => gate.promise);
    const onArchive = vi.fn(() => Promise.resolve());
    render(<Toolbar onCancel={onCancel} onArchive={onArchive} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
    gate.resolve();
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
  });
});
