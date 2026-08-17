# Decision log

Settled design decisions, newest last. Each entry: the decision, why, and what
it rules out. Reopening one is fine — arguing with a recorded reason is the
process working. Silently contradicting one is a bug.

Status key: **Settled** (build on it) · **Provisional** (revisit with real data)

---

## D1 — Money is integer cents, everywhere internally · Settled

`price_cents: int`, never float, never `Decimal` for currency. Formatting to
dollars happens at the API boundary only.

**Why:** reconciliation compares a sum of line items against a printed total
within tolerance. Float drift eventually fails a receipt that is arithmetically
perfect, and that failure is indistinguishable from a real extraction error.

**Rules out:** `Decimal` money columns, float arithmetic anywhere in
`domain/money.py`.

---

## D2 — Corrections key on `(normalized_text, store_id)` · Settled

Composite unique constraint, plus a partial unique index for the global
fallback row (`store_id IS NULL`). Resolution checks store-specific first, then
global.

**Why:** the brief specified `normalized_text` unique *and* a nullable
`store_id`, which cannot both hold — a globally-unique key makes the store
column dead weight and forbids a per-store override. Receipt abbreviations
genuinely collide across chains.

**Rules out:** a single global correction per text string.

---

## D3 — Corrections store a gram *rule*, never a raw gram figure · Settled

`grams_basis` enum (`from_receipt` | `per_package` | `per_unit_estimate` |
`density` | `unknown`) plus `grams_value` whose meaning depends on the basis.

**Why:** food identity is stable across purchases; weight often is not.
Correcting "1.2 lb of broccoli → 544 g" and replaying that figure onto every
future broccoli line silently corrupts every subsequent basket. The rule
survives; the number does not.

**Consequence for the UI:** fixing *what a thing is* and fixing *how much of it
there was* are two different actions on the review screen.

---

## D4 — Reconcile runs immediately after normalize, before resolve · Settled

Pipeline order: `ingest → extract → normalize → reconcile → resolve → derive`.

**Why:** reconciliation is pure arithmetic on prices and needs no food
knowledge. Running it after resolution means an arithmetically broken receipt
cannot be flagged until an LLM call succeeds and costs money. Cheap
deterministic checks run before expensive non-deterministic ones.

---

## D5 — Persistence is the boundary between stages, not a stage · Settled

Every stage reads persisted input and writes persisted output, and is idempotent
on re-run. `ingest` stores the image and creates the `Receipt` row *before*
extraction is attempted.

**Why:** the brief's `… → persist → derive` ordering contradicted two of its own
requirements — "if the Anthropic API is down, ingestion still stores the raw
receipt" (persist before extract) and "every downstream stage must be replayable"
(persist after every stage). Replaying one stage with a new prompt should be a
CLI flag, not a refactor.

---

## D6 — Confirmations are labels, not just corrections · Settled

The review screen records a `confirmed` label for lines left untouched, alongside
`corrected` labels for lines that were fixed. `EvalExample.label_source`
distinguishes them.

**Why:** an eval set built only from corrections is 100% cases the resolver got
wrong — a biased sample of hard cases that can never show improvement and never
measures the far larger population of first-pass successes. Accuracy measured
against it answers the wrong question.

**Chosen interaction (answers brief Q2):** high-confidence lines arrive
pre-confirmed and are skimmed; the user taps only what is wrong; submitting
writes `confirmed` for everything untouched. One tap for a clean basket. The
labels are weaker than deliberate per-line confirmation, so
`EvalExample.label_source` carries the distinction and holdout metrics can be
computed on explicit labels alone if the implicit ones prove noisy.

---

## D7 — Resolver emits graded confidence, not a boolean · Settled

`confidence: float` in `0.0–1.0`. The metric is expected calibration error over
5 bins, plus precision at the auto-accept threshold.

**Why:** "calibration error under 5%" is not computable on a boolean — one bin
is not a calibration curve, it is a precision number. A graded score also lets
the auto-accept threshold be tuned without touching the prompt.

---

## D8 — Nutrients live in a narrow table; raw USDA payload kept as jsonb · Settled

`FoodNutrient(food_id, nutrient_code, amount_per_100g, unit)` for anything
queried; `Food.usda_payload` jsonb for provenance.

**Why:** the flagship view ranks every food by cost per gram of protein — a
sort and aggregate over one nutrient. That is a join against a narrow table and
jsonb surgery against a blob. Phase 3 nutrient adequacy iterates over arbitrary
nutrients and wants the same shape.

---

## D9 — `PriceObservation` is derived and rebuildable · Settled

Carries `line_item_id`, stores raw components alongside the computed ratio, and
is regenerated per receipt by `derive`. Never hand-patched.

**Why:** `price_per_100g = price ÷ grams` and both inputs change when a line is
corrected. Without a rebuild path, a gram fix leaves stale price history — which
is exactly the data the flagship ranking reads.

---

## D10 — Model is `claude-opus-5` for both extraction and resolution · Settled

Supersedes the brief's `claude-sonnet-4-6`.

**Why:** volume is one receipt a week, ~40 lines. High-resolution vision
extraction of a thermal receipt runs roughly 5K input tokens; at $5/M input the
entire annual API bill is a few dollars. There is no cost argument for a weaker
model on a once-weekly personal pipeline, and Opus 5's high-resolution vision
(2576px long edge, pixel-accurate coordinates) directly addresses faded and
crumpled thermal print.

**Also adopted:**
- **Structured outputs** (`output_config.format` with a JSON schema) for both
  calls — responses are schema-guaranteed, which deletes the "output only valid
  JSON" prompting and the parse-retry loop entirely.
- **Prompt caching** on the resolution system prompt and food context — a stable
  prefix read at ~0.1× cost.
- **Batch API** (50% cost) for evaluation runs, which are not latency-sensitive.

---

## D11 — Flagship ranking: per food, median of non-discounted observations · Settled

Answers brief Q1. Cost per gram of protein (and fiber, and per 100 kcal) is
computed per **food**, aggregated across purchases, using the **median** of
observations where `was_discounted = false`. Every row displays its observation
count and whether its grams came from a receipt-stated weight or an estimate.

**Why:** median resists both a single sale price and a single bad gram estimate,
where latest-price whipsaws and cheapest-observed systematically flatters. Sale
prices are excluded from the baseline by design (a sale is not what the food
costs). Surfacing `n` and gram provenance is principle 6 applied to the one
screen the project exists for — a ranking built on one estimated purchase must
not look like one built on twelve measured ones.

**Provisional:** revisit after ~20 receipts. If most foods have `n < 3`, median
is doing nothing and a different estimator may be warranted.

---

## D12 — Partial baskets show totals with a prominent coverage header · Settled

Answers brief Q3. The basket summary always renders macro totals, headed by
resolution coverage stated in the headline: *"covers 62% of spend, 58% of
lines."* Unresolved lines are listed, with their prices, directly beneath.

**Why:** principle 6 says show uncertainty, not hide output. Refusing to render
totals below a threshold makes the first weeks useless precisely when corrections
are most needed, and a suppressed number teaches nothing. A labelled partial
number is honest; the failure mode to avoid is an *unlabelled* one.

**Rules out:** a minimum-resolution gate on the summary view.

---

## D13 — Duplicate detection on extracted content, not just image hash · Settled

Answers brief Q4. `image_sha256` gives exact-file idempotency. Additionally,
after extraction, a receipt matching an existing one on `(store, purchased_at,
total_cents)` is flagged as a probable duplicate and surfaced for a one-tap
confirm-or-merge — never auto-merged, never silently accepted.

**Why:** two photos of one receipt are two hashes, and re-photographing a
crumpled receipt is a realistic thing to do. Auto-merging risks destroying a
genuine same-day second trip to the same store; flagging costs one tap and
cannot lose data.

---

## D14 — Runtime, queue, and tooling · Settled

Python **3.12** pinned in Docker (local 3.13 is irrelevant once containerized),
`uv` for dependency management, **arq + Redis** for jobs.

**Why:** arq is async-native and a fraction of Celery's machinery, and this
system has one job type. Pinning 3.12 in the image makes dev and prod identical
and stops the host Python version from mattering.

---

## D15 — Repository lives at `~/Desktop/NutritionScanner`, public on GitHub · Settled

**Revised 2026-08-17.** Originally moved to `~/Projects/` to escape a
`CLAUDE.md` one directory up that was being loaded into every session. The
actual fix was to remove that file, not to move this repo: the owner keeps all
projects as folders on the Desktop, and moving one breaks that workflow (and
silently empties the editor sidebar).

Root cause: `~/Desktop/CLAUDE.md` was a stale March snapshot of an unrelated
project's instructions, duplicating a newer copy that already lived inside that
project's own repo. Because Claude Code loads `CLAUDE.md` from the working
directory *and every parent directory*, it was being injected into every project
kept on the Desktop — including the project it described, where it contradicted
the current version by marking completed work as outstanding.

It has been renamed to `~/Desktop/regulist-claude-STALE-2026-03-29.md.bak`.
Nothing was deleted; only files named exactly `CLAUDE.md` are loaded, so the
rename is sufficient.

**General rule:** a project's `CLAUDE.md` belongs *inside* that project's
directory, never in a shared parent. A `CLAUDE.md` at `~/Desktop` applies to
every project on the Desktop.

**Public** because the project is intended as portfolio work. Nothing personal
is committable: `.gitignore` blocks `.env`, `data/`, `receipts/`, `uploads/`,
`backups/`, and database dumps. Test fixture receipts are redacted and
explicitly allowlisted. Visibility is one command to change if that calculus
shifts.

---

## D16 — Design system is fixed up front · Settled

[DESIGN.md](DESIGN.md) defines tokens, type scale, spacing, components, and
copy rules before any component is written.

**Why:** UI quality erodes by accumulation — a one-off hex value here, an
inconsistent spacing value there. Fixing the system first makes "does this match
the system?" a reviewable question instead of a matter of taste.

**Core stance:** the entire interface is neutrals. Saturated color is reserved
exclusively for resolution and reconciliation state. There is no colored primary
button anywhere in the product. No emoji, no gradients, no decorative shadows.

---

## D17 — Phase 2 and 3 machinery is built early; *output* is gated on data sufficiency · Settled

Supersedes the brief's "don't start a phase before its inputs exist." Phase 2
and 3 tables land in the **first migration**. Their computation logic is built
and unit-tested against synthetic fixtures as soon as the Phase 1 spine works —
not months later. What waits is not the code but the **display**, behind an
explicit sufficiency check.

**Why the brief's framing was too conservative:**

- Schema is nearly free to add now and expensive to migrate later. The brief
  already conceded this for `cooked_yield_factor`; the same argument covers
  budgets, categories, and adequacy references.
- The Phase 2/3 computations are **pure functions** — price drift, cross-store
  comparison, nutrient adequacy, gap-closing suggestions. Pure functions are
  testable against fabricated inputs today. Waiting for real data to write them
  confuses "can I test this?" with "is the answer meaningful yet?"
- Reference data needed by Phase 3 is **static and available now**: DRI values,
  the category taxonomy, USDA seed foods. None of it depends on receipt history.

**What genuinely does require real history**, and is therefore gated:

| Feature | Sufficiency gate |
|---|---|
| Cross-store comparison | ≥ 3 observations per item at each of ≥ 2 stores |
| Price drift | ≥ 4 observations spanning ≥ 45 days |
| Seasonality | ≥ 2 observations in the same month across ≥ 2 years |
| Nutrient adequacy | ≥ 4 weeks of receipts with ≥ 80% line resolution |
| Next-shop suggestions | adequacy gate, plus ≥ 3 candidate foods with price history |

**A gated feature is visible, not hidden.** It renders its own empty state
naming what it needs and how far along you are: *"Needs 3 more weeks of
receipts."* This satisfies the brief's own "refuse to fire on thin data" while
turning the wait into visible progress rather than an absent screen.

**What this does not license:** building five half-finished modules at once. The
Phase 1 spine still ships first, vertically and tested. This decision changes
*when Phase 2/3 work starts* from "months out" to "immediately after the spine,"
and puts their schema in migration one.

---

## D18 — Backfill is a first-class ingest path · Settled

Old receipts can be photographed and dated retroactively. `Receipt.purchased_at`
comes from the receipt, never from upload time, and the pipeline is indifferent
to how old a receipt is.

**Why:** it collapses the data-gathering timeline from months to an afternoon.
A shoebox of old receipts, or a bank statement cross-check, seeds enough price
history to clear several D17 sufficiency gates immediately. This is the single
highest-leverage way to make Phase 2 and 3 real early.

**Consequence:** the capture screen offers a date override for backfilled
receipts where extraction can't read a faded date, and the summary views group
by purchase date, never ingest date.
