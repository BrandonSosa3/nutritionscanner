/**
 * The one place that talks to the backend.
 *
 * Errors are turned into messages a person can act on. FastAPI puts its
 * human-readable text in `detail`, and every message the backend writes is
 * already meant to be shown verbatim — "Couldn't read the total. The receipt
 * is saved." — so passing it through beats replacing it with "Request failed".
 */

import type {
  BasketSummary,
  BudgetStatus,
  CorrectionBody,
  FoodList,
  FoodSummary,
  LineItemList,
  NutrientCost,
  ReceiptDetail,
  ReceiptSummary,
  ResolutionResult,
  UploadResponse,
} from "./types";

const BASE = "/api";

export class ApiError extends Error {
  // Declared rather than a constructor parameter property: the project builds
  // with `erasableSyntaxOnly`, which rules out TypeScript syntax that emits
  // runtime code.
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, init);
  } catch {
    throw new ApiError(
      "Can't reach the server. Check it's running, then try again.",
      0,
    );
  }

  if (!response.ok) {
    let detail = `The server returned ${response.status}.`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Body wasn't JSON. The status-based message stands.
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  listReceipts: (limit = 50) =>
    request<{ items: ReceiptSummary[]; total: number }>(
      `/receipts?limit=${limit}`,
    ),

  getReceipt: (id: number) => request<ReceiptDetail>(`/receipts/${id}`),

  upload: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<UploadResponse>("/receipts", { method: "POST", body });
  },

  extract: (id: number) =>
    request<{ receipt_id: number }>(`/receipts/${id}/extract`, {
      method: "POST",
    }),
  normalize: (id: number) =>
    request<{ line_item_count: number }>(`/receipts/${id}/normalize`, {
      method: "POST",
    }),
  reconcile: (id: number) =>
    request<{ reconciliation_status: string; delta_cents: number | null }>(
      `/receipts/${id}/reconcile`,
      { method: "POST" },
    ),
  resolve: (id: number) =>
    request<ResolutionResult>(`/receipts/${id}/resolve`, { method: "POST" }),
  derive: (id: number) =>
    request<{ observations: number }>(`/receipts/${id}/derive`, {
      method: "POST",
    }),

  lines: (id: number) => request<LineItemList>(`/receipts/${id}/lines`),

  correct: (lineItemId: number, body: CorrectionBody) =>
    request<{ applied_to_line_items: number }>(
      `/line-items/${lineItemId}/correct`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
    ),

  confirm: (lineItemId: number) =>
    request<{ eval_example_id: number }>(`/line-items/${lineItemId}/confirm`, {
      method: "POST",
    }),

  summary: (receiptIds?: number[]) => {
    const query = receiptIds?.length
      ? `?${receiptIds.map((id) => `receipt_id=${id}`).join("&")}`
      : "";
    return request<BasketSummary>(`/summary${query}`);
  },

  costPerNutrient: (nutrient = "protein_g") =>
    request<NutrientCost>(`/summary/cost-per-nutrient?nutrient=${nutrient}`),

  budget: () => request<BudgetStatus>("/budget"),

  foods: (withoutNutrition = false) =>
    request<FoodList>(`/foods?without_nutrition=${withoutNutrition}`),

  searchFoods: (query: string) =>
    request<FoodList>(`/foods?limit=20&q=${encodeURIComponent(query)}`),

  createFood: (canonicalName: string) =>
    request<FoodSummary>("/foods", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ canonical_name: canonicalName }),
    }),
};
