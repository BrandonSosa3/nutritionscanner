# Build Brief: Grocery Receipt → Nutrition & Cost Intelligence

> Source of truth for scope, principles, and architecture. If a later decision
> conflicts with this document, either the document changes (with a note in the
> Amendments section) or the decision is wrong. Amendments are welcome — this
> brief is not scripture, and better ideas found during the build should be
> proposed, argued, and folded in here.

## What we're building

A service that turns grocery receipts into two linked pictures: what food came
into my kitchen, and what it cost me. Over time it learns my prices across
stores and tells me where my money and my nutrition are actually going.

One photo a week is the only input I have to provide. That constraint is the
whole product — every food tracker dies because logging every meal is
unbearable.

This is a real service I intend to rely on for years. Build it that way:
migrations, tests, backups, measured accuracy, no silent failures. There is no
deadline. Correctness and durability beat speed everywhere they conflict.

---

## Non-negotiable design principles

Read these first. They override any convenience decision made later.

1. **Supply, not intake.** Receipts show what I *bought*, not what I *ate*.
   Never label a number "calories consumed" or "protein eaten." The correct
   framing is always "this week's groceries contained X." Any UI copy that
   implies intake is a bug.

2. **Never invent data.** If an item can't be resolved to a real food, it is
   marked unresolved and surfaced for correction. Do not guess a nutrition
   value to make a total look complete. A visibly incomplete number is
   correct; a fabricated complete one is not.

3. **Corrections are permanent and they compound.** Every fix I make is stored
   and applied to all future receipts. This is the core loop, not a settings
   screen.

4. **Measure the resolver; don't trust it.** The LLM resolution step is the
   one component that can be confidently wrong. It gets a held-out labeled
   test set and a tracked accuracy number, like any other model in production.
   See the Evaluation section — it is not optional and not a Phase 3 nicety.

5. **Adequacy, not restriction.** This tool is about "am I getting enough of
   things" and "what does it cost." Do not build calorie targets, deficits,
   weight-loss goals, or anything that nudges toward eating less.

6. **Show uncertainty everywhere.** Every summary states how many line items
   resolved and how confidently. A basket where 60% of lines matched must say
   so prominently, not in a footnote.

---

## Stack

- **Backend:** Python 3.12, FastAPI, SQLModel over PostgreSQL, Alembic
  migrations.
- **Frontend:** React + TypeScript + Vite, Tailwind. Mobile-first — receipts
  get photographed on a phone, and the correction screen is used standing in a
  kitchen.
- **AI:** Anthropic API (`claude-sonnet-4-6`) for receipt extraction and item
  resolution. Batch requests; never one call per line item.
- **Nutrition data:** USDA FoodData Central. Cache aggressively. Ship a seeded
  local cache of the ~500 most common grocery foods so cold start works
  without network.
- **Jobs:** A real queue (Celery or arq + Redis). Extraction is slow and
  network-dependent; it does not belong in a request handler.
- **Infra:** Docker Compose for local dev, deployed somewhere I control.
  Nightly Postgres backups to object storage, and a tested restore path — this
  database becomes years of irreplaceable personal history.
- **Secrets** from environment. Never committed. Receipt images are personal
  financial records; treat storage accordingly.

---

## Data model

```
Receipt        id, store_id, purchased_at, subtotal, tax, total,
               image_hash (unique), image_path, raw_extraction (jsonb),
               reconciliation_status, created_at
LineItem       id, receipt_id, raw_text, normalized_text, price,
               quantity, unit, grams_as_purchased, grams_edible,
               food_id (nullable), resolution_source, confidence,
               is_nonfood, discount_applied
Food           id, canonical_name, fdc_id, nutrients (jsonb per 100g),
               density_g_per_ml (nullable), edible_portion_pct,
               cooked_yield_factor (nullable)
Correction     id, normalized_text (unique), food_id, grams_override,
               store_id (nullable), created_at, applied_count
Store          id, name, location, receipt_format_hints (jsonb)
PriceObservation  id, food_id, store_id, price_per_100g, observed_at,
                  was_discounted
Budget         id, month, amount
EvalExample    id, raw_text, expected_food_id, expected_grams,
               store_id, added_at, split (train|holdout)
ResolverRun    id, run_at, model, accuracy, calibration_error,
               n_examples, notes
```

Two fields worth explaining:

**`edible_portion_pct`** — a banana receipt line is peel-inclusive weight.
Bone-in chicken, melon, avocado, shrimp all have large discrepancies between
purchased weight and edible weight. Ignoring this overstates nutrition by 20%+
on a produce-heavy basket. USDA publishes refuse percentages; use them.

**`cooked_yield_factor`** — 1kg of raw ground beef is not 1kg cooked. Only
matters for recipe costing in Phase 3, but the column belongs in the schema
now so the migration isn't painful later.

---

## Ingestion pipeline

```
photo → extract → normalize → resolve → reconcile → persist → derive
```

**Extract.** Send the receipt image to Claude with vision. One call returns
store, date, every line item with raw text and price, discounts, subtotal,
tax, total. Do not use Tesseract — a vision model reads crumpled thermal
receipts far better. Store the raw extraction JSON permanently so the pipeline
can be re-run without re-photographing. Every downstream stage must be
replayable from stored raw extractions.

**Normalize.** Strip SKUs, tax flags, and per-unit pricing fragments
(`@ 0.69/LB`). Extract quantities, convert to grams. Volume units need
food-specific density — fall back to unresolved rather than assuming water.

**Resolve.** Three tiers, in order:
1. Corrections table (exact match on normalized text) — instant, free.
2. Batched LLM call for unseen lines, returning
   `{food, grams, confident, is_nonfood}`. Reject low-confidence results.
3. Unresolved. Queued for my review.

**Reconcile.** Line items plus tax minus discounts must equal the receipt
total within tolerance. Handle the real messiness: coupons on their own lines,
loyalty pricing, bottle deposits, bag fees, weighted items priced per pound.
If it doesn't reconcile, flag the receipt as suspect and show me why. Never
silently persist a receipt that doesn't add up.

**Derive.** Write `PriceObservation` rows, marking discounted prices so sale
prices don't corrupt the baseline. Update rolling aggregates.

Ingestion is **idempotent** — hash the image, re-uploading updates rather than
duplicates.

---

## Evaluation harness

This is the part that makes the project serious, and it's the part most
projects skip.

The LLM resolution step will be confidently wrong sometimes. Without
measurement you won't know how often, which means you can't tell whether a
prompt change made things better or worse.

Build:

- **A labeled holdout set.** Every correction I make is a labeled example.
  Reserve a portion as holdout that never feeds the corrections table used at
  inference, so accuracy isn't measured against data the system already
  memorized.
- **An accuracy run** invocable by command, scoring exact food match and
  grams-within-tolerance, broken out by store and by category.
- **Calibration tracking.** When the model says `confident: true`, how often
  is it right? If that's 80% rather than 97%, the confidence threshold is
  meaningless and needs recalibrating.
- **Regression gating.** Any prompt or model change is scored against the
  holdout set before it ships. Store results in `ResolverRun` so the trend is
  visible over time.
- **Cost and latency per receipt**, tracked alongside accuracy. A prompt that
  is 2% more accurate and 5x the cost is a bad trade.

Target: >95% resolution accuracy on the holdout set after 30 receipts, with
calibration error under 5%.

---

## Features, gated by data — not by calendar

Each phase needs data the previous one produces. Don't start a phase before
its inputs exist; you'd be building against nothing.

### Phase 1 — the spine
- Photo upload → extraction → resolution → stored, reconciled receipt
- **Correction UI.** The single most important screen in the app. It's the one
  I touch every week, standing in a kitchen, and if it's tedious I'll abandon
  the whole thing. One tap to fix, keyboard-free where possible, instant
  persistence, and it should show me what it *thinks* so I can confirm rather
  than type.
- Basket summary: spend, macros, fiber, resolution rate
- **Cost per gram of protein** (and fiber, and per 100 kcal), ranked across
  everything I buy. This is the flagship view — the number no other app shows
  and the reason this project exists.
- Evaluation harness, from day one, growing as corrections accumulate

Phase 1 is a complete, useful product. Deploy it. Run 20+ real receipts
through it before starting Phase 2.

### Phase 2 — the money (needs ~2 months of receipts)
- Monthly budget with spend pacing
- Category breakdown: produce, protein, packaged, household
- Cross-store comparison for the specific items *I* buy, not a generic basket.
  Requires enough overlapping observations per item to be meaningful — say so
  when there aren't enough.
- Price drift over time, separating sale prices from baseline
- Total food spend including takeout and restaurant receipts, so the picture
  isn't skewed by the meals that never hit a grocery receipt

### Phase 3 — the intelligence (needs ~6 months)
- Nutrient adequacy against reference intakes, framed as gaps not targets
- Next-shop suggestions: cheapest way to close a gap given *my* actual prices
- Recipe cost and nutrition prediction: "make this N times a week — these
  ingredients, this cost, this contribution"
- Seasonality: when produce I buy is historically cheapest

Phase 3 features must refuse to fire on thin data. If there isn't enough
history to say something honestly, say that instead of hedging a guess.

---

## Explicitly out of scope

Meal logging. Barcode scanning. Calorie targets or deficits. Weight tracking.
Multi-user accounts and sharing. A native mobile app — mobile web is enough.

---

## Quality bar

- Type hints throughout; `mypy` clean; `ruff` clean.
- Tests for: normalization, unit conversion, edible-portion math, resolution
  tiers, reconciliation against messy real receipts, and idempotent ingest.
  Fixture receipts from at least five store formats including one badly
  crumpled and one with coupons.
- Structured logging. Every LLM call logs tokens, latency, and cost.
- Graceful degradation: if the Anthropic API is down, ingestion still stores
  the raw receipt and queues it. Never lose a receipt.
- Data export: I can dump everything to JSON/CSV at any time. My data, my
  ownership.
- README explaining architecture, how to run cold, and how to restore from
  backup.

---

## How to start

Do not write the whole thing at once.

1. Propose the repo structure and Phase 1 data model. Ask me about anything
   ambiguous before building.
2. Build extraction end to end against one real receipt image. Prove that
   works before anything else exists.
3. Then normalization and resolution, with the eval harness alongside — not
   after.
4. Then the correction UI. Give it real design attention.
5. Then the summary views.

Push back on anything in this brief you think is wrong. I'd rather argue now
than refactor later.

---

## Amendments

### A6 — Store identity is not yet resolved (open gap, not a decision)

Extraction records `store_name` in the raw transcription, but nothing creates a
`Store` row, so `Receipt.store_id` is always null. Two consequences, both
currently live:

- **Tier 1a of resolution never fires.** Store-specific corrections are
  written, stored, and applied correctly when a receipt has a store — but no
  receipt does. Every correction made through the API today is effectively
  global, because the store it is scoped to is null.
- **Cross-store price comparison (a D17 Phase 2 gate) has nothing to group by.**

This is a gap in the pipeline, not a design decision: store resolution belongs
between extract and normalize, matching `store_name` and location against
`Store` and `StoreAlias`. Recorded here so it is not mistaken for intent.


Additions and corrections agreed after the original brief. Newest last.

### A1 — Client is a responsive web app, phone and laptop (2026-08-14)
Mobile web, but genuinely usable on both form factors. Not a phone-only app
with a stretched desktop layout, and not a desktop app that technically loads
on a phone. Capture and correction are phone-shaped; summary and cost-per-
nutrient views get real desktop layouts.

### A2 — Live camera capture on mobile (2026-08-14)
On a phone the primary capture path is taking the photo in the app, not
picking a file. `<input capture="environment">` at minimum; a `getUserMedia`
preview with framing guides if it proves worth the code. File upload stays as
the desktop path and the fallback.

### A3 — The brief is amendable (2026-08-14)
Better ideas discovered during the build get proposed with reasoning,
decided by the owner, and recorded here. Silently deviating from the brief is
a bug; arguing with it is the process working.

### A4 — Schema, pipeline, and eval revisions (2026-08-14)
The data model and pipeline in this document were reviewed and revised before
implementation. Superseded by [DECISIONS.md](DECISIONS.md): correction keying
and gram rules (D2, D3), pipeline stage order and persistence boundaries
(D4, D5), eval labeling methodology (D6, D7), nutrient storage (D8), derived
price rebuild (D9), model choice (D10), and money representation (D1). Where
this brief and DECISIONS.md disagree on those points, DECISIONS.md wins; the
brief remains authoritative on scope, principles, and phasing.

### A5 — Open questions resolved (2026-08-14)
The four open questions raised at kickoff are answered in DECISIONS.md:
flagship ranking method (D11), partial-basket presentation (D12), duplicate
receipt handling (D13), and correction-screen interaction (D6).
