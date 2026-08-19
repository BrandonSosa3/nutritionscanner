/**
 * Receipt history.
 *
 * Ordered by purchase date, not upload time: a backfilled receipt belongs in
 * its real place in the timeline. Receipts with no date yet sort first, which
 * is also where they want to be — they are the ones needing attention.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ReceiptSummary } from "../api/types";
import { Chip, EmptyState, ErrorNote, Money, Skeleton } from "../components/ui";

export function Receipts({
  onOpen,
  onScan,
}: {
  onOpen: (id: number) => void;
  onScan: () => void;
}) {
  const [receipts, setReceipts] = useState<ReceiptSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listReceipts()
      .then((result) => setReceipts(result.items))
      .catch((caught) =>
        setError(caught instanceof ApiError ? caught.message : "Couldn't load receipts."),
      );
  }, []);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!receipts) return <Skeleton rows={4} />;

  if (receipts.length === 0) {
    return (
      <EmptyState
        action={
          <button type="button" onClick={onScan} className="text-label font-medium text-ink">
            Add your first receipt
          </button>
        }
      >
        Receipts you photograph will appear here, newest shop first.
      </EmptyState>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-title font-semibold tracking-[-0.01em]">Receipts</h1>
      <div className="overflow-hidden rounded-card border border-line bg-surface">
        {receipts.map((receipt) => (
          <button
            key={receipt.id}
            type="button"
            onClick={() => onOpen(receipt.id)}
            className="flex w-full min-h-[44px] items-center justify-between gap-3 border-b border-line px-4 py-3 text-left last:border-b-0"
          >
            <span className="flex min-w-0 flex-col gap-[2px]">
              <span className="text-body">{receipt.purchased_at ?? "Not dated yet"}</span>
              <span className="flex items-center gap-2 text-caption text-ink-2">
                <StatusChip receipt={receipt} />
              </span>
            </span>
            <span className="font-mono text-body">
              {receipt.total_cents !== null ? <Money cents={receipt.total_cents} /> : "—"}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function StatusChip({ receipt }: { receipt: ReceiptSummary }) {
  if (receipt.reconciliation_status === "suspect") return <Chip tone="fail">doesn't add up</Chip>;
  if (receipt.status === "extract_failed") return <Chip tone="fail">couldn't read it</Chip>;
  if (receipt.status === "needs_review") return <Chip tone="attention">needs review</Chip>;
  if (receipt.status === "complete") return <Chip tone="resolved">done</Chip>;
  return <Chip>in progress</Chip>;
}
