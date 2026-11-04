import { type ElementType, useEffect, useState } from "react";
import logoUrl from "../../../assets/logo.svg";
import { useWorking } from "./working";

/** Wordmark used on the login screen and in the shell header. */
export function Brand({ as: Tag = "span" }: { as?: ElementType }) {
  const working = useWorking();
  // Keeps rolling past the end of the work until the cycle ends, so the gradient
  // never snaps back mid-roll (docs/specs/dashboard-working.spec.md).
  const [rolling, setRolling] = useState(false);
  useEffect(() => {
    if (working) setRolling(true);
  }, [working]);
  return (
    <Tag className="brand" aria-label="Argus">
      <img src={logoUrl} alt="" width={31} height={22} />
      <span
        className={rolling ? "brand-name roll-text rolling" : "brand-name roll-text"}
        onAnimationIteration={() => {
          if (!working) setRolling(false);
        }}
      >
        Argus
      </span>
    </Tag>
  );
}
