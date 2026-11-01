import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call, setToken } from "../api";
import { Brand } from "../Brand";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    await action.run(async () => {
      setError(undefined);
      setToken(value.trim());
      try {
        await call(api.GET("/api/agents"));
        onLogin();
      } catch {
        setToken(null);
        setError("That token was not accepted.");
      }
    });
  }

  return (
    <main className="login">
      <form onSubmit={submit} className="card">
        <Brand as="h1" />
        <p className="muted">
          Paste an admin token. The first one is in <code>/data/admin-token</code>.
        </p>
        <input
          type="password"
          autoComplete="off"
          aria-label="Token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="argus_admin_…"
          required
        />
        {error && <p className="error">{error}</p>}
        <ActionButton
          type="submit"
          className="primary"
          disabled={!value.trim()}
          pending={action.busy}
          pendingLabel="Checking…"
        >
          Sign in
        </ActionButton>
      </form>
    </main>
  );
}
