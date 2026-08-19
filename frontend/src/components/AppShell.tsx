/**
 * Three destinations, and no more.
 *
 * Scan, Receipts, Nutrition. Everything about how the system itself is doing
 * — spend against the ceiling, resolver accuracy, foods still waiting on
 * nutrition data — lives behind one quiet Status link rather than earning a
 * tab it would not deserve.
 *
 * On mobile the tabs sit in a fixed bottom bar within thumb reach, because
 * capture and correction happen one-handed at a kitchen counter. On desktop
 * they move to the header, where a bottom bar would be strange.
 */

import type { ReactNode } from "react";

export type Tab = "scan" | "receipts" | "nutrition" | "status";

const TABS: { id: Tab; label: string }[] = [
  { id: "scan", label: "Scan" },
  { id: "receipts", label: "Receipts" },
  { id: "nutrition", label: "Nutrition" },
];

export function AppShell({
  tab,
  onTab,
  children,
}: {
  tab: Tab;
  onTab: (tab: Tab) => void;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex w-full max-w-[1200px] items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <button
            type="button"
            onClick={() => onTab("scan")}
            className="text-heading font-semibold"
          >
            NutritionScanner
          </button>

          <nav className="hidden items-center gap-1 sm:flex" aria-label="Sections">
            {TABS.map((item) => (
              <TabButton
                key={item.id}
                label={item.label}
                active={tab === item.id}
                onClick={() => onTab(item.id)}
              />
            ))}
          </nav>

          <button
            type="button"
            onClick={() => onTab("status")}
            className={`text-label ${tab === "status" ? "text-ink" : "text-ink-2"}`}
          >
            Status
          </button>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1200px] flex-1 px-4 pb-24 pt-6 sm:px-6 sm:pb-12">
        {children}
      </main>

      {/* Bottom bar, mobile only. Respects the home-indicator inset. */}
      <nav
        className="fixed inset-x-0 bottom-0 border-t border-line bg-surface pb-[env(safe-area-inset-bottom)] sm:hidden"
        aria-label="Sections"
      >
        <div className="flex">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onTab(item.id)}
              aria-current={tab === item.id ? "page" : undefined}
              className={`min-h-[44px] flex-1 py-3 text-label font-medium ${
                tab === item.id ? "text-ink" : "text-ink-2"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </nav>
    </div>
  );
}

function TabButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={`min-h-[36px] rounded-full px-4 py-2 text-label font-medium transition-colors duration-150 ${
        active ? "bg-ink text-bg" : "text-ink-2 hover:text-ink"
      }`}
    >
      {label}
    </button>
  );
}
