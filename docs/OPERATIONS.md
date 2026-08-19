# Operations — external services, keys, and what they cost

Everything this project depends on that lives outside the repository, what it
costs, where its spend is visible, and what breaks if it goes away.

**No secret values appear in this file, and none ever should.** It names which
keys exist and where to get them. The values live in `.env`, which is
gitignored and must never be committed.

---

## 1. Accounts and keys

Two external services. Both are needed for the full pipeline; neither is
needed to run the tests, which use recorded payloads.

### Anthropic API — receipt extraction and food resolution

| | |
|---|---|
| **Variable** | `ANTHROPIC_API_KEY` |
| **Get a key** | <https://platform.claude.com/settings/keys> |
| **Check spend** | <https://platform.claude.com/usage> |
| **Set a hard limit** | <https://platform.claude.com/settings/billing> — this is *Billing*, not *Limits*. The Limits page is rate limits, which is a different thing |
| **Cost** | Paid, per token. Measured figures below |
| **Without it** | Uploads still work and images are still stored; extraction returns 503 and the receipt stays queued |

This is **separate from your Claude Code subscription**. Different balance,
different billing page. Work done in the editor does not draw on this key, and
this key does not draw on that balance.

### USDA FoodData Central — nutrition data

| | |
|---|---|
| **Variable** | `USDA_API_KEY` |
| **Get a key** | <https://fdc.nal.usda.gov/api-key-signup.html> — free, instant |
| **Cost** | **Free.** Rate limited per hour |
| **Without it** | Foods keep their identity and show as uncovered mass; enrichment returns 503 |

Responses are cached on disk permanently (`data/usda-cache/`), so re-running
enrichment costs no quota and works offline. `DEMO_KEY` works for a handful of
requests but is shared globally and rate-limits almost immediately — fine for
poking at the API, useless for a real run.

---

## 2. What a receipt actually costs

Measured on real calls, not estimated. Only two stages spend money.

| Stage | Calls per receipt | Measured cost | Notes |
|---|---|---|---|
| `extract` | 1 | **$0.076 – $0.086** | Vision, one photo. ~30 s |
| `resolve` | 1 | **$0.050 – $0.055** | One batched call for the whole basket. ~16 s |
| everything else | 0 | **$0.00** | normalize, reconcile, derive, store matching, USDA |

**About $0.13 per receipt, once.** At one receipt a week that is roughly
**$0.55/month**.

Re-running is free by design: normalize, reconcile, and derive replay from
stored data, and resolution is idempotent — re-resolving after adding a
correction fixes the unresolved lines without paying to re-answer the rest.

`extract` is only re-paid with `?force=true`, which exists for evaluating a
prompt revision against a receipt already on file.

### Evaluation is a separate cost

Eval runs are recorded under stage `eval`, deliberately, so the cost of
*measuring* the resolver stays separable from the cost of *running* it. A run
costs roughly **$0.009 for 2 examples** and scales with the size of the
holdout set. Run it after a prompt change, not on a schedule.

To see the split at any time:

```sql
select stage, count(*), round(sum(cost_usd), 4) from llm_call where ok group by stage;
```

Or `GET /budget` for this month against the ceiling.

---

## 3. Three independent spend guards

Any one of them stops a runaway. They are listed innermost first.

1. **The app's own ceiling** — `MONTHLY_BUDGET_USD` in `.env`, checked in our
   code before every model call against recorded costs for the calendar month.
   A call that would breach it is refused with a clear message and the receipt
   is left queued, never half-processed. `GET /budget` reports the state.
2. **The account credit balance** — nothing can spend money that is not there.
3. **The Console spend limit** — optional, set at platform.claude.com →
   Billing. The hard stop, independent of this codebase entirely.

**A caveat worth remembering:** `MONTHLY_BUDGET_USD` must be listed in
`compose.yaml` to reach the container. The app inside Docker has no `.env` to
read — pydantic-settings looks in the working directory, which is `/app` — so
a value set only in the host `.env` is silently ignored. This bit once: the
file said $5 while the app ran on the $10 default.

---

## 4. Every environment variable

Full template with comments: `.env.example`. Secrets are marked.

| Variable | Secret | Default | What it does |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | — | Extraction and resolution |
| `USDA_API_KEY` | **yes** | — | Nutrition lookup |
| `DATABASE_URL` | contains a password | — | Postgres. Compose supplies its own inside containers |
| `REDIS_URL` | no | — | Job queue |
| `ANTHROPIC_MODEL` | no | `claude-opus-5` | Changing it invalidates eval comparisons |
| `MONTHLY_BUDGET_USD` | no | `10.00` | App-side ceiling. Blank disables the guard |
| `RESOLUTION_MIN_CONFIDENCE` | no | `0.6` | Below this a resolver answer is not an answer |
| `RECONCILIATION_TOLERANCE_CENTS` | no | `2` | Arithmetic slack before a receipt is suspect |
| `RECEIPT_STORAGE_PATH` | no | `data/receipts` | Personal financial records. Gitignored |
| `CORS_ORIGINS` | no | localhost:5173 | Comma-separated. Add the deployed frontend |
| `ENVIRONMENT` | no | `local` | `production` disables the interactive API docs |
| `LOG_LEVEL` / `LOG_JSON` | no | `INFO` / `false` | JSON logs in production |

---

## 5. Services you run yourself

Started by `docker compose up`. No accounts, no cost.

| Service | Image | Host port | Why that port |
|---|---|---|---|
| Postgres | `postgres:16-alpine` | **5433** | Avoids colliding with a Postgres already on 5432 |
| Redis | `redis:7-alpine` | **6380** | Same, for 6379 |
| API | built from `backend/` | 8000 | |
| Worker | built from `backend/` | — | arq, no HTTP port |

Data lives in named Docker volumes (`postgres_data`, `redis_data`,
`receipt_data`). `make reset` destroys them.

---

## 6. Data that is not backed up by anything else

Two things cannot be regenerated and are not in git:

- **Receipt images** (`data/receipts/`) — personal financial records. Never
  commit one.
- **The database** — corrections, labels, and eval history are the compounding
  value of the system. Extraction transcripts are stored permanently, so
  everything downstream can be rebuilt from them without re-photographing;
  losing the database means losing that.

`make backup` exists. **A backup nobody has restored is not a backup** —
testing the restore path is still on the list.

---

## 7. Third-party code

Pinned in `backend/uv.lock`, installed with `uv sync --frozen` so CI, local,
and production resolve identically. Notable runtime packages: `fastapi`,
`uvicorn`, `sqlmodel`, `sqlalchemy`, `asyncpg`, `alembic`, `pydantic`,
`pydantic-settings`, `arq`, `redis`, `anthropic`, `httpx`, `structlog`,
`typer`, `pillow`. Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`,
`mypy`.

The frontend will add its own lockfile when it exists.

### GitHub Actions

Free for public repositories. Runs lint, type checks, migrations, the drift
check, and the full suite against real Postgres and Redis. It has **no API
keys**, deliberately: the one test that makes a real Anthropic call is
deselected there, so no push ever spends money.

---

## 8. A monthly once-over

- `GET /budget` — spend against the ceiling
- platform.claude.com → Usage — the same spend from the other side
- `GET /foods?without_nutrition=true` — the USDA review queue
- `GET /eval/runs` — resolver accuracy, and whether it moved
- Confirm a database backup exists **and restores**
