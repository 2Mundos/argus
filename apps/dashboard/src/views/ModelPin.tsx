import { useEffect, useState } from "react";
import { engineLabel, fetchModelPolicy, type ModelPolicy } from "../models";
import { PIN_CHOICES, PIN_FIELDS, parsePin } from "../taskEdit";

type PinField = (typeof PIN_FIELDS)[number];

// The policy maps whose chains suggest models for each pin.
const SCOPES = {
  harness_model: ["harness", "hard_harness"],
  executor_model: ["implementation", "trivial", "hard_implementation"],
} as const;

/**
 * A task's engine/executor and model pin ([task models](docs/specs/task-models.spec.md)) as
 * one "<name> <model>" value: "" follows the policy. The policy's models are suggested, any
 * model ID can be typed.
 */
export function ModelPin({
  field,
  id,
  value,
  disabled,
  onChange,
}: {
  field: PinField;
  id: string;
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  const [policy, setPolicy] = useState<ModelPolicy>();
  useEffect(() => {
    fetchModelPolicy().then(setPolicy, () => undefined); // suggestions only
  }, []);
  const pin = parsePin(value);
  const suggestions = pin
    ? [...new Set(SCOPES[field].flatMap((s) => policy?.[s]?.[pin.name] ?? []))]
    : [];
  return (
    <span className="row">
      <select
        id={id}
        aria-label={field === "harness_model" ? "Harness engine" : "Executor"}
        disabled={disabled}
        value={pin?.name ?? ""}
        onChange={(e) => onChange(e.target.value && `${e.target.value} ${pin?.model ?? ""}`)}
      >
        <option value="">model policy</option>
        {PIN_CHOICES[field].map((name) => (
          <option key={name} value={name}>
            {engineLabel(name)}
          </option>
        ))}
      </select>
      {pin && (
        <>
          <input
            aria-label={field === "harness_model" ? "Harness model" : "Executor model"}
            disabled={disabled}
            list={`${id}-models`}
            placeholder="model ID"
            value={pin.model}
            onChange={(e) => onChange(`${pin.name} ${e.target.value.trim()}`)}
          />
          <datalist id={`${id}-models`}>
            {suggestions.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </>
      )}
    </span>
  );
}
