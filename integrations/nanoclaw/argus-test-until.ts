/**
 * The one polling wait the channel tests use. They drive the channel black-box
 * through a fake Argus HTTP server, which has no event to await, so a test waits
 * for an observable condition (a call the fake recorded, a message delivered).
 * Never sleep a fixed time instead: wait for the thing that marks the step.
 */
export async function until(
  cond: () => boolean | Promise<boolean>,
  ms = 8000,
  what = "condition",
): Promise<void> {
  const start = Date.now();
  // check-tests: allow poll-loop — black-box HTTP fakes expose no event to await
  while (!(await cond())) {
    if (Date.now() - start > ms) throw new Error(`timed out waiting for ${what}`);
    // check-tests: allow sleep — the poll interval of the one sanctioned wait
    await new Promise((r) => setTimeout(r, 5));
  }
}
