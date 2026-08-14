# NutritionScanner

Turns photographed grocery receipts into structured food and price data, then
reports what a week's groceries contain nutritionally and what each nutrient
costs. One receipt photo a week is the only input. Single user. Built to be
relied on for years.

Full scope, architecture, and phasing: [docs/BRIEF.md](docs/BRIEF.md). The brief
is the source of truth. Amendments to it go in its Amendments section — never
deviate silently, but do argue.

Design decisions already settled (schema keys, pipeline order, eval methodology,
model choice): [docs/DECISIONS.md](docs/DECISIONS.md). Read it before proposing
a change to any of them — each one has a recorded reason.

---

## The six design principles

These override any convenience decision made later. If a principle and a
deadline disagree, the principle wins — there is no deadline.

1. **Supply, not intake.** Receipts show what was *bought*, not what was
   *eaten*. Never label a number "calories consumed" or "protein eaten." The
   framing is always "this week's groceries contained X." UI copy implying
   intake is a bug, not a wording nit.

2. **Never invent data.** An item that can't be resolved to a real food is
   marked unresolved and surfaced for correction. Never guess a nutrition
   value to make a total look complete. A visibly incomplete number is
   correct; a fabricated complete one is not. This applies to code output and
   to what Claude reports about its own work.

3. **Corrections are permanent and they compound.** Every user fix is stored
   and applied to all future receipts. This is the core product loop, not a
   settings screen.

4. **Measure the resolver; don't trust it.** LLM resolution is the one
   component that can be confidently wrong. It gets a held-out labeled test
   set and a tracked accuracy number. The eval harness ships in Phase 1
   alongside the resolver, not after it.

5. **Adequacy, not restriction.** The questions are "am I getting enough" and
   "what does it cost." No calorie targets, no deficits, no weight-loss goals,
   nothing that nudges toward eating less.

6. **Show uncertainty everywhere.** Every summary states how many line items
   resolved and how confidently. A basket where 60% of lines matched says so
   prominently, in the headline, not a footnote.

---

## Working agreement

- **Small, reviewable steps.** One pipeline stage at a time — working and
  tested — before starting the next. No scaffolding five half-finished modules.
- **Tests alongside, not after.** Especially unit conversion, edible-portion
  math, and reconciliation. That is where silent wrongness lives.
- **Only what was asked for.** No unrequested features, endpoints, or config
  options. Propose additions; let the owner decide.
- **Say when unsure.** Guessing at a USDA field name, an API shape, or a store's
  receipt format gets stated as a guess or looked up — never wrapped in
  confident code.
- **Phase 1 only.** Phases 2 and 3 need months of data that does not exist yet.
  Leave the schema room the brief specifies; build nothing else toward them.
- **Push back.** Disagreeing with the brief, the schema, or a request — with
  specific reasoning — is expected.

## Stack

Python 3.12 · FastAPI · SQLModel · PostgreSQL · Alembic (from the first
migration; never `create_all`) · arq + Redis for jobs · React + TypeScript +
Vite + Tailwind · Anthropic API for extraction and resolution · USDA
FoodData Central for nutrition · Docker Compose for local dev.

## Client

Responsive web, phone and laptop both first-class. Capture and correction are
phone-shaped (one-handed, kitchen-counter, keyboard-free where possible);
summaries and the cost-per-nutrient views get real desktop layouts. On mobile
the primary capture path is live camera, not a file picker.

## Non-negotiable engineering rules

- Alembic autogenerate for every schema change. Never edit a landed migration.
- Raw extraction JSON is stored permanently; every pipeline stage after
  `extract` must be replayable from it without re-photographing.
- Ingestion is idempotent on image hash — re-upload updates, never duplicates.
- A receipt that does not reconcile is flagged suspect with a reason, never
  silently persisted as clean.
- Every LLM call logs model, tokens, latency, and cost.
- Secrets from environment only. Receipt images are personal financial
  records — never commit one, never log a path as public.
- `mypy` clean, `ruff` clean, type hints throughout.

## Out of scope

Meal logging. Barcode scanning. Calorie targets or deficits. Weight tracking.
Multi-user accounts or sharing. Native mobile apps.
