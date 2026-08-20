/**
 * Changing what a line resolved to.
 *
 * The most consequential screen in the product. A correction is permanent and
 * compounds: it is applied to every line matching this text, on receipts
 * already processed as well as future ones, and it becomes a labelled example
 * the resolver is scored against. Picking the wrong food here is worse than
 * leaving the line unresolved, which is why search is a plain substring match
 * rather than something fuzzy and why creating a new food takes a deliberate
 * second step.
 *
 * The weight rule is offered because it is the largest gap in practice — most
 * lines that fail to contribute to a summary fail for want of a weight, not an
 * identity. What is stored is a *rule* ("comes in 500 g packs"), never the
 * weight of this particular purchase, so replaying it onto a future receipt
 * that buys three of them gives 1500 g rather than 500 g.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { CorrectionBody, FoodSummary, LineItem } from "../api/types";
import { Button, Card, Chip } from "./ui";

type Choice =
  | { kind: "food"; food: FoodSummary }
  | { kind: "new"; name: string }
  | { kind: "nonfood" };

export function CorrectionSheet({
  line,
  storeName,
  onCancel,
  onSaved,
}: {
  line: LineItem;
  storeName: string | null;
  onCancel: () => void;
  onSaved: (appliedTo: number) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FoodSummary[]>([]);
  const [searching, setSearching] = useState(false);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [gramsValue, setGramsValue] = useState("");
  const [gramsBasis, setGramsBasis] =
    useState<NonNullable<CorrectionBody["grams_basis"]>>("per_package");
  const [everywhere, setEverywhere] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Debounced so typing doesn't fire a request per keystroke.
  useEffect(() => {
    const term = query.trim();
    if (term.length < 2) {
      setResults([]);
      return;
    }
    setSearching(true);
    const timer = setTimeout(() => {
      api
        .searchFoods(term)
        .then((list) => setResults(list.items))
        .catch(() => setResults([]))
        .finally(() => setSearching(false));
    }, 200);
    return () => clearTimeout(timer);
  }, [query]);

  async function save() {
    if (!choice) return;
    setSaving(true);
    setError(null);
    try {
      let body: CorrectionBody;
      if (choice.kind === "nonfood") {
        body = { is_nonfood: true };
      } else {
        const food =
          choice.kind === "food" ? choice.food : await api.createFood(choice.name);
        body = { food_id: food.id };
      }

      const grams = gramsValue.trim();
      if (grams && choice.kind !== "nonfood") {
        body.grams_basis = gramsBasis;
        body.grams_value = grams;
      }
      body.global_scope = everywhere;

      const result = await api.correct(line.id, body);
      onSaved(result.applied_to_line_items);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Couldn't save that correction.",
      );
      setSaving(false);
    }
  }

  const exactMatch = results.some(
    (food) => food.canonical_name === query.trim().toLowerCase(),
  );
  const canCreate = query.trim().length >= 2 && !exactMatch && !searching;

  return (
    <Card>
      <div className="flex flex-col gap-1">
        <p className="font-mono text-caption text-ink-3">{line.raw_text}</p>

        {/* Where you are starting from. Without this the sheet gives no way to
            tell whether you are changing something or confirming it, which is
            most confusing on exactly the lines that are already right. */}
        {line.food_name ? (
          <p className="text-body">
            Currently{" "}
            <span className="font-medium">{line.food_name}</span>
            {line.confidence !== null && (
              <span className="text-ink-2">
                {" "}
                · {Math.round(line.confidence * 100)}% confident
              </span>
            )}
            {line.grams_as_purchased && (
              <span className="text-ink-2">
                {" "}
                · {Number(line.grams_as_purchased).toFixed(0)} g
              </span>
            )}
          </p>
        ) : (
          <p className="text-body text-ink-2">Not identified yet.</p>
        )}

        <p className="text-label text-ink-2">
          {line.food_name
            ? "Change the food, add a weight, or both. Whatever you save is applied to every receipt with this line, past ones included."
            : "What is this? Your answer is applied to every receipt with this line, past ones included."}
        </p>
      </div>

      {line.food_id !== null && line.food_name && (
        <button
          type="button"
          onClick={() =>
            setChoice({
              kind: "food",
              food: {
                id: line.food_id as number,
                canonical_name: line.food_name as string,
                category: "uncategorized",
                fdc_id: null,
                nutrient_count: 0,
                has_nutrition: false,
              },
            })
          }
          className={`min-h-[44px] rounded-[6px] border border-line px-3 py-2 text-left ${
            choice?.kind === "food" && choice.food.id === line.food_id ? "bg-sunken" : ""
          }`}
        >
          <span className="text-body">Keep {line.food_name}</span>
          <span className="block text-caption text-ink-2">
            The food is right — I only want to set a weight.
          </span>
        </button>
      )}

      <label className="flex flex-col gap-2 pt-2">
        <span className="text-label font-medium">
          {line.food_name ? "Or pick a different food" : "Find a food"}
        </span>
        <input
          type="search"
          value={query}
          autoFocus
          onChange={(event) => {
            setQuery(event.target.value);
            setChoice(null);
          }}
          placeholder="onions, chicken breast, cheddar"
          className="h-9 rounded-[6px] border border-line bg-surface px-3 text-body placeholder:text-ink-3"
        />
      </label>

      {(results.length > 0 || canCreate) && (
        <div className="flex max-h-64 flex-col overflow-y-auto rounded-[6px] border border-line">
          {results.map((food) => {
            const selected = choice?.kind === "food" && choice.food.id === food.id;
            return (
              <button
                key={food.id}
                type="button"
                onClick={() => setChoice({ kind: "food", food })}
                className={`flex min-h-[44px] items-center justify-between gap-3 border-b border-line px-3 py-2 text-left last:border-b-0 ${
                  selected ? "bg-sunken" : ""
                }`}
              >
                <span className="min-w-0 flex-1 truncate text-body">
                  {food.canonical_name}
                </span>
                {!food.has_nutrition && <Chip tone="attention">no nutrition yet</Chip>}
                {selected && <Chip tone="resolved">chosen</Chip>}
              </button>
            );
          })}

          {canCreate && (
            <button
              type="button"
              onClick={() => setChoice({ kind: "new", name: query.trim() })}
              className={`min-h-[44px] border-b border-line px-3 py-2 text-left last:border-b-0 ${
                choice?.kind === "new" ? "bg-sunken" : ""
              }`}
            >
              <span className="text-body">
                Add “{query.trim()}” as a new food
              </span>
              <span className="block text-caption text-ink-2">
                Nutrition is looked up separately, and may not be found.
              </span>
            </button>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => setChoice({ kind: "nonfood" })}
        className={`min-h-[44px] rounded-[6px] border border-line px-3 py-2 text-left ${
          choice?.kind === "nonfood" ? "bg-sunken" : ""
        }`}
      >
        <span className="text-body">This isn't food</span>
        <span className="block text-caption text-ink-2">
          Bags, foil, cleaning supplies, bottle deposits.
        </span>
      </button>

      {choice && choice.kind !== "nonfood" && (
        <div className="flex flex-col gap-2 rounded-[6px] border border-line bg-sunken p-3">
          <span className="text-label font-medium">How much, per item? (optional)</span>
          <p className="text-caption text-ink-2">
            A rule, not this purchase — “comes in 500 g packs”. It's multiplied
            by the quantity on each receipt, so buying three gives 1 500 g.
          </p>
          <p className="text-caption text-ink-2">
            Only fill this in if you know it, from the package or a scale.
            Leaving it blank keeps the line honest; a guess here is replayed
            onto every future receipt with this item.
          </p>
          <div className="flex flex-wrap gap-2">
            <input
              inputMode="decimal"
              value={gramsValue}
              onChange={(event) => setGramsValue(event.target.value)}
              placeholder="500"
              className="h-9 w-24 rounded-[6px] border border-line bg-surface px-3 text-body placeholder:text-ink-3"
            />
            <span className="self-center text-label text-ink-2">grams per</span>
            <select
              value={gramsBasis}
              onChange={(event) =>
                setGramsBasis(event.target.value as NonNullable<CorrectionBody["grams_basis"]>)
              }
              className="h-9 rounded-[6px] border border-line bg-surface px-2 text-body"
            >
              <option value="per_package">package</option>
              <option value="per_unit_estimate">single item</option>
            </select>
          </div>
        </div>
      )}

      {choice && (
        <label className="flex items-start gap-3 pt-1">
          <input
            type="checkbox"
            checked={everywhere}
            onChange={(event) => setEverywhere(event.target.checked)}
            className="mt-1 h-4 w-4"
          />
          <span className="flex flex-col">
            <span className="text-body">Apply at every store</span>
            <span className="text-caption text-ink-2">
              {storeName
                ? `Off, this applies only at ${storeName}. Receipt shorthand often means different things at different chains.`
                : "This receipt has no store, so the correction applies everywhere either way."}
            </span>
          </span>
        </label>
      )}

      {error && <p className="text-body text-fail">{error}</p>}

      <div className="flex gap-2 pt-1">
        <Button full onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button variant="primary" full onClick={() => void save()} disabled={!choice || saving}>
          {saving ? "Saving" : "Save correction"}
        </Button>
      </div>
    </Card>
  );
}
