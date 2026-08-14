# NutritionScanner

Turns a photograph of a grocery receipt into structured food and price data,
then reports what a week's groceries actually contain nutritionally — and what
each nutrient costs.

One photo a week is the only input. That constraint is the product: every food
tracker dies because logging every meal is unbearable.

> **Status: in development.** The architecture, data model, and design decisions
> below are settled and documented. Implementation is in progress — this README
> tracks what is actually built, not what is planned. See
> [docs/BRIEF.md](docs/BRIEF.md) for full scope and
> [docs/DECISIONS.md](docs/DECISIONS.md) for the reasoning behind each choice.

---

## The number this exists to produce

**Cost per gram of protein, ranked across everything I buy.** Not a generic
grocery basket — my actual prices, at my actual stores, for the food I actually
buy. Same for fiber, and per 100 kcal.

No other app shows this, because producing it requires resolving receipt
shorthand to real foods, converting purchase units to grams, correcting for
inedible weight, and tracking prices over time — which is the whole system.

---

## Design principles

These override convenience decisions, and are enforced in review:

1. **Supply, not intake.** Receipts show what was *bought*, not *eaten*. Copy
   implying intake is a bug.
2. **Never invent data.** An unresolvable item is marked unresolved and surfaced
   for correction. A visibly incomplete number is correct; a fabricated complete
   one is not.
3. **Corrections are permanent and compound.** Every fix is stored and applied to
   all future receipts. This is the core loop, not a settings screen.
4. **Measure the resolver.** LLM resolution is the one component that can be
   confidently wrong, so it gets a held-out labeled test set and a tracked
   accuracy number — shipped alongside the resolver, not after it.
5. **Adequacy, not restriction.** "Am I getting enough" and "what does it cost."
   No calorie targets, no deficits.
6. **Show uncertainty everywhere.** Every summary states how many line items
   resolved and how confidently, in the headline.

---

## How it works

```
photo → ingest → extract → normalize → reconcile → resolve → derive
```

| Stage | What it does |
|---|---|
| **ingest** | Hash the image, store it, create the receipt row. Happens *before* anything can fail — a receipt is never lost. |
| **extract** | Claude vision reads the receipt: store, date, every line, discounts, subtotal, tax, total. Raw JSON stored permanently. |
| **normalize** | Strip SKUs, tax flags, per-unit price fragments. Parse quantities, convert to grams. |
| **reconcile** | Pure arithmetic: lines + tax − discounts must equal the printed total. Runs *before* resolution, so a broken receipt is caught before spending a cent on it. |
| **resolve** | Three tiers: corrections table → batched LLM call → unresolved. Never guesses. |
| **derive** | Write price observations, rebuildable from line items so a later correction cannot leave stale price history. |

Each stage reads persisted input and writes persisted output, so any stage can be
replayed from stored data without re-photographing anything. Ingestion is
idempotent on image hash.

---

## Engineering decisions worth calling out

- **Corrections store a gram *rule*, not a gram number.** Food identity is stable
  across purchases; weight usually isn't. Replaying "1.2 lb of broccoli = 544 g"
  onto every future broccoli line silently corrupts every later basket.
- **Reconciliation before resolution.** Cheap deterministic checks precede
  expensive non-deterministic ones.
- **Confirmations are labels too.** An eval set built only from corrections is
  100% cases the model got wrong — a biased sample that can never show
  improvement. The review UI records confirmations alongside fixes.
- **Graded confidence, not a boolean.** Calibration error isn't computable over
  one bin.
- **Money is integer cents.** Float drift eventually fails an arithmetically
  perfect receipt, and that failure looks identical to a real extraction error.
- **Derived tables are rebuildable, never patched.**

Full reasoning for each: [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Stack

**Backend** — Python 3.12, FastAPI, SQLModel over PostgreSQL, Alembic (from the
first migration; never `create_all`), arq + Redis for jobs.
**Frontend** — React + TypeScript + Vite + Tailwind. Responsive: capture and
correction are phone-shaped, summaries get real desktop layouts. Live camera
capture on mobile.
**AI** — Anthropic API (`claude-opus-5`) with structured outputs for
schema-guaranteed extraction and resolution, prompt caching on the stable
prefix, and the Batch API for evaluation runs.
**Nutrition data** — USDA FoodData Central, aggressively cached, with a seeded
local cache so cold start works offline.

---

## Quality bar

- `mypy` clean, `ruff` clean, type hints throughout
- Tests for normalization, unit conversion, edible-portion math, resolution
  tiers, reconciliation against messy real receipts, and idempotent ingest
- Fixture receipts from 5+ store formats, including one badly crumpled and one
  with coupons
- Structured logging; every LLM call records model, tokens, latency, and cost
- Graceful degradation: if the API is down, the receipt is still stored and
  queued
- Full data export to JSON/CSV at any time
- Nightly Postgres backups with a tested restore path

---

## Privacy

Receipt images are personal financial records. They are never committed —
`.gitignore` blocks `data/`, `receipts/`, `uploads/`, `backups/`, database dumps,
and all `.env` files. Test fixtures are redacted and explicitly allowlisted.
Secrets come from the environment only.

---

## Local development

Setup instructions land here as the pieces are built. Target: `docker compose
up`, `alembic upgrade head`, and a working app from a cold clone.
