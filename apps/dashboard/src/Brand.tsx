import type { ElementType } from "react";
import logoUrl from "../../../assets/logo.svg";

/** Wordmark used on the login screen and in the shell header. */
export function Brand({ as: Tag = "span" }: { as?: ElementType }) {
  return (
    <Tag className="brand" aria-label="Argus">
      <img src={logoUrl} alt="" width={36} height={22} />
      <span className="brand-name">Argus</span>
    </Tag>
  );
}
