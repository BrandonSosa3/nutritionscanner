/**
 * Picking the USDA entry for a food the matcher would not claim.
 *
 * The matcher requires every distinguishing term of a food's name to appear in
 * the candidate, which is strict enough that real foods go unmatched — `eggs,
 * chicken, whole, raw` against USDA's `Egg, whole, raw, fresh`. That
 * strictness is deliberate: attaching the wrong food's numbers to a year of
 * baskets is far worse than a visible gap. This is the gap's exit.
 *
 * Rejected candidates are shown with their reason rather than hidden. A person
 * can see that `Egg, duck` was excluded for naming a different species and
 * decide for themselves — and seeing the reasons is what makes the matcher
 * look considered rather than arbitrary.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { FoodSummary, UsdaCandidate } from "../api/types";
import { Button, Card, Chip } from "./ui";

export function NutritionFix({
  food,
  onCancel,
  onFixed,
}: {
  food: FoodSummary;
  onCancel: () => void;
  onFixed: (description: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<UsdaCandidate[] | null>(null);
  const [queried, setQueried] = useState(food.canonical_name);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search(term?: string) {
    setLoading(true);
    setError(null);
    try {
      const result = await api.usdaCandidates(food.id, term);
      setCandidates(result.items);
      setQueried(result.queried);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Couldn't search FoodData Central.",
      );
      setCandidates([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void search();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [food.id]);

  async function choose(candidate: UsdaCandidate) {
    setSaving(candidate.fdc_id);
    setError(null);
    try {
      await api.setUsda(food.id, candidate.fdc_id);
      onFixed(candidate.description);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't attach that entry.");
      setSaving(null);
    }
  }

  const accepted = candidates?.filter((c) => !c.rejected_reason) ?? [];
  const rejected = candidates?.filter((c) => c.rejected_reason) ?? [];

  return (
    <Card>
      <div className="flex flex-col gap-1">
        <p className="text-heading font-semibold">{food.canonical_name}</p>
        <p className="text-label text-ink-2">
          Pick the USDA entry that matches. Its nutrition is used for every
          receipt line that resolves to this food.
        </p>
      </div>

      <form
        className="flex gap-2 pt-2"
        onSubmit={(event) => {
          event.preventDefault();
          void search(query.trim() || undefined);
        }}
      >
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={queried}
          className="h-9 min-w-0 flex-1 rounded-[6px] border border-line bg-surface px-3 text-body placeholder:text-ink-3"
        />
        <Button type="submit" disabled={loading}>
          Search
        </Button>
      </form>
      <p className="text-caption text-ink-2">
        Searching USDA for “{queried}”. Their wording often differs from ours —
        try <span className="font-mono">egg, whole, raw</span> rather than{" "}
        <span className="font-mono">eggs, chicken</span>.
      </p>

      {error && <p className="text-body text-fail">{error}</p>}
      {loading && <p className="text-body text-ink-2">Searching…</p>}

      {!loading && candidates?.length === 0 && (
        <p className="text-body text-ink-2">
          Nothing came back for that. Try different words — USDA describes
          ingredients, not products.
        </p>
      )}

      {accepted.length > 0 && (
        <CandidateGroup
          title="Good matches"
          candidates={accepted}
          saving={saving}
          onChoose={choose}
        />
      )}

      {rejected.length > 0 && (
        <CandidateGroup
          title="Excluded automatically — you can still pick one"
          candidates={rejected}
          saving={saving}
          onChoose={choose}
        />
      )}

      <Button full onClick={onCancel} disabled={saving !== null}>
        Cancel
      </Button>
    </Card>
  );
}

function CandidateGroup({
  title,
  candidates,
  saving,
  onChoose,
}: {
  title: string;
  candidates: UsdaCandidate[];
  saving: number | null;
  onChoose: (candidate: UsdaCandidate) => void;
}) {
  return (
    <div className="flex flex-col gap-2 pt-1">
      <p className="text-label font-medium text-ink-2">{title}</p>
      <div className="flex flex-col overflow-hidden rounded-[6px] border border-line">
        {candidates.map((candidate) => (
          <button
            key={candidate.fdc_id}
            type="button"
            disabled={saving !== null}
            onClick={() => onChoose(candidate)}
            className="flex min-h-[44px] flex-col gap-1 border-b border-line px-3 py-2 text-left last:border-b-0 disabled:opacity-50"
          >
            <span className="text-body">{candidate.description}</span>
            <span className="flex flex-wrap items-center gap-2 text-caption text-ink-2">
              <Chip>{candidate.data_type ?? "unknown source"}</Chip>
              {candidate.rejected_reason ? (
                <Chip tone="attention">{candidate.rejected_reason}</Chip>
              ) : (
                <Chip tone="resolved">matches</Chip>
              )}
              {saving === candidate.fdc_id && <span>attaching…</span>}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
