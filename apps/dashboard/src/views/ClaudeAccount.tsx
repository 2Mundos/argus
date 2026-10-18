import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { type Account, api, type ClaudeLogin, call } from "../api";
import {
  type ClaudeCheck,
  checkClaudeCredential,
  checkToken,
  cleanCode,
  safeClaudeUrl,
  type TokenType,
  tokenCredential,
} from "../claudeAuth";

type Path = "import" | "token" | "login";

const POLL_MS = 2000;

/** Add a Claude account, or renew an existing one (`renew`): import Claude's
 * credentials JSON, give a token with its type, or sign in with a dedicated login.
 * All end as the same encrypted account; renewing keeps the account's settings. */
export function ClaudeAccount({
  renew,
  onClose,
  onDone,
}: {
  renew?: Account;
  onClose: () => void;
  onDone: () => void;
}) {
  const [path, setPath] = useState<Path>("import");
  const title = renew ? "Renew Claude account" : "Add Claude account";
  const tab = (id: Path, label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={path === id}
      className={path === id ? "primary" : ""}
      onClick={() => setPath(id)}
    >
      {label}
    </button>
  );
  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-label={title}>
        <h2>{title}</h2>
        <p className="muted small">
          {renew
            ? `Replace the credential of “${renew.name}”. Its name, priority, lease limit and enabled state stay as they are. `
            : "Claude runs on a subscription token or an API key. "}
          The credential is stored encrypted as an Argus account and is never shown again; workers
          receive it through the vault, not as a file.
        </p>
        <div className="row" role="tablist">
          {tab("import", "Import credential file")}
          {tab("token", "Use a token or API key")}
          {tab("login", "Sign in with Claude")}
        </div>
        {path === "import" && <ImportCredential renew={renew} onClose={onClose} onDone={onDone} />}
        {path === "token" && <TokenCredential renew={renew} onClose={onClose} onDone={onDone} />}
        {path === "login" && <DedicatedLogin renew={renew} onClose={onClose} onDone={onDone} />}
      </div>
    </div>
  );
}

/** What a reason code from the login means for the person, without jargon. */
function loginReason(code: string): string {
  if (code === "oauth_400")
    return "Claude refused that code: it expired, was already used, or came from an earlier link. Claude has made a new sign-in link: open it, sign in again and paste the new code.";
  if (code === "code_invalid") return "Claude did not accept that code. Check it and try again.";
  if (code === "token_not_found")
    return "Claude said it signed in, but Argus could not find the token it made. Nothing was stored; start again.";
  if (code === "login_rejected")
    return "Claude closed the sign-in after the code without saying why.";
  if (code === "login_attempts_exhausted") return "Too many codes were tried; start a new login.";
  if (code.startsWith("oauth_")) return `Claude refused the code (error ${code.slice(6)}).`;
  return code;
}

type Shared = { renew?: Account; onClose: () => void; onDone: () => void };

/** Create the account, or replace the credential of the one being renewed. */
async function save(renew: Account | undefined, name: string, check: ClaudeCheck, doc: unknown) {
  if (!check.ok) return;
  const credential = doc as Record<string, unknown>;
  if (renew) {
    await call(
      api.PUT("/api/accounts/{account_id}/credential", {
        params: { path: { account_id: renew.id } },
        body: { credential },
      }),
    );
    return;
  }
  await call(
    api.POST("/api/accounts", {
      body: { name, provider: "anthropic", kind: check.kind, credential },
    }),
  );
}

function NameField({
  renew,
  name,
  setName,
}: {
  renew?: Account;
  name: string;
  setName: (v: string) => void;
}) {
  if (renew) return null;
  return (
    <label>
      Name
      <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
    </label>
  );
}

function kindLabel(check: Extract<ClaudeCheck, { ok: true }>): string {
  if (check.mode === "api_key") return "API key";
  return check.mode === "oauth_token" ? "OAuth token" : "Claude login file";
}

function ImportCredential({ renew, onClose, onDone }: Shared) {
  const [name, setName] = useState("Claude (imported)");
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const check: ClaudeCheck | null = text.trim() ? checkClaudeCredential(text) : null;

  async function pick(file: File | undefined) {
    setError(undefined);
    if (!file) return;
    setFileName(file.name);
    setText(await file.text());
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    const result = checkClaudeCredential(text);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setBusy(true);
    try {
      await save(renew, name, result, JSON.parse(text));
      setText(""); // the document does not stay in the page
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <p className="muted small">
        Use a credential you are already authorised to use: a JSON with <code>claudeAiOauth</code>,{" "}
        <code>oauth_token</code> or <code>api_key</code>. Argus never reads one from this computer
        by itself.
      </p>
      <NameField renew={renew} name={name} setName={setName} />
      <label>
        Credential file
        <input
          type="file"
          accept=".json,application/json"
          aria-label="Credential file"
          onChange={(e) => pick(e.target.files?.[0])}
        />
      </label>
      <label>
        …or paste its content
        <textarea
          rows={4}
          aria-label="Credential content"
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          autoComplete="off"
          placeholder="{ … }"
        />
      </label>
      {check?.ok && (
        <p className="small" role="status">
          {fileName ? `${fileName}: ` : ""}
          {kindLabel(check)} detected, {check.kind.replace("_", " ")} account (values hidden).
        </p>
      )}
      {check && !check.ok && <p className="error">{check.error}</p>}
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="primary" disabled={busy || !check?.ok}>
          {renew ? "Renew" : "Import"}
        </button>
      </div>
    </form>
  );
}

function TokenCredential({ renew, onClose, onDone }: Shared) {
  const [name, setName] = useState("Claude (token)");
  const [type, setType] = useState<TokenType>("oauth_token");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const check = token.trim() ? checkToken(type, token) : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    const result = checkToken(type, token);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setBusy(true);
    try {
      await save(renew, name, result, tokenCredential(type, token));
      setToken("");
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <p className="muted small">
        A subscription token from <code>claude setup-token</code> (it starts with{" "}
        <code>sk-ant-oat</code>), or an Anthropic API key. Say which one it is: the account kind
        follows the type. The code the sign-in page shows goes in the <b>Sign in with Claude</b>{" "}
        step, not here.
      </p>
      <NameField renew={renew} name={name} setName={setName} />
      <label>
        Type
        <select
          aria-label="Token type"
          value={type}
          onChange={(e) => setType(e.target.value as TokenType)}
        >
          <option value="oauth_token">Subscription OAuth token (sk-ant-oat…)</option>
          <option value="api_key">API key (sk-ant-api…)</option>
        </select>
      </label>
      <label>
        Token
        <input
          type="password"
          aria-label="Token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      {check && !check.ok && <p className="error">{check.error}</p>}
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="primary" disabled={busy || !check?.ok}>
          {renew ? "Renew" : "Add"}
        </button>
      </div>
    </form>
  );
}

function DedicatedLogin({ renew, onClose, onDone }: Shared) {
  const [name, setName] = useState("Claude (subscription login)");
  const [login, setLogin] = useState<ClaudeLogin>();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const live = useRef<ClaudeLogin>(undefined);
  const done = useRef(onDone);
  done.current = onDone;

  const track = useCallback((next: ClaudeLogin | undefined) => {
    live.current = next;
    setLogin(next);
  }, []);

  const id = login?.state === "pending" ? login.login_id : undefined;

  const poll = useCallback(
    async (loginId: string) => {
      try {
        const next = await call(
          api.GET("/api/accounts/claude-login/{login_id}", {
            params: { path: { login_id: loginId } },
          }),
        );
        track(next);
        if (next.state === "completed") done.current();
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [track],
  );

  useEffect(() => {
    if (!id) return;
    const timer = setInterval(() => void poll(id), POLL_MS);
    return () => clearInterval(timer);
  }, [id, poll]);

  // Closing the wizard (or switching tab) never leaves a client waiting.
  useEffect(
    () => () => {
      const l = live.current;
      if (l?.state !== "pending") return;
      live.current = undefined;
      void api.DELETE("/api/accounts/claude-login/{login_id}", {
        params: { path: { login_id: l.login_id } },
      });
    },
    [],
  );

  async function start(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    setBusy(true);
    try {
      track(
        await call(
          api.POST("/api/accounts/claude-login", {
            body: renew ? { renew_account_id: renew.id } : { name },
          }),
        ),
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function sendCode(e: FormEvent) {
    e.preventDefault();
    if (!login) return;
    setError(undefined);
    const clean = cleanCode(code);
    if (!clean) {
      setError(
        "That is not a valid code: paste everything the sign-in page shows, including the part after #.",
      );
      return;
    }
    setBusy(true);
    try {
      track(
        await call(
          api.POST("/api/accounts/claude-login/{login_id}/code", {
            params: { path: { login_id: login.login_id } },
            body: { code: clean },
          }),
        ),
      );
      setCode(""); // the code does not stay in the page
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!login) return onClose();
    if (login.state === "pending") {
      try {
        track(
          await call(
            api.DELETE("/api/accounts/claude-login/{login_id}", {
              params: { path: { login_id: login.login_id } },
            }),
          ),
        );
      } catch (err) {
        setError((err as Error).message);
        return;
      }
    }
    onClose();
  }

  if (!login) {
    return (
      <form onSubmit={start}>
        <p className="muted small">
          Argus runs the official Claude sign-in (<code>claude setup-token</code>) for this account
          only. You open a link, sign in, and paste the code Claude shows you back here. It makes a
          long-lived token for a Claude subscription; nothing is read from this computer.
        </p>
        <NameField renew={renew} name={name} setName={setName} />
        {error && <p className="error">{error}</p>}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy}>
            Start login
          </button>
        </div>
      </form>
    );
  }

  const link = safeClaudeUrl(login.url);
  const finishing = login.phase === "finishing";
  const renewing = login.phase === "renewing_link";
  return (
    <div>
      {login.state === "pending" && (
        <>
          <ol className="small">
            <li>
              Open{" "}
              {link ? (
                <a href={link} target="_blank" rel="noopener noreferrer">
                  the Claude sign-in page
                </a>
              ) : (
                "the sign-in page (the link was refused)"
              )}{" "}
              and sign in.
            </li>
            <li>
              Complete and copy the code it shows, then paste it below
              {finishing ? " (sent)" : ""}. The code expires quickly: paste everything the page
              shows, including the part after <code>#</code>.
            </li>
          </ol>
          {login.refusal && (
            <p className="error" role="alert">
              {loginReason(login.refusal)} You can try another code
              {login.attempts != null && login.max_attempts != null
                ? ` (${login.attempts} of ${login.max_attempts} tries used)`
                : ""}
              .
            </p>
          )}
          {!finishing && !renewing && (
            <form onSubmit={sendCode}>
              <label>
                Code from the sign-in page
                <input
                  type="password"
                  aria-label="Code from the sign-in page"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <div className="actions">
                <button type="submit" className="primary" disabled={busy || !code.trim()}>
                  Send code
                </button>
              </div>
            </form>
          )}
          <p className="muted small" role="status">
            {finishing && "Code sent, finishing sign-in…"}
            {renewing && "Preparing a new sign-in link…"}
            {!finishing && !renewing && "Waiting for the code…"} expires in{" "}
            {Math.ceil(login.expires_in_s / 60)} min.
          </p>
        </>
      )}
      {login.state === "completed" && (
        <p role="status">
          {renew ? "The credential was renewed." : "The account was created and is ready to lease."}
        </p>
      )}
      {login.state !== "pending" && login.state !== "completed" && (
        <>
          <p className="error" role="alert">
            Login {login.state}
            {login.error ? ` (${login.error})` : ""}. Nothing was stored.
          </p>
          {login.error && loginReason(login.error) !== login.error && (
            <p className="small">{loginReason(login.error)}</p>
          )}
          <div className="actions">
            <button type="button" onClick={() => track(undefined)}>
              Try again
            </button>
          </div>
        </>
      )}
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={cancel}>
          {login.state === "pending" ? "Cancel login" : "Close"}
        </button>
      </div>
    </div>
  );
}
