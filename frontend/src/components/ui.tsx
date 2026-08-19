/**
 * The small set of primitives every screen is built from.
 *
 * Deliberately few. The design system permits one primary button per screen,
 * three semantic colours used only for state, borders instead of shadows, and
 * a 4px spacing scale — a large component library would mostly be ways to
 * break those rules.
 */

import type { ReactNode } from "react";

type ButtonProps = {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "text";
  type?: "button" | "submit";
  disabled?: boolean;
  full?: boolean;
};

export function Button({
  children,
  onClick,
  variant = "secondary",
  type = "button",
  disabled,
  full,
}: ButtonProps) {
  // Primary is solid near-black, inverted in dark. There is no coloured
  // primary button anywhere in the product.
  const styles = {
    primary:
      "bg-ink text-bg border border-transparent disabled:opacity-40",
    secondary:
      "bg-transparent text-ink border border-line-strong disabled:opacity-40",
    text: "bg-transparent text-ink-2 border border-transparent px-0 disabled:opacity-40",
  }[variant];

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`min-h-[44px] rounded-[6px] px-4 py-[10px] text-body font-medium transition-colors duration-150 ${styles} ${full ? "w-full" : ""}`}
    >
      {children}
    </button>
  );
}

export function Card({
  children,
  accent,
}: {
  children: ReactNode;
  /** A 2px left border in a semantic colour. Never a filled panel. */
  accent?: "attention" | "fail" | "resolved";
}) {
  const border = accent
    ? { attention: "border-l-attention", fail: "border-l-fail", resolved: "border-l-resolved" }[
        accent
      ]
    : "";
  return (
    <div
      className={`flex flex-col gap-2 rounded-card border border-line bg-surface p-4 ${
        accent ? `border-l-2 ${border}` : ""
      }`}
    >
      {children}
    </div>
  );
}

export function Chip({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "resolved" | "attention" | "fail" | "neutral";
}) {
  const styles = {
    // 8-10% tints. Never fill a large area with a semantic colour.
    resolved: "text-resolved bg-resolved/10",
    attention: "text-attention bg-attention/10",
    fail: "text-fail bg-fail/10",
    neutral: "text-ink-2 bg-sunken",
  }[tone];
  return (
    <span
      className={`inline-flex items-center gap-[6px] rounded-full px-2 py-[2px] text-caption ${styles}`}
    >
      {tone !== "neutral" && (
        <span className="h-[6px] w-[6px] rounded-full bg-current" aria-hidden="true" />
      )}
      {children}
    </span>
  );
}

/** One line of body text and at most one action. No illustration. */
export function EmptyState({
  children,
  action,
}: {
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-start gap-4 rounded-card border border-dashed border-line bg-sunken p-6">
      <p className="text-body text-ink-2">{children}</p>
      {action}
    </div>
  );
}

/** Skeleton rows matching the real layout's dimensions. Never a spinner. */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-3" aria-hidden="true">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex flex-col gap-2 border-b border-line py-3">
          <div className="h-3 w-2/5 rounded bg-sunken" />
          <div className="h-3 w-3/5 rounded bg-sunken" />
        </div>
      ))}
    </div>
  );
}

/**
 * Errors say what happened and what to do about it. The backend's own
 * messages are written to be shown verbatim, so they are.
 */
export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <Card accent="fail">
      <p className="text-label font-medium text-fail">Something went wrong</p>
      <p className="text-body">{children}</p>
    </Card>
  );
}

export function Money({ cents }: { cents: number }) {
  return <span className="font-mono">{(cents / 100).toFixed(2)}</span>;
}
