import type { ReactNode } from "react";

/** Page title, subtitle, and actions on one row. */
export function PageHeader(props: { title: string; lead?: ReactNode; children?: ReactNode }) {
  const { title, lead, children } = props;
  return (
    <header className="page-header">
      <h2>{title}</h2>
      {lead != null &&
        (typeof lead === "string" ? (
          <p className="page-lead">{lead}</p>
        ) : (
          <div className="page-lead">{lead}</div>
        ))}
      {children != null && <div className="page-header-actions">{children}</div>}
    </header>
  );
}
