import { useEffect, useState } from "react";
import { api, call } from "../api";

// Providers whose accounts can run a harness (task models spec 3.4).
const ENGINE_PROVIDERS = new Set(["anthropic", "openai", "custom", "local"]);

type Option = { id: string; name: string; provider: string; enabled: boolean };

/**
 * A task's harness account pin ([task models](docs/specs/task-models.spec.md) 3.4): the
 * one engine account its lease may use, by id; "" lets any account serve. Local (Strata)
 * and custom (e.g. OpenRouter) accounts bring their own model.
 */
export function AccountPin({
  id,
  value,
  disabled,
  onChange,
}: {
  id: string;
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  const [options, setOptions] = useState<Option[]>();
  useEffect(() => {
    call(api.GET("/api/accounts", {}))
      .then((accounts) =>
        setOptions(
          (Array.isArray(accounts) ? accounts : [])
            .filter((a) => ENGINE_PROVIDERS.has(a.provider))
            .map((a) => ({
              id: a.id,
              name: a.name,
              provider: a.provider,
              enabled: a.state !== "disabled",
            })),
        ),
      )
      .catch(() => setOptions([])); // a reader without account access still sees the stored id
  }, []);
  const known = options?.some((o) => o.id === value);
  return (
    <select
      id={id}
      aria-label="Harness account"
      disabled={disabled}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">any account</option>
      {value && !known && <option value={value}>{value}</option>}
      {options?.map((o) => (
        <option key={o.id} value={o.id}>
          {o.name} ({o.provider}
          {o.enabled ? "" : ", disabled"})
        </option>
      ))}
    </select>
  );
}
