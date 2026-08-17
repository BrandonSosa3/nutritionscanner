# Design system

The bar: it should look like a tool someone charges money for. Restrained,
dense where density helps, generous where it doesn't. Nothing decorative.

**Hard rules.** No emoji anywhere in the product. No gradients. No bright or
saturated fills. No drop shadows for decoration. No illustrations, no mascots,
no rounded-everything. Color is used to carry meaning, never to add interest.
If a screen looks plain, that is the correct outcome.

---

## Color

Light theme is the reference; dark theme mirrors the same token roles.

### Neutrals — the entire interface is built from these

| Token | Light | Dark | Use |
|---|---|---|---|
| `bg` | `#FCFCFD` | `#0E0F11` | Page background |
| `surface` | `#FFFFFF` | `#17191C` | Cards, sheets, table rows |
| `surface-sunken` | `#F5F6F7` | `#121416` | Inset areas, code, empty states |
| `border` | `#E5E7EA` | `#26292E` | Hairlines, dividers, input outlines |
| `border-strong` | `#CDD1D6` | `#383C42` | Focused inputs, active selection |
| `text` | `#16181A` | `#F2F3F4` | Primary content |
| `text-secondary` | `#5F646A` | `#A0A5AC` | Labels, captions, metadata |
| `text-tertiary` | `#8B9097` | `#71767D` | Placeholders, disabled |

### Semantic — the only saturated color in the product

Reserved exclusively for resolution state and reconciliation state. Never used
for branding, navigation, or emphasis.

| Token | Light | Dark | Meaning |
|---|---|---|---|
| `resolved` | `#2F6F4E` | `#5BA37D` | Matched to a real food, high confidence |
| `attention` | `#8A6D1F` | `#C9A542` | Unresolved, or low confidence — needs review |
| `error` | `#A33A32` | `#D9736B` | Reconciliation failed, extraction failed |

Each has a `-subtle` background variant at roughly 8% opacity for row tinting.
Never fill a large area with a semantic color — use a 2px left border on the
row, or a small dot, plus text.

### Primary action

Solid near-black (`#16181A`) with white text. Dark theme inverts. There is no
colored primary button anywhere in the product.

---

## Typography

```
--font-ui:   ui-sans-serif, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif
--font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace
```

System stack, deliberately. It renders natively on every device, loads
instantly, and looks correct rather than branded.

| Step | Size / line-height | Weight | Use |
|---|---|---|---|
| `display` | 32 / 38 | 600 | The one number on a summary screen |
| `title` | 22 / 28 | 600 | Screen titles |
| `heading` | 17 / 24 | 600 | Section headings |
| `body` | 15 / 22 | 400 | Default |
| `label` | 13 / 18 | 500 | Field labels, table headers, metadata |
| `caption` | 12 / 16 | 400 | Timestamps, footnotes, counts |

**All numerals are tabular.** `font-variant-numeric: tabular-nums` globally on
anything showing a price, weight, gram figure, or percentage. Prices in a
column must align on the decimal — this is a receipt app; misaligned digits
read as broken.

Money uses `--font-mono` in tables and rankings, `--font-ui` in prose.

Letter-spacing: `-0.01em` on `display` and `title`, `0` everywhere else. No
uppercase tracking, no all-caps headings.

---

## Spacing, radius, elevation

4px base unit. Permitted values only: `4 8 12 16 24 32 48 64`. Nothing else —
if a layout needs 18px, the layout is wrong.

Radius: `6px` default, `8px` for cards and sheets, `9999px` for the two places a
pill is genuinely correct (status chips, filter toggles). Nothing larger.

Elevation: **borders, not shadows.** A `1px solid border` separates surfaces.
The only shadows in the product are on genuinely floating layers — modals,
popovers, and the mobile action bar — and they are near-invisible:
`0 1px 2px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.08)`.

---

## Layout

**Mobile first, desktop deliberate.** Not one layout stretched to fit both.

| Breakpoint | Target |
|---|---|
| `< 640px` | Single column. Capture and review. Bottom-anchored actions. |
| `640–1024px` | Single column, wider gutters, side-by-side form fields. |
| `> 1024px` | Real desktop layouts. Rankings become a dense sortable table. Receipt detail becomes two panes: image left, line items right. |

Max content width `1200px`, centered, `24px` page gutters (`16px` under 640px).

### Mobile specifics

Capture and correction are used one-handed while standing in a kitchen.

- Minimum touch target `44×44px`, no exceptions.
- Primary actions live in a fixed bottom bar within thumb reach, never at the
  top of a scrolled list.
- Respect `env(safe-area-inset-bottom)`.
- The review list is scrollable with the action bar pinned — never a footer that
  requires scrolling to the end of 40 line items to reach.
- Camera capture is the primary path: full-bleed viewfinder, one shutter
  control, framing guides for a long receipt. File selection is a secondary
  link, not a peer button.

---

## Components

**Tables** are the workhorse — rankings, line items, price history. Left-aligned
text, right-aligned numbers, `1px` row separators, no zebra striping, no vertical
rules. Row height 44px mobile, 36px desktop. Sticky header on desktop.

**Buttons.** Primary: solid near-black. Secondary: `1px border`, transparent
fill. Tertiary: text only. Destructive: text in `error`, and it always confirms.
One primary button per screen.

**Inputs.** `1px border`, `6px` radius, `36px` tall, no inner shadow. Focus is a
`2px` ring in `border-strong` — never a colored glow.

**Status chips.** Small pill, `caption` size, `-subtle` background, semantic
text color, optional 6px dot. Used for resolution state on a line item.

**Empty states.** One line of `body` text explaining what will appear here, and
a single action if one exists. No illustration.

**Loading.** Skeleton rows matching the real layout's dimensions. No spinners
except on buttons mid-action. Extraction takes real time — show pipeline stage
progress with named stages, not an indeterminate bar.

---

## Showing uncertainty

Principle 6 is a design requirement, not a copy requirement. It gets specific
treatment:

- Every summary screen carries a **coverage line directly under the headline
  number**, at `label` size in `text-secondary`: *"Covers 62% of spend · 58% of
  lines resolved."* Never a footnote, never a tooltip.
- Unresolved line items render with a 2px `attention` left border and their
  price still shown. Missing nutrition is an em-dash, never a zero.
- Ranking rows show observation count inline: *"n=3 · estimated weight"* in
  `caption`. A ranking built on one estimated purchase must not look identical
  to one built on twelve measured ones.
- Reconciliation failure renders the discrepancy in cents, with the offending
  lines highlighted — never a generic "something went wrong."

---

## Copy

Sentence case everywhere. Title Case Is Not Used. No exclamation marks.

Say *"this week's groceries contained 412g protein"* — never *"you ate"* or
*"you consumed"*. Copy implying intake is a bug (principle 1).

Numbers carry units always: `412 g`, `$3.20 / 100 g protein`, `62%`.

Errors state what happened and what to do: *"Couldn't read the total. The
receipt is saved — retry extraction or enter the total manually."* Never
*"Error"*, never *"Oops"*.

---

## Accessibility

Text contrast ≥ 4.5:1, UI boundaries ≥ 3:1 — the palette above is built to
clear this. Semantic state is never carried by color alone; it always pairs with
text or an icon. Full keyboard navigation on desktop with visible focus rings.
Icons that carry meaning get an accessible label. Respect
`prefers-reduced-motion`.

Motion is functional only: `150ms ease-out` for state changes, `200ms` for
sheets. Nothing animates on load. Nothing bounces.
