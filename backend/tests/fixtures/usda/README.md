# USDA FoodData Central fixtures

Real API payloads, so the parser is tested against the shape the API actually
has rather than the shape I believed it has. That distinction is the whole
point: the two endpoints return nutrients in **different shapes**, and a parser
that only knows one fails silently on the other — it finds no nutrients and
produces a food with no nutrition rather than an error.

| File | Endpoint | Provenance |
|---|---|---|
| `search-chicken-breast.json` | `GET /foods/search` | Captured verbatim, 2026-08-18 |
| `food-2646170-chicken-breast.json` | `GET /food/{id}` | **Reconstructed** — see below |

## What each one is for

`search-chicken-breast.json` is the query `chicken breast, boneless skinless,
raw` and it is a better adversarial set than anything I would have invented.
Among its ten hits: three **cooked** variants of the right food, a **fried,
coated** one, `Chicken, thigh, boneless, skinless, raw`, and the breast meat of
a **Ruffed Grouse** and a **Canada Goose**. The thigh entry scores 0.80 against
this query, against 0.84 for the weakest genuine breast entry — so any cutoff
low enough to admit real matches admits the thigh too. That single fact is why
matching requires every term of the canonical name to appear, rather than
relying on a score threshold.

It also carries both unit spellings (`UG`, `KCAL` uppercase from search) and an
SR Legacy hit with all 129 nutrients, which covers energy at nutrient 1008 and
folate at 1190.

`food-2646170-chicken-breast.json` covers the **nested** detail shape and the
group-heading entries — `Proximates`, `Lipids`, `Minerals`, `Carbohydrates`
arrive as nutrients with no `amount` key at all, and must not be read as zero.
It also covers a Foundation food publishing **no nutrient 1008**, so energy has
to fall back to the Atwater factors.

## Why one file is reconstructed

`DEMO_KEY` hit its shared hourly rate limit partway through capture. This file
is transcribed from the live `GET /food/2646170` response observed on
2026-08-18 — same field names, same nesting, same nutrient ids, units, and
amounts, same four group headings — but it was assembled by hand rather than
written straight from the socket, so it is labelled as such rather than
presented as a capture.

**Replace it with a real capture** once a key is configured:

```
curl -s "https://api.nal.usda.gov/fdc/v1/food/2646170?api_key=$USDA_API_KEY" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin),separators=(",",":")))' \
  > tests/fixtures/usda/food-2646170-chicken-breast.json
```

The tests should pass unchanged. If they do not, the reconstruction was wrong
about something and the test suite has just earned its keep.

A free key takes about a minute: <https://fdc.nal.usda.gov/api-key-signup.html>

## No PII

Unlike the receipt fixtures, these are public reference data with nothing
personal in them.
