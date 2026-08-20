/**
 * Receipt review — the screen used every week.
 *
 * Layout B by default: anything doubtful is pulled to the top as a short work
 * queue, and the lines the resolver got right collapse behind a count. Most
 * weeks that makes review one tap.
 *
 * Layout A — every line in printed order — is one toggle away, because
 * mirroring the paper is the better screen the first time you use this, or
 * when something looks wrong and you want to check line by line.
 *
 * Confirming is not a no-op. A confirmed line becomes a labelled eval example,
 * which is what stops the resolver's score being measured only on the cases it
 * already failed.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { LineItem, LineItemList, ReceiptDetail } from "../api/types";
import { CorrectionSheet } from "../components/CorrectionSheet";
import { Button, Card, Chip, ErrorNote, Money, Skeleton } from "../components/ui";

const NEEDS_REVIEW_BELOW = 0.8;

function needsReview(line: LineItem): boolean {
  if (line.kind !== "product" && line.kind !== "unknown") return false;
  if (line.resolution_source === "unresolved") return true;
  return line.confidence !== null && line.confidence < NEEDS_REVIEW_BELOW;
}

function isBasketLine(line: LineItem): boolean {
  return ["product", "unknown", "fee"].includes(line.kind);
}

export function Review({ receiptId }: { receiptId: number }) {
  const [receipt, setReceipt] = useState<ReceiptDetail | null>(null);
  const [lines, setLines] = useState<LineItemList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [correcting, setCorrecting] = useState<number | null>(null);
  const [applied, setApplied] = useState<string | null>(null);

  async function load() {
    try {
      const [detail, list] = await Promise.all([
        api.getReceipt(receiptId),
        api.lines(receiptId),
      ]);
      setReceipt(detail);
      setLines(list);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't load this receipt.");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [receiptId]);

  async function confirm(line: LineItem) {
    setBusy(line.id);
    try {
      await api.confirm(line.id);
      await load();
      setApplied("Confirmed. That's now a labelled example the resolver is scored against.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't save that.");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!receipt || !lines) return <Skeleton rows={6} />;

  const basket = lines.items.filter(isBasketLine);
  const queue = basket.filter(needsReview);

  return (
    <div className="flex flex-col gap-6">
      <Header receipt={receipt} lines={lines} />

      {applied && (
        <Card accent="resolved">
          <p className="text-body">{applied}</p>
        </Card>
      )}

      {queue.length === 0 ? (
        <Card accent="resolved">
          <p className="text-body">
            Nothing needs you. Every line was identified with confidence.
          </p>
        </Card>
      ) : (
        <section className="flex flex-col gap-3">
          <h2 className="text-heading font-semibold">
            {queue.length === 1 ? "1 item needs you" : `${queue.length} items need you`}
          </h2>
          <div className="flex flex-col gap-3">
            {queue.map((line) =>
              correcting === line.id ? (
                <CorrectionSheet
                  key={line.id}
                  line={line}
                  storeName={receipt.store_name}
                  onCancel={() => setCorrecting(null)}
                  onSaved={(appliedTo) => {
                    setCorrecting(null);
                    setApplied(
                      appliedTo === 1
                        ? "Saved. Applied to this line, and to every future receipt with it."
                        : `Saved. Applied to ${appliedTo} lines across your receipts, and to every future one.`,
                    );
                    void load();
                  }}
                />
              ) : (
                <QueueItem
                  key={line.id}
                  line={line}
                  busy={busy === line.id}
                  onConfirm={() => void confirm(line)}
                  onChange={() => {
                    setApplied(null);
                    setCorrecting(line.id);
                  }}
                />
              ),
            )}
          </div>
        </section>
      )}

      <section className="flex flex-col gap-3">
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          className="self-start text-label font-medium text-ink-2"
        >
          {showAll
            ? "Hide the full receipt"
            : `Show all ${basket.length} lines in printed order`}
        </button>

        {showAll && (
          <div className="overflow-hidden rounded-card border border-line bg-surface">
            {basket.map((line) => (
              <Row
                key={line.id}
                line={line}
                onChange={
                  line.kind === "product" || line.kind === "unknown"
                    ? () => {
                        setApplied(null);
                        setCorrecting(line.id);
                        setShowAll(false);
                      }
                    : undefined
                }
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function Header({ receipt, lines }: { receipt: ReceiptDetail; lines: LineItemList }) {
  const reconciliation = receipt.reconciliation_status;
  const delta = receipt.reconciliation_delta_cents;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h1 className="text-title font-semibold tracking-[-0.01em]">
          {receipt.purchased_at ?? "Undated receipt"}
        </h1>
        <p className="text-label text-ink-2">
          {receipt.total_cents !== null ? (
            <>
              <Money cents={receipt.total_cents} /> {receipt.currency}
            </>
          ) : receipt.subtotal_cents !== null ? (
            <>
              <Money cents={receipt.subtotal_cents} /> {receipt.currency} · total unreadable
            </>
          ) : (
            "No total on file"
          )}
          {" · "}
          {lines.resolved} of {lines.total} lines identified
        </p>
      </div>

      {reconciliation === "suspect" && delta !== null && (
        <Card accent="fail">
          <p className="text-label font-medium text-fail">Doesn't add up</p>
          <p className="text-body">
            The line items differ from the printed total by{" "}
            <span className="font-mono">{(delta / 100).toFixed(2)}</span>. The receipt
            is saved — nothing has been discarded.
          </p>
        </Card>
      )}
      {reconciliation === "unreconcilable" && (
        <Card accent="attention">
          <p className="text-body">
            Couldn't check the arithmetic — no total or subtotal was readable.
          </p>
        </Card>
      )}
    </div>
  );
}

function QueueItem({
  line,
  busy,
  onConfirm,
  onChange,
}: {
  line: LineItem;
  busy: boolean;
  onConfirm: () => void;
  onChange: () => void;
}) {
  const unresolved = line.resolution_source === "unresolved";

  return (
    <Card accent="attention">
      <p className="font-mono text-caption text-ink-3">{line.raw_text}</p>
      <p className="text-body">
        {unresolved ? (
          <span className="text-ink-2">Not identified yet</span>
        ) : (
          line.food_name
        )}
      </p>
      <p className="text-label text-ink-2">
        <Money cents={line.price_cents} />
        {line.confidence !== null && !unresolved && (
          <> · {Math.round(line.confidence * 100)}% confident — is this right?</>
        )}
      </p>
      <div className="flex gap-2 pt-1">
        <Button full onClick={onChange} disabled={busy}>
          Change
        </Button>
        <Button variant="primary" full onClick={onConfirm} disabled={busy || unresolved}>
          {busy ? "Saving" : "Yes, correct"}
        </Button>
      </div>
    </Card>
  );
}

function Row({ line, onChange }: { line: LineItem; onChange?: () => void }) {
  const attention = needsReview(line);
  const grams = line.grams_as_purchased;

  const body = (
    <div
      className={`grid grid-cols-[1fr_auto] gap-x-3 border-b border-line px-3 py-3 last:border-b-0 ${
        attention ? "border-l-2 border-l-attention bg-attention/[0.06]" : "border-l-2 border-l-transparent"
      }`}
    >
      <div className="flex min-w-0 flex-col gap-[2px]">
        <span className="truncate font-mono text-caption text-ink-3">{line.raw_text}</span>
        <span className={`text-body ${line.food_name ? "" : "text-ink-2"}`}>
          {line.food_name ?? (line.kind === "fee" ? "Not food" : "Not identified yet")}
        </span>
        <span className="flex flex-wrap items-center gap-2 text-caption text-ink-2">
          {grams && <span>{Number(grams).toFixed(0)} g</span>}
          <StateChip line={line} />
        </span>
      </div>
      <div className="text-right font-mono text-body">
        <Money cents={line.price_cents} />
      </div>
    </div>
  );

  // A confident wrong answer never reaches the queue, so every product line
  // stays correctable from the full list.
  return onChange ? (
    <button type="button" onClick={onChange} className="w-full text-left">
      {body}
    </button>
  ) : (
    body
  );
}

function StateChip({ line }: { line: LineItem }) {
  switch (line.resolution_source) {
    case "correction_store":
    case "correction_global":
      return <Chip>corrected by you</Chip>;
    case "nonfood":
      return <Chip>not food</Chip>;
    case "unresolved":
      return <Chip tone="attention">needs review</Chip>;
    default:
      return (
        <Chip tone={needsReview(line) ? "attention" : "resolved"}>
          {line.confidence !== null ? `${Math.round(line.confidence * 100)}%` : "identified"}
        </Chip>
      );
  }
}
