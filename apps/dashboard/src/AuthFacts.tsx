import type { Account } from "./api";
import { credentialLabel, rejectedSinceAccepted } from "./authHealth";
import { DateChip } from "./DateChip";

/** Credential state, last accepted and last rejected as three separate facts. */
export function AuthFacts({ account: a }: { account: Account }) {
  const credential = credentialLabel(a.credential);
  if (credential === null) return null;
  return (
    <ul className="auth-facts small">
      <li>
        Credential: <span className={`badge credential-${a.credential?.state}`}>{credential}</span>
        {a.credential?.expires_at && (
          <span className="muted">
            {" "}
            · expires <DateChip at={a.credential.expires_at} />
          </span>
        )}
      </li>
      <li>
        Last accepted:{" "}
        {a.last_accepted_at ? (
          <DateChip at={a.last_accepted_at} />
        ) : (
          <span className="muted">never</span>
        )}
      </li>
      <li>
        Last rejected:{" "}
        {a.last_rejected_at ? (
          <DateChip at={a.last_rejected_at} />
        ) : (
          <span className="muted">none</span>
        )}
        {rejectedSinceAccepted(a) && (
          <span className="badge account-exhausted"> newer than last accepted</span>
        )}
      </li>
    </ul>
  );
}
