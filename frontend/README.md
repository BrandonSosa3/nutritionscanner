# Frontend

React + TypeScript + Vite + Tailwind v4.

```
npm install
npm run dev      # http://localhost:5173, proxies /api to the backend on :8000
npm run build    # type-checks, then bundles
```

The backend must be running (`docker compose up` from the repository root).
In development Vite proxies `/api` to it, so the browser makes no cross-origin
request and CORS never enters the picture locally.

## Design tokens

`src/index.css` holds the tokens, copied verbatim from
[`../docs/DESIGN.md`](../docs/DESIGN.md), which is the source of truth. The
whole interface is neutrals; the three semantic colours are reserved for
resolution and reconciliation state and are used nowhere else. There is no
brand colour and no coloured primary button.

Changing a colour means changing DESIGN.md first.

## Structure

```
src/api/        one typed client, the only place that talks to the backend
src/components/ the small set of primitives every screen uses
src/screens/    Scan · Receipts (→ Review) · Nutrition · Status
```

Navigation is component state rather than a router: three tabs and one
drill-down. `App.tsx` is the single place that changes if deep links or a
browser back button are wanted.
