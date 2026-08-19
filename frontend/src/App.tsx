/**
 * Three destinations and a detail view.
 *
 * Navigation is state rather than a router: there are three tabs and one
 * drill-down, and a routing library would be more configuration than the app
 * has structure. When deep links or a back button are wanted, this is the one
 * place that changes.
 */

import { useState } from "react";
import { AppShell, type Tab } from "./components/AppShell";
import { Nutrition } from "./screens/Nutrition";
import { Receipts } from "./screens/Receipts";
import { Review } from "./screens/Review";
import { Scan } from "./screens/Scan";
import { Status } from "./screens/Status";

export default function App() {
  const [tab, setTab] = useState<Tab>("scan");
  const [openReceipt, setOpenReceipt] = useState<number | null>(null);

  function go(next: Tab) {
    setOpenReceipt(null);
    setTab(next);
  }

  function openReview(id: number) {
    setOpenReceipt(id);
    setTab("receipts");
  }

  return (
    <AppShell tab={tab} onTab={go}>
      {openReceipt !== null ? (
        <div className="flex flex-col gap-4">
          <button
            type="button"
            onClick={() => setOpenReceipt(null)}
            className="self-start text-label font-medium text-ink-2"
          >
            All receipts
          </button>
          <Review receiptId={openReceipt} />
        </div>
      ) : tab === "scan" ? (
        <Scan onDone={openReview} />
      ) : tab === "receipts" ? (
        <Receipts onOpen={openReview} onScan={() => go("scan")} />
      ) : tab === "nutrition" ? (
        <Nutrition />
      ) : (
        <Status />
      )}
    </AppShell>
  );
}
