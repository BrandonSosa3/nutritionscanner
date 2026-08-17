# Fixture receipts

Five real receipts spanning four countries and five POS formats. Every one of
them breaks an assumption a naive parser would make. Each is paired with a
hand-written `*.expected.json` — the ground truth the extraction stage is
scored against.

**Nothing in this directory may contain personally identifying information.**
Before a receipt image is committed, redact: cardholder name, card digits, auth
and reference numbers, loyalty/membership numbers, and payment-method balances.
Redact by painting over the region — never by cropping alone, since a crop can
be reversed from image metadata. If an image can't be safely redacted, leave it
out of git and reference it from a local-only path.

**Strip EXIF from any camera original.** Phone photos embed GPS coordinates
accurate to a few metres. That data is invisible in an image viewer and would
be committed silently. `05-us-sprouts.jpg` arrived with 15 GPS fields; they
were removed, and it was downscaled to a 2576px long edge — the maximum the
vision model uses anyway, so extraction loses nothing.

**Verify redactions by re-opening the image, not by trusting the coordinates.**
The first redaction pass on `05` looked correct and still leaked a card
fragment and the barcode digits. Both were caught only by looking at the
result.

---

## `01-au-produce.png` — metric weights, ambiguous discount lines

Australian greengrocer, 06/01/2016. Produce-only basket.

| Tests | Detail |
|---|---|
| Metric weighted items | `0.778kg NET @ $5.99/kg` |
| Weight on a **continuation line below** the item | opposite of the Whole Foods layout |
| Standalone `SPECIAL` lines | positive amounts, no item name, **included in subtotal** |
| Basket-level discount | `LOYALTY -15.00` |
| Repeated `SUBTOTAL` lines | three of them, two after the discount |
| No tax line at all | reconciliation must not assume tax exists |

Reconciliation: items sum to exactly `39.20`; `39.20 − 15.00 = 24.20 = TOTAL`.

The `SPECIAL` lines are the interesting failure mode — they *look* like
discounts and are not. A parser that treats them as negative gets `35.28` and
flags a clean receipt as broken. They must resolve as unresolved items with a
known price, never guessed at.

## `02-us-wholefoods.png` — imperial weights, PLU codes, trailing-minus

Whole Foods Market, Sharon Rd.

| Tests | Detail |
|---|---|
| Weight line **above** the item | `1.08 lb @ 1.99 /lb  TARE = .01` |
| `TARE` line | container weight, must be ignored, not parsed as an item |
| PLU codes | `ITEM = 4040` (plums), `ITEM = 94135` (organic gala apples) |
| Trailing-minus negatives | `$2 off (1) WC Fill    2.00-` |
| Truncated 18-char names | `OG LF COTTAGE CHEE`, `NHP SLICED OVEN RO`, `Frozen Mangoes 16o` |
| Prefix + suffix flags | `*`, `*VC`, `*WT`, `WT`, and a trailing `B` tax flag |
| Mixed case | some items title-case, some upper |

`94135` is a live signal — the leading `9` in a 5-digit PLU means organic, which
maps to a different USDA food than the conventional variant.

## `03-za-spar.png` — non-USD, package sizes in the item name, bag fee

SPAR Bergville, South Africa, 23.02.21. Rand.

| Tests | Detail |
|---|---|
| Package size embedded in item text | `125ML`, `80GR`, `500GR`, `1KG`, `375ML`, `400G` |
| Count notation | `1'S` |
| Non-food line | `CARRIER BAG 24L  0.75` |
| VAT-inclusive pricing | `A` = 15% VAT, `*` = 0% VAT, reconciled in a trailing table |
| Metric weighted item | `BANANAS LOOSE 17KG` / `0.596kg @ 15.99 R/kg` |
| Misspelled source text | `PEALED PEACHES` |
| Item count cross-check | `TOTAL FOR 14 ITEMS` |

This is the best receipt in the set for gram extraction: the embedded pack sizes
are directly `grams_basis = per_package` without any LLM estimate. Roughly half
this basket resolves to exact grams from text alone.

`BANANAS LOOSE 17KG` is a trap — `17KG` is a bin or lot code, not the purchase
weight. The real weight is `0.596kg` on the continuation line.

## `04-us-costco.png` — SKU prefixes, quantity multipliers, dual tax rates

Costco Thornton #629, 04/20/2016.

| Tests | Detail |
|---|---|
| Leading SKU numbers | `673919 FF BS BREAST` |
| Quantity multiplier **above** the item | `3 @ 4.29` → `878137 18CT EGGS 12.87` |
| Pound notation in the name | `MONT JACK 2#` |
| Severe abbreviation | `KS DICED TOM`, `CHPD ONION`, `JACKORGSALSA` |
| Two tax rates with letter codes | `A 8.50%` and `E 3.75%`, reconciled separately |
| Non-food | `ECO HALF PAN` (foil pans) |
| Item count cross-check | `TOTAL NUMBER OF ITEMS SOLD = 11` vs 9 printed lines |

Reconciliation: `85.61 + 3.52 = 89.13`. Clean. The item count mismatch (11 sold,
9 lines) is explained entirely by the `3 @` egg line — a good assertion that
quantity parsing worked.

`FF BS BREAST` is the hardest resolution target in the set. `KS` = Kirkland
Signature, a store brand that carries no nutritional meaning and should be
stripped during normalization.

## `05-us-sprouts.jpg` — rotated, crumpled, deposits, multi-buy

Sprouts Farmers Market, San Diego, 2026-08-15. **Photographed sideways and
heavily creased** — the vision stress test.

| Tests | Detail |
|---|---|
| 90° rotation | the model must read it without a client-side orientation fix |
| Heavy creasing across the print | |
| Department section headers | `GROCERY`, `DAIRY` — grouping, not line items |
| CRV bottle deposits | `*CRV FS/TX 05  0.05` — a real charge, not food |
| Multi-buy pricing | `1 @ 2 FOR 6.00` → line charged `3.00` |
| Zero-tax report block | `Tax Report  TAX 1  0.00` |
| EBT / food-stamp payment split | |

Reconciliation: `3.99 + 0.05 + 3.00 + 3.99 + 0.05 = 11.08 = BALANCE DUE`.

**Redacted.** Six full-height black bars cover the card numbers, auth and
reference numbers, EBT balances, cardholder greeting, rewards points, and the
barcode with its printed digits. Because the receipt is rotated, each printed
line is a vertical strip, so bar-shaped redaction is exact rather than
approximate.

Everything the fixture exists to test survives the redaction: rotation,
creasing, department headers, both CRV lines, the multi-buy, the store header,
the purchase date, and the full reconciliation chain.

---

## Coverage summary

| Concern | Covered by |
|---|---|
| Weight line *below* item | 01, 03 |
| Weight line *above* item | 02 |
| Metric (kg) | 01, 03 |
| Imperial (lb, #) | 02, 04 |
| Pack size in item name | 03 |
| Quantity multiplier line | 04 |
| Multi-buy pricing | 05 |
| Discount as negative line | 01, 02 |
| Discount as ambiguous positive line | 01 |
| Basket-level discount | 01 |
| Deposits / fees | 03, 05 |
| No tax line | 01 |
| Single tax line | 02 |
| Multiple tax rates | 03, 04 |
| Non-USD currency | 01 (AUD), 03 (ZAR) |
| PLU codes | 02 |
| SKU prefixes | 04 |
| Truncated item names | 02, 04 |
| Store-brand prefixes | 02 (`OG`, `NHP`), 04 (`KS`) |
| Department headers | 05 |
| Rotated image | 05 |
| Crumpled / degraded print | 05 |
| Item-count cross-check | 03, 04 |

Gap: no receipt here has a **line-level coupon tied to a specific item**
(`BUY 1 GET 1` attached to the line above). Worth adding a sixth fixture when
one turns up.
