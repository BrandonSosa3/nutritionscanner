/**
 * What the groceries contained, and what each nutrient cost.
 *
 * Supply, not intake. Every word on this screen describes what was *bought*.
 *
 * Coverage leads (layout D). A protein figure computed from a quarter of a
 * basket's weight is a lower bound, and the reader has to know that before
 * they read the number — not in a footnote, and not in a tooltip.
 *
 * The monthly grocery budget card belongs at the top of this screen when it
 * is built. It is a Phase 2 feature and needs a few weeks of receipts before
 * pacing means anything.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { BasketSummary, NutrientCost } from "../api/types";
import { Card, EmptyState, ErrorNote, Skeleton } from "../components/ui";

const SHOWN = [
  "protein_g",
  "energy_kcal",
  "fiber_g",
  "carbohydrate_g",
  "fat_g",
  "calcium_mg",
  "iron_mg",
  "sodium_mg",
];

const LABELS: Record<string, string> = {
  protein_g: "Protein",
  energy_kcal: "Energy",
  fiber_g: "Fibre",
  carbohydrate_g: "Carbohydrate",
  fat_g: "Fat",
  calcium_mg: "Calcium",
  iron_mg: "Iron",
  sodium_mg: "Sodium",
};

export function Nutrition() {
  const [summary, setSummary] = useState<BasketSummary | null>(null);
  const [ranking, setRanking] = useState<NutrientCost | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.summary(), api.costPerNutrient("protein_g")])
      .then(([basket, cost]) => {
        setSummary(basket);
        setRanking(cost);
      })
      .catch((caught) =>
        setError(caught instanceof ApiError ? caught.message : "Couldn't load the summary."),
      );
  }, []);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!summary || !ranking) return <Skeleton rows={5} />;

  if (summary.receipt_ids.length === 0) {
    return <EmptyState>Add a receipt and this will show what those groceries contained.</EmptyState>;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-title font-semibold tracking-[-0.01em]">
          What these groceries contained
        </h1>
        <p className="text-label text-ink-2">
          {summary.starts_on} to {summary.ends_on} · $
          {(summary.total_spend_cents / 100).toFixed(2)} spent
        </p>
      </div>

      {/* Coverage first. The caveat arrives with the number, never after it.
          Weight share is deliberately absent: unweighed lines are missing from
          both sides of it, so it reads 100% on a basket where four of nine
          lines were never weighed. Spend and line counts are complete. */}
      <Card accent={summary.coverage.is_partial ? "attention" : "resolved"}>
        <p className="text-body">{summary.headline}</p>
        {summary.coverage.is_partial && (
          <p className="text-caption text-ink-2">
            {summary.coverage.unresolved_lines} not yet identified ·{" "}
            {summary.coverage.lines_without_nutrition} without nutrition data ·{" "}
            {summary.coverage.lines_without_weight} without a weight
          </p>
        )}
      </Card>

      <section className="flex flex-col gap-3">
        <h2 className="text-heading font-semibold">Contained</h2>
        <div className="overflow-hidden rounded-card border border-line bg-surface">
          {SHOWN.map((code) => {
            const amount = summary.nutrients[code];
            return (
              <div
                key={code}
                className="flex min-h-[44px] items-center justify-between gap-3 border-b border-line px-4 py-2 last:border-b-0 sm:min-h-[36px]"
              >
                <span className="text-body">{LABELS[code] ?? code}</span>
                <span className="font-mono text-body">
                  {amount === undefined ? (
                    // Missing nutrition is an em-dash, never a zero.
                    <span className="text-ink-3">—</span>
                  ) : (
                    `${Number(amount).toFixed(1)} ${summary.units[code] ?? ""}`
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="text-heading font-semibold">Cheapest {ranking.label.toLowerCase()}</h2>
          <p className="text-label text-ink-2">
            cents per {ranking.unit} · non-discounted prices only
          </p>
        </div>

        {ranking.items.length === 0 ? (
          <EmptyState>
            Nothing can be ranked yet. A food needs both a price and a weight on
            a receipt before its cost per {ranking.unit} means anything.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-card border border-line">
            <table className="w-full border-collapse bg-surface">
              <thead>
                <tr>
                  <th className="border-b border-line px-3 py-2 text-left text-label font-medium text-ink-2">
                    Food
                  </th>
                  <th className="whitespace-nowrap border-b border-line px-3 py-2 text-right text-label font-medium text-ink-2">
                    Per {ranking.unit}
                  </th>
                  <th className="whitespace-nowrap border-b border-line px-3 py-2 text-left text-label font-medium text-ink-2">
                    Evidence
                  </th>
                </tr>
              </thead>
              <tbody>
                {ranking.items.map((row) => (
                  <tr key={row.food_id}>
                    <td className="border-b border-line px-3 py-2 text-body">
                      {row.canonical_name}
                    </td>
                    <td className="whitespace-nowrap border-b border-line px-3 py-2 text-right font-mono text-body">
                      {Number(row.cost_cents_per_unit).toFixed(2)}c
                    </td>
                    <td className="whitespace-nowrap border-b border-line px-3 py-2 text-caption text-ink-2">
                      n={row.observations} ·{" "}
                      {row.from_receipt_weights === row.observations
                        ? "weighed"
                        : "estimated weight"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
