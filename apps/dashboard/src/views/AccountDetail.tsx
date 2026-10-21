import { useState } from "react";
import { type Account, api, call } from "../api";
import { relative } from "../format";
import { useLiveData } from "../live";
import {
  hasModelUsage,
  tokens,
  totalTokens,
  unmeasuredNote,
  viewerTimeZone,
  WINDOWS,
  type WindowId,
  windowQuery,
} from "../usage";
import { QuotaMeter, QuotaStatusView, StackedBars } from "./charts";
import { ProviderMark, StateIcon } from "./providerIcons";

/** One account: quota per window and tokens over a chosen time window
 * (account usage spec 3.6). */
export function AccountDetail(props: { account: Account; onClose: () => void }) {
  const { account: a, onClose } = props;
  const [win, setWin] = useState<WindowId>("7d");
  const [refresh, setRefresh] = useState<{ busy: boolean; text?: string }>({ busy: false });
  const tz = viewerTimeZone();
  const model = hasModelUsage(a.provider);
  const { data, error } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/accounts/usage", {
          params: {
            query: { ...windowQuery(win, new Date(), a.created_at), tz, account_id: a.id },
          },
          signal,
        }),
      ),
    [win, a.id],
  );
  const usage = data?.accounts?.[0];

  async function refreshQuota() {
    setRefresh({ busy: true });
    try {
      const r = await call(
        api.POST("/api/accounts/{account_id}/quota/refresh", {
          params: { path: { account_id: a.id } },
        }),
      );
      const text = {
        updated: "Quota updated.",
        skipped: `Not read: ${r.reason}.`,
        rate_limited: "Read less than 5 minutes ago; try later.",
        failed: `The read failed (${r.reason}).`,
      }[r.status];
      setRefresh({ busy: false, text });
    } catch (err) {
      setRefresh({ busy: false, text: (err as Error).message });
    }
  }

  return (
    <div className="overlay" onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <aside
        className="drawer account-detail"
        aria-label={`Account ${a.name}`}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={() => {}}
      >
        <div className="toolbar">
          <h2>
            <ProviderMark provider={a.provider} /> {a.name}
          </h2>
          <span className={`badge account-${a.state}`}>
            <StateIcon state={a.state} /> {a.state}
          </span>
          <button type="button" className="link" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="muted small">
          {a.provider} · {a.kind.replace("_", " ")} · created {relative(a.created_at)}
          {a.state === "exhausted" && ` · resets ${relative(a.exhausted_until)}`}
        </p>

        {!model ? (
          <p className="muted">Repository account: no model tokens or quota.</p>
        ) : (
          <>
            <h3>Quota left</h3>
            <p className="muted small">
              The provider's figures, with where they came from and when.
            </p>
            {(a.quota ?? []).length === 0 ? (
              <QuotaStatusView status={a.quota_status} />
            ) : (
              <div className="quota-list">
                {(a.quota ?? []).map((q) => (
                  <QuotaMeter key={q.window} q={q} />
                ))}
              </div>
            )}
            {a.provider === "openai" && (
              <p className="small">
                <button type="button" onClick={refreshQuota} disabled={refresh.busy}>
                  {refresh.busy ? "Reading…" : "Refresh quota"}
                </button>{" "}
                {refresh.text && <span className="muted">{refresh.text}</span>}
              </p>
            )}

            <div className="toolbar">
              <h3>Tokens</h3>
              <div className="segmented" role="group" aria-label="Time window">
                {WINDOWS.map((w) => (
                  <button
                    key={w.id}
                    type="button"
                    className={w.id === win ? "active" : ""}
                    aria-pressed={w.id === win}
                    onClick={() => setWin(w.id)}
                  >
                    {w.label}
                  </button>
                ))}
              </div>
            </div>
            <p className="muted small">Counted by Argus from what workers report. Times in {tz}.</p>
            {error && <p className="error">{error.message}</p>}
            {usage && !usage.reported && (
              <p className="muted">Not reported: no worker has reported usage for this account.</p>
            )}
            {usage?.reported && data && (
              <>
                <dl className="stats">
                  <div className="stat">
                    <dt>Total</dt>
                    <dd className="n">{tokens(totalTokens(usage.totals))}</dd>
                  </div>
                  <div className="stat">
                    <dt>Input</dt>
                    <dd className="n">{tokens(usage.totals.input_tokens)}</dd>
                  </div>
                  <div className="stat">
                    <dt>Cached input</dt>
                    <dd className="n">{tokens(usage.totals.cached_input_tokens ?? 0)}</dd>
                  </div>
                  <div className="stat">
                    <dt>Output</dt>
                    <dd className="n">{tokens(usage.totals.output_tokens)}</dd>
                  </div>
                  <div className="stat">
                    <dt>Runs</dt>
                    <dd className="n">{usage.totals.runs ?? 0}</dd>
                  </div>
                  {a.kind === "api_key" && (
                    <div className="stat">
                      <dt>Reported cost</dt>
                      <dd className="n">${usage.totals.cost_usd.toFixed(2)}</dd>
                    </div>
                  )}
                </dl>
                {unmeasuredNote(usage.totals.unmeasured_runs) && (
                  <p className="muted small">
                    {unmeasuredNote(usage.totals.unmeasured_runs)}: the engine gave no figures, so
                    they are not counted in the tokens above.
                  </p>
                )}
                <StackedBars buckets={usage.buckets} bucket={data.bucket} tz={tz} />
              </>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
