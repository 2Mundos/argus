import { type FormEvent, useState } from "react";
import { api, call, setToken } from "../api";
import { Brand } from "../Brand";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    setToken(value.trim());
    try {
      await call(api.GET("/api/agents"));
      onLogin();
    } catch {
      setToken(null);
      setError("That token was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form onSubmit={submit} className="card">
        <Brand as="h1" />
        <p className="muted">
          Paste an admin token. The first one is in <code>/data/admin-token</code>.
        </p>
        <label>
          Token
          <input
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="argus_admin_…"
            required
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" className="primary" disabled={busy || !value.trim()}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
