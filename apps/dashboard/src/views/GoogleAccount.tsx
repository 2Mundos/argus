import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call } from "../api";

/** Register a Gemini API key as a native `google` account, the provider the Antigravity
 * (`agy`) harness and executor lease (docs/specs/google-accounts.spec.md). The key is sent once
 * and never shown again. */
export function GoogleAccount({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("Google AI Studio");
  const [key, setKey] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/accounts", {
            body: {
              name,
              provider: "google",
              kind: "api_key",
              credential: { api_key: key.trim() },
            },
          }),
        );
        setKey(""); // the key does not stay in the page
        onDone();
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-label="Add Google AI Studio account">
        <h2>Add Google AI Studio account</h2>
        <p className="muted small">
          Stores a Gemini API key as a native Google account for the Antigravity harness and code
          executor. The key is stored encrypted and is never shown again. For an OpenAI-compatible
          endpoint choose Custom provider instead.
        </p>
        <form onSubmit={submit}>
          <label>
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={100}
            />
          </label>
          <label>
            Gemini API key
            <input
              type="password"
              autoComplete="off"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              required
            />
          </label>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <div className="row">
            <ActionButton
              type="submit"
              className="primary"
              pending={action.busy}
              pendingLabel="Saving…"
            >
              Add
            </ActionButton>
            <button type="button" disabled={action.busy} onClick={onClose}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
